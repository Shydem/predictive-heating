"""
Frontend panel registration and WebSocket API for the dashboard.

Registers a sidebar panel in Home Assistant and provides WebSocket
endpoints for the dashboard to fetch room data and training progress.

Reliability note: every WebSocket handler catches unexpected
exceptions and sends a structured error. Without this, a single bad
field (e.g. a None value being .toFixed()'d on the frontend) would
make the entire detail view silently fail to open — which is exactly
the "can't open rooms" regression we're guarding against.
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

from .const import (
    CONF_ROOM_NAME,
    CONF_SCHEDULE_ENTITY,
    CONF_SCHEDULE_OFF_TEMP,
    CONF_SCHEDULE_ON_TEMP,
    CONF_WINDOW_SENSORS,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
    DOMAIN,
    MIN_ACTIVE_SAMPLES,
    MIN_IDLE_SAMPLES,
    STATE_CALIBRATED,
)
from .solar import get_solar_calculation

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
    websocket_api.async_register_command(hass, ws_list_orphans)
    websocket_api.async_register_command(hass, ws_delete_orphan)
    websocket_api.async_register_command(hass, ws_set_temperature)
    websocket_api.async_register_command(hass, ws_set_preset)

    _LOGGER.info("Predictive Heating dashboard registered at sidebar")


def _safe_float(value) -> float | None:
    """Best-effort float coercion. Returns None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _room_window_state(hass: HomeAssistant, config: dict) -> dict:
    """Return aggregated window-sensor state for a room config."""
    ids = config.get(CONF_WINDOW_SENSORS) or []
    if isinstance(ids, str):
        ids = [ids]

    any_open = False
    open_ones: list[str] = []
    details: list[dict] = []
    for sensor_id in ids:
        state = hass.states.get(sensor_id)
        st = state.state if state else None
        is_open = st == "on"
        if is_open:
            any_open = True
            open_ones.append(sensor_id)
        details.append(
            {
                "entity_id": sensor_id,
                "state": st,
                "open": is_open,
                "friendly_name": (
                    state.attributes.get("friendly_name") if state else None
                ),
            }
        )
    return {
        "configured": bool(ids),
        "any_open": any_open,
        "open_count": len(open_ones),
        "sensors": details,
    }


def _schedule_state(hass: HomeAssistant, config: dict) -> dict | None:
    """Return the current schedule state if one is configured.

    The returned dict carries everything the dashboard needs to render a
    schedule card: the entity being followed, its current on/off state, a
    slot-override temperature (if the schedule has a per-slot ``temperature``
    attribute), and the on/off default temperatures from the user's options
    so the user can see what "ON" and "OFF" resolve to.
    """
    schedule_id = config.get(CONF_SCHEDULE_ENTITY)
    if not schedule_id:
        return None

    on_temp = _safe_float(config.get(CONF_SCHEDULE_ON_TEMP))
    if on_temp is None:
        on_temp = _safe_float(config.get("comfort_temp")) or DEFAULT_COMFORT_TEMP
    off_temp = _safe_float(config.get(CONF_SCHEDULE_OFF_TEMP))
    if off_temp is None:
        off_temp = _safe_float(config.get("eco_temp")) or DEFAULT_ECO_TEMP

    state = hass.states.get(schedule_id)
    if state is None:
        return {
            "entity_id": schedule_id,
            "state": None,
            "friendly_name": None,
            "next_event": None,
            "override_temp": None,
            "on_temp": on_temp,
            "off_temp": off_temp,
        }
    # If the schedule's current slot carries a per-slot `temperature`
    # attribute, expose it so the dashboard can show the active value.
    override_temp = _safe_float(state.attributes.get("temperature"))
    return {
        "entity_id": schedule_id,
        "state": state.state,
        "friendly_name": state.attributes.get("friendly_name"),
        "next_event": state.attributes.get("next_event"),
        "override_temp": override_temp,
        "on_temp": on_temp,
        "off_temp": off_temp,
    }


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
    """Return overview data for all configured rooms.

    Defensive by design: a single broken room must not break the whole
    overview. Each room is built inside its own try/except and problems
    are surfaced as an ``error`` field on that room card.
    """
    rooms = []

    for entry_id, data in hass.data.get(DOMAIN, {}).items():
        # Skip internal keys (e.g. _zone_manager)
        if entry_id.startswith("_") or not isinstance(data, dict):
            continue

        try:
            rooms.append(_build_room_overview(hass, entry_id, data))
        except Exception as err:  # noqa: BLE001 — reliability is the goal
            _LOGGER.exception(
                "Could not build overview for room %s: %s", entry_id, err
            )
            rooms.append(
                {
                    "entry_id": entry_id,
                    "room_name": (
                        data.get("config", {}).get(CONF_ROOM_NAME, entry_id)
                    ),
                    "error": str(err),
                    "model_state": "error",
                    "learning_progress": 0,
                    "idle_samples": 0,
                    "active_samples": 0,
                    "min_idle": MIN_IDLE_SAMPLES,
                    "min_active": MIN_ACTIVE_SAMPLES,
                    "heat_loss_coeff": 0.0,
                    "thermal_mass": 0.0,
                }
            )

    connection.send_result(msg["id"], {"rooms": rooms})


def _build_room_overview(
    hass: HomeAssistant, entry_id: str, data: dict
) -> dict:
    """Return the overview payload for a single room."""
    model = data.get("model")
    config = data.get("config", {})
    # Use the entity_id our climate entity registered (rename-safe).
    climate_entity_id = data.get("climate_entity_id")

    current_temp = None
    target_temp = None
    outdoor_temp = None
    hvac_action = "idle"
    preset_mode = None

    state = (
        hass.states.get(climate_entity_id) if climate_entity_id else None
    )
    if state:
        current_temp = _safe_float(state.attributes.get("current_temperature"))
        target_temp = _safe_float(state.attributes.get("temperature"))
        outdoor_temp = _safe_float(state.attributes.get("outdoor_temperature"))
        hvac_action = state.attributes.get("hvac_action") or "idle"
        preset_mode = state.attributes.get("preset_mode")

    if current_temp is None:
        temp_sensor_id = config.get("temperature_sensor")
        if temp_sensor_id:
            sensor_state = hass.states.get(temp_sensor_id)
            if sensor_state:
                current_temp = _safe_float(sensor_state.state)

    if outdoor_temp is None:
        outdoor_sensor_id = config.get("outdoor_temperature_sensor")
        if outdoor_sensor_id:
            sensor_state = hass.states.get(outdoor_sensor_id)
            if sensor_state:
                outdoor_temp = _safe_float(sensor_state.state)

    # Learning progress — handle a missing or minimal model gracefully.
    idle_count = getattr(model, "idle_count", 0) if model is not None else 0
    active_count = getattr(model, "active_count", 0) if model is not None else 0
    idle_pct = min(100, idle_count / MIN_IDLE_SAMPLES * 100)
    active_pct = min(100, active_count / MIN_ACTIVE_SAMPLES * 100)
    progress = int((idle_pct + active_pct) / 2)

    params = getattr(model, "params", None) if model is not None else None
    heat_loss_coeff = float(getattr(params, "heat_loss_coeff", 0.0) or 0.0)
    thermal_mass = float(getattr(params, "thermal_mass", 0.0) or 0.0)

    zone = data.get("zone")
    zone_info: dict = {}
    if zone is not None:
        leader = zone.leading_room
        leader_name = leader.room_name if leader else None
        this_room_name = config.get(CONF_ROOM_NAME, "Unknown")
        co_heated = (
            zone.is_heating
            and leader_name is not None
            and leader_name != this_room_name
        )
        zone_info = {
            "zone_id": zone.zone_id,
            "zone_rooms": zone.room_names,
            "zone_is_heating": zone.is_heating,
            "zone_setpoint": zone._last_setpoint,
            "zone_leader_room": leader_name,
            "co_heated_by_zone": co_heated,
        }

    window = _room_window_state(hass, config)
    schedule = _schedule_state(hass, config)

    # Gas / heat power is stashed on the climate entity's attributes.
    heat_power_w = None
    if state:
        heat_power_w = _safe_float(state.attributes.get("heat_power_w"))

    return {
        "entry_id": entry_id,
        "room_name": config.get(CONF_ROOM_NAME, "Unknown"),
        "climate_entity_id": climate_entity_id,
        "model_state": getattr(model, "state", "learning"),
        "current_temp": current_temp,
        "target_temp": target_temp,
        "outdoor_temp": outdoor_temp,
        "hvac_action": hvac_action,
        "preset_mode": preset_mode,
        "heat_loss_coeff": heat_loss_coeff,
        "thermal_mass": thermal_mass,
        "heat_power_w": heat_power_w,
        "idle_samples": idle_count,
        "active_samples": active_count,
        "min_idle": MIN_IDLE_SAMPLES,
        "min_active": MIN_ACTIVE_SAMPLES,
        "learning_progress": progress,
        # Flat, always-present window / schedule fields — the dashboard
        # uses these directly and it's cheaper to flatten here than to
        # reach through nested optional dicts in JS.
        "window": window,
        "window_open": bool(window.get("any_open")),
        "schedule": schedule,
        "schedule_entity": schedule.get("entity_id") if schedule else None,
        "schedule_state": schedule.get("state") if schedule else None,
        **zone_info,
    }


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

    try:
        result = _build_room_detail(hass, entry_id, domain_data[entry_id])
    except Exception as err:  # noqa: BLE001 — must not silently fail
        _LOGGER.exception(
            "Failed to build detail for room %s: %s", entry_id, err
        )
        connection.send_error(
            msg["id"],
            "room_detail_failed",
            f"Could not load details for this room: {err}",
        )
        return

    connection.send_result(msg["id"], result)


def _build_room_detail(
    hass: HomeAssistant, entry_id: str, data: dict
) -> dict:
    """Heavy-lift builder for the detail view payload."""
    model = data.get("model")
    if model is None:
        raise RuntimeError(
            "Thermal model not yet initialized — please wait a moment "
            "and try again."
        )
    config = data.get("config", {})

    # Current temperatures (same logic as overview)
    current_temp = None
    target_temp = None
    outdoor_temp = None
    hvac_action = "idle"
    preset_mode = None
    preset_modes: list[str] = []
    heat_power_w = None
    gas_meter_sensor = None
    boiler_efficiency = None
    heat_share = None

    climate_entity_id = data.get("climate_entity_id")
    state = (
        hass.states.get(climate_entity_id) if climate_entity_id else None
    )
    if state:
        current_temp = _safe_float(state.attributes.get("current_temperature"))
        target_temp = _safe_float(state.attributes.get("temperature"))
        outdoor_temp = _safe_float(state.attributes.get("outdoor_temperature"))
        hvac_action = state.attributes.get("hvac_action") or "idle"
        preset_mode = state.attributes.get("preset_mode")
        preset_modes = list(state.attributes.get("preset_modes") or [])
        heat_power_w = _safe_float(state.attributes.get("heat_power_w"))
        gas_meter_sensor = state.attributes.get("gas_meter_sensor")
        boiler_efficiency = _safe_float(state.attributes.get("boiler_efficiency"))
        heat_share = _safe_float(state.attributes.get("heat_share"))

    if current_temp is None:
        temp_sensor_id = config.get("temperature_sensor")
        if temp_sensor_id:
            s = hass.states.get(temp_sensor_id)
            if s:
                current_temp = _safe_float(s.state)

    if outdoor_temp is None:
        outdoor_sensor_id = config.get("outdoor_temperature_sensor")
        if outdoor_sensor_id:
            s = hass.states.get(outdoor_sensor_id)
            if s:
                outdoor_temp = _safe_float(s.state)

    # Observation history for the temperature chart
    observations = []
    for obs in getattr(model, "observations", [])[-200:]:
        observations.append(
            {
                "timestamp": getattr(obs, "timestamp", 0),
                "t_indoor": _safe_float(getattr(obs, "t_indoor", None)),
                "t_outdoor": _safe_float(getattr(obs, "t_outdoor", None)),
                "heating_on": bool(getattr(obs, "heating_on", False)),
                "solar_irradiance": _safe_float(
                    getattr(obs, "solar_irradiance", 0.0)
                ) or 0.0,
                "heat_power_w": _safe_float(
                    getattr(obs, "heat_power_w", None)
                ),
            }
        )

    # H evolution history for the learning chart
    h_history = []
    for entry in getattr(model, "h_history", []):
        if not isinstance(entry, dict):
            continue
        sample = entry.get("sample")
        value = _safe_float(entry.get("value"))
        if sample is None or value is None:
            continue
        h_history.append({"sample": sample, "value": value})

    # Learning progress
    idle_count = getattr(model, "idle_count", 0)
    active_count = getattr(model, "active_count", 0)
    idle_pct = min(100, idle_count / MIN_IDLE_SAMPLES * 100)
    active_pct = min(100, active_count / MIN_ACTIVE_SAMPLES * 100)
    progress = int((idle_pct + active_pct) / 2)

    # Ensure params are well-formed.
    params = getattr(model, "params", None)
    params_out = {
        "heat_loss_coeff": float(
            getattr(params, "heat_loss_coeff", 0.0) or 0.0
        ),
        "thermal_mass": float(getattr(params, "thermal_mass", 0.0) or 0.0),
        "heating_power": float(getattr(params, "heating_power", 0.0) or 0.0),
        "solar_gain_factor": float(
            getattr(params, "solar_gain_factor", 0.0) or 0.0
        ),
    }

    # Predictions (only if calibrated)
    predictions = None
    model_state = getattr(model, "state", "learning")
    if model_state == STATE_CALIBRATED and current_temp is not None:
        try:
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
                "time_to_target": (
                    round(time_to_target, 1) if time_to_target else None
                ),
            }
        except Exception as err:  # noqa: BLE001 — prediction is optional
            _LOGGER.debug("Prediction failed for %s: %s", entry_id, err)

    # Zone info — leader & co-heat reason + nudge history
    zone = data.get("zone")
    zone_info: dict = {}
    nudge_history: list[dict] = []
    if zone is not None:
        leader = zone.leading_room
        leader_name = leader.room_name if leader else None
        this_room_name = config.get(CONF_ROOM_NAME, "Unknown")
        zone_info = {
            "zone_id": zone.zone_id,
            "zone_rooms": zone.room_names,
            "zone_is_heating": zone.is_heating,
            "zone_setpoint": zone._last_setpoint,
            "zone_leader_room": leader_name,
            "co_heated_by_zone": (
                zone.is_heating
                and leader_name is not None
                and leader_name != this_room_name
            ),
        }
        # Nudge history is populated by HeatingZone._commit_setpoint.
        nudge_history = list(getattr(zone, "nudge_history", []))[-50:]

    # Solar diagnostics — detailed breakdown (best-effort).
    try:
        solar_calc = get_solar_calculation(hass)
    except Exception as err:  # noqa: BLE001 — solar is cosmetic
        _LOGGER.debug("Solar calc failed for %s: %s", entry_id, err)
        solar_calc = None

    window = _room_window_state(hass, config)
    schedule = _schedule_state(hass, config)

    mean_err = getattr(model, "mean_prediction_error", float("inf"))
    mean_err_out = (
        round(mean_err, 3) if mean_err != float("inf") else None
    )

    return {
        "entry_id": entry_id,
        "room_name": config.get(CONF_ROOM_NAME, "Unknown"),
        "climate_entity_id": climate_entity_id,
        "model_state": model_state,
        "current_temp": current_temp,
        "target_temp": target_temp,
        "outdoor_temp": outdoor_temp,
        "hvac_action": hvac_action,
        "preset_mode": preset_mode,
        "preset_modes": preset_modes,
        "params": params_out,
        "idle_samples": idle_count,
        "active_samples": active_count,
        "total_updates": getattr(model, "total_updates", 0),
        "min_idle": MIN_IDLE_SAMPLES,
        "min_active": MIN_ACTIVE_SAMPLES,
        "learning_progress": progress,
        "mean_prediction_error": mean_err_out,
        "prediction_error_history": list(
            getattr(model, "prediction_error_history", [])
        )[-200:],
        "observations": observations,
        "h_history": h_history,
        "predictions": predictions,
        "uses_ekf": (
            hasattr(model, "_ekf") and getattr(model, "_ekf", None) is not None
        ),
        "solar_calc": solar_calc,
        "window": window,
        "window_open": bool(window.get("any_open")),
        "window_sensors": window.get("sensors", []),
        "schedule": schedule,
        "schedule_entity": schedule.get("entity_id") if schedule else None,
        "schedule_state": schedule.get("state") if schedule else None,
        "nudge_history": nudge_history,
        "heat_power_w": heat_power_w,
        "gas_meter_sensor": gas_meter_sensor,
        "boiler_efficiency": boiler_efficiency,
        "heat_share": heat_share,
        **zone_info,
    }


# ─── Orphan management ─────────────────────────────────────────


@websocket_api.websocket_command(
    {vol.Required("type"): "predictive_heating/list_orphans"}
)
@callback
def ws_list_orphans(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """List persisted thermal-model files with no matching config entry."""
    # Lazy import to avoid circular dependency at module load.
    from . import list_orphan_models

    orphans = list_orphan_models(hass)
    connection.send_result(msg["id"], {"orphans": orphans})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "predictive_heating/delete_orphan",
        vol.Required("entry_id"): str,
    }
)
@callback
def ws_delete_orphan(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Delete a single orphan thermal-model file."""
    from . import delete_orphan_model

    ok = delete_orphan_model(hass, msg["entry_id"])
    if ok:
        connection.send_result(msg["id"], {"deleted": True})
    else:
        connection.send_error(
            msg["id"], "not_found", "No orphan file matched that entry_id"
        )


# ─── Dashboard control (temperature + preset) ────────────────────
#
# These wrap climate.set_temperature / climate.set_preset_mode so the
# dashboard has inline controls instead of the user having to hunt for
# the climate entity in HA's regular UI.


@websocket_api.websocket_command(
    {
        vol.Required("type"): "predictive_heating/set_temperature",
        vol.Required("entry_id"): str,
        vol.Required("temperature"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_set_temperature(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Set the target temperature for a room from the dashboard."""
    data = hass.data.get(DOMAIN, {}).get(msg["entry_id"])
    if not data:
        connection.send_error(msg["id"], "not_found", "Room not found")
        return
    climate_entity_id = data.get("climate_entity_id")
    if not climate_entity_id:
        connection.send_error(
            msg["id"],
            "not_ready",
            "Climate entity not ready yet — try again in a moment.",
        )
        return
    try:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {
                "entity_id": climate_entity_id,
                "temperature": float(msg["temperature"]),
            },
            blocking=True,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("set_temperature failed: %s", err)
        connection.send_error(msg["id"], "service_call_failed", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "predictive_heating/set_preset",
        vol.Required("entry_id"): str,
        # Accept either ``preset_mode`` (what climate.set_preset_mode uses,
        # and what the dashboard sends) or the legacy ``preset`` alias.
        vol.Exclusive("preset_mode", "preset"): str,
        vol.Exclusive("preset", "preset"): str,
    }
)
@websocket_api.async_response
async def ws_set_preset(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Set the preset mode for a room from the dashboard."""
    data = hass.data.get(DOMAIN, {}).get(msg["entry_id"])
    if not data:
        connection.send_error(msg["id"], "not_found", "Room not found")
        return
    climate_entity_id = data.get("climate_entity_id")
    if not climate_entity_id:
        connection.send_error(
            msg["id"],
            "not_ready",
            "Climate entity not ready yet — try again in a moment.",
        )
        return
    preset = msg.get("preset_mode") or msg.get("preset")
    if not preset:
        connection.send_error(
            msg["id"],
            "missing_preset",
            "A preset_mode value is required.",
        )
        return
    try:
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {
                "entity_id": climate_entity_id,
                "preset_mode": preset,
            },
            blocking=True,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("set_preset failed: %s", err)
        connection.send_error(msg["id"], "service_call_failed", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})
