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
    COUPLING_LEARN_RATE,
    COUPLING_U_MAX,
    COUPLING_U_MIN,
    DEFAULT_BUILDING_TYPE,
    DEFAULT_CEILING_HEIGHT_M,
    DEFAULT_COUPLING_U,
    DEFAULT_HEAT_LOSS_COEFFICIENT,
    DEFAULT_HEATING_POWER,
    DEFAULT_SOLAR_GAIN_FACTOR,
    DEFAULT_THERMAL_MASS,
    MIN_ACTIVE_SAMPLES,
    MIN_IDLE_SAMPLES,
    PREDICTION_HISTORY_MAX,
    PREDICTION_HORIZON_HOURS,
    STATE_CALIBRATED,
    STATE_LEARNING,
)


def _proportional_heat_plan(
    *,
    t_indoor: float,
    setpoint_trace: list[float],
    outdoor_trace: list[float],
    solar_trace: list[float],
    params,
    hours: int,
    band: float = 0.6,
) -> list[float]:
    """Derive a per-hour 0..1 heating fraction that tracks ``setpoint_trace``.

    This is a simple forward-sim proportional controller. We walk the
    temperature forward hour by hour; at each step the heating
    fraction is scaled linearly with the error (target - temp) over
    the proportional band. This is what produces a realistic "the
    boiler modulated around 50% for two hours as the setpoint held"
    recorded forecast, instead of the old "no heat ever" behaviour
    which drifted badly after any scheduled transition.
    """
    p = params
    C_watt_h = p.thermal_mass * 1000 / 3600
    if C_watt_h <= 0 or hours <= 0:
        return []

    plan: list[float] = []
    t = t_indoor
    for h in range(max(1, hours)):
        target = float(
            setpoint_trace[min(len(setpoint_trace) - 1, h)]
        )
        error = target - t
        frac = max(0.0, min(1.0, 0.5 + error / band))
        plan.append(frac)
        # Step the temperature forward one hour at this fraction so the
        # next iteration's error reflects the thermal response.
        q_heat = frac * p.heating_power
        q_solar = (
            solar_trace[min(len(solar_trace) - 1, h)] * p.solar_gain_factor
            if solar_trace else 0.0
        )
        t_out = (
            outdoor_trace[min(len(outdoor_trace) - 1, h)]
            if outdoor_trace else t
        )
        q_loss = p.heat_loss_coeff * (t - t_out)
        t += (q_heat + q_solar - q_loss) / C_watt_h  # 1 h step
    return plan


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
    # Summed heat contribution (W) from neighbouring rooms via
    # multi-room thermal coupling. The neighbour temperatures are
    # computed and aggregated by the climate entity before being
    # passed to the model.
    coupling_power_w: float = 0.0


@dataclass
class CouplingSpec:
    """Description of a thermal connection to another room.

    Two U-values are tracked — one for when the linking door is closed
    (solid partition conductance) and one for when it is open (open
    doorway conductance, typically 5–10× larger). A ``door_sensor``
    binary_sensor selects which U to apply at each tick. When no door
    sensor is configured we always use ``u_closed``.

    Both U-values can be *learned online* from observed data — when
    ``learn`` is True the model's update loop nudges them toward the
    value that best explains the sign/magnitude of observed cross-room
    temperature divergence. This lets the user declare the connection
    without having to know the exact conductance.
    """

    # entry_id of the other predictive-heating room
    neighbour_entry_id: str
    # Heat-exchange coefficient (W/K) when the door between the two
    # rooms is *closed*. Kept under the legacy name ``u_value`` for
    # back-compat with pickles from v0.6.
    u_value: float = DEFAULT_COUPLING_U
    # Conductance when the door is reported open. Initialised to a
    # "typical doorway" prior; auto-learned when ``learn`` is True.
    u_open: float = 100.0
    # Optional binary_sensor.* entity reporting door state
    # ("on" == open).
    door_sensor: str | None = None
    # If false, the coupling is *declared* but not included in the
    # model step — useful for keeping an edge in the config while
    # temporarily ignoring it.
    enabled: bool = True
    # Whether to let the online learner update u_value / u_open from
    # observations. Defaults True so the user gets auto-calibration
    # without extra clicks.
    learn: bool = True

    @property
    def u_closed(self) -> float:
        """Conductance with the door closed (alias for u_value)."""
        return self.u_value

    @u_closed.setter
    def u_closed(self, value: float) -> None:
        self.u_value = value

    def active_u(self, door_is_open: bool | None) -> float:
        """Return the U-value to use given the current door state.

        ``door_is_open`` is ``None`` when the coupling has no linked
        sensor or its state is unknown — we assume closed (the more
        conservative choice: smaller conductance → less cross-talk
        attributed to the coupling, so learning is slower but safer).
        """
        return self.u_open if door_is_open else self.u_value


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

    # Multi-room coupling (v0.5).
    # ``couplings`` is the declared list of possible connections; only
    # those marked ``enabled`` participate in the learning / prediction
    # math. ``last_dT_observed`` and ``last_dT_predicted`` keep the raw
    # EKF residual so the gas-heat source can distinguish cooking /
    # shower spikes from real space heating.
    couplings: list = field(default_factory=list)  # list[CouplingSpec]
    last_dT_observed: float = 0.0
    last_dT_predicted: float = 0.0

    # Prediction trace: for each periodic update we log the rolling
    # 8-hour forecast so the dashboard can overlay "what we thought
    # would happen 8 hours ago" against the observed trajectory.
    prediction_history: list = field(default_factory=list)

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

        # Merge the coupling heat contribution into the measured heat
        # input. The EKF treats this as extra "known" heat-in, so H
        # estimates stay clean even when neighbour rooms are warmer.
        measured_w = prev.heat_power_w
        coupling_w = prev.coupling_power_w
        if coupling_w:
            measured_w = (measured_w or 0.0) + coupling_w

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
                measured_heat_w=measured_w,
            )

            # Record the raw dT vs predicted dT for spike detection:
            # if the gas source reported heat_power_w but the room didn't
            # warm as expected we need to flag it (see heat_source.py).
            self.last_dT_observed = dT
            self.last_dT_predicted = dT - innovation

            # ── Online coupling-U learner (v0.7) ─────────────────────
            # Nudge each learnable coupling's U-value in the direction
            # that would better explain the innovation (observed minus
            # predicted dT). Per-spec, per-door-state — so u_closed and
            # u_open converge independently depending on which door
            # state dominated recent observations.
            self._learn_couplings(prev, dt_hours, innovation)

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

    def _learn_couplings(
        self,
        prev: ThermalObservation,
        dt_hours: float,
        innovation: float,
    ) -> None:
        """Online gradient update for per-coupling U-values.

        The integration's climate entity stashes
        ``spec._last_neighbour_temp`` and ``spec._last_door_open`` on each
        :class:`CouplingSpec` every time it computes the neighbour-coupling
        heat flux. We use those stashed values together with the EKF
        innovation (observed dT minus predicted dT) to drive a very small,
        bounded gradient step per observation:

            * If the neighbour is warmer than us (``dT_neigh > 0``) and the
              room warmed *more* than predicted (``innovation > 0``), the
              current U was too low → increase it.
            * If the neighbour is warmer and we warmed *less* than
              predicted, U was overstated → decrease it.
            * Symmetric for a colder neighbour.

        u_closed and u_open are learned *separately*, selected at each tick
        by the stashed door-state, which is why the door sensor is the
        single most useful per-coupling input the user can provide.

        The learner is intentionally crude: the EKF can absorb unexplained
        heat into H, so a large learn-rate would fight it. We keep the rate
        small (``COUPLING_LEARN_RATE``) and clamp each per-step change to a
        physically reasonable window, so the net effect is a slow drift
        toward the value that minimally reduces residuals — good enough for
        a monitor-first integration, and safe even when there are several
        couplings that can "explain" the same anomaly.
        """
        if not self.couplings:
            return
        # Skip tiny sample intervals (too noisy) and outlier innovations
        # that are probably spurious (window opened, heat-pump DHW spike,
        # sensor glitch).
        if dt_hours <= 0 or abs(innovation) > 1.5:
            return

        for spec in self.couplings:
            if not getattr(spec, "enabled", True):
                continue
            if not getattr(spec, "learn", True):
                continue
            t_neigh = getattr(spec, "_last_neighbour_temp", None)
            if t_neigh is None:
                continue
            door_open = bool(getattr(spec, "_last_door_open", False))
            dT_neigh = float(t_neigh) - float(prev.t_indoor)
            # Weak driving gradient — below ~0.1 °C the innovation is
            # dominated by measurement noise, not by this coupling.
            if abs(dT_neigh) < 0.1:
                continue

            # Gradient sign: same sign as innovation * sign(dT_neigh).
            direction = 1.0 if dT_neigh > 0 else -1.0
            # Scale by dt so long-interval observations aren't dwarfed by
            # tight-cadence ones; cap the effective dt so a 90-min gap
            # doesn't produce a giant step.
            eff_dt = max(0.05, min(dt_hours, 0.5))
            step = COUPLING_LEARN_RATE * innovation * direction * eff_dt
            # Hard cap: never move a single U by more than 5 W/K per tick.
            step = max(-5.0, min(5.0, step))

            if door_open:
                new_u = spec.u_open + step
                spec.u_open = max(COUPLING_U_MIN, min(COUPLING_U_MAX, new_u))
            else:
                new_u = spec.u_value + step
                spec.u_value = max(COUPLING_U_MIN, min(COUPLING_U_MAX, new_u))

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

    def predict_trajectory(
        self,
        *,
        t_indoor: float,
        hours_ahead: float,
        outdoor_trace: list[float] | None,
        solar_trace: list[float] | None,
        heating_fraction_trace: list[float] | None,
        step_minutes: float = 15.0,
    ) -> list[dict]:
        """
        Simulate a detailed forecast returning a list of
        ``{"t": offset_hours, "temperature": °C, "q_heat_w": W,
          "q_solar_w": W, "q_loss_w": W, "heating_fraction": 0..1}``
        for each simulation step.

        ``outdoor_trace``, ``solar_trace`` and ``heating_fraction_trace``
        are optional lists with one entry per **hour**. Shorter traces
        are padded by repeating the last known value; ``None`` treats
        the input as constant (last known).
        """
        p = self.params
        C_watt_h = p.thermal_mass * 1000 / 3600
        if C_watt_h <= 0 or hours_ahead <= 0:
            return []

        step_h = step_minutes / 60.0
        steps = max(1, int(hours_ahead / step_h))

        def _sample(trace: list[float] | None, fallback: float, hour_offset: float) -> float:
            if not trace:
                return fallback
            idx = min(len(trace) - 1, max(0, int(hour_offset)))
            return float(trace[idx])

        t = t_indoor
        out: list[dict] = []
        last_outdoor = outdoor_trace[0] if outdoor_trace else 10.0
        for step in range(steps):
            hour_offset = step * step_h
            t_outdoor = _sample(outdoor_trace, last_outdoor, hour_offset)
            solar = _sample(solar_trace, 0.0, hour_offset)
            heat_frac = _sample(heating_fraction_trace, 0.0, hour_offset)

            q_heat = heat_frac * p.heating_power
            q_solar = solar * p.solar_gain_factor
            q_loss = p.heat_loss_coeff * (t - t_outdoor)

            dT = (q_heat + q_solar - q_loss) / C_watt_h * step_h
            t += dT

            out.append(
                {
                    "t": round(hour_offset + step_h, 3),
                    "temperature": round(t, 3),
                    "q_heat_w": round(q_heat, 1),
                    "q_solar_w": round(q_solar, 1),
                    "q_loss_w": round(q_loss, 1),
                    "heating_fraction": round(heat_frac, 3),
                    "t_outdoor": round(t_outdoor, 2),
                }
            )
        return out

    def record_prediction_snapshot(
        self,
        *,
        timestamp: float,
        t_indoor: float,
        t_outdoor: float,
        solar_irradiance: float,
        horizon_hours: float = PREDICTION_HORIZON_HOURS,
        setpoint_trace: list[float] | None = None,
        heating_fraction_trace: list[float] | None = None,
    ) -> None:
        """
        Store a forecast curve for later comparison with reality.

        Keeps the model's opinion of the *next* ``horizon_hours`` hours
        around so the dashboard can overlay "prediction from T-8h" on
        top of the recorded actual trajectory.

        ``setpoint_trace`` is an optional per-hour target temperature
        that lets the snapshot account for scheduled transitions (e.g.
        the setpoint dropping to the sleep preset at 22:00). When
        provided AND ``heating_fraction_trace`` is not, we derive a
        simple proportional heat schedule so the recorded forecast
        actually follows the setpoint profile — otherwise the overlay
        would ignore the schedule entirely and drift away from reality
        after the first scheduled transition, which is the bug Sietse
        reported ("prediction from 8 hours ago doesn't include the
        schedule").
        """
        # Very coarse outdoor trace — one point per hour, flat at the
        # current outdoor reading. We accept this simplification
        # because the dashboard overlay only needs a rough "was the
        # model right?" check, not a science-grade forecast.
        hours = int(horizon_hours)
        outdoor_trace = [t_outdoor] * max(1, hours)
        # Solar: naive fall-off — irradiance halves every 4 hours.
        # Replaced at call time by the climate entity when a richer
        # solar schedule is available.
        solar_trace = [solar_irradiance * (0.5 ** (h / 4.0)) for h in range(hours)]

        # If the caller provided a setpoint trace but no explicit
        # heating-fraction trace, build a proportional heat plan from
        # it. This mirrors how the real controller modulates around
        # the setpoint, so the recorded forecast tracks scheduled
        # transitions instead of assuming "no heat" forever.
        if heating_fraction_trace is None and setpoint_trace:
            heating_fraction_trace = _proportional_heat_plan(
                t_indoor=t_indoor,
                setpoint_trace=setpoint_trace,
                outdoor_trace=outdoor_trace,
                solar_trace=solar_trace,
                params=self.params,
                hours=hours,
            )

        traj = self.predict_trajectory(
            t_indoor=t_indoor,
            hours_ahead=horizon_hours,
            outdoor_trace=outdoor_trace,
            solar_trace=solar_trace,
            heating_fraction_trace=heating_fraction_trace,
            step_minutes=15.0,
        )
        # Annotate each step with the active setpoint so the dashboard
        # can draw the scheduled target alongside the predicted temp.
        if setpoint_trace and traj:
            for entry in traj:
                idx = min(len(setpoint_trace) - 1, int(entry["t"]))
                entry["setpoint"] = round(float(setpoint_trace[idx]), 2)
        self.prediction_history.append(
            {
                "ts": timestamp,
                "horizon_hours": horizon_hours,
                "t_indoor": t_indoor,
                "t_outdoor": t_outdoor,
                "solar_irradiance": solar_irradiance,
                "setpoint_trace": (
                    [round(float(x), 2) for x in setpoint_trace]
                    if setpoint_trace
                    else None
                ),
                "trajectory": traj,
            }
        )
        if len(self.prediction_history) > PREDICTION_HISTORY_MAX:
            self.prediction_history = self.prediction_history[-PREDICTION_HISTORY_MAX:]

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
                    "coupling_power_w": obs.coupling_power_w,
                }
            )

        couplings_list = [
            {
                "neighbour_entry_id": c.neighbour_entry_id,
                "u_value": c.u_value,
                "enabled": c.enabled,
            }
            for c in self.couplings
        ]

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
            "couplings": couplings_list,
            "prediction_history": self.prediction_history[-PREDICTION_HISTORY_MAX:],
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
        model.prediction_history = data.get("prediction_history", [])
        model._last_obs = None
        model._heat_source_state = data.get("heat_source")
        model.last_dT_observed = float(data.get("last_dT_observed") or 0.0)
        model.last_dT_predicted = float(data.get("last_dT_predicted") or 0.0)

        # Couplings
        model.couplings = []
        for c in data.get("couplings", []) or []:
            try:
                model.couplings.append(
                    CouplingSpec(
                        neighbour_entry_id=c["neighbour_entry_id"],
                        u_value=float(c.get("u_value", DEFAULT_COUPLING_U)),
                        enabled=bool(c.get("enabled", True)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

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
                    coupling_power_w=obs_data.get("coupling_power_w", 0.0),
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
