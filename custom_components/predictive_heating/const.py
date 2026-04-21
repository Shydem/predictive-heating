"""Constants for Predictive Heating integration."""

DOMAIN = "predictive_heating"

# Configuration keys
CONF_ROOM_NAME = "room_name"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_OUTDOOR_TEMPERATURE_SENSOR = "outdoor_temperature_sensor"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_WINDOW_SENSORS = "window_sensors"
CONF_HEAT_PUMP_COP_SENSOR = "heat_pump_cop_sensor"
CONF_HEATING_ZONE = "heating_zone"
CONF_MAX_SETPOINT_DELTA = "max_setpoint_delta"

# Multi-room thermal coupling:
# list of ``{neighbour_entry_id: str, u_value: float, enabled: bool}``
# describing heat exchange between this room and another predictive-heating
# room. The coupling term is ``U*(T_neighbour - T_room)``, added to the
# standard loss equation. The optimizer/EKF only fits coupling for pairs
# that are marked enabled. Defaults to an empty list — rooms are isolated
# unless explicitly coupled.
CONF_THERMAL_COUPLINGS = "thermal_couplings"
# Default U-value (W/K) for an internal partition with a closed door —
# glazed interior doors ≈ 20–40 W/K, solid doors ≈ 10–15 W/K.
DEFAULT_COUPLING_U = 20.0

# Gas / heat-source modelling (v0.3)
CONF_GAS_METER_SENSOR = "gas_meter_sensor"
CONF_BOILER_EFFICIENCY = "boiler_efficiency"
CONF_GAS_CALORIFIC_VALUE = "gas_calorific_value_mj_m3"
CONF_HEAT_SHARE = "heat_share"

# Setpoint nudge control
CONF_NUDGE_STEP = "nudge_step"
CONF_NUDGE_INTERVAL_MIN = "nudge_interval_min"

# Schedule support — the user points us at a `schedule.*` entity and
# we follow it. The schedule's state is interpreted as a *mode selector*
# (``preset`` attribute wins, then the raw on/off), and the actual
# temperature for that mode is taken from the per-room preset number
# entities (see below). This avoids the old conflict where
# ``schedule_on_temp`` could disagree with the comfort preset setpoint.
CONF_SCHEDULE_ENTITY = "schedule_entity"
# Legacy keys, kept for back-compat with existing config entries. When
# present and no preset number entity matches, we still honour them.
CONF_SCHEDULE_ON_TEMP = "schedule_on_temp"
CONF_SCHEDULE_OFF_TEMP = "schedule_off_temp"
# Which preset to use when the schedule is ON / OFF. Defaults keep old
# behaviour (on→comfort, off→eco). Any of the five preset names works.
CONF_SCHEDULE_ON_PRESET = "schedule_on_preset"
CONF_SCHEDULE_OFF_PRESET = "schedule_off_preset"
DEFAULT_SCHEDULE_ON_PRESET = "comfort"
DEFAULT_SCHEDULE_OFF_PRESET = "eco"
# Default "vacation" preset — separate from away, for extended absences
# where you want a deeper setback without losing the distinction in the UI.
DEFAULT_VACATION_TEMP = 12.0

# Override: when an input_boolean / switch attached here is ``on`` the
# integration treats the room as "occupied / comfort", bypassing
# presence-away and schedule-off logic. Used for WFH scenarios.
CONF_OVERRIDE_ENTITY = "override_entity"
# Optional presence sensor (binary_sensor) whose "on" state forces
# comfort the same way an override does (e.g. a motion-based
# occupancy sensor). Distinct from CONF_PERSON_ENTITIES which is a
# global home/away signal.
CONF_OCCUPANCY_ENTITY = "occupancy_entity"

# Room-size / building-type bootstrap (v0.2 ROADMAP item)
CONF_FLOOR_AREA_M2 = "floor_area_m2"
CONF_CEILING_HEIGHT_M = "ceiling_height_m"
CONF_BUILDING_TYPE = "building_type"

DEFAULT_CEILING_HEIGHT_M = 2.6

# Building-type presets used to seed the EKF with a reasonable starting point.
# These are rough Dutch-housing-stock ballparks — the EKF will correct them
# as observations come in. Each preset gives:
#   u_per_m2_floor: W / (K * m² of floor area) — heat loss to outside
#   vol_heat_capacity: kJ / (K * m³ of room volume) — thermal mass
BUILDING_TYPES: dict[str, dict[str, float]] = {
    "poor_insulation": {  # pre-1975, single glass, no wall insulation
        "u_per_m2_floor": 5.0,
        "vol_heat_capacity": 80.0,
    },
    "moderate_insulation": {  # 1975–2000, double glass, partial insulation
        "u_per_m2_floor": 3.0,
        "vol_heat_capacity": 70.0,
    },
    "good_insulation": {  # post-2000, HR++ glass, wall insulation
        "u_per_m2_floor": 2.0,
        "vol_heat_capacity": 60.0,
    },
    "passive_house": {  # triple glass, heavily insulated, near-passive
        "u_per_m2_floor": 0.8,
        "vol_heat_capacity": 50.0,
    },
}
DEFAULT_BUILDING_TYPE = "moderate_insulation"

# Thermal model defaults
DEFAULT_HEAT_LOSS_COEFFICIENT = 150.0  # W/K — total heat loss per kelvin delta
DEFAULT_THERMAL_MASS = 5000.0  # kJ/K — thermal inertia of the room
DEFAULT_SOLAR_GAIN_FACTOR = 0.3  # fraction of solar irradiance that reaches the room
DEFAULT_HEATING_POWER = 5000.0  # W — max output of the heating system for this room

# Control defaults
DEFAULT_COMFORT_TEMP = 21.0
DEFAULT_ECO_TEMP = 18.0
DEFAULT_AWAY_TEMP = 15.0
DEFAULT_SLEEP_TEMP = 18.5
DEFAULT_BOOST_TEMP = 24.0
DEFAULT_HYSTERESIS = 0.3  # degrees C
# When the room is in an "off" preset (no heat demand) we *still* send
# the preset target to the underlying thermostat so it can modulate
# itself instead of slamming shut. The min is a floor — we never send
# below this even if a preset is configured lower (keeps anti-frost).
DEFAULT_IDLE_MIN_TEMP = 10.0
# When the window is open we drop to this floor setpoint; it's only
# used as a short-term "boiler off" signal.
DEFAULT_WINDOW_OPEN_TEMP = 5.0
# Max degrees above target we'll ever push the setpoint to. Kept small so
# OpenTherm modulation keeps working — an OpenTherm thermostat reads a
# large (setpoint − measured) gap as "run hot water hard", which causes
# the overshoot we're trying to avoid.
DEFAULT_MAX_SETPOINT_DELTA = 1.0
# How much to step the thermostat setpoint per adjustment. Intentionally
# small to keep the thermostat close to target so the OpenTherm curve
# can modulate the boiler properly.
DEFAULT_NUDGE_STEP = 0.5  # °C
# Minimum time between setpoint changes, so we don't whipsaw the boiler
# while the room is responding to the last adjustment.
DEFAULT_NUDGE_INTERVAL_MIN = 10  # minutes
# Error bands for deciding whether to nudge up / down / hold.
NUDGE_COLD_BAND = 0.3   # °C: below target − this → nudge up
NUDGE_WARM_BAND = 0.3   # °C: above target + this → nudge down

# ── Predictive pre-heat + MPC (v0.3) ──────────────────────────────
#
# Weather entity: a HA `weather.*` entity supplying hourly forecast
# temperatures, used by the pre-heat planner to compute lead time.
CONF_WEATHER_ENTITY = "weather_entity"
# Person entities: any number of `person.*` entities. When everyone is
# away for `CONF_AWAY_GRACE_MIN` minutes the room auto-switches to the
# Away preset; when anyone comes home the previous preset is restored.
CONF_PERSON_ENTITIES = "person_entities"
CONF_AWAY_GRACE_MIN = "away_grace_min"
# Comfort ramp: "instant" snaps the target up at the start of the
# pre-heat window; "gradual" linearly ramps it over the window so the
# room warms smoothly and the MPC has a rising setpoint to track.
CONF_COMFORT_RAMP = "comfort_ramp"
# Master switch for the MPC. When disabled we fall back to the v0.1
# hysteresis controller.
CONF_MPC_ENABLED = "mpc_enabled"
# MPC horizon and granularity. Longer horizon = more anticipation but
# the search space grows quadratically with N.
CONF_MPC_HORIZON_MIN = "mpc_horizon_min"
CONF_MPC_STEP_MIN = "mpc_step_min"
# Transport delay on the boiler / radiator circuit. This is the
# single most important MPC parameter for overshoot prevention —
# increase it if you still see overshoot despite MPC being active.
CONF_MPC_CONTROL_DELAY_MIN = "mpc_control_delay_min"

DEFAULT_COMFORT_RAMP = "gradual"  # or "instant"
DEFAULT_AWAY_GRACE_MIN = 10
DEFAULT_MPC_ENABLED = True
DEFAULT_MPC_HORIZON_MIN = 60
DEFAULT_MPC_STEP_MIN = 5
DEFAULT_MPC_CONTROL_DELAY_MIN = 5
COMFORT_RAMP_OPTIONS = ("gradual", "instant")

# Gas / heat source
# Dutch Groningen-gas upper calorific value in MJ/m³ (typical billed value).
DEFAULT_GAS_CALORIFIC_VALUE = 35.17
# Typical HR-107 (condensing) boiler seasonal efficiency.
DEFAULT_BOILER_EFFICIENCY = 0.95
# Fraction of boiler heat allocated to this room. Default 1.0 so a user
# with one configured room (or one representative "main" room) gets all
# the heat attributed. For zones with multiple rooms, set so the total
# across rooms ≈ 1.0.
DEFAULT_HEAT_SHARE = 1.0
# Ignore gas-meter derivatives that correspond to <2 minutes of data —
# these are usually noise when the meter ticks over a full unit.
MIN_GAS_DT_SECONDS = 60

# Cooking / shower spike rejection:
#   A gas-usage spike that is NOT accompanied by a matching rise in room
#   temperature is almost always a DHW event (shower) or a hob burner
#   (cooking). We freeze the heat-power input to the EKF during these
#   events so they don't pollute H / P_heat estimates.
#
#   Heuristic:
#     * Watch the rolling (5 min) average dT / gas_power.
#     * If gas_power > SPIKE_POWER_W for SPIKE_WINDOW_S seconds but the
#       room has warmed less than SPIKE_EXPECTED_DT_RATIO × predicted
#       dT during that window → mark the gas power as "non-heating".
#     * The event auto-expires after MAX_SPIKE_DURATION_S.
SPIKE_POWER_W = 6000.0          # watts — typical shower / oven demand
SPIKE_EXPECTED_DT_RATIO = 0.3   # require ≥30% of predicted dT to credit it as heating
SPIKE_WINDOW_S = 5 * 60         # observation window
MAX_SPIKE_DURATION_S = 30 * 60  # never hold longer than this

# Prediction history buffer: every periodic update records the model's
# 8h-ahead forecast so the dashboard can overlay "prediction from 8h ago"
# vs actual temperatures. Size is one point per update, so ~12/h × 24h.
PREDICTION_HISTORY_MAX = 350
PREDICTION_HORIZON_HOURS = 8.0

# Thermal model states
STATE_LEARNING = "learning"
STATE_CALIBRATED = "calibrated"

# Minimum samples before the model is considered calibrated
MIN_IDLE_SAMPLES = 40
MIN_ACTIVE_SAMPLES = 15

# Update interval in seconds
UPDATE_INTERVAL = 120  # 2 minutes

# Platforms
PLATFORMS = ["climate", "sensor", "number", "switch", "button"]
