"""
Preset temperature number entities.

Each room exposes five NumberEntity helpers — ``comfort_temp``,
``eco_temp``, ``away_temp``, ``sleep_temp``, and ``vacation_temp`` — so
the user can tweak the preset setpoints directly from the HA frontend
(Lovelace, voice, automations) without opening the options flow.

The numbers are the *source of truth* for preset temperatures. The
climate entity's ``preset_mode`` simply selects which of these numbers
feeds into the current target. Schedules become pure *mode selectors*:
they pick a preset and the preset number supplies the actual °C.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityFeature,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_ROOM_NAME,
    DEFAULT_AWAY_TEMP,
    DEFAULT_BOOST_TEMP,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
    DEFAULT_SLEEP_TEMP,
    DEFAULT_VACATION_TEMP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


PRESET_NUMBERS = [
    # (key in options, slug, friendly label, default °C, min, max, step)
    ("comfort_temp", "comfort", "Comfort temperature", DEFAULT_COMFORT_TEMP, 16.0, 26.0, 0.5),
    ("eco_temp", "eco", "Eco temperature", DEFAULT_ECO_TEMP, 12.0, 22.0, 0.5),
    ("away_temp", "away", "Away temperature", DEFAULT_AWAY_TEMP, 8.0, 20.0, 0.5),
    ("sleep_temp", "sleep", "Sleep temperature", DEFAULT_SLEEP_TEMP, 14.0, 22.0, 0.5),
    ("boost_temp", "boost", "Boost temperature", DEFAULT_BOOST_TEMP, 18.0, 28.0, 0.5),
    ("vacation_temp", "vacation", "Vacation temperature", DEFAULT_VACATION_TEMP, 5.0, 18.0, 0.5),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one number entity per preset for this room."""
    data = hass.data[DOMAIN][entry.entry_id]
    room_name = data["config"].get(CONF_ROOM_NAME, entry.title)
    preset_temps: dict = data.setdefault("preset_temps", {})

    entities = [
        PresetTemperatureNumber(
            entry=entry,
            room_name=room_name,
            slug=slug,
            option_key=opt,
            label=label,
            default=default,
            min_val=vmin,
            max_val=vmax,
            step=step,
            preset_temps=preset_temps,
        )
        for opt, slug, label, default, vmin, vmax, step in PRESET_NUMBERS
    ]

    async_add_entities(entities)


class PresetTemperatureNumber(NumberEntity, RestoreEntity):
    """User-adjustable target temperature for one preset."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_supported_features = NumberEntityFeature(0)

    def __init__(
        self,
        *,
        entry: ConfigEntry,
        room_name: str,
        slug: str,
        option_key: str,
        label: str,
        default: float,
        min_val: float,
        max_val: float,
        step: float,
        preset_temps: dict,
    ) -> None:
        self._entry = entry
        self._room_name = room_name
        self._slug = slug
        self._option_key = option_key
        self._preset_temps = preset_temps

        # Seed with the value from entry.options if present, else default.
        # Be defensive: old entries may have stored ``None`` under the key,
        # and ``float(None)`` would otherwise crash platform setup.
        seeded = entry.options.get(option_key, entry.data.get(option_key))
        try:
            seeded_val = float(seeded) if seeded is not None else float(default)
        except (TypeError, ValueError):
            seeded_val = float(default)
        self._attr_native_value = seeded_val
        # Share the current value with the climate entity straight away.
        self._preset_temps[slug] = seeded_val

        self._attr_unique_id = f"{entry.entry_id}_preset_{slug}"
        self._attr_name = f"{room_name} {label}"
        self._attr_native_min_value = min_val
        self._attr_native_max_value = max_val
        self._attr_native_step = step
        self._attr_icon = "mdi:thermometer"

    async def async_added_to_hass(self) -> None:
        """Restore previous value across restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (None, "unknown", "unavailable"):
            return
        try:
            val = float(last.state)
        except (TypeError, ValueError):
            return
        self._attr_native_value = val
        self._preset_temps[self._slug] = val

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = float(value)
        self._preset_temps[self._slug] = float(value)
        # Notify the climate entity if it's registered a callback.
        data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        cb = data.get("_on_preset_update")
        if cb is not None:
            cb(self._slug, float(value))
        self.async_write_ha_state()
