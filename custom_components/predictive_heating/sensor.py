"""
Sensor platform for Predictive Heating.

Exposes diagnostic sensors so you can monitor the thermal model's
learning progress and predictions in the HA dashboard.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ROOM_NAME, DOMAIN, STATE_CALIBRATED, UPDATE_INTERVAL
from .thermal_model import ThermalModel

# Normalisation factor: EKF innovations are in °C per UPDATE_INTERVAL tick.
# Multiply by this to get a human-readable °C/h rate.
_DT_TO_PER_HOUR = 3600.0 / UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    model: ThermalModel = data["model"]
    room_name = data["config"][CONF_ROOM_NAME]

    sensors = [
        ThermalModelStateSensor(entry, model, room_name),
        HeatLossCoefficientSensor(entry, model, room_name),
        LearningProgressSensor(entry, model, room_name),
        MeanPredictionErrorSensor(entry, model, room_name),
        HeatingPowerSensor(entry, model, room_name, data),
        SpikeStateSensor(entry, model, room_name, data),
    ]

    async_add_entities(sensors)


class ThermalModelStateSensor(SensorEntity):
    """Shows whether the thermal model is learning or calibrated."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer-lines"

    def __init__(
        self, entry: ConfigEntry, model: ThermalModel, room_name: str
    ) -> None:
        self._model = model
        self._attr_unique_id = f"predictive_heating_{entry.entry_id}_model_state"
        self._attr_name = f"{room_name} Thermal Model"

    @property
    def native_value(self) -> str:
        return self._model.state

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "idle_samples": self._model.idle_count,
            "active_samples": self._model.active_count,
            "is_calibrated": self._model.state == STATE_CALIBRATED,
        }


class HeatLossCoefficientSensor(SensorEntity):
    """Shows the learned heat loss coefficient (W/K)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:heat-wave"
    _attr_native_unit_of_measurement = "W/K"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, entry: ConfigEntry, model: ThermalModel, room_name: str
    ) -> None:
        self._model = model
        self._attr_unique_id = (
            f"predictive_heating_{entry.entry_id}_heat_loss"
        )
        self._attr_name = f"{room_name} Heat Loss Coefficient"

    @property
    def native_value(self) -> float:
        return round(self._model.params.heat_loss_coeff, 1)


class LearningProgressSensor(SensorEntity):
    """Shows thermal model learning progress as a percentage."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:school"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, entry: ConfigEntry, model: ThermalModel, room_name: str
    ) -> None:
        self._model = model
        self._attr_unique_id = (
            f"predictive_heating_{entry.entry_id}_learning_progress"
        )
        self._attr_name = f"{room_name} Learning Progress"

    @property
    def native_value(self) -> int:
        from .const import MIN_IDLE_SAMPLES, MIN_ACTIVE_SAMPLES

        idle_pct = min(100, self._model.idle_count / MIN_IDLE_SAMPLES * 100)
        active_pct = min(
            100, self._model.active_count / MIN_ACTIVE_SAMPLES * 100
        )
        return int((idle_pct + active_pct) / 2)


class MeanPredictionErrorSensor(SensorEntity):
    """Rolling average |observed-predicted| dT, expressed as °C/h."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:chart-bell-curve"
    # Displayed as a rate (°C/h) — no HA device_class maps to this.
    _attr_native_unit_of_measurement = "°C/h"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, entry: ConfigEntry, model: ThermalModel, room_name: str
    ) -> None:
        self._model = model
        self._attr_unique_id = (
            f"predictive_heating_{entry.entry_id}_prediction_error"
        )
        self._attr_name = f"{room_name} Model Prediction Error"

    @property
    def native_value(self) -> float | None:
        mpe = self._model.mean_prediction_error
        if mpe == float("inf"):
            return None
        # Convert from °C/tick to °C/h for human readability.
        return round(mpe * _DT_TO_PER_HOUR, 3)


class HeatingPowerSensor(SensorEntity):
    """Current thermal watts being delivered to the room (gas-meter derived)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:fire"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, entry: ConfigEntry, model: ThermalModel, room_name: str, data: dict
    ) -> None:
        self._model = model
        self._room_data = data
        self._attr_unique_id = (
            f"predictive_heating_{entry.entry_id}_heat_power_w"
        )
        self._attr_name = f"{room_name} Heat Input Power"

    @property
    def native_value(self) -> float | None:
        # The climate entity stores the last measured heat-source reading
        # on the model for persistence; prefer the live value on hass.data.
        source = self._room_data.get("heat_source") if self._room_data else None
        if source is not None:
            try:
                return round(source.current_power_w(), 1)
            except Exception:  # noqa: BLE001
                pass
        # Fallback: the EKF-averaged estimate in params.
        return round(self._model.params.heating_power, 1)


class SpikeStateSensor(SensorEntity):
    """Whether the heat source is currently flagged as a cooking/DHW spike."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:stove"

    def __init__(
        self, entry: ConfigEntry, model: ThermalModel, room_name: str, data: dict
    ) -> None:
        self._model = model
        self._room_data = data
        self._attr_unique_id = (
            f"predictive_heating_{entry.entry_id}_spike_state"
        )
        self._attr_name = f"{room_name} Gas Spike State"

    @property
    def native_value(self) -> str:
        source = self._room_data.get("heat_source") if self._room_data else None
        if source is None:
            return "unknown"
        return "spike" if source.in_spike else "idle"

    @property
    def extra_state_attributes(self) -> dict:
        source = self._room_data.get("heat_source") if self._room_data else None
        if source is None:
            return {}
        return {
            "spike_events": source.spike_events,
            "raw_power_w": round(source.raw_power_w(), 1),
            "effective_power_w": round(source.current_power_w(), 1),
        }
