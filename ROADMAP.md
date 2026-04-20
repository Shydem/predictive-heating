# Predictive Heating — Development Roadmap

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   Home Assistant                     │
│                                                      │
│  ┌──────────────┐   ┌─────────────────────────────┐ │
│  │ Temp Sensor   │──▶│                             │ │
│  │ Outdoor Temp  │──▶│   Predictive Heating        │ │
│  │ Window Sensor │──▶│   Climate Entity            │ │
│  │ Humidity      │──▶│                             │ │
│  │ Energy Prices │──▶│  ┌────────────────────────┐ │ │
│  │ Heat Pump COP │──▶│  │ Thermal Model (learns) │ │ │
│  └──────────────┘   │  └──────────┬─────────────┘ │ │
│                      │             ▼               │ │
│                      │  ┌────────────────────────┐ │ │
│                      │  │ Controller (decides)    │ │ │
│                      │  └──────────┬─────────────┘ │ │
│                      │             ▼               │ │
│                      │  ┌────────────────────────┐ │ │
│                      │  │ Underlying TRV/Climate  │ │ │
│                      │  │ (receives setpoints)    │ │ │
│                      │  └────────────────────────┘ │ │
│                      └─────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

---

## Phase 1 — Foundation (v0.1) ✅ COMPLETE

**Goal:** Working HACS integration with basic smart thermostat features.

What's built:
- HACS-compatible custom component with config flow UI
- Virtual climate entity that wraps an underlying TRV/thermostat
- External temperature sensor support (reads real room temp, not radiator temp)
- Window/door detection (pauses heating when open)
- Preset modes: Comfort, Eco, Away, Sleep, Boost
- Configurable preset temperatures via options flow
- Hysteresis-based on/off control (reliable fallback)
- Thermal model skeleton that begins collecting observations
- Diagnostic sensors: model state, heat loss coefficient, learning progress
- Model persistence across HA restarts

**Inspired by:** Better Thermostat's TRV wrapping + preset system.

---

## Phase 2 — Self-Learning Thermal Model (v0.2) ✅ COMPLETE

**Goal:** The model accurately predicts how each room heats and cools.

Tasks:
- ✅ EKF tracking H, C, heating power, and solar gain as state variables (`ekf.py:EKFState`)
- ✅ Learn heating_power from active heating observations (`thermal_model.py:_learn_from_pair`)
- ✅ Solar gain estimation from sun.sun + weather (`solar.py`)
- ✅ Prediction accuracy tracking — auto-calibrate when error < 0.5°C (`ekf.py:is_calibrated`)
- ✅ Room size / thermal mass configuration helper — floor area × ceiling height × building type (`thermal_model.py:estimate_initial_thermal_params`, wired through config flow)
- ✅ Persist full observation history for model retraining across HA updates (`thermal_model.py:to_dict`)

**Inspired by:** RoomMind's EKF thermal model with per-room learning.

---

## Phase 3 — Predictive Pre-Heating + MPC (v0.3) ✅ COMPLETE

**Goal:** Start heating at the right time so the room is comfortable when you need it, and stop early enough to prevent overshoot.

Tasks:
- ✅ Use thermal_model.time_to_reach() to calculate pre-heat start times (`preheat.py:PreheatPlanner._estimate_lead_minutes`)
- ✅ Follow HA schedule helpers via `CONF_SCHEDULE_ENTITY` + pre-heat planner that reads the next ON transition
- ✅ Pre-heat controller: given a comfort window and the current state, start heating so the room hits target exactly on time (`preheat.py`, safety margin `_LEAD_MARGIN = 1.15`)
- ✅ Account for outdoor temperature forecasts — `weather.*` hourly forecast blended with the current outdoor reading to extend lead time on cold nights (`climate.py:_refresh_weather_forecast`, `preheat.py:_outdoor_temp_average`)
- ✅ Presence-based adjustment: `person.*` entities watched with a configurable grace period, auto-switches between Away and the previously-active preset (`presence.py:PresenceMonitor`)
- ✅ Configurable comfort ramp — `"gradual"` linearly interpolates the target over the pre-heat window, `"instant"` snaps to the high target (`preheat.py:_effective_target`)
- ✅ **Bonus — Model Predictive Control for overshoot prevention**: short-horizon switching-time search over a first-order thermal model with transport-delay input (captures OpenTherm ramp-down + radiator coast). Runs every cycle when enabled + model calibrated; falls back to hysteresis otherwise (`mpc.py:MPCController`). Verified in closed-loop sim: ~0.87°C reduction in peak overshoot vs plain hysteresis under a 15-min transport delay.

**Inspired by:** RoomMind's MPC look-ahead + Better Thermostat's schedule planner.

---

## Phase 4 — Energy Price Optimization (v0.4)

**Goal:** Heat at the cheapest times while maintaining comfort.

Tasks:
- Integrate with Nordpool / ENTSO-E / other energy price sensors for electricity and gas spot prices
- Define a cost function: cost = Σ (energy_price × power × time_step) over the planning horizon
- Implement a simple MPC (Model Predictive Control) optimizer: given the thermal model, price forecast, and comfort constraints, find the cheapest heating schedule for the next 12–24 hours
- Thermal battery strategy: if electricity is cheap now and expensive later, pre-heat the room above target (use thermal mass as a battery)
- Configurable comfort vs. cost trade-off slider (0 = cheapest possible, 100 = always perfect comfort)
- Dashboard card showing: projected cost today, savings vs. naive heating, planned heating schedule

**Key insight:** A well-insulated room with high thermal mass can be pre-heated during cheap hours and coast through expensive hours.

---

## Phase 5 — Heat Pump COP Optimization (v0.5)

**Goal:** Account for heat pump efficiency curves in the cost optimization.

Tasks:
- Read heat pump COP from a sensor entity (many heat pump integrations expose this)
- Model COP as a function of outdoor temperature and flow temperature: COP ≈ f(T_outdoor, T_flow)
- Integrate COP into the cost function: effective_cost = electricity_price / COP
- Flow temperature optimization: calculate the lowest possible flow temperature that still meets heating demand — lower flow temp = higher COP = lower cost
- Boiler temperature setpoint control: expose a service to adjust the heat pump's hot water / buffer tank setpoint based on the optimizer's recommendation
- Defrost cycle awareness: account for efficiency drops during defrost at low outdoor temps

**Key insight:** Running the heat pump at a lower flow temperature with longer run times is usually cheaper than short bursts at high flow temperature.

---

## Phase 6 — Multi-Room Coordination (v0.6)

**Goal:** Optimize heating across all rooms simultaneously.

Tasks:
- Global optimizer that coordinates all room controllers
- Respect maximum electrical power constraints (don't run all rooms at full heat simultaneously)
- Prioritize rooms based on occupancy, schedule, and thermal urgency
- Zone grouping: rooms on the same heating circuit share constraints
- Heat distribution modeling: account for internal heat transfer between rooms (warm living room heats adjacent hallway)

---

## Phase 7 — Advanced Features (v0.7+)

Future possibilities:
- Humidity-aware control (mold prevention via DIN 4108-2, as RoomMind does)
- Cooling support for reversible heat pumps
- Hot water scheduling optimization (pre-heat DHW during cheap electricity)
- Machine learning layer on top of the physics model for anomaly detection
- Custom Lovelace card showing thermal model visualization, cost projections, and predicted temperature curves
- Valve maintenance cycling (periodic open/close to prevent sticking, as Better Thermostat does)
- TRV calibration: auto-calibrate radiator valve offset based on external sensor readings

---

## File Structure

```
custom_components/predictive_heating/
├── __init__.py          # Integration setup, model persistence, panel registration
├── manifest.json        # HACS metadata (requires numpy)
├── const.py             # Constants and defaults
├── config_flow.py       # UI configuration flow
├── climate.py           # Main climate entity (wraps underlying TRV)
├── sensor.py            # Diagnostic sensors (model state, heat loss, progress)
├── controller.py        # Heating controller (hysteresis now, MPC later)
├── thermal_model.py     # Self-learning room thermal model (EKF + simple fallback)
├── ekf.py               # Extended Kalman Filter for thermal parameter estimation
├── solar.py             # Solar irradiance estimation from sun.sun + weather
├── mpc.py               # Model Predictive Control (v0.3) — overshoot prevention
├── preheat.py           # Pre-heat planner (v0.3) — schedule + forecast → lead time
├── presence.py          # Presence monitor (v0.3) — person.* → Away auto-switch
├── frontend_panel.py    # Sidebar panel registration + WebSocket API
├── frontend/
│   └── entrypoint.js    # Dashboard UI (room cards, charts, training progress)
├── strings.json         # UI strings
└── translations/
    └── en.json          # English translations

tests/
├── test_thermal_model.py  # Verification tests (EKF, predictions, serialization)
└── test_v03.py            # v0.3 tests (MPC overshoot, preheat timing, presence)

hacs.json                  # HACS integration metadata
ROADMAP.md                 # This file
```

---

## Key Design Principles

1. **Simple physics first** — The thermal model is based on real heat transfer equations, not black-box ML. This means it works with very little data and its behavior is predictable and debuggable.

2. **Graceful degradation** — Before the model is calibrated, the system falls back to simple hysteresis control. Every phase adds capability without breaking the previous phase.

3. **Local-only** — No cloud dependencies. All computation runs on the HA instance.

4. **One room = one config entry** — Each room is independently configured and learns its own thermal characteristics. Multi-room coordination is layered on top later.

5. **Wrap, don't replace** — The integration wraps existing climate entities rather than directly controlling hardware. This means it works with any TRV, thermostat, or heat pump that HA supports.
