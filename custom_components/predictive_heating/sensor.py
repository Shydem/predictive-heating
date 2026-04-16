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

from .const import CONF_ROOM_NAME, DOMAIN, STATE_CALIBRATED
from .thermal_model import ThermalModel

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
