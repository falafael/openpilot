from common.numpy_fast import interp
from common.realtime import sec_since_boot
from selfdrive.config import Conversions as CV
from selfdrive.boardd.boardd import can_list_to_can_capnp
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.gm import gmcan
from selfdrive.car.gm.values import CAR, DBC
from selfdrive.can.packer import CANPacker


class CarControllerParams():
  def __init__(self, car_fingerprint):
    if car_fingerprint == CAR.VOLT:
      self.STEER_MAX = 255
      self.STEER_STEP = 2              # how often we update the steer cmd
      self.STEER_DELTA_UP = 7          # ~0.75s time to peak torque (255/50hz/0.75s)
      self.STEER_DELTA_DOWN = 17       # ~0.3s from peak torque to zero
    elif car_fingerprint == CAR.CADILLAC_CT6:
      self.STEER_MAX = 150
      self.STEER_STEP = 1              # how often we update the steer cmd
      self.STEER_DELTA_UP = 2          # 0.75s time to peak torque
      self.STEER_DELTA_DOWN = 5        # 0.3s from peak torque to zero

    self.STEER_DRIVER_ALLOWANCE = 50   # allowed driver torque before start limiting
    self.STEER_DRIVER_MULTIPLIER = 4   # weight driver torque heavily
    self.STEER_DRIVER_FACTOR = 100     # from dbc
    self.NEAR_STOP_BRAKE_PHASE = 0.5 # m/s, more aggressive braking near full stop

    self.ADAS_KEEPALIVE_STEP = 10
    # pedal lookups, only for Volt
    MAX_GAS = 3072              # Only a safety limit
    ZERO_GAS = 2048
    MAX_BRAKE = 350             # Should be around 3.5m/s^2, including regen
    self.MAX_ACC_REGEN = 1404  # ACC Regen braking is slightly less powerful than max regen paddle
    self.GAS_LOOKUP_BP = [-0.25, 0., 0.5]
    self.GAS_LOOKUP_V = [self.MAX_ACC_REGEN, ZERO_GAS, MAX_GAS]
    self.BRAKE_LOOKUP_BP = [-1., -0.25]
    self.BRAKE_LOOKUP_V = [MAX_BRAKE, 0]


def actuator_hystereses(final_pedal, pedal_steady):
  # hyst params... TODO: move these to VehicleParams
  pedal_hyst_gap = 0.01    # don't change pedal command for small oscilalitons within this value

  # for small pedal oscillations within pedal_hyst_gap, don't change the pedal command
  if final_pedal == 0.:
    pedal_steady = 0.
  elif final_pedal > pedal_steady + pedal_hyst_gap:
    pedal_steady = final_pedal - pedal_hyst_gap
  elif final_pedal < pedal_steady - pedal_hyst_gap:
    pedal_steady = final_pedal + pedal_hyst_gap
  final_pedal = pedal_steady

  return final_pedal, pedal_steady


class CarController(object):
  def __init__(self, canbus, car_fingerprint, allow_controls):
    self.pedal_steady = 0.
    self.start_time = sec_since_boot()
    self.chime = 0
    self.lkas_active = False
    self.inhibit_steer_for = 0
    self.steer_idx = 0
    self.apply_steer_last = 0
    self.car_fingerprint = car_fingerprint
    self.allow_controls = allow_controls

    # Setup detection helper. Routes commands to
    # an appropriate CAN bus number.
    self.canbus = canbus
    self.params = CarControllerParams(car_fingerprint)

    self.packer_pt = CANPacker(DBC[car_fingerprint]['pt'])
    self.packer_ch = CANPacker(DBC[car_fingerprint]['chassis'])

  def update(self, sendcan, enabled, CS, frame, actuators, \
             hud_v_cruise, hud_show_lanes, hud_show_car, chime, chime_cnt):
    """ Controls thread """

    # Sanity check.
    if not self.allow_controls:
      return

    P = self.params

    # Send CAN commands.
    can_sends = []
    canbus = self.canbus

    ### STEER ###

    if (frame % P.STEER_STEP) == 0:
      final_steer = actuators.steer if enabled else 0.
      apply_steer = final_steer * P.STEER_MAX

      apply_steer = apply_std_steer_torque_limits(apply_steer, self.apply_steer_last, CS.steer_torque_driver, P)

      lkas_enabled = enabled and not CS.steer_not_allowed and CS.v_ego > 3.

      if not lkas_enabled:
        apply_steer = 0

      self.apply_steer_last = apply_steer
      idx = (frame / P.STEER_STEP) % 4

      if self.car_fingerprint == CAR.VOLT:
        can_sends.append(gmcan.create_steering_control(self.packer_pt,
          canbus.powertrain, apply_steer, idx, lkas_enabled))
      if self.car_fingerprint == CAR.CADILLAC_CT6:
        can_sends += gmcan.create_steering_control_ct6(self.packer_pt,
          canbus, apply_steer, CS.v_ego, idx, lkas_enabled)

    ### GAS/BRAKE ###

    if self.car_fingerprint == CAR.VOLT:
      # no output if not enabled, but keep sending keepalive messages
      # threat pedals as one
      final_pedal = actuators.gas - actuators.brake

      # *** apply pedal hysteresis ***
      final_brake, self.brake_steady = actuator_hystereses(
        final_pedal, self.pedal_steady)

      if not enabled:
        apply_gas = P.MAX_ACC_REGEN  # TODO: do we really need to send max regen when not enabled?
        apply_brake = 0
      else:
        apply_gas = int(round(interp(final_pedal, P.GAS_LOOKUP_BP, P.GAS_LOOKUP_V)))
        apply_brake = int(round(interp(final_pedal, P.BRAKE_LOOKUP_BP, P.BRAKE_LOOKUP_V)))

      # Gas/regen and brakes - all at 25Hz
      if (frame % 4) == 0:
        idx = (frame / 4) % 4

        at_full_stop = enabled and CS.standstill
        near_stop = enabled and (CS.v_ego < P.NEAR_STOP_BRAKE_PHASE)
        can_sends.append(gmcan.create_friction_brake_command(self.packer_ch, canbus.chassis, apply_brake, idx, near_stop, at_full_stop))

        at_full_stop = enabled and CS.standstill
        can_sends.append(gmcan.create_gas_regen_command(self.packer_pt, canbus.powertrain, apply_gas, idx, enabled, at_full_stop))

      # Send dashboard UI commands (ACC status), 25hz
      if (frame % 4) == 0:
        can_sends.append(gmcan.create_acc_dashboard_command(self.packer_pt, canbus.powertrain, enabled, hud_v_cruise * CV.MS_TO_KPH, hud_show_car))

      # Radar needs to know current speed and yaw rate (50hz),
      # and that ADAS is alive (10hz)
      time_and_headlights_step = 10
      tt = sec_since_boot()

      if frame % time_and_headlights_step == 0:
        idx = (frame / time_and_headlights_step) % 4
        can_sends.append(gmcan.create_adas_time_status(canbus.obstacle, int((tt - self.start_time) * 60), idx))
        can_sends.append(gmcan.create_adas_headlights_status(canbus.obstacle))

      speed_and_accelerometer_step = 2
      if frame % speed_and_accelerometer_step == 0:
        idx = (frame / speed_and_accelerometer_step) % 4
        can_sends.append(gmcan.create_adas_steering_status(canbus.obstacle, idx))
        can_sends.append(gmcan.create_adas_accelerometer_speed_status(canbus.obstacle, CS.v_ego, idx))

      # Send ADAS keepalive, 10hz
      if frame % P.ADAS_KEEPALIVE_STEP == 0:
        can_sends += gmcan.create_adas_keepalive(canbus.powertrain)

    # Send chimes
    if self.chime != chime:
      duration = 0x3c

      # There is no 'repeat forever' chime command
      # TODO: Manage periodic re-issuing of chime command
      # and chime cancellation
      if chime_cnt == -1:
        chime_cnt = 10

      if chime != 0:
        can_sends.append(gmcan.create_chime_command(canbus.sw_gmlan, chime, duration, chime_cnt))

      # If canceling a repeated chime, cancel command must be
      # issued for the same chime type and duration
      self.chime = chime

    sendcan.send(can_list_to_can_capnp(can_sends, msgtype='sendcan').to_bytes())
