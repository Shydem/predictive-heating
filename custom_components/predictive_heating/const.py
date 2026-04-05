"""Constants for the Predictive Heating integration.

Every tunable number lives here with an explanation of what it does
and why it has that default value. No magic numbers anywhere else.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "predictive_heating"

# ─── Config entry keys ────────────────────────────────────────────────────────

CONF_INDOOR_TEMP_ENTITY: Final = "indoor_temp_entity"
CONF_OUTDOOR_TEMP_ENTITY: Final = "outdoor_temp_entity"
CONF_GAS_CONSUMPTION_ENTITY: Final = "gas_consumption_entity"
CONF_TOTAL_ELECTRICITY_ENTITY: Final = "total_electricity_entity"
CONF_HEATPUMP_ELECTRICITY_ENTITY: Final = "heatpump_electricity_entity"
CONF_ELECTRICITY_PRICE_ENTITY: Final = "electricity_price_entity"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_GAS_PRICE: Final = "gas_price"
CONF_HEATING_HOT_WATER_FRACTION: Final = "heating_hot_water_fraction"
CONF_GAS_EFFICIENCY: Final = "gas_efficiency"
CONF_COP_COEFFICIENTS: Final = "cop_coefficients"
CONF_OUTDOOR_ELECTRIC_LOADS_W: Final = "outdoor_electric_loads_w"
CONF_TRAINING_INTERVAL_DAYS: Final = "training_interval_days"
CONF_TRAINING_WINDOW_DAYS: Final = "training_window_days"
CONF_PREDICTION_HORIZON_HOURS: Final = "prediction_horizon_hours"
CONF_OPTIMIZATION_TIMESTEP_MIN: Final = "optimization_timestep_min"
CONF_HEATING_DEVICES: Final = "heating_devices"
CONF_TEMPERATURE_SCHEDULE: Final = "temperature_schedule"

# House profile (cold start estimation)
CONF_HOUSE_FLOOR_AREA_M2: Final = "house_floor_area_m2"
CONF_HOUSE_TYPE: Final = "house_type"
CONF_HOUSE_INSULATION: Final = "house_insulation"
CONF_HOUSE_THERMAL_MASS: Final = "house_thermal_mass_class"

HOUSE_TYPE_DETACHED: Final = "detached"
HOUSE_TYPE_SEMI_DETACHED: Final = "semi_detached"
HOUSE_TYPE_TERRACED: Final = "terraced"
HOUSE_TYPE_APARTMENT: Final = "apartment"

INSULATION_POOR: Final = "poor"       # Pre-1975, label E-G
INSULATION_MODERATE: Final = "moderate"  # 1975-2000, label C-D
INSULATION_GOOD: Final = "good"       # Post-2000, label A-B
INSULATION_EXCELLENT: Final = "excellent"  # Passive-house level

THERMAL_MASS_LIGHT: Final = "light"     # Timber frame, prefab
THERMAL_MASS_MEDIUM: Final = "medium"    # Brick cavity walls
THERMAL_MASS_HEAVY: Final = "heavy"     # Solid brick, concrete floors

CONF_DEVICE_NAME: Final = "name"
CONF_DEVICE_ENTITY: Final = "entity_id"
CONF_DEVICE_TYPE: Final = "device_type"
CONF_DEVICE_SOURCE: Final = "energy_source"
CONF_DEVICE_MAX_OUTPUT_W: Final = "max_output_w"

DEVICE_TYPE_ON_OFF: Final = "on_off"
DEVICE_TYPE_STEPLESS: Final = "stepless"
SOURCE_GAS: Final = "gas"
SOURCE_ELECTRIC: Final = "electric"

# ─── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_HEATING_HOT_WATER_FRACTION: Final = 0.85
"""85% of gas goes to space heating, 15% to hot water."""

DEFAULT_GAS_EFFICIENCY: Final = 0.90
"""Modern condensing boiler: 90-97%. Older boiler: 80-90%."""

DEFAULT_COP_A: Final = 2.8
DEFAULT_COP_B: Final = 0.05
"""COP = A + B * T_outdoor. At 7°C → 3.15, at -5°C → 2.55. COP drops in cold weather."""

DEFAULT_OUTDOOR_ELECTRIC_LOADS_W: Final = 0.0
DEFAULT_GAS_PRICE: Final = 1.0
DEFAULT_TRAINING_INTERVAL_DAYS: Final = 7
DEFAULT_TRAINING_WINDOW_DAYS: Final = 30
DEFAULT_PREDICTION_HORIZON_HOURS: Final = 24
DEFAULT_OPTIMIZATION_TIMESTEP_MIN: Final = 15

DEFAULT_TEMPERATURE_SCHEDULE: Final = {
    "00:00": 17.0,
    "06:00": 20.0,
    "08:00": 19.0,
    "17:00": 20.5,
    "22:00": 18.0,
}

# ─── Optimizer tuning (all in one place) ──────────────────────────────────────

ON_OFF_MIN_DUTY_CYCLE: Final = 0.30
"""On/off devices only turn on if need > 30% of capacity. Prevents short-cycling."""

PREHEAT_PRICE_RATIO: Final = 1.20
"""Preheat if next slot price > 120% of current. Set to 999 to disable."""

PREHEAT_MAX_OVERSHOOT_K: Final = 0.5
"""Max °C above target when preheating."""

COMFORT_PENALTY_WEIGHT: Final = 50.0
"""€/K²/h penalty for being below target. Higher = prioritize comfort."""

INDOOR_ELEC_HEAT_FRACTION: Final = 0.80
"""Fraction of indoor electricity that becomes heat (lights, cooking, PCs)."""

FALLBACK_ELEC_PRICE: Final = 0.30
FALLBACK_OUTDOOR_TEMP: Final = 5.0
FALLBACK_INTERNAL_GAIN_W: Final = 200.0

# ─── Training tuning ─────────────────────────────────────────────────────────

TRAINING_MIN_POINTS: Final = 20
"""Minimum data points to attempt training. 20 × 15min = 5 hours."""

TRAINING_UA_BOUNDS: Final = (10.0, 2000.0)
"""W/K range. 10 = passive house. 2000 = a tent."""

TRAINING_C_BOUNDS: Final = (1.0, 200.0)
"""kWh/K range. 1 = caravan. 200 = massive concrete building."""

TRAINING_MAX_ITER: Final = 5000
TRAINING_INITIAL_UA: Final = 150.0
TRAINING_INITIAL_C: Final = 10.0

# ─── Physical constants ───────────────────────────────────────────────────────

GAS_KWH_PER_M3: Final = 9.769
"""Dutch Groningen-quality natural gas energy content."""

J_PER_KWH: Final = 3_600_000.0

# ─── Platforms & Services ─────────────────────────────────────────────────────

PLATFORMS: Final = ["sensor"]
SERVICE_TRAIN_MODEL: Final = "train_model"
SERVICE_SET_SCHEDULE: Final = "set_schedule"
SERVICE_GET_FORECAST: Final = "get_forecast"
SERVICE_SET_MODEL_PARAMS: Final = "set_model_params"
