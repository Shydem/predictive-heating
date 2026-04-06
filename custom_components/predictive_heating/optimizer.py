"""Heating optimizer — decides what to heat and when.

Separated from the thermal model so each can be understood and tested alone.
Every decision is recorded in the trace with full reasoning.
"""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    COMFORT_PENALTY_WEIGHT,
    DEFAULT_AWAY_TEMP,
    J_PER_KWH,
    ON_OFF_MIN_DUTY_CYCLE,
    PREHEAT_MAX_OVERSHOOT_K,
    PREHEAT_PRICE_RATIO,
    SOURCE_ELECTRIC,
)
from .model import (
    DeviceDecision,
    HeatingDevice,
    OptimizationResult,
    SlotInput,
    SlotResult,
    ThermalParams,
    compute_cop,
    compute_heat_deficit_wh,
    device_cop,
    euler_step,
    heat_cost_per_wh,
)
from .trace import Trace

_LOGGER = logging.getLogger(__name__)


def optimize_heating(
    params: ThermalParams,
    devices: list[HeatingDevice],
    slots: list[SlotInput],
    t_initial: float,
    cop_a: float,
    cop_b: float,
    gas_efficiency: float,
    away_temp: float = DEFAULT_AWAY_TEMP,
) -> OptimizationResult:
    """Find the cheapest heating plan that meets comfort targets.

    This is a greedy forward-pass optimizer: for each time slot, it
    determines the heat deficit, ranks devices by cost, and allocates
    heating cheapest-first. It looks one slot ahead for preheating
    opportunities.

    Args:
        params: Fitted thermal model parameters.
        devices: Available heating devices.
        slots: Future time slots with weather/price data.
        t_initial: Current indoor temperature.
        cop_a, cop_b: COP linear coefficients.
        gas_efficiency: Gas boiler efficiency.

    Returns:
        OptimizationResult with full per-slot breakdown.
    """
    trace = Trace("optimize")
    trace.step("start", inputs={
        "t_initial": t_initial,
        "n_slots": len(slots),
        "n_devices": len(devices),
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
            devices=devices,
            cop_a=cop_a,
            cop_b=cop_b,
            gas_efficiency=gas_efficiency,
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
    devices: list[HeatingDevice],
    cop_a: float,
    cop_b: float,
    gas_efficiency: float,
    away_temp: float,
    trace: Trace,
) -> SlotResult:
    """Optimize heating for a single time slot.

    Steps:
    1. Simulate temperature drift without heating
    2. Compute heat deficit to reach target
    3. Check if preheating is worthwhile (next slot more expensive?)
    4. Rank devices by cost per Wh of heat
    5. Allocate heating cheapest-first
    6. Simulate actual temperature with allocated heating
    """
    dt = slot.duration_s

    # Step 1: Where does temperature drift without heating?
    t_no_heat, q_loss_w, q_net_drift = euler_step(
        t_current, slot.t_outdoor, params.ua, params.c_joules,
        q_heating_w=0.0,
        q_solar_w=slot.solar_gain_w,
        q_internal_w=slot.internal_gain_w,
        dt_seconds=dt,
    )

    # Step 2: How much heat do we need?
    deficit_wh = compute_heat_deficit_wh(
        params.c_joules, slot.t_target, t_no_heat,
    )

    # Step 3: Should we preheat?
    is_preheating = False
    preheat_extra_wh = 0.0
    preheat_reason = ""
    if next_slot is not None and deficit_wh > 0:
        price_ratio = next_slot.electricity_price / max(slot.electricity_price, 0.001)
        if price_ratio > PREHEAT_PRICE_RATIO:
            extra_j = params.c_joules * PREHEAT_MAX_OVERSHOOT_K
            preheat_extra_wh = extra_j / 3600.0
            is_preheating = True
            preheat_reason = (
                f"Next slot {price_ratio:.1f}× more expensive "
                f"(€{slot.electricity_price:.3f} → €{next_slot.electricity_price:.3f}), "
                f"preheating {PREHEAT_MAX_OVERSHOOT_K}°C"
            )

    total_need_wh = deficit_wh + preheat_extra_wh

    # Step 4: Rank devices by cost (COP computed per device)
    ranked = _rank_devices_by_cost(
        devices, slot.electricity_price, slot.gas_price,
        slot.t_outdoor, cop_a, cop_b, gas_efficiency,
    )

    # Step 5: Allocate heating
    decisions, remaining_wh = _allocate_heating(
        ranked, total_need_wh, dt, trace, slot_index,
    )

    # Step 5b: Compute recommended thermostat setpoint per device
    for d in decisions:
        if d.output_pct > 0:
            if is_preheating:
                d.recommended_setpoint = round(slot.t_target + PREHEAT_MAX_OVERSHOOT_K, 1)
            else:
                d.recommended_setpoint = round(slot.t_target, 1)
        else:
            d.recommended_setpoint = round(away_temp, 1)

    # Step 6: Compute actual temperature with allocated heating
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
    energy_cost = sum(
        d.heat_output_w * (dt / 3600.0) * d.cost_per_wh for d in decisions
    )
    total_cost = energy_cost + comfort_cost

    # Trace: only log first slot in detail (the one that matters for control),
    # plus any slot with warnings
    if slot_index == 0 or is_preheating or undershoot > 0.5:
        trace.step(f"slot_{slot_index}", inputs={
            "t_current": round(t_current, 2),
            "t_outdoor": round(slot.t_outdoor, 1),
            "t_target": round(slot.t_target, 1),
            "elec_price": round(slot.electricity_price, 4),
        }, result={
            "t_no_heat": round(t_no_heat, 2),
            "t_after": round(t_after, 2),
            "deficit_wh": round(deficit_wh, 1),
            "total_heating_w": round(total_heating_w, 0),
            "energy_cost": round(energy_cost, 4),
            "comfort_cost": round(comfort_cost, 4),
            "is_preheating": is_preheating,
            "devices": {d.device_name: f"{d.recommended_setpoint}°C ({d.reason})" for d in decisions},
        }, note=(
            f"{'PREHEAT: ' + preheat_reason + ' | ' if is_preheating else ''}"
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


def _rank_devices_by_cost(
    devices: list[HeatingDevice],
    elec_price: float,
    gas_price: float,
    t_outdoor: float,
    cop_a: float,
    cop_b: float,
    gas_efficiency: float,
) -> list[tuple[HeatingDevice, float]]:
    """Rank devices by cost per Wh of heat delivered, cheapest first.

    COP is computed per device from its own data points (or the legacy
    linear model as fallback).

    Returns list of (device, cost_per_wh) tuples.
    """
    ranked = []
    for dev in devices:
        cop = device_cop(dev, t_outdoor, cop_a, cop_b)
        cost = heat_cost_per_wh(
            dev.energy_source, elec_price, gas_price, cop, gas_efficiency,
        )
        ranked.append((dev, cost))
    ranked.sort(key=lambda x: x[1])
    return ranked


def _allocate_heating(
    ranked_devices: list[tuple[HeatingDevice, float]],
    need_wh: float,
    dt_seconds: float,
    trace: Trace,
    slot_index: int,
) -> tuple[list[DeviceDecision], float]:
    """Allocate heating to devices, cheapest first.

    For on/off devices: only turn on if need > ON_OFF_MIN_DUTY_CYCLE of capacity.
    For stepless: calculate exact output percentage.

    Returns (list of decisions, remaining unmet Wh).
    """
    decisions = []
    remaining = need_wh

    for dev, cost_per_wh in ranked_devices:
        max_wh = dev.max_output_w * (dt_seconds / 3600.0)

        if remaining <= 0:
            decisions.append(DeviceDecision(
                device_name=dev.name,
                output_pct=0.0,
                heat_output_w=0.0,
                cost_per_wh=cost_per_wh,
                energy_source=dev.energy_source,
                reason="no heat needed",
            ))
            continue

        if dev.device_type == "on_off":
            # Only turn on if need exceeds minimum duty cycle
            threshold = max_wh * ON_OFF_MIN_DUTY_CYCLE
            if remaining >= threshold:
                output_wh = max_wh
                decisions.append(DeviceDecision(
                    device_name=dev.name,
                    output_pct=100.0,
                    heat_output_w=dev.max_output_w,
                    cost_per_wh=cost_per_wh,
                    energy_source=dev.energy_source,
                    reason=f"ON: need {remaining:.0f}Wh > threshold {threshold:.0f}Wh",
                ))
            else:
                output_wh = 0.0
                decisions.append(DeviceDecision(
                    device_name=dev.name,
                    output_pct=0.0,
                    heat_output_w=0.0,
                    cost_per_wh=cost_per_wh,
                    energy_source=dev.energy_source,
                    reason=f"OFF: need {remaining:.0f}Wh < threshold {threshold:.0f}Wh (short-cycle prevention)",
                ))
        else:
            # Stepless: proportional output
            output_wh = min(remaining, max_wh)
            pct = (output_wh / max_wh * 100.0) if max_wh > 0 else 0.0
            power_w = dev.max_output_w * (pct / 100.0)
            decisions.append(DeviceDecision(
                device_name=dev.name,
                output_pct=round(pct, 1),
                heat_output_w=round(power_w, 0),
                cost_per_wh=cost_per_wh,
                energy_source=dev.energy_source,
                reason=f"MODULATE: {pct:.1f}% = {output_wh:.0f}Wh of {max_wh:.0f}Wh capacity",
            ))

        remaining -= output_wh

    return decisions, max(0.0, remaining)
