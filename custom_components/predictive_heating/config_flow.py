"""Config flow for Predictive Heating integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    BUILDING_TYPES,
    COMFORT_RAMP_OPTIONS,
    CONF_AWAY_GRACE_MIN,
    CONF_BOILER_EFFICIENCY,
    CONF_BUILDING_TYPE,
    CONF_CEILING_HEIGHT_M,
    CONF_CLIMATE_ENTITY,
    CONF_COMFORT_RAMP,
    CONF_FLOOR_AREA_M2,
    CONF_GAS_CALORIFIC_VALUE,
    CONF_GAS_METER_SENSOR,
    CONF_HEAT_SHARE,
    CONF_CONTROL_MODE,
    CONF_HUMIDITY_SENSOR,
    CONF_MAX_PREHEAT_NUDGE,
    CONF_MAX_SETPOINT_DELTA,
    CONF_NUDGE_INTERVAL_MIN,
    CONF_NUDGE_STEP,
    CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_PERSON_ENTITIES,
    CONF_ROOM_NAME,
    CONF_SCHEDULE_ENTITY,
    CONF_SCHEDULE_OFF_TEMP,
    CONF_SCHEDULE_ON_TEMP,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMAL_COUPLINGS,
    CONF_WEATHER_ENTITY,
    CONF_WINDOW_SENSORS,
    CONTROL_MODE_OPTIONS,
    DEFAULT_AWAY_GRACE_MIN,
    DEFAULT_AWAY_TEMP,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_BUILDING_TYPE,
    DEFAULT_CEILING_HEIGHT_M,
    DEFAULT_COMFORT_RAMP,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_CONTROL_MODE,
    DEFAULT_COUPLING_U,
    DEFAULT_COUPLING_U_CLOSED,
    DEFAULT_COUPLING_U_OPEN,
    DEFAULT_ECO_TEMP,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_HEAT_SHARE,
    DEFAULT_MAX_PREHEAT_NUDGE,
    DEFAULT_MAX_SETPOINT_DELTA,
    DEFAULT_NUDGE_INTERVAL_MIN,
    DEFAULT_NUDGE_STEP,
    DEFAULT_SLEEP_TEMP,
    DOMAIN,
)


_BUILDING_TYPE_OPTIONS = [
    selector.SelectOptionDict(value=key, label=key.replace("_", " ").title())
    for key in BUILDING_TYPES
]

_COMFORT_RAMP_OPTIONS = [
    selector.SelectOptionDict(value=v, label=v.title())
    for v in COMFORT_RAMP_OPTIONS
]

_CONTROL_MODE_OPTIONS = [
    selector.SelectOptionDict(value=v, label=v.title())
    for v in CONTROL_MODE_OPTIONS
]

_LOGGER = logging.getLogger(__name__)


class PredictiveHeatingConfigFlow(
    config_entries.ConfigFlow, domain=DOMAIN
):
    """Handle a config flow for Predictive Heating."""

    VERSION = 2

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step — room setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(
                f"predictive_heating_{user_input[CONF_ROOM_NAME]}"
            )
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=user_input[CONF_ROOM_NAME],
                data=user_input,
            )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ROOM_NAME): str,
                vol.Required(CONF_TEMPERATURE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=SENSOR_DOMAIN)
                ),
                vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN)
                ),
                vol.Optional(CONF_OUTDOOR_TEMPERATURE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=SENSOR_DOMAIN)
                ),
                vol.Optional(CONF_HUMIDITY_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=SENSOR_DOMAIN)
                ),
                vol.Optional(CONF_WINDOW_SENSORS): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="binary_sensor",
                        multiple=True,
                    )
                ),
                vol.Optional(CONF_GAS_METER_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=SENSOR_DOMAIN)
                ),
                vol.Optional(
                    CONF_BOILER_EFFICIENCY,
                    default=DEFAULT_BOILER_EFFICIENCY,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.5, max=1.10, step=0.01,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_HEAT_SHARE,
                    default=DEFAULT_HEAT_SHARE,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0.01, max=1.0, step=0.01,
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(
                    CONF_GAS_CALORIFIC_VALUE,
                    default=DEFAULT_GAS_CALORIFIC_VALUE,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=20.0, max=45.0, step=0.01,
                        unit_of_measurement="MJ/m³",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_FLOOR_AREA_M2): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=2, max=500, step=0.5,
                        unit_of_measurement="m²",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_CEILING_HEIGHT_M,
                    default=DEFAULT_CEILING_HEIGHT_M,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1.8, max=6.0, step=0.1,
                        unit_of_measurement="m",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_BUILDING_TYPE,
                    default=DEFAULT_BUILDING_TYPE,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_BUILDING_TYPE_OPTIONS,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_SCHEDULE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="schedule")
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> PredictiveHeatingOptionsFlow:
        """Return the options flow handler.

        Note: do NOT pass ``config_entry`` to the handler. Home Assistant
        2025.12+ manages ``self.config_entry`` automatically on the
        OptionsFlow base class. Explicitly setting it (even by passing it
        in) raises an error and breaks the options menu with a 500
        "config flow kon niet geladen worden" error.
        """
        return PredictiveHeatingOptionsFlow()


class PredictiveHeatingOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Predictive Heating.

    Do not define ``__init__`` or assign ``self.config_entry``; the base
    class exposes it as a managed property in HA 2025.12+ and assignment
    now raises. Access the entry via ``self.config_entry``.
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Root options step — show menu between main settings and couplings.

        Splitting the options flow into a menu keeps the main form from
        growing unbounded as we add per-room features. The coupling editor
        also depends on *other* configured rooms, which don't exist at the
        time the first room is created via ``async_step_user`` — the menu
        lets the user come back later to wire them up.
        """
        return self.async_show_menu(
            step_id="init",
            menu_options=["main", "couplings"],
        )

    async def async_step_main(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage room name, temperature presets, and setpoint limits."""
        # Fields that live on entry.data (room identity), not in options.
        # Everything else (window sensors, gas sensor, temps…) goes to options
        # so the user can reconfigure without removing and re-adding the room.
        _DATA_FIELDS = (
            CONF_ROOM_NAME,
            CONF_FLOOR_AREA_M2,
            CONF_CEILING_HEIGHT_M,
            CONF_BUILDING_TYPE,
        )

        if user_input is not None:
            current_data = dict(self.config_entry.data)
            new_data = dict(current_data)
            data_changed = False
            title = self.config_entry.title

            for key in _DATA_FIELDS:
                if key not in user_input:
                    continue
                value = user_input[key]
                if value != current_data.get(key):
                    new_data[key] = value
                    data_changed = True
                    if key == CONF_ROOM_NAME and value:
                        title = value

            if data_changed:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                    title=title,
                )

            # Strip entry.data fields out of options, then merge the rest
            # onto the existing options so keys that live exclusively in
            # other steps (e.g. CONF_THERMAL_COUPLINGS from async_step_couplings)
            # are not clobbered when the user saves the main form.
            options_to_save = dict(self.config_entry.options)
            for k, v in user_input.items():
                if k in _DATA_FIELDS:
                    continue
                options_to_save[k] = v
            return self.async_create_entry(title="", data=options_to_save)

        data = self.config_entry.data
        options = self.config_entry.options
        current_name = data.get(CONF_ROOM_NAME) or self.config_entry.title

        schema_dict = {
            vol.Required(
                CONF_ROOM_NAME,
                default=current_name,
            ): str,
            vol.Optional(
                "comfort_temp",
                default=options.get("comfort_temp", DEFAULT_COMFORT_TEMP),
            ): vol.Coerce(float),
            vol.Optional(
                "eco_temp",
                default=options.get("eco_temp", DEFAULT_ECO_TEMP),
            ): vol.Coerce(float),
            vol.Optional(
                "away_temp",
                default=options.get("away_temp", DEFAULT_AWAY_TEMP),
            ): vol.Coerce(float),
            vol.Optional(
                "sleep_temp",
                default=options.get("sleep_temp", DEFAULT_SLEEP_TEMP),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_MAX_SETPOINT_DELTA,
                default=options.get(CONF_MAX_SETPOINT_DELTA, DEFAULT_MAX_SETPOINT_DELTA),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=5.0, step=0.1, unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(
                CONF_NUDGE_STEP,
                default=options.get(CONF_NUDGE_STEP, DEFAULT_NUDGE_STEP),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=1.0, step=0.1, unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(
                CONF_NUDGE_INTERVAL_MIN,
                default=options.get(
                    CONF_NUDGE_INTERVAL_MIN, DEFAULT_NUDGE_INTERVAL_MIN
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=2, max=60, step=1, unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_WINDOW_SENSORS,
                default=(
                    options.get(CONF_WINDOW_SENSORS)
                    or data.get(CONF_WINDOW_SENSORS)
                    or vol.UNDEFINED
                ),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="binary_sensor",
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_GAS_METER_SENSOR,
                default=(
                    data.get(CONF_GAS_METER_SENSOR)
                    or options.get(CONF_GAS_METER_SENSOR)
                    or vol.UNDEFINED
                ),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=SENSOR_DOMAIN)
            ),
            vol.Optional(
                CONF_BOILER_EFFICIENCY,
                default=options.get(
                    CONF_BOILER_EFFICIENCY,
                    data.get(CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY),
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.5, max=1.10, step=0.01,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_HEAT_SHARE,
                default=options.get(
                    CONF_HEAT_SHARE,
                    data.get(CONF_HEAT_SHARE, DEFAULT_HEAT_SHARE),
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.01, max=1.0, step=0.01,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(
                CONF_GAS_CALORIFIC_VALUE,
                default=options.get(
                    CONF_GAS_CALORIFIC_VALUE,
                    data.get(CONF_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE),
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=20.0, max=45.0, step=0.01,
                    unit_of_measurement="MJ/m³",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            # ── Schedule (optional) ─────────────────────────────
            vol.Optional(
                CONF_SCHEDULE_ENTITY,
                default=(
                    options.get(CONF_SCHEDULE_ENTITY)
                    or data.get(CONF_SCHEDULE_ENTITY)
                    or vol.UNDEFINED
                ),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="schedule")
            ),
            vol.Optional(
                CONF_SCHEDULE_ON_TEMP,
                default=options.get(
                    CONF_SCHEDULE_ON_TEMP,
                    options.get("comfort_temp", DEFAULT_COMFORT_TEMP),
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5.0, max=30.0, step=0.5,
                    unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_SCHEDULE_OFF_TEMP,
                default=options.get(
                    CONF_SCHEDULE_OFF_TEMP,
                    options.get("eco_temp", DEFAULT_ECO_TEMP),
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5.0, max=30.0, step=0.5,
                    unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            # ── Predictive pre-heat + monitoring (v0.7) ───────────
            vol.Optional(
                CONF_WEATHER_ENTITY,
                default=(
                    options.get(CONF_WEATHER_ENTITY)
                    or data.get(CONF_WEATHER_ENTITY)
                    or vol.UNDEFINED
                ),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="weather")
            ),
            vol.Optional(
                CONF_PERSON_ENTITIES,
                default=(
                    options.get(CONF_PERSON_ENTITIES)
                    or data.get(CONF_PERSON_ENTITIES)
                    or vol.UNDEFINED
                ),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="person",
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_AWAY_GRACE_MIN,
                default=options.get(CONF_AWAY_GRACE_MIN, DEFAULT_AWAY_GRACE_MIN),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=120, step=1,
                    unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_COMFORT_RAMP,
                default=options.get(CONF_COMFORT_RAMP, DEFAULT_COMFORT_RAMP),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_COMFORT_RAMP_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            # v0.7: MPC was removed. The integration is now monitor-first,
            # with the controller just following the preset schedule and the
            # PreheatPlanner raising the target a bit earlier so the thermostat
            # reaches the scheduled temperature on time. The two knobs exposed
            # to the user are:
            #   * control_mode — "observe" (never writes a setpoint; purely a
            #       predictive monitor) vs "follow" (writes the scheduled preset
            #       plus at most max_preheat_nudge extra while pre-heating).
            #   * max_preheat_nudge — upper bound on how far above the scheduled
            #       target the pre-heat planner is allowed to push the setpoint
            #       when it needs to reach the target on time. 0 disables.
            vol.Optional(
                CONF_CONTROL_MODE,
                default=options.get(CONF_CONTROL_MODE, DEFAULT_CONTROL_MODE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_CONTROL_MODE_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_MAX_PREHEAT_NUDGE,
                default=options.get(
                    CONF_MAX_PREHEAT_NUDGE, DEFAULT_MAX_PREHEAT_NUDGE
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0, max=2.0, step=0.1,
                    unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
        }

        # Room dimensions — keep defaults from entry.data if the user already set them.
        floor_area_default = data.get(CONF_FLOOR_AREA_M2)
        if floor_area_default is not None:
            schema_dict[
                vol.Optional(CONF_FLOOR_AREA_M2, default=floor_area_default)
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=2, max=500, step=0.5,
                    unit_of_measurement="m²",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        else:
            schema_dict[vol.Optional(CONF_FLOOR_AREA_M2)] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=2, max=500, step=0.5,
                    unit_of_measurement="m²",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )

        schema_dict[
            vol.Optional(
                CONF_CEILING_HEIGHT_M,
                default=data.get(CONF_CEILING_HEIGHT_M, DEFAULT_CEILING_HEIGHT_M),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1.8, max=6.0, step=0.1,
                unit_of_measurement="m",
                mode=selector.NumberSelectorMode.BOX,
            )
        )
        schema_dict[
            vol.Optional(
                CONF_BUILDING_TYPE,
                default=data.get(CONF_BUILDING_TYPE, DEFAULT_BUILDING_TYPE),
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_BUILDING_TYPE_OPTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        return self.async_show_form(
            step_id="main",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_couplings(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Edit thermal couplings to neighbouring rooms.

        For every *other* predictive-heating config entry we render a small
        group of fields:

          * ``couple_<entry_id>_enabled`` — whether heat exchange with this
            neighbour is modelled at all.
          * ``couple_<entry_id>_u_closed`` — conductance in W/K when the door
            between the two rooms is closed (or always, if no door sensor).
            Closed solid door ≈ 10–20 W/K, glazed door ≈ 20–40.
          * ``couple_<entry_id>_u_open`` — conductance when the door is open.
            Typical open doorway ≈ 60–150 W/K.
          * ``couple_<entry_id>_door_sensor`` — optional binary_sensor used to
            pick between u_closed and u_open live.
          * ``couple_<entry_id>_learn`` — whether the online learner is
            allowed to refine u_closed/u_open from observed thermal drift.

        On submit we serialise to a list of dicts under
        ``CONF_THERMAL_COUPLINGS`` in the entry's options:
        ``{neighbour_entry_id, enabled, u_closed, u_open, door_sensor, learn}``.
        We also keep writing the legacy ``u_value`` key (= u_closed) so older
        builds of the runtime loader stay compatible during rollout.
        """
        # Build the list of neighbour entries once per render so we know
        # which keys to read back when the user submits the form.
        neighbours: list[tuple[str, str]] = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == self.config_entry.entry_id:
                continue
            # ``entry.title`` is the room name (see async_step_user).
            title = entry.title or entry.data.get(CONF_ROOM_NAME) or entry.entry_id
            neighbours.append((entry.entry_id, title))
        neighbours.sort(key=lambda pair: pair[1].lower())

        if user_input is not None:
            couplings: list[dict[str, Any]] = []
            for entry_id, _title in neighbours:
                enabled = bool(user_input.get(f"couple_{entry_id}_enabled", False))

                def _read_float(key: str, fallback: float) -> float:
                    try:
                        return float(user_input.get(key, fallback))
                    except (TypeError, ValueError):
                        return fallback

                u_closed = _read_float(
                    f"couple_{entry_id}_u_closed", DEFAULT_COUPLING_U_CLOSED
                )
                u_open = _read_float(
                    f"couple_{entry_id}_u_open", DEFAULT_COUPLING_U_OPEN
                )
                # Clamp — schema enforces this but be defensive since the
                # options flow input is essentially user-controlled JSON.
                u_closed = max(0.0, min(500.0, u_closed))
                u_open = max(0.0, min(500.0, u_open))
                # Physically, an open door conducts more heat than a closed
                # one. If the user (or a stale default) violates that, push
                # u_open up to at least u_closed so the learner starts from
                # a sane prior.
                if u_open < u_closed:
                    u_open = u_closed

                door_sensor = user_input.get(f"couple_{entry_id}_door_sensor") or None
                learn = bool(user_input.get(f"couple_{entry_id}_learn", True))

                couplings.append(
                    {
                        "neighbour_entry_id": entry_id,
                        "enabled": enabled,
                        "u_closed": u_closed,
                        "u_open": u_open,
                        # Legacy key — older thermal_model builds read
                        # ``u_value`` instead of ``u_closed``. Keep it in sync
                        # so rollbacks and partial upgrades don't misread.
                        "u_value": u_closed,
                        "door_sensor": door_sensor,
                        "learn": learn,
                    }
                )

            # Merge with whatever else was already in options — we must
            # not blow away unrelated keys by returning only couplings.
            new_options = dict(self.config_entry.options)
            new_options[CONF_THERMAL_COUPLINGS] = couplings
            return self.async_create_entry(title="", data=new_options)

        # No other rooms configured — show an empty form with a note.
        if not neighbours:
            return self.async_show_form(
                step_id="couplings",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "info": (
                        "No other rooms are configured yet. Add another "
                        "Predictive Heating room first, then come back here "
                        "to link them thermally."
                    )
                },
                errors={"base": "no_neighbours"},
            )

        # Build current state lookup so defaults reflect what's saved.
        existing: dict[str, dict[str, Any]] = {}
        for row in self.config_entry.options.get(CONF_THERMAL_COUPLINGS, []) or []:
            nid = row.get("neighbour_entry_id")
            if nid:
                existing[nid] = row

        schema_dict: dict[Any, Any] = {}
        for entry_id, title in neighbours:
            saved = existing.get(entry_id, {})

            # Resolve defaults with v0.6 back-compat: if only the old
            # ``u_value`` field is present, use it as the u_closed default
            # and fall back to DEFAULT_COUPLING_U_OPEN for u_open.
            legacy_u = saved.get("u_value")
            u_closed_default = float(
                saved.get(
                    "u_closed",
                    legacy_u if legacy_u is not None else DEFAULT_COUPLING_U_CLOSED,
                )
            )
            u_open_default = float(
                saved.get("u_open", DEFAULT_COUPLING_U_OPEN)
            )
            door_sensor_default = saved.get("door_sensor") or vol.UNDEFINED
            learn_default = bool(saved.get("learn", True))

            schema_dict[
                vol.Optional(
                    f"couple_{entry_id}_enabled",
                    default=bool(saved.get("enabled", False)),
                    description={"suggested_value": bool(saved.get("enabled", False))},
                )
            ] = selector.BooleanSelector()
            schema_dict[
                vol.Optional(
                    f"couple_{entry_id}_u_closed",
                    default=u_closed_default,
                )
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=200.0,
                    step=1.0,
                    unit_of_measurement="W/K",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[
                vol.Optional(
                    f"couple_{entry_id}_u_open",
                    default=u_open_default,
                )
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=400.0,
                    step=1.0,
                    unit_of_measurement="W/K",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            schema_dict[
                vol.Optional(
                    f"couple_{entry_id}_door_sensor",
                    default=door_sensor_default,
                )
            ] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            )
            schema_dict[
                vol.Optional(
                    f"couple_{entry_id}_learn",
                    default=learn_default,
                )
            ] = selector.BooleanSelector()
            # A blank spacer — in HA's forms there's no header element,
            # so we rely on field order and the label prefix ("couple_"
            # + room name) to communicate grouping. Labels come from
            # translations; absent those, HA shows the raw key, which
            # is already reasonably self-describing.
            # (No actual spacer inserted — voluptuous schemas don't
            # support that — this comment documents intent.)

        # Render labels: HA's form renderer will fall back to the raw
        # key unless a translation file supplies a label. Provide a
        # description_placeholders table so the template can interpolate
        # neighbour names if the user ships a translation later.
        placeholders = {
            f"name_{eid}": name for eid, name in neighbours
        }

        return self.async_show_form(
            step_id="couplings",
            data_schema=vol.Schema(schema_dict),
            description_placeholders=placeholders,
        )
