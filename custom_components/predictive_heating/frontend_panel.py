"""
Frontend panel registration and WebSocket API for the dashboard.

Registers a sidebar panel in Home Assistant and provides WebSocket
endpoints for the dashboard to fetch room data and training progress.
"""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.frontend import (
    async_register_built_in_panel,
)
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, MIN_ACTIVE_SAMPLES, MIN_IDLE_SAMPLES, STATE_CALIBRATED

_LOGGER = logging.getLogger(__name__)

URL_BASE = "/predictive_heating"
PANEL_URL = f"{URL_BASE}/frontend"
PANEL_ICON = "mdi:home-thermometer"
PANEL_TITLE = "Predictive Heating"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the frontend panel and websocket API."""

    # Serve the frontend JS files
    frontend_dir = str(Path(__file__).parent / "frontend")

    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_URL, frontend_dir, cache_headers=False)]
    )

    # Register the sidebar panel
    if DOMAIN not in hass.data.get("frontend_panels", {}):
        async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=DOMAIN,
            config={
                "_panel_custom": {
                    "name": "predictive-heating-panel",
                    "embed_iframe": False,
                    "trust_external": False,
                    "module_url": f"{PANEL_URL}/entrypoint.js",
                }
            },
            require_admin=False,
        )

    # Register websocket commands
    websocket_api.async_register_command(hass, ws_get_rooms)
    websocket_api.async_register_command(hass, ws_get_room_detail)

    _LOGGER.info("Predictive Heating dashboard registered at sidebar")


# ─── WebSocket API ───────────────────────────────────────────


@websocket_api.websocket_command(
    {
        vol.Required("type"): "predictive_heating/rooms",
    }
)
@callback
def ws_get_rooms(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return overview data for all configured rooms."""
    rooms = []

    for entry_id, data in hass.data.get(DOMAIN, {}).items():
        model = data.get("model")
        config = data.get("config", {})
        if model is None:
            continue

        # Get current temperatures from the climate entity's state
        climate_entity_id = f"climate.predictive_{config.get('room_name', 'unknown').lower().replace(' ', '_')}"

        current_temp = None
        target_temp = None
        outdoor_temp = None
        hvac_action = "idle"

        # Try to read from our climate entity
        state = hass.states.get(climate_entity_id)
        if state:
            current_temp = state.attributes.get("current_temperature")
            target_temp = state.attributes.get("temperature")
            outdoor_temp = state.attributes.get("outdoor_temperature")
            hvac_action = state.attributes.get("hvac_action", "idle")

        # If we can't find our entity, try reading sensors directly
        if current_temp is None:
            temp_sensor_id = config.get("temperature_sensor")
            if temp_sensor_id:
                sensor_state = hass.states.get(temp_sensor_id)
                if sensor_state:
                    try:
                        current_temp = float(sensor_state.state)
                    except (ValueError, TypeError):
                        pass

        if outdoor_temp is None:
            outdoor_sensor_id = config.get("outdoor_temperature_sensor")
            if outdoor_sensor_id:
                sensor_state = hass.states.get(outdoor_sensor_id)
                if sensor_state:
                    try:
                        outdoor_temp = float(sensor_state.state)
                    except (ValueError, TypeError):
                        pass

        idle_pct = min(100, model.idle_count / MIN_IDLE_SAMPLES * 100)
        active_pct = min(100, model.active_count / MIN_ACTIVE_SAMPLES * 100)
        progress = int((idle_pct + active_pct) / 2)

        # Zone info
        zone = data.get("zone")
        zone_info = {}
        if zone:
            zone_info = {
                "zone_id": zone.zone_id,
                "zone_rooms": zone.room_names,
                "zone_is_heating": zone.is_heating,
                "zone_setpoint": zone._last_setpoint,
                "zone_flow_temp": zone._last_flow_temp,
                "opentherm_enabled": zone.opentherm_enabled,
            }

        rooms.append(
            {
                "entry_id": entry_id,
                "room_name": config.get("room_name", "Unknown"),
                "model_state": model.state,
                "current_temp": current_temp,
                "target_temp": target_temp,
                "outdoor_temp": outdoor_temp,
                "hvac_action": hvac_action,
                "heat_loss_coeff": model.params.heat_loss_coeff,
                "thermal_mass": model.params.thermal_mass,
                "idle_samples": model.idle_count,
                "active_samples": model.active_count,
                "min_idle": MIN_IDLE_SAMPLES,
                "min_active": MIN_ACTIVE_SAMPLES,
                "learning_progress": progress,
                **zone_info,
            }
        )

    connection.send_result(msg["id"], {"rooms": rooms})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "predictive_heating/room_detail",
        vol.Required("entry_id"): str,
    }
)
@callback
def ws_get_room_detail(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return detailed data for a single room including training history."""
    entry_id = msg["entry_id"]
    domain_data = hass.data.get(DOMAIN, {})

    if entry_id not in domain_data:
        connection.send_error(msg["id"], "not_found", "Room not found")
        return

    data = domain_data[entry_id]
    model = data["model"]
    config = data.get("config", {})

    # Current temperatures (same logic as overview)
    current_temp = None
    target_temp = None
    outdoor_temp = None
    hvac_action = "idle"

    climate_entity_id = f"climate.predictive_{config.get('room_name', 'unknown').lower().replace(' ', '_')}"
    state = hass.states.get(climate_entity_id)
    if state:
        current_temp = state.attributes.get("current_temperature")
        target_temp = state.attributes.get("temperature")
        outdoor_temp = state.attributes.get("outdoor_temperature")
        hvac_action = state.attributes.get("hvac_action", "idle")

    if current_temp is None:
        temp_sensor_id = config.get("temperature_sensor")
        if temp_sensor_id:
            s = hass.states.get(temp_sensor_id)
            if s:
                try:
                    current_temp = float(s.state)
                except (ValueError, TypeError):
                    pass

    if outdoor_temp is None:
        outdoor_sensor_id = config.get("outdoor_temperature_sensor")
        if outdoor_sensor_id:
            s = hass.states.get(outdoor_sensor_id)
            if s:
                try:
                    outdoor_temp = float(s.state)
                except (ValueError, TypeError):
                    pass

    # Observation history for the temperature chart
    observations = []
    for obs in model.observations[-200:]:  # last 200 observations
        observations.append(
            {
                "timestamp": obs.timestamp,
                "t_indoor": obs.t_indoor,
                "t_outdoor": obs.t_outdoor,
                "heating_on": obs.heating_on,
                "solar_irradiance": obs.solar_irradiance,
            }
        )

    # H evolution history for the learning chart
    h_history = []
    for entry in model.h_history:
        h_history.append(
            {
                "sample": entry["sample"],
                "value": entry["value"],
            }
        )

    # Learning progress
    idle_pct = min(100, model.idle_count / MIN_IDLE_SAMPLES * 100)
    active_pct = min(100, model.active_count / MIN_ACTIVE_SAMPLES * 100)
    progress = int((idle_pct + active_pct) / 2)

    # Predictions (only if calibrated)
    predictions = None
    if model.state == STATE_CALIBRATED and current_temp is not None:
        t_out = outdoor_temp if outdoor_temp is not None else 10.0
        temp_1h_off = model.predict_temperature(current_temp, t_out, 0.0, 1.0)
        temp_1h_on = model.predict_temperature(current_temp, t_out, 1.0, 1.0)
        time_to_target = None
        if target_temp is not None and current_temp < target_temp:
            ttt = model.time_to_reach(current_temp, target_temp, t_out)
            if ttt is not None:
                time_to_target = ttt * 60  # convert to minutes

        predictions = {
            "temp_1h_off": round(temp_1h_off, 1),
            "temp_1h_on": round(temp_1h_on, 1),
            "time_to_target": round(time_to_target, 1) if time_to_target else None,
        }

    result = {
        "entry_id": entry_id,
        "room_name": config.get("room_name", "Unknown"),
        "model_state": model.state,
        "current_temp": current_temp,
        "target_temp": target_temp,
        "outdoor_temp": outdoor_temp,
        "hvac_action": hvac_action,
        "params": {
            "heat_loss_coeff": model.params.heat_loss_coeff,
            "thermal_mass": model.params.thermal_mass,
            "heating_power": model.params.heating_power,
            "solar_gain_factor": model.params.solar_gain_factor,
        },
        "idle_samples": model.idle_count,
        "active_samples": model.active_count,
        "total_updates": model.total_updates,
        "min_idle": MIN_IDLE_SAMPLES,
        "min_active": MIN_ACTIVE_SAMPLES,
        "learning_progress": progress,
        "mean_prediction_error": (
            round(model.mean_prediction_error, 3)
            if model.mean_prediction_error != float("inf")
            else None
        ),
        "prediction_error_history": model.prediction_error_history[-200:],
        "observations": observations,
        "h_history": h_history,
        "predictions": predictions,
        "uses_ekf": hasattr(model, "_ekf") and model._ekf is not None,
    }

    connection.send_result(msg["id"], result)
