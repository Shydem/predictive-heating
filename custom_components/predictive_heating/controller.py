"""
Heating controller — decides when and how much to heat.

v0.1: Simple hysteresis on/off with window detection.
v0.7: MPC removed — the integration is now monitor-first and uses a
    separate optimal-start ``PreheatPlanner`` (see preheat.py) to
    decide when to start heating early so a scheduled target is
    reached on time.

The controller exposes a single ``update()`` entry point, currently
running plain hysteresis. The ``mpc_enabled`` / ``mpc_config`` kwargs
are accepted for signature back-compat but ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from .const import DEFAULT_HYSTERESIS, STATE_CALIBRATED
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
    VACATION = "vacation"
    NONE = "none"


@dataclass
class ControllerState:
    """Current controller decision state."""

    action: HeatingAction = HeatingAction.IDLE
    target_temp: float = 21.0
    preset: PresetMode = PresetMode.COMFORT
    window_open: bool = False
    is_heating: bool = False
    # v0.7: MPC removed. Left as ``None`` for back-compat with any
    # external code that still reads the attribute; no longer populated.
    last_mpc_result: object | None = None
    # "hysteresis" or "window_open" — which branch produced the last
    # decision. Used for diagnostics in the UI.
    mode_used: str = "hysteresis"


class HeatingController:
    """
    Decides when to heat and at what intensity.

    v0.1 — Hysteresis control:
        Heat ON  when temp < target - hysteresis
        Heat OFF when temp > target + hysteresis

    v0.7 — MPC removed:
        The controller runs in plain hysteresis. Pre-heat anticipation
        is provided by the separate :class:`PreheatPlanner` which
        raises the *target* earlier; the controller itself is intentionally
        simple so it never fights the thermostat's own modulation.

    Window open → always OFF.
    """

    def __init__(
        self,
        thermal_model: ThermalModel,
        hysteresis: float = DEFAULT_HYSTERESIS,
        *,
        mpc_enabled: bool = False,  # kept for signature back-compat; ignored
        mpc_config: object | None = None,  # ditto
        preset_temps_source: dict | None = None,
    ) -> None:
        self.model = thermal_model
        self.hysteresis = hysteresis
        self.state = ControllerState()
        # v0.7: MPC removed. These attributes are retained so external
        # code probing for them doesn't crash, but they're always
        # False / None and the update() path never uses them.
        self.mpc_enabled = False
        self._mpc = None

        # ``preset_temps_source`` is a shared dict owned by the integration
        # (populated by the preset number entities). The controller always
        # reads from it on demand so a change via number entity is picked
        # up on the next ``set_preset`` call without extra plumbing.
        self._preset_src = preset_temps_source

        # Preset temperatures (fallback defaults). The source dict, when
        # supplied, wins whenever it has a matching slug.
        self.preset_temps: dict[PresetMode, float] = {
            PresetMode.COMFORT: 21.0,
            PresetMode.ECO: 18.0,
            PresetMode.AWAY: 15.0,
            PresetMode.SLEEP: 18.5,
            PresetMode.BOOST: 24.0,
            PresetMode.VACATION: 12.0,
        }

    def _current_preset_temp(self, preset: PresetMode) -> float | None:
        if preset == PresetMode.NONE:
            return None
        fallback = self.preset_temps.get(preset)
        if self._preset_src is None:
            return fallback
        val = self._preset_src.get(preset.value)
        if val is None:
            return fallback
        try:
            return float(val)
        except (TypeError, ValueError):
            return fallback

    def set_mpc_enabled(self, enabled: bool, config: object | None = None) -> None:
        """No-op since v0.7 — MPC was removed. Kept for API stability."""
        # Accepting the call without effect means options-flow handlers
        # that still forward the old toggle keep working during upgrades.
        self.mpc_enabled = False
        self._mpc = None

    def update_mpc_config(self, config: object) -> None:
        """No-op since v0.7 — MPC was removed."""
        return

    def set_preset(self, preset: PresetMode) -> None:
        """Change the active preset mode."""
        self.state.preset = preset
        t = self._current_preset_temp(preset)
        if t is not None:
            self.state.target_temp = t
        _LOGGER.debug("Preset changed to %s (%.1f°C)", preset, self.state.target_temp)

    def refresh_target_from_preset(self) -> None:
        """Re-read the active preset's number entity and update target.

        Called by the climate entity after a preset-number change so the
        controller picks up the new temperature without having to flip
        presets.
        """
        if self.state.preset == PresetMode.NONE:
            return
        t = self._current_preset_temp(self.state.preset)
        if t is not None:
            self.state.target_temp = t

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
            1. Window open   → OFF
            2. Otherwise     → hysteresis on target
        """
        target = self.state.target_temp

        # Rule 1: Window open → always off
        if self.state.window_open:
            self.state.action = HeatingAction.OFF
            self.state.is_heating = False
            self.state.mode_used = "window_open"
            self.state.last_mpc_result = None
            return HeatingAction.OFF

        # Rule 2: Hysteresis
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
