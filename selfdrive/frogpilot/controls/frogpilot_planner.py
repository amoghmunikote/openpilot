import cereal.messaging as messaging

from cereal import car

from openpilot.common.conversions import Conversions as CV
from openpilot.common.numpy_fast import clip, interp
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL

from openpilot.selfdrive.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.selfdrive.controls.lib.drive_helpers import V_CRUISE_UNSET
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import A_CHANGE_COST, COMFORT_BRAKE, DANGER_ZONE_COST, J_EGO_COST, STOP_DISTANCE, \
                                                                           get_jerk_factor, get_safe_obstacle_distance, get_stopped_equivalence_factor, get_T_FOLLOW
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_MIN, Lead, get_max_accel

from openpilot.selfdrive.frogpilot.controls.lib.conditional_experimental_mode import MODEL_LENGTH, PLANNER_TIME, ConditionalExperimentalMode
from openpilot.selfdrive.frogpilot.controls.lib.frogpilot_functions import MovingAverageCalculator, calculate_lane_width, calculate_road_curvature
from openpilot.selfdrive.frogpilot.controls.lib.frogpilot_variables import CITY_SPEED_LIMIT, CRUISING_SPEED, PROBABILITY

GearShifter = car.CarState.GearShifter

class FrogPilotPlanner:
  def __init__(self):
    self.params_memory = Params("/dev/shm/params")

    self.cem = ConditionalExperimentalMode(self)

    self.model_stopped = False
    self.slower_lead = False
    self.tracking_lead = False

    self.acceleration_jerk = 0
    self.danger_jerk = 0
    self.model_length = 0
    self.road_curvature = 0
    self.speed_jerk = 0
    self.v_cruise = 0

    self.tracking_lead_mac = MovingAverageCalculator()

  def update(self, carState, controlsState, frogpilotCarControl, frogpilotCarState, frogpilotNavigation, modelData, radarState, frogpilot_toggles):
    self.lead_one = radarState.leadOne

    v_cruise = min(controlsState.vCruise, V_CRUISE_UNSET) * CV.KPH_TO_MS
    v_ego = max(carState.vEgo, 0)
    v_lead = self.lead_one.vLead

    driving_gear = carState.gearShifter not in (GearShifter.neutral, GearShifter.park, GearShifter.reverse, GearShifter.unknown)

    lead_distance = self.lead_one.dRel
    stopping_distance = STOP_DISTANCE

    run_cem = frogpilot_toggles.conditional_experimental_mode
    if run_cem and (controlsState.enabled or frogpilotCarControl.alwaysOnLateral) and driving_gear:
      self.cem.update(carState, frogpilotNavigation, modelData, v_ego, v_lead, frogpilot_toggles)

    if v_ego >= frogpilot_toggles.minimum_lane_change_speed:
      self.lane_width_left = calculate_lane_width(modelData.laneLines[0], modelData.laneLines[1], modelData.roadEdges[0])
      self.lane_width_right = calculate_lane_width(modelData.laneLines[3], modelData.laneLines[2], modelData.roadEdges[1])
    else:
      self.lane_width_left = 0
      self.lane_width_right = 0

    self.model_length = modelData.position.x[MODEL_LENGTH - 1]
    self.model_stopped = self.model_length < CRUISING_SPEED * PLANNER_TIME
    self.road_curvature = calculate_road_curvature(modelData, v_ego) if not carState.standstill and driving_gear else 1

    self.set_acceleration(controlsState, frogpilotCarState, v_cruise, v_ego, frogpilot_toggles)
    self.set_follow_values(controlsState, frogpilotCarState, lead_distance, stopping_distance, v_ego, v_lead, frogpilot_toggles)
    self.set_lead_status(lead_distance, stopping_distance, v_ego)
    self.update_v_cruise(carState, controlsState, frogpilotCarState, frogpilotNavigation, modelData, v_cruise, v_ego, frogpilot_toggles)

  def set_acceleration(self, controlsState, frogpilotCarState, v_cruise, v_ego, frogpilot_toggles):
    if controlsState.experimentalMode:
      self.max_accel = ACCEL_MAX
    else:
      self.max_accel = get_max_accel(v_ego)

    if controlsState.experimentalMode:
      self.min_accel = ACCEL_MIN
    else:
      self.min_accel = A_CRUISE_MIN

  def set_follow_values(self, controlsState, frogpilotCarState, lead_distance, stopping_distance, v_ego, v_lead, frogpilot_toggles):
    self.base_acceleration_jerk, self.base_danger_jerk, self.base_speed_jerk = get_jerk_factor(
      frogpilot_toggles.aggressive_jerk_acceleration, frogpilot_toggles.aggressive_jerk_danger, frogpilot_toggles.aggressive_jerk_speed,
      frogpilot_toggles.standard_jerk_acceleration, frogpilot_toggles.standard_jerk_danger, frogpilot_toggles.standard_jerk_speed,
      frogpilot_toggles.relaxed_jerk_acceleration, frogpilot_toggles.relaxed_jerk_danger, frogpilot_toggles.relaxed_jerk_speed,
      frogpilot_toggles.custom_personalities, controlsState.personality
    )

    self.t_follow = get_T_FOLLOW(
      frogpilot_toggles.aggressive_follow,
      frogpilot_toggles.standard_follow,
      frogpilot_toggles.relaxed_follow,
      frogpilot_toggles.custom_personalities, controlsState.personality
    )

    if self.tracking_lead:
      self.update_follow_values(lead_distance, stopping_distance, v_ego, v_lead, frogpilot_toggles)
    else:
      self.acceleration_jerk = self.base_acceleration_jerk
      self.danger_jerk = self.base_danger_jerk
      self.speed_jerk = self.base_speed_jerk

  def set_lead_status(self, lead_distance, stopping_distance, v_ego):
    following_lead = self.lead_one.status and 1 < lead_distance < self.model_length + stopping_distance
    following_lead &= v_ego > CRUISING_SPEED or self.tracking_lead

    self.tracking_lead_mac.add_data(following_lead)
    self.tracking_lead = self.tracking_lead_mac.get_moving_average() >= PROBABILITY

  def update_follow_values(self, lead_distance, stopping_distance, v_ego, v_lead, frogpilot_toggles):
    # Offset by FrogAi for FrogPilot for a more natural approach to a slower lead
    if frogpilot_toggles.conditional_slower_lead and v_lead < v_ego:
      distance_factor = max(lead_distance - (v_lead * self.t_follow), 1)
      braking_offset = clip((v_ego - v_lead) - COMFORT_BRAKE, 1, distance_factor)
      self.slower_lead = braking_offset > 1

  def update_v_cruise(self, carState, controlsState, frogpilotCarState, frogpilotNavigation, modelData, v_cruise, v_ego, frogpilot_toggles):
    v_cruise_cluster = max(controlsState.vCruiseCluster, v_cruise) * CV.KPH_TO_MS
    v_cruise_diff = v_cruise_cluster - v_cruise

    v_ego_cluster = max(carState.vEgoCluster, v_ego)
    v_ego_diff = v_ego_cluster - v_ego

    targets = []
    self.v_cruise = float(min([target if target > CRUISING_SPEED else v_cruise for target in targets]))

  def publish(self, sm, pm, frogpilot_toggles):
    frogpilot_plan_send = messaging.new_message('frogpilotPlan')
    frogpilot_plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState'])
    frogpilotPlan = frogpilot_plan_send.frogpilotPlan

    frogpilotPlan.accelerationJerk = float(A_CHANGE_COST * self.acceleration_jerk)
    frogpilotPlan.accelerationJerkStock = float(A_CHANGE_COST * self.base_acceleration_jerk)
    frogpilotPlan.dangerJerk = float(DANGER_ZONE_COST * self.danger_jerk)
    frogpilotPlan.speedJerk = float(J_EGO_COST * self.speed_jerk)
    frogpilotPlan.speedJerkStock = float(J_EGO_COST * self.base_speed_jerk)
    frogpilotPlan.tFollow = float(self.t_follow)

    frogpilotPlan.conditionalExperimentalActive = self.cem.experimental_mode

    frogpilotPlan.laneWidthLeft = self.lane_width_left
    frogpilotPlan.laneWidthRight = self.lane_width_right

    frogpilotPlan.maxAcceleration = float(self.max_accel)
    frogpilotPlan.minAcceleration = float(self.min_accel)

    frogpilotPlan.vCruise = self.v_cruise

    pm.send('frogpilotPlan', frogpilot_plan_send)