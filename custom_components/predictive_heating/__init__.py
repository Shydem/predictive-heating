"""
Predictive Heating — Smart climate control for Home Assistant.

This integration creates a self-learning thermal model for each room,
then uses it to optimally schedule heating based on comfort targets,
energy prices, and heat pump COP.

Rooms sharing the same thermostat (climate entity) are automatically
grouped into heating zones. The zone coordinator ensures:
- Proportional setpoint control (no overshoot)
- Correct heating state across all rooms in the zone
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_MAX_SETPOINT_DELTA,
    CONF_ROOM_NAME,
    DEFAULT_MAX_SETPOINT_DELTA,
    DOMAIN,
    PLATFORMS,
)
from .frontend_panel import async_register_frontend
from .thermal_model import ThermalModel
from .zone import ZoneManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS_LIST = [Platform.CLIMATE, Platform.SENSOR]

_PANEL_REGISTERED = False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Predictive Heating from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Ensure the zone manager exists (shared across all entries)
    if "_zone_manager" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["_zone_manager"] = ZoneManager()

    # Register the dashboard panel (once, on first entry setup)
    global _PANEL_REGISTERED
    if not _PANEL_REGISTERED:
        await async_register_frontend(hass)
        _PANEL_REGISTERED = True

    # Load or create thermal model for this room
    model = await _load_model(hass, entry.entry_id)

    # Register this room in its heating zone
    zone_mgr: ZoneManager = hass.data[DOMAIN]["_zone_manager"]
    climate_entity_id = entry.data.get(CONF_CLIMATE_ENTITY, "")
    max_delta = entry.options.get(
        "max_setpoint_delta",
        entry.data.get(CONF_MAX_SETPOINT_DELTA, DEFAULT_MAX_SETPOINT_DELTA),
    )

    zone = zone_mgr.get_or_create_zone(
        climate_entity_id=climate_entity_id,
        max_setpoint_delta=max_delta,
    )
    zone.register_room(
        entry_id=entry.entry_id,
        room_name=entry.data.get(CONF_ROOM_NAME, entry.title),
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "model": model,
        "config": dict(entry.data),
        "zone": zone,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_LIST)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.info(
        "Predictive Heating set up for room: %s (zone: %s, rooms in zone: %d, "
        "model state: %s)",
        entry.data.get(CONF_ROOM_NAME, entry.title),
        climate_entity_id,
        zone.room_count,
        model.state,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Persist the thermal model before unloading
    if entry.entry_id in hass.data.get(DOMAIN, {}):
        model = hass.data[DOMAIN][entry.entry_id]["model"]
        await _save_model(hass, entry.entry_id, model)

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_LIST
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


def _get_model_path(hass: HomeAssistant, entry_id: str) -> Path:
    """Get the path for persisting a thermal model."""
    return Path(hass.config.path(f".storage/predictive_heating_{entry_id}.json"))


async def _load_model(hass: HomeAssistant, entry_id: str) -> ThermalModel:
    """Load a thermal model from disk, or create a new one."""
    path = _get_model_path(hass, entry_id)

    def _read() -> ThermalModel:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return ThermalModel.from_dict(data)
            except Exception:
                _LOGGER.warning("Failed to load thermal model, starting fresh")
        return ThermalModel()

    return await hass.async_add_executor_job(_read)


async def _save_model(
    hass: HomeAssistant, entry_id: str, model: ThermalModel
) -> None:
    """Persist a thermal model to disk."""
    path = _get_model_path(hass, entry_id)

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(model.to_dict(), indent=2))

    await hass.async_add_executor_job(_write)
