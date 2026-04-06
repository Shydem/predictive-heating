"""Data collection from HA recorder for model training.

Pulls historical state data, validates it, resamples to even intervals,
and computes derived quantities (gas heat, HP heat, internal gains).
Reports data quality issues in the trace.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COP_COEFFICIENTS,
    CONF_GAS_CONSUMPTION_ENTITY,
    CONF_GAS_EFFICIENCY,
    CONF_HEATING_HOT_WATER_FRACTION,
    CONF_HEATPUMP_ELECTRICITY_ENTITY,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_INTERNAL_GAIN_W,
    CONF_OUTDOOR_ELECTRIC_LOADS_W,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_TOTAL_ELECTRICITY_ENTITY,
    DEFAULT_COP_A,
    DEFAULT_COP_B,
    DEFAULT_GAS_EFFICIENCY,
    DEFAULT_HEATING_HOT_WATER_FRACTION,
    DEFAULT_OUTDOOR_ELECTRIC_LOADS_W,
    FALLBACK_INTERNAL_GAIN_W,
    GAS_KWH_PER_M3,
    INDOOR_ELEC_HEAT_FRACTION,
    J_PER_KWH,
)
from .model import compute_cop
from .trace import Trace

_LOGGER = logging.getLogger(__name__)


from dataclasses import dataclass, field


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


def _interpolate(series: list[tuple[datetime, float]], target: datetime) -> float | None:
    """Linear interpolation of a sorted time series at a target time."""
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


def _cumulative_diff(
    series: list[tuple[datetime, float]], t_start: datetime, t_end: datetime
) -> float:
    """Delta of a cumulative sensor between two times. Returns 0 on failure."""
    v0 = _interpolate(series, t_start)
    v1 = _interpolate(series, t_end)
    if v0 is None or v1 is None:
        return 0.0
    return max(0.0, v1 - v0)  # cumulative meters should only go up


async def collect_training_data(
    hass: HomeAssistant,
    config: dict[str, Any],
    window_days: int = 30,
    resample_minutes: int = 15,
    trace: Trace | None = None,
) -> TrainingData:
    """Collect, validate, and resample historical data for training.

    Returns TrainingData with aligned arrays + quality report.
    """
    if trace is None:
        trace = Trace("data_collect")

    # Use dt_util.now() to get timezone-aware datetime (matches recorder data)
    now = dt_util.now()
    start = now - timedelta(days=window_days)

    def _normalize_entity_id(value: Any) -> str:
        """Normalize entity ID to string (handle multi-select lists)."""
        if isinstance(value, list):
            return value[0] if value else ""
        return str(value) if value else ""

    entity_map = {
        "indoor": _normalize_entity_id(config[CONF_INDOOR_TEMP_ENTITY]),
        "outdoor": _normalize_entity_id(config[CONF_OUTDOOR_TEMP_ENTITY]),
    }
    gas_entity = _normalize_entity_id(config.get(CONF_GAS_CONSUMPTION_ENTITY, ""))
    if gas_entity:
        entity_map["gas"] = gas_entity
    elec_entity = _normalize_entity_id(config.get(CONF_TOTAL_ELECTRICITY_ENTITY, ""))
    if elec_entity:
        entity_map["elec_total"] = elec_entity
    hp_entity = _normalize_entity_id(config.get(CONF_HEATPUMP_ELECTRICITY_ENTITY, ""))
    if hp_entity:
        entity_map["hp_elec"] = hp_entity

    trace.step("fetch_start", inputs={
        "entities": entity_map,
        "window": f"{window_days} days ({start.isoformat()} → {now.isoformat()})",
        "resample": f"{resample_minutes} min",
    })

    # Fetch from recorder (pass only non-empty entity IDs, one at a time)
    history: dict[str, list] = {}
    for label, entity_id in entity_map.items():
        if not entity_id:
            continue
        try:
            entity_history = await get_instance(hass).async_add_executor_job(
                state_changes_during_period, hass, start, now, entity_id,
            )
            history[entity_id] = entity_history.get(entity_id, [])
        except Exception as err:
            _LOGGER.warning(f"Failed to fetch history for {entity_id}: {err}")
            history[entity_id] = []

    # Parse into sorted (timestamp, float) series
    raw: dict[str, list[tuple[datetime, float]]] = {}
    for label, entity_id in entity_map.items():
        states = history.get(entity_id, [])
        points = []
        bad_count = 0
        for state in states:
            val = _safe_float(state.state)
            if val is not None:
                ts = getattr(state, "last_updated", None) or state.last_changed
                points.append((ts, val))
            else:
                bad_count += 1
        raw[label] = sorted(points, key=lambda x: x[0])

        trace.step(f"parsed_{label}", result={
            "entity": entity_id,
            "valid_points": len(points),
            "bad_points": bad_count,
            "time_range": (
                f"{points[0][0].isoformat()} → {points[-1][0].isoformat()}"
                if points else "no data"
            ),
        })

        if not points:
            trace.error(f"no_data_{label}", f"Entity {entity_id} returned no valid data")

    # Build even time grid
    dt_s = resample_minutes * 60.0
    grid_times = []
    t = start
    while t <= now:
        grid_times.append(t)
        t += timedelta(minutes=resample_minutes)

    # Config values
    hw_frac = config.get(CONF_HEATING_HOT_WATER_FRACTION, DEFAULT_HEATING_HOT_WATER_FRACTION)
    gas_eff = config.get(CONF_GAS_EFFICIENCY, DEFAULT_GAS_EFFICIENCY)
    cop_coeffs = config.get(CONF_COP_COEFFICIENTS, [DEFAULT_COP_A, DEFAULT_COP_B])
    outdoor_loads_w = config.get(CONF_OUTDOOR_ELECTRIC_LOADS_W, DEFAULT_OUTDOOR_ELECTRIC_LOADS_W)
    configured_internal_gain_w = config.get(CONF_INTERNAL_GAIN_W, FALLBACK_INTERNAL_GAIN_W)

    # Resample and compute derived quantities
    data = TrainingData()
    data.quality.total_intervals = len(grid_times) - 1

    for i in range(len(grid_times) - 1):
        t_start = grid_times[i]
        t_end = grid_times[i + 1]

        t_in = _interpolate(raw.get("indoor", []), t_start)
        t_out = _interpolate(raw.get("outdoor", []), t_start)

        if t_in is None or t_out is None:
            data.quality.gaps.append(t_start.isoformat())
            continue

        # Gas → heating power (skip interval if gas data missing)
        gas_series = raw.get("gas", [])
        gas_m3 = _cumulative_diff(gas_series, t_start, t_end)
        has_gas_data = len(gas_series) > 0 and _interpolate(gas_series, t_start) is not None

        # Heat pump → heating power (skip interval if HP data missing)
        hp_series = raw.get("hp_elec", [])
        hp_kwh = _cumulative_diff(hp_series, t_start, t_end)
        has_hp_data = len(hp_series) > 0 and _interpolate(hp_series, t_start) is not None

        # Total electricity (skip interval if missing)
        elec_series = raw.get("elec_total", [])
        total_kwh = _cumulative_diff(elec_series, t_start, t_end)
        has_elec_data = len(elec_series) > 0 and _interpolate(elec_series, t_start) is not None

        # If ALL energy sources are missing, skip — we can't know heating input
        if not has_gas_data and not has_hp_data:
            data.quality.gaps.append(t_start.isoformat())
            continue

        gas_heat_kwh = gas_m3 * GAS_KWH_PER_M3 * hw_frac * gas_eff if has_gas_data else 0.0
        gas_heat_w = (gas_heat_kwh * J_PER_KWH) / dt_s

        cop = compute_cop(t_out, cop_coeffs[0], cop_coeffs[1])
        hp_heat_w = (hp_kwh * cop * J_PER_KWH) / dt_s if has_hp_data else 0.0

        # Internal gains from electricity
        if has_elec_data:
            outdoor_kwh = outdoor_loads_w * dt_s / J_PER_KWH
            indoor_kwh = max(0.0, total_kwh - (hp_kwh if has_hp_data else 0.0) - outdoor_kwh)
            internal_w = (indoor_kwh * INDOOR_ELEC_HEAT_FRACTION * J_PER_KWH) / dt_s
        else:
            internal_w = configured_internal_gain_w

        data.timestamps.append(t_start)
        data.t_indoor.append(t_in)
        data.t_outdoor.append(t_out)
        data.q_heating_w.append(gas_heat_w + hp_heat_w)
        data.q_solar_w.append(0.0)  # not measured, absorbed into UA/C fit
        data.q_internal_w.append(internal_w)
        data.quality.valid_intervals += 1

    trace.step("resample_done", result={
        "valid_points": data.n_points,
        "total_intervals": data.quality.total_intervals,
        "coverage": f"{data.quality.coverage_pct:.1f}%",
        "gaps": len(data.quality.gaps),
    }, note=f"Collected {data.n_points} points, {data.quality.coverage_pct:.0f}% coverage")

    if data.quality.coverage_pct < 50:
        trace.warn("low_coverage",
            f"Only {data.quality.coverage_pct:.0f}% data coverage. "
            "Training may be inaccurate. Check if sensors were offline.")

    return data
