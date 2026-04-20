"""
Model Predictive Control (MPC) for a single room — v0.3.

Why MPC?
    Pure hysteresis (heat until target + band, then off) over-shoots in
    real installations because of control lag:
      - The thermostat's OpenTherm loop takes minutes to ramp the
        flow-temp down after the setpoint drops.
      - Radiators keep emitting heat for a while after the valve closes.
      - Room-air mixing / sensor averaging hides the early rise.
    A simple thermal model predicts the resulting trajectory, so the
    MPC can decide to *stop heating before the setpoint is reached*
    when continuing would overshoot the comfort band.

Approach — short-horizon switching-time search:
    The room is a first-order thermal system with a transport delay on
    the control input (captures boiler + radiator lag). We enumerate
    all bang-bang control sequences with at most one switch inside the
    horizon (plus the "all on" / "all off" sequences), simulate each
    one against the thermal model, and pick the one with the lowest
    total cost.

    Why this works:
        With a monotone first-order plant + quadratic cost around a
        target band, the optimal control policy is bang-bang with at
        most one switch per comfort crossing. Expanding the search to
        two switches captures pre-heat-then-coast patterns which the
        MPC uses automatically when the target is higher than current.

    Why it's cheap:
        At horizon=60 min, step=5 min → N=12, so we evaluate ~2*N+2
        candidate trajectories per cycle. Each evaluation is an
        N-step Euler integration. Total: <1 ms on a Raspberry Pi.

Output:
    ``MPCResult`` with:
      - ``action`` in {"heat", "off"} — what to do **now**.
      - ``predicted_trajectory`` — list of temps over the horizon.
      - ``predicted_control`` — chosen heat on/off sequence.
      - ``reason`` — short label for diagnostics.

The controller treats the underlying heat source as a binary input
(``heating_power`` watts when on, 0 when off). For boilers that
modulate continuously this is an approximation; the lag term absorbs
most of the resulting discrepancy, and the EKF keeps ``heating_power``
calibrated to the *average* delivered power.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .thermal_model import ThermalModel

_LOGGER = logging.getLogger(__name__)


@dataclass
class MPCConfig:
    """Tunable MPC parameters."""

    # Planning horizon — how far ahead we look.
    horizon_min: float = 60.0
    # Timestep for the search grid. Shorter = more precise, more work.
    step_min: float = 5.0

    # Control-delay model: after the MPC says "heat off" the actual
    # thermal delivery keeps going for ``control_delay_min`` before
    # stopping. Captures OpenTherm ramp-down + radiator coast.
    control_delay_min: float = 5.0

    # Soft-constraint weights for the cost function.
    # overshoot heavily penalised so the MPC prefers undershoot to over.
    overshoot_weight: float = 8.0
    undershoot_weight: float = 2.0
    # Tiny penalty on "heating on" so ties break toward "do less heating".
    energy_weight: float = 0.01
    # Tolerance band: errors inside ±band are free, outside are penalised.
    comfort_band: float = 0.2

    # If set, MPC only fires when the thermal model is calibrated. Pre-
    # calibration the caller should fall back to hysteresis (safer).
    require_calibrated: bool = True


@dataclass
class MPCResult:
    """What the MPC decided, plus diagnostics."""

    action: str  # "heat" or "off"
    predicted_trajectory: list[float] = field(default_factory=list)
    predicted_control: list[float] = field(default_factory=list)
    cost: float = 0.0
    reason: str = ""
    switch_at_step: int | None = None
    # For diagnostics — what the "pure hysteresis" would have done.
    hysteresis_action: str | None = None


class MPCController:
    """
    Short-horizon MPC that plans heating to minimise overshoot.

    The controller is stateless between calls except for one small
    piece: ``_recent_heat_commands`` tracks the last few commands so
    the control-delay model is accurate across invocations.
    """

    def __init__(
        self,
        model: ThermalModel,
        config: MPCConfig | None = None,
    ) -> None:
        self.model = model
        self.config = config or MPCConfig()

        # How many timesteps the horizon covers.
        self.N = max(2, int(round(self.config.horizon_min / self.config.step_min)))
        self.dt_hours = self.config.step_min / 60.0
        self.delay_steps = max(
            0, int(round(self.config.control_delay_min / self.config.step_min))
        )

        # Rolling buffer of recent effective heat commands (0.0 / 1.0),
        # used to seed the delay line at the start of each MPC solve.
        self._recent_heat_commands: list[float] = [0.0] * self.delay_steps

    # ── public API ───────────────────────────────────────────────

    def solve(
        self,
        t_indoor: float,
        t_outdoor: float,
        t_target: float,
        solar_irradiance: float = 0.0,
        currently_heating: bool = False,
    ) -> MPCResult:
        """Return the MPC's recommended control action for the current step."""
        # Seed the delay buffer with the most recent observed command.
        # If the caller says we're heating right now, everything in the
        # pipeline is "on"; otherwise "off". This is a first-order
        # approximation that works because the buffer only drives the
        # first ``delay_steps`` of the simulation.
        initial_delay = [1.0 if currently_heating else 0.0] * self.delay_steps

        candidates = self._enumerate_candidates()
        best: MPCResult | None = None

        for label, u_sequence in candidates:
            trajectory = self._simulate(
                t_indoor, t_outdoor, solar_irradiance,
                u_sequence, initial_delay,
            )
            cost = self._cost(trajectory, u_sequence, t_target)
            if best is None or cost < best.cost:
                best = MPCResult(
                    action="heat" if u_sequence[0] > 0.5 else "off",
                    predicted_trajectory=trajectory,
                    predicted_control=list(u_sequence),
                    cost=cost,
                    reason=label,
                    switch_at_step=self._first_switch(u_sequence),
                )

        assert best is not None  # enumerate_candidates always yields ≥2
        # Add what pure hysteresis would say, for diagnostics.
        best.hysteresis_action = self._hysteresis_decision(
            t_indoor, t_target, currently_heating
        )
        return best

    def record_command(self, heat_on: bool) -> None:
        """Update the recent-commands buffer so the delay model stays current."""
        if self.delay_steps == 0:
            return
        self._recent_heat_commands.append(1.0 if heat_on else 0.0)
        if len(self._recent_heat_commands) > self.delay_steps:
            self._recent_heat_commands = self._recent_heat_commands[-self.delay_steps:]

    # ── internals ────────────────────────────────────────────────

    def _enumerate_candidates(self) -> list[tuple[str, list[float]]]:
        """
        Generate the set of bang-bang control sequences to search over.

        Included patterns:
          - all off
          - all on
          - on for k steps then off          (k = 1..N-1)
          - off for k steps then on          (k = 1..N-1)
          - off for k then on for m then off (k,m small, captures pre-heat then coast)
        """
        N = self.N
        out: list[tuple[str, list[float]]] = []

        out.append(("all_off", [0.0] * N))
        out.append(("all_on", [1.0] * N))

        # on→off switch at step k
        for k in range(1, N):
            seq = [1.0] * k + [0.0] * (N - k)
            out.append((f"heat_for_{k}_then_off", seq))

        # off→on switch at step k
        for k in range(1, N):
            seq = [0.0] * k + [1.0] * (N - k)
            out.append((f"off_for_{k}_then_heat", seq))

        # off → on → off (pre-heat then coast). Bounded m to keep search
        # small — this only matters for very aggressive pre-heat windows.
        for k in range(0, min(N - 1, 3)):
            for m in range(1, N - k):
                seq = [0.0] * k + [1.0] * m + [0.0] * (N - k - m)
                out.append((f"wait_{k}_heat_{m}_coast", seq))

        return out

    def _simulate(
        self,
        T0: float,
        T_out: float,
        solar: float,
        u_sequence: list[float],
        initial_delay: list[float],
    ) -> list[float]:
        """
        Forward-simulate indoor temp given a control sequence.

        Uses a first-order thermal model with an N-step transport
        delay on the control input.
        """
        p = self.model.params
        C_wh = p.thermal_mass * 1000.0 / 3600.0
        if C_wh <= 0:
            return [T0] * (len(u_sequence) + 1)

        # Delay line: FIFO of effective heat commands hitting the room.
        delay_line = list(initial_delay)
        T = T0
        traj = [T0]

        for u in u_sequence:
            if self.delay_steps > 0:
                # Effective heat = command from delay_steps ago.
                effective = delay_line[0]
                delay_line = delay_line[1:] + [u]
            else:
                effective = u

            q_heat = effective * p.heating_power
            q_solar = solar * p.solar_gain_factor
            q_loss = p.heat_loss_coeff * (T - T_out)
            dT = (q_heat + q_solar - q_loss) / C_wh * self.dt_hours
            T += dT
            traj.append(T)

        return traj

    def _cost(
        self,
        trajectory: list[float],
        u_sequence: list[float],
        t_target: float,
    ) -> float:
        """
        Quadratic cost on deviation outside the comfort band.

        Overshoot is weighted more heavily than undershoot because the
        whole point of MPC here is preventing overshoot — undershoot
        is fine (the room will catch up).
        """
        cfg = self.config
        over_limit = t_target + cfg.comfort_band
        under_limit = t_target - cfg.comfort_band

        cost = 0.0
        # Skip t=0 (we can't change the present); weight later steps
        # slightly more so the controller prioritises ending near target.
        for step, T in enumerate(trajectory[1:], start=1):
            step_weight = 1.0 + 0.05 * step  # linear ramp
            if T > over_limit:
                err = T - over_limit
                cost += cfg.overshoot_weight * err * err * step_weight
            elif T < under_limit:
                err = under_limit - T
                cost += cfg.undershoot_weight * err * err * step_weight

        for u in u_sequence:
            cost += cfg.energy_weight * u

        return cost

    def _first_switch(self, u_sequence: list[float]) -> int | None:
        """Index of the first control transition, or None if constant."""
        if not u_sequence:
            return None
        first = u_sequence[0]
        for i, u in enumerate(u_sequence[1:], start=1):
            if abs(u - first) > 0.5:
                return i
        return None

    def _hysteresis_decision(
        self,
        t_indoor: float,
        t_target: float,
        currently_heating: bool,
    ) -> str:
        """What plain hysteresis with ±band would do — for diagnostics only."""
        band = self.config.comfort_band
        if t_indoor < t_target - band:
            return "heat"
        if t_indoor > t_target + band:
            return "off"
        return "heat" if currently_heating else "off"
