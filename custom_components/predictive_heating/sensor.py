"""Sensor platform for Predictive Heating.

Exposes model parameters, predictions, and per-device recommendations
as HA sensor entities. Each sensor includes debug attributes showing
the reasoning behind values.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PredictiveHeatingCoordinator

_LOGGER = logging.getLogger(__name__)

MODEL_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="ua_value",
        translation_key="ua_value",
        name="Heat Loss Coefficient (UA)",
        native_unit_of_measurement="W/K",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-minus",
    ),
    SensorEntityDescription(
        key="thermal_mass",
        translation_key="thermal_mass",
        name="Thermal Mass",
        native_unit_of_measurement="kWh/K",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-thermometer",
    ),
    SensorEntityDescription(
        key="predicted_temperature",
        translation_key="predicted_temperature",
        name="Predicted Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-chevron-up",
    ),
    SensorEntityDescription(
        key="estimated_cost_24h",
        translation_key="estimated_cost_24h",
        name="Estimated Heating Cost (24h)",
        native_unit_of_measurement="€",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:currency-eur",
    ),
    SensorEntityDescription(
        key="model_fit_r2",
        translation_key="model_fit_r2",
        name="Model Fit R²",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-bell-curve-cumulative",
    ),
    SensorEntityDescription(
        key="next_training",
        translation_key="next_training",
        name="Next Model Training",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:brain",
    ),
    SensorEntityDescription(
        key="current_target",
        translation_key="current_target",
        name="Current Target Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermostat",
    ),
)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Shared DeviceInfo so all sensors group under one device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Predictive Heating",
        manufacturer="Custom",
        model="Lumped Capacitance Model",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator: PredictiveHeatingCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []

    for desc in MODEL_SENSORS:
        entities.append(ModelSensor(coordinator, desc, entry))

    # Debug trace sensor — shows the full optimization reasoning
    entities.append(DebugTraceSensor(coordinator, entry))

    # Debug visualization sensors for graphs
    entities.append(PredictionHorizonSensor(coordinator, entry))
    entities.append(OptimizationDetailsSensor(coordinator, entry))

    # Per-device sensors
    for device in coordinator.devices:
        entities.append(DeviceSetpointSensor(coordinator, device.name, entry))
        entities.append(DeviceStateSensor(coordinator, device.name, entry))
        entities.append(DeviceOutputSensor(coordinator, device.name, entry))
        entities.append(DeviceHeatSensor(coordinator, device.name, entry))

    async_add_entities(entities)


class ModelSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Sensor for a model parameter or prediction."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator,
        description: SensorEntityDescription, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self.entity_description.key)
        if self.entity_description.key == "next_training" and isinstance(value, str):
            from datetime import datetime
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Show debug info relevant to this specific sensor."""
        if self.coordinator.data is None:
            return None

        key = self.entity_description.key
        data = self.coordinator.data
        attrs: dict[str, Any] = {}

        if key == "ua_value":
            attrs["last_training"] = data.get("last_training")
            attrs["r_squared"] = data.get("model_fit_r2")
            attrs["n_training_points"] = data.get("n_training_points")
            if self.coordinator.last_training_trace:
                attrs["training_trace"] = self.coordinator.last_training_trace

        elif key == "predicted_temperature":
            attrs["current_indoor"] = data.get("t_indoor")
            attrs["current_outdoor"] = data.get("t_outdoor")
            attrs["target"] = data.get("current_target")

        elif key == "estimated_cost_24h":
            # Show cost breakdown by device
            devices = data.get("devices", {})
            for name, info in devices.items():
                attrs[f"{name}_cost_per_wh"] = info.get("cost_per_wh")

        return attrs if attrs else None


class DebugTraceSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Exposes the full optimization trace as attributes.

    State shows a human-readable summary. Attributes contain the
    detailed step-by-step reasoning the optimizer used.
    """

    _attr_has_entity_name = True
    _attr_name = "Decision Trace"
    _attr_icon = "mdi:bug-outline"

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_debug_trace"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """One-line summary of last optimization."""
        if self.coordinator.last_optimize_trace is None:
            return "No optimization run yet"
        trace = self.coordinator.last_optimize_trace
        return (
            f"{trace.get('total_steps', 0)} steps, "
            f"{trace.get('warnings', 0)} warnings, "
            f"{trace.get('elapsed_seconds', 0)}s"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs: dict[str, Any] = {}
        if self.coordinator.last_optimize_trace:
            attrs["optimization"] = self.coordinator.last_optimize_trace
        if self.coordinator.last_training_trace:
            attrs["training"] = self.coordinator.last_training_trace
        return attrs if attrs else None


class DeviceStateSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Shows if a device should be on or off, with the reason why."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator,
        device_name: str, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_state"
        self._attr_name = f"{device_name} Recommended State"
        self._attr_icon = "mdi:fire"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        dev = self.coordinator.data.get("devices", {}).get(self._device_name, {})
        return "on" if dev.get("recommended_state", False) else "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Show WHY the device is on or off."""
        if self.coordinator.data is None:
            return None
        dev = self.coordinator.data.get("devices", {}).get(self._device_name, {})
        return {
            "reason": dev.get("reason", "unknown"),
            "cost_per_wh": dev.get("cost_per_wh"),
            "energy_source": dev.get("energy_source", "unknown"),
        } if dev else None


class DeviceOutputSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Recommended output percentage (0-100%)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator,
        device_name: str, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_output"
        self._attr_name = f"{device_name} Recommended Output"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:gauge"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        dev = self.coordinator.data.get("devices", {}).get(self._device_name, {})
        return dev.get("recommended_output_pct")


class DeviceHeatSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Estimated heat output in Watts."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator,
        device_name: str, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_heat_w"
        self._attr_name = f"{device_name} Heat Output"
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:radiator"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        dev = self.coordinator.data.get("devices", {}).get(self._device_name, {})
        return dev.get("heat_output_w")


class DeviceSetpointSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Recommended thermostat setpoint for this device.

    This is the primary output of the integration — set your thermostat
    to this value and the optimizer handles the rest. Use it in an
    automation or enable auto-control to have it applied automatically.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator,
        device_name: str, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._device_name = device_name
        self._attr_unique_id = f"{entry.entry_id}_{device_name}_setpoint"
        self._attr_name = f"{device_name} Recommended Setpoint"
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:thermostat"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        dev = self.coordinator.data.get("devices", {}).get(self._device_name, {})
        return dev.get("recommended_setpoint")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Show WHY this setpoint was chosen."""
        if self.coordinator.data is None:
            return None
        dev = self.coordinator.data.get("devices", {}).get(self._device_name, {})
        if not dev:
            return None
        data = self.coordinator.data
        return {
            "reason": dev.get("reason", "unknown"),
            "current_target": data.get("current_target"),
            "current_indoor": data.get("t_indoor"),
            "current_outdoor": data.get("t_outdoor"),
            "cost_per_wh": dev.get("cost_per_wh"),
        }


class PredictionHorizonSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Shows the predicted temperature trajectory over the next 24 hours.

    Attributes contain arrays suitable for graphing:
    - timestamps: list of ISO 8601 timestamps
    - predicted_temps: list of predicted indoor temperatures
    - system_state: list of heating system recommendations
    """

    _attr_has_entity_name = True
    _attr_name = "24h Temperature Forecast"
    _attr_icon = "mdi:chart-line"

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_prediction_horizon"
        self._attr_device_info = _device_info(entry)
        self._temperature_history: list[dict] = []

    @property
    def native_value(self) -> str | None:
        """Summary of current forecast."""
        if self.coordinator.last_optimization is None:
            return "No forecast available"
        temps = self.coordinator.last_optimization.predicted_temperatures
        if not temps:
            return "No data"
        final_temp = temps[-1] if temps else None
        current_temp = self.coordinator.data.get("t_indoor") if self.coordinator.data else None
        return f"Currently {current_temp:.1f}°C → Forecast {final_temp:.1f}°C" if final_temp and current_temp else "No forecast"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Detailed forecast arrays for graphing."""
        if self.coordinator.last_optimization is None:
            return None

        attrs: dict[str, Any] = {}

        # Predicted temperatures over time
        temps = self.coordinator.last_optimization.predicted_temperatures
        if temps:
            # Assume 15-minute intervals starting from now
            from datetime import datetime, timedelta
            from homeassistant.util import dt as dt_util
            now = dt_util.now()
            timestamps = [
                (now + timedelta(minutes=i * 15)).isoformat()
                for i in range(len(temps))
            ]
            attrs["timestamps"] = timestamps
            attrs["predicted_temperatures"] = [round(t, 2) for t in temps]

        # Device heating state over forecast window
        if self.coordinator.last_optimization.slot_results:
            system_states = []
            for slot in self.coordinator.last_optimization.slot_results:
                active_devices = []
                for device_decision in slot.device_decisions:
                    if device_decision.output_pct > 0:
                        active_devices.append({
                            "name": device_decision.device_name,
                            "output_pct": round(device_decision.output_pct, 1),
                            "heat_w": round(device_decision.heat_output_w, 0),
                        })
                system_states.append(active_devices if active_devices else None)
            attrs["heating_plan"] = system_states

        # Cost forecast
        attrs["total_cost_24h"] = round(self.coordinator.last_optimization.total_cost, 4)

        return attrs if attrs else None


class OptimizationDetailsSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Detailed optimization behavior for debugging.

    Shows what the optimizer decided and why, including:
    - Comfort vs cost trade-off
    - Energy prices used
    - Thermal load calculations
    """

    _attr_has_entity_name = True
    _attr_name = "Optimization Debug Info"
    _attr_icon = "mdi:bug"

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_optimization_debug"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """One-line status of optimization."""
        if self.coordinator.last_optimization is None:
            return "Pending first optimization"
        opt = self.coordinator.last_optimization
        n_slots = len(opt.slot_results) if opt.slot_results else 0
        return f"Optimized {n_slots} slots, cost €{opt.total_cost:.2f}"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Detailed debug info for analysis."""
        attrs: dict[str, Any] = {}

        if self.coordinator.data:
            data = self.coordinator.data
            attrs["current_indoor_temp"] = data.get("t_indoor")
            attrs["current_outdoor_temp"] = data.get("t_outdoor")
            attrs["target_temperature"] = data.get("current_target")
            attrs["n_training_points"] = data.get("n_training_points")
            attrs["model_fit_r2"] = data.get("model_fit_r2")

        if self.coordinator.last_optimization:
            opt = self.coordinator.last_optimization
            attrs["total_cost_24h"] = round(opt.total_cost, 4)
            attrs["final_predicted_temp"] = (
                round(opt.predicted_temperatures[-1], 2)
                if opt.predicted_temperatures else None
            )
            attrs["n_slots_optimized"] = len(opt.slot_results) if opt.slot_results else 0

        if self.coordinator.last_optimize_trace:
            trace = self.coordinator.last_optimize_trace
            attrs["optimization_trace"] = trace

        return attrs if attrs else None
