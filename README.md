# Predictive Heating - Home Assistant Integration

A Home Assistant custom integration that models your house as a **first-order lumped capacitance thermal model** and optimizes heating schedules based on energy prices, weather, and occupancy.

## Features

- **Thermostat setpoint output** — tells each device what temperature to set, not on/off. Works with any climate entity (Daikin, Climatherm, OpenTherm CV, etc.)
- **Auto-control mode** — optionally pushes the computed setpoint to your thermostat every 5 minutes
- **Self-training thermal model** — fits UA and C parameters weekly from historical data
- **Per-device COP curves** — enter your heat pump's spec sheet data points, the model interpolates for any outdoor temperature
- **Multiple heat pumps** — each with their own COP curve; optimizer picks the cheapest one first
- **Gas-free support** — all gas-related fields are optional
- **Minimal sensor requirements** — only indoor temp, outdoor temp, and electricity price are required
- **Price optimization** — auto-detects Nordpool (HACS or official), ENTSO-e, or any price sensor
- **Solar gain estimation** — uses weather forecast cloud coverage

## How It Works

The integration models your home as a single thermal node:

```
C × dT/dt = Q_heating + Q_solar + Q_internal − UA × (T_indoor − T_outdoor)
```

Every week, UA and C are re-fitted from sensor data. The optimizer then computes the cheapest heating plan over the next 24h and outputs a **thermostat setpoint** for each device:

| Situation | Setpoint |
|---|---|
| Heating needed | Your schedule target (e.g. 20°C) |
| Preheating (next hour is expensive) | Target + 0.5°C |
| No heating needed | Away temperature (default 15°C) |
| Secondary device (primary is cheaper) | Away temperature |

## Installation

### HACS (Recommended)

1. HACS → Custom repositories → Add this URL (category: Integration)
2. Search "Predictive Heating" → Install → Restart HA
3. Settings → Devices & Services → Add Integration → Predictive Heating

### Manual

Copy `custom_components/predictive_heating/` to your HA `config/custom_components/` directory.

## Configuration

### Step 1: Sensor Entities

| Entity | Required | Notes |
|---|---|---|
| Indoor temperature | **Yes** | `sensor.living_room_temperature` |
| Outdoor temperature | **Yes** | `sensor.outdoor_temperature` |
| Electricity price | **Yes** | Nordpool, ENTSO-e, or any price sensor |
| Gas consumption | No | Only if you have gas heating |
| Total electricity | No | Used to estimate internal heat gains |
| Heat pump electricity | No | Cumulative kWh from Daikin, Climatherm, etc. |
| Weather entity | No | For temperature/cloud forecast |

### Step 2: House Profile

Describe your house (type, floor area, insulation, thermal mass) for cold-start estimates. Refined automatically after 1-2 weeks.

### Step 3: Model Parameters

Only relevant fields are shown based on your setup.

| Parameter | Default | Notes |
|---|---|---|
| Away temperature | 15°C | Setpoint when device shouldn't heat |
| Auto-control | Off | Push setpoints to thermostats automatically |
| Gas heating fraction | 0.85 | Only shown if gas sensor configured |
| Gas efficiency | 0.90 | Only shown if gas sensor configured |
| Gas price | €1.00/m³ | Only shown if gas sensor configured |
| Background heat gain | 200W | Only shown if no total electricity sensor |
| Training interval | 7 days | |
| Prediction horizon | 24 hours | |

### Step 4: Heating Devices

Add each heating device with:
- **Name** (e.g. "Daikin", "Climatherm", "CV Ketel")
- **Entity** (the climate entity of this device)
- **Type**: On/Off or Stepless (modulating)
- **Source**: Gas or Electric
- **Max output** (W)
- **COP data** (electric only): pairs from the spec sheet, e.g. `-7:2.5, 2:3.2, 7:4.0, 12:4.8`

If no COP data is entered, a typical air-source curve is used.

### Supported Price Integrations

Auto-detected from sensor attributes:

| Integration | Format |
|---|---|
| Custom Nordpool (HACS) | `raw_today`/`raw_tomorrow` → `{start, value}` |
| ENTSO-e (HACS) | `prices` → `{time, price}` |
| Official Nordpool | `today`/`tomorrow` plain lists |
| Any sensor | Falls back to current state as flat rate |

## Sensors Created

### Model sensors

| Sensor | Description |
|---|---|
| `..._ua_value` | Heat loss coefficient (W/K) |
| `..._thermal_mass` | Thermal capacitance (kWh/K) |
| `..._predicted_temperature` | Model-predicted indoor temp |
| `..._estimated_cost_24h` | Estimated heating cost for next 24h |
| `..._model_fit_r2` | Goodness of fit (1.0 = perfect) |
| `..._current_target_temperature` | Active schedule target |

### Per device

| Sensor | Description |
|---|---|
| `..._recommended_setpoint` | **The main output.** Set your thermostat to this. |
| `..._recommended_state` | on/off (for automations) |
| `..._recommended_output` | 0-100% for modulating devices |
| `..._heat_output` | Estimated heat output in W |

### Using setpoints without auto-control

If you prefer manual automations:

```yaml
automation:
  - alias: "Apply Daikin setpoint"
    trigger:
      - platform: state
        entity_id: sensor.predictive_heating_daikin_recommended_setpoint
    action:
      - service: climate.set_temperature
        target:
          entity_id: climate.daikin
        data:
          temperature: "{{ states('sensor.predictive_heating_daikin_recommended_setpoint') | float }}"
```

## Services

| Service | Description |
|---|---|
| `predictive_heating.train_model` | Trigger immediate re-training |
| `predictive_heating.set_schedule` | Update temperature schedule |
| `predictive_heating.get_forecast` | Get the optimized heating plan |
| `predictive_heating.set_model_params` | Override UA / thermal mass |

## License

MIT
