"""
Heat source measurement.

Translates a raw gas-meter reading (cumulative m³) into the average
thermal power delivered to the room since the previous sample. This
gives the thermal model a much more accurate "heat in" signal than the
old binary heating_on flag: the boiler modulates, runs for fractional
duty cycles, and shares gas with domestic hot-water draws, so a simple
on/off approximation loses information the EKF could otherwise exploit.

Formula (gas):
    delta_m3          = m3_now - m3_prev
    energy_MJ         = delta_m3 * calorific_value_MJ_per_m3 * efficiency
    gross_power_W     = energy_MJ * 1e6 / dt_seconds
    heat_to_room_W    = gross_power_W * heat_share

Units:
    - Dutch Groningen-gas billed calorific value ≈ 35.17 MJ/m³
    - HR-107 condensing boiler seasonal η ≈ 0.95
    - heat_share: the fraction of whole-house boiler heat that ends up
      in THIS room (for single-room or lump-sum setups, leave at 1.0;
      with multiple rooms configured, the user tunes these so the total
      per zone is ≈ 1.0).

Heat pump variant (future):
    For a heat pump with an electricity meter and a COP entity, the same
    abstraction applies: ``heat_to_room_W = electric_power_W * cop *
    heat_share``. The thermal model doesn't care where the watts come
    from — only what was delivered to the room.

This module is deliberately free of Home Assistant imports, so it can
be unit-tested without a running HA instance.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .const import (
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_HEAT_SHARE,
    MAX_SPIKE_DURATION_S,
    MIN_GAS_DT_SECONDS,
    SPIKE_EXPECTED_DT_RATIO,
    SPIKE_POWER_W,
    SPIKE_WINDOW_S,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class GasHeatSource:
    """Derivative-based heat estimator from a cumulative gas meter."""

    calorific_value_mj_m3: float = DEFAULT_GAS_CALORIFIC_VALUE
    efficiency: float = DEFAULT_BOILER_EFFICIENCY
    heat_share: float = DEFAULT_HEAT_SHARE

    # Bookkeeping for the derivative.
    _last_m3: float | None = field(default=None, init=False, repr=False)
    _last_ts: float | None = field(default=None, init=False, repr=False)
    # The most recently computed average power (W) between readings, so
    # the controller can sample it at update time without having to
    # provide the current gas reading itself.
    _last_power_w: float = field(default=0.0, init=False, repr=False)
    _last_power_ts: float = field(default=0.0, init=False, repr=False)
    # ─── Spike (cooking / shower) rejection ──────────────────────
    # Rolling window of (timestamp, power_w, dT_since_last_obs) tuples
    # used to decide whether the current gas consumption is actually
    # heating the room. When we decide it isn't, ``_in_spike`` is
    # flipped and ``current_power_w`` returns 0 until the spike
    # resolves (power drops below threshold OR window timer expires).
    _spike_samples: list = field(default_factory=list, init=False, repr=False)
    _in_spike: bool = field(default=False, init=False, repr=False)
    _spike_since_ts: float = field(default=0.0, init=False, repr=False)
    # Diagnostic counters
    _spike_events: int = field(default=0, init=False, repr=False)
    _spike_energy_mj: float = field(default=0.0, init=False, repr=False)

    # --------------------------------------------------------------

    def reset(self) -> None:
        self._last_m3 = None
        self._last_ts = None
        self._last_power_w = 0.0
        self._last_power_ts = 0.0
        self._spike_samples = []
        self._in_spike = False
        self._spike_since_ts = 0.0

    # ── Spike tracking ────────────────────────────────────────────
    def record_heating_result(
        self,
        *,
        dT_observed: float,
        dT_predicted: float,
        timestamp: float | None = None,
    ) -> None:
        """Called once per model update cycle with the observed vs
        predicted temperature change, so the filter can decide whether
        the last gas pulse was really heating the room.

        * ``dT_observed``: actual room dT (°C) over the update window.
        * ``dT_predicted``: what the current thermal model expected
           given the *raw* gas power.

        If the ratio dT_observed / dT_predicted stays below
        ``SPIKE_EXPECTED_DT_RATIO`` while the power is high, we flag a
        spike and zero out the heat input used by the EKF.
        """
        now = timestamp if timestamp is not None else time.time()
        self._spike_samples.append(
            {
                "ts": now,
                "power_w": self._last_power_w,
                "dT_obs": dT_observed,
                "dT_pred": dT_predicted,
            }
        )
        # Keep samples within the detection window only.
        cutoff = now - SPIKE_WINDOW_S
        self._spike_samples = [
            s for s in self._spike_samples if s["ts"] >= cutoff
        ]

        high_power_total = sum(
            max(s["power_w"] - SPIKE_POWER_W, 0.0) for s in self._spike_samples
        )
        obs_total = sum(max(s["dT_obs"], 0.0) for s in self._spike_samples)
        pred_total = sum(max(s["dT_pred"], 1e-6) for s in self._spike_samples)

        # Entering a spike: high gas power, but room didn't warm.
        if (
            not self._in_spike
            and high_power_total > 0
            and obs_total < SPIKE_EXPECTED_DT_RATIO * pred_total
            and self._last_power_w > SPIKE_POWER_W
        ):
            self._in_spike = True
            self._spike_since_ts = now
            self._spike_events += 1
            _LOGGER.debug(
                "Gas spike detected: power=%.0f W, dT_obs=%.3f, "
                "dT_pred=%.3f — attributing to DHW/cooking, not heating",
                self._last_power_w, obs_total, pred_total,
            )

        # Leaving a spike: power dropped OR max duration exceeded OR
        # room starts warming up again.
        if self._in_spike:
            max_expired = (now - self._spike_since_ts) > MAX_SPIKE_DURATION_S
            power_low = self._last_power_w < SPIKE_POWER_W / 2
            ratio_ok = obs_total >= SPIKE_EXPECTED_DT_RATIO * pred_total
            if max_expired or power_low or ratio_ok:
                self._in_spike = False

    def update_reading(
        self,
        m3_cumulative: float,
        timestamp: float | None = None,
    ) -> float | None:
        """
        Record a new meter reading and return the average power (W)
        over the interval since the previous reading, or ``None`` if
        no derivative can be computed yet.

        Args:
            m3_cumulative: current meter reading in m³.
            timestamp: unix timestamp for this reading; defaults to now.
        """
        if timestamp is None:
            timestamp = time.time()

        if self._last_m3 is None or self._last_ts is None:
            self._last_m3 = m3_cumulative
            self._last_ts = timestamp
            return None

        dt = timestamp - self._last_ts
        if dt < MIN_GAS_DT_SECONDS:
            # Need at least one minute to smooth out meter quantization.
            return None

        delta_m3 = m3_cumulative - self._last_m3

        # Advance the window regardless; meter resets (new billing cycle
        # or sensor reboot) manifest as negative deltas — treat as 0.
        self._last_m3 = m3_cumulative
        self._last_ts = timestamp

        if delta_m3 < 0:
            _LOGGER.debug(
                "Gas meter reading decreased (%.3f → %.3f) — treating as 0",
                self._last_m3, m3_cumulative,
            )
            delta_m3 = 0.0

        # Sanity cap: a 50 kW boiler at full tilt consumes at most ~6 m³/h
        # (≈ 0.0017 m³/s). Anything beyond 10× that rate almost certainly
        # means the stored baseline came from a different sensor (e.g. after
        # the user corrected the entity ID) or a unit mismatch (L vs m³).
        # In that case, discard the derivative and let the next tick form a
        # clean baseline rather than feeding a ~MW figure to the EKF.
        max_plausible_m3 = dt * (60.0 / 3600.0)  # 60 m³/h absolute ceiling
        if delta_m3 > max_plausible_m3:
            _LOGGER.warning(
                "Gas meter delta %.3f m³ in %.0f s is physically implausible "
                "(max %.3f m³) — baseline was likely from a different sensor. "
                "Resetting baseline; spike counter cleared.",
                delta_m3, dt, max_plausible_m3,
            )
            # Reset spike state too — the bogus reading may have triggered it.
            self._in_spike = False
            self._spike_samples.clear()
            self._last_power_w = 0.0
            return None

        energy_mj = delta_m3 * self.calorific_value_mj_m3 * self.efficiency
        gross_power_w = energy_mj * 1.0e6 / dt
        heat_power_w = gross_power_w * self.heat_share

        self._last_power_w = heat_power_w
        self._last_power_ts = timestamp
        return heat_power_w

    def current_power_w(self, stale_after_s: float = 900.0) -> float:
        """
        Return the most recently measured heat power (W) *usable as
        space-heating input*. Returns 0 during a detected cooking /
        shower spike, even if raw gas usage is high.
        """
        if self._last_power_ts == 0.0:
            return 0.0
        age = time.time() - self._last_power_ts
        if age > stale_after_s:
            return 0.0
        if self._in_spike:
            return 0.0
        return self._last_power_w

    def raw_power_w(self) -> float:
        """Last measured gross gas power (W), ignoring spike gating."""
        return self._last_power_w

    @property
    def in_spike(self) -> bool:
        return self._in_spike

    @property
    def spike_events(self) -> int:
        return self._spike_events

    # --------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "calorific_value_mj_m3": self.calorific_value_mj_m3,
            "efficiency": self.efficiency,
            "heat_share": self.heat_share,
            "last_m3": self._last_m3,
            "last_ts": self._last_ts,
            "last_power_w": self._last_power_w,
            "last_power_ts": self._last_power_ts,
            "spike_events": self._spike_events,
            "in_spike": self._in_spike,
            "spike_since_ts": self._spike_since_ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GasHeatSource":
        def _num(value, default, caster=float):
            """Coerce stored scalars defensively: ``None``/strings fall
            back to ``default`` instead of crashing ``from_dict`` (and,
            by extension, platform setup)."""
            if value is None:
                return default
            try:
                return caster(value)
            except (TypeError, ValueError):
                return default

        src = cls(
            calorific_value_mj_m3=_num(
                data.get("calorific_value_mj_m3"), DEFAULT_GAS_CALORIFIC_VALUE
            ),
            efficiency=_num(data.get("efficiency"), DEFAULT_BOILER_EFFICIENCY),
            heat_share=_num(data.get("heat_share"), DEFAULT_HEAT_SHARE),
        )
        last_m3 = data.get("last_m3")
        last_ts = data.get("last_ts")
        src._last_m3 = _num(last_m3, None) if last_m3 is not None else None
        src._last_ts = _num(last_ts, None) if last_ts is not None else None
        src._last_power_w = _num(data.get("last_power_w"), 0.0)
        src._last_power_ts = _num(data.get("last_power_ts"), 0.0)
        src._spike_events = _num(data.get("spike_events"), 0, int)
        src._in_spike = bool(data.get("in_spike", False))
        src._spike_since_ts = _num(data.get("spike_since_ts"), 0.0)
        return src
