"""Weather forecast integration for outdoor temperature and cloud coverage.

Uses the modern HA weather.get_forecasts service (2023.9+) to pull hourly
forecast data, then provides interpolated values at any future timestamp.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class ForecastPoint:
    """A single forecast data point."""

    time: datetime
    temperature: float  # °C
    cloud_coverage: float  # 0-100%
    condition: str  # e.g. "sunny", "cloudy", "rainy"
    wind_speed: float  # km/h
    humidity: float  # %


class WeatherForecast:
    """Manages weather forecast data from a HA weather entity.

    Fetches hourly forecasts via the weather.get_forecasts service
    and provides interpolated values at any future timestamp.
    """

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        """Initialize with a weather entity ID."""
        self.hass = hass
        self.entity_id = entity_id
        self._points: list[ForecastPoint] = []
        self._last_fetch: datetime | None = None
        self._fetch_interval = timedelta(minutes=30)

    @property
    def is_available(self) -> bool:
        """Return True if we have forecast data."""
        return len(self._points) > 0

    @property
    def horizon_hours(self) -> float:
        """Return how many hours of forecast data we have."""
        if len(self._points) < 2:
            return 0.0
        span = self._points[-1].time - self._points[0].time
        return span.total_seconds() / 3600.0

    async def async_update(self) -> bool:
        """Fetch fresh forecast data from the weather entity.

        Returns True if data was successfully fetched.
        """
        now = datetime.now(timezone.utc)

        # Don't fetch too often
        if (
            self._last_fetch is not None
            and now - self._last_fetch < self._fetch_interval
            and self._points
        ):
            return True

        try:
            # Call weather.get_forecasts service
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": self.entity_id},
                blocking=True,
                return_response=True,
            )

            if not response or self.entity_id not in response:
                _LOGGER.warning(
                    "No forecast data returned for %s", self.entity_id
                )
                return False

            forecast_data = response[self.entity_id].get("forecast", [])
            if not forecast_data:
                _LOGGER.warning(
                    "Empty forecast list from %s", self.entity_id
                )
                return False

            self._points = []
            for entry in forecast_data:
                dt_str = entry.get("datetime")
                if not dt_str:
                    continue

                # Parse datetime (may or may not have timezone)
                dt = _parse_datetime(dt_str)
                if dt is None:
                    continue

                temp = entry.get("temperature")
                if temp is None:
                    continue

                self._points.append(ForecastPoint(
                    time=dt,
                    temperature=float(temp),
                    cloud_coverage=float(entry.get("cloud_coverage", 50)),
                    condition=entry.get("condition", "unknown"),
                    wind_speed=float(entry.get("wind_speed", 0)),
                    humidity=float(entry.get("humidity", 50)),
                ))

            self._points.sort(key=lambda p: p.time)
            self._last_fetch = now

            _LOGGER.debug(
                "Fetched %d forecast points from %s (%.0fh horizon)",
                len(self._points), self.entity_id, self.horizon_hours,
            )
            return True

        except Exception as err:
            _LOGGER.error(
                "Failed to fetch forecast from %s: %s", self.entity_id, err
            )
            return False

    def temperature_at(self, target: datetime) -> float | None:
        """Get interpolated outdoor temperature at a target time.

        Uses linear interpolation between the two nearest forecast points.
        Returns None if target is outside the forecast range.
        """
        return self._interpolate("temperature", target)

    def cloud_coverage_at(self, target: datetime) -> float:
        """Get interpolated cloud coverage (0-100%) at a target time.

        Returns 50% (overcast assumption) if no data available.
        """
        result = self._interpolate("cloud_coverage", target)
        return result if result is not None else 50.0

    def condition_at(self, target: datetime) -> str:
        """Get the weather condition at a target time.

        Uses nearest-neighbor (no interpolation for categorical data).
        """
        if not self._points:
            return "unknown"

        # Make target timezone-aware if needed
        target = _ensure_utc(target)

        best = self._points[0]
        best_delta = abs((best.time - target).total_seconds())
        for p in self._points[1:]:
            delta = abs((p.time - target).total_seconds())
            if delta < best_delta:
                best = p
                best_delta = delta
        return best.condition

    def get_forecast_series(
        self, start: datetime, duration_hours: float, step_minutes: float
    ) -> list[dict[str, Any]]:
        """Get a time series of forecast data.

        Returns a list of dicts with keys: time, temperature, cloud_coverage,
        condition — evenly spaced at step_minutes intervals.
        """
        result = []
        n_steps = int(duration_hours * 60 / step_minutes)
        dt = timedelta(minutes=step_minutes)

        for i in range(n_steps):
            t = start + dt * i
            temp = self.temperature_at(t)
            result.append({
                "time": t,
                "temperature": temp if temp is not None else self._fallback_temperature(t),
                "cloud_coverage": self.cloud_coverage_at(t),
                "condition": self.condition_at(t),
            })
        return result

    def _interpolate(self, field: str, target: datetime) -> float | None:
        """Linear interpolation of a numeric field at target time."""
        if not self._points:
            return None

        target = _ensure_utc(target)

        # Before first point
        if target <= self._points[0].time:
            return getattr(self._points[0], field)

        # After last point
        if target >= self._points[-1].time:
            return getattr(self._points[-1], field)

        # Find bracketing points
        for i in range(1, len(self._points)):
            if self._points[i].time >= target:
                p0 = self._points[i - 1]
                p1 = self._points[i]
                span = (p1.time - p0.time).total_seconds()
                if span <= 0:
                    return getattr(p0, field)
                frac = (target - p0.time).total_seconds() / span
                v0 = getattr(p0, field)
                v1 = getattr(p1, field)
                return v0 + frac * (v1 - v0)

        return getattr(self._points[-1], field)

    def _fallback_temperature(self, target: datetime) -> float:
        """Fallback temperature when forecast is unavailable.

        Uses the current outdoor temperature from the last known point,
        or returns 5°C as a safe default.
        """
        if self._points:
            return self._points[-1].temperature
        return 5.0


def estimate_solar_irradiance(
    cloud_coverage_pct: float,
    hour_of_day: float,
    day_of_year: int,
    latitude: float = 52.0,
) -> float:
    """Estimate global horizontal irradiance (W/m²) from cloud coverage.

    Uses a simplified clear-sky model scaled by cloud coverage.
    This is approximate but captures the main effects:
    - Time of day (solar elevation)
    - Season (day length and max elevation)
    - Cloud cover (linear reduction)

    Args:
        cloud_coverage_pct: Cloud coverage 0-100%
        hour_of_day: Fractional hour (e.g. 14.5 = 2:30 PM)
        day_of_year: 1-365
        latitude: Degrees north (default 52° for Netherlands)

    Returns:
        Estimated GHI in W/m²
    """
    import math

    # Solar declination (Spencer's approximation)
    b = 2 * math.pi * (day_of_year - 1) / 365
    declination = (
        0.006918
        - 0.399912 * math.cos(b)
        + 0.070257 * math.sin(b)
        - 0.006758 * math.cos(2 * b)
        + 0.000907 * math.sin(2 * b)
    )

    # Hour angle (15° per hour from solar noon)
    solar_noon = 12.0  # simplified, ignores longitude/equation of time
    hour_angle = math.radians(15.0 * (hour_of_day - solar_noon))

    # Solar elevation angle
    lat_rad = math.radians(latitude)
    sin_elevation = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle)
    )

    if sin_elevation <= 0:
        return 0.0  # Sun is below horizon

    elevation = math.asin(sin_elevation)

    # Clear-sky irradiance (simplified Ineichen-Perez model)
    # At sea level, roughly: GHI_clear = 1050 * sin(elevation)^1.2
    ghi_clear = 1050.0 * (sin_elevation ** 1.2)

    # Cloud attenuation: linear model
    # 0% cloud = full clear sky, 100% cloud = ~20% of clear sky (diffuse)
    cloud_factor = 1.0 - 0.8 * (cloud_coverage_pct / 100.0)

    return max(0.0, ghi_clear * cloud_factor)


def estimate_solar_gain_from_forecast(
    cloud_coverage_pct: float,
    hour_of_day: float,
    day_of_year: int,
    latitude: float = 52.0,
    window_area_m2: float = 8.0,
    solar_heat_gain_coeff: float = 0.5,
    shading_factor: float = 0.7,
) -> float:
    """Estimate solar heat gain through windows using forecast cloud data.

    Args:
        cloud_coverage_pct: From weather forecast (0-100%)
        hour_of_day: Fractional hour of day
        day_of_year: 1-365
        latitude: Degrees north
        window_area_m2: Total sun-exposed window area
        solar_heat_gain_coeff: SHGC of windows (~0.5 for double glazing)
        shading_factor: 0=fully shaded, 1=no shade. Accounts for
                        neighboring buildings, trees, overhangs, etc.

    Returns:
        Solar heat gain in Watts
    """
    irradiance = estimate_solar_irradiance(
        cloud_coverage_pct, hour_of_day, day_of_year, latitude
    )
    return irradiance * window_area_m2 * solar_heat_gain_coeff * shading_factor


def _parse_datetime(dt_str: str) -> datetime | None:
    """Parse a datetime string, handling various formats."""
    try:
        dt = datetime.fromisoformat(dt_str)
        # Ensure timezone-aware (assume UTC if naive)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _ensure_utc(dt: datetime) -> datetime:
    """Make a datetime timezone-aware (UTC) if it's naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
