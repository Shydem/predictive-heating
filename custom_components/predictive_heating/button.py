"""
One-shot action buttons per room.

* ``Recompute thermal properties`` — forces a fresh pass over the
  stored observation history, rebuilding H / C / P_heat / S_gain
  estimates from scratch. Useful after a long run of noisy data when
  the EKF has drifted off.

* ``Simulate next 24h`` — runs the thermal-model trajectory simulator
  for the next 24 hours using the current schedule / weather forecast
  / solar forecast, taking into account solar warming so the system
  doesn't pre-heat in the morning and then have to dump that heat
  in the afternoon. The result is stored on ``data["last_simulation"]``
  so the dashboard can visualise it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ROOM_NAME,
    DOMAIN,
    PREDICTION_HORIZON_HOURS,
)
from .ekf import ThermalEKF
from .thermal_model import ThermalModel, ThermalObservation

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    room_name = data["config"].get(CONF_ROOM_NAME, entry.title)

    async_add_entities(
        [
            RecomputeThermalPropertiesButton(
                entry=entry, room_name=room_name, data=data
            ),
            SimulateScheduleButton(entry=entry, room_name=room_name, data=data),
        ]
    )


class RecomputeThermalPropertiesButton(ButtonEntity):
    """Re-run the EKF from scratch over the stored observation history."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calculator-variant"

    def __init__(self, *, entry: ConfigEntry, room_name: str, data: dict) -> None:
        self._entry = entry
        self._data = data
        self._attr_unique_id = f"{entry.entry_id}_recompute"
        self._attr_name = f"{room_name} Recompute thermal properties"

    async def async_press(self) -> None:
        model: ThermalModel = self._data["model"]
        try:
            _recompute_thermal_params(model)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Recompute failed: %s", err)
            return
        _LOGGER.info(
            "Recomputed thermal parameters for %s: H=%.1f W/K, "
            "C=%.0f kJ/K, P=%.0f W, S=%.2f",
            self._attr_name,
            model.params.heat_loss_coeff,
            model.params.thermal_mass,
            model.params.heating_power,
            model.params.solar_gain_factor,
        )


def _recompute_thermal_params(model: ThermalModel) -> None:
    """
    Replay every stored observation through a brand-new EKF.

    Keeps the raw observation history intact — only the parameter
    estimates get reset. Call this when the model has been driven
    off-course by a bad run (e.g. a broken sensor fixed later).
    """
    if not model.observations:
        return

    try:
        ekf = ThermalEKF()
    except Exception:  # numpy missing
        return

    prev: ThermalObservation | None = None
    for obs in model.observations:
        if prev is not None:
            dt = obs.timestamp - prev.timestamp
            if 0 < dt < 7200:
                measured = prev.heat_power_w
                if prev.coupling_power_w:
                    measured = (measured or 0.0) + prev.coupling_power_w
                ekf.update(
                    dt=dt / 3600.0,
                    T_in=prev.t_indoor,
                    T_out=prev.t_outdoor,
                    u_heat=1.0 if prev.heating_on else 0.0,
                    I_solar=prev.solar_irradiance,
                    dT_measured=obs.t_indoor - prev.t_indoor,
                    measured_heat_w=measured,
                )
        prev = obs

    model._ekf = ekf
    model.params.heat_loss_coeff = ekf.state.H
    model.params.thermal_mass = ekf.state.C_kj
    model.params.solar_gain_factor = ekf.state.S_gain
    if ekf.state.P_heat > 0:
        model.params.heating_power = ekf.state.P_heat
    model.mean_prediction_error = ekf.mean_prediction_error
    model.h_history.append(
        {"sample": model.total_updates, "value": ekf.state.H, "source": "recompute"}
    )


class SimulateScheduleButton(ButtonEntity):
    """Run a full 24 h predictive simulation and store the result."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(self, *, entry: ConfigEntry, room_name: str, data: dict) -> None:
        self._entry = entry
        self._data = data
        self._attr_unique_id = f"{entry.entry_id}_simulate"
        self._attr_name = f"{room_name} Simulate heating plan"

    async def async_press(self) -> None:
        cb = self._data.get("_on_simulate_request")
        if cb is None:
            _LOGGER.debug("Simulation hook not yet registered")
            return
        try:
            result = await cb()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Simulation failed: %s", err)
            return
        self._data["last_simulation"] = result
        _LOGGER.info(
            "Simulation completed for %s (%d steps, horizon %s h)",
            self._attr_name,
            len(result.get("trajectory", [])) if isinstance(result, dict) else 0,
            PREDICTION_HORIZON_HOURS,
        )
