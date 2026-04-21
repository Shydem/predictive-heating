"""
Predictive Heating switches.

Two switches per room:

* ``Override`` — while ``on``, the room is forced to the comfort preset
  regardless of schedule, presence, or away-grace logic. Intended for
  WFH days or when a specific room needs to stay warm.

* ``Coupling enabled`` — one switch per configured thermal coupling
  (door/partition between two rooms). Toggles whether that edge is
  included in the learning and prediction math. Useful to model
  "door open vs. door closed" without removing the coupling spec.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_ROOM_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    room_name = data["config"].get(CONF_ROOM_NAME, entry.title)

    entities: list[SwitchEntity] = [
        OverrideSwitch(entry=entry, room_name=room_name, data=data),
    ]

    # One switch per coupling edge.
    model = data["model"]
    for idx, coupling in enumerate(getattr(model, "couplings", []) or []):
        entities.append(
            CouplingEnableSwitch(
                entry=entry,
                room_name=room_name,
                coupling_index=idx,
                data=data,
            )
        )

    async_add_entities(entities)


class OverrideSwitch(SwitchEntity, RestoreEntity):
    """When on, the room is pinned to comfort preset until turned off."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:account-clock"

    def __init__(self, *, entry: ConfigEntry, room_name: str, data: dict) -> None:
        self._entry = entry
        self._data = data
        self._attr_unique_id = f"{entry.entry_id}_override"
        self._attr_name = f"{room_name} Override"
        self._attr_is_on = False
        data["override_on"] = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self._attr_is_on = True
            self._data["override_on"] = True
            cb = self._data.get("_on_override_change")
            if cb is not None:
                cb(True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._data["override_on"] = True
        cb = self._data.get("_on_override_change")
        if cb is not None:
            cb(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._data["override_on"] = False
        cb = self._data.get("_on_override_change")
        if cb is not None:
            cb(False)
        self.async_write_ha_state()


class CouplingEnableSwitch(SwitchEntity, RestoreEntity):
    """Enable / disable one specific room-to-room coupling edge."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:door-open"

    def __init__(
        self, *, entry: ConfigEntry, room_name: str, coupling_index: int, data: dict
    ) -> None:
        self._entry = entry
        self._data = data
        self._index = coupling_index
        spec = data["model"].couplings[coupling_index]
        self._attr_unique_id = (
            f"{entry.entry_id}_coupling_{spec.neighbour_entry_id}"
        )
        # Use the neighbour's entry_id tail as a short identifier.
        nb_tail = spec.neighbour_entry_id[-6:]
        self._attr_name = f"{room_name} Coupling → {nb_tail}"
        self._attr_is_on = bool(spec.enabled)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            is_on = last.state == "on"
            self._attr_is_on = is_on
            try:
                self._data["model"].couplings[self._index].enabled = is_on
            except IndexError:
                pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._toggle(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._toggle(False)

    def _toggle(self, state: bool) -> None:
        self._attr_is_on = state
        try:
            self._data["model"].couplings[self._index].enabled = state
        except IndexError:
            return
        self.async_write_ha_state()
