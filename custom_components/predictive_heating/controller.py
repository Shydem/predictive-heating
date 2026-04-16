"""
Heating controller — decides when and how much to heat.

Phase 1 (v0.1): Simple hysteresis on/off control with window detection.
Phase 2+: Will use the thermal model for predictive pre-heating and
           cost-optimal scheduling based on energy prices and heat pump COP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from .const import DEFAULT_HYSTERESIS
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


class HeatingController:
    """
    Decides when to heat and at what intensity.

    v0.1 — Hysteresis control:
        Heat ON  when temp < target - hysteresis
        Heat OFF when temp > target + hysteresis

    With window detection: always OFF when a window is open.

    Future (v0.2+): Use thermal_model.predict_temperature() and
    thermal_model.time_to_reach() for predictive pre-heating and
    cost-optimal scheduling.
    """

    def __init__(
        self,
        thermal_model: ThermalModel,
        hysteresis: float = DEFAULT_HYSTERESIS,
    ) -> None:
        self.model = thermal_model
        self.hysteresis = hysteresis
        self.state = ControllerState()

        # Preset temperatures (can be updated via HA number entities)
        self.preset_temps: dict[PresetMode, float] = {
            PresetMode.COMFORT: 21.0,
            PresetMode.ECO: 18.0,
            PresetMode.AWAY: 15.0,
            PresetMode.SLEEP: 18.5,
            PresetMode.BOOST: 24.0,
        }

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

        Args:
            t_indoor: current indoor temperature
            t_outdoor: current outdoor temperature
            solar_irradiance: current solar irradiance (W/m2)

        Returns:
            HeatingAction indicating what to do.
        """
        target = self.state.target_temp

        # Rule 1: Window open → always off
        if self.state.window_open:
            self.state.action = HeatingAction.OFF
            self.state.is_heating = False
            return HeatingAction.OFF

        # Rule 2: Hysteresis-based on/off
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
