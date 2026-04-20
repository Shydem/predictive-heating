"""
Heating controller — decides when and how much to heat.

v0.1: Simple hysteresis on/off with window detection.
v0.3: Optional MPC mode for anticipation + overshoot prevention.

Control modes:
    * ``HYSTERESIS`` — plain ±band on/off. Always safe, works before
      the thermal model is calibrated.
    * ``MPC``        — short-horizon receding-horizon optimiser that
      plans ``N`` timesteps ahead using the learned thermal model and
      picks the control action that minimises predicted overshoot.
      Automatically falls back to hysteresis if the model hasn't
      calibrated yet or if the MPC solve fails.

The controller still exposes the same single ``update()`` entry point,
so the climate entity doesn't need to care which mode is active.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from .const import DEFAULT_HYSTERESIS, STATE_CALIBRATED
from .mpc import MPCConfig, MPCController, MPCResult
from .thermal_model import ThermalModel

_LOGGER = logging.getLogger(__name__)


class HeatingAction(StrEnum):
    """What the controller wants the heating system to do."""

    OFF = "off"
    HEAT = "heat"
    IDLE = "idle"  # within hysteresis band, keep current state


class PresetMode(StrEnum):
    """Temperature preset modes."""

    COMFORT = "comfort"
    ECO = "eco"
    AWAY = "away"
    SLEEP = "sleep"
    BOOST = "boost"
    NONE = "none"


@dataclass
class ControllerState:
    """Current controller decision state."""

    action: HeatingAction = HeatingAction.IDLE
    target_temp: float = 21.0
    preset: PresetMode = PresetMode.COMFORT
    window_open: bool = False
    is_heating: bool = False
    # Populated by the MPC on each solve — None when running in plain
    # hysteresis mode. Useful as a diagnostic in the UI.
    last_mpc_result: MPCResult | None = None
    # "hysteresis" or "mpc" — which mode produced the last decision.
    mode_used: str = "hysteresis"


class HeatingController:
    """
    Decides when to heat and at what intensity.

    v0.1 — Hysteresis control:
        Heat ON  when temp < target - hysteresis
        Heat OFF when temp > target + hysteresis

    v0.3 — MPC mode (optional):
        When ``mpc_enabled`` and the thermal model is calibrated, the
        MPC plans ``horizon_min`` ahead and picks the action that
        minimises predicted overshoot. Falls back to hysteresis if the
        model isn't calibrated yet.

    Window open → always OFF, in both modes.
    """

    def __init__(
        self,
        thermal_model: ThermalModel,
        hysteresis: float = DEFAULT_HYSTERESIS,
        *,
        mpc_enabled: bool = False,
        mpc_config: MPCConfig | None = None,
    ) -> None:
        self.model = thermal_model
        self.hysteresis = hysteresis
        self.state = ControllerState()
        self.mpc_enabled = mpc_enabled
        self._mpc = MPCController(thermal_model, mpc_config) if mpc_enabled else None

        # Preset temperatures (can be updated via HA number entities)
        self.preset_temps: dict[PresetMode, float] = {
            PresetMode.COMFORT: 21.0,
            PresetMode.ECO: 18.0,
            PresetMode.AWAY: 15.0,
            PresetMode.SLEEP: 18.5,
            PresetMode.BOOST: 24.0,
        }

    def set_mpc_enabled(self, enabled: bool, config: MPCConfig | None = None) -> None:
        """Toggle MPC at runtime (called from the options flow)."""
        self.mpc_enabled = enabled
        if enabled:
            self._mpc = MPCController(self.model, config)
        else:
            self._mpc = None

    def update_mpc_config(self, config: MPCConfig) -> None:
        """Replace the active MPC config (horizon / delay tuning)."""
        if self._mpc is not None:
            self._mpc = MPCController(self.model, config)

    def set_preset(self, preset: PresetMode) -> None:
        """Change the active preset mode."""
        self.state.preset = preset
        if preset in self.preset_temps:
            self.state.target_temp = self.preset_temps[preset]
        _LOGGER.debug("Preset changed to %s (%.1f°C)", preset, self.state.target_temp)

    def set_target_temp(self, temp: float) -> None:
        """Manually override the target temperature."""
        self.state.target_temp = temp
        self.state.preset = PresetMode.NONE
        _LOGGER.debug("Manual target set to %.1f°C", temp)

    def set_window_open(self, is_open: bool) -> None:
        """Update window state."""
        self.state.window_open = is_open

    def update(
        self,
        t_indoor: float,
        t_outdoor: float,
        solar_irradiance: float = 0.0,
    ) -> HeatingAction:
        """
        Evaluate current conditions and return the desired heating action.

        Order of decisions:
            1. Window open          → OFF
            2. MPC enabled + model  → MPC solves ahead and decides
               calibrated
            3. Otherwise            → hysteresis
        """
        target = self.state.target_temp

        # Rule 1: Window open → always off
        if self.state.window_open:
            self.state.action = HeatingAction.OFF
            self.state.is_heating = False
            self.state.mode_used = "window_open"
            self.state.last_mpc_result = None
            return HeatingAction.OFF

        # Rule 2: MPC (if enabled + model calibrated)
        if (
            self.mpc_enabled
            and self._mpc is not None
            and self.model.state == STATE_CALIBRATED
        ):
            try:
                result = self._mpc.solve(
                    t_indoor=t_indoor,
                    t_outdoor=t_outdoor,
                    t_target=target,
                    solar_irradiance=solar_irradiance,
                    currently_heating=self.state.is_heating,
                )
                self.state.last_mpc_result = result
                self.state.mode_used = "mpc"

                heat_now = result.action == "heat"
                self._mpc.record_command(heat_now)
                self.state.is_heating = heat_now
                self.state.action = (
                    HeatingAction.HEAT if heat_now else HeatingAction.OFF
                )
                return self.state.action
            except Exception as err:  # noqa: BLE001 — fall back to hysteresis
                _LOGGER.warning(
                    "MPC solve failed (%s) — falling back to hysteresis", err
                )

        # Rule 3: Hysteresis fallback
        self.state.mode_used = "hysteresis"
        self.state.last_mpc_result = None
        if t_indoor < target - self.hysteresis:
            self.state.action = HeatingAction.HEAT
            self.state.is_heating = True
        elif t_indoor > target + self.hysteresis:
            self.state.action = HeatingAction.OFF
            self.state.is_heating = False
        else:
            # Within hysteresis band — keep current state
            self.state.action = (
                HeatingAction.HEAT if self.state.is_heating else HeatingAction.IDLE
            )

        return self.state.action

    def get_preheat_start_time(
        self,
        t_indoor: float,
        t_outdoor: float,
        target_temp: float,
        target_time_hours: float,
    ) -> float | None:
        """
        Calculate when to start pre-heating to reach target_temp by target_time.

        This is a placeholder for v0.2 — requires a calibrated thermal model.

        Returns:
            Hours before target_time to start heating, or None if model
            isn't calibrated yet.
        """
        from .const import STATE_CALIBRATED

        if self.model.state != STATE_CALIBRATED:
            return None

        time_needed = self.model.time_to_reach(
            t_indoor=t_indoor,
            t_target=target_temp,
            t_outdoor=t_outdoor,
            heating_power_fraction=1.0,
        )

        if time_needed is None:
            return None

        # Add 10% margin
        return time_needed * 1.1
