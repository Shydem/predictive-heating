"""First-order lumped capacitance thermal model.

This module contains pure functions and simple data classes.
No Home Assistant imports, no side effects, no I/O.
Everything is testable in isolation with plain Python.

The model equation:
    C × dT/dt = Q_heating + Q_solar + Q_internal − UA × (T_indoor − T_outdoor)

Rearranged for a forward Euler step:
    T_new = T_old + (Q_net / C) × dt

Where:
    Q_net = Q_heating + Q_solar + Q_internal − UA × (T_old − T_outdoor)

Two parameters are fitted from historical data:
    UA  — heat loss coefficient in W/K  (bigger = leakier house)
    C   — thermal capacitance in kWh/K  (bigger = slower to heat/cool)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from scipy.optimize import minimize

from .const import (
    J_PER_KWH,
    TRAINING_C_BOUNDS,
    TRAINING_INITIAL_C,
    TRAINING_INITIAL_UA,
    TRAINING_MAX_ITER,
    TRAINING_MAX_RESIDUAL_POINTS,
    TRAINING_MIN_POINTS,
    TRAINING_UA_BOUNDS,
)
from .trace import Trace

_LOGGER = logging.getLogger(__name__)


# ─── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class ThermalParams:
    """The two fitted model parameters plus metadata."""

    ua: float = TRAINING_INITIAL_UA
    """Heat loss coefficient in W/K. Bigger = leakier house."""

    thermal_mass: float = TRAINING_INITIAL_C
    """Thermal capacitance in kWh/K. Bigger = slower to heat/cool."""

    r_squared: float = 0.0
    """Goodness of fit. 1.0 = perfect, 0.0 = useless."""

    last_trained: datetime | None = None
    n_data_points: int = 0

    @property
    def c_joules(self) -> float:
        """Thermal capacitance in J/K (SI units for calculations)."""
        return self.thermal_mass * J_PER_KWH

    def describe(self) -> str:
        """Human-readable summary."""
        return (
            f"UA={self.ua:.1f} W/K, C={self.thermal_mass:.1f} kWh/K, "
            f"R²={self.r_squared:.3f}, trained on {self.n_data_points} points"
        )


@dataclass
class SimpleHeater:
    """A heating device with a known constant power output.

    Heat output is binary: either power_w watts (when on) or 0 W (when off).
    The on/off state is read from the HA entity at each timestep.
    """

    name: str
    entity_id: str
    """HA switch, climate, or binary_sensor entity to read state from."""

    power_w: float
    """Rated heat output in watts. Use the nameplate value."""


@dataclass
class SlotInput:
    """Input data for one optimization time slot."""

    start: datetime
    duration_s: float
    t_outdoor: float
    t_target: float
    electricity_price: float
    solar_gain_w: float = 0.0
    internal_gain_w: float = 200.0


@dataclass
class DeviceDecision:
    """What the optimizer decided for one heater in one slot."""

    device_name: str
    heating_on: bool
    heat_output_w: float
    cost_per_wh: float
    reason: str
    recommended_setpoint: float = 15.0
    """Temperature setpoint to send to this device's thermostat."""


@dataclass
class SlotResult:
    """Optimizer output for one time slot."""

    slot_index: int
    t_before: float
    t_after: float
    t_target: float
    t_without_heating: float
    heat_deficit_wh: float
    total_heating_w: float
    total_cost: float
    device_decisions: list[DeviceDecision]
    is_preheating: bool


@dataclass
class OptimizationResult:
    """Full output of one optimization run."""

    slot_results: list[SlotResult] = field(default_factory=list)
    predicted_temperatures: list[float] = field(default_factory=list)
    total_cost: float = 0.0
    trace: Trace | None = None


# ─── Pure calculation functions ───────────────────────────────────────────────
# Each function does ONE thing, takes explicit inputs, returns explicit output.


def euler_step(
    t_current: float,
    t_outdoor: float,
    ua: float,
    c_joules: float,
    q_heating_w: float,
    q_solar_w: float,
    q_internal_w: float,
    dt_seconds: float,
) -> tuple[float, float, float]:
    """One forward Euler temperature step.

    Returns:
        (t_new, q_loss_w, q_net_w) so you can see all intermediate values.
    """
    q_loss_w = ua * (t_current - t_outdoor)
    q_net_w = q_heating_w + q_solar_w + q_internal_w - q_loss_w
    dt_temp = (q_net_w / c_joules) * dt_seconds if c_joules > 0 else 0.0
    t_new = t_current + dt_temp
    return t_new, q_loss_w, q_net_w


def compute_heat_deficit_wh(
    c_joules: float, t_target: float, t_without_heating: float
) -> float:
    """How much heat energy (Wh) is needed to reach target from drift temp.

    Returns 0 if no heating is needed (house is warm enough).
    """
    delta_k = t_target - t_without_heating
    if delta_k <= 0:
        return 0.0
    return (c_joules * delta_k) / 3600.0  # J → Wh


# ─── Model trainer ────────────────────────────────────────────────────────────


def train_model(
    timestamps: list[datetime],
    t_indoor: list[float],
    t_outdoor: list[float],
    q_heating_w: list[float],
    q_solar_w: list[float],
    q_internal_w: list[float],
    trace: Trace | None = None,
) -> tuple[ThermalParams, list[dict]]:
    """Fit UA and thermal_mass from historical data.

    Uses least-squares: simulate forward from measured data,
    minimize sum of squared errors vs actual indoor temperature.

    Returns:
        (ThermalParams, residuals) where residuals is a list of dicts:
        [{ts, measured, predicted, error}] — useful for visualization.
    """
    if trace is None:
        trace = Trace("training")

    n = len(timestamps)
    trace.step("start", inputs={"data_points": n})

    if n < TRAINING_MIN_POINTS:
        trace.warn("insufficient_data",
            f"Need {TRAINING_MIN_POINTS} points, got {n}. Returning defaults.",
            points=n)
        return ThermalParams(), []

    # Time deltas between consecutive samples
    dts = np.array([
        max((timestamps[i] - timestamps[i - 1]).total_seconds(), 1.0)
        for i in range(1, n)
    ])

    t_in = np.array(t_indoor, dtype=np.float64)
    t_out = np.array(t_outdoor, dtype=np.float64)
    q_heat = np.array(q_heating_w, dtype=np.float64)
    q_solar = np.array(q_solar_w, dtype=np.float64)
    q_int = np.array(q_internal_w, dtype=np.float64)

    trace.step("data_stats", result={
        "t_indoor_range": f"{t_in.min():.1f} – {t_in.max():.1f} °C",
        "t_outdoor_range": f"{t_out.min():.1f} – {t_out.max():.1f} °C",
        "mean_heating_w": f"{q_heat.mean():.0f} W",
        "total_hours": f"{dts.sum() / 3600:.1f}",
    })

    eval_count = [0]

    def simulate(ua: float, c_kwh_k: float) -> np.ndarray:
        """Simulate indoor temperature trajectory for given UA and C."""
        c = c_kwh_k * J_PER_KWH
        predicted = np.empty(n)
        predicted[0] = t_in[0]
        for i in range(1, n):
            q_loss = ua * (predicted[i - 1] - t_out[i - 1])
            q_net = q_heat[i - 1] + q_solar[i - 1] + q_int[i - 1] - q_loss
            predicted[i] = predicted[i - 1] + (q_net / c) * dts[i - 1]
        return predicted

    def residual(x: np.ndarray) -> float:
        ua, c_kwh_k = x[0], x[1]
        if ua <= 0 or c_kwh_k <= 0:
            return 1e12
        predicted = simulate(ua, c_kwh_k)
        eval_count[0] += 1
        return float(np.sum((t_in[1:] - predicted[1:]) ** 2))

    x0 = np.array([TRAINING_INITIAL_UA, TRAINING_INITIAL_C])
    trace.step("optimize_start", inputs={
        "initial_ua": TRAINING_INITIAL_UA,
        "initial_c": TRAINING_INITIAL_C,
        "max_iterations": TRAINING_MAX_ITER,
    })

    result = minimize(
        residual, x0, method="Nelder-Mead",
        options={"maxiter": TRAINING_MAX_ITER, "xatol": 0.1, "fatol": 1e-6},
    )

    ua_fit = float(np.clip(result.x[0], *TRAINING_UA_BOUNDS))
    c_fit = float(np.clip(result.x[1], *TRAINING_C_BOUNDS))

    # Compute R²
    predicted_final = simulate(ua_fit, c_fit)
    ss_res = float(np.sum((t_in[1:] - predicted_final[1:]) ** 2))
    ss_tot = float(np.sum((t_in[1:] - np.mean(t_in[1:])) ** 2))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    params = ThermalParams(
        ua=ua_fit,
        thermal_mass=c_fit,
        r_squared=r2,
        last_trained=datetime.now(),
        n_data_points=n,
    )

    trace.step("optimize_done", result={
        "ua": round(ua_fit, 1),
        "thermal_mass": round(c_fit, 1),
        "r_squared": round(r2, 4),
        "iterations": result.nit,
        "function_evals": eval_count[0],
        "converged": result.success,
    }, note=params.describe())

    if r2 < 0.5:
        trace.warn("low_r_squared",
            f"R²={r2:.3f} is low. Check sensor data quality or heater power rating.",
            r_squared=r2)

    # Build residuals array for visualization
    # Sample evenly to stay within HA attribute size limits
    errors = t_in[1:] - predicted_final[1:]
    step = max(1, (n - 1) // TRAINING_MAX_RESIDUAL_POINTS)
    residuals = [
        {
            "ts": timestamps[i + 1].isoformat(),
            "measured": round(float(t_in[i + 1]), 2),
            "predicted": round(float(predicted_final[i + 1]), 2),
            "error": round(float(errors[i]), 3),
        }
        for i in range(0, n - 1, step)
    ]

    return params, residuals
