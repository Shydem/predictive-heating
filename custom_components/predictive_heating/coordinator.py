"""DataUpdateCoordinator for Predictive Heating.

Orchestrates data collection, model training, and optimization.
Stores the latest traces so sensors can expose them for debugging.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_CONTROL,
    CONF_AWAY_TEMP,
    CONF_COP_COEFFICIENTS,
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_GAS_EFFICIENCY,
    CONF_GAS_PRICE,
    CONF_HEATING_DEVICES,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INTERNAL_GAIN_W,
    CONF_OPTIMIZATION_TIMESTEP_MIN,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PREDICTION_HORIZON_HOURS,
    CONF_TEMPERATURE_SCHEDULE,
    CONF_TRAINING_INTERVAL_DAYS,
    CONF_TRAINING_WINDOW_DAYS,
    CONF_WEATHER_ENTITY,
    CONF_DEVICE_COP_DATA,
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_MAX_OUTPUT_W,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SOURCE,
    CONF_DEVICE_TYPE,
    DEFAULT_AWAY_TEMP,
    DEFAULT_COP_A,
    DEFAULT_COP_B,
    DEFAULT_COP_DATA_AIR_SOURCE,
    DEFAULT_GAS_EFFICIENCY,
    DEFAULT_GAS_PRICE,
    DEFAULT_OPTIMIZATION_TIMESTEP_MIN,
    DEFAULT_PREDICTION_HORIZON_HOURS,
    DEFAULT_TEMPERATURE_SCHEDULE,
    DEFAULT_TRAINING_INTERVAL_DAYS,
    DEFAULT_TRAINING_WINDOW_DAYS,
    DOMAIN,
    FALLBACK_ELEC_PRICE,
    FALLBACK_INTERNAL_GAIN_W,
    FALLBACK_OUTDOOR_TEMP,
    SOURCE_ELECTRIC,
)
from .data_collector import collect_training_data
from .model import HeatingDevice, SlotInput, ThermalParams, train_model
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
        self.devices = self._build_devices()
        self.schedule = dict(self.config.get(CONF_TEMPERATURE_SCHEDULE, DEFAULT_TEMPERATURE_SCHEDULE))
        self.last_optimization: OptimizationResult | None = None

        # Traces — stored for sensor attributes
        self.last_training_trace: dict[str, Any] | None = None
        self.last_optimize_trace: dict[str, Any] | None = None

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

    def _build_devices(self) -> list[HeatingDevice]:
        """Build HeatingDevice list from config."""
        devices = []
        for d in self.config.get(CONF_HEATING_DEVICES, []):
            cop_data = d.get(CONF_DEVICE_COP_DATA, [])
            # Parse COP data: stored as list of [temp, cop] pairs
            cop_points = []
            if isinstance(cop_data, list):
                for item in cop_data:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        try:
                            cop_points.append((float(item[0]), float(item[1])))
                        except (ValueError, TypeError):
                            pass
            # Default COP curve for electric devices without explicit data
            if d[CONF_DEVICE_SOURCE] == SOURCE_ELECTRIC and not cop_points:
                cop_points = list(DEFAULT_COP_DATA_AIR_SOURCE)

            devices.append(HeatingDevice(
                name=d[CONF_DEVICE_NAME],
                entity_id=d[CONF_DEVICE_ENTITY],
                device_type=d[CONF_DEVICE_TYPE],
                energy_source=d[CONF_DEVICE_SOURCE],
                max_output_w=d[CONF_DEVICE_MAX_OUTPUT_W],
                cop_data_points=cop_points,
            ))
        return devices

    # ── Parameter persistence ─────────────────────────────────────────────────

    def _load_params(self) -> None:
        """Load saved model parameters from disk.

        Priority: saved params > house profile estimates > blind defaults.
        """
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
                "Using house profile estimates: UA=%.0f W/K, C=%.1f kWh/K "
                "(will be refined by training)",
                self.params.ua, self.params.thermal_mass,
            )
        else:
            _LOGGER.info(
                "No saved params or house profile — using defaults: UA=%.0f, C=%.1f",
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

    def _get_prices(self) -> list[tuple[datetime, float]]:
        """Read electricity prices from multiple possible integrations.

        Supports (in detection order):
        1. Custom Nordpool HACS: raw_today/raw_tomorrow attributes
           [{start, end, value}, ...]
        2. ENTSO-e HACS: prices/prices_today/prices_tomorrow attributes
           [{time, price}, ...]
        3. Any sensor with today/tomorrow plain list attributes
        4. Fallback: current sensor state as flat rate for 48h
        """
        entity_id = self.config.get(CONF_ELECTRICITY_PRICE_ENTITY, "")
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return []

        prices: list[tuple[datetime, float]] = []
        attrs = state.attributes
        now = dt_util.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # ── Strategy 1: Custom Nordpool HACS (raw_today / raw_tomorrow) ───
        raw_today = attrs.get("raw_today")
        raw_tomorrow = attrs.get("raw_tomorrow")
        if isinstance(raw_today, list) and raw_today:
            prices.extend(self._parse_raw_price_list(raw_today))
            tomorrow_valid = attrs.get("tomorrow_valid", False)
            if tomorrow_valid and isinstance(raw_tomorrow, list):
                prices.extend(self._parse_raw_price_list(raw_tomorrow))
            if prices:
                _LOGGER.debug(
                    "Loaded %d prices via custom Nordpool (raw_today/raw_tomorrow)",
                    len(prices),
                )
                return sorted(prices, key=lambda x: x[0])

        # ── Strategy 2: ENTSO-e HACS (prices attribute) ──────────────────
        entsoe_prices = attrs.get("prices")
        if isinstance(entsoe_prices, list) and entsoe_prices:
            prices.extend(self._parse_raw_price_list(entsoe_prices))
            if prices:
                _LOGGER.debug(
                    "Loaded %d prices via ENTSO-e (prices attr)", len(prices),
                )
                return sorted(prices, key=lambda x: x[0])

        # Also try prices_today + prices_tomorrow variant
        for key in ("prices_today", "prices_tomorrow"):
            plist = attrs.get(key)
            if isinstance(plist, list):
                prices.extend(self._parse_raw_price_list(plist))
        if prices:
            _LOGGER.debug(
                "Loaded %d prices via ENTSO-e (today/tomorrow)", len(prices),
            )
            return sorted(prices, key=lambda x: x[0])

        # ── Strategy 3: Plain list attributes (today / tomorrow) ──────────
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
            _LOGGER.debug("Loaded %d prices from plain list attrs", len(prices))
            return sorted(prices, key=lambda x: x[0])

        # ── Strategy 4: Flat rate fallback from current state ─────────────
        try:
            p = float(state.state)
            prices = [(now + timedelta(hours=h), p) for h in range(48)]
            _LOGGER.debug("Using flat rate fallback: %.4f/kWh", p)
        except (ValueError, TypeError):
            pass

        return sorted(prices, key=lambda x: x[0])

    @staticmethod
    def _parse_raw_price_list(
        price_list: list,
    ) -> list[tuple[datetime, float]]:
        """Parse price entries from various integration formats.

        Handles:
        - Custom Nordpool: {start: datetime|str, end: ..., value: float}
        - ENTSO-e:         {time: str, price: float}
        - Mixed:           {start: ..., price: ...} or {time: ..., value: ...}
        """
        prices = []
        for item in price_list:
            if not isinstance(item, dict):
                continue
            # Find timestamp: try 'start', then 'time', then 'datetime'
            ts = item.get("start") or item.get("time") or item.get("datetime")
            if ts is None:
                continue
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    continue
            # Find price: try 'value', then 'price'
            val = item.get("value")
            if val is None:
                val = item.get("price")
            if val is None:
                continue
            try:
                prices.append((ts, float(val)))
            except (ValueError, TypeError):
                pass
        return prices

    def _price_at(self, prices: list[tuple[datetime, float]], t: datetime) -> float:
        """Interpolate price at time t."""
        if not prices:
            return FALLBACK_ELEC_PRICE
        for i in range(len(prices) - 1):
            if prices[i][0] <= t < prices[i + 1][0]:
                return prices[i][1]
        return prices[-1][1]

    # ── Training ──────────────────────────────────────────────────────────────

    async def async_train_model(
        self, exclude_periods: list[tuple[str, str]] | None = None
    ) -> None:
        """Train the thermal model from historical data.

        Args:
            exclude_periods: Optional list of (start_iso, end_iso) tuples.
                             Data within these ranges is dropped before fitting.
                             Useful for excluding sensor outages or anomalies.
        """
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
                    keep = []
                    for i, ts in enumerate(data.timestamps):
                        excluded = any(s <= ts <= e for s, e in excluded_ranges)
                        if not excluded:
                            keep.append(i)

                    data.timestamps = [data.timestamps[i] for i in keep]
                    data.t_indoor = [data.t_indoor[i] for i in keep]
                    data.t_outdoor = [data.t_outdoor[i] for i in keep]
                    data.q_heating_w = [data.q_heating_w[i] for i in keep]
                    data.q_solar_w = [data.q_solar_w[i] for i in keep]
                    data.q_internal_w = [data.q_internal_w[i] for i in keep]

                    trace.step("exclude_periods", result={
                        "excluded_ranges": len(excluded_ranges),
                        "points_before": before,
                        "points_after": data.n_points,
                        "points_dropped": before - data.n_points,
                    })

            if data.n_points < 20:
                trace.warn("skip", f"Only {data.n_points} points, skipping training")
                self.last_training_trace = trace.summary()
                return

            self.params = await self.hass.async_add_executor_job(
                train_model,
                data.timestamps, data.t_indoor, data.t_outdoor,
                data.q_heating_w, data.q_solar_w, data.q_internal_w,
                trace,
            )
            self._save_params()
            _LOGGER.info("Training complete: %s", self.params.describe())

        except Exception as err:
            trace.error("exception", f"Training failed: {err}", error=str(err))
            _LOGGER.error("Training failed: %s", err)

        self.last_training_trace = trace.summary()

    def set_model_params(
        self, ua: float | None = None, thermal_mass: float | None = None
    ) -> None:
        """Manually override model parameters.

        Useful when you know your house's UA from an energy audit,
        or want to experiment with different values.
        Overridden values persist until the next training run.
        """
        if ua is not None:
            self.params.ua = max(10.0, min(2000.0, ua))
            _LOGGER.info("UA manually set to %.1f W/K", self.params.ua)
        if thermal_mass is not None:
            self.params.thermal_mass = max(1.0, min(200.0, thermal_mass))
            _LOGGER.info("Thermal mass manually set to %.1f kWh/K", self.params.thermal_mass)
        self._save_params()

    # ── Main update loop ──────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """Called every 5 minutes. Trains if due, then optimizes."""
        now = dt_util.now()

        # Check if training is due
        interval = self.config.get(CONF_TRAINING_INTERVAL_DAYS, DEFAULT_TRAINING_INTERVAL_DAYS)
        if self.params.last_trained is None or (
            now - self.params.last_trained > timedelta(days=interval)
        ):
            await self.async_train_model()

        next_training = (
            self.params.last_trained + timedelta(days=interval)
            if self.params.last_trained else now + timedelta(hours=1)
        )

        # Read current temperatures
        t_indoor = self._entity_float(self.config.get(CONF_INDOOR_TEMP_ENTITY, ""))
        if t_indoor is None:
            raise UpdateFailed("Indoor temperature sensor unavailable")

        t_outdoor = self._entity_float(self.config.get(CONF_OUTDOOR_TEMP_ENTITY, ""))
        if t_outdoor is None:
            t_outdoor = FALLBACK_OUTDOOR_TEMP

        # Build time slots — use weather forecast if available
        horizon = self.config.get(CONF_PREDICTION_HORIZON_HOURS, DEFAULT_PREDICTION_HORIZON_HOURS)
        timestep = self.config.get(CONF_OPTIMIZATION_TIMESTEP_MIN, DEFAULT_OPTIMIZATION_TIMESTEP_MIN)
        gas_price = self.config.get(CONF_GAS_PRICE, DEFAULT_GAS_PRICE)
        dt_s = timestep * 60.0
        n_slots = int(horizon * 60 / timestep)
        prices = self._get_prices()

        # Fetch weather forecast (non-blocking, falls back gracefully)
        forecast_available = False
        if self.weather is not None:
            try:
                forecast_available = await self.weather.async_update()
                if forecast_available:
                    _LOGGER.debug(
                        "Weather forecast available: %.0fh horizon from %s",
                        self.weather.horizon_hours, self.weather.entity_id,
                    )
            except Exception as err:
                _LOGGER.warning("Weather forecast fetch failed: %s", err)

        # Get HA latitude for solar calculations
        latitude = self.hass.config.latitude or 52.0

        slots = []
        for j in range(n_slots):
            slot_time = now + timedelta(seconds=j * dt_s)

            # Outdoor temperature: prefer forecast, fall back to current sensor
            if forecast_available and self.weather is not None:
                slot_t_outdoor = self.weather.temperature_at(slot_time)
                if slot_t_outdoor is None:
                    slot_t_outdoor = t_outdoor  # fall back to sensor
            else:
                slot_t_outdoor = t_outdoor

            # Solar gain: estimate from forecast cloud coverage
            solar_w = 0.0
            if forecast_available and self.weather is not None:
                cloud_pct = self.weather.cloud_coverage_at(slot_time)
                hour_of_day = slot_time.hour + slot_time.minute / 60.0
                day_of_year = slot_time.timetuple().tm_yday
                solar_w = estimate_solar_gain_from_forecast(
                    cloud_coverage_pct=cloud_pct,
                    hour_of_day=hour_of_day,
                    day_of_year=day_of_year,
                    latitude=latitude,
                )

            slots.append(SlotInput(
                start=slot_time,
                duration_s=dt_s,
                t_outdoor=slot_t_outdoor,
                t_target=self._get_target_temp(slot_time),
                electricity_price=self._price_at(prices, slot_time),
                gas_price=gas_price,
                solar_gain_w=solar_w,
                internal_gain_w=self.config.get(CONF_INTERNAL_GAIN_W, FALLBACK_INTERNAL_GAIN_W),
            ))

        # Run optimizer
        cop_coeffs = self.config.get(CONF_COP_COEFFICIENTS, [DEFAULT_COP_A, DEFAULT_COP_B])
        gas_eff = self.config.get(CONF_GAS_EFFICIENCY, DEFAULT_GAS_EFFICIENCY)
        away_temp = self.config.get(CONF_AWAY_TEMP, DEFAULT_AWAY_TEMP)

        self.last_optimization = await self.hass.async_add_executor_job(
            optimize_heating,
            self.params, self.devices, slots, t_indoor,
            cop_coeffs[0], cop_coeffs[1], gas_eff, away_temp,
        )

        if self.last_optimization.trace:
            self.last_optimize_trace = self.last_optimization.trace.summary()

        # Extract first-slot recommendations for each device
        first_slot_decisions = {}
        if self.last_optimization.slot_results:
            for d in self.last_optimization.slot_results[0].device_decisions:
                first_slot_decisions[d.device_name] = {
                    "recommended_state": d.output_pct > 0,
                    "recommended_output_pct": d.output_pct,
                    "recommended_setpoint": d.recommended_setpoint,
                    "heat_output_w": d.heat_output_w,
                    "cost_per_wh": round(d.cost_per_wh, 6),
                    "reason": d.reason,
                }

        # Auto-control: push setpoint to climate entities if enabled
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
            "weather_forecast_horizon_h": (
                round(self.weather.horizon_hours, 1)
                if self.weather and forecast_available else 0
            ),
            "solar_gain_current_w": round(slots[0].solar_gain_w, 0) if slots else 0,
        }

    async def _apply_setpoints(
        self, decisions: dict[str, dict],
    ) -> None:
        """Push recommended setpoints to climate entities.

        Only acts on climate.* entities (ignores switch/number).
        Logs every change so the user can verify behavior.
        """
        for device in self.devices:
            info = decisions.get(device.name)
            if info is None:
                continue

            entity_id = device.entity_id
            if not entity_id.startswith("climate."):
                continue

            setpoint = info.get("recommended_setpoint")
            if setpoint is None:
                continue

            # Read current setpoint to avoid unnecessary service calls
            state = self.hass.states.get(entity_id)
            if state is not None:
                current = state.attributes.get("temperature")
                if current is not None:
                    try:
                        if abs(float(current) - setpoint) < 0.1:
                            continue  # Already at the right setpoint
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
                _LOGGER.warning(
                    "Failed to set temperature on %s: %s", entity_id, err,
                )
