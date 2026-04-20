"""
Presence-based preset switching — v0.3.

Watches one or more ``person.*`` entities. When everyone is away the
room auto-switches to the Away preset; when anyone returns home the
room reverts to the preset that was active before the switch-out.

Design notes:
    * The monitor never *drives* target temperatures directly — it only
      emits ``"away"`` / ``"home"`` transition events and the climate
      entity decides how to apply them. This keeps the MPC / preheat
      logic from having to know about presence at all.
    * A short "leave grace period" prevents a single person.* glitch
      (GPS jitter, phone battery) from triggering a cold-down cycle.
      The default is 10 minutes.
    * Returning home is applied immediately — nobody wants to come back
      to a cold house because the grace period hasn't elapsed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

# States considered "at home". HA uses "home" for the zone named
# Home and custom zones for other places. We treat anything non-home,
# non-unknown as away.
_HOME_STATES = frozenset({"home"})
_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", "none", None})


@dataclass
class PresenceConfig:
    """Tunable presence behaviour."""

    # Minutes everyone must be away before we switch to Away mode.
    # Prevents the system from reacting to momentary glitches.
    away_grace_min: float = 10.0
    # If any of these person entities can't be read, err on the side
    # of "someone is home". Set to False if you want the opposite.
    assume_home_on_unknown: bool = True


@dataclass
class PresenceState:
    """Internal state of the monitor — exposed for serialization."""

    everyone_away_since: float | None = None
    currently_away: bool = False
    last_home_person: str | None = None
    # Preset the user had active before we took over with "away".
    # Restored when someone gets home.
    saved_preset: str | None = None


class PresenceMonitor:
    """
    Tracks ``person.*`` entity states and emits preset transitions.

    The caller registers one or more person entity IDs and calls
    ``update()`` whenever any of them changes state. ``update()``
    returns one of:

        ``None``        — no preset change needed.
        ``"away"``      — everyone is away past the grace period; the
                          caller should switch to the Away preset
                          (and remember which preset was active).
        ``"home"``      — at least one person came back home; the
                          caller should restore ``saved_preset``.
    """

    def __init__(
        self,
        person_entity_ids: list[str],
        config: PresenceConfig | None = None,
    ) -> None:
        self.person_entity_ids = list(person_entity_ids or [])
        self.config = config or PresenceConfig()
        self.state = PresenceState()

    @property
    def enabled(self) -> bool:
        return len(self.person_entity_ids) > 0

    def update(
        self,
        person_states: dict[str, str | None],
        now_ts: float | None = None,
    ) -> str | None:
        """
        Feed the monitor the latest person.* states and get a decision.

        Args:
            person_states: mapping of person entity_id → state string
                (e.g. ``{"person.sietse": "home", "person.alice": "work"}``).
            now_ts: unix timestamp of "now". Defaults to ``time.time()``.

        Returns:
            ``"away"`` if we just crossed into Away mode this tick.
            ``"home"`` if we just returned from Away mode this tick.
            ``None`` otherwise.
        """
        if not self.enabled:
            return None

        now_ts = now_ts if now_ts is not None else time.time()

        anyone_home = self._anyone_home(person_states)

        if anyone_home:
            self.state.everyone_away_since = None
            if self.state.currently_away:
                # Switch back home — immediate.
                self.state.currently_away = False
                self.state.last_home_person = self._first_home_person(person_states)
                _LOGGER.info(
                    "Presence: someone came home (%s) — restoring preset",
                    self.state.last_home_person,
                )
                return "home"
            return None

        # Nobody is home.
        if self.state.everyone_away_since is None:
            self.state.everyone_away_since = now_ts

        grace_s = self.config.away_grace_min * 60.0
        time_away = now_ts - self.state.everyone_away_since

        if not self.state.currently_away and time_away >= grace_s:
            self.state.currently_away = True
            _LOGGER.info(
                "Presence: everyone away for %.1f min — switching to Away",
                time_away / 60.0,
            )
            return "away"

        return None

    def remember_preset(self, preset: str) -> None:
        """
        Remember the preset active *before* we switched to Away, so we
        can restore it when someone returns. The caller should call
        this before forcing the Away preset.
        """
        if preset and preset != "away":
            self.state.saved_preset = preset

    def saved_preset_or(self, default: str) -> str:
        return self.state.saved_preset or default

    # ── helpers ──────────────────────────────────────────────────

    def _anyone_home(self, person_states: dict[str, str | None]) -> bool:
        assume_home = self.config.assume_home_on_unknown
        # If we have no states at all for any configured person, be
        # safe: stay "home" rather than cool down.
        saw_any = False
        for eid in self.person_entity_ids:
            state = person_states.get(eid)
            if state in _UNAVAILABLE_STATES:
                if assume_home:
                    return True
                continue
            saw_any = True
            if state in _HOME_STATES:
                return True
        return not saw_any  # saw some states but none at home → away

    def _first_home_person(self, person_states: dict[str, str | None]) -> str | None:
        for eid in self.person_entity_ids:
            if person_states.get(eid) in _HOME_STATES:
                return eid
        return None
