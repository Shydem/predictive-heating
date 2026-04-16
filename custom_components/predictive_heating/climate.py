"""
Climate platform for Predictive Heating.

Creates a virtual climate entity per room that:
- Wraps an underlying TRV / climate entity (like Better Thermostat does)
- Uses an external temperature sensor for accurate readings
- Feeds observations into the thermal model
- Runs the controller to decide heating actions
- Coordinates with other rooms in the same heating zone
- Uses proportional setpoints to prevent overshoot
- Supports OpenTherm flow temperature modulation

Zone-aware behavior:
    When woonkamer and slaapkamer share the same thermostat, they form
    a zone. If slaapkamer requests heat, woonkamer also sees "heating"
    because the same boiler/circuit is running. The zone calculates a
    single proportional setpoint from the room that needs the most heat.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_OPENTHERM_ENABLED,
    CONF_OPENTHERM_FLOW_TEMP_NUMBER,
    CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_ROOM_NAME,
    CONF_TEMPERATURE_SENSOR,
    CONF_WINDOW_SENSORS,
    DEFAULT_COMFORT_TEMP,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .controller import HeatingAction, HeatingController, PresetMode
from .solar import estimate_solar_irradiance
from .thermal_model import ThermalModel, ThermalObservation
from .zone import HeatingZone

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up climate entity from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    model: ThermalModel = data["model"]
    config: dict = data["config"]
    zone: HeatingZone = data["zone"]

    entity = PredictiveHeatingClimate(
        hass=hass,
        entry=entry,
        model=model,
        config=config,
        zone=zone,
    )

    async_add_entities([entity])


class PredictiveHeatingClimate(ClimateEntity):
    """A smart climate entity that learns, predicts, and coordinates zones."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = [
        PresetMode.COMFORT,
        PresetMode.ECO,
        PresetMode.AWAY,
        PresetMode.SLEEP,
        PresetMode.BOOST,
    ]
    _attr_min_temp = 5.0
    _attr_max_temp = 30.0
    _attr_target_temperature_step = 0.5

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        model: ThermalModel,
        config: dict,
        zone: HeatingZone,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._model = model
        self._config = config
        self._zone = zone
        self._controller = HeatingController(model)

        self._room_name = config[CONF_ROOM_NAME]
        self._temp_sensor_id = config[CONF_TEMPERATURE_SENSOR]
        self._climate_entity_id = config[CONF_CLIMATE_ENTITY]
        self._outdoor_sensor_id = config.get(CONF_OUTDOOR_TEMPERATURE_SENSOR)
        self._window_sensor_ids = config.get(CONF_WINDOW_SENSORS, [])
        self._opentherm_enabled = config.get(CONF_OPENTHERM_ENABLED, False)
        self._opentherm_flow_entity = config.get(CONF_OPENTHERM_FLOW_TEMP_NUMBER)

        # State
        self._hvac_mode = HVACMode.HEAT
        self._current_temp: float | None = None
        self._outdoor_temp: float | None = None
        self._target_temp = DEFAULT_COMFORT_TEMP
        self._preset_mode = PresetMode.COMFORT
        self._hvac_action = HVACAction.IDLE
        self._window_open = False
        self._wants_heat = False

        # Entity attributes
        self._attr_unique_id = f"predictive_heating_{entry.entry_id}"
        self._attr_name = f"Predictive {self._room_name}"

        # Apply options overrides
        options = entry.options
        if "comfort_temp" in options:
            self._controller.preset_temps[PresetMode.COMFORT] = options["comfort_temp"]
        if "eco_temp" in options:
            self._controller.preset_temps[PresetMode.ECO] = options["eco_temp"]
        if "away_temp" in options:
            self._controller.preset_temps[PresetMode.AWAY] = options["away_temp"]
        if "sleep_temp" in options:
            self._controller.preset_temps[PresetMode.SLEEP] = options["sleep_temp"]

        self._controller.set_preset(PresetMode.COMFORT)
        self._target_temp = self._controller.state.target_temp

    async def async_added_to_hass(self) -> None:
        """Subscribe to sensor updates when added to HA."""
        # Track temperature sensor
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._temp_sensor_id],
                self._async_temp_changed,
            )
        )

        # Track outdoor temperature
        if self._outdoor_sensor_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._outdoor_sensor_id],
                    self._async_outdoor_temp_changed,
                )
            )

        # Track window sensors
        if self._window_sensor_ids:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    self._window_sensor_ids,
                    self._async_window_changed,
                )
            )

        # Track underlying thermostat state (for zone heating detection)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._climate_entity_id],
                self._async_underlying_changed,
            )
        )

        # Periodic model update
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_periodic_update,
                timedelta(seconds=UPDATE_INTERVAL),
            )
        )

        # Read initial states
        self._read_current_state()

    def _read_current_state(self) -> None:
        """Read current sensor values."""
        state = self.hass.states.get(self._temp_sensor_id)
        if state and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            try:
                self._current_temp = float(state.state)
            except ValueError:
                pass

        if self._outdoor_sensor_id:
            state = self.hass.states.get(self._outdoor_sensor_id)
            if state and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                try:
                    self._outdoor_temp = float(state.state)
                except ValueError:
                    pass

        # Check if the underlying thermostat is currently heating
        self._update_zone_heating_state()

    @callback
    def _async_temp_changed(self, event) -> None:
        """Handle indoor temperature sensor update."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return

        try:
            self._current_temp = float(new_state.state)
        except ValueError:
            return

        self._run_control_loop()
        self.async_write_ha_state()

    @callback
    def _async_outdoor_temp_changed(self, event) -> None:
        """Handle outdoor temperature sensor update."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return

        try:
            self._outdoor_temp = float(new_state.state)
        except ValueError:
            return

    @callback
    def _async_window_changed(self, event) -> None:
        """Handle window sensor update."""
        any_open = False
        for sensor_id in self._window_sensor_ids:
            state = self.hass.states.get(sensor_id)
            if state and state.state == STATE_ON:
                any_open = True
                break

        self._window_open = any_open
        self._controller.set_window_open(any_open)
        self._run_control_loop()
        self.async_write_ha_state()

    @callback
    def _async_underlying_changed(self, event) -> None:
        """
        Handle state changes on the underlying thermostat.

        This is KEY for the zone bug fix: when the shared thermostat
        starts heating (because slaapkamer triggered it), we detect
        that here and update the zone heating state so woonkamer also
        shows "heating".
        """
        self._update_zone_heating_state()
        self._update_hvac_action_from_zone()
        self.async_write_ha_state()

    def _update_zone_heating_state(self) -> None:
        """Read the underlying thermostat's actual heating state and update the zone."""
        state = self.hass.states.get(self._climate_entity_id)
        if state is None:
            return

        # Check hvac_action attribute (most reliable)
        underlying_action = state.attributes.get("hvac_action", "")
        is_heating = underlying_action in ("heating", "preheating")

        # Fallback: check if hvac_mode is heat and current_temp < target
        if not is_heating and state.state == "heat":
            underlying_target = state.attributes.get("temperature")
            underlying_current = state.attributes.get("current_temperature")
            if underlying_target and underlying_current:
                try:
                    is_heating = float(underlying_current) < float(underlying_target) - 0.2
                except (ValueError, TypeError):
                    pass

        self._zone.is_heating = is_heating

    def _update_hvac_action_from_zone(self) -> None:
        """
        Set this room's HVAC action based on zone state.

        If the zone is heating (thermostat is firing), ALL rooms in the
        zone show "heating" — because the boiler is running and all
        radiators on that circuit are getting hot water.
        """
        if self._hvac_mode == HVACMode.OFF:
            self._hvac_action = HVACAction.OFF
            return

        if self._zone.is_heating:
            self._hvac_action = HVACAction.HEATING
        elif self._wants_heat:
            # We want heat but thermostat hasn't started yet
            self._hvac_action = HVACAction.HEATING
        else:
            self._hvac_action = HVACAction.IDLE

    @callback
    def _async_periodic_update(self, now=None) -> None:
        """Periodic update: feed the thermal model and run controller."""
        if self._current_temp is None:
            return

        outdoor = self._outdoor_temp if self._outdoor_temp is not None else 10.0

        # Estimate solar irradiance from sun position + weather
        solar = estimate_solar_irradiance(self.hass)

        # Determine actual heating state from zone (not just our request)
        self._update_zone_heating_state()
        actually_heating = self._zone.is_heating

        # Feed observation to thermal model (EKF learns from this)
        obs = ThermalObservation(
            timestamp=time.time(),
            t_indoor=self._current_temp,
            t_outdoor=outdoor,
            heating_on=actually_heating,
            solar_irradiance=solar,
        )
        self._model.add_observation(obs)

        self._run_control_loop()
        self.async_write_ha_state()

    def _run_control_loop(self) -> None:
        """
        Run the controller and coordinate with the zone.

        Instead of each room blindly sending setpoints to the thermostat,
        rooms report their demand to the zone, and the zone calculates
        a single proportional setpoint.
        """
        if self._current_temp is None:
            return
        if self._hvac_mode == HVACMode.OFF:
            self._hvac_action = HVACAction.OFF
            self._wants_heat = False
            self._zone.update_room_demand(
                entry_id=self._entry.entry_id,
                current_temp=self._current_temp,
                target_temp=self._target_temp,
                wants_heat=False,
                window_open=self._window_open,
            )
            return

        outdoor = self._outdoor_temp if self._outdoor_temp is not None else 10.0

        # Ask the controller if THIS room wants heat
        action = self._controller.update(
            t_indoor=self._current_temp,
            t_outdoor=outdoor,
        )

        self._wants_heat = action == HeatingAction.HEAT

        # Report our demand to the zone
        self._zone.update_room_demand(
            entry_id=self._entry.entry_id,
            current_temp=self._current_temp,
            target_temp=self._target_temp,
            wants_heat=self._wants_heat,
            window_open=self._window_open,
        )

        # The zone decides the actual setpoint for the shared thermostat
        if self._zone.any_room_wants_heat:
            setpoint = self._zone.calculate_setpoint()
            if setpoint is not None:
                self.hass.async_create_task(
                    self._async_set_underlying_temp(setpoint)
                )
                self._zone._last_setpoint = setpoint

            # OpenTherm flow temperature control
            if self._zone.opentherm_enabled:
                flow_temp = self._zone.calculate_flow_temperature(
                    t_outdoor=self._outdoor_temp
                )
                if flow_temp is not None:
                    self.hass.async_create_task(
                        self._async_set_flow_temperature(flow_temp)
                    )
                    self._zone._last_flow_temp = flow_temp
        else:
            # No room wants heat — tell the thermostat to idle
            # Use a setpoint just below the current temp so it stops
            min_temp_in_zone = self._current_temp
            idle_setpoint = max(5.0, min_temp_in_zone - 1.0)
            self.hass.async_create_task(
                self._async_set_underlying_temp(idle_setpoint)
            )
            self._zone._last_setpoint = idle_setpoint

            # Lower flow temp when idling
            if self._zone.opentherm_enabled:
                from .const import DEFAULT_MIN_FLOW_TEMP
                self.hass.async_create_task(
                    self._async_set_flow_temperature(DEFAULT_MIN_FLOW_TEMP)
                )
                self._zone._last_flow_temp = DEFAULT_MIN_FLOW_TEMP

        # Update our HVAC action based on what the zone is actually doing
        self._update_hvac_action_from_zone()

    async def _async_set_underlying_temp(self, temperature: float) -> None:
        """Forward a temperature setpoint to the underlying climate entity."""
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {
                "entity_id": self._climate_entity_id,
                ATTR_TEMPERATURE: temperature,
            },
        )

    async def _async_set_flow_temperature(self, flow_temp: float) -> None:
        """
        Set the OpenTherm flow/boiler temperature.

        This works with the OpenTherm Gateway integration or similar
        integrations that expose a number entity for flow temperature.
        """
        flow_entity = self._zone.opentherm_flow_temp_entity
        if not flow_entity:
            return

        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": flow_entity,
                    "value": flow_temp,
                },
            )
            _LOGGER.debug(
                "Set OpenTherm flow temperature to %.1f°C via %s",
                flow_temp,
                flow_entity,
            )
        except Exception as err:
            _LOGGER.warning(
                "Failed to set flow temperature on %s: %s",
                flow_entity,
                err,
            )

    # --- ClimateEntity interface ---

    @property
    def current_temperature(self) -> float | None:
        return self._current_temp

    @property
    def target_temperature(self) -> float:
        return self._target_temp

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        return self._hvac_action

    @property
    def preset_mode(self) -> str:
        return self._preset_mode

    @property
    def extra_state_attributes(self) -> dict:
        """Expose thermal model and zone state as attributes."""
        attrs = {
            "thermal_model_state": self._model.state,
            "heat_loss_coefficient": round(self._model.params.heat_loss_coeff, 1),
            "thermal_mass_kj": round(self._model.params.thermal_mass, 0),
            "heating_power": round(self._model.params.heating_power, 0),
            "solar_gain_factor": round(self._model.params.solar_gain_factor, 3),
            "idle_samples": self._model.idle_count,
            "active_samples": self._model.active_count,
            "total_updates": self._model.total_updates,
            "mean_prediction_error": (
                round(self._model.mean_prediction_error, 3)
                if self._model.mean_prediction_error != float("inf")
                else None
            ),
            # Zone info
            "heating_zone": self._zone.zone_id,
            "zone_rooms": self._zone.room_names,
            "zone_is_heating": self._zone.is_heating,
            "zone_setpoint": self._zone._last_setpoint,
            "this_room_wants_heat": self._wants_heat,
        }

        if self._outdoor_temp is not None:
            attrs["outdoor_temperature"] = self._outdoor_temp

        # OpenTherm info
        if self._zone.opentherm_enabled:
            attrs["opentherm_enabled"] = True
            attrs["flow_temperature"] = self._zone._last_flow_temp

        # Leading room in zone
        leader = self._zone.leading_room
        if leader:
            attrs["zone_leading_room"] = leader.room_name

        # Current solar irradiance
        solar = estimate_solar_irradiance(self.hass)
        if solar > 0:
            attrs["solar_irradiance"] = round(solar, 1)

        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        self._hvac_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            self._hvac_action = HVACAction.OFF
            self._wants_heat = False
            self._zone.update_room_demand(
                entry_id=self._entry.entry_id,
                current_temp=self._current_temp,
                target_temp=self._target_temp,
                wants_heat=False,
            )
            # Only idle the thermostat if no other room in the zone wants heat
            if not self._zone.any_room_wants_heat:
                await self._async_set_underlying_temp(5.0)
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs) -> None:
        """Set target temperature."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            self._target_temp = temp
            self._controller.set_target_temp(temp)
            self._preset_mode = PresetMode.NONE
            self._run_control_loop()
            self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set a preset mode."""
        try:
            preset = PresetMode(preset_mode)
        except ValueError:
            _LOGGER.warning("Unknown preset mode: %s", preset_mode)
            return

        self._preset_mode = preset
        self._controller.set_preset(preset)
        self._target_temp = self._controller.state.target_temp
        self._run_control_loop()
        self.async_write_ha_state()
