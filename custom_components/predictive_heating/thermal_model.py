"""
Thermal model for a room — v0.2 with Extended Kalman Filter.

The model captures the core physics of a heated room:

    dT/dt = (1/C) * [Q_heat - H*(T_in - T_out) + Q_solar]

Where:
    T_in   = indoor temperature (C)
    T_out  = outdoor temperature (C)
    C      = thermal mass of the room (kJ/K)
    H      = heat loss coefficient (W/K) — walls, windows, ventilation
    Q_heat = heating power delivered to the room (W)
    Q_solar = solar gain (W)

v0.2 upgrades:
    - Extended Kalman Filter learns H, C, heating_power, and solar_gain
      simultaneously from all observations (idle AND active)
    - Solar irradiance input from sun position + weather entity
    - Prediction accuracy tracking → auto-calibration when error < 0.5°C
    - Simple estimator kept as bootstrap for the first few observations
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .const import (
    BUILDING_TYPES,
    DEFAULT_BUILDING_TYPE,
    DEFAULT_CEILING_HEIGHT_M,
    DEFAULT_HEAT_LOSS_COEFFICIENT,
    DEFAULT_HEATING_POWER,
    DEFAULT_SOLAR_GAIN_FACTOR,
    DEFAULT_THERMAL_MASS,
    MIN_ACTIVE_SAMPLES,
    MIN_IDLE_SAMPLES,
    STATE_CALIBRATED,
    STATE_LEARNING,
)


def estimate_initial_thermal_params(
    floor_area_m2: float | None,
    ceiling_height_m: float | None = None,
    building_type: str | None = None,
) -> dict[str, float] | None:
    """
    Estimate starting H (W/K) and C (kJ/K) from room dimensions + building type.

    Returns ``None`` if the floor area is missing — no estimation possible.
    Otherwise returns ``{"H": ..., "C": ..., "volume_m3": ..., "building_type": ...}``.

    These are only used as a seed for the EKF when no saved model exists.
    The EKF will correct them as observations come in.
    """
    if not floor_area_m2 or floor_area_m2 <= 0:
        return None

    if not ceiling_height_m or ceiling_height_m <= 0:
        ceiling_height_m = DEFAULT_CEILING_HEIGHT_M

    btype = building_type or DEFAULT_BUILDING_TYPE
    preset = BUILDING_TYPES.get(btype) or BUILDING_TYPES[DEFAULT_BUILDING_TYPE]

    volume_m3 = floor_area_m2 * ceiling_height_m
    H = floor_area_m2 * preset["u_per_m2_floor"]  # W / K
    C = volume_m3 * preset["vol_heat_capacity"]   # kJ / K

    return {
        "H": H,
        "C": C,
        "volume_m3": volume_m3,
        "building_type": btype,
    }

_LOGGER = logging.getLogger(__name__)

# Try to import numpy for EKF; fall back to simple model if unavailable
try:
    import numpy as np
    from .ekf import ThermalEKF

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    _LOGGER.warning(
        "numpy not available — using simple thermal model (install numpy for EKF)"
    )


@dataclass
class ThermalObservation:
    """A single observation used for model learning."""

    timestamp: float  # unix timestamp
    t_indoor: float  # indoor temperature (C)
    t_outdoor: float  # outdoor temperature (C)
    heating_on: bool  # whether heating was active (derived boolean)
    solar_irradiance: float = 0.0  # W/m2, estimated
    # Actual thermal power delivered to the room (W). If set, this is
    # the preferred heat input for the EKF — it's much richer than a
    # binary on/off because it captures modulation and DHW draws.
    heat_power_w: float | None = None


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

    Uses an Extended Kalman Filter (v0.2) to learn all four thermal
    parameters simultaneously. Falls back to a simple H/C ratio
    estimator if numpy is not available.

    The EKF learns from every observation pair (idle and active),
    using the measured dT between consecutive readings as the
    measurement input.
    """

    params: ThermalParameters = field(default_factory=ThermalParameters)
    observations: list[ThermalObservation] = field(default_factory=list)
    h_history: list[dict] = field(default_factory=list)  # [{sample, value}]
    prediction_error_history: list[dict] = field(default_factory=list)
    idle_count: int = 0
    active_count: int = 0
    total_updates: int = 0
    state: str = STATE_LEARNING
    mean_prediction_error: float = float("inf")

    # Simple estimator (fallback / bootstrap)
    _h_over_c_sum: float = 0.0
    _h_over_c_count: int = 0

    _last_obs: ThermalObservation | None = None
    _ekf: object | None = None  # ThermalEKF if numpy available
    _ekf_dict: dict | None = None  # for deferred EKF initialization
    # Opaque GasHeatSource state — stashed here so the model's save/load
    # round-trip carries it across restarts without requiring a
    # homeassistant dependency at module level.
    _heat_source_state: dict | None = None
    # Rolling average of measured heat power (W) from gas/heat-pump meter.
    # When a meter is available this is more informative than EKF's P_heat,
    # which is intentionally frozen (dh/dP=0) when measured_heat_w is given.
    _measured_power_sum: float = 0.0
    _measured_power_count: int = 0

    def __post_init__(self):
        if HAS_NUMPY and self._ekf is None:
            if self._ekf_dict:
                self._ekf = ThermalEKF.from_dict(self._ekf_dict)
            else:
                self._ekf = ThermalEKF()

    def seed_from_room_dimensions(
        self,
        floor_area_m2: float | None,
        ceiling_height_m: float | None = None,
        building_type: str | None = None,
    ) -> bool:
        """
        Seed the model with initial H and C estimates from room dimensions.

        Only applied when the model has no prior observations (fresh room).
        Returns True if seeding was applied.
        """
        if self.total_updates > 0:
            return False  # never overwrite a trained model

        est = estimate_initial_thermal_params(
            floor_area_m2, ceiling_height_m, building_type
        )
        if est is None:
            return False

        H = est["H"]
        C_kj = est["C"]
        C_wh = C_kj / 3.6

        self.params.heat_loss_coeff = H
        self.params.thermal_mass = C_kj

        if HAS_NUMPY and self._ekf is not None:
            # Push the seed into the EKF state so it starts from a
            # reasonable point rather than the generic defaults.
            self._ekf.state.x[0] = H
            self._ekf.state.x[1] = C_wh

        _LOGGER.info(
            "Seeded thermal model from dimensions: H=%.1f W/K, C=%.0f kJ/K "
            "(floor %.1f m², height %.1f m, type %s)",
            H, C_kj,
            floor_area_m2 or 0.0,
            ceiling_height_m or DEFAULT_CEILING_HEIGHT_M,
            est["building_type"],
        )
        return True

    def add_observation(self, obs: ThermalObservation) -> None:
        """Record an observation and update model parameters."""
        if self._last_obs is not None:
            dt_seconds = obs.timestamp - self._last_obs.timestamp
            if 0 < dt_seconds < 7200:  # ignore gaps > 2h
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
        dT = curr.t_indoor - prev.t_indoor
        delta_T = prev.t_indoor - prev.t_outdoor

        # Count samples by type
        if not prev.heating_on and not curr.heating_on:
            self.idle_count += 1
        elif prev.heating_on:
            self.active_count += 1
        self.total_updates += 1

        # ── EKF update (v0.2) ──
        if HAS_NUMPY and self._ekf is not None:
            u_heat = 1.0 if prev.heating_on else 0.0
            innovation = self._ekf.update(
                dt=dt_hours,
                T_in=prev.t_indoor,
                T_out=prev.t_outdoor,
                u_heat=u_heat,
                I_solar=prev.solar_irradiance,
                dT_measured=dT,
                measured_heat_w=prev.heat_power_w,
            )

            # Sync EKF estimates back to params
            ekf_state = self._ekf.state
            self.params.heat_loss_coeff = ekf_state.H
            self.params.thermal_mass = ekf_state.C_kj
            self.params.solar_gain_factor = ekf_state.S_gain
            self.mean_prediction_error = self._ekf.mean_prediction_error

            # heating_power: prefer the rolling average of directly-measured
            # watts (gas meter / heat-pump meter) over the EKF's P_heat.
            # When measured_heat_w is provided, EKF intentionally freezes
            # P_heat (dh/dP=0), so it stays at its initial 5 kW default —
            # meaningless as a display value.
            if prev.heat_power_w is not None and prev.heat_power_w > 0:
                # Exponential moving average α≈0.05 → ~20-sample window.
                # Only update while the boiler was actually delivering heat,
                # so domestic-hot-water spikes and true heating get averaged
                # together naturally.
                alpha = 0.05
                if self._measured_power_count == 0:
                    self.params.heating_power = prev.heat_power_w
                else:
                    self.params.heating_power = (
                        (1 - alpha) * self.params.heating_power
                        + alpha * prev.heat_power_w
                    )
                self._measured_power_count += 1
            else:
                # No meter: use the EKF's learned P_heat.
                self.params.heating_power = ekf_state.P_heat

            # Track H evolution
            self.h_history.append(
                {
                    "sample": self.total_updates,
                    "value": ekf_state.H,
                }
            )
            if len(self.h_history) > 300:
                self.h_history = self.h_history[-300:]

            # Track prediction error evolution
            if self.total_updates % 5 == 0:
                self.prediction_error_history.append(
                    {
                        "sample": self.total_updates,
                        "value": self._ekf.mean_prediction_error,
                    }
                )
                if len(self.prediction_error_history) > 200:
                    self.prediction_error_history = self.prediction_error_history[-200:]

        # ── Simple fallback estimator ──
        elif abs(delta_T) >= 0.5 and not prev.heating_on and not curr.heating_on:
            h_over_c = -dT / (dt_hours * delta_T)
            if 0.001 < h_over_c < 2.0:
                self._h_over_c_sum += h_over_c
                self._h_over_c_count += 1

                avg_h_over_c = self._h_over_c_sum / self._h_over_c_count
                self.params.heat_loss_coeff = (
                    avg_h_over_c * self.params.thermal_mass * 1000 / 3600
                )

                self.h_history.append(
                    {
                        "sample": self._h_over_c_count,
                        "value": self.params.heat_loss_coeff,
                    }
                )
                if len(self.h_history) > 300:
                    self.h_history = self.h_history[-300:]

    def _check_calibration(self) -> None:
        """Check if the model is calibrated."""
        if self.state == STATE_CALIBRATED:
            return

        if HAS_NUMPY and self._ekf is not None:
            # EKF: calibrated when prediction error < 0.5°C
            if self._ekf.is_calibrated:
                self.state = STATE_CALIBRATED
                _LOGGER.info(
                    "Thermal model CALIBRATED (EKF): H=%.1f W/K, C=%.0f kJ/K, "
                    "P=%.0f W, S=%.2f, error=%.3f°C (%d updates)",
                    self.params.heat_loss_coeff,
                    self.params.thermal_mass,
                    self.params.heating_power,
                    self.params.solar_gain_factor,
                    self.mean_prediction_error,
                    self.total_updates,
                )
        else:
            # Simple model: calibrated after enough samples
            if (
                self.idle_count >= MIN_IDLE_SAMPLES
                and self.active_count >= MIN_ACTIVE_SAMPLES
            ):
                self.state = STATE_CALIBRATED
                _LOGGER.info(
                    "Thermal model CALIBRATED (simple): H=%.1f W/K, C=%.0f kJ/K",
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

        Uses Euler integration with 5-minute steps and the current
        parameter estimates (from EKF or simple model).
        """
        p = self.params
        C_watt_h = p.thermal_mass * 1000 / 3600  # kJ/K → Wh/K

        if C_watt_h <= 0:
            return t_indoor

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
        """Estimate hours needed to reach target temperature."""
        if t_indoor >= t_target:
            return 0.0

        p = self.params
        C_watt_h = p.thermal_mass * 1000 / 3600
        if C_watt_h <= 0:
            return None

        steps = int(max_hours * 12)
        dt = 1.0 / 12.0
        temp = t_indoor

        for step in range(steps):
            q_heat = heating_power_fraction * p.heating_power
            q_solar = solar_irradiance * p.solar_gain_factor
            q_loss = p.heat_loss_coeff * (temp - t_outdoor)

            dT = (q_heat + q_solar - q_loss) / C_watt_h * dt
            temp += dT

            if temp >= t_target:
                return (step + 1) * dt

        return None

    def to_dict(self) -> dict:
        """Serialize model state for persistence."""
        obs_list = []
        for obs in self.observations[-300:]:
            obs_list.append(
                {
                    "timestamp": obs.timestamp,
                    "t_indoor": obs.t_indoor,
                    "t_outdoor": obs.t_outdoor,
                    "heating_on": obs.heating_on,
                    "solar_irradiance": obs.solar_irradiance,
                    "heat_power_w": obs.heat_power_w,
                }
            )

        result = {
            "version": 2,
            "params": {
                "heat_loss_coeff": self.params.heat_loss_coeff,
                "thermal_mass": self.params.thermal_mass,
                "heating_power": self.params.heating_power,
                "solar_gain_factor": self.params.solar_gain_factor,
            },
            "idle_count": self.idle_count,
            "active_count": self.active_count,
            "total_updates": self.total_updates,
            "state": self.state,
            # JSON cannot represent float("inf") — store as None and restore
            # to float("inf") on load. This prevents json.dumps from either
            # raising or producing "Infinity" which json.loads rejects.
            "mean_prediction_error": (
                None
                if self.mean_prediction_error == float("inf")
                else self.mean_prediction_error
            ),
            "_h_over_c_sum": self._h_over_c_sum,
            "_h_over_c_count": self._h_over_c_count,
            "_measured_power_count": self._measured_power_count,
            "observations": obs_list,
            "h_history": self.h_history[-300:],
            "prediction_error_history": self.prediction_error_history[-200:],
        }

        # Serialize EKF state if available
        if HAS_NUMPY and self._ekf is not None:
            result["ekf"] = self._ekf.to_dict()

        if self._heat_source_state:
            result["heat_source"] = self._heat_source_state

        return result

    @classmethod
    def from_dict(cls, data: dict) -> ThermalModel:
        """Restore model from persisted state."""
        model = cls.__new__(cls)

        # Restore params
        if "params" in data:
            p = data["params"]
            # Use `or default` so a stored null/None/0 for any param falls
            # back gracefully to the default rather than propagating None.
            model.params = ThermalParameters(
                heat_loss_coeff=float(
                    p.get("heat_loss_coeff") or DEFAULT_HEAT_LOSS_COEFFICIENT
                ),
                thermal_mass=float(
                    p.get("thermal_mass") or DEFAULT_THERMAL_MASS
                ),
                heating_power=float(
                    p.get("heating_power") or DEFAULT_HEATING_POWER
                ),
                solar_gain_factor=float(
                    p.get("solar_gain_factor") or DEFAULT_SOLAR_GAIN_FACTOR
                ),
            )
        else:
            model.params = ThermalParameters()

        model.idle_count = data.get("idle_count", 0)
        model.active_count = data.get("active_count", 0)
        model.total_updates = data.get("total_updates", 0)
        model.state = data.get("state", STATE_LEARNING)
        # None is stored when the value was float("inf") — restore that.
        _mpe = data.get("mean_prediction_error")
        model.mean_prediction_error = float("inf") if _mpe is None else float(_mpe)
        model._h_over_c_sum = data.get("_h_over_c_sum", 0.0)
        model._h_over_c_count = data.get("_h_over_c_count", 0)
        model._measured_power_count = data.get("_measured_power_count", 0)
        model._measured_power_sum = 0.0  # not persisted; derived from EMA in params
        model.h_history = data.get("h_history", [])
        model.prediction_error_history = data.get("prediction_error_history", [])
        model._last_obs = None
        model._heat_source_state = data.get("heat_source")

        # Restore observations
        model.observations = []
        for obs_data in data.get("observations", []):
            model.observations.append(
                ThermalObservation(
                    timestamp=obs_data["timestamp"],
                    t_indoor=obs_data["t_indoor"],
                    t_outdoor=obs_data["t_outdoor"],
                    heating_on=obs_data["heating_on"],
                    solar_irradiance=obs_data.get("solar_irradiance", 0.0),
                    heat_power_w=obs_data.get("heat_power_w"),
                )
            )

        # Restore EKF
        model._ekf = None
        model._ekf_dict = data.get("ekf")
        if HAS_NUMPY and model._ekf_dict:
            model._ekf = ThermalEKF.from_dict(model._ekf_dict)
        elif HAS_NUMPY:
            model._ekf = ThermalEKF()

        return model
