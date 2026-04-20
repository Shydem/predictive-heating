"""
Climate platform for Predictive Heating.

Creates a virtual climate entity per room that:
- Wraps an underlying TRV / climate entity (like Better Thermostat does)
- Uses an external temperature sensor for accurate readings
- Feeds observations into the thermal model
- Runs the controller to decide heating actions
- Coordinates with other rooms in the same heating zone
- Uses proportional setpoints to prevent overshoot

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
    CONF_AWAY_GRACE_MIN,
    CONF_BOILER_EFFICIENCY,
    CONF_CLIMATE_ENTITY,
    CONF_COMFORT_RAMP,
    CONF_GAS_CALORIFIC_VALUE,
    CONF_GAS_METER_SENSOR,
    CONF_HEAT_SHARE,
    CONF_MPC_CONTROL_DELAY_MIN,
    CONF_MPC_ENABLED,
    CONF_MPC_HORIZON_MIN,
    CONF_MPC_STEP_MIN,
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
    DEFAULT_AWAY_TEMP,
    DEFAULT_BOILER_EFFICIENCY,
    DEFAULT_COMFORT_RAMP,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DEFAULT_HEAT_SHARE,
    DEFAULT_MPC_CONTROL_DELAY_MIN,
    DEFAULT_MPC_ENABLED,
    DEFAULT_MPC_HORIZON_MIN,
    DEFAULT_MPC_STEP_MIN,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .controller import HeatingAction, HeatingController, PresetMode
from .heat_source import GasHeatSource
from .mpc import MPCConfig
from .preheat import PreheatConfig, PreheatPlan, PreheatPlanner
from .presence import PresenceConfig, PresenceMonitor
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

        # Fetch options up-front — several blocks below rely on options
        # winning over the initial config entry data, so users can tweak
        # window sensors, gas meter, schedule etc. without removing the
        # room. The ordering here matters: referencing ``options`` before
        # this line raises NameError and the climate entity silently
        # fails to register.
        options = entry.options

        # ── v0.3: MPC, pre-heat, presence ─────────────────────────
        mpc_enabled = bool(options.get(CONF_MPC_ENABLED, DEFAULT_MPC_ENABLED))
        mpc_config = MPCConfig(
            horizon_min=float(options.get(
                CONF_MPC_HORIZON_MIN, DEFAULT_MPC_HORIZON_MIN
            )),
            step_min=float(options.get(CONF_MPC_STEP_MIN, DEFAULT_MPC_STEP_MIN)),
            control_delay_min=float(options.get(
                CONF_MPC_CONTROL_DELAY_MIN, DEFAULT_MPC_CONTROL_DELAY_MIN
            )),
        )
        self._controller = HeatingController(
            model,
            mpc_enabled=mpc_enabled,
            mpc_config=mpc_config,
        )
        self._preheat_planner = PreheatPlanner(
            model,
            PreheatConfig(
                comfort_ramp=options.get(CONF_COMFORT_RAMP, DEFAULT_COMFORT_RAMP),
            ),
        )
        self._last_preheat_plan: PreheatPlan | None = None
        self._weather_entity_id: str | None = (
            options.get(CONF_WEATHER_ENTITY)
            or config.get(CONF_WEATHER_ENTITY)
        )
        self._forecast_hourly: list[float] = []
        person_entities = (
            options.get(CONF_PERSON_ENTITIES)
            or config.get(CONF_PERSON_ENTITIES)
            or []
        )
        self._presence = PresenceMonitor(
            person_entities,
            PresenceConfig(
                away_grace_min=float(options.get(
                    CONF_AWAY_GRACE_MIN, DEFAULT_AWAY_GRACE_MIN
                )),
            ),
        )

        self._room_name = config[CONF_ROOM_NAME]
        self._temp_sensor_id = config[CONF_TEMPERATURE_SENSOR]
        self._climate_entity_id = config[CONF_CLIMATE_ENTITY]
        self._outdoor_sensor_id = config.get(CONF_OUTDOOR_TEMPERATURE_SENSOR)
        # Options win over initial data so users can add/change window sensors
        # retroactively via the Options dialog without removing the room.
        self._window_sensor_ids = (
            options.get(CONF_WINDOW_SENSORS)
            or config.get(CONF_WINDOW_SENSORS)
            or []
        )

        # Gas / heat-source (options win over data, so users can reconfigure
        # without re-adding the room).
        self._gas_sensor_id = (
            options.get(CONF_GAS_METER_SENSOR) or config.get(CONF_GAS_METER_SENSOR)
        )
        self._heat_source: GasHeatSource | None = None
        if self._gas_sensor_id:
            self._heat_source = GasHeatSource(
                calorific_value_mj_m3=(
                    options.get(CONF_GAS_CALORIFIC_VALUE)
                    or config.get(CONF_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE)
                ),
                efficiency=(
                    options.get(CONF_BOILER_EFFICIENCY)
                    or config.get(CONF_BOILER_EFFICIENCY, DEFAULT_BOILER_EFFICIENCY)
                ),
                heat_share=(
                    options.get(CONF_HEAT_SHARE)
                    or config.get(CONF_HEAT_SHARE, DEFAULT_HEAT_SHARE)
                ),
            )
            # Restore last reading state from the model, if persisted.
            saved = getattr(model, "_heat_source_state", None)
            if saved:
                self._heat_source = GasHeatSource.from_dict(saved)

        # State
        self._hvac_mode = HVACMode.HEAT
        self._current_temp: float | None = None
        self._outdoor_temp: float | None = None
        self._target_temp = DEFAULT_COMFORT_TEMP
        self._preset_mode = PresetMode.COMFORT
        self._hvac_action = HVACAction.IDLE
        self._window_open = False
        self._wants_heat = False

        # Schedule-entity integration (optional).
        # The user can point us at a `schedule.*` entity and we follow
        # its on/off state. On ON → schedule_on_temp (default comfort),
        # on OFF → schedule_off_temp (default eco). If the schedule
        # entity exposes a `temperature` attribute (newer HA versions),
        # that wins. Leaves the user's manual setpoint alone when the
        # schedule isn't configured.
        self._schedule_entity_id: str | None = (
            options.get(CONF_SCHEDULE_ENTITY)
            or config.get(CONF_SCHEDULE_ENTITY)
        )
        self._schedule_on_temp = float(
            options.get(
                CONF_SCHEDULE_ON_TEMP,
                options.get("comfort_temp", DEFAULT_COMFORT_TEMP),
            )
        )
        self._schedule_off_temp = float(
            options.get(
                CONF_SCHEDULE_OFF_TEMP,
                options.get("eco_temp", DEFAULT_ECO_TEMP),
            )
        )
        self._schedule_active_state: str | None = None
        self._schedule_override_temp: float | None = None

        # Entity attributes
        self._attr_unique_id = f"predictive_heating_{entry.entry_id}"
        self._attr_name = f"Predictive {self._room_name}"

        # Apply preset-temperature overrides from options
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
        # Make our entity_id discoverable by the dashboard WebSocket API,
        # so it doesn't have to guess based on the (renameable) room name.
        domain_data = self.hass.data.setdefault(DOMAIN, {}).get(
            self._entry.entry_id
        )
        if domain_data is not None:
            domain_data["climate_entity_id"] = self.entity_id

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

        # Track gas meter so we can derive heat input in W
        if self._gas_sensor_id and self._heat_source is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._gas_sensor_id],
                    self._async_gas_changed,
                )
            )

        # Track schedule entity so target temp auto-follows it.
        if self._schedule_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._schedule_entity_id],
                    self._async_schedule_changed,
                )
            )

        # Track weather entity (for outdoor forecast used in pre-heat planning)
        if self._weather_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._weather_entity_id],
                    self._async_weather_changed,
                )
            )

        # Track person entities for presence-based Away switching
        if self._presence.enabled:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    list(self._presence.person_entity_ids),
                    self._async_presence_changed,
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

        # Apply the schedule's current state if configured.
        if self._schedule_entity_id:
            self._apply_schedule_state(self.hass.states.get(self._schedule_entity_id))

        # Prime the weather forecast cache.
        if self._weather_entity_id:
            self._refresh_weather_forecast(
                self.hass.states.get(self._weather_entity_id)
            )

        # Prime the presence state so we start in Away if everyone's gone.
        if self._presence.enabled:
            self._evaluate_presence()

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
    def _async_gas_changed(self, event) -> None:
        """Handle new cumulative gas-meter reading → update heat source."""
        if self._heat_source is None:
            return
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return
        try:
            m3 = float(new_state.state)
        except (ValueError, TypeError):
            return

        # The last_changed field is our best estimate of when this
        # reading was taken — more precise than time.time() when the
        # meter reports only on unit ticks.
        ts = (
            new_state.last_changed.timestamp()
            if new_state.last_changed is not None
            else time.time()
        )
        self._heat_source.update_reading(m3, timestamp=ts)

    @callback
    def _async_schedule_changed(self, event) -> None:
        """Schedule entity changed state → re-apply target temp."""
        new_state = event.data.get("new_state")
        self._apply_schedule_state(new_state)
        # Re-run the control loop now that target may have shifted.
        self._run_control_loop()
        self.async_write_ha_state()

    def _apply_schedule_state(self, state) -> None:
        """
        Update the cached schedule state + delegate target selection
        to the preheat planner.

        When the schedule exposes a per-slot ``temperature`` attribute
        (newer HA versions), we treat that as the "high" target and
        keep the user-configured off-temp as the fallback "low" target.
        """
        if state is None:
            self._schedule_active_state = None
            self._schedule_override_temp = None
            self._apply_preheat_plan()
            return

        s = state.state
        self._schedule_active_state = s

        override = state.attributes.get("temperature")
        try:
            override_val = float(override) if override is not None else None
        except (TypeError, ValueError):
            override_val = None
        self._schedule_override_temp = override_val

        self._apply_preheat_plan()

    # ── v0.3 helpers: preheat, weather, presence ─────────────────

    def _schedule_is_on(self) -> bool:
        return self._schedule_active_state == "on"

    def _schedule_targets(self) -> tuple[float, float]:
        """Return (low_target, high_target) for the current schedule."""
        high = (
            self._schedule_override_temp
            if self._schedule_override_temp is not None
            else self._schedule_on_temp
        )
        low = self._schedule_off_temp
        return low, high

    def _schedule_next_transition_ts(self) -> float | None:
        """Best-effort unix timestamp of the next schedule on/off flip."""
        if not self._schedule_entity_id:
            return None
        state = self.hass.states.get(self._schedule_entity_id)
        if state is None:
            return None

        # HA exposes next_event on schedule entities as a datetime string.
        # Parse defensively — formats vary across versions.
        for attr_name in ("next_event", "next_toggle", "next"):
            raw = state.attributes.get(attr_name)
            if raw is None:
                continue
            ts = self._parse_timestamp(raw)
            if ts is not None:
                return ts
        return None

    @staticmethod
    def _parse_timestamp(value) -> float | None:
        """Convert various timestamp shapes to a unix ts float."""
        from datetime import datetime

        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if hasattr(value, "timestamp"):
            try:
                return float(value.timestamp())
            except (TypeError, ValueError):
                return None
        if isinstance(value, str):
            try:
                # Python 3.11+ parses "Z" natively; older: replace with +00:00.
                v = value.replace("Z", "+00:00")
                return datetime.fromisoformat(v).timestamp()
            except (TypeError, ValueError):
                return None
        return None

    def _apply_preheat_plan(self) -> None:
        """Run the preheat planner and push the effective target to the controller."""
        low, high = self._schedule_targets()
        if self._current_temp is None:
            # Nothing sensible to plan without a reading — fall back to low.
            self._target_temp = low
            self._controller.set_target_temp(low)
            return

        outdoor = self._outdoor_temp if self._outdoor_temp is not None else 10.0
        plan = self._preheat_planner.plan(
            now_ts=time.time(),
            t_indoor=self._current_temp,
            t_outdoor=outdoor,
            low_target=low,
            high_target=high,
            schedule_on=self._schedule_is_on(),
            next_transition_ts=self._schedule_next_transition_ts(),
            forecast_hourly=self._forecast_hourly,
            solar_irradiance=estimate_solar_irradiance(self.hass),
        )
        self._last_preheat_plan = plan

        new_target = plan.effective_target_temp
        if abs(new_target - self._target_temp) > 0.01:
            self._target_temp = new_target
            self._controller.set_target_temp(new_target)

    @callback
    def _async_weather_changed(self, event) -> None:
        """Weather entity updated → refresh the forecast cache."""
        self._refresh_weather_forecast(event.data.get("new_state"))

    def _refresh_weather_forecast(self, state) -> None:
        """Extract an hourly temperature list from a weather entity state."""
        if state is None:
            self._forecast_hourly = []
            return
        # HA's weather entities expose `forecast` as a list of dicts
        # with a "temperature" key. Some integrations return daily
        # forecasts; we just read the first N entries either way.
        forecast = state.attributes.get("forecast") or []
        temps: list[float] = []
        for entry in forecast[:24]:
            t = entry.get("temperature") or entry.get("native_temperature")
            if t is None:
                continue
            try:
                temps.append(float(t))
            except (TypeError, ValueError):
                continue
        self._forecast_hourly = temps

    @callback
    def _async_presence_changed(self, event) -> None:
        """Person entity state changed → re-evaluate presence."""
        self._evaluate_presence()

    def _evaluate_presence(self) -> None:
        """
        Feed the monitor the latest person-entity states and react.

        We only change the preset here; the target temp is recomputed
        by ``set_preset_mode`` → ``_run_control_loop``.
        """
        if not self._presence.enabled:
            return
        states = {
            eid: (self.hass.states.get(eid).state
                  if self.hass.states.get(eid) is not None else None)
            for eid in self._presence.person_entity_ids
        }
        decision = self._presence.update(states)
        if decision == "away":
            # Remember the currently-active preset so we can restore it.
            self._presence.remember_preset(str(self._preset_mode))
            self.hass.async_create_task(
                self.async_set_preset_mode(PresetMode.AWAY.value)
            )
        elif decision == "home":
            previous = self._presence.saved_preset_or(PresetMode.COMFORT.value)
            self.hass.async_create_task(self.async_set_preset_mode(previous))

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

        # If a gas meter is configured, use its derivative for the actual
        # thermal watts delivered since the last sample — much richer
        # than the old binary on/off.
        measured_heat_w: float | None = None
        if self._heat_source is not None:
            measured_heat_w = self._heat_source.current_power_w()

        # Feed observation to thermal model (EKF learns from this)
        obs = ThermalObservation(
            timestamp=time.time(),
            t_indoor=self._current_temp,
            t_outdoor=outdoor,
            heating_on=actually_heating,
            solar_irradiance=solar,
            heat_power_w=measured_heat_w,
        )
        self._model.add_observation(obs)

        # v0.3: re-evaluate presence (grace-period may have elapsed) and
        # roll the pre-heat plan forward each cycle.
        if self._presence.enabled:
            self._evaluate_presence()
        if self._schedule_entity_id:
            self._apply_preheat_plan()

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

        # The zone decides the actual setpoint for the shared thermostat.
        # calculate_setpoint() returns None when no change is due — we
        # intentionally DO NOT send a command in that case, so the
        # thermostat's OpenTherm modulation is left alone to work.
        if self._zone.any_room_wants_heat:
            setpoint = self._zone.calculate_setpoint()
            if setpoint is not None:
                self.hass.async_create_task(
                    self._async_set_underlying_temp(setpoint)
                )
        else:
            # No room wants heat — idle the thermostat by asking for a
            # low setpoint. Only do it once (when we first idle) so we
            # don't spam the boiler.
            if self._zone._last_setpoint is None or self._zone._last_setpoint > 10.0:
                idle_setpoint = 5.0
                self.hass.async_create_task(
                    self._async_set_underlying_temp(idle_setpoint)
                )
                self._zone._last_setpoint = idle_setpoint

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
            "heat_loss_coefficient": (
                round(self._model.params.heat_loss_coeff, 1)
                if self._model.params.heat_loss_coeff is not None else None
            ),
            "thermal_mass_kj": (
                round(self._model.params.thermal_mass, 0)
                if self._model.params.thermal_mass is not None else None
            ),
            "heating_power": (
                round(self._model.params.heating_power, 0)
                if self._model.params.heating_power is not None else None
            ),
            "solar_gain_factor": (
                round(self._model.params.solar_gain_factor, 3)
                if self._model.params.solar_gain_factor is not None else None
            ),
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

        # Leading room in zone
        leader = self._zone.leading_room
        if leader:
            attrs["zone_leading_room"] = leader.room_name

        # Current solar irradiance
        solar = estimate_solar_irradiance(self.hass)
        if solar > 0:
            attrs["solar_irradiance"] = round(solar, 1)

        # Gas-based heat input diagnostics
        if self._heat_source is not None:
            attrs["gas_meter_sensor"] = self._gas_sensor_id
            attrs["heat_power_w"] = round(self._heat_source.current_power_w(), 1)
            attrs["boiler_efficiency"] = self._heat_source.efficiency
            attrs["heat_share"] = self._heat_source.heat_share
            attrs["gas_calorific_value_mj_m3"] = self._heat_source.calorific_value_mj_m3

        # Schedule diagnostics
        if self._schedule_entity_id:
            attrs["schedule_entity"] = self._schedule_entity_id
            attrs["schedule_state"] = self._schedule_active_state
            attrs["schedule_on_temp"] = self._schedule_on_temp
            attrs["schedule_off_temp"] = self._schedule_off_temp
            attrs["schedule_override_temp"] = self._schedule_override_temp

        # Window detection summary
        if self._window_sensor_ids:
            attrs["window_sensors"] = list(self._window_sensor_ids)
            attrs["window_open"] = self._window_open

        # v0.3: predictive pre-heat diagnostics
        if self._last_preheat_plan is not None:
            attrs["preheat"] = self._last_preheat_plan.as_diagnostic()

        # v0.3: MPC diagnostics
        attrs["control_mode"] = self._controller.state.mode_used
        mpc_result = self._controller.state.last_mpc_result
        if mpc_result is not None:
            attrs["mpc"] = {
                "action": mpc_result.action,
                "reason": mpc_result.reason,
                "cost": round(mpc_result.cost, 4),
                "switch_at_step": mpc_result.switch_at_step,
                "hysteresis_would_do": mpc_result.hysteresis_action,
                # Only expose a short trajectory preview so attrs stay small.
                "trajectory_preview": [
                    round(t, 2) for t in mpc_result.predicted_trajectory[:6]
                ],
            }

        # v0.3: presence diagnostics
        if self._presence.enabled:
            attrs["presence"] = {
                "person_entities": list(self._presence.person_entity_ids),
                "currently_away": self._presence.state.currently_away,
                "saved_preset": self._presence.state.saved_preset,
            }

        if self._weather_entity_id and self._forecast_hourly:
            attrs["weather_forecast_hours"] = self._forecast_hourly[:6]

        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        prev_mode = self._hvac_mode
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
                self._zone.reset_setpoint_tracking()
        elif prev_mode == HVACMode.OFF:
            # Coming back online: drop any stale nudge history so the
            # next cycle starts at the room target.
            self._zone.reset_setpoint_tracking()
            self._run_control_loop()
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
