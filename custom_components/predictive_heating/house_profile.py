"""Estimate initial thermal model parameters from house characteristics.

Used during cold start when there isn't enough historical data for
a proper model fit. These estimates are rough but much better than
blind defaults — they typically get UA within ±30% of the true value,
which is good enough for useful recommendations from day one.

The estimates are overwritten by the first proper training run once
enough data has accumulated.

Reference values are based on Dutch housing stock (ISSO, RVO, AgentschapNL)
and validated against typical energy label data.
"""

from __future__ import annotations

import logging

from .const import (
    HOUSE_TYPE_APARTMENT,
    HOUSE_TYPE_DETACHED,
    HOUSE_TYPE_SEMI_DETACHED,
    HOUSE_TYPE_TERRACED,
    INSULATION_EXCELLENT,
    INSULATION_GOOD,
    INSULATION_MODERATE,
    INSULATION_POOR,
    THERMAL_MASS_HEAVY,
    THERMAL_MASS_LIGHT,
    THERMAL_MASS_MEDIUM,
)

_LOGGER = logging.getLogger(__name__)

# ── Specific heat loss per m² floor area (W/m²K) ─────────────────────────
# These combine fabric losses (walls, roof, floor, windows) and ventilation
# losses into a single number per m² of floor area.
#
# Source: typical Dutch energy label calculations, ISSO 82.3
# Assumes 2.6m ceiling height, standard window-to-wall ratio (~20-25%)

_SPECIFIC_HEAT_LOSS: dict[str, float] = {
    INSULATION_POOR: 3.0,       # Pre-1975: single glazing, uninsulated cavity
    INSULATION_MODERATE: 2.0,   # 1975-2000: double glazing, some cavity fill
    INSULATION_GOOD: 1.2,       # Post-2000: HR++ glass, full insulation
    INSULATION_EXCELLENT: 0.6,  # Passive house: triple glass, no thermal bridges
}

# ── Exposed surface multiplier by house type ─────────────────────────────
# A detached house has 4 exposed walls + roof. A mid-terrace has
# only 2 walls + roof. This multiplier adjusts UA accordingly.

_EXPOSURE_FACTOR: dict[str, float] = {
    HOUSE_TYPE_DETACHED: 1.0,
    HOUSE_TYPE_SEMI_DETACHED: 0.80,
    HOUSE_TYPE_TERRACED: 0.65,
    HOUSE_TYPE_APARTMENT: 0.50,  # Middle apartment; corner = ~0.65
}

# ── Thermal mass per m² floor area (Wh/m²K) ─────────────────────────────
# This is the effective thermal capacitance that participates in
# short-term temperature swings (the first ~10cm of walls/floors).
#
# Source: EN ISO 13786, simplified for residential

_THERMAL_MASS_PER_M2: dict[str, float] = {
    THERMAL_MASS_LIGHT: 30.0,   # Timber frame, stud walls, suspended floor
    THERMAL_MASS_MEDIUM: 60.0,  # Brick cavity walls, screed floor
    THERMAL_MASS_HEAVY: 100.0,  # Solid brick/stone, concrete floors, plaster
}


def estimate_initial_params(
    floor_area_m2: float,
    house_type: str,
    insulation: str,
    thermal_mass_class: str,
) -> tuple[float, float, str]:
    """Estimate UA (W/K) and thermal mass (kWh/K) from house profile.

    Args:
        floor_area_m2: Total heated floor area
        house_type: One of detached, semi_detached, terraced, apartment
        insulation: One of poor, moderate, good, excellent
        thermal_mass_class: One of light, medium, heavy

    Returns:
        (estimated_ua, estimated_thermal_mass_kwh_k, explanation_string)
    """
    # Look up coefficients with safe fallbacks
    specific_loss = _SPECIFIC_HEAT_LOSS.get(insulation, 2.0)
    exposure = _EXPOSURE_FACTOR.get(house_type, 0.8)
    mass_per_m2 = _THERMAL_MASS_PER_M2.get(thermal_mass_class, 60.0)

    # UA = specific_loss × floor_area × exposure_factor
    ua = specific_loss * floor_area_m2 * exposure

    # C = mass_per_m2 × floor_area / 1000 (Wh/K → kWh/K)
    thermal_mass = mass_per_m2 * floor_area_m2 / 1000.0

    explanation = (
        f"Estimated from {floor_area_m2:.0f}m² {house_type} house, "
        f"{insulation} insulation, {thermal_mass_class} thermal mass. "
        f"UA = {specific_loss:.1f} W/m²K × {floor_area_m2:.0f} m² × "
        f"{exposure:.2f} exposure = {ua:.0f} W/K. "
        f"C = {mass_per_m2:.0f} Wh/m²K × {floor_area_m2:.0f} m² = "
        f"{thermal_mass:.1f} kWh/K. "
        f"These are starting estimates — they'll be refined by the model "
        f"trainer once 1-2 weeks of data are available."
    )

    _LOGGER.info(
        "House profile estimate: UA=%.0f W/K, C=%.1f kWh/K (%s, %s, %s, %.0fm²)",
        ua, thermal_mass, house_type, insulation, thermal_mass_class, floor_area_m2,
    )

    return ua, thermal_mass, explanation
