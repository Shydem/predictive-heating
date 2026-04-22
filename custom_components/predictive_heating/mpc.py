"""Model Predictive Control — REMOVED in v0.7.

This module used to contain a short-horizon receding-horizon optimiser
that actively modulated the thermostat setpoint inside a preset. It was
removed at the user's request: the integration is now monitor-first,
and the "reach target on time" case is handled by the separate
:class:`PreheatPlanner` (``preheat.py``), which raises the *target*
earlier rather than fighting the thermostat's own modulation loop.

The file is retained as a stub so old imports don't fail silently —
references to MPC classes now raise a clear RuntimeError instead.
"""

from __future__ import annotations


class _RemovedSentinel:
    """Explanatory stub raised when legacy MPC code is invoked."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: D401
        raise RuntimeError(
            "MPC was removed in v0.7. Use the pre-heat planner "
            "(see preheat.py) for optimal-start behaviour."
        )


# Aliases so ``from .mpc import MPCConfig`` etc. still import cleanly
# but fail loudly at instantiation if anything still tries to use them.
MPCConfig = _RemovedSentinel
MPCController = _RemovedSentinel
MPCResult = _RemovedSentinel
