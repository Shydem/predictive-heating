"""The Predictive Heating integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    DOMAIN, PLATFORMS, SERVICE_GET_FORECAST, SERVICE_SET_MODEL_PARAMS,
    SERVICE_SET_SCHEDULE, SERVICE_TRAIN_MODEL,
)
from .coordinator import PredictiveHeatingCoordinator

_LOGGER = logging.getLogger(__name__)

type PredictiveHeatingConfigEntry = ConfigEntry[PredictiveHeatingCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PredictiveHeatingConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = PredictiveHeatingCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PredictiveHeatingConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _register_services(hass: HomeAssistant) -> None:
    """Register services (idempotent — safe to call multiple times)."""

    async def handle_train(call: ServiceCall) -> None:
        exclude = call.data.get("exclude_periods", [])
        # Parse list of {"start": ..., "end": ...} dicts into tuples
        exclude_tuples = []
        for period in exclude:
            if isinstance(period, dict) and "start" in period and "end" in period:
                exclude_tuples.append((period["start"], period["end"]))
        for entry in hass.config_entries.async_entries(DOMAIN):
            await entry.runtime_data.async_train_model(
                exclude_periods=exclude_tuples or None
            )
            await entry.runtime_data.async_request_refresh()

    async def handle_schedule(call: ServiceCall) -> None:
        schedule = call.data.get("schedule", {})
        for entry in hass.config_entries.async_entries(DOMAIN):
            entry.runtime_data.schedule = schedule
            await entry.runtime_data.async_request_refresh()

    async def handle_forecast(call: ServiceCall) -> dict[str, Any]:
        for entry in hass.config_entries.async_entries(DOMAIN):
            opt = entry.runtime_data.last_optimization
            if opt:
                return {
                    "temperatures": [round(t, 2) for t in opt.predicted_temperatures],
                    "total_cost": round(opt.total_cost, 4),
                    "slots": len(opt.slot_results),
                }
        return {}

    async def handle_set_params(call: ServiceCall) -> None:
        ua = call.data.get("ua")
        thermal_mass = call.data.get("thermal_mass")
        for entry in hass.config_entries.async_entries(DOMAIN):
            entry.runtime_data.set_model_params(ua=ua, thermal_mass=thermal_mass)
            await entry.runtime_data.async_request_refresh()

    if not hass.services.has_service(DOMAIN, SERVICE_TRAIN_MODEL):
        hass.services.async_register(
            DOMAIN, SERVICE_TRAIN_MODEL, handle_train,
            schema=vol.Schema({
                vol.Optional("exclude_periods"): list,
            }),
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SET_SCHEDULE, handle_schedule,
            schema=vol.Schema({vol.Required("schedule"): dict}),
        )
        hass.services.async_register(DOMAIN, SERVICE_GET_FORECAST, handle_forecast)
        hass.services.async_register(
            DOMAIN, SERVICE_SET_MODEL_PARAMS, handle_set_params,
            schema=vol.Schema({
                vol.Optional("ua"): vol.Coerce(float),
                vol.Optional("thermal_mass"): vol.Coerce(float),
            }),
        )
