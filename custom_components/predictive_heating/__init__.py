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
    CONF_BUILDING_TYPE,
    CONF_CEILING_HEIGHT_M,
    CONF_CLIMATE_ENTITY,
    CONF_FLOOR_AREA_M2,
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

    # Seed a fresh model with initial H/C from room dimensions if available.
    # Only applies when the model has no prior observations.
    floor_area = entry.data.get(CONF_FLOOR_AREA_M2)
    if floor_area:
        model.seed_from_room_dimensions(
            floor_area_m2=floor_area,
            ceiling_height_m=entry.data.get(CONF_CEILING_HEIGHT_M),
            building_type=entry.data.get(CONF_BUILDING_TYPE),
        )

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
        await _save_model(
            hass,
            entry.entry_id,
            model,
            room_name=entry.data.get(CONF_ROOM_NAME, entry.title),
        )

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_LIST
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted thermal-model file when a room is removed.

    HA calls this after the entry has already been unloaded, so the model
    file is the only thing left to clean up.
    """
    path = _get_model_path(hass, entry.entry_id)

    def _delete() -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError as err:
            _LOGGER.warning("Could not delete thermal model %s: %s", path, err)

    await hass.async_add_executor_job(_delete)
    _LOGGER.info(
        "Removed predictive-heating room '%s' and its thermal model",
        entry.data.get(CONF_ROOM_NAME, entry.title),
    )


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


def _get_model_path(hass: HomeAssistant, entry_id: str) -> Path:
    """Get the path for persisting a thermal model."""
    return Path(hass.config.path(f".storage/predictive_heating_{entry_id}.json"))


def _get_storage_dir(hass: HomeAssistant) -> Path:
    """Directory where thermal-model files live."""
    return Path(hass.config.path(".storage"))


def list_orphan_models(hass: HomeAssistant) -> list[dict]:
    """Return persisted thermal-model files that no longer have a config entry.

    Used by the dashboard's manual cleanup tool — handy for tidying up
    after old bugs that left stale files behind.
    """
    storage_dir = _get_storage_dir(hass)
    if not storage_dir.exists():
        return []

    active_ids = {
        eid
        for eid in hass.data.get(DOMAIN, {})
        if not eid.startswith("_")
    }

    orphans: list[dict] = []
    prefix = "predictive_heating_"
    for path in storage_dir.glob(f"{prefix}*.json"):
        entry_id = path.stem[len(prefix):]
        if entry_id in active_ids:
            continue
        try:
            stat = path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            continue

        room_name = entry_id
        try:
            data = json.loads(path.read_text())
            room_name = data.get("room_name") or room_name
        except Exception:  # noqa: BLE001 — best-effort label
            pass

        orphans.append(
            {
                "entry_id": entry_id,
                "path": str(path),
                "room_name": room_name,
                "size_bytes": size,
                "modified": mtime,
            }
        )
    return orphans


def delete_orphan_model(hass: HomeAssistant, entry_id: str) -> bool:
    """Delete a single orphan model file. Returns True on success."""
    storage_dir = _get_storage_dir(hass)
    path = storage_dir / f"predictive_heating_{entry_id}.json"

    # Refuse to delete a file belonging to an active entry
    if entry_id in hass.data.get(DOMAIN, {}):
        return False

    try:
        if path.exists():
            path.unlink()
            return True
    except OSError as err:
        _LOGGER.warning("Could not delete orphan %s: %s", path, err)
    return False


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
    hass: HomeAssistant,
    entry_id: str,
    model: ThermalModel,
    room_name: str | None = None,
) -> None:
    """Persist a thermal model to disk.

    The room_name is also written into the file so the orphan-cleanup
    dashboard can show a friendly label for stale files.
    """
    path = _get_model_path(hass, entry_id)

    def _write() -> None:
        payload = model.to_dict()
        if room_name:
            payload["room_name"] = room_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))

    await hass.async_add_executor_job(_write)
