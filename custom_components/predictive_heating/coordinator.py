"""DataUpdateCoordinator for Predictive Heating.

Orchestrates data collection, model training, and optimization.
Stores the latest traces so sensors can expose them for debugging.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_CONTROL,
    CONF_AWAY_TEMP,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_W,
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_HEATING_DEVICES,
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
    DEFAULT_OPTIMIZATION_TIMESTEP_MIN,
    DEFAULT_PREDICTION_HORIZON_HOURS,
    DEFAULT_TEMPERATURE_SCHEDULE,
    DEFAULT_TRAINING_INTERVAL_DAYS,
    DEFAULT_TRAINING_WINDOW_DAYS,
    DOMAIN,
    FALLBACK_ELEC_PRICE,
    FALLBACK_INTERNAL_GAIN_W,
    FALLBACK_OUTDOOR_TEMP,
)
from .data_collector import collect_training_data
from .model import SimpleHeater, SlotInput, ThermalParams, train_model
from .optimizer import OptimizationResult, optimize_heating
from .trace import Trace
from .weather import WeatherForecast, estimate_solar_gain_from_forecast

_LOGGER = logging.getLogger(__name__)


class PredictiveHeatingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages training, optimization, and exposes results to sensors."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=5))
        self.config = {**entry.data, **(entry.options or {})}

        # State
        self.params = ThermalParams()
        self.heaters = self._build_heaters()
        self.schedule = dict(self.config.get(CONF_TEMPERATURE_SCHEDULE, DEFAULT_TEMPERATURE_SCHEDULE))
        self.last_optimization: OptimizationResult | None = None

        # Traces & visualization data — stored for sensor attributes
        self.last_training_trace: dict[str, Any] | None = None
        self.last_optimize_trace: dict[str, Any] | None = None
        self.last_training_residuals: list[dict] = []
        """Per-timestep residuals from last training run, for visualization."""
        self.last_training_inputs: dict[str, Any] = {}
        """Sampled raw training inputs (temps, heating) for TrainingInputSensor."""

        # Persistence
        self._storage_path = os.path.join(
            hass.config.config_dir, ".storage", f"{DOMAIN}_{entry.entry_id}.json"
        )
        self._load_params()

        # Weather forecast (optional)
        weather_entity = self.config.get(CONF_WEATHER_ENTITY, "")
        self.weather: WeatherForecast | None = (
            WeatherForecast(hass, weather_entity) if weather_entity else None
        )

    # ── Device building ───────────────────────────────────────────────────────

    def _build_heaters(self) -> list[SimpleHeater]:
        """Build SimpleHeater list from config."""
        heaters = []
        for d in self.config.get(CONF_HEATING_DEVICES, []):
            name = d.get(CONF_DEVICE_NAME, "Heater")
            entity_id = d.get(CONF_DEVICE_ENTITY, "")
            power_w = float(d.get(CONF_DEVICE_POWER_W, 0.0))
            if entity_id and power_w > 0:
                heaters.append(SimpleHeater(
                    name=name,
                    entity_id=entity_id,
                    power_w=power_w,
                ))
        return heaters

    # ── Parameter persistence ─────────────────────────────────────────────────

    def _load_params(self) -> None:
        """Load saved model parameters from disk."""
        try:
            if os.path.exists(self._storage_path):
                with open(self._storage_path) as f:
                    data = json.load(f)
                self.params = ThermalParams(
                    ua=data.get("ua", self.params.ua),
                    thermal_mass=data.get("thermal_mass", self.params.thermal_mass),
                    r_squared=data.get("r_squared", 0.0),
                    last_trained=(
                        datetime.fromisoformat(data["last_trained"])
                        if data.get("last_trained") else None
                    ),
                    n_data_points=data.get("n_data_points", 0),
                    t_outdoor_avg_training=data.get("t_outdoor_avg_training"),
                    training_mode=data.get("training_mode", "phase2_variable_outdoor"),
                    q_heating_source=data.get("q_heating_source", "heater_onoff"),
                )
                _LOGGER.info("Loaded saved model: %s", self.params.describe())
                return
        except Exception as err:
            _LOGGER.warning("Could not load model params: %s", err)

        # No saved params — try house profile estimates from config
        initial_ua = self.config.get("initial_ua_estimate")
        initial_c = self.config.get("initial_thermal_mass_estimate")
        if initial_ua is not None and initial_c is not None:
            self.params = ThermalParams(
                ua=float(initial_ua),
                thermal_mass=float(initial_c),
                r_squared=0.0,
                last_trained=None,
                n_data_points=0,
            )
            _LOGGER.info(
                "Using house profile estimates: UA=%.0f W/K, C=%.1f kWh/K",
                self.params.ua, self.params.thermal_mass,
            )
        else:
            _LOGGER.info(
                "No saved params — using defaults: UA=%.0f, C=%.1f",
                self.params.ua, self.params.thermal_mass,
            )

    def _save_params(self) -> None:
        """Persist model parameters to disk."""
        try:
            os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)
            with open(self._storage_path, "w") as f:
                json.dump({
                    "ua": self.params.ua,
                    "thermal_mass": self.params.thermal_mass,
                    "r_squared": self.params.r_squared,
                    "last_trained": (
                        self.params.last_trained.isoformat()
                        if self.params.last_trained else None
                    ),
                    "n_data_points": self.params.n_data_points,
                    "t_outdoor_avg_training": self.params.t_outdoor_avg_training,
                    "training_mode": self.params.training_mode,
                    "q_heating_source": self.params.q_heating_source,
                }, f, indent=2)
        except Exception as err:
            _LOGGER.error("Could not save model params: %s", err)

    # ── Entity helpers ────────────────────────────────────────────────────────

    def _entity_float(self, entity_id: str) -> float | None:
        """Read current numeric value of an HA entity."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _get_target_temp(self, dt: datetime) -> float:
        """Look up target temperature from schedule."""
        time_str = dt.strftime("%H:%M")
        sorted_times = sorted(self.schedule.keys())
        target = list(self.schedule.values())[0]
        for t in sorted_times:
            if t <= time_str:
                target = self.schedule[t]
            else:
                break
        return target

    # ── Electricity prices ────────────────────────────────────────────────────

    async def _fetch_official_nordpool_prices(self) -> list[tuple[datetime, float]]:
        """Fetch prices from official HA Nord Pool integration."""
        try:
            now = dt_util.now()
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            prices: list[tuple[datetime, float]] = []

            for day_offset in [0, 1]:
                target_date = (today + timedelta(days=day_offset)).date()
                try:
                    response = await self.hass.services.async_call(
                        "nordpool",
                        "get_prices_for_date",
                        {"date": target_date.isoformat()},
                        blocking=True,
                        return_response=True,
                    )
                    if response and "prices" in response:
                        for item in response["prices"]:
                            try:
                                item_time = item.get("time")
                                if isinstance(item_time, str):
                                    item_time = datetime.fromisoformat(item_time)
                                prices.append((item_time, float(item.get("price", 0))))
                            except (ValueError, TypeError):
                                pass
                except Exception:
                    pass

            if prices:
                return sorted(prices, key=lambda x: x[0])
        except Exception as err:
            _LOGGER.debug("Official Nord Pool service call failed: %s", err)
        return []

    async def _get_prices(self) -> list[tuple[datetime, float]]:
        """Read electricity prices from multiple possible integrations.

        Supports (in detection order):
        1. Official Nord Pool HA integration
        2. Custom Nordpool HACS: raw_today/raw_tomorrow attributes
        3. ENTSO-e HACS: prices attribute
        4. Plain list attributes (today/tomorrow)
        5. Fallback: current sensor state as flat rate
        """
        entity_id = self.config.get(CONF_ELECTRICITY_PRICE_ENTITY, "")

        # Try official Nord Pool service first
        official_prices = await self._fetch_official_nordpool_prices()
        if official_prices:
            return official_prices

        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return []

        attrs = state.attributes
        now = dt_util.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        prices: list[tuple[datetime, float]] = []

        # Custom Nordpool HACS (raw_today / raw_tomorrow)
        raw_today = attrs.get("raw_today")
        if isinstance(raw_today, list) and raw_today:
            prices.extend(self._parse_raw_price_list(raw_today))
            raw_tomorrow = attrs.get("raw_tomorrow")
            if attrs.get("tomorrow_valid") and isinstance(raw_tomorrow, list):
                prices.extend(self._parse_raw_price_list(raw_tomorrow))
            if prices:
                return sorted(prices, key=lambda x: x[0])

        # ENTSO-e HACS
        for key in ("prices", "prices_today", "prices_tomorrow"):
            plist = attrs.get(key)
            if isinstance(plist, list) and plist:
                prices.extend(self._parse_raw_price_list(plist))
        if prices:
            return sorted(prices, key=lambda x: x[0])

        # Plain list attributes (today / tomorrow)
        for day_offset, key in [(0, "today"), (1, "tomorrow")]:
            data = attrs.get(key)
            if isinstance(data, list):
                base = today + timedelta(days=day_offset)
                for i, val in enumerate(data):
                    if val is not None:
                        try:
                            prices.append((base + timedelta(hours=i), float(val)))
                        except (ValueError, TypeError):
                            pass
        if prices:
            return sorted(prices, key=lambda x: x[0])

        # Flat rate fallback from current state
        try:
            p = float(state.state)
            return [(now + timedelta(hours=h), p) for h in range(48)]
        except (ValueError, TypeError):
            pass

        return []

    @staticmethod
    def _parse_raw_price_list(price_list: list) -> list[tuple[datetime, float]]:
        """Parse price entries from various integration formats."""
        prices = []
        for item in price_list:
            if not isinstance(item, dict):
                continue
            ts = item.get("start") or item.get("time") or item.get("datetime")
            if ts is None:
                continue
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    continue
            val = item.get("value") if item.get("value") is not None else item.get("price")
            if val is None:
                continue
            try:
                prices.append((ts, float(val)))
            except (ValueError, TypeError):
                pass
        return prices

    def _price_at(self, prices: list[tuple[datetime, float]], t: datetime) -> float:
        """Find electricity price at time t."""
        if not prices:
            return FALLBACK_ELEC_PRICE

        is_aware = prices[0][0].tzinfo is not None
        try:
            if is_aware and t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            elif not is_aware and t.tzinfo is not None:
                t = t.replace(tzinfo=None)

            for i in range(len(prices) - 1):
                p0, p1 = prices[i][0], prices[i + 1][0]
                if is_aware and p0.tzinfo is None:
                    p0 = p0.replace(tzinfo=timezone.utc)
                    p1 = p1.replace(tzinfo=timezone.utc)
                elif not is_aware and p0.tzinfo is not None:
                    p0 = p0.replace(tzinfo=None)
                    p1 = p1.replace(tzinfo=None)
                if p0 <= t < p1:
                    return prices[i][1]
            return prices[-1][1]
        except TypeError:
            t_naive = t.replace(tzinfo=None)
            for i in range(len(prices) - 1):
                p0 = prices[i][0].replace(tzinfo=None)
                p1 = prices[i + 1][0].replace(tzinfo=None)
                if p0 <= t_naive < p1:
                    return prices[i][1]
            return prices[-1][1] if prices else FALLBACK_ELEC_PRICE

    # ── Training ──────────────────────────────────────────────────────────────

    async def async_train_model(
        self, exclude_periods: list[tuple[str, str]] | None = None
    ) -> None:
        """Train the thermal model from historical data."""
        trace = Trace("training")
        window = self.config.get(CONF_TRAINING_WINDOW_DAYS, DEFAULT_TRAINING_WINDOW_DAYS)

        try:
            data = await collect_training_data(
                self.hass, self.config, window_days=window, trace=trace,
            )

            # Filter out excluded periods
            if exclude_periods:
                excluded_ranges = []
                for start_str, end_str in exclude_periods:
                    try:
                        excluded_ranges.append((
                            datetime.fromisoformat(start_str),
                            datetime.fromisoformat(end_str),
                        ))
                    except (ValueError, TypeError) as err:
                        trace.warn("bad_exclude", f"Invalid exclude period: {err}")

                if excluded_ranges:
                    before = data.n_points
                    keep = [
                        i for i, ts in enumerate(data.timestamps)
                        if not any(s <= ts <= e for s, e in excluded_ranges)
                    ]
                    data.timestamps = [data.timestamps[i] for i in keep]
                    data.t_indoor = [data.t_indoor[i] for i in keep]
                    data.t_outdoor = [data.t_outdoor[i] for i in keep]
                    data.q_heating_w = [data.q_heating_w[i] for i in keep]
                    data.q_solar_w = [data.q_solar_w[i] for i in keep]
                    data.q_internal_w = [data.q_internal_w[i] for i in keep]
                    trace.step("exclude_periods", result={
                        "points_before": before,
                        "points_after": data.n_points,
                        "points_dropped": before - data.n_points,
                    })

            if data.n_points < 20:
                trace.warn("skip", f"Only {data.n_points} points, skipping training")
                self.last_training_trace = trace.summary()
                return

            use_constant_outdoor = bool(
                self.config.get(CONF_TRAINING_USE_CONSTANT_OUTDOOR, True)
            )

            params, residuals = await self.hass.async_add_executor_job(
                train_model,
                data.timestamps, data.t_indoor, data.t_outdoor,
                data.q_heating_w, data.q_solar_w, data.q_internal_w,
                use_constant_outdoor,
                data.q_heating_source,
                trace,
            )
            self.params = params
            self.last_training_residuals = residuals

            # Store sampled input arrays for TrainingInputSensor
            self.last_training_inputs = {
                "timestamps": data.viz_timestamps,
                "t_indoor": data.viz_t_indoor,
                "t_outdoor": data.viz_t_outdoor,
                "q_heating_w": data.viz_q_heating_w,
                "q_heating_source": data.q_heating_source,
                "n_points": data.n_points,
                "coverage_pct": round(data.quality.coverage_pct, 1),
            }

            self._save_params()
            _LOGGER.info("Training complete: %s", self.params.describe())

        except Exception as err:
            trace.error("exception", f"Training failed: {err}", error=str(err))
            _LOGGER.error("Training failed: %s", err)

        self.last_training_trace = trace.summary()

    def set_model_params(
        self, ua: float | None = None, thermal_mass: float | None = None
    ) -> None:
        """Manually override model parameters."""
        if ua is not None:
            self.params.ua = max(10.0, min(2000.0, ua))
        if thermal_mass is not None:
            self.params.thermal_mass = max(1.0, min(200.0, thermal_mass))
        self._save_params()

    # ── Main update loop ──────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """Called every 5 minutes. Trains if due, then optimizes."""
        now = dt_util.now()

        # Check if training is due
        interval = self.config.get(CONF_TRAINING_INTERVAL_DAYS, DEFAULT_TRAINING_INTERVAL_DAYS)
        last_trained = self.params.last_trained
        # Ensure timezone-aware for comparison with now (which is always aware from dt_util)
        if last_trained is not None and last_trained.tzinfo is None:
            last_trained = last_trained.replace(tzinfo=timezone.utc)

        if last_trained is None or (now - last_trained > timedelta(days=interval)):
            await self.async_train_model()

        next_training = (
            (last_trained or self.params.last_trained) + timedelta(days=interval)
            if last_trained else now + timedelta(hours=1)
        )

        # Read current temperatures
        t_indoor = self._entity_float(self.config.get(CONF_INDOOR_TEMP_ENTITY, ""))
        if t_indoor is None:
            raise UpdateFailed("Indoor temperature sensor unavailable")

        t_outdoor = self._entity_float(self.config.get(CONF_OUTDOOR_TEMP_ENTITY, ""))
        if t_outdoor is None:
            t_outdoor = FALLBACK_OUTDOOR_TEMP

        # Build time slots
        horizon = self.config.get(CONF_PREDICTION_HORIZON_HOURS, DEFAULT_PREDICTION_HORIZON_HOURS)
        timestep = self.config.get(CONF_OPTIMIZATION_TIMESTEP_MIN, DEFAULT_OPTIMIZATION_TIMESTEP_MIN)
        dt_s = timestep * 60.0
        n_slots = int(horizon * 60 / timestep)
        prices = await self._get_prices()

        # Weather forecast (optional)
        forecast_available = False
        if self.weather is not None:
            try:
                forecast_available = await self.weather.async_update()
            except Exception as err:
                _LOGGER.warning("Weather forecast fetch failed: %s", err)

        latitude = self.hass.config.latitude or 52.0

        slots = []
        for j in range(n_slots):
            slot_time = now + timedelta(seconds=j * dt_s)

            slot_t_outdoor = t_outdoor
            if forecast_available and self.weather is not None:
                forecast_temp = self.weather.temperature_at(slot_time)
                if forecast_temp is not None:
                    slot_t_outdoor = forecast_temp

            solar_w = 0.0
            if forecast_available and self.weather is not None:
                cloud_pct = self.weather.cloud_coverage_at(slot_time)
                solar_w = estimate_solar_gain_from_forecast(
                    cloud_coverage_pct=cloud_pct,
                    hour_of_day=slot_time.hour + slot_time.minute / 60.0,
                    day_of_year=slot_time.timetuple().tm_yday,
                    latitude=latitude,
                )

            slots.append(SlotInput(
                start=slot_time,
                duration_s=dt_s,
                t_outdoor=slot_t_outdoor,
                t_target=self._get_target_temp(slot_time),
                electricity_price=self._price_at(prices, slot_time),
                solar_gain_w=solar_w,
                internal_gain_w=self.config.get(CONF_INTERNAL_GAIN_W, FALLBACK_INTERNAL_GAIN_W),
            ))

        # Run optimizer
        away_temp = self.config.get(CONF_AWAY_TEMP, DEFAULT_AWAY_TEMP)
        self.last_optimization = await self.hass.async_add_executor_job(
            optimize_heating,
            self.params, self.heaters, slots, t_indoor, away_temp,
        )

        if self.last_optimization.trace:
            self.last_optimize_trace = self.last_optimization.trace.summary()

        # Extract first-slot decisions
        first_slot_decisions: dict[str, dict] = {}
        if self.last_optimization.slot_results:
            for d in self.last_optimization.slot_results[0].device_decisions:
                first_slot_decisions[d.device_name] = {
                    "heating_on": d.heating_on,
                    "recommended_state": d.heating_on,
                    "recommended_setpoint": d.recommended_setpoint,
                    "heat_output_w": d.heat_output_w,
                    "cost_per_wh": round(d.cost_per_wh, 6),
                    "reason": d.reason,
                }

        # Auto-control: push setpoints to climate entities
        auto_control = self.config.get(CONF_AUTO_CONTROL, False)
        if auto_control and first_slot_decisions:
            await self._apply_setpoints(first_slot_decisions)

        predicted_temp = (
            self.last_optimization.predicted_temperatures[1]
            if len(self.last_optimization.predicted_temperatures) > 1
            else t_indoor
        )

        return {
            "ua_value": round(self.params.ua, 1),
            "thermal_mass": round(self.params.thermal_mass, 1),
            "predicted_temperature": round(predicted_temp, 2),
            "estimated_cost_24h": round(self.last_optimization.total_cost, 4),
            "model_fit_r2": round(self.params.r_squared, 4),
            "next_training": next_training.isoformat(),
            "last_training": (
                self.params.last_trained.isoformat() if self.params.last_trained else None
            ),
            "current_target": self._get_target_temp(now),
            "t_indoor": t_indoor,
            "t_outdoor": t_outdoor,
            "n_training_points": self.params.n_data_points,
            "devices": first_slot_decisions,
            "weather_forecast_available": forecast_available,
            "solar_gain_current_w": round(slots[0].solar_gain_w, 0) if slots else 0,
            # Phase 1 metadata
            "training_mode": self.params.training_mode,
            "q_heating_source": self.params.q_heating_source,
            "t_outdoor_avg_training": self.params.t_outdoor_avg_training,
        }

    async def _apply_setpoints(self, decisions: dict[str, dict]) -> None:
        """Push recommended setpoints to climate entities."""
        for heater in self.heaters:
            info = decisions.get(heater.name)
            if info is None:
                continue

            entity_id = heater.entity_id
            if not entity_id.startswith("climate."):
                continue

            setpoint = info.get("recommended_setpoint")
            if setpoint is None:
                continue

            state = self.hass.states.get(entity_id)
            if state is not None:
                current = state.attributes.get("temperature")
                if current is not None:
                    try:
                        if abs(float(current) - setpoint) < 0.1:
                            continue
                    except (ValueError, TypeError):
                        pass

            try:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {"entity_id": entity_id, "temperature": setpoint},
                    blocking=False,
                )
                _LOGGER.info(
                    "Auto-control: set %s to %.1f°C (%s)",
                    entity_id, setpoint, info.get("reason", ""),
                )
            except Exception as err:
                _LOGGER.warning("Failed to set temperature on %s: %s", entity_id, err)
