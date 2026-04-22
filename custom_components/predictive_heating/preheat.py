"""
Pre-heat planner — v0.3.

Given a schedule (a HA ``schedule.*`` entity) and the current thermal
state of a room, decide **when** to start heating so the room hits its
target exactly at the schedule's next ON transition, not after.

Key inputs:
    * ``thermal_model``  — provides ``time_to_reach()`` and
      ``predict_temperature()``.
    * The currently active schedule state + next-transition time.
    * Current indoor / outdoor temperature.
    * Optional weather forecast — the outdoor temp used for
      ``time_to_reach`` averages the forecast over the pre-heat window,
      so cold nights get a longer lead time than the snapshot outdoor
      temp would suggest.
    * Comfort ramp — ``"instant"`` jumps the target temp at the
      pre-heat start; ``"gradual"`` linearly interpolates the effective
      target during the pre-heat window so the room warms smoothly.

The planner returns a ``PreheatPlan`` describing what the *effective*
target temperature should be **now**. The caller (climate.py) applies
that target to the room.

Why this matters:
    Without predictive pre-heat the only options are
      (a) start heating when the schedule goes ON → room hits target
          30-60 min late, and
      (b) always keep the high target → wasteful.
    Pre-heat solves both: heat early enough to arrive on time, but
    only as early as necessary.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .const import STATE_CALIBRATED
from .thermal_model import ThermalModel

_LOGGER = logging.getLogger(__name__)


# Absolute minimum lead time we'll plan for. Below this the plan
# simplifies to "apply the high target immediately" — splitting hairs
# at sub-5-minute resolution is not useful given sensor update rates.
_MIN_LEAD_MIN = 5.0

# Safety margin: multiply the computed pre-heat minutes by this to
# account for modelling error / unexpected outdoor temperature drops.
_LEAD_MARGIN = 1.15


@dataclass
class PreheatConfig:
    """Tunable pre-heat behaviour."""

    # ``"instant"`` → target temp jumps to high at preheat_start.
    # ``"gradual"`` → target temp linearly ramps from low to high over
    #                 the pre-heat window. Gentler on thermal comfort.
    comfort_ramp: str = "gradual"
    # Max lead time we'll ever plan for. Beyond this it's cheaper to
    # just run a higher baseline target.
    max_lead_hours: float = 4.0
    # Heating power fraction to assume when estimating time_to_reach.
    # 1.0 = full blast; 0.8 leaves headroom for real-world losses and
    # the fact that we're using the *average* power (DHW draws dilute).
    assumed_heat_fraction: float = 0.8
    # Weight to average forecast outdoor temps with the current one.
    # 0 = use forecast only, 1 = use current only. 0.3 leans on forecast.
    current_outdoor_weight: float = 0.3


@dataclass
class PreheatPlan:
    """
    What the pre-heat planner recommends for **now**.

    Fields:
        effective_target_temp: what the climate should set as target
        in_preheat_window: whether we're actively pre-heating
        low_target: the schedule-off target (for display / fallback)
        high_target: the schedule-on target we're aiming to hit
        preheat_start_ts: unix timestamp when pre-heat began (or would)
        schedule_on_ts: unix timestamp of the next schedule ON transition
        lead_minutes: how many minutes before schedule_on we must start
        reason: short human-readable label
    """

    effective_target_temp: float
    in_preheat_window: bool
    low_target: float
    high_target: float
    preheat_start_ts: float | None
    schedule_on_ts: float | None
    lead_minutes: float
    reason: str

    def as_diagnostic(self) -> dict:
        """Serializable form for the climate entity attributes."""
        return {
            "effective_target_temp": round(self.effective_target_temp, 2),
            "preheat_active": self.in_preheat_window,
            "low_target": round(self.low_target, 2),
            "high_target": round(self.high_target, 2),
            "preheat_start_ts": self.preheat_start_ts,
            "schedule_on_ts": self.schedule_on_ts,
            "lead_minutes": round(self.lead_minutes, 1),
            "reason": self.reason,
        }


class PreheatPlanner:
    """Computes how much to pre-heat given schedule + weather + model."""

    def __init__(
        self,
        thermal_model: ThermalModel,
        config: PreheatConfig | None = None,
    ) -> None:
        self.model = thermal_model
        self.config = config or PreheatConfig()

    def plan(
        self,
        *,
        now_ts: float,
        t_indoor: float,
        t_outdoor: float,
        low_target: float,
        high_target: float,
        schedule_on: bool,
        next_transition_ts: float | None,
        forecast_hourly: list[float] | None = None,
        solar_irradiance: float = 0.0,
    ) -> PreheatPlan:
        """
        Build a plan for the current instant.

        Args:
            now_ts: unix timestamp of "now".
            t_indoor: current indoor temperature.
            t_outdoor: current outdoor temperature.
            low_target: target when schedule is off.
            high_target: target when schedule is on.
            schedule_on: is the schedule currently ON?
            next_transition_ts: unix ts of the next schedule flip; ``None``
                if unknown (no schedule configured or static).
            forecast_hourly: optional list of hourly forecast temps
                covering the next ``max_lead_hours``. Index 0 = next hour.
            solar_irradiance: current solar W/m² (passed through to
                ``time_to_reach`` for rooms with big sunny windows).
        """
        # If schedule is already ON, no pre-heat needed — we're in the
        # comfort window, apply the high target directly.
        if schedule_on:
            return PreheatPlan(
                effective_target_temp=high_target,
                in_preheat_window=False,
                low_target=low_target,
                high_target=high_target,
                preheat_start_ts=None,
                schedule_on_ts=None,
                lead_minutes=0.0,
                reason="schedule_on",
            )

        # Schedule off and no upcoming transition — stay at low target.
        if next_transition_ts is None or next_transition_ts <= now_ts:
            return PreheatPlan(
                effective_target_temp=low_target,
                in_preheat_window=False,
                low_target=low_target,
                high_target=high_target,
                preheat_start_ts=None,
                schedule_on_ts=next_transition_ts,
                lead_minutes=0.0,
                reason="no_upcoming_on",
            )

        # High target not actually higher → nothing to pre-heat to.
        if high_target <= low_target + 0.05:
            return PreheatPlan(
                effective_target_temp=low_target,
                in_preheat_window=False,
                low_target=low_target,
                high_target=high_target,
                preheat_start_ts=None,
                schedule_on_ts=next_transition_ts,
                lead_minutes=0.0,
                reason="no_temp_rise_needed",
            )

        # Already above the high target → no pre-heat needed.
        if t_indoor >= high_target:
            return PreheatPlan(
                effective_target_temp=high_target,
                in_preheat_window=False,
                low_target=low_target,
                high_target=high_target,
                preheat_start_ts=None,
                schedule_on_ts=next_transition_ts,
                lead_minutes=0.0,
                reason="already_at_high",
            )

        seconds_until_on = next_transition_ts - now_ts
        minutes_until_on = seconds_until_on / 60.0

        # Blend current outdoor temp with forecast average to get a
        # representative T_out for the pre-heat window. Colder nights
        # → longer lead time.
        t_outdoor_avg = self._outdoor_temp_average(
            t_outdoor=t_outdoor,
            forecast_hourly=forecast_hourly,
            hours=max(0.1, minutes_until_on / 60.0),
        )

        # Use the thermal model if calibrated; else use a conservative
        # rule of thumb (0.8°C per hour of heating).
        lead_minutes = self._estimate_lead_minutes(
            t_indoor=t_indoor,
            t_target=high_target,
            t_outdoor=t_outdoor_avg,
            solar_irradiance=solar_irradiance,
        )

        if lead_minutes <= _MIN_LEAD_MIN:
            # Too close to transition (or already there) — apply the
            # high target now. No structured pre-heat needed.
            return PreheatPlan(
                effective_target_temp=high_target,
                in_preheat_window=True,
                low_target=low_target,
                high_target=high_target,
                preheat_start_ts=now_ts,
                schedule_on_ts=next_transition_ts,
                lead_minutes=lead_minutes,
                reason="apply_high_now",
            )

        preheat_start_ts = next_transition_ts - lead_minutes * 60.0

        if now_ts < preheat_start_ts:
            # Still too early to pre-heat — keep the low target.
            return PreheatPlan(
                effective_target_temp=low_target,
                in_preheat_window=False,
                low_target=low_target,
                high_target=high_target,
                preheat_start_ts=preheat_start_ts,
                schedule_on_ts=next_transition_ts,
                lead_minutes=lead_minutes,
                reason="waiting_to_preheat",
            )

        # We're inside the pre-heat window.
        effective = self._effective_target(
            now_ts=now_ts,
            preheat_start_ts=preheat_start_ts,
            schedule_on_ts=next_transition_ts,
            low_target=low_target,
            high_target=high_target,
        )

        return PreheatPlan(
            effective_target_temp=effective,
            in_preheat_window=True,
            low_target=low_target,
            high_target=high_target,
            preheat_start_ts=preheat_start_ts,
            schedule_on_ts=next_transition_ts,
            lead_minutes=lead_minutes,
            reason="preheating",
        )

    # ── internals ────────────────────────────────────────────────

    def _estimate_lead_minutes(
        self,
        t_indoor: float,
        t_target: float,
        t_outdoor: float,
        solar_irradiance: float,
    ) -> float:
        """
        How many minutes of heating to go from ``t_indoor`` to ``t_target``.

        Uses the thermal model when calibrated; otherwise a conservative
        fallback (0.8°C per hour warmup).
        """
        if t_indoor >= t_target:
            return 0.0

        if self.model.state == STATE_CALIBRATED:
            hours = self.model.time_to_reach(
                t_indoor=t_indoor,
                t_target=t_target,
                t_outdoor=t_outdoor,
                heating_power_fraction=self.config.assumed_heat_fraction,
                solar_irradiance=solar_irradiance,
                max_hours=self.config.max_lead_hours,
            )
            if hours is not None:
                return min(hours * 60.0 * _LEAD_MARGIN,
                           self.config.max_lead_hours * 60.0)

        # Fallback: room warms at 0.8°C/hour when T_out < target.
        deg = t_target - t_indoor
        cold_bonus = max(0.0, (18.0 - t_outdoor) / 30.0)  # up to +50 % when it's 0°C outside
        rate_per_h = 0.8 / (1.0 + cold_bonus)
        return min(
            (deg / rate_per_h) * 60.0 * _LEAD_MARGIN,
            self.config.max_lead_hours * 60.0,
        )

    def _outdoor_temp_average(
        self,
        t_outdoor: float,
        forecast_hourly: list[float] | None,
        hours: float,
    ) -> float:
        """Average current outdoor temp with the forecast over the window."""
        if not forecast_hourly or hours <= 0:
            return t_outdoor

        n = max(1, int(round(hours)))
        sample = forecast_hourly[:n]
        if not sample:
            return t_outdoor
        forecast_avg = sum(sample) / len(sample)
        w = self.config.current_outdoor_weight
        return w * t_outdoor + (1.0 - w) * forecast_avg

    def _effective_target(
        self,
        now_ts: float,
        preheat_start_ts: float,
        schedule_on_ts: float,
        low_target: float,
        high_target: float,
    ) -> float:
        """Compute the current effective target inside the pre-heat window."""
        if self.config.comfort_ramp == "instant":
            return high_target

        # "gradual": linear ramp from low_target at start of pre-heat
        # to high_target at schedule_on, so the room warms smoothly
        # and the controller has a rising setpoint to track.
        total = max(1.0, schedule_on_ts - preheat_start_ts)
        progress = (now_ts - preheat_start_ts) / total
        progress = max(0.0, min(1.0, progress))
        return low_target + (high_target - low_target) * progress
