"""Config flow for Predictive Heating integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO_CONTROL,
    CONF_AWAY_TEMP,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_W,
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_GAS_CONSUMPTION_ENTITY,
    CONF_GAS_EFFICIENCY,
    CONF_HEATING_DEVICES,
    CONF_HOUSE_FLOOR_AREA_M2,
    CONF_HOUSE_INSULATION,
    CONF_HOUSE_THERMAL_MASS,
    CONF_HOUSE_TYPE,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INTERNAL_GAIN_W,
    CONF_OPTIMIZATION_TIMESTEP_MIN,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PREDICTION_HORIZON_HOURS,
    CONF_TEMPERATURE_SCHEDULE,
    CONF_TRAINING_INTERVAL_DAYS,
    CONF_TRAINING_USE_CONSTANT_OUTDOOR,
    CONF_TRAINING_WINDOW_DAYS,
    CONF_WEATHER_ENTITY,
    DEFAULT_AWAY_TEMP,
    DEFAULT_GAS_EFFICIENCY,
    DEFAULT_OPTIMIZATION_TIMESTEP_MIN,
    DEFAULT_PREDICTION_HORIZON_HOURS,
    DEFAULT_TEMPERATURE_SCHEDULE,
    DEFAULT_TRAINING_INTERVAL_DAYS,
    DEFAULT_TRAINING_WINDOW_DAYS,
    DOMAIN,
    FALLBACK_INTERNAL_GAIN_W,
    HOUSE_TYPE_APARTMENT,
    HOUSE_TYPE_DETACHED,
    HOUSE_TYPE_SEMI_DETACHED,
    HOUSE_TYPE_TERRACED,
    INSULATION_EXCELLENT,
    INSULATION_GOOD,
    INSULATION_MODERATE,
    INSULATION_POOR,
    THERMAL_MASS_HEAVY,
    THERMAL_MASS_LIGHT,
    THERMAL_MASS_MEDIUM,
)
from .house_profile import estimate_initial_params

_LOGGER = logging.getLogger(__name__)


class PredictiveHeatingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow: entities → house profile → devices."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1: Select sensor entities."""
        errors: dict[str, str] = {}
        if user_input is not None:
            for key in (CONF_INDOOR_TEMP_ENTITY, CONF_OUTDOOR_TEMP_ENTITY, CONF_ELECTRICITY_PRICE_ENTITY):
                if user_input.get(key) and self.hass.states.get(user_input[key]) is None:
                    errors[key] = "entity_not_found"
            for key in (CONF_WEATHER_ENTITY,):
                if user_input.get(key) and self.hass.states.get(user_input[key]) is None:
                    errors[key] = "entity_not_found"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_house_profile()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_INDOOR_TEMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(CONF_OUTDOOR_TEMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(CONF_ELECTRICITY_PRICE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")),
                vol.Optional(CONF_WEATHER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")),
                vol.Optional(CONF_GAS_CONSUMPTION_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")),
            }),
            errors=errors,
        )

    async def async_step_house_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: House profile for initial parameter estimation."""
        if user_input is not None:
            self._data.update(user_input)
            ua, thermal_mass, _ = estimate_initial_params(
                floor_area_m2=user_input[CONF_HOUSE_FLOOR_AREA_M2],
                house_type=user_input[CONF_HOUSE_TYPE],
                insulation=user_input[CONF_HOUSE_INSULATION],
                thermal_mass_class=user_input[CONF_HOUSE_THERMAL_MASS],
            )
            self._data["initial_ua_estimate"] = ua
            self._data["initial_thermal_mass_estimate"] = thermal_mass
            return await self.async_step_parameters()

        return self.async_show_form(
            step_id="house_profile",
            data_schema=vol.Schema({
                vol.Required(CONF_HOUSE_FLOOR_AREA_M2, default=100): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=20, max=500, step=5, unit_of_measurement="m²")),
                vol.Required(CONF_HOUSE_TYPE, default=HOUSE_TYPE_SEMI_DETACHED): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(value=HOUSE_TYPE_DETACHED, label="Detached (vrijstaand)"),
                        selector.SelectOptionDict(value=HOUSE_TYPE_SEMI_DETACHED, label="Semi-detached (twee-onder-een-kap)"),
                        selector.SelectOptionDict(value=HOUSE_TYPE_TERRACED, label="Terraced (rijtjeshuis)"),
                        selector.SelectOptionDict(value=HOUSE_TYPE_APARTMENT, label="Apartment (appartement)"),
                    ])),
                vol.Required(CONF_HOUSE_INSULATION, default=INSULATION_MODERATE): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(value=INSULATION_POOR, label="Poor — pre-1975, energy label E-G"),
                        selector.SelectOptionDict(value=INSULATION_MODERATE, label="Moderate — 1975-2000, label C-D"),
                        selector.SelectOptionDict(value=INSULATION_GOOD, label="Good — post-2000, label A-B"),
                        selector.SelectOptionDict(value=INSULATION_EXCELLENT, label="Excellent — passive house level"),
                    ])),
                vol.Required(CONF_HOUSE_THERMAL_MASS, default=THERMAL_MASS_MEDIUM): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(value=THERMAL_MASS_LIGHT, label="Light — timber frame, prefab"),
                        selector.SelectOptionDict(value=THERMAL_MASS_MEDIUM, label="Medium — brick cavity walls"),
                        selector.SelectOptionDict(value=THERMAL_MASS_HEAVY, label="Heavy — solid brick, concrete floors"),
                    ])),
            }),
        )

    async def async_step_parameters(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 3: Model and optimizer parameters."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_devices()

        return self.async_show_form(
            step_id="parameters",
            data_schema=vol.Schema({
                vol.Optional(CONF_INTERNAL_GAIN_W, default=FALLBACK_INTERNAL_GAIN_W): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=2000, step=50, unit_of_measurement="W")),
                vol.Optional(CONF_AWAY_TEMP, default=DEFAULT_AWAY_TEMP): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=20, step=0.5, unit_of_measurement="°C")),
                vol.Optional(CONF_AUTO_CONTROL, default=False): selector.BooleanSelector(),
                vol.Optional(CONF_TRAINING_INTERVAL_DAYS, default=DEFAULT_TRAINING_INTERVAL_DAYS): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=30, step=1)),
                vol.Optional(CONF_TRAINING_WINDOW_DAYS, default=DEFAULT_TRAINING_WINDOW_DAYS): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=7, max=90, step=1)),
                vol.Optional(CONF_PREDICTION_HORIZON_HOURS, default=DEFAULT_PREDICTION_HORIZON_HOURS): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=48, step=1)),
                vol.Optional(CONF_OPTIMIZATION_TIMESTEP_MIN, default=DEFAULT_OPTIMIZATION_TIMESTEP_MIN): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=60, step=5)),
                vol.Optional(CONF_TRAINING_USE_CONSTANT_OUTDOOR, default=True): selector.BooleanSelector(),
                vol.Optional(CONF_GAS_EFFICIENCY, default=DEFAULT_GAS_EFFICIENCY): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.5, max=1.0, step=0.01)),
            }),
        )

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 4: Add heaters (entity + rated power).

        Each heater needs:
        - A friendly name (for sensor labels)
        - The HA entity that controls it (switch.*, climate.*, etc.)
        - Its rated heat output in watts (from nameplate or spec sheet)
        """
        if user_input is not None:
            self._devices.append({
                CONF_DEVICE_NAME: user_input[CONF_DEVICE_NAME],
                CONF_DEVICE_ENTITY: user_input[CONF_DEVICE_ENTITY],
                CONF_DEVICE_POWER_W: user_input[CONF_DEVICE_POWER_W],
            })
            if user_input.get("add_another", False):
                return await self.async_step_devices()

            self._data[CONF_HEATING_DEVICES] = self._devices
            self._data[CONF_TEMPERATURE_SCHEDULE] = DEFAULT_TEMPERATURE_SCHEDULE
            return self.async_create_entry(title="Predictive Heating", data=self._data)

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_NAME): selector.TextSelector(),
                vol.Required(CONF_DEVICE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["climate", "switch", "binary_sensor"])),
                vol.Required(CONF_DEVICE_POWER_W, default=2000): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=100, max=100000, step=100, unit_of_measurement="W")),
                vol.Optional("add_another", default=False): selector.BooleanSelector(),
            }),
            description_placeholders={"device_count": str(len(self._devices))},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> PredictiveHeatingOptionsFlow:
        return PredictiveHeatingOptionsFlow(config_entry)


class PredictiveHeatingOptionsFlow(config_entries.OptionsFlow):
    """Options flow for tuning parameters after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_INTERNAL_GAIN_W,
                    default=current.get(CONF_INTERNAL_GAIN_W, FALLBACK_INTERNAL_GAIN_W)):
                    selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=2000, step=50, unit_of_measurement="W")),
                vol.Optional(CONF_AWAY_TEMP,
                    default=current.get(CONF_AWAY_TEMP, DEFAULT_AWAY_TEMP)):
                    selector.NumberSelector(selector.NumberSelectorConfig(min=5, max=20, step=0.5, unit_of_measurement="°C")),
                vol.Optional(CONF_AUTO_CONTROL,
                    default=current.get(CONF_AUTO_CONTROL, False)):
                    selector.BooleanSelector(),
                vol.Optional(CONF_TRAINING_INTERVAL_DAYS,
                    default=current.get(CONF_TRAINING_INTERVAL_DAYS, DEFAULT_TRAINING_INTERVAL_DAYS)):
                    selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=30, step=1)),
                vol.Optional(CONF_PREDICTION_HORIZON_HOURS,
                    default=current.get(CONF_PREDICTION_HORIZON_HOURS, DEFAULT_PREDICTION_HORIZON_HOURS)):
                    selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=48, step=1)),
                vol.Optional(CONF_TRAINING_USE_CONSTANT_OUTDOOR,
                    default=current.get(CONF_TRAINING_USE_CONSTANT_OUTDOOR, True)):
                    selector.BooleanSelector(),
                vol.Optional(CONF_GAS_EFFICIENCY,
                    default=current.get(CONF_GAS_EFFICIENCY, DEFAULT_GAS_EFFICIENCY)):
                    selector.NumberSelector(selector.NumberSelectorConfig(min=0.5, max=1.0, step=0.01)),
            }),
        )
