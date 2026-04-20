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
    CONF_HUMIDITY_SENSOR,
    CONF_MAX_SETPOINT_DELTA,
    CONF_MPC_CONTROL_DELAY_MIN,
    CONF_MPC_ENABLED,
    CONF_MPC_HORIZON_MIN,
    CONF_MPC_STEP_MIN,
    CONF_NUDGE_INTERVAL_MIN,
    CONF_NUDGE_STEP,
    CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_PERSON_ENTITIES,
    CONF_ROOM_NAME,
    CONF_SCHEDULE_ENTITY,
    CONF_SCHEDULE_OFF_TEMP,
    CONF_SCHEDULE_ON_TEMP,
    CONF_TEMPERATURE_SENSOR,
    CONF_WEATHER_ENTITY,
    CONF_WINDOW_SENSORS,
    DEFAULT_AWAY_GRACE_MIN,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_BUILDING_TYPE,
    DEFAULT_CEILING_HEIGHT_M,
    DEFAULT_COMFORT_RAMP,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
    DEFAULT_AWAY_TEMP,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_HEAT_SHARE,
    DEFAULT_MAX_SETPOINT_DELTA,
    DEFAULT_MPC_CONTROL_DELAY_MIN,
    DEFAULT_MPC_ENABLED,
    DEFAULT_MPC_HORIZON_MIN,
    DEFAULT_MPC_STEP_MIN,
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

            # Strip entry.data fields out of options.
            options_to_save = {
                k: v for k, v in user_input.items() if k not in _DATA_FIELDS
            }
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
            # ── Predictive pre-heat + MPC (v0.3) ──────────────────
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
            vol.Optional(
                CONF_MPC_ENABLED,
                default=options.get(CONF_MPC_ENABLED, DEFAULT_MPC_ENABLED),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_MPC_HORIZON_MIN,
                default=options.get(CONF_MPC_HORIZON_MIN, DEFAULT_MPC_HORIZON_MIN),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=15, max=240, step=5,
                    unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MPC_STEP_MIN,
                default=options.get(CONF_MPC_STEP_MIN, DEFAULT_MPC_STEP_MIN),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=15, step=1,
                    unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MPC_CONTROL_DELAY_MIN,
                default=options.get(
                    CONF_MPC_CONTROL_DELAY_MIN,
                    DEFAULT_MPC_CONTROL_DELAY_MIN,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=30, step=1,
                    unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
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
            step_id="init",
            data_schema=vol.Schema(schema_dict),
        )
