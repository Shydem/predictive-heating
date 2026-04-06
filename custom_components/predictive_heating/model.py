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

from .const import (
    J_PER_KWH,
    TRAINING_C_BOUNDS,
    TRAINING_INITIAL_C,
    TRAINING_INITIAL_UA,
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

    # Phase 1 metadata
    t_outdoor_avg_training: float | None = None
    """Mean outdoor temperature used in training. Set when use_constant_outdoor=True."""

    training_mode: str = "phase2_variable_outdoor"
    """Which training mode was used: 'phase1_constant_outdoor' or 'phase2_variable_outdoor'."""

    q_heating_source: str = "heater_onoff"
    """How Q_heating was derived: 'gas' or 'heater_onoff'."""

    @property
    def c_joules(self) -> float:
        """Thermal capacitance in J/K (SI units for calculations)."""
        return self.thermal_mass * J_PER_KWH

    def describe(self) -> str:
        """Human-readable summary."""
        return (
            f"UA={self.ua:.1f} W/K, C={self.thermal_mass:.1f} kWh/K, "
            f"R²={self.r_squared:.3f}, trained on {self.n_data_points} points "
            f"[{self.training_mode}, Q from {self.q_heating_source}]"
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
    use_constant_outdoor: bool = True,
    q_heating_source: str = "heater_onoff",
    trace: Trace | None = None,
) -> tuple[ThermalParams, list[dict]]:
    """Fit UA and thermal_mass from historical data using direct linear regression.

    Model: C × dT/dt = Q_total − UA × (T_in − T_out)

    Rearranged:  dT/dt = (1/C) × Q_total − (UA/C) × ΔT

    This is a linear system: y = a·x₁ + b·x₂
      where  a = 1/C_joules,  b = UA/C_joules

    Solved analytically with least squares — no iterative optimizer.
    Works on as few as 5 data points. Typically runs in milliseconds.

    After fitting a and b, forward-simulate the temperature trajectory
    to compute R² (how well the model reproduces the measured history).
    """
    if trace is None:
        trace = Trace("training")

    n = len(timestamps)
    trace.step("start", inputs={"data_points": n, "use_constant_outdoor": use_constant_outdoor})

    if n < TRAINING_MIN_POINTS:
        trace.warn("insufficient_data",
            f"Need at least {TRAINING_MIN_POINTS} points, got {n}. "
            "Returning defaults — wait for more history to accumulate.",
            points=n)
        return ThermalParams(), []

    t_in = np.array(t_indoor, dtype=np.float64)
    t_out = np.array(t_outdoor, dtype=np.float64)
    q_heat = np.array(q_heating_w, dtype=np.float64)
    q_solar = np.array(q_solar_w, dtype=np.float64)
    q_int = np.array(q_internal_w, dtype=np.float64)

    # Phase 1: replace per-timestep outdoor with a single mean value
    t_outdoor_avg: float | None = None
    if use_constant_outdoor:
        t_outdoor_avg = float(np.mean(t_out))
        t_out = np.full(n, t_outdoor_avg)
        trace.step("phase1_constant_outdoor", result={
            "t_outdoor_avg": round(t_outdoor_avg, 2),
            "t_outdoor_original_range": (
                f"{float(np.array(t_outdoor).min()):.1f}–{float(np.array(t_outdoor).max()):.1f} °C"
            ),
        })

    # Time deltas between consecutive samples (seconds)
    dts = np.array([
        max((timestamps[i + 1] - timestamps[i]).total_seconds(), 1.0)
        for i in range(n - 1)
    ])

    trace.step("data_stats", result={
        "t_indoor_range": f"{t_in.min():.1f}–{t_in.max():.1f} °C",
        "t_outdoor_eff": f"{t_out.min():.1f}–{t_out.max():.1f} °C",
        "mean_heating_w": f"{q_heat.mean():.0f} W",
        "total_hours": f"{dts.sum() / 3600:.1f}",
        "q_heating_source": q_heating_source,
    })

    # ── Linear regression ─────────────────────────────────────────────────────
    # Compute dT/dt [K/s] for each interval
    dTdt = (t_in[1:] - t_in[:-1]) / dts

    # Total heat input per interval [W]
    Q_total = q_heat[:-1] + q_solar[:-1] + q_int[:-1]

    # Temperature delta from outdoor [K]
    delta_T = t_in[:-1] - t_out[:-1]

    # Build regression matrix A (n-1 rows, 2 columns)
    # Row i: [Q_total_i, -delta_T_i]
    A = np.column_stack([Q_total, -delta_T])

    try:
        coeff, _, rank, _ = np.linalg.lstsq(A, dTdt, rcond=None)
        a, b = float(coeff[0]), float(coeff[1])
    except np.linalg.LinAlgError as err:
        trace.error("lstsq_failed", f"Linear regression failed: {err}")
        return ThermalParams(), []

    # Guard: coefficients must be physically positive
    # a = 1/C_joules → if a ≤ 0, data has insufficient heating variation
    # b = UA/C_joules → if b ≤ 0, house appears to not lose heat (implausible)
    a_min = 1.0 / (TRAINING_C_BOUNDS[1] * J_PER_KWH)  # from C_max bound
    b_min = TRAINING_UA_BOUNDS[0] / (TRAINING_C_BOUNDS[1] * J_PER_KWH)  # UA_min / C_max
    if a <= a_min or b <= b_min:
        trace.warn("degenerate_fit",
            f"Regression gave a={a:.2e}, b={b:.2e} (physically implausible). "
            "Possible causes: heater was never on/off, or data covers too short a period. "
            "Using initial estimates as fallback.",
            a=a, b=b)
        # Fall back to house-profile estimates rather than garbage values
        return ThermalParams(
            ua=TRAINING_INITIAL_UA,
            thermal_mass=TRAINING_INITIAL_C,
            r_squared=0.0,
            last_trained=datetime.now(),
            n_data_points=n,
            t_outdoor_avg_training=t_outdoor_avg,
            training_mode="phase1_constant_outdoor" if use_constant_outdoor else "phase2_variable_outdoor",
            q_heating_source=q_heating_source,
        ), []

    # Recover physical parameters from regression coefficients
    C_joules = 1.0 / a                  # J/K
    UA = b * C_joules                   # W/K  (= b/a)

    # Apply physical bounds
    C_kwh = float(np.clip(C_joules / J_PER_KWH, *TRAINING_C_BOUNDS))
    UA = float(np.clip(UA, *TRAINING_UA_BOUNDS))
    C_joules = C_kwh * J_PER_KWH

    # ── Forward simulation to compute R² on temperature trajectory ────────────
    # Simulates the house using the fitted params and measures how well
    # it reproduces the actual indoor temperature history.
    predicted = np.empty(n)
    predicted[0] = t_in[0]
    for i in range(1, n):
        q_loss = UA * (predicted[i - 1] - t_out[i - 1])
        q_net = q_heat[i - 1] + q_solar[i - 1] + q_int[i - 1] - q_loss
        predicted[i] = predicted[i - 1] + (q_net / C_joules) * dts[i - 1]

    ss_res = float(np.sum((t_in[1:] - predicted[1:]) ** 2))
    ss_tot = float(np.sum((t_in[1:] - np.mean(t_in[1:])) ** 2))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    params = ThermalParams(
        ua=UA,
        thermal_mass=C_kwh,
        r_squared=r2,
        last_trained=datetime.now(),
        n_data_points=n,
        t_outdoor_avg_training=t_outdoor_avg,
        training_mode="phase1_constant_outdoor" if use_constant_outdoor else "phase2_variable_outdoor",
        q_heating_source=q_heating_source,
    )

    trace.step("fit_done", result={
        "ua": round(UA, 1),
        "thermal_mass_kwh_k": round(C_kwh, 2),
        "r_squared": round(r2, 4),
        "regression_rank": int(rank) if isinstance(rank, (int, float)) else rank,
    }, note=params.describe())

    if r2 < 0.5:
        trace.warn("low_r_squared",
            f"R²={r2:.3f} is low. "
            "Check: (1) heater was on AND off during training window, "
            "(2) heater power_w matches the actual device nameplate, "
            "(3) sensor data is continuous with no long gaps.",
            r_squared=r2)

    # Build residuals for visualization (sampled to stay under 16KB HA limit)
    errors = t_in[1:] - predicted[1:]
    step = max(1, (n - 1) // TRAINING_MAX_RESIDUAL_POINTS)
    residuals = [
        {
            "ts": timestamps[i + 1].isoformat(),
            "measured": round(float(t_in[i + 1]), 2),
            "predicted": round(float(predicted[i + 1]), 2),
            "error": round(float(errors[i]), 3),
        }
        for i in range(0, n - 1, step)
    ]

    return params, residuals
