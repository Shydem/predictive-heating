# Predictive Heating - Home Assistant Integration

A Home Assistant custom integration that models your house as a **first-order lumped capacitance thermal model** and optimizes heating schedules based on energy prices, weather, and occupancy.

## Features

- **Self-training thermal model**: Automatically fits UA (heat loss coefficient) and C (thermal capacitance) parameters weekly from the past month of data
- **Multi-source heating**: Supports gas boilers and heat pumps simultaneously, with on/off and stepless (modulating) control
- **Price optimization**: Minimizes total heating cost using future electricity prices (e.g. Nordpool) and gas prices
- **Solar gain estimation**: Accounts for passive solar heating through windows
- **Flexible inputs**: Subtracts outdoor electrical loads (EV chargers, etc.) from total consumption
- **Desired temperature schedule**: Follows your comfort preferences throughout the day

## How It Works

The integration models your home as a single thermal node:

```
C * dT/dt = Q_heating + Q_solar + Q_internal - UA * (T_indoor - T_outdoor)
```

Where:
- **C** = thermal capacitance of the building (J/K)
- **UA** = overall heat loss coefficient (W/K)
- **Q_heating** = heat input from gas boiler + heat pump
- **Q_solar** = passive solar gain (estimated from weather data)
- **Q_internal** = internal gains (cooking, appliances, people)
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

### Required Entity IDs

| Parameter | Description |
|---|---|
| Indoor temperature sensor | e.g. `sensor.living_room_temperature` |
| Outdoor temperature sensor | e.g. `sensor.outdoor_temperature` |
| Gas consumption sensor | Total gas meter (m³ or kWh) |
| Total electricity sensor | Total electricity consumption (kWh) |
| Heat pump power sensor | Heat pump electrical consumption (kWh, optional) |
| Electricity price sensor | e.g. Nordpool sensor with future prices |
| Weather entity | HA weather entity for forecast (optional) |

### Heating Devices

Add one or more heating devices (gas boiler, heat pump, etc.) with:
- Entity ID of the climate/switch entity
- Type: `on_off` or `stepless`
- Source: `gas` or `electric`
- Maximum heat output (W)

### Model Parameters

| Parameter | Default | Description |
|---|---|
| Heating/hot water fraction | 0.85 | Fraction of gas used for space heating vs. hot water |
| Gas heating efficiency | 0.90 | Boiler efficiency |
| Heat pump COP coefficients | [2.8, 0.05] | COP = a + b * T_outdoor |
| Outdoor electric loads (W) | 0 | EV chargers, outdoor lighting, etc. to subtract |
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

## License

MIT
