"""
Solar irradiance estimation from Home Assistant's sun.sun entity.

Estimates Global Horizontal Irradiance (GHI) using a clear-sky model
based on sun elevation, with optional cloud cover correction from
a weather entity.

Clear-sky GHI model (simplified Haurwitz, 1945):
    GHI_clear = 1098 * sin(elevation) * exp(-0.057 / sin(elevation))

With cloud correction:
    GHI = GHI_clear * (1 - 0.75 * cloud_cover^3.4)

This is a simple but effective model. RoomMind uses a similar approach
with DIN 4108-2 corrections; we keep it simpler for now.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Maximum solar irradiance at Earth's surface (W/m2)
SOLAR_CONSTANT_SURFACE = 1098.0


def estimate_solar_irradiance(hass: HomeAssistant) -> float:
    """
    Estimate current solar irradiance (W/m2) using HA's sun entity.

    Returns 0.0 if sun entity is unavailable or sun is below horizon.
    """
    sun_state = hass.states.get("sun.sun")
    if sun_state is None:
        return 0.0

    elevation = sun_state.attributes.get("elevation", 0.0)
    if elevation <= 0:
        return 0.0

    # Clear-sky GHI using Haurwitz model
    elevation_rad = math.radians(elevation)
    sin_elev = math.sin(elevation_rad)

    if sin_elev <= 0:
        return 0.0

    ghi_clear = SOLAR_CONSTANT_SURFACE * sin_elev * math.exp(
        -0.057 / sin_elev
    )

    # Try to get cloud cover from weather entity
    cloud_factor = _get_cloud_factor(hass)
    ghi = ghi_clear * cloud_factor

    return max(0.0, ghi)


_CONDITION_CLOUD_MAP: dict[str, float] = {
    "sunny": 0.0,
    "clear-night": 0.0,
    "partlycloudy": 0.4,
    "cloudy": 0.8,
    "rainy": 0.9,
    "snowy": 0.85,
    "fog": 0.95,
    "hail": 0.95,
    "lightning": 0.9,
    "pouring": 0.95,
    "snowy-rainy": 0.9,
    "windy": 0.3,
    "windy-variant": 0.5,
    "exceptional": 0.5,
}


def _find_weather_entity(hass: HomeAssistant) -> tuple[str, object] | tuple[None, None]:
    """Return the first available weather entity (id, state) — or (None, None)."""
    # Prefer the standard names; fall back to any weather.* entity.
    preferred = (
        "weather.home",
        "weather.forecast_home",
        "weather.openweathermap",
    )
    for entity_id in preferred:
        state = hass.states.get(entity_id)
        if state is not None:
            return entity_id, state

    # Generic discovery — first weather.* in the registry
    for state in hass.states.async_all("weather"):
        return state.entity_id, state

    return None, None


def _get_cloud_factor(hass: HomeAssistant) -> float:
    """
    Get cloud cover correction factor from a weather entity.

    Tries common weather entity IDs first, then any weather.* entity.
    Returns 1.0 (clear sky) if no weather entity is found.
    """
    _, state = _find_weather_entity(hass)
    if state is None:
        return 1.0

    cloud_pct = state.attributes.get("cloud_coverage")
    if cloud_pct is not None:
        try:
            cloud_frac = float(cloud_pct) / 100.0
            return 1.0 - 0.75 * (cloud_frac ** 3.4)
        except (ValueError, TypeError):
            pass

    cloud_frac = _CONDITION_CLOUD_MAP.get(state.state, 0.3)
    return 1.0 - 0.75 * (cloud_frac ** 3.4)


def get_solar_calculation(hass: HomeAssistant) -> dict:
    """Return a fully-detailed breakdown of how solar irradiance is computed.

    Used by the dashboard so the user can see *why* a particular
    irradiance value was produced (sun position + weather).
    """
    sun_state = hass.states.get("sun.sun")
    elevation = (
        float(sun_state.attributes.get("elevation", 0.0))
        if sun_state is not None
        else None
    )
    azimuth = (
        float(sun_state.attributes.get("azimuth", 0.0))
        if sun_state is not None
        else None
    )

    # Clear-sky GHI
    ghi_clear = 0.0
    if elevation is not None and elevation > 0:
        sin_elev = math.sin(math.radians(elevation))
        if sin_elev > 0:
            ghi_clear = SOLAR_CONSTANT_SURFACE * sin_elev * math.exp(
                -0.057 / sin_elev
            )

    # Weather entity / cloud info
    weather_entity_id, weather_state = _find_weather_entity(hass)
    cloud_pct: float | None = None
    cloud_source = "none"
    cloud_factor = 1.0
    weather_condition: str | None = None

    if weather_state is not None:
        weather_condition = weather_state.state
        raw = weather_state.attributes.get("cloud_coverage")
        if raw is not None:
            try:
                cloud_pct = float(raw)
                cloud_source = "cloud_coverage attribute"
            except (ValueError, TypeError):
                cloud_pct = None
        if cloud_pct is None:
            mapped = _CONDITION_CLOUD_MAP.get(weather_condition, 0.3)
            cloud_pct = mapped * 100.0
            cloud_source = f"condition '{weather_condition}'"
        cloud_frac = max(0.0, min(1.0, cloud_pct / 100.0))
        cloud_factor = 1.0 - 0.75 * (cloud_frac ** 3.4)

    ghi = max(0.0, ghi_clear * cloud_factor)

    return {
        "sun_elevation_deg": round(elevation, 2) if elevation is not None else None,
        "sun_azimuth_deg": round(azimuth, 2) if azimuth is not None else None,
        "weather_entity": weather_entity_id,
        "weather_condition": weather_condition,
        "cloud_coverage_pct": round(cloud_pct, 1) if cloud_pct is not None else None,
        "cloud_source": cloud_source,
        "cloud_factor": round(cloud_factor, 3),
        "ghi_clear_sky_w_m2": round(ghi_clear, 1),
        "ghi_w_m2": round(ghi, 1),
    }


def get_sun_azimuth(hass: HomeAssistant) -> float | None:
    """Get sun azimuth from HA's sun entity."""
    sun_state = hass.states.get("sun.sun")
    if sun_state is None:
        return None
    return sun_state.attributes.get("azimuth")


def get_sun_elevation(hass: HomeAssistant) -> float | None:
    """Get sun elevation from HA's sun entity."""
    sun_state = hass.states.get("sun.sun")
    if sun_state is None:
        return None
    return sun_state.attributes.get("elevation")
