"""Data collection from HA recorder for model training.

Simple approach: read temperature history and heater on/off state history.
No gas meters, no electricity meters, no COP curves required.

Heat input per timestep = sum(power_w for each heater that was ON)

This works because:
  - We know each heater's rated power (from config)
  - We read when each heater was on/off from HA recorder
  - Together: Q_heating = Σ (heater_on × power_w)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_ENTITY,
    CONF_DEVICE_POWER_W,
    CONF_GAS_CONSUMPTION_ENTITY,
    CONF_GAS_EFFICIENCY,
    CONF_HEATING_DEVICES,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INTERNAL_GAIN_W,
    CONF_OUTDOOR_TEMP_ENTITY,
    DEFAULT_GAS_EFFICIENCY,
    FALLBACK_INTERNAL_GAIN_W,
    FALLBACK_OUTDOOR_TEMP,
    GAS_KWH_PER_M3,
)
from .trace import Trace

_LOGGER = logging.getLogger(__name__)


@dataclass
class DataQuality:
    """Report on the quality of collected data."""

    total_intervals: int = 0
    valid_intervals: int = 0
    gaps: list[str] = field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        if self.total_intervals == 0:
            return 0.0
        return (self.valid_intervals / self.total_intervals) * 100.0


@dataclass
class TrainingData:
    """Validated, resampled data ready for model training."""

    timestamps: list[datetime] = field(default_factory=list)
    t_indoor: list[float] = field(default_factory=list)
    t_outdoor: list[float] = field(default_factory=list)
    q_heating_w: list[float] = field(default_factory=list)
    q_solar_w: list[float] = field(default_factory=list)
    q_internal_w: list[float] = field(default_factory=list)
    quality: DataQuality = field(default_factory=DataQuality)

    # Phase 1 metadata
    q_heating_source: str = "heater_onoff"
    """How Q_heating was derived: 'gas' or 'heater_onoff'."""

    # Sampled arrays for dashboard visualization (max VIZ_MAX_POINTS)
    viz_timestamps: list[str] = field(default_factory=list)
    viz_t_indoor: list[float] = field(default_factory=list)
    viz_t_outdoor: list[float] = field(default_factory=list)
    viz_q_heating_w: list[float] = field(default_factory=list)

    @property
    def n_points(self) -> int:
        return len(self.timestamps)


def _safe_float(value: Any) -> float | None:
    """Convert state value to float, returning None on any failure."""
    try:
        v = float(value)
        return v if v == v else None  # NaN check
    except (ValueError, TypeError):
        return None


def _is_heater_on(state_str: str) -> bool:
    """Check if a HA state string means the heater is actively heating.

    Handles switch (on/off), climate (heat/heating/cool/idle/off),
    binary_sensor (on/off), and boolean representations.
    """
    return str(state_str).lower() in (
        "on", "heat", "heating", "auto", "true", "1",
    )


def _interpolate_temp(
    series: list[tuple[datetime, float]], target: datetime
) -> float | None:
    """Linear interpolation of a sorted temperature time series."""
    if not series:
        return None
    if target <= series[0][0]:
        return series[0][1]
    if target >= series[-1][0]:
        return series[-1][1]
    for i in range(1, len(series)):
        if series[i][0] >= target:
            t0, v0 = series[i - 1]
            t1, v1 = series[i]
            elapsed = max((t1 - t0).total_seconds(), 1.0)
            frac = (target - t0).total_seconds() / elapsed
            return v0 + frac * (v1 - v0)
    return series[-1][1]


def _last_known_state(
    series: list[tuple[datetime, str]], target: datetime
) -> str | None:
    """Find the most recent state at or before target time (step interpolation).

    Binary state is step-interpolated: the last known value holds until
    the next state change.
    """
    result = None
    for ts, val in series:
        if ts <= target:
            result = val
        else:
            break
    return result


_VIZ_MAX_POINTS = 150
"""Max data points to store for dashboard visualization (keeps attrs under 16KB)."""


async def collect_training_data(
    hass: HomeAssistant,
    config: dict[str, Any],
    window_days: int = 30,
    resample_minutes: int = 15,
    trace: Trace | None = None,
) -> TrainingData:
    """Collect temperature and heater state history for model training.

    Data pipeline:
    1. Read indoor/outdoor temperature history from recorder
    2a. If gas_consumption_entity configured: derive Q_heating from gas delta (Phase 1)
    2b. Otherwise: read heater on/off state history × rated power (fallback)
    3. Resample everything to a uniform time grid
    4. Store sampled arrays for dashboard visualization
    """
    if trace is None:
        trace = Trace("data_collect")

    now = dt_util.now()
    start = now - timedelta(days=window_days)

    def _normalize_entity_id(value: Any) -> str:
        if isinstance(value, list):
            return value[0] if value else ""
        return str(value) if value else ""

    indoor_entity = _normalize_entity_id(config[CONF_INDOOR_TEMP_ENTITY])
    outdoor_entity = _normalize_entity_id(config.get(CONF_OUTDOOR_TEMP_ENTITY, ""))
    internal_gain_w = config.get(CONF_INTERNAL_GAIN_W, FALLBACK_INTERNAL_GAIN_W)

    # Phase 1: optional gas consumption entity
    gas_entity = _normalize_entity_id(config.get(CONF_GAS_CONSUMPTION_ENTITY, ""))
    gas_efficiency = float(config.get(CONF_GAS_EFFICIENCY, DEFAULT_GAS_EFFICIENCY))
    use_gas = bool(gas_entity)

    # Heater devices: list of (entity_id, power_w) — used as fallback when no gas entity
    heaters: list[tuple[str, float]] = []
    if not use_gas:
        for dev in config.get(CONF_HEATING_DEVICES, []):
            eid = _normalize_entity_id(dev.get(CONF_DEVICE_ENTITY, ""))
            power = float(dev.get(CONF_DEVICE_POWER_W, 0.0))
            if eid and power > 0:
                heaters.append((eid, power))

    # Build entity list to fetch
    entities_to_fetch = [indoor_entity, outdoor_entity]
    if use_gas:
        entities_to_fetch.append(gas_entity)
    else:
        entities_to_fetch.extend(h[0] for h in heaters)

    trace.step("fetch_start", inputs={
        "indoor": indoor_entity,
        "outdoor": outdoor_entity,
        "q_heating_source": "gas" if use_gas else "heater_onoff",
        "gas_entity": gas_entity if use_gas else None,
        "gas_efficiency": gas_efficiency if use_gas else None,
        "heaters": [f"{eid} ({pw}W)" for eid, pw in heaters] if not use_gas else [],
        "window": f"{window_days} days",
        "resample": f"{resample_minutes} min",
    })

    # Fetch all entity histories from recorder
    raw_history: dict[str, list] = {}
    for entity_id in entities_to_fetch:
        if not entity_id:
            continue
        try:
            result = await get_instance(hass).async_add_executor_job(
                state_changes_during_period, hass, start, now, entity_id,
            )
            raw_history[entity_id] = result.get(entity_id, [])
        except Exception as err:
            _LOGGER.warning("Failed to fetch history for %s: %s", entity_id, err)
            raw_history[entity_id] = []

    # Parse temperature histories into sorted (datetime, float) series
    def parse_temp_series(entity_id: str) -> list[tuple[datetime, float]]:
        points = []
        for state in raw_history.get(entity_id, []):
            val = _safe_float(state.state)
            if val is not None:
                ts = getattr(state, "last_updated", None) or state.last_changed
                points.append((ts, val))
        series = sorted(points, key=lambda x: x[0])
        trace.step(f"parsed_temp_{entity_id.split('.')[-1]}", result={
            "valid_points": len(series),
            "bad_points": len(raw_history.get(entity_id, [])) - len(series),
        })
        return series

    # Parse heater state histories into sorted (datetime, str) series
    def parse_state_series(entity_id: str) -> list[tuple[datetime, str]]:
        points = []
        for state in raw_history.get(entity_id, []):
            if state.state not in ("unknown", "unavailable", None):
                ts = getattr(state, "last_updated", None) or state.last_changed
                points.append((ts, str(state.state)))
        series = sorted(points, key=lambda x: x[0])
        trace.step(f"parsed_state_{entity_id.split('.')[-1]}", result={
            "state_changes": len(series),
            "on_periods": sum(1 for _, s in series if _is_heater_on(s)),
        })
        return series

    indoor_series = parse_temp_series(indoor_entity)
    outdoor_series = parse_temp_series(outdoor_entity)

    if not indoor_series:
        trace.error("no_indoor_data", f"No valid data from indoor sensor {indoor_entity}")

    if not outdoor_entity:
        trace.step("no_outdoor_entity",
            result={"fallback": FALLBACK_OUTDOOR_TEMP},
            note=f"No outdoor sensor configured — using {FALLBACK_OUTDOOR_TEMP}°C constant. "
                 "Add an outdoor sensor in Settings → Integrations → Predictive Heating for better accuracy.")
    elif not outdoor_series:
        trace.warn("no_outdoor_data",
            f"No valid data from outdoor sensor {outdoor_entity} — using {FALLBACK_OUTDOOR_TEMP}°C fallback.")

    # Gas series: cumulative m³ counter (most common: DSMR/P1 integration)
    gas_series: list[tuple[datetime, float]] = []
    if use_gas:
        gas_series = parse_temp_series(gas_entity)
        if not gas_series:
            trace.warn("no_gas_data",
                f"No valid data from gas entity {gas_entity}. "
                "Check entity ID and that gas sensor has history.")

    # Heater on/off series (only when no gas entity)
    heater_series: list[tuple[list[tuple[datetime, str]], float]] = []
    if not use_gas:
        heater_series = [
            (parse_state_series(eid), power_w)
            for eid, power_w in heaters
        ]
        if not heater_series:
            trace.warn("no_heaters", "No heater entities configured. UA fit may be unreliable.")

    # Build uniform time grid
    dt_s = resample_minutes * 60.0
    dt_hours = resample_minutes / 60.0
    grid_times: list[datetime] = []
    t = start
    while t <= now:
        grid_times.append(t)
        t += timedelta(minutes=resample_minutes)

    # Resample onto grid
    data = TrainingData()
    data.quality.total_intervals = len(grid_times) - 1
    data.q_heating_source = "gas" if use_gas else "heater_onoff"

    for i in range(len(grid_times) - 1):
        t_start = grid_times[i]

        t_in = _interpolate_temp(indoor_series, t_start)
        # Outdoor: use sensor data if available, else fallback constant
        t_out = _interpolate_temp(outdoor_series, t_start) if outdoor_series else None
        if t_out is None:
            t_out = FALLBACK_OUTDOOR_TEMP

        if t_in is None:
            data.quality.gaps.append(t_start.isoformat())
            continue

        # ── Q_heating from gas consumption (Phase 1 primary source) ──────────
        if use_gas and gas_series:
            # Read cumulative counter at start and end of this slot
            t_end = grid_times[i + 1]
            v_start = _interpolate_temp(gas_series, t_start)
            v_end = _interpolate_temp(gas_series, t_end)
            if v_start is not None and v_end is not None:
                delta_m3 = max(0.0, v_end - v_start)  # m³ consumed this slot
                # Convert: m³ → kWh → W (average power over the slot)
                q_heating = (delta_m3 / dt_hours) * GAS_KWH_PER_M3 * gas_efficiency * 1000.0
            else:
                q_heating = 0.0

        # ── Q_heating from heater on/off states (fallback) ───────────────────
        else:
            q_heating = 0.0
            for states, power_w in heater_series:
                state_at_t = _last_known_state(states, t_start)
                if state_at_t is not None and _is_heater_on(state_at_t):
                    q_heating += power_w

        data.timestamps.append(t_start)
        data.t_indoor.append(t_in)
        data.t_outdoor.append(t_out)
        data.q_heating_w.append(q_heating)
        data.q_solar_w.append(0.0)  # absorbed into UA/C fit; improves with weather entity
        data.q_internal_w.append(internal_gain_w)
        data.quality.valid_intervals += 1

    trace.step("resample_done", result={
        "valid_points": data.n_points,
        "total_intervals": data.quality.total_intervals,
        "coverage": f"{data.quality.coverage_pct:.1f}%",
        "gaps": len(data.quality.gaps),
        "mean_heating_w": (
            f"{sum(data.q_heating_w) / max(len(data.q_heating_w), 1):.0f} W"
        ),
        "q_heating_source": data.q_heating_source,
    }, note=f"Collected {data.n_points} points, {data.quality.coverage_pct:.0f}% coverage")

    if data.quality.coverage_pct < 50:
        trace.warn("low_coverage",
            f"Only {data.quality.coverage_pct:.0f}% data coverage. "
            "Check if temperature sensors were offline.")

    if all(q == 0 for q in data.q_heating_w):
        source_hint = (
            f"gas entity {gas_entity} returned only zeros"
            if use_gas else
            "either no heaters are configured, the heater was off the entire window, "
            "or the entity state does not match the expected on/off pattern"
        )
        trace.warn("no_heating_detected",
            f"All Q_heating values are 0. Possible cause: {source_hint}.")

    # Build sampled viz arrays for dashboard sensors (stay under 16KB)
    n = data.n_points
    if n > 0:
        step = max(1, n // _VIZ_MAX_POINTS)
        idxs = range(0, n, step)
        data.viz_timestamps = [data.timestamps[i].isoformat() for i in idxs]
        data.viz_t_indoor = [round(data.t_indoor[i], 2) for i in idxs]
        data.viz_t_outdoor = [round(data.t_outdoor[i], 2) for i in idxs]
        data.viz_q_heating_w = [round(data.q_heating_w[i], 0) for i in idxs]

    return data
