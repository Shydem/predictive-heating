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
    CONF_COP_COEFFICIENTS,
    CONF_DEVICE_COP_DATA,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_MAX_OUTPUT_W,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SOURCE,
    CONF_DEVICE_TYPE,
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_GAS_CONSUMPTION_ENTITY,
    CONF_GAS_EFFICIENCY,
    CONF_GAS_PRICE,
    CONF_HEATING_DEVICES,
    CONF_HEATING_HOT_WATER_FRACTION,
    CONF_HEATPUMP_ELECTRICITY_ENTITY,
    CONF_HOUSE_FLOOR_AREA_M2,
    CONF_HOUSE_INSULATION,
    CONF_HOUSE_THERMAL_MASS,
    CONF_HOUSE_TYPE,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INTERNAL_GAIN_W,
    CONF_OPTIMIZATION_TIMESTEP_MIN,
    CONF_OUTDOOR_ELECTRIC_LOADS_W,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PREDICTION_HORIZON_HOURS,
    CONF_TEMPERATURE_SCHEDULE,
    CONF_TOTAL_ELECTRICITY_ENTITY,
    CONF_TRAINING_INTERVAL_DAYS,
    CONF_TRAINING_WINDOW_DAYS,
    CONF_WEATHER_ENTITY,
    DEFAULT_AWAY_TEMP,
    DEFAULT_COP_A,
    DEFAULT_COP_B,
    DEFAULT_GAS_EFFICIENCY,
    DEFAULT_GAS_PRICE,
    DEFAULT_HEATING_HOT_WATER_FRACTION,
    DEFAULT_OPTIMIZATION_TIMESTEP_MIN,
    DEFAULT_OUTDOOR_ELECTRIC_LOADS_W,
    DEFAULT_PREDICTION_HORIZON_HOURS,
    DEFAULT_TEMPERATURE_SCHEDULE,
    DEFAULT_TRAINING_INTERVAL_DAYS,
    DEFAULT_TRAINING_WINDOW_DAYS,
    DEVICE_TYPE_ON_OFF,
    DEVICE_TYPE_STEPLESS,
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
    SOURCE_ELECTRIC,
    SOURCE_GAS,
    THERMAL_MASS_HEAVY,
    THERMAL_MASS_LIGHT,
    THERMAL_MASS_MEDIUM,
)
from .house_profile import estimate_initial_params

_LOGGER = logging.getLogger(__name__)


class PredictiveHeatingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow: entities → parameters → devices."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1: Select sensor entities."""
        errors: dict[str, str] = {}
        if user_input is not None:
            for key in (
                CONF_INDOOR_TEMP_ENTITY, CONF_OUTDOOR_TEMP_ENTITY,
                CONF_ELECTRICITY_PRICE_ENTITY,
            ):
                if user_input.get(key) and self.hass.states.get(user_input[key]) is None:
                    errors[key] = "entity_not_found"
            # Validate optional entities only if provided
            for key in (
                CONF_GAS_CONSUMPTION_ENTITY, CONF_TOTAL_ELECTRICITY_ENTITY,
                CONF_HEATPUMP_ELECTRICITY_ENTITY, CONF_WEATHER_ENTITY,
            ):
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
                vol.Optional(CONF_GAS_CONSUMPTION_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")),
                vol.Optional(CONF_TOTAL_ELECTRICITY_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")),
                vol.Optional(CONF_HEATPUMP_ELECTRICITY_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(CONF_ELECTRICITY_PRICE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")),
                vol.Optional(CONF_WEATHER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")),
            }),
            errors=errors,
        )

    async def async_step_house_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: House profile for initial parameter estimation."""
        if user_input is not None:
            # Store house profile in config
            self._data.update(user_input)

            # Compute initial estimates
            ua, thermal_mass, explanation = estimate_initial_params(
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
                    selector.NumberSelectorConfig(
                        min=20, max=500, step=5, unit_of_measurement="m²",
                    ),
                ),
                vol.Required(CONF_HOUSE_TYPE, default=HOUSE_TYPE_SEMI_DETACHED): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(value=HOUSE_TYPE_DETACHED, label="Detached (vrijstaand)"),
                        selector.SelectOptionDict(value=HOUSE_TYPE_SEMI_DETACHED, label="Semi-detached (twee-onder-een-kap)"),
                        selector.SelectOptionDict(value=HOUSE_TYPE_TERRACED, label="Terraced (rijtjeshuis)"),
                        selector.SelectOptionDict(value=HOUSE_TYPE_APARTMENT, label="Apartment (appartement)"),
                    ]),
                ),
                vol.Required(CONF_HOUSE_INSULATION, default=INSULATION_MODERATE): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(value=INSULATION_POOR, label="Poor — pre-1975, energy label E-G"),
                        selector.SelectOptionDict(value=INSULATION_MODERATE, label="Moderate — 1975-2000, label C-D"),
                        selector.SelectOptionDict(value=INSULATION_GOOD, label="Good — post-2000, label A-B"),
                        selector.SelectOptionDict(value=INSULATION_EXCELLENT, label="Excellent — passive house level"),
                    ]),
                ),
                vol.Required(CONF_HOUSE_THERMAL_MASS, default=THERMAL_MASS_MEDIUM): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(value=THERMAL_MASS_LIGHT, label="Light — timber frame, prefab"),
                        selector.SelectOptionDict(value=THERMAL_MASS_MEDIUM, label="Medium — brick cavity walls"),
                        selector.SelectOptionDict(value=THERMAL_MASS_HEAVY, label="Heavy — solid brick, concrete floors"),
                    ]),
                ),
            }),
        )

    async def async_step_parameters(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 2: Model parameters."""
        if user_input is not None:
            # Remove legacy COP string if present (COP is now per-device)
            user_input.pop("cop_coefficients_str", None)
            self._data.update(user_input)
            return await self.async_step_devices()

        # Show gas fields only if gas entity was provided
        has_gas = bool(self._data.get(CONF_GAS_CONSUMPTION_ENTITY))
        has_elec = bool(self._data.get(CONF_TOTAL_ELECTRICITY_ENTITY))
        schema_fields: dict = {}

        if has_gas:
            schema_fields[vol.Optional(CONF_HEATING_HOT_WATER_FRACTION, default=DEFAULT_HEATING_HOT_WATER_FRACTION)] = \
                selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1.0, step=0.05, mode="slider"))
            schema_fields[vol.Optional(CONF_GAS_EFFICIENCY, default=DEFAULT_GAS_EFFICIENCY)] = \
                selector.NumberSelector(selector.NumberSelectorConfig(min=0.5, max=1.0, step=0.05, mode="slider"))
            schema_fields[vol.Optional(CONF_GAS_PRICE, default=DEFAULT_GAS_PRICE)] = \
                selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=5.0, step=0.01, unit_of_measurement="€/m³"))

        if has_elec:
            schema_fields[vol.Optional(CONF_OUTDOOR_ELECTRIC_LOADS_W, default=DEFAULT_OUTDOOR_ELECTRIC_LOADS_W)] = \
                selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=50000, step=100, unit_of_measurement="W"))
        else:
            # No total electricity sensor → ask for a fixed internal gain estimate
            schema_fields[vol.Optional(CONF_INTERNAL_GAIN_W, default=FALLBACK_INTERNAL_GAIN_W)] = \
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=0, max=2000, step=50, unit_of_measurement="W",
                ))

        schema_fields[vol.Optional(CONF_AWAY_TEMP, default=DEFAULT_AWAY_TEMP)] = \
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=5, max=20, step=0.5, unit_of_measurement="°C",
            ))
        schema_fields[vol.Optional(CONF_AUTO_CONTROL, default=False)] = \
            selector.BooleanSelector()

        schema_fields[vol.Optional(CONF_TRAINING_INTERVAL_DAYS, default=DEFAULT_TRAINING_INTERVAL_DAYS)] = \
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=30, step=1))
        schema_fields[vol.Optional(CONF_TRAINING_WINDOW_DAYS, default=DEFAULT_TRAINING_WINDOW_DAYS)] = \
            selector.NumberSelector(selector.NumberSelectorConfig(min=7, max=90, step=1))
        schema_fields[vol.Optional(CONF_PREDICTION_HORIZON_HOURS, default=DEFAULT_PREDICTION_HORIZON_HOURS)] = \
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=48, step=1))
        schema_fields[vol.Optional(CONF_OPTIMIZATION_TIMESTEP_MIN, default=DEFAULT_OPTIMIZATION_TIMESTEP_MIN)] = \
            selector.NumberSelector(selector.NumberSelectorConfig(min=5, max=60, step=5))

        return self.async_show_form(
            step_id="parameters",
            data_schema=vol.Schema(schema_fields),
        )

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 3: Add heating devices."""
        if user_input is not None:
            # Parse COP data string into list of [temp, cop] pairs
            cop_data = []
            cop_str = user_input.pop("cop_data_str", "")
            if cop_str.strip() and user_input.get(CONF_DEVICE_SOURCE) == SOURCE_ELECTRIC:
                for pair in cop_str.split(","):
                    pair = pair.strip()
                    if ":" in pair:
                        parts = pair.split(":")
                        try:
                            cop_data.append([float(parts[0].strip()), float(parts[1].strip())])
                        except (ValueError, IndexError):
                            pass

            self._devices.append({
                CONF_DEVICE_NAME: user_input[CONF_DEVICE_NAME],
                CONF_DEVICE_ENTITY: user_input[CONF_DEVICE_ENTITY],
                CONF_DEVICE_TYPE: user_input[CONF_DEVICE_TYPE],
                CONF_DEVICE_SOURCE: user_input[CONF_DEVICE_SOURCE],
                CONF_DEVICE_MAX_OUTPUT_W: user_input[CONF_DEVICE_MAX_OUTPUT_W],
                CONF_DEVICE_COP_DATA: cop_data,
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
                    selector.EntitySelectorConfig(domain=["climate", "switch", "number"])),
                vol.Required(CONF_DEVICE_TYPE): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(value=DEVICE_TYPE_ON_OFF, label="On/Off"),
                        selector.SelectOptionDict(value=DEVICE_TYPE_STEPLESS, label="Stepless (Modulating)"),
                    ])),
                vol.Required(CONF_DEVICE_SOURCE): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(value=SOURCE_GAS, label="Gas"),
                        selector.SelectOptionDict(value=SOURCE_ELECTRIC, label="Electric"),
                    ])),
                vol.Required(CONF_DEVICE_MAX_OUTPUT_W, default=10000):
                    selector.NumberSelector(selector.NumberSelectorConfig(min=100, max=100000, step=100, unit_of_measurement="W")),
                vol.Optional("cop_data_str", default="-15:2.0, -7:2.5, 2:3.2, 7:4.0, 12:4.8"):
                    selector.TextSelector(selector.TextSelectorConfig(
                        multiline=False,
                    )),
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
        has_gas = bool(current.get(CONF_GAS_CONSUMPTION_ENTITY))

        schema_fields: dict = {}
        if has_gas:
            schema_fields[vol.Optional(CONF_HEATING_HOT_WATER_FRACTION,
                default=current.get(CONF_HEATING_HOT_WATER_FRACTION, DEFAULT_HEATING_HOT_WATER_FRACTION))] = \
                selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=1.0, step=0.05, mode="slider"))
            schema_fields[vol.Optional(CONF_GAS_EFFICIENCY,
                default=current.get(CONF_GAS_EFFICIENCY, DEFAULT_GAS_EFFICIENCY))] = \
                selector.NumberSelector(selector.NumberSelectorConfig(min=0.5, max=1.0, step=0.05, mode="slider"))
            schema_fields[vol.Optional(CONF_GAS_PRICE,
                default=current.get(CONF_GAS_PRICE, DEFAULT_GAS_PRICE))] = \
                selector.NumberSelector(selector.NumberSelectorConfig(min=0.0, max=5.0, step=0.01, unit_of_measurement="€/m³"))

        schema_fields[vol.Optional(CONF_OUTDOOR_ELECTRIC_LOADS_W,
            default=current.get(CONF_OUTDOOR_ELECTRIC_LOADS_W, DEFAULT_OUTDOOR_ELECTRIC_LOADS_W))] = \
            selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=50000, step=100, unit_of_measurement="W"))
        schema_fields[vol.Optional(CONF_AWAY_TEMP,
            default=current.get(CONF_AWAY_TEMP, DEFAULT_AWAY_TEMP))] = \
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=5, max=20, step=0.5, unit_of_measurement="°C",
            ))
        schema_fields[vol.Optional(CONF_AUTO_CONTROL,
            default=current.get(CONF_AUTO_CONTROL, False))] = \
            selector.BooleanSelector()
        schema_fields[vol.Optional(CONF_TRAINING_INTERVAL_DAYS,
            default=current.get(CONF_TRAINING_INTERVAL_DAYS, DEFAULT_TRAINING_INTERVAL_DAYS))] = \
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=30, step=1))
        schema_fields[vol.Optional(CONF_PREDICTION_HORIZON_HOURS,
            default=current.get(CONF_PREDICTION_HORIZON_HOURS, DEFAULT_PREDICTION_HORIZON_HOURS))] = \
            selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=48, step=1))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
        )
