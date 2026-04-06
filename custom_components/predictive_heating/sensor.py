"""Sensor platform for Predictive Heating.

Exposes model parameters, predictions, per-device recommendations,
and training visualization data as HA sensor entities.
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

    # Visualization sensors
    entities.append(TrainingVisualizationSensor(coordinator, entry))
    entities.append(PredictionHorizonSensor(coordinator, entry))
    entities.append(DebugTraceSensor(coordinator, entry))

    # Per-heater sensors
    for heater in coordinator.heaters:
        entities.append(HeaterStateSensor(coordinator, heater.name, entry))
        entities.append(HeaterSetpointSensor(coordinator, heater.name, entry))
        entities.append(HeaterHeatSensor(coordinator, heater.name, entry))

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
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(value)
                # Ensure timezone-aware for HA TIMESTAMP sensor
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None

        key = self.entity_description.key
        data = self.coordinator.data
        attrs: dict[str, Any] = {}

        if key == "ua_value":
            attrs["last_training"] = data.get("last_training")
            attrs["r_squared"] = data.get("model_fit_r2")
            attrs["n_training_points"] = data.get("n_training_points")

        elif key == "predicted_temperature":
            attrs["current_indoor"] = data.get("t_indoor")
            attrs["current_outdoor"] = data.get("t_outdoor")
            attrs["target"] = data.get("current_target")

        return attrs if attrs else None


class TrainingVisualizationSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Exposes training residuals and scatter data for graphs.

    This is the key sensor for understanding how well the model learned:

    Attributes:
      residuals: [{ts, measured, predicted, error}] — overlay measured vs predicted.
        Plot measured and predicted on the same time axis to see fit quality.

      scatter: [{t_outdoor, delta_t_per_h, heating_on}] — shows what the model
        learned from. X=outdoor temp, Y=indoor temp change per hour, color=heater on.
        A good model separates heating_on vs off clearly.

      param_history: current UA and C with interpretation guidance.
    """

    _attr_has_entity_name = True
    _attr_name = "Training Visualization"
    _attr_icon = "mdi:chart-scatter-plot"

    def __init__(
        self, coordinator: PredictiveHeatingCoordinator, entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_training_viz"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str:
        """Summary of training quality."""
        r2 = self.coordinator.params.r_squared
        n = self.coordinator.params.n_data_points
        if n == 0:
            return "Not trained yet"
        quality = "excellent" if r2 > 0.85 else "good" if r2 > 0.7 else "poor"
        return f"R²={r2:.3f} ({quality}), {n} points"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Graph data for ApexCharts.

        residuals  → area/line chart: overlay measured vs predicted temperature
        scatter    → scatter chart: outdoor temp vs delta-T, colored by heater state
        """
        params = self.coordinator.params
        attrs: dict[str, Any] = {
            "ua_w_per_k": round(params.ua, 1),
            "thermal_mass_kwh_per_k": round(params.thermal_mass, 1),
            "r_squared": round(params.r_squared, 4),
            "n_training_points": params.n_data_points,
            "last_trained": (
                params.last_trained.isoformat() if params.last_trained else None
            ),
        }

        # Residuals: measured vs predicted over time
        # Store only arrays (not full dicts) to stay under 16KB HA limit
        residuals = self.coordinator.last_training_residuals
        if residuals:
            # Extract arrays for ApexCharts — this is more efficient than storing full dicts
            attrs["residuals_timestamps"] = [r["ts"] for r in residuals]
            attrs["residuals_measured"] = [r["measured"] for r in residuals]
            attrs["residuals_predicted"] = [r["predicted"] for r in residuals]
            attrs["residuals_errors"] = [r["error"] for r in residuals]
            # RMSE summary
            errors = [r["error"] for r in residuals]
            rmse = (sum(e ** 2 for e in errors) / len(errors)) ** 0.5
            attrs["rmse_k"] = round(rmse, 3)

        return attrs


class PredictionHorizonSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """24-hour temperature forecast with heating plan.

    Attributes contain arrays suitable for graphing with ApexCharts:
    - predicted_temps: predicted indoor temperature per slot
    - target_temps: target temperature per slot
    - outdoor_temps: outdoor temperature per slot
    - heating_plan: heater on/off per slot (for area fill)
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

    @property
    def native_value(self) -> str | None:
        if self.coordinator.last_optimization is None:
            return "No forecast available"
        temps = self.coordinator.last_optimization.predicted_temperatures
        if not temps:
            return "No data"
        current = self.coordinator.data.get("t_indoor") if self.coordinator.data else None
        return f"{current:.1f}°C → {temps[-1]:.1f}°C" if current and temps else "No forecast"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.last_optimization is None:
            return None

        opt = self.coordinator.last_optimization
        attrs: dict[str, Any] = {}

        from datetime import timedelta
        from homeassistant.util import dt as dt_util
        now = dt_util.now()

        temps = opt.predicted_temperatures
        if temps:
            timestamps = [
                (now + timedelta(minutes=i * 15)).isoformat()
                for i in range(len(temps))
            ]
            attrs["timestamps"] = timestamps
            attrs["predicted_temps"] = [round(t, 2) for t in temps]

        if opt.slot_results:
            attrs["target_temps"] = [round(s.t_target, 1) for s in opt.slot_results]
            attrs["outdoor_temps"] = [round(s.t_without_heating, 2) for s in opt.slot_results]
            # Heating plan: total watts per slot (useful for area fill in graph)
            attrs["heating_watts"] = [round(s.total_heating_w, 0) for s in opt.slot_results]
            attrs["total_cost_24h"] = round(opt.total_cost, 4)

        return attrs if attrs else None


class DebugTraceSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Full optimization and training trace for debugging."""

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
        if self.coordinator.last_optimize_trace is None:
            return "No optimization run yet"
        trace = self.coordinator.last_optimize_trace
        return (
            f"{trace.get('total_steps', 0)} steps, "
            f"{trace.get('warnings', 0)} warnings"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs: dict[str, Any] = {}
        if self.coordinator.last_optimize_trace:
            attrs["optimization"] = self.coordinator.last_optimize_trace
        if self.coordinator.last_training_trace:
            attrs["training"] = self.coordinator.last_training_trace
        return attrs if attrs else None


class HeaterStateSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Shows if a heater should be on or off, with the reason why."""

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
        return "on" if dev.get("heating_on", False) else "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        dev = self.coordinator.data.get("devices", {}).get(self._device_name, {})
        return {"reason": dev.get("reason", "unknown")} if dev else None


class HeaterSetpointSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
    """Recommended thermostat setpoint — the primary output of the integration.

    Set your thermostat to this value, or enable auto-control to have
    the integration do it automatically.
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
        }


class HeaterHeatSensor(CoordinatorEntity[PredictiveHeatingCoordinator], SensorEntity):
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
