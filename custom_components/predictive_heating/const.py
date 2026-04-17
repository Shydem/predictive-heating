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
DEFAULT_HYSTERESIS = 0.3  # degrees C
DEFAULT_MAX_SETPOINT_DELTA = 2.5  # max degrees above target to send to thermostat

# Thermal model states
STATE_LEARNING = "learning"
STATE_CALIBRATED = "calibrated"

# Minimum samples before the model is considered calibrated
MIN_IDLE_SAMPLES = 40
MIN_ACTIVE_SAMPLES = 15

# Update interval in seconds
UPDATE_INTERVAL = 120  # 2 minutes

# Platforms
PLATFORMS = ["climate", "sensor"]
