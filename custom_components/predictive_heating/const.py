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
CONF_ELECTRICITY_PRICE_ENTITY: Final = "electricity_price_entity"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_INTERNAL_GAIN_W: Final = "internal_gain_w"
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

INSULATION_POOR: Final = "poor"        # Pre-1975, label E-G
INSULATION_MODERATE: Final = "moderate"   # 1975-2000, label C-D
INSULATION_GOOD: Final = "good"        # Post-2000, label A-B
INSULATION_EXCELLENT: Final = "excellent"  # Passive-house level

THERMAL_MASS_LIGHT: Final = "light"     # Timber frame, prefab
THERMAL_MASS_MEDIUM: Final = "medium"    # Brick cavity walls
THERMAL_MASS_HEAVY: Final = "heavy"     # Solid brick, concrete floors

# Simple heater device config keys
CONF_DEVICE_NAME: Final = "name"
CONF_DEVICE_ENTITY: Final = "entity_id"
CONF_DEVICE_POWER_W: Final = "power_w"
"""Rated heat output in watts when the heater is on.
Start with the nameplate value; the model will account for cycling patterns."""

# ─── Defaults ─────────────────────────────────────────────────────────────────

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
"""On/off heaters only turn on if heat need > 30% of capacity. Prevents short-cycling."""

PREHEAT_PRICE_RATIO: Final = 1.20
"""Preheat if next slot price > 120% of current. Set to 999 to disable."""

PREHEAT_MAX_OVERSHOOT_K: Final = 0.5
"""Max °C above target when preheating."""

DEFAULT_AWAY_TEMP: Final = 15.0
"""Setpoint when heater should not heat (frost prevention)."""

CONF_AWAY_TEMP: Final = "away_temp"
CONF_AUTO_CONTROL: Final = "auto_control"

COMFORT_PENALTY_WEIGHT: Final = 50.0
"""€/K²/h penalty for being below target. Higher = prioritize comfort over cost."""

FALLBACK_ELEC_PRICE: Final = 0.30
FALLBACK_OUTDOOR_TEMP: Final = 5.0
FALLBACK_INTERNAL_GAIN_W: Final = 200.0
"""Constant internal heat gain from occupants, appliances, etc."""

# ─── Gas heating source (Phase 1) ────────────────────────────────────────────

CONF_GAS_CONSUMPTION_ENTITY: Final = "gas_consumption_entity"
"""Cumulative gas meter entity (m³). Used to derive Q_heating when available.
Typically sensor.gas_consumption from the DSMR/P1 integration.
Falls back to heater on/off × power_w when not configured."""

CONF_GAS_EFFICIENCY: Final = "gas_efficiency"
"""Boiler thermal efficiency (0–1). Default 0.90 for a modern HR boiler."""

CONF_TRAINING_USE_CONSTANT_OUTDOOR: Final = "use_constant_outdoor_temp"
"""Phase 1: use mean outdoor temperature as a constant during training.
This simplifies the regression and makes Phase 1 more robust when outdoor
data is sparse. Disable for Phase 2 (time-varying outdoor temp)."""

DEFAULT_GAS_EFFICIENCY: Final = 0.90
GAS_KWH_PER_M3: Final = 9.77
"""Lower heating value of Dutch natural gas in kWh/m³ (Groningen quality)."""

# ─── Training tuning ─────────────────────────────────────────────────────────

TRAINING_MIN_POINTS: Final = 10
"""Minimum data points to attempt training. 10 × 15min = 2.5 hours.
Linear regression works on very few points — even 10 gives a usable result."""

TRAINING_UA_BOUNDS: Final = (10.0, 2000.0)
"""W/K range. 10 = passive house. 2000 = a tent."""

TRAINING_C_BOUNDS: Final = (1.0, 200.0)
"""kWh/K range. 1 = caravan. 200 = massive concrete building."""

TRAINING_INITIAL_UA: Final = 150.0
"""Fallback UA if training degenerates. Reasonable for a medium Dutch house."""

TRAINING_INITIAL_C: Final = 10.0
"""Fallback thermal mass if training degenerates. Medium house with brick walls."""

# Max residual points to store for visualization (covers ~2 days at 15min)
TRAINING_MAX_RESIDUAL_POINTS: Final = 200

# ─── Physical constants ───────────────────────────────────────────────────────

J_PER_KWH: Final = 3_600_000.0

# ─── Platforms & Services ─────────────────────────────────────────────────────

PLATFORMS: Final = ["sensor"]
SERVICE_TRAIN_MODEL: Final = "train_model"
SERVICE_SET_SCHEDULE: Final = "set_schedule"
SERVICE_GET_FORECAST: Final = "get_forecast"
SERVICE_SET_MODEL_PARAMS: Final = "set_model_params"
SERVICE_FORCE_OPTIMIZATION: Final = "force_optimization"
