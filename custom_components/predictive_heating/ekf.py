"""
Extended Kalman Filter for thermal parameter estimation.

State vector x = [H, C, P_heat, S_gain]
    H       = heat loss coefficient (W/K)
    C       = thermal mass (Wh/K) — stored in Wh for numerical convenience
    P_heat  = effective heating power (W)
    S_gain  = solar gain factor (dimensionless, 0..1)

Process model:
    Parameters are assumed to drift slowly (random walk):
        x[k+1] = x[k] + w[k],  w ~ N(0, Q)

Measurement model:
    We measure dT (temperature change over an interval dt):
        dT_measured = T[k+1] - T[k]

    The predicted dT from the thermal model:
        dT_predicted = dt/C * (P_heat * u_heat + S_gain * I_solar - H * (T_in - T_out))

    Where:
        u_heat = 1 if heating, 0 if not
        I_solar = solar irradiance (W/m2)
        dt = time interval (hours)

    Measurement: z = dT_measured
    h(x) = dT_predicted(x, inputs)

    Jacobian H_jac = dh/dx (partial derivatives w.r.t. each state)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

_LOGGER = logging.getLogger(__name__)

# State indices
IDX_H = 0       # heat loss coefficient
IDX_C = 1       # thermal mass (Wh/K)
IDX_P = 2       # heating power
IDX_S = 3       # solar gain factor
N_STATES = 4


@dataclass
class EKFState:
    """Extended Kalman Filter state."""

    # State estimate [H, C, P_heat, S_gain]
    x: np.ndarray = field(default_factory=lambda: np.array([
        150.0,    # H: 150 W/K initial guess
        1389.0,   # C: 5000 kJ/K = 1389 Wh/K
        5000.0,   # P_heat: 5 kW
        0.3,      # S_gain: 30%
    ]))

    # State covariance matrix
    P: np.ndarray = field(default_factory=lambda: np.diag([
        2500.0,   # H variance (50^2)
        250000.0, # C variance (500^2)
        2500000.0,# P variance (1500^2)
        0.04,     # S variance (0.2^2)
    ]))

    # Process noise covariance (how fast parameters can drift)
    Q: np.ndarray = field(default_factory=lambda: np.diag([
        0.01,     # H drifts very slowly
        0.1,      # C drifts very slowly
        0.1,      # P drifts very slowly
        0.0001,   # S drifts very slowly
    ]))

    # Measurement noise variance (temperature sensor noise + model mismatch)
    R: float = 0.04  # (0.2°C)^2

    @property
    def H(self) -> float:
        return float(self.x[IDX_H])

    @property
    def C_wh(self) -> float:
        return float(self.x[IDX_C])

    @property
    def C_kj(self) -> float:
        return float(self.x[IDX_C]) * 3.6  # Wh/K → kJ/K

    @property
    def P_heat(self) -> float:
        return float(self.x[IDX_P])

    @property
    def S_gain(self) -> float:
        return float(self.x[IDX_S])


class ThermalEKF:
    """
    Extended Kalman Filter that learns thermal parameters from observations.

    Each update takes:
        - dt: time interval since last observation (hours)
        - T_in: indoor temperature at start of interval
        - T_out: outdoor temperature (average over interval)
        - u_heat: heating active (1.0) or not (0.0)
        - I_solar: solar irradiance W/m2
        - dT_measured: actual temperature change over the interval
    """

    def __init__(self, state: EKFState | None = None) -> None:
        self.state = state or EKFState()
        self._update_count = 0
        self._prediction_errors: list[float] = []

    def predict_dT(
        self,
        dt: float,
        T_in: float,
        T_out: float,
        u_heat: float,
        I_solar: float,
    ) -> float:
        """Predict temperature change using current parameter estimates."""
        x = self.state.x
        H = x[IDX_H]
        C = x[IDX_C]
        P = x[IDX_P]
        S = x[IDX_S]

        if C <= 0:
            return 0.0

        dT = dt / C * (P * u_heat + S * I_solar - H * (T_in - T_out))
        return float(dT)

    def _measurement_jacobian(
        self,
        dt: float,
        T_in: float,
        T_out: float,
        u_heat: float,
        I_solar: float,
    ) -> np.ndarray:
        """
        Compute the Jacobian of the measurement function h(x) w.r.t. state x.

        h(x) = dt/C * (P*u_heat + S*I_solar - H*(T_in - T_out))

        dh/dH = -dt/C * (T_in - T_out)
        dh/dC = -dt/C^2 * (P*u_heat + S*I_solar - H*(T_in - T_out))
        dh/dP = dt/C * u_heat
        dh/dS = dt/C * I_solar
        """
        x = self.state.x
        H = x[IDX_H]
        C = x[IDX_C]
        P = x[IDX_P]
        S = x[IDX_S]

        if C <= 0:
            return np.zeros((1, N_STATES))

        delta_T = T_in - T_out
        total_power = P * u_heat + S * I_solar - H * delta_T

        J = np.zeros((1, N_STATES))
        J[0, IDX_H] = -dt / C * delta_T
        J[0, IDX_C] = -dt / (C * C) * total_power
        J[0, IDX_P] = dt / C * u_heat
        J[0, IDX_S] = dt / C * I_solar

        return J

    def update(
        self,
        dt: float,
        T_in: float,
        T_out: float,
        u_heat: float,
        I_solar: float,
        dT_measured: float,
    ) -> float:
        """
        Run one EKF update step.

        Args:
            dt: time interval in hours
            T_in: indoor temp at start
            T_out: outdoor temp
            u_heat: 1.0 if heating, 0.0 if not
            I_solar: solar irradiance (W/m2)
            dT_measured: actual temperature change

        Returns:
            Prediction error (innovation) for this step.
        """
        if dt <= 0 or dt > 2.0:
            return 0.0  # skip invalid intervals

        s = self.state

        # ── Prediction step (parameters are constant + noise) ──
        # x_pred = x  (random walk model)
        # P_pred = P + Q
        P_pred = s.P + s.Q

        # ── Update step ──
        # Innovation (measurement residual)
        dT_predicted = self.predict_dT(dt, T_in, T_out, u_heat, I_solar)
        innovation = dT_measured - dT_predicted

        # Jacobian
        H_jac = self._measurement_jacobian(dt, T_in, T_out, u_heat, I_solar)

        # Innovation covariance: S = H @ P_pred @ H^T + R
        S = H_jac @ P_pred @ H_jac.T + s.R
        S_val = float(S[0, 0])

        if abs(S_val) < 1e-12:
            return float(innovation)

        # Kalman gain: K = P_pred @ H^T @ S^-1
        K = P_pred @ H_jac.T / S_val

        # State update
        s.x = s.x + K.flatten() * innovation

        # Enforce physical constraints
        s.x[IDX_H] = max(10.0, min(1000.0, s.x[IDX_H]))     # 10–1000 W/K
        s.x[IDX_C] = max(100.0, min(50000.0, s.x[IDX_C]))    # 100–50000 Wh/K
        s.x[IDX_P] = max(500.0, min(50000.0, s.x[IDX_P]))    # 0.5–50 kW
        s.x[IDX_S] = max(0.0, min(1.0, s.x[IDX_S]))          # 0–100%

        # Covariance update: P = (I - K @ H) @ P_pred
        I_KH = np.eye(N_STATES) - K @ H_jac
        s.P = I_KH @ P_pred

        # Ensure P stays symmetric and positive semi-definite
        s.P = (s.P + s.P.T) / 2
        eigvals = np.linalg.eigvalsh(s.P)
        if np.any(eigvals < 0):
            s.P += np.eye(N_STATES) * (abs(min(eigvals)) + 1e-6)

        self._update_count += 1

        # Track prediction accuracy
        self._prediction_errors.append(abs(innovation))
        if len(self._prediction_errors) > 100:
            self._prediction_errors = self._prediction_errors[-100:]

        if self._update_count % 20 == 0:
            _LOGGER.debug(
                "EKF update #%d: H=%.1f W/K, C=%.0f Wh/K (%.0f kJ/K), "
                "P=%.0f W, S=%.2f, err=%.3f°C",
                self._update_count,
                s.H, s.C_wh, s.C_kj, s.P_heat, s.S_gain,
                self.mean_prediction_error,
            )

        return float(innovation)

    @property
    def mean_prediction_error(self) -> float:
        """Mean absolute prediction error over recent updates."""
        if not self._prediction_errors:
            return float("inf")
        return sum(self._prediction_errors) / len(self._prediction_errors)

    @property
    def is_calibrated(self) -> bool:
        """Model is considered calibrated when prediction error < 0.5°C."""
        return (
            self._update_count >= 30
            and self.mean_prediction_error < 0.5
        )

    @property
    def update_count(self) -> int:
        return self._update_count

    def to_dict(self) -> dict:
        """Serialize EKF state for persistence."""
        return {
            "x": self.state.x.tolist(),
            "P": self.state.P.tolist(),
            "Q": self.state.Q.tolist(),
            "R": self.state.R,
            "update_count": self._update_count,
            "prediction_errors": self._prediction_errors[-100:],
        }

    @classmethod
    def from_dict(cls, data: dict) -> ThermalEKF:
        """Restore EKF from persisted state."""
        state = EKFState()
        if "x" in data:
            state.x = np.array(data["x"])
        if "P" in data:
            state.P = np.array(data["P"])
        if "Q" in data:
            state.Q = np.array(data["Q"])
        if "R" in data:
            state.R = data["R"]

        ekf = cls(state)
        ekf._update_count = data.get("update_count", 0)
        ekf._prediction_errors = data.get("prediction_errors", [])
        return ekf
