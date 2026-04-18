"""
Heating zone manager.

Handles the case where multiple rooms share a single thermostat/boiler
circuit (no TRVs, just manual taps on radiators). This is common in
Dutch homes where e.g. woonkamer and slaapkamer are heated by the
same Honeywell T6 thermostat.

Key responsibilities:
- Group rooms by their shared climate entity (zone)
- When ANY room in a zone requests heat, ALL rooms reflect "heating"
- Gently nudge the thermostat setpoint toward a value that keeps the
  room at target, while preserving OpenTherm modulation
- Prevent conflicting setpoint commands to the same thermostat

Setpoint strategy — gentle nudging (NOT proportional):

    Background:
        Earlier versions boosted the setpoint proportionally to the
        temperature error (setpoint = target + error * 1.5, capped at
        target + 2.5°C). That guarantees overshoot on OpenTherm setups,
        because the thermostat interprets a big (setpoint − measured)
        gap as "run the boiler hot", and the flow temperature climbs
        far above what's actually needed.

    New approach:
        - Initial setpoint = target (NOT target + boost).
        - Re-evaluate every ``nudge_interval_min`` minutes.
        - If the room is persistently cold (> ``cold_band`` below
          target), nudge the setpoint UP by ``nudge_step`` (default
          0.5°C), capped at target + ``max_setpoint_delta`` (default
          1.0°C).
        - If the room is overshooting (> ``warm_band`` above target),
          nudge the setpoint DOWN by ``nudge_step``, floored at target.
        - Otherwise, pull the setpoint gently back toward target so
          small past boosts don't stick around.

    Why:
        Small setpoint offsets keep the OpenTherm flow-temp curve in a
        reasonable range, which is where the heat pump / condensing
        boiler is most efficient.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .const import (
    DEFAULT_MAX_SETPOINT_DELTA,
    DEFAULT_NUDGE_INTERVAL_MIN,
    DEFAULT_NUDGE_STEP,
    DOMAIN,
    NUDGE_COLD_BAND,
    NUDGE_WARM_BAND,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RoomHeatDemand:
    """A room's current heating demand within a zone."""

    entry_id: str
    room_name: str
    current_temp: float | None = None
    target_temp: float = 21.0
    wants_heat: bool = False
    window_open: bool = False

    @property
    def error(self) -> float:
        """Temperature error: how far below target (0 if at/above)."""
        if self.current_temp is None:
            return 0.0
        return max(0.0, self.target_temp - self.current_temp)


class HeatingZone:
    """A group of rooms sharing one physical thermostat."""

    def __init__(
        self,
        zone_id: str,
        climate_entity_id: str,
        max_setpoint_delta: float = DEFAULT_MAX_SETPOINT_DELTA,
        nudge_step: float = DEFAULT_NUDGE_STEP,
        nudge_interval_min: float = DEFAULT_NUDGE_INTERVAL_MIN,
    ) -> None:
        self.zone_id = zone_id
        self.climate_entity_id = climate_entity_id
        self.max_setpoint_delta = max_setpoint_delta
        self.nudge_step = nudge_step
        self.nudge_interval_seconds = nudge_interval_min * 60.0

        self._rooms: dict[str, RoomHeatDemand] = {}
        self._is_heating = False
        self._last_setpoint: float | None = None
        self._last_setpoint_time: float = 0.0

    # ── room registration / updates ──────────────────────────────

    def register_room(self, entry_id: str, room_name: str) -> None:
        """Register a room in this zone."""
        if entry_id not in self._rooms:
            self._rooms[entry_id] = RoomHeatDemand(
                entry_id=entry_id, room_name=room_name
            )
            _LOGGER.debug(
                "Room '%s' registered in zone '%s' (thermostat: %s)",
                room_name, self.zone_id, self.climate_entity_id,
            )

    def update_room_demand(
        self,
        entry_id: str,
        current_temp: float | None,
        target_temp: float,
        wants_heat: bool,
        window_open: bool = False,
    ) -> None:
        """Update a room's heating demand."""
        if entry_id in self._rooms:
            room = self._rooms[entry_id]
            room.current_temp = current_temp
            room.target_temp = target_temp
            room.wants_heat = wants_heat
            room.window_open = window_open

    def update_tuning(
        self,
        max_setpoint_delta: float | None = None,
        nudge_step: float | None = None,
        nudge_interval_min: float | None = None,
    ) -> None:
        """Update zone tuning (called when options change)."""
        if max_setpoint_delta is not None:
            self.max_setpoint_delta = max_setpoint_delta
        if nudge_step is not None:
            self.nudge_step = nudge_step
        if nudge_interval_min is not None:
            self.nudge_interval_seconds = nudge_interval_min * 60.0

    # ── state queries ────────────────────────────────────────────

    @property
    def any_room_wants_heat(self) -> bool:
        return any(
            r.wants_heat and not r.window_open
            for r in self._rooms.values()
        )

    @property
    def is_heating(self) -> bool:
        return self._is_heating

    @is_heating.setter
    def is_heating(self, value: bool) -> None:
        self._is_heating = value

    @property
    def max_error(self) -> float:
        errors = [
            r.error for r in self._rooms.values()
            if r.wants_heat and not r.window_open and r.current_temp is not None
        ]
        return max(errors) if errors else 0.0

    @property
    def leading_room(self) -> RoomHeatDemand | None:
        """The room with the largest heat demand (drives the zone)."""
        best = None
        best_error = -1.0
        for r in self._rooms.values():
            if r.wants_heat and not r.window_open and r.error > best_error:
                best = r
                best_error = r.error
        return best

    # ── setpoint nudging ─────────────────────────────────────────

    def _time_since_last_change(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        if self._last_setpoint_time == 0:
            return float("inf")
        return now - self._last_setpoint_time

    def _clamp_setpoint(self, setpoint: float, target: float) -> float:
        """Clamp setpoint to [target, target + max_setpoint_delta]."""
        upper = target + self.max_setpoint_delta
        return max(target, min(setpoint, upper))

    def calculate_setpoint(self, now: float | None = None) -> float | None:
        """
        Return the setpoint to push to the shared thermostat, or ``None``
        if nothing should be sent right now.

        Rules:
            - If no room wants heat → return None (caller will idle).
            - First call since boot → setpoint = target. Send it.
            - Room within the deadband (±warm_band) → pull the setpoint
              back toward target by up to nudge_step (small correction
              to undo past nudges), and only if ≥ nudge_interval since
              the last change. Returns None when no change is needed.
            - Room is cold → increase setpoint by nudge_step, once per
              nudge_interval. Capped at target + max_setpoint_delta.
            - Room is warm → decrease setpoint by nudge_step, once per
              nudge_interval. Floored at target.

        The returned value, if any, should be sent to the thermostat.
        """
        if not self.any_room_wants_heat:
            return None

        leader = self.leading_room
        if leader is None or leader.current_temp is None:
            return None

        target = leader.target_temp
        current = leader.current_temp
        deviation = target - current  # >0 cold, <0 warm

        # First time: start at the room's target.
        if self._last_setpoint is None:
            setpoint = self._clamp_setpoint(target, target)
            self._commit_setpoint(setpoint, now=now)
            return setpoint

        # Respect the minimum interval between nudges so the boiler has
        # time to react before we change our minds.
        if self._time_since_last_change(now) < self.nudge_interval_seconds:
            return None

        prev = self._last_setpoint
        step = self.nudge_step

        if deviation > NUDGE_COLD_BAND:
            # Room is persistently cold — nudge up.
            new_setpoint = self._clamp_setpoint(prev + step, target)
        elif deviation < -NUDGE_WARM_BAND:
            # Room is overshooting — nudge down.
            new_setpoint = self._clamp_setpoint(prev - step, target)
        else:
            # In deadband. Drift back toward target so we don't leave
            # the setpoint stuck at +1°C forever.
            if prev > target + 0.05:
                new_setpoint = self._clamp_setpoint(
                    max(target, prev - step), target
                )
            else:
                new_setpoint = target

        # Only report a change if the setpoint actually moved.
        if abs(new_setpoint - prev) < 0.05:
            return None

        self._commit_setpoint(new_setpoint, now=now)
        return new_setpoint

    def _commit_setpoint(self, setpoint: float, now: float | None = None) -> None:
        rounded = round(setpoint, 1)
        self._last_setpoint = rounded
        self._last_setpoint_time = now if now is not None else time.time()

    # Used by the climate entity when the user turns the whole room off,
    # so the next "on" transition starts nudging from target again.
    def reset_setpoint_tracking(self) -> None:
        self._last_setpoint = None
        self._last_setpoint_time = 0.0

    @property
    def room_count(self) -> int:
        return len(self._rooms)

    @property
    def room_names(self) -> list[str]:
        return [r.room_name for r in self._rooms.values()]

    def to_dict(self) -> dict:
        """Serialize zone state for the dashboard."""
        leader = self.leading_room
        return {
            "zone_id": self.zone_id,
            "climate_entity_id": self.climate_entity_id,
            "room_count": self.room_count,
            "room_names": self.room_names,
            "any_wants_heat": self.any_room_wants_heat,
            "is_heating": self._is_heating,
            "max_error": round(self.max_error, 2),
            "leading_room": leader.room_name if leader else None,
            "last_setpoint": self._last_setpoint,
        }


class ZoneManager:
    """Manages all heating zones in the integration."""

    def __init__(self) -> None:
        self._zones: dict[str, HeatingZone] = {}

    def get_or_create_zone(
        self,
        climate_entity_id: str,
        max_setpoint_delta: float = DEFAULT_MAX_SETPOINT_DELTA,
        nudge_step: float = DEFAULT_NUDGE_STEP,
        nudge_interval_min: float = DEFAULT_NUDGE_INTERVAL_MIN,
    ) -> HeatingZone:
        """Get existing zone for a climate entity, or create a new one."""
        zone_id = climate_entity_id

        if zone_id not in self._zones:
            self._zones[zone_id] = HeatingZone(
                zone_id=zone_id,
                climate_entity_id=climate_entity_id,
                max_setpoint_delta=max_setpoint_delta,
                nudge_step=nudge_step,
                nudge_interval_min=nudge_interval_min,
            )
            _LOGGER.info(
                "Created heating zone for thermostat: %s",
                climate_entity_id,
            )
        else:
            # Existing zone — update its tuning with the latest options.
            self._zones[zone_id].update_tuning(
                max_setpoint_delta=max_setpoint_delta,
                nudge_step=nudge_step,
                nudge_interval_min=nudge_interval_min,
            )

        return self._zones[zone_id]

    def get_zone_for_room(self, climate_entity_id: str) -> HeatingZone | None:
        return self._zones.get(climate_entity_id)

    @property
    def zones(self) -> dict[str, HeatingZone]:
        return self._zones

    @property
    def zone_count(self) -> int:
        return len(self._zones)
