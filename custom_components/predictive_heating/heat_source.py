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
    MIN_GAS_DT_SECONDS,
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

    # --------------------------------------------------------------

    def reset(self) -> None:
        self._last_m3 = None
        self._last_ts = None
        self._last_power_w = 0.0
        self._last_power_ts = 0.0

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

        energy_mj = delta_m3 * self.calorific_value_mj_m3 * self.efficiency
        gross_power_w = energy_mj * 1.0e6 / dt
        heat_power_w = gross_power_w * self.heat_share

        self._last_power_w = heat_power_w
        self._last_power_ts = timestamp
        return heat_power_w

    def current_power_w(self, stale_after_s: float = 900.0) -> float:
        """
        Return the most recently measured heat power (W).

        If no fresh reading is available within ``stale_after_s``, we
        assume the boiler is idle and return 0.0.
        """
        if self._last_power_ts == 0.0:
            return 0.0
        age = time.time() - self._last_power_ts
        if age > stale_after_s:
            return 0.0
        return self._last_power_w

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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GasHeatSource":
        src = cls(
            calorific_value_mj_m3=data.get(
                "calorific_value_mj_m3", DEFAULT_GAS_CALORIFIC_VALUE
            ),
            efficiency=data.get("efficiency", DEFAULT_BOILER_EFFICIENCY),
            heat_share=data.get("heat_share", DEFAULT_HEAT_SHARE),
        )
        src._last_m3 = data.get("last_m3")
        src._last_ts = data.get("last_ts")
        src._last_power_w = data.get("last_power_w", 0.0)
        src._last_power_ts = data.get("last_power_ts", 0.0)
        return src
