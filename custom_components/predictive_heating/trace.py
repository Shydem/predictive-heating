"""Decision trace log for Predictive Heating.

This module provides a structured log of every decision the model makes.
Instead of being a black box, every calculation is recorded with its inputs,
outputs, and reasoning. The trace is exposed as sensor attributes so you
can inspect exactly why the model recommended what it did.

Usage:
    trace = Trace("optimize")
    trace.step("compute_heat_deficit",
        inputs={"t_current": 19.5, "t_target": 20.0, "t_no_heat": 19.2},
        result={"deficit_wh": 450.0},
        note="House would cool to 19.2 without heating, need 450 Wh to reach 20.0"
    )
    trace.warn("low_cop", "COP is only 1.8 at -10°C, gas boiler is much cheaper")

    # Later: expose trace.entries as a sensor attribute
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass
class TraceEntry:
    """One step in the decision trace."""

    timestamp: str
    phase: str          # e.g. "training", "optimize", "data_collect"
    step: str           # e.g. "compute_heat_deficit"
    inputs: dict[str, Any]
    result: dict[str, Any]
    note: str           # human-readable explanation
    level: str = "info" # "info", "warn", "error"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict for sensor attributes."""
        return {
            "time": self.timestamp,
            "phase": self.phase,
            "step": self.step,
            "inputs": self.inputs,
            "result": self.result,
            "note": self.note,
            "level": self.level,
        }

    def __str__(self) -> str:
        """Human-readable single-line summary."""
        return f"[{self.phase}/{self.step}] {self.note}"


class Trace:
    """Accumulates a decision trace for one run of the model.

    Keeps the last N entries to avoid unbounded memory growth.
    Also logs to the standard HA logger at debug level, so you
    can see everything in the HA logs when debug logging is enabled.
    """

    MAX_ENTRIES = 200

    def __init__(self, phase: str) -> None:
        """Start a new trace for a phase (e.g. 'optimize', 'training')."""
        self.phase = phase
        self.entries: list[TraceEntry] = []
        self._start = datetime.now()

    def step(
        self,
        name: str,
        inputs: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        note: str = "",
    ) -> None:
        """Record a computation step."""
        entry = TraceEntry(
            timestamp=datetime.now().isoformat(),
            phase=self.phase,
            step=name,
            inputs=inputs or {},
            result=result or {},
            note=note,
        )
        self.entries.append(entry)
        if len(self.entries) > self.MAX_ENTRIES:
            self.entries = self.entries[-self.MAX_ENTRIES:]

        _LOGGER.debug("TRACE %s", entry)

    def warn(self, name: str, note: str, **details: Any) -> None:
        """Record a warning."""
        entry = TraceEntry(
            timestamp=datetime.now().isoformat(),
            phase=self.phase,
            step=name,
            inputs=details,
            result={},
            note=note,
            level="warn",
        )
        self.entries.append(entry)
        _LOGGER.warning("TRACE %s", entry)

    def error(self, name: str, note: str, **details: Any) -> None:
        """Record an error."""
        entry = TraceEntry(
            timestamp=datetime.now().isoformat(),
            phase=self.phase,
            step=name,
            inputs=details,
            result={},
            note=note,
            level="error",
        )
        self.entries.append(entry)
        _LOGGER.error("TRACE %s", entry)

    def summary(self) -> dict[str, Any]:
        """Return a compact summary for sensor attributes."""
        warnings = [e for e in self.entries if e.level == "warn"]
        errors = [e for e in self.entries if e.level == "error"]
        elapsed = (datetime.now() - self._start).total_seconds()

        return {
            "phase": self.phase,
            "total_steps": len(self.entries),
            "warnings": len(warnings),
            "errors": len(errors),
            "elapsed_seconds": round(elapsed, 2),
            "last_steps": [e.to_dict() for e in self.entries[-10:]],
        }

    def to_list(self) -> list[dict[str, Any]]:
        """Full trace as a list of dicts."""
        return [e.to_dict() for e in self.entries]
