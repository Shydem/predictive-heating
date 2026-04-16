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
    CONF_CLIMATE_ENTITY,
    CONF_HUMIDITY_SENSOR,
    CONF_MAX_SETPOINT_DELTA,
    CONF_OPENTHERM_ENABLED,
    CONF_OPENTHERM_FLOW_TEMP_NUMBER,
    CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_ROOM_NAME,
    CONF_TEMPERATURE_SENSOR,
    CONF_WINDOW_SENSORS,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
    DEFAULT_AWAY_TEMP,
    DEFAULT_MAX_FLOW_TEMP,
    DEFAULT_MAX_SETPOINT_DELTA,
    DEFAULT_MIN_FLOW_TEMP,
    DEFAULT_SLEEP_TEMP,
    DOMAIN,
)

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
                vol.Optional(
                    CONF_OPENTHERM_ENABLED, default=False
                ): selector.BooleanSelector(),
                vol.Optional(CONF_OPENTHERM_FLOW_TEMP_NUMBER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
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
        """Return the options flow handler."""
        return PredictiveHeatingOptionsFlow(config_entry)


class PredictiveHeatingOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Predictive Heating."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage temperature presets, setpoint limits, and flow temp."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        is_opentherm = self.config_entry.data.get(CONF_OPENTHERM_ENABLED, False)

        schema_dict = {
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
                "max_setpoint_delta",
                default=options.get("max_setpoint_delta", DEFAULT_MAX_SETPOINT_DELTA),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.5, max=10.0, step=0.5, unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
        }

        # OpenTherm-specific options
        if is_opentherm:
            schema_dict[
                vol.Optional(
                    "min_flow_temp",
                    default=options.get("min_flow_temp", DEFAULT_MIN_FLOW_TEMP),
                )
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=20.0, max=50.0, step=1.0, unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            )
            schema_dict[
                vol.Optional(
                    "max_flow_temp",
                    default=options.get("max_flow_temp", DEFAULT_MAX_FLOW_TEMP),
                )
            ] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=30.0, max=80.0, step=1.0, unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
        )
