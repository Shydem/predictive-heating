"""
Heating zone manager.

Handles the case where multiple rooms share a single thermostat/boiler
circuit (no TRVs, just manual taps on radiators). This is common in
Dutch homes where e.g. woonkamer and slaapkamer are heated by the
same Honeywell T6 thermostat.

Key responsibilities:
- Group rooms by their shared climate entity (zone)
- When ANY room in a zone requests heat, ALL rooms reflect "heating"
- Calculate the zone setpoint from the room that needs the most heat
- Coordinate OpenTherm flow temperature across the zone
- Prevent conflicting setpoint commands to the same thermostat
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_MAX_FLOW_TEMP,
    DEFAULT_MAX_SETPOINT_DELTA,
    DEFAULT_MIN_FLOW_TEMP,
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
        """Temperature error: how far below target."""
        if self.current_temp is None:
            return 0.0
        return max(0.0, self.target_temp - self.current_temp)


class HeatingZone:
    """
    A group of rooms sharing one physical thermostat.

    The zone collects heat demands from all rooms and decides:
    - Whether the thermostat should be calling for heat
    - What setpoint to send to the thermostat
    - What flow temperature to request (OpenTherm)

    Setpoint strategy (proportional, prevents overshoot):
        Instead of jumping to target + 5°C, we calculate:
            setpoint = target + min(error * gain, max_delta)

        Where:
            error = max(room.target - room.current) across all rooms
            gain = 1.5 (proportional gain, gentle)
            max_delta = 2.5°C (configurable, prevents 26°C anxiety)

        This means:
            - 0.5°C below target → setpoint = target + 0.75°C
            - 1.0°C below target → setpoint = target + 1.5°C
            - 2.0°C below target → setpoint = target + 2.5°C (capped)

    Flow temperature strategy (OpenTherm):
        flow_temp = min_flow + (max_flow - min_flow) * demand_fraction
        Where demand_fraction is based on how cold the worst room is.
    """

    def __init__(
        self,
        zone_id: str,
        climate_entity_id: str,
        opentherm_enabled: bool = False,
        opentherm_flow_temp_entity: str | None = None,
        max_setpoint_delta: float = DEFAULT_MAX_SETPOINT_DELTA,
        min_flow_temp: float = DEFAULT_MIN_FLOW_TEMP,
        max_flow_temp: float = DEFAULT_MAX_FLOW_TEMP,
    ) -> None:
        self.zone_id = zone_id
        self.climate_entity_id = climate_entity_id
        self.opentherm_enabled = opentherm_enabled
        self.opentherm_flow_temp_entity = opentherm_flow_temp_entity
        self.max_setpoint_delta = max_setpoint_delta
        self.min_flow_temp = min_flow_temp
        self.max_flow_temp = max_flow_temp

        self._rooms: dict[str, RoomHeatDemand] = {}
        self._is_heating = False
        self._last_setpoint: float | None = None
        self._last_flow_temp: float | None = None

        # Proportional gain for setpoint calculation
        self._setpoint_gain = 1.5

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

    @property
    def any_room_wants_heat(self) -> bool:
        """Whether any room in this zone wants heating."""
        return any(
            r.wants_heat and not r.window_open
            for r in self._rooms.values()
        )

    @property
    def is_heating(self) -> bool:
        """Whether this zone's thermostat is actively heating."""
        return self._is_heating

    @is_heating.setter
    def is_heating(self, value: bool) -> None:
        self._is_heating = value

    @property
    def max_error(self) -> float:
        """Largest temperature error across all rooms wanting heat."""
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

    def calculate_setpoint(self) -> float | None:
        """
        Calculate the thermostat setpoint for the zone.

        Uses proportional control to prevent overshoot.
        Returns None if no room wants heat.
        """
        if not self.any_room_wants_heat:
            return None

        leader = self.leading_room
        if leader is None or leader.current_temp is None:
            return None

        error = leader.error
        # Proportional delta: gentle increase based on how cold we are
        delta = min(error * self._setpoint_gain, self.max_setpoint_delta)
        # Setpoint is the leader's target + a proportional boost
        setpoint = leader.target_temp + delta

        return round(setpoint, 1)

    def calculate_flow_temperature(self, t_outdoor: float | None = None) -> float | None:
        """
        Calculate the optimal OpenTherm flow temperature.

        Strategy:
        - Small error (< 0.5°C): low flow temp (gentle maintenance)
        - Medium error (0.5-2°C): medium flow temp
        - Large error (> 2°C): high flow temp (fast recovery)

        If outdoor temp is available, we also consider it:
        - Colder outside → higher flow temp needed
        """
        if not self.opentherm_enabled:
            return None

        if not self.any_room_wants_heat:
            return self.min_flow_temp

        error = self.max_error

        # Demand fraction: 0.0 (just maintaining) to 1.0 (max heating)
        # Using a smooth ramp: 0 at error=0, 1 at error≥3°C
        demand = min(1.0, error / 3.0)

        # Outdoor compensation: colder outside needs higher flow
        if t_outdoor is not None:
            # At 10°C outside: no extra boost
            # At -10°C outside: +10°C flow boost
            outdoor_boost = max(0.0, (10.0 - t_outdoor) * 0.5)
        else:
            outdoor_boost = 0.0

        flow_temp = (
            self.min_flow_temp
            + (self.max_flow_temp - self.min_flow_temp) * demand
            + outdoor_boost
        )

        # Clamp to range
        flow_temp = max(self.min_flow_temp, min(self.max_flow_temp, flow_temp))

        return round(flow_temp, 1)

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
            "opentherm_enabled": self.opentherm_enabled,
            "room_count": self.room_count,
            "room_names": self.room_names,
            "any_wants_heat": self.any_room_wants_heat,
            "is_heating": self._is_heating,
            "max_error": round(self.max_error, 2),
            "leading_room": leader.room_name if leader else None,
            "last_setpoint": self._last_setpoint,
            "last_flow_temp": self._last_flow_temp,
        }


class ZoneManager:
    """
    Manages all heating zones in the integration.

    Zones are auto-created: rooms pointing to the same climate entity
    are automatically grouped into the same zone.
    """

    def __init__(self) -> None:
        self._zones: dict[str, HeatingZone] = {}

    def get_or_create_zone(
        self,
        climate_entity_id: str,
        opentherm_enabled: bool = False,
        opentherm_flow_temp_entity: str | None = None,
        max_setpoint_delta: float = DEFAULT_MAX_SETPOINT_DELTA,
    ) -> HeatingZone:
        """Get existing zone for a climate entity, or create a new one."""
        # Use the climate entity ID as the zone ID
        zone_id = climate_entity_id

        if zone_id not in self._zones:
            self._zones[zone_id] = HeatingZone(
                zone_id=zone_id,
                climate_entity_id=climate_entity_id,
                opentherm_enabled=opentherm_enabled,
                opentherm_flow_temp_entity=opentherm_flow_temp_entity,
                max_setpoint_delta=max_setpoint_delta,
            )
            _LOGGER.info(
                "Created heating zone for thermostat: %s (OpenTherm: %s)",
                climate_entity_id,
                opentherm_enabled,
            )

        return self._zones[zone_id]

    def get_zone_for_room(self, climate_entity_id: str) -> HeatingZone | None:
        """Get the zone that contains a given climate entity."""
        return self._zones.get(climate_entity_id)

    @property
    def zones(self) -> dict[str, HeatingZone]:
        return self._zones

    @property
    def zone_count(self) -> int:
        return len(self._zones)
