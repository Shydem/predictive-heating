"""
Predictive Heating — Smart climate control for Home Assistant.

This integration creates a self-learning thermal model for each room,
then uses it to optimally schedule heating based on comfort targets,
energy prices, and heat pump COP.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .thermal_model import ThermalModel

_LOGGER = logging.getLogger(__name__)

PLATFORMS_LIST = [Platform.CLIMATE, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Predictive Heating from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Load or create thermal model for this room
    model = await _load_model(hass, entry.entry_id)

    hass.data[DOMAIN][entry.entry_id] = {
        "model": model,
        "config": dict(entry.data),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_LIST)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.info(
        "Predictive Heating set up for room: %s (model state: %s)",
        entry.data.get("room_name", entry.title),
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
