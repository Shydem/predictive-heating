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
    CONF_OCCUPANCY_ENTITY,
    CONF_OUTDOOR_TEMPERATURE_SENSOR,
    CONF_OVERRIDE_ENTITY,
    CONF_PERSON_ENTITIES,
    CONF_ROOM_NAME,
    CONF_SCHEDULE_ENTITY,
    CONF_SCHEDULE_OFF_PRESET,
    CONF_SCHEDULE_OFF_TEMP,
    CONF_SCHEDULE_ON_PRESET,
    CONF_SCHEDULE_ON_TEMP,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMAL_COUPLINGS,
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
    DEFAULT_IDLE_MIN_TEMP,
    DEFAULT_MPC_CONTROL_DELAY_MIN,
    DEFAULT_MPC_ENABLED,
    DEFAULT_MPC_HORIZON_MIN,
    DEFAULT_MPC_STEP_MIN,
    DEFAULT_SCHEDULE_OFF_PRESET,
    DEFAULT_SCHEDULE_ON_PRESET,
    DEFAULT_WINDOW_OPEN_TEMP,
    DOMAIN,
    PREDICTION_HORIZON_HOURS,
    UPDATE_INTERVAL,
)
from .controller import HeatingAction, HeatingController, PresetMode
from .heat_source import GasHeatSource
from .mpc import MPCConfig
from .preheat import PreheatConfig, PreheatPlan, PreheatPlanner
from .presence import PresenceConfig, PresenceMonitor
from .solar import estimate_solar_irradiance, get_solar_calculation
from .thermal_model import CouplingSpec, ThermalModel, ThermalObservation
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
        PresetMode.VACATION,
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
        # Preset number entities share a dict with us via
        # hass.data[DOMAIN][entry_id]["preset_temps"]. We pass it in to
        # the controller so preset changes flow through automatically.
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        self._preset_temps = data.setdefault("preset_temps", {})
        self._room_data = data
        self._controller = HeatingController(
            model,
            mpc_enabled=mpc_enabled,
            mpc_config=mpc_config,
            preset_temps_source=self._preset_temps,
        )
        # Register callbacks so the preset-number / override / simulate
        # platforms can find us.
        data["_on_preset_update"] = self._on_preset_number_update
        data["_on_override_change"] = self._on_override_change
        data["_on_simulate_request"] = self._simulate_schedule
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
            # Publish the heat source on the shared data dict so the
            # sensor platform (and any future platform) can read it
            # without a back-reference to this climate entity.
            data["heat_source"] = self._heat_source

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
        self._schedule_on_preset = str(
            options.get(
                CONF_SCHEDULE_ON_PRESET,
                config.get(CONF_SCHEDULE_ON_PRESET, DEFAULT_SCHEDULE_ON_PRESET),
            )
        )
        self._schedule_off_preset = str(
            options.get(
                CONF_SCHEDULE_OFF_PRESET,
                config.get(CONF_SCHEDULE_OFF_PRESET, DEFAULT_SCHEDULE_OFF_PRESET),
            )
        )
        # Legacy temperature fields — used only as a last-resort fallback
        # when the user hasn't configured the matching preset number.
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

        # Override + occupancy entities
        self._override_entity_id: str | None = (
            options.get(CONF_OVERRIDE_ENTITY)
            or config.get(CONF_OVERRIDE_ENTITY)
        )
        self._occupancy_entity_id: str | None = (
            options.get(CONF_OCCUPANCY_ENTITY)
            or config.get(CONF_OCCUPANCY_ENTITY)
        )
        self._override_on = False

        # Multi-room coupling spec (list of dicts from the config entry).
        coupling_cfg = (
            options.get(CONF_THERMAL_COUPLINGS)
            or config.get(CONF_THERMAL_COUPLINGS)
            or []
        )
        if coupling_cfg and not model.couplings:
            from .const import DEFAULT_COUPLING_U
            model.couplings = [
                CouplingSpec(
                    neighbour_entry_id=c["neighbour_entry_id"],
                    u_value=float(c.get("u_value", DEFAULT_COUPLING_U)),
                    enabled=bool(c.get("enabled", True)),
                )
                for c in coupling_cfg
                if isinstance(c, dict) and c.get("neighbour_entry_id")
            ]

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

        # Track user-configured override entity (e.g. input_boolean)
        if self._override_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._override_entity_id],
                    self._async_override_entity_changed,
                )
            )
            init_state = self.hass.states.get(self._override_entity_id)
            if init_state is not None:
                self._override_on = init_state.state == "on"
                self._room_data["override_on"] = self._override_on

        # Track occupancy binary sensor (motion / presence)
        if self._occupancy_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._occupancy_entity_id],
                    self._async_occupancy_changed,
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
        Update the cached schedule state.

        The schedule is treated as a *mode selector*:
          * If it exposes a ``preset`` attribute (e.g. ``comfort``,
            ``sleep``), that preset is activated on the controller.
          * Otherwise the on/off state selects the configured on- /
            off-preset (``schedule_on_preset`` / ``schedule_off_preset``).

        The actual °C for the chosen preset comes from the per-room
        preset number entities — not from the schedule. This eliminates
        the old conflict where a schedule's ``temperature`` attribute
        could disagree with the comfort preset setpoint.
        """
        if state is None:
            self._schedule_active_state = None
            self._schedule_override_temp = None
            self._apply_preheat_plan()
            return

        s = state.state
        self._schedule_active_state = s

        # If the schedule still exposes a numeric temperature attribute
        # we keep it around for diagnostics, but it is NOT used as the
        # target any more.
        override = state.attributes.get("temperature")
        try:
            override_val = float(override) if override is not None else None
        except (TypeError, ValueError):
            override_val = None
        self._schedule_override_temp = override_val

        # Select the preset that this schedule slot represents.
        preset_attr = state.attributes.get("preset")
        chosen_preset: str | None = None
        if preset_attr:
            chosen_preset = str(preset_attr).lower()
        elif s == "on":
            chosen_preset = self._schedule_on_preset
        elif s == "off":
            chosen_preset = self._schedule_off_preset

        # Apply unless the user has an override / WFH switch engaged.
        if chosen_preset and not self._override_on:
            try:
                preset_enum = PresetMode(chosen_preset)
            except ValueError:
                preset_enum = None
            if preset_enum is not None and self._preset_mode != preset_enum:
                self._preset_mode = preset_enum
                self._controller.set_preset(preset_enum)
                self._target_temp = self._controller.state.target_temp

        self._apply_preheat_plan()

    # ── v0.3 helpers: preheat, weather, presence ─────────────────

    def _schedule_is_on(self) -> bool:
        return self._schedule_active_state == "on"

    def _schedule_targets(self) -> tuple[float, float]:
        """Return (low_target, high_target) for the current schedule.

        Uses the preset-number dict as the source of truth, falling
        back to the legacy per-schedule temperature fields when no
        matching preset number is configured.
        """

        def _preset_temp(slug: str, fallback: float) -> float:
            val = self._preset_temps.get(slug)
            try:
                return float(val) if val is not None else fallback
            except (TypeError, ValueError):
                return fallback

        high = _preset_temp(self._schedule_on_preset, self._schedule_on_temp)
        low = _preset_temp(self._schedule_off_preset, self._schedule_off_temp)
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

    # ── Override + occupancy wiring ─────────────────────────────

    @callback
    def _async_override_entity_changed(self, event) -> None:
        """An external HA entity is being used as override source."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        new_on = new_state.state == "on"
        if new_on == self._override_on:
            return
        self._on_override_change(new_on)

    @callback
    def _async_occupancy_changed(self, event) -> None:
        """Binary occupancy sensor — when on, force comfort preset."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if new_state.state == "on" and self._preset_mode != PresetMode.COMFORT:
            self._preset_mode = PresetMode.COMFORT
            self._controller.set_preset(PresetMode.COMFORT)
            self._target_temp = self._controller.state.target_temp
            self._run_control_loop()
            self.async_write_ha_state()

    def _on_override_change(self, is_on: bool) -> None:
        """Called by the override switch or the external override entity."""
        self._override_on = bool(is_on)
        self._room_data["override_on"] = self._override_on
        if is_on:
            # Force comfort preset — remember the previous one so we
            # can restore it cleanly on release.
            self._room_data["_pre_override_preset"] = str(self._preset_mode)
            self._preset_mode = PresetMode.COMFORT
            self._controller.set_preset(PresetMode.COMFORT)
        else:
            # Restore whatever the schedule / user had before.
            prev = self._room_data.pop("_pre_override_preset", None)
            if prev is not None:
                try:
                    self._preset_mode = PresetMode(prev)
                    self._controller.set_preset(self._preset_mode)
                except ValueError:
                    pass
            # Re-apply the schedule so the off-preset kicks back in.
            if self._schedule_entity_id:
                self._apply_schedule_state(
                    self.hass.states.get(self._schedule_entity_id)
                )
        self._target_temp = self._controller.state.target_temp
        self._run_control_loop()
        self.async_write_ha_state()

    def _on_preset_number_update(self, slug: str, value: float) -> None:
        """The user moved a preset-number slider."""
        # If the controller's active preset is the one just changed,
        # push the new value into target_temp immediately.
        current_preset = str(self._preset_mode)
        if slug == current_preset:
            self._controller.refresh_target_from_preset()
            self._target_temp = self._controller.state.target_temp
            self._run_control_loop()
            self.async_write_ha_state()

    # ── Simulation hook (force-compute button) ──────────────────

    async def _simulate_schedule(self) -> dict:
        """Produce a rich 24-hour trajectory under the current config.

        Runs the thermal model forward with:
          * an hourly outdoor-temperature trace from the weather entity,
          * a naive solar-irradiance trace derived from sun position,
          * the *current* preset target as a constant setpoint,
          * a proportional heating schedule: whenever the model
            predicts the room will fall below target, full heat is
            applied; whenever the model predicts target + hysteresis
            would be exceeded, heat is zeroed.

        The solar trace is crucial for the anti-overshoot requirement
        — the simulator sees an afternoon solar boost coming and the
        proportional heater stops earlier in the morning, so the
        resulting trajectory doesn't overshoot when the sun hits.
        """
        outdoor = self._outdoor_temp if self._outdoor_temp is not None else 10.0
        forecast = list(self._forecast_hourly)
        if not forecast:
            forecast = [outdoor] * 24
        while len(forecast) < 24:
            forecast.append(forecast[-1])

        # Build a rough solar trace from sun position — the calculator
        # returns current W/m², so we simulate 24 hourly samples by
        # rotating the sun's elevation through the day. If numpy
        # isn't available we just fall back to a simple shape.
        solar_now = estimate_solar_irradiance(self.hass)
        # Crude model: solar follows a sine bump peaking at local noon
        # with amplitude ``solar_now`` if it's currently daytime, else
        # reduced by 0.5. The point is only to *have* a solar shape —
        # the exact numbers are a guide for the simulator, not ground
        # truth.
        import math
        solar_trace: list[float] = []
        now_hour = time.gmtime().tm_hour
        peak = max(solar_now, 200.0) if solar_now > 0 else 150.0
        for h in range(24):
            hour = (now_hour + h) % 24
            angle = math.pi * (hour - 6) / 12
            if angle <= 0 or angle >= math.pi:
                solar_trace.append(0.0)
            else:
                solar_trace.append(peak * math.sin(angle))

        target = self._target_temp
        hysteresis = self._controller.hysteresis
        # Generate the heating-fraction schedule step-by-step so the
        # controller decision uses the *simulated* temperature at each
        # step, not a constant assumption.
        steps = 24 * 4  # 15-minute steps
        step_h = 0.25
        model = self._model
        C_watt_h = model.params.thermal_mass * 1000 / 3600 or 1.0

        t = self._current_temp if self._current_temp is not None else target
        trajectory: list[dict] = []
        heating_on = False
        for step in range(steps):
            hour_offset = step * step_h
            hour_idx = min(23, int(hour_offset))
            t_out = forecast[hour_idx]
            solar = solar_trace[hour_idx]

            # Decide heating state with hysteresis.
            if t < target - hysteresis:
                heating_on = True
            elif t > target + hysteresis:
                heating_on = False
            # Anti-overshoot: if solar irradiance is currently strong
            # enough that the passive solar gain alone would lift the
            # room, bias heating OFF. This is the emergent "don't
            # heat when the sun is going to help" behaviour.
            passive_dT_per_h = (
                solar * model.params.solar_gain_factor
                - model.params.heat_loss_coeff * (t - t_out)
            ) / C_watt_h
            if passive_dT_per_h > 0.5 and t > target - 0.5:
                heating_on = False

            heat_frac = 1.0 if heating_on else 0.0
            q_heat = heat_frac * model.params.heating_power
            q_solar = solar * model.params.solar_gain_factor
            q_loss = model.params.heat_loss_coeff * (t - t_out)
            dT = (q_heat + q_solar - q_loss) / C_watt_h * step_h
            t += dT
            trajectory.append(
                {
                    "t": round(hour_offset + step_h, 3),
                    "temperature": round(t, 3),
                    "t_outdoor": round(t_out, 2),
                    "q_heat_w": round(q_heat, 1),
                    "q_solar_w": round(q_solar, 1),
                    "q_loss_w": round(q_loss, 1),
                    "heating": heating_on,
                }
            )

        return {
            "generated_ts": time.time(),
            "horizon_hours": 24.0,
            "target_temp": target,
            "hysteresis": hysteresis,
            "trajectory": trajectory,
            "initial_temp": self._current_temp,
            "initial_outdoor": outdoor,
            "solar_trace": solar_trace,
            "forecast_outdoor": forecast,
        }

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
        # than the old binary on/off. ``current_power_w()`` already
        # filters out cooking / shower spikes.
        measured_heat_w: float | None = None
        if self._heat_source is not None:
            measured_heat_w = self._heat_source.current_power_w()

        # Coupling: sum heat flowing in from neighbouring rooms (positive
        # means neighbour is warmer → heat flows IN). The EKF treats this
        # as extra known "heat-in" so it doesn't confuse neighbour
        # warming for an unusually low H.
        coupling_power_w = self._compute_coupling_power_w()

        # Feed observation to thermal model (EKF learns from this)
        obs = ThermalObservation(
            timestamp=time.time(),
            t_indoor=self._current_temp,
            t_outdoor=outdoor,
            heating_on=actually_heating,
            solar_irradiance=solar,
            heat_power_w=measured_heat_w,
            coupling_power_w=coupling_power_w,
        )
        self._model.add_observation(obs)

        # Spike detection: let the heat source see how the model's last
        # prediction lined up with reality. If high gas power consistently
        # fails to warm the room, subsequent ``current_power_w()`` calls
        # will zero out during the spike — the EKF then stops blaming H
        # for the missing heat.
        if self._heat_source is not None:
            self._heat_source.record_heating_result(
                dT_observed=self._model.last_dT_observed,
                dT_predicted=self._model.last_dT_predicted,
                timestamp=obs.timestamp,
            )
            # Stash the latest spike-aware state into the model so the
            # save/load round-trip picks it up across restarts.
            self._model._heat_source_state = self._heat_source.to_dict()

        # Roll an 8-hour forecast snapshot so the dashboard can later
        # overlay "predicted 8 h ago" against observed trajectory.
        try:
            self._model.record_prediction_snapshot(
                timestamp=obs.timestamp,
                t_indoor=self._current_temp,
                t_outdoor=outdoor,
                solar_irradiance=solar,
                horizon_hours=PREDICTION_HORIZON_HOURS,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Prediction snapshot failed: %s", err)

        # v0.3: re-evaluate presence (grace-period may have elapsed) and
        # roll the pre-heat plan forward each cycle.
        if self._presence.enabled:
            self._evaluate_presence()
        if self._schedule_entity_id:
            self._apply_preheat_plan()

        self._run_control_loop()
        self.async_write_ha_state()

    def _compute_coupling_power_w(self) -> float:
        """
        Estimate heat-exchange (W) flowing into this room from coupled
        neighbours using their current indoor temperatures.

        Formula per coupling edge:
            Q = U * (T_neighbour - T_this)     [W]

        Positive = heat flows IN. A neighbour that's colder contributes
        a negative term (heat leaves this room into the neighbour).

        Only enabled couplings are counted; disabled ones let the user
        represent "door closed" without removing the declared edge.
        """
        if self._current_temp is None:
            return 0.0
        total_w = 0.0
        domain = self.hass.data.get(DOMAIN, {})
        for spec in getattr(self._model, "couplings", []) or []:
            if not spec.enabled:
                continue
            nb = domain.get(spec.neighbour_entry_id)
            if not nb:
                continue
            nb_eid = nb.get("climate_entity_id")
            if not nb_eid:
                continue
            nb_state = self.hass.states.get(nb_eid)
            if nb_state is None:
                continue
            nb_temp_raw = nb_state.attributes.get("current_temperature")
            if nb_temp_raw is None:
                continue
            try:
                nb_temp = float(nb_temp_raw)
            except (TypeError, ValueError):
                continue
            total_w += spec.u_value * (nb_temp - self._current_temp)
        return total_w

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
        #
        # Key design note: even when no room *actively* wants heat, we
        # leave the thermostat parked at the highest room-target (via
        # HeatingZone.calculate_setpoint — which now returns the max
        # target across rooms instead of None) so the thermostat can
        # modulate down naturally instead of slamming to 5°C every time
        # we hit the hysteresis top. That 5°C behaviour is reserved for
        # the ventilation case (a window is open).
        if self._window_open:
            # Ventilation: force a low setpoint so the boiler does not
            # waste gas heating an open window.
            window_sp = self._zone.window_open_setpoint()
            last = self._zone._last_setpoint
            if last is None or abs(last - window_sp) > 0.1:
                self.hass.async_create_task(
                    self._async_set_underlying_temp(window_sp)
                )
                self._zone._last_setpoint = window_sp
        else:
            setpoint = self._zone.calculate_setpoint()
            if setpoint is not None:
                last = self._zone._last_setpoint
                if last is None or abs(last - setpoint) > 0.05:
                    self.hass.async_create_task(
                        self._async_set_underlying_temp(setpoint)
                    )
                    self._zone._last_setpoint = setpoint

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

        # v0.5: override + coupling + simulation diagnostics
        attrs["override_on"] = self._override_on
        if getattr(self._model, "couplings", None):
            attrs["thermal_couplings"] = [
                {
                    "neighbour_entry_id": c.neighbour_entry_id,
                    "u_value": c.u_value,
                    "enabled": c.enabled,
                }
                for c in self._model.couplings
            ]
        sim = self._room_data.get("last_simulation")
        if sim:
            attrs["last_simulation_ts"] = sim.get("generated_ts")
        if self._heat_source is not None:
            attrs["gas_spike_active"] = self._heat_source.in_spike
            attrs["gas_spike_events"] = self._heat_source.spike_events

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
            # Only idle the thermostat if no other room in the zone wants
            # heat. Instead of dropping the setpoint to 5°C (which forces
            # a large cold-start delta on the next "on" flip), we park it
            # at the minimum configured idle temperature so the
            # thermostat can still modulate gently.
            if not self._zone.any_room_wants_heat:
                await self._async_set_underlying_temp(DEFAULT_IDLE_MIN_TEMP)
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
