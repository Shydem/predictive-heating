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
CONF_OPENTHERM_ENABLED = "opentherm_enabled"
CONF_OPENTHERM_FLOW_TEMP_NUMBER = "opentherm_flow_temp_number"
CONF_MAX_SETPOINT_DELTA = "max_setpoint_delta"

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

# OpenTherm flow temperature defaults
DEFAULT_MIN_FLOW_TEMP = 25.0  # °C — minimum flow temperature
DEFAULT_MAX_FLOW_TEMP = 55.0  # °C — maximum flow temperature (lower = better COP)
DEFAULT_COMFORT_FLOW_TEMP = 40.0  # °C — comfortable middle ground

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
