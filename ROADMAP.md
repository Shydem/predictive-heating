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

## Phase 1 — Foundation (v0.1) ✅ CURRENT

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

## Phase 2 — Self-Learning Thermal Model (v0.2)

**Goal:** The model accurately predicts how each room heats and cools.

Tasks:
- Upgrade from simple H/C ratio estimation to a proper Extended Kalman Filter (as RoomMind does), tracking: heat loss coefficient (H), thermal mass (C), heating power, and solar gain factor as state variables
- Learn heating_power from active heating observations (currently only heat loss is learned from idle periods)
- Add solar gain estimation using HA's sun.sun entity (elevation + azimuth) and weather integration for cloud cover
- Implement prediction accuracy tracking (compare predicted vs actual temps) — auto-calibrate when error drops below 0.5°C
- Add room size / thermal mass configuration helper (estimate C from floor area × ceiling height × building type)
- Persist full observation history for model retraining after HA updates

**Inspired by:** RoomMind's EKF thermal model with per-room learning.

---

## Phase 3 — Predictive Pre-Heating (v0.3)

**Goal:** Start heating at the right time so the room is comfortable when you need it.

Tasks:
- Use thermal_model.time_to_reach() to calculate pre-heat start times
- Integrate with HA schedule helpers or the built-in scheduler to define "comfort windows" (e.g., 07:00–09:00, 17:00–23:00)
- Pre-heat controller: given a comfort window and the current state, decide when to start heating so the room hits target temp exactly on time
- Account for outdoor temperature forecasts (weather integration) — colder nights need earlier start
- Presence-based adjustment: use person entities to detect nobody-home and auto-switch to Away mode
- Configurable "comfort ramp" — some people want gradual warmup, others want instant

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
├── __init__.py          # Integration setup, model persistence
├── manifest.json        # HACS metadata
├── const.py             # Constants and defaults
├── config_flow.py       # UI configuration flow
├── climate.py           # Main climate entity (wraps underlying TRV)
├── sensor.py            # Diagnostic sensors (model state, heat loss, progress)
├── controller.py        # Heating controller (hysteresis now, MPC later)
├── thermal_model.py     # Self-learning room thermal model
├── strings.json         # UI strings
└── translations/
    └── en.json          # English translations
```

---

## Key Design Principles

1. **Simple physics first** — The thermal model is based on real heat transfer equations, not black-box ML. This means it works with very little data and its behavior is predictable and debuggable.

2. **Graceful degradation** — Before the model is calibrated, the system falls back to simple hysteresis control. Every phase adds capability without breaking the previous phase.

3. **Local-only** — No cloud dependencies. All computation runs on the HA instance.

4. **One room = one config entry** — Each room is independently configured and learns its own thermal characteristics. Multi-room coordination is layered on top later.

5. **Wrap, don't replace** — The integration wraps existing climate entities rather than directly controlling hardware. This means it works with any TRV, thermostat, or heat pump that HA supports.
