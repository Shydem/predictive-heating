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
            ResetThermalHistoryButton(
                entry=entry, room_name=room_name, data=data
            ),
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


def _is_plausible_observation(obs: ThermalObservation) -> bool:
    """Sanity-check a single observation before feeding it to the EKF.

    A few pathologies kill the recompute:
      * ``NaN`` / ``inf`` anywhere in the tuple → numpy silently
        propagates them and the whole EKF state goes to NaN.
      * Indoor temperatures outside a habitable range (sensor
        disconnects often return 0.0 or −127.0).
      * Outdoor temperature far outside plausible Dutch range —
        catches faulty sensor reads without punishing extreme
        weather within reason.

    Reason this matters: users have reported that a single bad
    sensor day poisons the history; without this filter, the
    "Recompute" button would silently produce garbage parameters
    and the user has no way to recover short of deleting the file.
    """
    import math

    floats = (
        obs.t_indoor,
        obs.t_outdoor,
        obs.solar_irradiance,
        obs.heat_power_w or 0.0,
    )
    for v in floats:
        if v is None or not math.isfinite(float(v)):
            return False
    if not (-10.0 <= obs.t_indoor <= 45.0):
        return False
    if not (-40.0 <= obs.t_outdoor <= 55.0):
        return False
    return True


def _recompute_thermal_params(
    model: ThermalModel, *, reset_on_failure: bool = True
) -> None:
    """
    Replay every stored observation through a brand-new EKF.

    Keeps the raw observation history intact — only the parameter
    estimates get reset. Call this when the model has been driven
    off-course by a bad run (e.g. a broken sensor fixed later).

    Robustness: observations that fail ``_is_plausible_observation``
    are skipped, and step-to-step temperature jumps of more than 5 °C
    in under an hour are also dropped as "sensor spike / glitch".
    If after filtering we end up with fewer than 10 usable samples we
    fall back to a clean-slate EKF rather than producing garbage
    params from a handful of outliers.
    """
    if not model.observations:
        return

    try:
        ekf = ThermalEKF()
    except Exception:  # numpy missing
        return

    # Pre-filter: drop obviously broken observations before replay.
    clean: list[ThermalObservation] = [
        obs for obs in model.observations if _is_plausible_observation(obs)
    ]
    dropped = len(model.observations) - len(clean)
    if dropped:
        _LOGGER.warning(
            "Recompute: dropped %d/%d observations that failed plausibility "
            "checks (sensor glitches, NaN, out-of-range)",
            dropped,
            len(model.observations),
        )

    applied = 0
    prev: ThermalObservation | None = None
    for obs in clean:
        if prev is not None:
            dt = obs.timestamp - prev.timestamp
            jump = abs(obs.t_indoor - prev.t_indoor)
            # Skip impossible jumps: real rooms never slew 5 °C in one
            # controller tick. These are sensor re-boots or replaced
            # batteries and poison the EKF innovation term.
            if 0 < dt < 7200 and jump < 5.0:
                measured = prev.heat_power_w
                if prev.coupling_power_w:
                    measured = (measured or 0.0) + prev.coupling_power_w
                try:
                    ekf.update(
                        dt=dt / 3600.0,
                        T_in=prev.t_indoor,
                        T_out=prev.t_outdoor,
                        u_heat=1.0 if prev.heating_on else 0.0,
                        I_solar=prev.solar_irradiance,
                        dT_measured=obs.t_indoor - prev.t_indoor,
                        measured_heat_w=measured,
                    )
                    applied += 1
                except Exception as err:  # noqa: BLE001 — one bad step can't kill the recompute
                    _LOGGER.debug("EKF step failed, skipping: %s", err)
        prev = obs

    # Guardrail: if we couldn't replay enough clean observations, the
    # recomputed params are worse than the old ones — don't clobber.
    # When ``reset_on_failure`` is True we at least keep the caller's
    # "recompute was pressed" intent visible by wiping the EKF state,
    # but we don't change the exposed parameters.
    if applied < 10:
        _LOGGER.warning(
            "Recompute: only %d usable observations after filtering; "
            "keeping existing parameters (avoids overwriting with garbage).",
            applied,
        )
        if reset_on_failure:
            model._ekf = ekf
        return

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


def _reset_thermal_history(model: ThermalModel) -> int:
    """Wipe observations, EKF state and derived parameters.

    Returns the number of observations that were discarded. Exposed
    as a last-resort recovery tool when the history itself is the
    problem (e.g. the boiler sensor spent a week reporting 0 kW).
    """
    n = len(getattr(model, "observations", []) or [])
    model.observations.clear()
    model.h_history.clear()
    model.prediction_history.clear()
    model.prediction_error_history.clear()
    model.idle_count = 0
    model.active_count = 0
    model.total_updates = 0
    model.mean_prediction_error = float("inf")
    try:
        model._ekf = ThermalEKF()
    except Exception:
        model._ekf = None
    _LOGGER.info("Reset thermal history: %d observations discarded", n)
    return n


class ResetThermalHistoryButton(ButtonEntity):
    """Nuclear-option: wipe all learning history and start fresh.

    Needed when the stored observation history itself is bad (e.g.
    a broken sensor produced a week of flat-line readings). In that
    state, Recompute can't rescue the model because it's replaying
    the same broken data — the only sane action is to throw the
    history away and relearn.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:delete-forever"

    def __init__(self, *, entry: ConfigEntry, room_name: str, data: dict) -> None:
        self._entry = entry
        self._data = data
        self._attr_unique_id = f"{entry.entry_id}_reset_history"
        self._attr_name = f"{room_name} Reset learning history"

    async def async_press(self) -> None:
        model: ThermalModel = self._data["model"]
        try:
            n = _reset_thermal_history(model)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Reset history failed: %s", err)
            return
        _LOGGER.info(
            "Reset learning history for %s: %d observations discarded",
            self._attr_name,
            n,
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
