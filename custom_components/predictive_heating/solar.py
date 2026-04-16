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


def _get_cloud_factor(hass: HomeAssistant) -> float:
    """
    Get cloud cover correction factor from a weather entity.

    Tries common weather entity IDs. Returns 1.0 (clear sky) if
    no weather entity is found.
    """
    # Try common weather entity patterns
    weather_entities = [
        "weather.home",
        "weather.forecast_home",
        "weather.openweathermap",
    ]

    for entity_id in weather_entities:
        state = hass.states.get(entity_id)
        if state is None:
            continue

        # Some weather integrations expose cloud_coverage directly
        cloud_pct = state.attributes.get("cloud_coverage")
        if cloud_pct is not None:
            try:
                cloud_frac = float(cloud_pct) / 100.0
                # Kasten-Czeplak cloud correction
                return 1.0 - 0.75 * (cloud_frac ** 3.4)
            except (ValueError, TypeError):
                pass

        # Fallback: estimate from condition string
        condition = state.state
        cloud_map = {
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

        cloud_frac = cloud_map.get(condition, 0.3)
        return 1.0 - 0.75 * (cloud_frac ** 3.4)

    return 1.0  # no weather entity found → assume clear


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
