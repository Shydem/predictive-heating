# Predictive Heating - Home Assistant Integration

A Home Assistant custom integration that models your house as a **first-order lumped capacitance thermal model** and optimizes heating schedules based on energy prices, weather, and occupancy.

## Features

- **Self-training thermal model**: Automatically fits UA (heat loss coefficient) and C (thermal capacitance) parameters weekly from the past month of data
- **Multi-source heating**: Supports gas boilers and heat pumps simultaneously, with on/off and stepless (modulating) control
- **Per-device COP curves**: Enter your heat pump's manufacturer COP data points — the integration interpolates for any outdoor temperature
- **Multiple heat pumps**: Add as many devices as you need, each with their own performance curve
- **Gas-free support**: Works with heat-pump-only homes — gas sensors and parameters are fully optional
- **Price optimization**: Minimizes total heating cost using future electricity prices from Nordpool (custom HACS or official), ENTSO-e, or any sensor with price attributes
- **Solar gain estimation**: Accounts for passive solar heating through windows using weather forecast data
- **Flexible inputs**: All sensors except indoor/outdoor temperature and electricity price are optional

## How It Works

The integration models your home as a single thermal node:

```
C * dT/dt = Q_heating + Q_solar + Q_internal - UA * (T_indoor - T_outdoor)
```

Where:
- **C** = thermal capacitance of the building (J/K)
- **UA** = overall heat loss coefficient (W/K)
- **Q_heating** = heat input from all heating devices
- **Q_solar** = passive solar gain (estimated from weather forecast cloud coverage)
- **Q_internal** = internal gains (appliances, cooking, people)
- **T_indoor**, **T_outdoor** = indoor and outdoor temperatures

Every week, the model re-fits UA and C from recorded data using least-squares optimization. It then uses these parameters along with future energy prices and your desired temperature schedule to compute the cheapest heating plan.

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots → Custom repositories
3. Add this repository URL, category: Integration
4. Search for "Predictive Heating" and install
5. Restart Home Assistant
6. Go to Settings → Devices & Services → Add Integration → Predictive Heating

### Manual

Copy `custom_components/predictive_heating/` to your Home Assistant `config/custom_components/` directory.

## Configuration

The integration is configured through the UI config flow. You will need to provide:

### Sensor Entities

| Parameter | Required | Description |
|---|---|---|
| Indoor temperature sensor | **Yes** | e.g. `sensor.living_room_temperature` |
| Outdoor temperature sensor | **Yes** | e.g. `sensor.outdoor_temperature` |
| Electricity price sensor | **Yes** | Nordpool, ENTSO-e, or any sensor with price data |
| Gas consumption sensor | No | Total gas meter (m³ or kWh) — only if you have gas heating |
| Total electricity sensor | No | Total electricity consumption (kWh) — used to estimate internal heat gains |
| Heat pump power sensor | No | Heat pump electrical consumption (kWh) |
| Weather entity | No | HA weather entity for temperature/cloud forecast |

### Supported Price Integrations

The integration auto-detects the format from your electricity price sensor:

| Integration | Detected via | Format |
|---|---|---|
| Custom Nordpool (HACS) | `raw_today` / `raw_tomorrow` attributes | `{start, end, value}` dicts |
| ENTSO-e (HACS) | `prices` attribute | `{time, price}` dicts |
| Official Nordpool (HA Core) | `today` / `tomorrow` plain lists | Price per hour |
| Any price sensor | Sensor state | Flat rate fallback |

### Heating Devices

Add one or more heating devices with:
- Entity ID of the climate/switch entity
- Type: `on_off` or `stepless` (modulating)
- Source: `gas` or `electric`
- Maximum heat output (W)
- **COP data points** (electric devices only): Enter manufacturer data as `outdoor_temp:COP` pairs, e.g. `-7:2.5, 2:3.2, 7:4.0, 12:4.8`

If no COP data is provided for an electric device, a typical air-source curve is used as default.

### Model Parameters

Parameters shown depend on your setup — gas-specific fields only appear if you configured a gas sensor.

| Parameter | Default | Description |
|---|---|---|
| Heating/hot water fraction | 0.85 | Fraction of gas used for space heating vs. hot water |
| Gas heating efficiency | 0.90 | Boiler efficiency |
| Outdoor electric loads (W) | 0 | EV chargers, outdoor lighting, etc. to subtract |
| Background heat gain (W) | 200 | Heat from appliances/people (shown if no electricity sensor) |
| Gas price (€/m³) | 1.0 | Current gas price |
| Training interval (days) | 7 | How often to retrain the model |
| Training window (days) | 30 | How much history to use for training |
| Prediction horizon (hours) | 24 | How far ahead to optimize |
| Optimization timestep (min) | 15 | Resolution of the optimization |

## Sensors Created

| Sensor | Description |
|---|---|
| `sensor.predictive_heating_ua_value` | Fitted UA heat loss coefficient (W/K) |
| `sensor.predictive_heating_thermal_mass` | Fitted thermal capacitance (kWh/K) |
| `sensor.predictive_heating_predicted_temperature` | Model-predicted indoor temperature |
| `sensor.predictive_heating_estimated_cost_24h` | Estimated heating cost for next 24h |
| `sensor.predictive_heating_model_fit_r2` | R² goodness of fit from last training |
| `sensor.predictive_heating_next_training` | Timestamp of next scheduled training |

### Per heating device:
| Sensor | Description |
|---|---|
| `sensor.predictive_heating_{name}_recommended_state` | on/off recommendation |
| `sensor.predictive_heating_{name}_recommended_output` | 0-100% for stepless devices |
| `sensor.predictive_heating_{name}_heat_output_w` | Current estimated heat output |

## Services

| Service | Description |
|---|---|
| `predictive_heating.train_model` | Trigger immediate model re-training |
| `predictive_heating.set_schedule` | Update the desired temperature schedule |
| `predictive_heating.get_forecast` | Return the optimized heating plan as a forecast |
| `predictive_heating.set_model_params` | Manually override UA and/or thermal mass |

## License

MIT
