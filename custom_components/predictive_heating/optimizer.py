"""Heating optimizer — decides when to heat and when to save.

Simple greedy forward-pass:
  For each 15-min slot → simulate drift → compute deficit → turn heater on/off.

Cost is based purely on electricity price per kWh (no COP or gas calculations).
Price-based preheating: if the next slot is significantly more expensive,
heat a bit extra now to avoid running during the expensive slot.
"""

from __future__ import annotations

import logging

from .const import (
    COMFORT_PENALTY_WEIGHT,
    DEFAULT_AWAY_TEMP,
    ON_OFF_MIN_DUTY_CYCLE,
    PREHEAT_MAX_OVERSHOOT_K,
    PREHEAT_PRICE_RATIO,
)
from .model import (
    DeviceDecision,
    OptimizationResult,
    SimpleHeater,
    SlotInput,
    SlotResult,
    ThermalParams,
    compute_heat_deficit_wh,
    euler_step,
)
from .trace import Trace

_LOGGER = logging.getLogger(__name__)


def optimize_heating(
    params: ThermalParams,
    heaters: list[SimpleHeater],
    slots: list[SlotInput],
    t_initial: float,
    away_temp: float = DEFAULT_AWAY_TEMP,
) -> OptimizationResult:
    """Find a cheap heating plan that meets comfort targets.

    Greedy forward-pass: process each slot independently, cheapest-first
    within each slot (though with a single heater there's no ranking needed).

    Args:
        params: Fitted thermal model parameters (UA, C).
        heaters: List of simple on/off heaters with rated power.
        slots: Future time slots with outdoor temp and price.
        t_initial: Current indoor temperature.
        away_temp: Setpoint when not heating.
    """
    trace = Trace("optimize")
    trace.step("start", inputs={
        "t_initial": t_initial,
        "n_slots": len(slots),
        "n_heaters": len(heaters),
        "ua": params.ua,
        "thermal_mass": params.thermal_mass,
    })

    result = OptimizationResult(trace=trace)
    result.predicted_temperatures = [t_initial]
    t_current = t_initial

    for i, slot in enumerate(slots):
        slot_result = _optimize_one_slot(
            slot_index=i,
            slot=slot,
            next_slot=slots[i + 1] if i + 1 < len(slots) else None,
            t_current=t_current,
            params=params,
            heaters=heaters,
            away_temp=away_temp,
            trace=trace,
        )

        result.slot_results.append(slot_result)
        result.predicted_temperatures.append(slot_result.t_after)
        result.total_cost += slot_result.total_cost
        t_current = slot_result.t_after

    trace.step("done", result={
        "total_cost": round(result.total_cost, 4),
        "final_temperature": round(t_current, 2),
        "slots_heated": sum(1 for s in result.slot_results if s.total_heating_w > 0),
        "slots_total": len(slots),
    })

    return result


def _optimize_one_slot(
    slot_index: int,
    slot: SlotInput,
    next_slot: SlotInput | None,
    t_current: float,
    params: ThermalParams,
    heaters: list[SimpleHeater],
    away_temp: float,
    trace: Trace,
) -> SlotResult:
    """Optimize heating for a single time slot.

    Steps:
    1. Simulate temperature drift without heating
    2. Compute heat deficit to reach target
    3. Check if preheating is worthwhile (next slot more expensive?)
    4. Turn on heaters to cover the deficit, cheapest-first
    5. Simulate actual temperature with heating
    """
    dt = slot.duration_s

    # Step 1: Temperature drift without heating
    t_no_heat, _, _ = euler_step(
        t_current, slot.t_outdoor, params.ua, params.c_joules,
        q_heating_w=0.0,
        q_solar_w=slot.solar_gain_w,
        q_internal_w=slot.internal_gain_w,
        dt_seconds=dt,
    )

    # Step 2: Heat deficit
    deficit_wh = compute_heat_deficit_wh(params.c_joules, slot.t_target, t_no_heat)

    # Step 3: Preheat check — is next slot significantly more expensive?
    is_preheating = False
    preheat_extra_wh = 0.0
    if next_slot is not None and deficit_wh > 0:
        price_ratio = next_slot.electricity_price / max(slot.electricity_price, 0.001)
        if price_ratio > PREHEAT_PRICE_RATIO:
            preheat_extra_wh = (params.c_joules * PREHEAT_MAX_OVERSHOOT_K) / 3600.0
            is_preheating = True

    total_need_wh = deficit_wh + preheat_extra_wh

    # Step 4: Allocate heating across heaters (sorted by electricity price — same for all
    # since they all use electricity, but keeps the door open for multi-price scenarios)
    cost_per_wh = slot.electricity_price / 1000.0  # €/Wh (electricity only)
    decisions, _ = _allocate_to_heaters(heaters, total_need_wh, dt, cost_per_wh)

    # Set recommended setpoints
    for d in decisions:
        if d.heating_on:
            d.recommended_setpoint = round(
                slot.t_target + (PREHEAT_MAX_OVERSHOOT_K if is_preheating else 0.0), 1
            )
        else:
            d.recommended_setpoint = round(away_temp, 1)

    # Step 5: Simulate temperature with actual heating
    total_heating_w = sum(d.heat_output_w for d in decisions)
    t_after, _, _ = euler_step(
        t_current, slot.t_outdoor, params.ua, params.c_joules,
        q_heating_w=total_heating_w,
        q_solar_w=slot.solar_gain_w,
        q_internal_w=slot.internal_gain_w,
        dt_seconds=dt,
    )

    # Comfort penalty for undershoot
    undershoot = max(0.0, slot.t_target - t_after)
    comfort_cost = COMFORT_PENALTY_WEIGHT * (undershoot ** 2) * (dt / 3600.0)
    energy_cost = total_heating_w * (dt / 3600.0) * cost_per_wh
    total_cost = energy_cost + comfort_cost

    if slot_index == 0 or is_preheating or undershoot > 0.5:
        trace.step(f"slot_{slot_index}", inputs={
            "t_current": round(t_current, 2),
            "t_outdoor": round(slot.t_outdoor, 1),
            "t_target": round(slot.t_target, 1),
            "price": round(slot.electricity_price, 4),
        }, result={
            "t_no_heat": round(t_no_heat, 2),
            "t_after": round(t_after, 2),
            "deficit_wh": round(deficit_wh, 1),
            "total_heating_w": round(total_heating_w, 0),
            "is_preheating": is_preheating,
            "heaters": {d.device_name: "ON" if d.heating_on else "OFF" for d in decisions},
        }, note=(
            f"T {t_current:.1f}→{t_after:.1f}°C (target {slot.t_target:.1f}), "
            f"heating {total_heating_w:.0f}W, cost €{energy_cost:.4f}"
        ))

    return SlotResult(
        slot_index=slot_index,
        t_before=t_current,
        t_after=t_after,
        t_target=slot.t_target,
        t_without_heating=t_no_heat,
        heat_deficit_wh=deficit_wh,
        total_heating_w=total_heating_w,
        total_cost=total_cost,
        device_decisions=decisions,
        is_preheating=is_preheating,
    )


def _allocate_to_heaters(
    heaters: list[SimpleHeater],
    need_wh: float,
    dt_seconds: float,
    cost_per_wh: float,
) -> tuple[list[DeviceDecision], float]:
    """Turn on heaters to cover the heat need.

    All heaters are on/off only (no modulation). They are turned on in
    order until the deficit is covered. The minimum duty-cycle threshold
    prevents short-cycling: a heater only turns on if the need exceeds
    30% of what it can deliver in this slot.

    Returns (list of decisions, remaining unmet Wh).
    """
    decisions = []
    remaining = need_wh

    for heater in heaters:
        max_wh = heater.power_w * (dt_seconds / 3600.0)

        if remaining <= 0:
            decisions.append(DeviceDecision(
                device_name=heater.name,
                heating_on=False,
                heat_output_w=0.0,
                cost_per_wh=cost_per_wh,
                reason="no heat needed",
            ))
            continue

        threshold = max_wh * ON_OFF_MIN_DUTY_CYCLE
        if remaining >= threshold:
            decisions.append(DeviceDecision(
                device_name=heater.name,
                heating_on=True,
                heat_output_w=heater.power_w,
                cost_per_wh=cost_per_wh,
                reason=f"ON: need {remaining:.0f}Wh > threshold {threshold:.0f}Wh",
            ))
            remaining -= max_wh
        else:
            decisions.append(DeviceDecision(
                device_name=heater.name,
                heating_on=False,
                heat_output_w=0.0,
                cost_per_wh=cost_per_wh,
                reason=f"OFF: need {remaining:.0f}Wh < short-cycle threshold {threshold:.0f}Wh",
            ))

    return decisions, max(0.0, remaining)
