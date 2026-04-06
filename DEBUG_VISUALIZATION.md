# Predictive Heating - Debug & Visualization Guide

This guide explains how to view graphs about predicted vs. actual heating behavior for debugging purposes.

---

## Quick Start

### 1. **View 24-Hour Prediction with Attributes**

1. Go to **Settings → Developer Tools → States**
2. Search for `sensor.predictive_heating_24h_temperature_forecast`
3. Click on it and expand the **Attributes** section

**You'll see:**
- `timestamps`: Array of ISO 8601 times (every 15 minutes for next 24h)
- `predicted_temperatures`: Array of predicted indoor temps (°C)
- `heating_plan`: What devices are planned to be on/off each time slot
- `total_cost_24h`: Estimated energy cost for the plan

### 2. **View Optimization Debug Info**

1. Go to **States** (same as above)  
2. Search for `sensor.predictive_heating_optimization_debug_info`
3. Expand **Attributes**

**You'll see:**
- Current temps (indoor, outdoor, target)
- Model fit (R² - how good are the predictions?)
- Total cost breakdown
- Final predicted temperature
- Complete optimization trace

---

## Using the Dashboard

### Temperature Graphs Tab

The dashboard has a **Graphs** tab showing:
- 24-hour temperature history (actual vs predicted)
- 7-day temperature trend
- Device on/off activity
- Heating output power

**To view:**
1. Open the Predictive Heating dashboard
2. Click **Graphs** tab
3. Graphs update every 5 minutes

---

## Advanced: Export to Spreadsheet

To analyze trends over days/weeks:

### Option A: Via Developer Tools

1. **States tab** → find `sensor.predictive_heating_24h_temperature_forecast`
2. Copy the **timestamps** and **predicted_temperatures** arrays
3. Paste into Excel/Google Sheets
4. Create a chart

### Option B: Via HA History Stats

Use the `history_stats` integration to export historical data:

```yaml
history_stats:
  heating_runtime_today:
    entity_id: sensor.predictive_heating_comfoclime_recommended_state
    state: 'on'
    type: time
    period: day
```

Then use the resulting sensor in automations or charts.

---

## Understanding the Trace Data

### What's "Optimization Trace"?

The optimizer runs every 5 minutes and decides:
- **Which devices turn on/off**
- **How hard each device should work**
- **When to pre-heat to save money**

The trace shows every decision with reasoning.

### Example Trace Output

```json
{
  "phase": "optimize",
  "total_steps": 96,
  "last_steps": [
    {
      "time": "2026-04-06T12:45:00",
      "step": "slot_0",
      "inputs": {
        "t_current": 17.8,
        "t_outdoor": 12.0,
        "t_target": 19.0,
        "elec_price": -0.0514
      },
      "result": {
        "t_no_heat": 17.82,
        "t_after": 18.05,
        "total_heating_w": 10000.0,
        "energy_cost": -0.0268
      },
      "note": "T 17.8→18.1°C (target 19.0), heating 10000W"
    }
  ]
}
```

**What it means:**
- **t_current**: Indoor temp now (17.8°C)
- **t_no_heat**: What temp would be with zero heating (17.82°C - almost no change in 15 min)
- **t_after**: Predicted temp after heating (18.05°C)
- **t_target**: Desired temperature (19.0°C)

---

## Custom Cards for Graphing

### Install ApexCharts

For more control over graphs:

1. **HACS → Frontend → Custom Repositories**
2. Add: `https://github.com/RomRider/apexcharts-card`
3. Install **ApexCharts Card**

### Example Card: Predicted vs Actual

```yaml
type: custom:apexcharts-card
header:
  title: Temperature Prediction Performance
graph:
  span:
    unit: hour
series:
  - entity: sensor.living_room_temperature
    name: Actual Indoor
    type: line
    color: blue
  - entity: sensor.predictive_heating_predicted_temperature
    name: Predicted
    type: line
    color: red
  - entity: sensor.outdoor_temperature
    name: Outdoor
    type: line
    color: gray
  - entity: sensor.predictive_heating_current_target_temperature
    name: Target
    type: line
    color: orange
    stroke_width: 2
yaxis:
  min: 10
  max: 25
```

---

## Interpreting Behavior

### Good Predictions

✅ Predicted line (red) closely follows Actual (blue)  
✅ R² > 0.8 (check in Model Health section)  
✅ No sudden jumps in prediction

### Problems to Watch For

❌ Predictions diverge from actual → Model needs retraining  
❌ Very zigzagging predictions → Too many unstable devices  
❌ R² < 0.5 → Insufficient training data or wrong house profile

### What to do:

1. **Low R²?** → Run "Train Model Now" button
2. **Bad predictions?** → Check that temperature sensors are accurate
3. **Unstable heating?** → Look at device decision reasons in attributes

---

## Force Calculation (Manual Trigger)

To see new predictions without waiting 5 minutes:

1. **Developer Tools → Services**
2. Choose: `predictive_heating.force_optimization`
3. Click **Call Service**

Sensors update immediately with new prediction.

---

## Full Diagnostic Export

For developers/support:

1. **Settings → Devices & Services**
2. Find "Predictive Heating"
3. Click the **⋮** menu
4. Click **Download Diagnostics**

This exports:
- Current config
- Model parameters
- Last 10 optimization traces
- Training data quality
- All sensor states

---

## Example: Debugging a Bad Prediction

**Scenario:** Predicted 19.5°C but actual is 18°C

**Debug steps:**

```
1. Check current data:
   - sensor.predictive_heating_24h_temperature_forecast
   - Look at timestamps & predicted_temperatures arrays
   
2. Check model fit:
   - sensor.predictive_heating_model_fit_r2
   - If < 0.7, retrain: predictive_heating.train_model
   
3. Check device activity:
   - sensor.predictive_heating_comfoclime_recommended_state (or your device name)
   - Check the "reason" attribute — why is it on/off?
   
4. Check inputs:
   - sensor.predictive_heating_optimization_debug_info
   - Verify current temps, target, electricity prices are correct
   
5. Check trace:
   - sensor.predictive_heating_decision_trace
   - Look at optimization.last_steps to see slot-by-slot reasoning
```

---

## Enable Debug Logging

For maximum verbosity:

**Add to `configuration.yaml`:**

```yaml
logger:
  logs:
    custom_components.predictive_heating: debug
```

Then check **Settings → Logs** to see every decision in real-time.

---

## Common Questions

**Q: Why is prediction different each time I refresh?**
- Prediction updates every 5 minutes or when you call `force_optimization`

**Q: Can I graph the heating plan vs actual device activity?**
- Yes! Compare:
  - `sensor.predictive_heating_24h_temperature_forecast` (heating_plan array)
  - With actual device entity history (e.g., `climate.comfoclime_36`)

**Q: How do I export data for analysis?**
- See "Export to Spreadsheet" section above

**Q: What does negative electricity price mean?**
- You're getting paid to use power (happens during excess solar production)

---

## Files & Sensors to Know

| Entity | Purpose |
|--------|---------|
| `sensor.predictive_heating_24h_temperature_forecast` | Full 24h prediction with heating plan |
| `sensor.predictive_heating_optimization_debug_info` | Current optimization state |
| `sensor.predictive_heating_decision_trace` | Full trace of optimizer reasoning |
| `sensor.predictive_heating_predicted_temperature` | Current 1-slot-ahead prediction |
| `sensor.predictive_heating_model_fit_r2` | Model accuracy metric |
| Any `sensor.predictive_heating_*_recommended_state` | Per-device on/off recommendation |

---

## Need More Help?

1. Check the **Decision Trace** sensor attributes for detailed reasoning
2. Download **Diagnostics** and inspect the traces
3. Enable **debug logging** in `configuration.yaml`
4. Post the trace/diagnostic data to GitHub Issues

