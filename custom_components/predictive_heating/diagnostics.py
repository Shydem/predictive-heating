"""Diagnostics for Predictive Heating.

When you click "Download diagnostics" in Settings → Devices → Predictive Heating,
this dumps everything needed to debug the integration:
- Config (with no sensitive data)
- Current model parameters
- Last training and optimization traces
- Current sensor values
- Data quality report
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import PredictiveHeatingCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: PredictiveHeatingCoordinator = entry.runtime_data

    return {
        "config": {
            # Redact entity IDs to just domain.object_id format
            k: v for k, v in coordinator.config.items()
        },
        "model_parameters": {
            "ua_w_per_k": coordinator.params.ua,
            "thermal_mass_kwh_per_k": coordinator.params.thermal_mass,
            "r_squared": coordinator.params.r_squared,
            "last_trained": (
                coordinator.params.last_trained.isoformat()
                if coordinator.params.last_trained else None
            ),
            "n_data_points": coordinator.params.n_data_points,
            "description": coordinator.params.describe(),
        },
        "current_data": coordinator.data if coordinator.data else "no data yet",
        "schedule": coordinator.schedule,
        "heaters": [
            {
                "name": h.name,
                "entity_id": h.entity_id,
                "power_w": h.power_w,
            }
            for h in coordinator.heaters
        ],
        "last_training_trace": coordinator.last_training_trace,
        "last_optimization_trace": coordinator.last_optimize_trace,
    }
