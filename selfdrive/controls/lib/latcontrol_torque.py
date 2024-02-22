from collections import deque
import math
import numpy as np

from cereal import log
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.numpy_fast import interp
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, apply_deadzone
from openpilot.selfdrive.controls.lib.latcontrol import LatControl
from openpilot.selfdrive.controls.lib.pid import PIDController
from openpilot.selfdrive.controls.lib.vehicle_model import ACCELERATION_DUE_TO_GRAVITY
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.common.params import Params

# At higher speeds (25+mph) we can assume:
# Lateral acceleration achieved by a specific car correlates to
# torque applied to the steering rack. It does not correlate to
# wheel slip, or to speed.

# This controller applies torque to achieve desired lateral
# accelerations. To compensate for the low speed effects we
# use a LOW_SPEED_FACTOR in the error. Additionally, there is
# friction in the steering wheel that needs to be overcome to
# move it at all, this is compensated for too.

LOW_SPEED_X = [0, 10, 20, 30]
LOW_SPEED_Y = [15, 13, 10, 5]
LOW_SPEED_Y_NN = [12, 4, 1, 0]

def sign(x):
  return 1.0 if x > 0.0 else (-1.0 if x < 0.0 else 0.0)

LAT_PLAN_MIN_IDX = 5
def get_lookahead_value(future_vals, current_val):
  if len(future_vals) == 0:
    return current_val

  same_sign_vals = [v for v in future_vals if sign(v) == sign(current_val)]

  # if any future val has opposite sign of current val, return 0
  if len(same_sign_vals) < len(future_vals):
    return 0.0

  # otherwise return the value with minimum absolute value
  min_val = min(same_sign_vals + [current_val], key=lambda x: abs(x))
  return min_val

# At a given roll, if pitch magnitude increases, the
# gravitational acceleration component starts pointing
# in the longitudinal direction, decreasing the lateral
# acceleration component. Here we do the same thing
# to the roll value itself, then passed to nnff.
def roll_pitch_adjust(roll, pitch):
  return roll * math.cos(pitch)

class LatControlTorque(LatControl):
  def __init__(self, CP, CI):
    super().__init__(CP, CI)
    self.torque_params = CP.lateralTuning.torque
    self.pid = PIDController(self.torque_params.kp, self.torque_params.ki,
                             k_f=self.torque_params.kf, pos_limit=self.steer_max, neg_limit=-self.steer_max)
    self.torque_from_lateral_accel = CI.torque_from_lateral_accel()
    self.use_steering_angle = self.torque_params.useSteeringAngle
    self.steering_angle_deadzone_deg = self.torque_params.steeringAngleDeadzoneDeg

    # Twilsonco's Lateral Neural Network Feedforward
    self.use_nn = CI.has_lateral_torque_nn
    if self.use_nn:
      self.pitch = FirstOrderFilter(0.0, 0.5, 0.01)
      # NN model takes current v_ego, lateral_accel, lat accel/jerk error, roll, and past/future/planned data
      # of lat accel and roll
      # Past value is computed using previous desired lat accel and observed roll
      self.torque_from_nn = CI.get_ff_nn
      self.nn_friction_override = CI.lat_torque_nn_model.friction_override

      # setup future time offsets
      self.nn_time_offset = CP.steerActuatorDelay + 0.2
      future_times = [0.3, 0.6, 1.0, 1.5] # seconds in the future
      self.nn_future_times = [i + self.nn_time_offset for i in future_times]
      self.nn_future_times_np = np.array(self.nn_future_times)

      # setup past time offsets
      self.past_times = [-0.3, -0.2, -0.1]
      history_check_frames = [int(abs(i)*100) for i in self.past_times]
      self.history_frame_offsets = [history_check_frames[0] - i for i in history_check_frames]
      self.lateral_accel_desired_deque = deque(maxlen=history_check_frames[0])
      self.roll_deque = deque(maxlen=history_check_frames[0])
      self.past_future_len = len(self.past_times) + len(self.nn_future_times)

      # Setup adjustable parameters

      # Instantaneous lateral jerk changes very rapidly, making it not useful on its own,
      # however, we can "look ahead" to the future planned lateral jerk in order to guage
      # whether the current desired lateral jerk will persist into the future, i.e.
      # whether it's "deliberate" or not. This lets us simply ignore short-lived jerk.
      # Note that LAT_PLAN_MIN_IDX is defined above and is used in order to prevent
      # using a "future" value that is actually planned to occur before the "current" desired
      # value, which is offset by the steerActuatorDelay.
      self.friction_look_ahead_v = [1.4, 2.0] # how many seconds in the future to look ahead in [0, ~2.1] in 0.1 increments
      self.friction_look_ahead_bp = [9.0, 30.0] # corresponding speeds in m/s in [0, ~40] in 1.0 increments

      # Scaling the lateral acceleration "friction response" could be helpful for some.
      # Increase for a stronger response, decrease for a weaker response.
      self.lat_jerk_friction_factor = 0.4
      self.lat_accel_friction_factor = 0.7 # in [0, 3], in 0.05 increments. 3 is arbitrary safety limit
      

    #ajouatom      
    self.paramsCount = 0
    self.lateralTorqueCustom = int(Params().get("LateralTorqueCustom", encoding="utf8"))
    self.lateralTorqueAccelFactor = float(int(Params().get("LateralTorqueAccelFactor", encoding="utf8")))*0.001
    self.lateralTorqueFriction = float(int(Params().get("LateralTorqueFriction", encoding="utf8")))*0.001


  def update_live_torque_params(self, latAccelFactor, latAccelOffset, friction):
    if self.lateralTorqueCustom > 0: 
      return
    self.torque_params.latAccelFactor = latAccelFactor
    self.torque_params.latAccelOffset = latAccelOffset
    self.torque_params.friction = friction

  def update_params(self):
    self.paramsCount += 1
    if self.paramsCount > 30:
      self.paramsCount = 0
    elif self.paramsCount == 10:
      self.lateralTorqueCustom = Params().get_int("LateralTorqueCustom")
      self.lateralTorqueAccelFactor = float(Params().get_int("LateralTorqueAccelFactor"))*0.001
      self.lateralTorqueFriction = float(Params().get_int("LateralTorqueFriction"))*0.001
      if self.lateralTorqueCustom > 0:
        self.torque_params.latAccelFactor = self.lateralTorqueAccelFactor
        self.torque_params.friction = self.lateralTorqueFriction

  def update(self, active, CS, VM, params, steer_limited, desired_curvature, llk, lat_plan=None, model_data=None):
    self.update_params()
    pid_log = log.ControlsState.LateralTorqueState.new_message()
    nn_log = None

    if not active:
      output_torque = 0.0
      pid_log.active = False
    else:
      if self.use_steering_angle:
        actual_curvature = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
        curvature_deadzone = abs(VM.calc_curvature(math.radians(self.steering_angle_deadzone_deg), CS.vEgo, 0.0))
        if self.use_nn:
          actual_curvature_rate = -VM.calc_curvature(math.radians(CS.steeringRateDeg), CS.vEgo, 0.0)
          actual_lateral_jerk = actual_curvature_rate * CS.vEgo ** 2
      else:
        actual_curvature_vm = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
        actual_curvature_llk = llk.angularVelocityCalibrated.value[2] / CS.vEgo
        actual_curvature = interp(CS.vEgo, [2.0, 5.0], [actual_curvature_vm, actual_curvature_llk])
        curvature_deadzone = 0.0
        actual_lateral_jerk = 0.0
      desired_lateral_accel = desired_curvature * CS.vEgo ** 2

      # desired rate is the desired rate of change in the setpoint, not the absolute desired curvature
      actual_lateral_accel = actual_curvature * CS.vEgo ** 2
      lateral_accel_deadzone = curvature_deadzone * CS.vEgo ** 2

      low_speed_factor = interp(CS.vEgo, LOW_SPEED_X, LOW_SPEED_Y if not self.use_nn else LOW_SPEED_Y_NN)**2
      setpoint = desired_lateral_accel + low_speed_factor * desired_curvature
      measurement = actual_lateral_accel + low_speed_factor * actual_curvature

      if self.use_nn:
        # update past data
        roll = params.roll
        pitch = self.pitch.update(llk.calibratedOrientationNED.value[1])
        roll = roll_pitch_adjust(roll, pitch)

        # compute NNFF error response
        nn_setpoint_input = [CS.vEgo, setpoint, 0.0, roll]
        # past lateral accel error shouldn't count, so use past desired like the setpoint input
        nn_measurement_input = [CS.vEgo, measurement, 0.0, roll]
        torque_from_setpoint = self.torque_from_nn(nn_setpoint_input)
        torque_from_measurement = self.torque_from_nn(nn_measurement_input)
        pid_log.error = torque_from_setpoint - torque_from_measurement

        # compute feedforward (same as nn setpoint output)
        error = setpoint - measurement
        friction_input = error
        nn_input = [CS.vEgo, desired_lateral_accel, friction_input, roll]
        ff = self.torque_from_nn(nn_input)

        # apply friction override for cars with low NN friction response
        if self.nn_friction_override:
          pid_log.error += self.torque_from_lateral_accel(0.0, self.torque_params,
                                            friction_input,
                                            lateral_accel_deadzone, friction_compensation=True)
        nn_log = nn_input + nn_setpoint_input + nn_measurement_input
      else:
        gravity_adjusted_lateral_accel = desired_lateral_accel - params.roll * ACCELERATION_DUE_TO_GRAVITY
        torque_from_setpoint = self.torque_from_lateral_accel(setpoint, self.torque_params, setpoint,
                                                      lateral_accel_deadzone, friction_compensation=False)
        torque_from_measurement = self.torque_from_lateral_accel(measurement, self.torque_params, measurement,
                                                      lateral_accel_deadzone, friction_compensation=False)
        pid_log.error = torque_from_setpoint - torque_from_measurement
        ff = self.torque_from_lateral_accel(gravity_adjusted_lateral_accel, self.torque_params,
                                            desired_lateral_accel - actual_lateral_accel,
                                            lateral_accel_deadzone, friction_compensation=True)

      freeze_integrator = steer_limited or CS.steeringPressed or CS.vEgo < 5
      output_torque = self.pid.update(pid_log.error,
                                      feedforward=ff,
                                      speed=CS.vEgo,
                                      freeze_integrator=freeze_integrator)

      pid_log.active = True
      pid_log.p = self.pid.p
      pid_log.i = self.pid.i
      pid_log.d = self.pid.d
      pid_log.f = self.pid.f
      pid_log.output = -output_torque
      pid_log.actualLateralAccel = actual_lateral_accel
      pid_log.desiredLateralAccel = desired_lateral_accel
      pid_log.saturated = self._check_saturation(self.steer_max - abs(output_torque) < 1e-3, CS, steer_limited)
      if nn_log is not None:
        pid_log.nnLog = nn_log

    # TODO left is positive in this convention
    return -output_torque, 0.0, pid_log