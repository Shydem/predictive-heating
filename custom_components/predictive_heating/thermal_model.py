"""
Simplified thermal model for a room.

The model captures the core physics of a heated room:

    dT/dt = (1/C) * [Q_heat - H*(T_in - T_out) + Q_solar]

Where:
    T_in   = indoor temperature (C)
    T_out  = outdoor temperature (C)
    C      = thermal mass of the room (kJ/K)
    H      = heat loss coefficient (W/K) — combines walls, windows, ventilation
    Q_heat = heating power delivered to the room (W)
    Q_solar = solar gain (W)

The model self-learns H and C by observing temperature changes over time
using a simple recursive least-squares approach (simpler than a full EKF,
but effective enough for v0.1).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .const import (
    DEFAULT_HEAT_LOSS_COEFFICIENT,
    DEFAULT_HEATING_POWER,
    DEFAULT_SOLAR_GAIN_FACTOR,
    DEFAULT_THERMAL_MASS,
    MIN_ACTIVE_SAMPLES,
    MIN_IDLE_SAMPLES,
    STATE_CALIBRATED,
    STATE_LEARNING,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ThermalObservation:
    """A single observation used for model learning."""

    timestamp: float  # unix timestamp
    t_indoor: float  # indoor temperature (C)
    t_outdoor: float  # outdoor temperature (C)
    heating_on: bool  # whether heating was active
    solar_irradiance: float = 0.0  # W/m2, estimated


@dataclass
class ThermalParameters:
    """Learned thermal parameters for a room."""

    heat_loss_coeff: float = DEFAULT_HEAT_LOSS_COEFFICIENT  # W/K
    thermal_mass: float = DEFAULT_THERMAL_MASS  # kJ/K
    heating_power: float = DEFAULT_HEATING_POWER  # W
    solar_gain_factor: float = DEFAULT_SOLAR_GAIN_FACTOR


@dataclass
class ThermalModel:
    """
    Self-learning thermal model for a single room.

    Phase 1 (v0.1): Learns heat_loss_coeff from idle observations.
    Uses a simple exponential-decay fitting approach:

        When heating is off and windows are closed, the room cools:
        dT/dt ≈ -H/C * (T_in - T_out)

        By observing dT and the temperature difference, we can estimate H/C.
        Combined with a rough C estimate (from room size), we get H.
    """

    params: ThermalParameters = field(default_factory=ThermalParameters)
    observations: list[ThermalObservation] = field(default_factory=list)
    idle_count: int = 0
    active_count: int = 0
    state: str = STATE_LEARNING

    # Running estimate accumulators for H/C ratio
    _h_over_c_sum: float = 0.0
    _h_over_c_count: int = 0

    _last_obs: ThermalObservation | None = None

    def add_observation(self, obs: ThermalObservation) -> None:
        """Record an observation and update model parameters."""
        if self._last_obs is not None:
            dt_seconds = obs.timestamp - self._last_obs.timestamp
            if dt_seconds > 0 and dt_seconds < 3600:  # ignore gaps > 1h
                self._learn_from_pair(self._last_obs, obs, dt_seconds)

        self._last_obs = obs

        # Keep a bounded history
        self.observations.append(obs)
        if len(self.observations) > 500:
            self.observations = self.observations[-500:]

        self._check_calibration()

    def _learn_from_pair(
        self,
        prev: ThermalObservation,
        curr: ThermalObservation,
        dt_seconds: float,
    ) -> None:
        """Learn from two consecutive observations."""
        dt_hours = dt_seconds / 3600.0
        dT = curr.t_indoor - prev.t_indoor  # temperature change
        delta_T = prev.t_indoor - prev.t_outdoor  # indoor-outdoor difference

        if abs(delta_T) < 0.5:
            return  # not enough gradient to learn from

        if not prev.heating_on and not curr.heating_on:
            # Idle cooling: dT/dt = -H/C * delta_T  (ignoring solar for now)
            # => H/C = -dT / (dt * delta_T)
            h_over_c = -dT / (dt_hours * delta_T)  # units: 1/h
            if 0.001 < h_over_c < 2.0:  # sanity bounds
                self._h_over_c_sum += h_over_c
                self._h_over_c_count += 1
                self.idle_count += 1

                # Update the heat loss coefficient estimate
                avg_h_over_c = self._h_over_c_sum / self._h_over_c_count
                # H = (H/C) * C, with C in kJ/K and H/C in 1/h
                # Convert: H(W/K) = H/C(1/h) * C(kJ/K) * 1000/3600
                self.params.heat_loss_coeff = (
                    avg_h_over_c * self.params.thermal_mass * 1000 / 3600
                )

                _LOGGER.debug(
                    "Thermal model idle update: H/C=%.4f, H=%.1f W/K "
                    "(from %d samples)",
                    avg_h_over_c,
                    self.params.heat_loss_coeff,
                    self._h_over_c_count,
                )

        elif prev.heating_on:
            self.active_count += 1
            # Future: learn heating_power from active observations

    def _check_calibration(self) -> None:
        """Check if we have enough data to consider the model calibrated."""
        if (
            self.idle_count >= MIN_IDLE_SAMPLES
            and self.active_count >= MIN_ACTIVE_SAMPLES
            and self.state != STATE_CALIBRATED
        ):
            self.state = STATE_CALIBRATED
            _LOGGER.info(
                "Thermal model calibrated: H=%.1f W/K, C=%.0f kJ/K",
                self.params.heat_loss_coeff,
                self.params.thermal_mass,
            )

    def predict_temperature(
        self,
        t_indoor: float,
        t_outdoor: float,
        heating_power_fraction: float,
        hours_ahead: float,
        solar_irradiance: float = 0.0,
    ) -> float:
        """
        Predict indoor temperature after `hours_ahead` hours.

        Args:
            t_indoor: current indoor temp (C)
            t_outdoor: outdoor temp (C), assumed constant
            heating_power_fraction: 0.0 to 1.0
            hours_ahead: prediction horizon
            solar_irradiance: W/m2

        Returns:
            Predicted indoor temperature.
        """
        p = self.params
        C_watt_h = p.thermal_mass * 1000 / 3600  # convert kJ/K to Wh/K

        # Simple Euler integration with 5-minute steps
        steps = max(1, int(hours_ahead * 12))
        dt = hours_ahead / steps
        temp = t_indoor

        for _ in range(steps):
            q_heat = heating_power_fraction * p.heating_power
            q_solar = solar_irradiance * p.solar_gain_factor
            q_loss = p.heat_loss_coeff * (temp - t_outdoor)

            dT = (q_heat + q_solar - q_loss) / C_watt_h * dt
            temp += dT

        return temp

    def time_to_reach(
        self,
        t_indoor: float,
        t_target: float,
        t_outdoor: float,
        heating_power_fraction: float = 1.0,
        solar_irradiance: float = 0.0,
        max_hours: float = 8.0,
    ) -> float | None:
        """
        Estimate hours needed to reach target temperature.

        Returns None if target can't be reached within max_hours.
        """
        if t_indoor >= t_target:
            return 0.0

        p = self.params
        C_watt_h = p.thermal_mass * 1000 / 3600

        steps = int(max_hours * 12)
        dt = 1.0 / 12.0  # 5-minute steps
        temp = t_indoor

        for step in range(steps):
            q_heat = heating_power_fraction * p.heating_power
            q_solar = solar_irradiance * p.solar_gain_factor
            q_loss = p.heat_loss_coeff * (temp - t_outdoor)

            dT = (q_heat + q_solar - q_loss) / C_watt_h * dt
            temp += dT

            if temp >= t_target:
                return (step + 1) * dt

        return None  # can't reach in time

    def to_dict(self) -> dict:
        """Serialize model state for persistence."""
        return {
            "params": {
                "heat_loss_coeff": self.params.heat_loss_coeff,
                "thermal_mass": self.params.thermal_mass,
                "heating_power": self.params.heating_power,
                "solar_gain_factor": self.params.solar_gain_factor,
            },
            "idle_count": self.idle_count,
            "active_count": self.active_count,
            "state": self.state,
            "_h_over_c_sum": self._h_over_c_sum,
            "_h_over_c_count": self._h_over_c_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ThermalModel:
        """Restore model from persisted state."""
        model = cls()
        if "params" in data:
            p = data["params"]
            model.params = ThermalParameters(
                heat_loss_coeff=p.get(
                    "heat_loss_coeff", DEFAULT_HEAT_LOSS_COEFFICIENT
                ),
                thermal_mass=p.get("thermal_mass", DEFAULT_THERMAL_MASS),
                heating_power=p.get("heating_power", DEFAULT_HEATING_POWER),
                solar_gain_factor=p.get(
                    "solar_gain_factor", DEFAULT_SOLAR_GAIN_FACTOR
                ),
            )
        model.idle_count = data.get("idle_count", 0)
        model.active_count = data.get("active_count", 0)
        model.state = data.get("state", STATE_LEARNING)
        model._h_over_c_sum = data.get("_h_over_c_sum", 0.0)
        model._h_over_c_count = data.get("_h_over_c_count", 0)
        return model
