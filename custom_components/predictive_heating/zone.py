"""
Heating zone manager.

Handles the case where multiple rooms share a single thermostat/boiler
circuit (no TRVs, just manual taps on radiators). This is common in
Dutch homes where e.g. woonkamer and slaapkamer are heated by the
same Honeywell T6 thermostat.

Key responsibilities:
- Group rooms by their shared climate entity (zone)
- When ANY room in a zone requests heat, ALL rooms reflect "heating"
- Set the thermostat setpoint to the target temperature of the leading
  room and leave OpenTherm to handle the modulation
- Prevent conflicting setpoint commands to the same thermostat

Setpoint strategy — direct target (simple & OpenTherm-friendly):

    The thermostat's own built-in temperature sensor is accurate and
    its OpenTherm modulation loop is well-tuned. Sending the exact
    room target and letting the thermostat handle the rest is the
    most efficient approach.

    Rules:
        - When any room wants heat: setpoint = leading room's target.
        - Only re-send the setpoint when the target changes or after a
          minimum quiet interval (to avoid spamming OpenTherm during
          its slow update cycle).
        - When no room wants heat: idle the thermostat at a low value.

    Why this is better than nudging:
        Nudging requires a fast feedback loop to work well. The
        OpenTherm update cycle is slow (~minutes), so a nudge sent now
        won't be visible in the room temperature for many minutes, by
        which time the next nudge has already fired. This causes
        instability. Setting the target directly and trusting the
        thermostat avoids all of that.
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
        # Rolling log of the last few setpoint decisions so the dashboard
        # can show *why* the controller is doing what it's doing.
        self.nudge_history: list[dict] = []

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

    def calculate_setpoint(self, now: float | None = None) -> float | None:
        """
        Return the setpoint to push to the shared thermostat, or ``None``
        if nothing should be sent right now.

        Rules:
            - If no room wants heat → return None (caller idles the stat).
            - First call since boot → always send the target.
            - Subsequent calls → only send if the target changed, or if
              the minimum quiet interval has elapsed and the setpoint
              differs from the current target (e.g. target was updated
              by a schedule change).

        The thermostat's own OpenTherm loop handles modulation; we just
        tell it what temperature we want and leave it alone.
        """
        if not self.any_room_wants_heat:
            return None

        leader = self.leading_room
        if leader is None or leader.current_temp is None:
            return None

        target = round(leader.target_temp, 1)
        current = leader.current_temp

        # First send since boot (or after reset): always deliver the target.
        if self._last_setpoint is None:
            self._commit_setpoint(
                target,
                now=now,
                reason="initial",
                leader=leader.room_name,
                target=target,
                current=current,
            )
            return target

        # Target hasn't changed → nothing to do.
        if abs(target - self._last_setpoint) < 0.05:
            return None

        # Target changed, but respect a minimum quiet interval so we don't
        # spam OpenTherm during its slow update cycle.
        if self._time_since_last_change(now) < self.nudge_interval_seconds:
            return None

        self._commit_setpoint(
            target,
            now=now,
            reason="target_changed",
            leader=leader.room_name,
            target=target,
            current=current,
        )
        return target

    def _commit_setpoint(
        self,
        setpoint: float,
        now: float | None = None,
        *,
        reason: str = "",
        leader: str | None = None,
        target: float | None = None,
        current: float | None = None,
    ) -> None:
        rounded = round(setpoint, 1)
        self._last_setpoint = rounded
        self._last_setpoint_time = now if now is not None else time.time()
        self.nudge_history.append(
            {
                "timestamp": self._last_setpoint_time,
                "setpoint": rounded,
                "reason": reason,
                "leader": leader,
                "target": target,
                "current": current,
            }
        )
        if len(self.nudge_history) > 100:
            self.nudge_history = self.nudge_history[-100:]

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
