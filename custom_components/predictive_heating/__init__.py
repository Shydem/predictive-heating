"""
Predictive Heating — Smart climate control for Home Assistant.

This integration creates a self-learning thermal model for each room,
then uses it to optimally schedule heating based on comfort targets,
energy prices, and heat pump COP.

Rooms sharing the same thermostat (climate entity) are automatically
grouped into heating zones. The zone coordinator ensures:
- Gentle setpoint nudging (no overshoot, preserves OpenTherm modulation)
- Correct heating state across all rooms in the zone

Persistence: the thermal model is stored in Home Assistant's built-in
``.storage`` directory via ``homeassistant.helpers.storage.Store``. Data
survives HA restarts AND integration updates (HACS replaces the code in
``custom_components/``, but ``.storage`` is untouched). A periodic save
(every ``SAVE_INTERVAL``) guards against data loss on ungraceful
shutdowns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    CONF_BUILDING_TYPE,
    CONF_CEILING_HEIGHT_M,
    CONF_CLIMATE_ENTITY,
    CONF_FLOOR_AREA_M2,
    CONF_MAX_SETPOINT_DELTA,
    CONF_NUDGE_INTERVAL_MIN,
    CONF_NUDGE_STEP,
    CONF_ROOM_NAME,
    DEFAULT_MAX_SETPOINT_DELTA,
    DEFAULT_NUDGE_INTERVAL_MIN,
    DEFAULT_NUDGE_STEP,
    DOMAIN,
    PLATFORMS,
)
from .frontend_panel import async_register_frontend
from .thermal_model import ThermalModel
from .zone import ZoneManager

# Pre-import platform modules so HA doesn't trip the "blocking call to
# import_module inside the event loop" detector the first time a new
# platform (number / switch / button) is forwarded. Since this __init__
# itself is imported synchronously during integration load, these
# imports all happen before any async entry-setup runs.
from . import button as _pre_button  # noqa: F401
from . import climate as _pre_climate  # noqa: F401
from . import number as _pre_number  # noqa: F401
from . import sensor as _pre_sensor  # noqa: F401
from . import switch as _pre_switch  # noqa: F401

_LOGGER = logging.getLogger(__name__)

PLATFORMS_LIST = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
]

_PANEL_REGISTERED = False

# Store version — bump if we make an incompatible change to the
# serialised ``ThermalModel.to_dict()`` payload that needs migration.
STORE_VERSION = 1
STORE_KEY_PREFIX = f"{DOMAIN}.model"

# How often to persist the thermal model to disk. The EKF state has
# dozens of floats, so writing every update is wasteful and wears the
# storage. Saving every 15 minutes means at most 15 min of learning
# progress is lost on an ungraceful shutdown.
SAVE_INTERVAL = timedelta(minutes=15)

# Legacy storage path (pre-Store migration). Kept so existing users
# don't lose their learned thermal parameters on upgrade.
_LEGACY_FILENAME = "predictive_heating_{entry_id}.json"


def _store_for_entry(hass: HomeAssistant, entry_id: str) -> Store:
    """Return a Store instance for a given config entry."""
    return Store(
        hass,
        STORE_VERSION,
        f"{STORE_KEY_PREFIX}_{entry_id}",
        private=True,
        atomic_writes=True,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Predictive Heating from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if "_zone_manager" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["_zone_manager"] = ZoneManager()

    # Register the dashboard panel (once, on first entry setup)
    global _PANEL_REGISTERED
    if not _PANEL_REGISTERED:
        await async_register_frontend(hass)
        _PANEL_REGISTERED = True

    # Load or create thermal model for this room
    store = _store_for_entry(hass, entry.entry_id)
    model = await _load_model(hass, entry.entry_id, store)

    # Seed a fresh model with initial H/C from room dimensions if available.
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
        CONF_MAX_SETPOINT_DELTA,
        entry.data.get(CONF_MAX_SETPOINT_DELTA, DEFAULT_MAX_SETPOINT_DELTA),
    )
    nudge_step = entry.options.get(
        CONF_NUDGE_STEP,
        entry.data.get(CONF_NUDGE_STEP, DEFAULT_NUDGE_STEP),
    )
    nudge_interval_min = entry.options.get(
        CONF_NUDGE_INTERVAL_MIN,
        entry.data.get(CONF_NUDGE_INTERVAL_MIN, DEFAULT_NUDGE_INTERVAL_MIN),
    )

    zone = zone_mgr.get_or_create_zone(
        climate_entity_id=climate_entity_id,
        max_setpoint_delta=max_delta,
        nudge_step=nudge_step,
        nudge_interval_min=nudge_interval_min,
    )
    zone.register_room(
        entry_id=entry.entry_id,
        room_name=entry.data.get(CONF_ROOM_NAME, entry.title),
    )

    # Merge options over data so the frontend panel and any code that reads
    # data["config"] sees the full effective configuration, including options
    # that were set after initial setup (window sensors, schedule, gas meter…).
    merged_config = {**dict(entry.data), **dict(entry.options)}

    hass.data[DOMAIN][entry.entry_id] = {
        "model": model,
        "config": merged_config,
        "zone": zone,
        "store": store,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_LIST)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Periodic save so data isn't lost on ungraceful shutdown.
    async def _periodic_save(_now) -> None:
        await _save_model(
            hass,
            entry.entry_id,
            model,
            store,
            room_name=entry.data.get(CONF_ROOM_NAME, entry.title),
        )

    entry.async_on_unload(
        async_track_time_interval(hass, _periodic_save, SAVE_INTERVAL)
    )

    _LOGGER.info(
        "Predictive Heating set up for room: %s (zone: %s, rooms in zone: %d, "
        "model state: %s, total_updates=%d)",
        entry.data.get(CONF_ROOM_NAME, entry.title),
        climate_entity_id,
        zone.room_count,
        model.state,
        model.total_updates,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Persist the thermal model before unloading so we don't lose the
    # last window of learning when HA restarts, HACS pushes an update,
    # or the user reloads the integration.
    if entry.entry_id in hass.data.get(DOMAIN, {}):
        entry_data = hass.data[DOMAIN][entry.entry_id]
        model = entry_data["model"]
        store = entry_data["store"]
        try:
            await _save_model(
                hass,
                entry.entry_id,
                model,
                store,
                room_name=entry.data.get(CONF_ROOM_NAME, entry.title),
            )
        except Exception as err:  # noqa: BLE001 — never block unload on save
            _LOGGER.warning(
                "Failed to persist thermal model for %s on unload: %s",
                entry.entry_id, err,
            )

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_LIST
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted thermal-model data when a room is removed."""
    # Clean up both the Store entry and any legacy JSON file.
    store = _store_for_entry(hass, entry.entry_id)
    try:
        await store.async_remove()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not remove stored model: %s", err)

    legacy_path = _legacy_model_path(hass, entry.entry_id)

    def _delete_legacy() -> None:
        try:
            if legacy_path.exists():
                legacy_path.unlink()
        except OSError as err:
            _LOGGER.warning(
                "Could not delete legacy model file %s: %s", legacy_path, err
            )

    await hass.async_add_executor_job(_delete_legacy)
    _LOGGER.info(
        "Removed predictive-heating room '%s' and its thermal model",
        entry.data.get(CONF_ROOM_NAME, entry.title),
    )


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


# ─── Persistence ────────────────────────────────────────────────


def _legacy_model_path(hass: HomeAssistant, entry_id: str) -> Path:
    """Return the legacy JSON path used before the Store migration."""
    return Path(
        hass.config.path(
            ".storage/" + _LEGACY_FILENAME.format(entry_id=entry_id)
        )
    )


def _get_storage_dir(hass: HomeAssistant) -> Path:
    """Directory where HA stores integration data."""
    return Path(hass.config.path(".storage"))


def list_orphan_models(hass: HomeAssistant) -> list[dict]:
    """Return persisted thermal-model files with no live config entry."""
    storage_dir = _get_storage_dir(hass)
    if not storage_dir.exists():
        return []

    active_ids = {
        eid
        for eid in hass.data.get(DOMAIN, {})
        if not eid.startswith("_")
    }

    orphans: list[dict] = []

    # Match both the Store format and the legacy filename format.
    for path in storage_dir.glob(f"{STORE_KEY_PREFIX}_*"):
        entry_id = path.name[len(STORE_KEY_PREFIX) + 1:]
        if entry_id in active_ids:
            continue
        orphans.append(_describe_orphan(path, entry_id))

    for path in storage_dir.glob("predictive_heating_*.json"):
        entry_id = path.stem[len("predictive_heating_"):]
        if entry_id in active_ids:
            continue
        orphans.append(_describe_orphan(path, entry_id, legacy=True))

    return orphans


def _describe_orphan(
    path: Path, entry_id: str, *, legacy: bool = False
) -> dict:
    room_name = entry_id
    size = 0
    mtime = 0.0
    try:
        stat = path.stat()
        size = stat.st_size
        mtime = stat.st_mtime
    except OSError:
        pass

    try:
        raw = json.loads(path.read_text())
        data = raw if legacy else raw.get("data", {})
        room_name = data.get("room_name") or room_name
    except Exception:  # noqa: BLE001 — best-effort
        pass

    return {
        "entry_id": entry_id,
        "path": str(path),
        "room_name": room_name,
        "size_bytes": size,
        "modified": mtime,
        "legacy": legacy,
    }


def delete_orphan_model(hass: HomeAssistant, entry_id: str) -> bool:
    """Delete an orphan model (either Store entry or legacy JSON)."""
    storage_dir = _get_storage_dir(hass)

    # Refuse if this entry is still active.
    if entry_id in hass.data.get(DOMAIN, {}):
        return False

    candidates = [
        storage_dir / f"{STORE_KEY_PREFIX}_{entry_id}",
        storage_dir / f"predictive_heating_{entry_id}.json",
    ]

    deleted = False
    for path in candidates:
        try:
            if path.exists():
                path.unlink()
                deleted = True
        except OSError as err:
            _LOGGER.warning("Could not delete orphan %s: %s", path, err)
    return deleted


async def _load_model(
    hass: HomeAssistant,
    entry_id: str,
    store: Store,
) -> ThermalModel:
    """Load a thermal model, migrating from legacy JSON if needed.

    This is the critical path for preserving learned thermal parameters
    across integration updates. Failures log a clear warning but never
    crash setup — a fresh model is the fallback.
    """
    try:
        data = await store.async_load()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Failed to read Store for entry %s: %s (starting fresh)",
            entry_id, err,
        )
        data = None

    if data:
        try:
            return ThermalModel.from_dict(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Thermal-model data for %s was corrupt: %s — starting fresh",
                entry_id, err,
            )
            return ThermalModel()

    # Migrate from the legacy JSON file, if present.
    legacy_path = _legacy_model_path(hass, entry_id)

    def _read_legacy() -> dict | None:
        if not legacy_path.exists():
            return None
        try:
            return json.loads(legacy_path.read_text())
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Legacy thermal-model file %s unreadable: %s",
                legacy_path, err,
            )
            return None

    legacy = await hass.async_add_executor_job(_read_legacy)
    if legacy:
        try:
            model = ThermalModel.from_dict(legacy)
            _LOGGER.info(
                "Migrated thermal model for %s from legacy JSON → Store",
                entry_id,
            )
            # Persist into the Store immediately, then delete the
            # legacy file so we don't migrate twice.
            await store.async_save(model.to_dict())
            try:
                await hass.async_add_executor_job(legacy_path.unlink)
            except OSError as err:
                _LOGGER.debug("Could not remove legacy file: %s", err)
            return model
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not migrate legacy thermal model for %s: %s", entry_id, err
            )

    return ThermalModel()


async def _save_model(
    hass: HomeAssistant,
    entry_id: str,
    model: ThermalModel,
    store: Store,
    room_name: str | None = None,
) -> None:
    """Persist a thermal model via HA's Store helper."""
    payload = model.to_dict()
    if room_name:
        payload["room_name"] = room_name

    try:
        await store.async_save(payload)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Failed to persist thermal model for %s: %s", entry_id, err
        )
