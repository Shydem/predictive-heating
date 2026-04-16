/**
 * Predictive Heating — Dashboard Panel
 *
 * A custom Home Assistant sidebar panel that shows:
 * - Room overview cards with model state, temperatures, learning progress
 * - Detailed training analytics per room: observation history, prediction accuracy
 * - Temperature chart with indoor/outdoor temps and heating periods
 * - Thermal parameter evolution over time
 *
 * Registered via async_register_built_in_panel in __init__.py
 * Communicates with HA via the WebSocket API (hass.callWS)
 */

class PredictiveHeatingPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._rooms = [];
    this._selectedRoom = null;
    this._roomDetail = null;
    this._narrow = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._loadRooms();
    }
  }

  set narrow(narrow) {
    this._narrow = narrow;
  }

  set panel(panel) {
    this._panel = panel;
  }

  async _loadRooms() {
    try {
      const result = await this._hass.callWS({
        type: "predictive_heating/rooms",
      });
      this._rooms = result.rooms || [];
    } catch (e) {
      console.warn("Failed to load rooms, retrying in 2s...", e);
      setTimeout(() => this._loadRooms(), 2000);
      return;
    }
    this._render();

    // Auto-refresh every 30 seconds
    this._refreshInterval = setInterval(() => this._refresh(), 30000);
  }

  async _refresh() {
    try {
      const result = await this._hass.callWS({
        type: "predictive_heating/rooms",
      });
      this._rooms = result.rooms || [];

      if (this._selectedRoom) {
        await this._loadRoomDetail(this._selectedRoom);
      } else {
        this._render();
      }
    } catch (e) {
      console.warn("Refresh failed:", e);
    }
  }

  async _loadRoomDetail(entryId) {
    try {
      const result = await this._hass.callWS({
        type: "predictive_heating/room_detail",
        entry_id: entryId,
      });
      this._roomDetail = result;
      this._selectedRoom = entryId;
    } catch (e) {
      console.error("Failed to load room detail:", e);
      this._roomDetail = null;
    }
    this._render();
  }

  _selectRoom(entryId) {
    this._loadRoomDetail(entryId);
  }

  _goBack() {
    this._selectedRoom = null;
    this._roomDetail = null;
    this._render();
  }

  disconnectedCallback() {
    if (this._refreshInterval) {
      clearInterval(this._refreshInterval);
    }
  }

  _render() {
    const root = this.shadowRoot;
    root.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = this._getStyles();
    root.appendChild(style);

    const container = document.createElement("div");
    container.className = "container";

    // Header
    const header = document.createElement("div");
    header.className = "header";

    if (this._selectedRoom && this._roomDetail) {
      header.innerHTML = `
        <button class="back-btn" id="back-btn">
          <svg width="24" height="24" viewBox="0 0 24 24"><path fill="currentColor" d="M20,11V13H8L13.5,18.5L12.08,19.92L4.16,12L12.08,4.08L13.5,5.5L8,11H20Z"/></svg>
        </button>
        <div class="header-text">
          <h1>${this._roomDetail.room_name}</h1>
          <span class="subtitle">Thermal Model Details</span>
        </div>
      `;
      container.appendChild(header);
      this._renderRoomDetail(container);
    } else {
      header.innerHTML = `
        <div class="header-text">
          <h1>🌡️ Predictive Heating</h1>
          <span class="subtitle">${this._rooms.length} room${this._rooms.length !== 1 ? "s" : ""} configured</span>
        </div>
      `;
      container.appendChild(header);
      this._renderOverview(container);
    }

    root.appendChild(container);

    // Bind back button
    const backBtn = root.getElementById("back-btn");
    if (backBtn) {
      backBtn.addEventListener("click", () => this._goBack());
    }

    // Bind room card clicks
    root.querySelectorAll(".room-card").forEach((card) => {
      card.addEventListener("click", () => {
        this._selectRoom(card.dataset.entryId);
      });
    });
  }

  // ─── Overview: room cards ─────────────────────────────────
  _renderOverview(container) {
    if (this._rooms.length === 0) {
      container.innerHTML += `
        <div class="empty-state">
          <svg width="64" height="64" viewBox="0 0 24 24"><path fill="var(--text-muted)" d="M17,3H7A2,2 0 0,0 5,5V21L12,18L19,21V5C19,3.89 18.1,3 17,3Z"/></svg>
          <h2>No rooms configured</h2>
          <p>Add rooms via Settings → Devices & Services → Predictive Heating</p>
        </div>
      `;
      return;
    }

    const grid = document.createElement("div");
    grid.className = "room-grid";

    for (const room of this._rooms) {
      const stateColor = room.model_state === "calibrated" ? "var(--success)" : "var(--warning)";
      const stateIcon = room.model_state === "calibrated" ? "✓" : "◌";
      const progressPct = Math.min(100, room.learning_progress || 0);

      const card = document.createElement("div");
      card.className = "room-card";
      card.dataset.entryId = room.entry_id;

      card.innerHTML = `
        <div class="card-header">
          <span class="room-name">${room.room_name}</span>
          <span class="model-badge" style="background:${stateColor}">${stateIcon} ${room.model_state}</span>
        </div>

        <div class="temp-display">
          <div class="temp-current">
            <span class="temp-value">${room.current_temp !== null ? room.current_temp.toFixed(1) : "--"}°</span>
            <span class="temp-label">Indoor</span>
          </div>
          <div class="temp-target">
            <span class="temp-value">${room.target_temp !== null ? room.target_temp.toFixed(1) : "--"}°</span>
            <span class="temp-label">Target</span>
          </div>
          <div class="temp-outdoor">
            <span class="temp-value">${room.outdoor_temp !== null ? room.outdoor_temp.toFixed(1) : "--"}°</span>
            <span class="temp-label">Outdoor</span>
          </div>
        </div>

        <div class="progress-section">
          <div class="progress-header">
            <span>Learning Progress</span>
            <span class="progress-pct">${progressPct}%</span>
          </div>
          <div class="progress-bar">
            <div class="progress-fill" style="width:${progressPct}%;background:${stateColor}"></div>
          </div>
          <div class="progress-detail">
            <span>Idle: ${room.idle_samples}/${room.min_idle}</span>
            <span>Active: ${room.active_samples}/${room.min_active}</span>
          </div>
        </div>

        <div class="zone-info">
          ${room.zone_rooms && room.zone_rooms.length > 1
            ? `<span class="zone-badge">🔗 Zone: ${room.zone_rooms.join(", ")}</span>`
            : ""
          }
          ${room.zone_setpoint != null
            ? `<span class="setpoint-info">Setpoint: ${room.zone_setpoint}°C</span>`
            : ""
          }
        </div>

        <div class="card-footer">
          <span class="hvac-state ${room.zone_is_heating ? "heating" : room.hvac_action}">${room.zone_is_heating ? "heating" : (room.hvac_action || "idle")}</span>
          <span class="heat-loss">H = ${room.heat_loss_coeff.toFixed(1)} W/K</span>
        </div>
      `;

      grid.appendChild(card);
    }

    container.appendChild(grid);
  }

  // ─── Room detail view ────────────────────────────────────
  _renderRoomDetail(container) {
    const d = this._roomDetail;
    if (!d) return;

    const detail = document.createElement("div");
    detail.className = "detail-view";

    // Stats cards row
    detail.innerHTML = `
      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-icon">🌡️</div>
          <div class="stat-value">${d.current_temp !== null ? d.current_temp.toFixed(1) + "°C" : "--"}</div>
          <div class="stat-label">Indoor Temperature</div>
        </div>
        <div class="stat-card">
          <div class="stat-icon">🎯</div>
          <div class="stat-value">${d.target_temp !== null ? d.target_temp.toFixed(1) + "°C" : "--"}</div>
          <div class="stat-label">Target Temperature</div>
        </div>
        <div class="stat-card">
          <div class="stat-icon">❄️</div>
          <div class="stat-value">${d.outdoor_temp !== null ? d.outdoor_temp.toFixed(1) + "°C" : "--"}</div>
          <div class="stat-label">Outdoor Temperature</div>
        </div>
        <div class="stat-card">
          <div class="stat-icon">${d.hvac_action === "heating" ? "🔥" : "💤"}</div>
          <div class="stat-value">${d.hvac_action || "idle"}</div>
          <div class="stat-label">Current Action</div>
        </div>
      </div>
    `;

    // Thermal Model Parameters
    detail.innerHTML += `
      <div class="section">
        <h2>Thermal Model Parameters</h2>
        <div class="params-grid">
          <div class="param-card">
            <div class="param-name">Heat Loss Coefficient (H)</div>
            <div class="param-value">${d.params.heat_loss_coeff.toFixed(1)} <span class="param-unit">W/K</span></div>
            <div class="param-desc">How fast the room loses heat per degree temperature difference</div>
          </div>
          <div class="param-card">
            <div class="param-name">Thermal Mass (C)</div>
            <div class="param-value">${d.params.thermal_mass.toFixed(0)} <span class="param-unit">kJ/K</span></div>
            <div class="param-desc">How much energy the room stores — higher = slower to heat/cool</div>
          </div>
          <div class="param-card">
            <div class="param-name">Heating Power</div>
            <div class="param-value">${d.params.heating_power.toFixed(0)} <span class="param-unit">W</span></div>
            <div class="param-desc">Maximum heating output for this room</div>
          </div>
          <div class="param-card">
            <div class="param-name">Solar Gain Factor</div>
            <div class="param-value">${d.params.solar_gain_factor.toFixed(2)}</div>
            <div class="param-desc">Fraction of solar irradiance that heats the room</div>
          </div>
        </div>
        <div class="engine-badge">${d.uses_ekf ? "⚡ Extended Kalman Filter (v0.2)" : "📊 Simple Estimator (v0.1)"}</div>
        ${d.mean_prediction_error != null ? `<div class="prediction-accuracy">Prediction accuracy: <strong>±${d.mean_prediction_error.toFixed(3)}°C</strong> ${d.mean_prediction_error < 0.5 ? "✓" : "(target: < 0.5°C)"}</div>` : ""}
      </div>
    `;

    // Learning Progress Detail
    const progressPct = Math.min(100, d.learning_progress || 0);
    const idlePct = Math.min(100, (d.idle_samples / d.min_idle) * 100);
    const activePct = Math.min(100, (d.active_samples / d.min_active) * 100);

    detail.innerHTML += `
      <div class="section">
        <h2>Training Progress</h2>
        <div class="training-detail">
          <div class="training-overall">
            <div class="circle-progress" style="--progress: ${progressPct}">
              <svg viewBox="0 0 100 100">
                <circle class="bg" cx="50" cy="50" r="42"/>
                <circle class="fg" cx="50" cy="50" r="42" style="stroke-dasharray: ${progressPct * 2.64} 264"/>
                <text x="50" y="50" text-anchor="middle" dominant-baseline="central" class="circle-text">${progressPct}%</text>
              </svg>
            </div>
            <div class="training-status">
              <span class="model-badge ${d.model_state}" style="background: ${d.model_state === "calibrated" ? "var(--success)" : "var(--warning)"}">${d.model_state === "calibrated" ? "✓ Calibrated" : "◌ Learning"}</span>
              <p>${d.model_state === "calibrated"
                ? "The thermal model is calibrated and predictions are active."
                : "The model is still learning. Hysteresis control is used as fallback."
              }</p>
            </div>
          </div>

          <div class="sample-bars">
            <div class="sample-bar-group">
              <div class="sample-bar-header">
                <span>Idle Observations</span>
                <span>${d.idle_samples} / ${d.min_idle}</span>
              </div>
              <div class="progress-bar large">
                <div class="progress-fill" style="width:${idlePct}%;background:var(--blue)"></div>
              </div>
              <div class="sample-bar-desc">Collected when heating is off — used to learn heat loss rate</div>
            </div>
            <div class="sample-bar-group">
              <div class="sample-bar-header">
                <span>Active Observations</span>
                <span>${d.active_samples} / ${d.min_active}</span>
              </div>
              <div class="progress-bar large">
                <div class="progress-fill" style="width:${activePct}%;background:var(--orange)"></div>
              </div>
              <div class="sample-bar-desc">Collected during heating — used to learn heating power</div>
            </div>
          </div>
        </div>
      </div>
    `;

    // Temperature History Chart (canvas)
    detail.innerHTML += `
      <div class="section">
        <h2>Temperature History</h2>
        <div class="chart-container">
          <canvas id="temp-chart" width="800" height="300"></canvas>
        </div>
        <div class="chart-legend">
          <span class="legend-item"><span class="legend-dot" style="background:var(--blue)"></span>Indoor</span>
          <span class="legend-item"><span class="legend-dot" style="background:var(--cyan)"></span>Outdoor</span>
          <span class="legend-item"><span class="legend-dot" style="background:var(--orange)"></span>Target</span>
          <span class="legend-item"><span class="legend-shade" style="background:var(--red-dim)"></span>Heating</span>
        </div>
      </div>
    `;

    // H/C learning evolution chart
    detail.innerHTML += `
      <div class="section">
        <h2>Heat Loss Learning History</h2>
        <div class="chart-container">
          <canvas id="learning-chart" width="800" height="200"></canvas>
        </div>
        <p class="chart-note">Shows how the estimated heat loss coefficient (H) evolves as more observations are collected.</p>
      </div>
    `;

    // Prediction error evolution chart
    if (d.prediction_error_history && d.prediction_error_history.length > 1) {
      detail.innerHTML += `
        <div class="section">
          <h2>Prediction Accuracy Over Time</h2>
          <div class="chart-container">
            <canvas id="error-chart" width="800" height="200"></canvas>
          </div>
          <div class="chart-legend">
            <span class="legend-item"><span class="legend-dot" style="background:var(--red)"></span>Mean abs. error</span>
            <span class="legend-item"><span class="legend-dot" style="background:var(--green)"></span>Calibration threshold (0.5°C)</span>
          </div>
          <p class="chart-note">When the prediction error drops below 0.5°C, the model auto-calibrates and predictive control activates.</p>
        </div>
      `;
    }

    // Prediction section (only if calibrated)
    if (d.model_state === "calibrated" && d.predictions) {
      detail.innerHTML += `
        <div class="section">
          <h2>Predictions</h2>
          <div class="predictions-grid">
            <div class="prediction-card">
              <div class="prediction-label">Temp in 1 hour (heating off)</div>
              <div class="prediction-value">${d.predictions.temp_1h_off !== null ? d.predictions.temp_1h_off.toFixed(1) + "°C" : "--"}</div>
            </div>
            <div class="prediction-card">
              <div class="prediction-label">Temp in 1 hour (heating on)</div>
              <div class="prediction-value">${d.predictions.temp_1h_on !== null ? d.predictions.temp_1h_on.toFixed(1) + "°C" : "--"}</div>
            </div>
            <div class="prediction-card">
              <div class="prediction-label">Time to reach target</div>
              <div class="prediction-value">${d.predictions.time_to_target !== null ? d.predictions.time_to_target.toFixed(0) + " min" : "N/A"}</div>
            </div>
          </div>
        </div>
      `;
    }

    container.appendChild(detail);

    // Draw charts after DOM is ready
    requestAnimationFrame(() => {
      this._drawTempChart(d);
      this._drawLearningChart(d);
      this._drawErrorChart(d);
    });
  }

  // ─── Canvas charts ────────────────────────────────────────
  _drawTempChart(data) {
    const canvas = this.shadowRoot.getElementById("temp-chart");
    if (!canvas || !data.observations || data.observations.length < 2) return;

    const ctx = canvas.getContext("2d");
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * (window.devicePixelRatio || 1);
    canvas.height = 300 * (window.devicePixelRatio || 1);
    canvas.style.width = rect.width + "px";
    canvas.style.height = "300px";
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);

    const W = rect.width;
    const H = 300;
    const pad = { top: 20, right: 20, bottom: 40, left: 50 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    const obs = data.observations;
    const tMin = obs[0].timestamp;
    const tMax = obs[obs.length - 1].timestamp;
    const tRange = tMax - tMin || 1;

    // Find temp range
    let yMin = Infinity, yMax = -Infinity;
    for (const o of obs) {
      yMin = Math.min(yMin, o.t_indoor, o.t_outdoor);
      yMax = Math.max(yMax, o.t_indoor, o.t_outdoor);
    }
    if (data.target_temp != null) {
      yMin = Math.min(yMin, data.target_temp);
      yMax = Math.max(yMax, data.target_temp);
    }
    yMin = Math.floor(yMin - 1);
    yMax = Math.ceil(yMax + 1);
    const yRange = yMax - yMin || 1;

    const toX = (t) => pad.left + ((t - tMin) / tRange) * plotW;
    const toY = (v) => pad.top + (1 - (v - yMin) / yRange) * plotH;

    // Background
    ctx.fillStyle = "var(--card-bg, #1c1c1e)";
    ctx.fillRect(0, 0, W, H);

    // Grid lines
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    const ySteps = 5;
    for (let i = 0; i <= ySteps; i++) {
      const v = yMin + (yRange * i) / ySteps;
      const y = toY(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();

      ctx.fillStyle = "rgba(255,255,255,0.4)";
      ctx.font = "11px -apple-system, sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(1) + "°", pad.left - 8, y + 4);
    }

    // Time labels
    ctx.textAlign = "center";
    ctx.fillStyle = "rgba(255,255,255,0.4)";
    const tSteps = Math.min(6, obs.length);
    for (let i = 0; i <= tSteps; i++) {
      const t = tMin + (tRange * i) / tSteps;
      const x = toX(t);
      const date = new Date(t * 1000);
      ctx.fillText(date.getHours().toString().padStart(2, "0") + ":" + date.getMinutes().toString().padStart(2, "0"), x, H - pad.bottom + 20);
    }

    // Heating shading
    ctx.fillStyle = "rgba(255,69,58,0.12)";
    for (let i = 0; i < obs.length - 1; i++) {
      if (obs[i].heating_on) {
        const x1 = toX(obs[i].timestamp);
        const x2 = toX(obs[i + 1].timestamp);
        ctx.fillRect(x1, pad.top, x2 - x1, plotH);
      }
    }

    // Target temperature line (dashed)
    if (data.target_temp != null) {
      ctx.strokeStyle = "rgba(255,159,10,0.6)";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      const ty = toY(data.target_temp);
      ctx.moveTo(pad.left, ty);
      ctx.lineTo(W - pad.right, ty);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Outdoor temp line
    ctx.strokeStyle = "rgba(90,200,250,0.7)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (let i = 0; i < obs.length; i++) {
      const x = toX(obs[i].timestamp);
      const y = toY(obs[i].t_outdoor);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Indoor temp line
    ctx.strokeStyle = "rgba(10,132,255,1)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < obs.length; i++) {
      const x = toX(obs[i].timestamp);
      const y = toY(obs[i].t_indoor);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  _drawLearningChart(data) {
    const canvas = this.shadowRoot.getElementById("learning-chart");
    if (!canvas || !data.h_history || data.h_history.length < 2) return;

    const ctx = canvas.getContext("2d");
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * (window.devicePixelRatio || 1);
    canvas.height = 200 * (window.devicePixelRatio || 1);
    canvas.style.width = rect.width + "px";
    canvas.style.height = "200px";
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);

    const W = rect.width;
    const H = 200;
    const pad = { top: 20, right: 20, bottom: 30, left: 60 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    const hist = data.h_history;
    let yMin = Infinity, yMax = -Infinity;
    for (const v of hist) {
      yMin = Math.min(yMin, v.value);
      yMax = Math.max(yMax, v.value);
    }
    const margin = (yMax - yMin) * 0.15 || 10;
    yMin -= margin;
    yMax += margin;
    const yRange = yMax - yMin || 1;

    const toX = (i) => pad.left + (i / (hist.length - 1)) * plotW;
    const toY = (v) => pad.top + (1 - (v - yMin) / yRange) * plotH;

    // Background
    ctx.fillStyle = "var(--card-bg, #1c1c1e)";
    ctx.fillRect(0, 0, W, H);

    // Grid
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = yMin + (yRange * i) / 4;
      const y = toY(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,0.4)";
      ctx.font = "11px -apple-system, sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(1), pad.left - 8, y + 4);
    }

    // Y-axis label
    ctx.save();
    ctx.translate(14, H / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = "rgba(255,255,255,0.4)";
    ctx.textAlign = "center";
    ctx.fillText("W/K", 0, 0);
    ctx.restore();

    // X-axis label
    ctx.fillStyle = "rgba(255,255,255,0.4)";
    ctx.textAlign = "center";
    ctx.fillText("Sample #", W / 2, H - 4);

    // Line
    ctx.strokeStyle = "rgba(48,209,88,0.9)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < hist.length; i++) {
      const x = toX(i);
      const y = toY(hist[i].value);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Dots
    ctx.fillStyle = "rgba(48,209,88,1)";
    for (let i = 0; i < hist.length; i++) {
      const x = toX(i);
      const y = toY(hist[i].value);
      ctx.beginPath();
      ctx.arc(x, y, 2.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  _drawErrorChart(data) {
    const canvas = this.shadowRoot.getElementById("error-chart");
    if (!canvas || !data.prediction_error_history || data.prediction_error_history.length < 2) return;

    const ctx = canvas.getContext("2d");
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * (window.devicePixelRatio || 1);
    canvas.height = 200 * (window.devicePixelRatio || 1);
    canvas.style.width = rect.width + "px";
    canvas.style.height = "200px";
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);

    const W = rect.width;
    const H = 200;
    const pad = { top: 20, right: 20, bottom: 30, left: 60 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    const hist = data.prediction_error_history;
    const yMax = Math.max(1.0, ...hist.map(h => h.value)) * 1.1;
    const yMin = 0;
    const yRange = yMax - yMin;

    const toX = (i) => pad.left + (i / (hist.length - 1)) * plotW;
    const toY = (v) => pad.top + (1 - (v - yMin) / yRange) * plotH;

    // Background
    ctx.fillStyle = "var(--card-bg, #1c1c1e)";
    ctx.fillRect(0, 0, W, H);

    // Grid
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = yMin + (yRange * i) / 4;
      const y = toY(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = "rgba(255,255,255,0.4)";
      ctx.font = "11px -apple-system, sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(2) + "°", pad.left - 8, y + 4);
    }

    // Calibration threshold line (0.5°C)
    ctx.strokeStyle = "rgba(48,209,88,0.6)";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    const threshY = toY(0.5);
    ctx.moveTo(pad.left, threshY);
    ctx.lineTo(W - pad.right, threshY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Error line
    ctx.strokeStyle = "rgba(255,69,58,0.9)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < hist.length; i++) {
      const x = toX(i);
      const y = toY(hist[i].value);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Y-axis label
    ctx.save();
    ctx.translate(14, H / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = "rgba(255,255,255,0.4)";
    ctx.textAlign = "center";
    ctx.fillText("°C error", 0, 0);
    ctx.restore();
  }

  // ─── Styles ───────────────────────────────────────────────
  _getStyles() {
    return `
      :host {
        --bg: #111113;
        --card-bg: #1c1c1e;
        --card-border: rgba(255,255,255,0.08);
        --text: #f5f5f7;
        --text-secondary: rgba(255,255,255,0.55);
        --text-muted: rgba(255,255,255,0.3);
        --blue: #0a84ff;
        --cyan: #5ac8fa;
        --green: #30d158;
        --orange: #ff9f0a;
        --red: #ff453a;
        --red-dim: rgba(255,69,58,0.15);
        --success: #30d158;
        --warning: #ff9f0a;
        --radius: 12px;
        --shadow: 0 2px 12px rgba(0,0,0,0.3);

        display: block;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: var(--bg);
        color: var(--text);
        min-height: 100vh;
      }

      @media (prefers-color-scheme: light) {
        :host {
          --bg: #f2f2f7;
          --card-bg: #ffffff;
          --card-border: rgba(0,0,0,0.08);
          --text: #1c1c1e;
          --text-secondary: rgba(0,0,0,0.55);
          --text-muted: rgba(0,0,0,0.25);
          --shadow: 0 2px 12px rgba(0,0,0,0.08);
        }
      }

      .container {
        max-width: 1200px;
        margin: 0 auto;
        padding: 24px;
      }

      .header {
        display: flex;
        align-items: center;
        gap: 16px;
        margin-bottom: 32px;
      }

      .header h1 {
        margin: 0;
        font-size: 28px;
        font-weight: 700;
      }

      .subtitle {
        color: var(--text-secondary);
        font-size: 14px;
      }

      .back-btn {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 10px;
        color: var(--blue);
        width: 40px;
        height: 40px;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: background 0.15s;
      }
      .back-btn:hover { background: rgba(10,132,255,0.1); }

      /* ── Room Grid ───────────── */
      .room-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
        gap: 20px;
      }

      .room-card {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: var(--radius);
        padding: 20px;
        cursor: pointer;
        transition: transform 0.15s, box-shadow 0.15s;
      }
      .room-card:hover {
        transform: translateY(-2px);
        box-shadow: var(--shadow);
      }

      .card-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 16px;
      }

      .room-name {
        font-size: 18px;
        font-weight: 600;
      }

      .model-badge {
        font-size: 11px;
        font-weight: 600;
        padding: 3px 10px;
        border-radius: 20px;
        color: #000;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }

      .temp-display {
        display: flex;
        justify-content: space-around;
        margin-bottom: 16px;
        text-align: center;
      }

      .temp-value {
        font-size: 24px;
        font-weight: 700;
        display: block;
      }
      .temp-label {
        font-size: 11px;
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .temp-current .temp-value { color: var(--blue); }
      .temp-target .temp-value { color: var(--orange); }
      .temp-outdoor .temp-value { color: var(--cyan); }

      .progress-section { margin-bottom: 12px; }
      .progress-header {
        display: flex;
        justify-content: space-between;
        font-size: 13px;
        color: var(--text-secondary);
        margin-bottom: 6px;
      }
      .progress-pct { font-weight: 600; color: var(--text); }

      .progress-bar {
        height: 6px;
        background: rgba(255,255,255,0.08);
        border-radius: 3px;
        overflow: hidden;
      }
      .progress-bar.large { height: 8px; border-radius: 4px; }
      .progress-fill {
        height: 100%;
        border-radius: inherit;
        transition: width 0.6s ease;
      }

      .progress-detail {
        display: flex;
        justify-content: space-between;
        font-size: 11px;
        color: var(--text-muted);
        margin-top: 4px;
      }

      .card-footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 12px;
        color: var(--text-secondary);
        padding-top: 12px;
        border-top: 1px solid var(--card-border);
      }

      .zone-info {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-bottom: 10px;
        font-size: 11px;
      }
      .zone-badge {
        background: rgba(10,132,255,0.1);
        color: var(--blue);
        padding: 2px 8px;
        border-radius: 6px;
      }
      .setpoint-info {
        color: var(--text-secondary);
        padding: 2px 8px;
      }

      .hvac-state {
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .hvac-state.heating { color: var(--red); }
      .hvac-state.idle { color: var(--text-muted); }

      /* ── Detail View ──────────── */
      .detail-view { }

      .stats-row {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 16px;
        margin-bottom: 28px;
      }

      .stat-card {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: var(--radius);
        padding: 16px;
        text-align: center;
      }
      .stat-icon { font-size: 24px; margin-bottom: 8px; }
      .stat-value { font-size: 22px; font-weight: 700; }
      .stat-label { font-size: 11px; color: var(--text-secondary); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }

      .section {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: var(--radius);
        padding: 24px;
        margin-bottom: 20px;
      }
      .section h2 {
        margin: 0 0 20px 0;
        font-size: 18px;
        font-weight: 600;
      }

      .params-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 16px;
      }
      .param-card {
        background: rgba(255,255,255,0.03);
        border-radius: 8px;
        padding: 16px;
      }
      .param-name { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
      .param-value { font-size: 28px; font-weight: 700; }
      .param-unit { font-size: 14px; font-weight: 400; color: var(--text-secondary); }
      .param-desc { font-size: 12px; color: var(--text-muted); margin-top: 8px; line-height: 1.4; }

      /* Training progress */
      .training-detail { }
      .training-overall {
        display: flex;
        align-items: center;
        gap: 24px;
        margin-bottom: 24px;
      }

      .circle-progress { width: 100px; height: 100px; flex-shrink: 0; }
      .circle-progress svg { width: 100%; height: 100%; transform: rotate(-90deg); }
      .circle-progress .bg { fill: none; stroke: rgba(255,255,255,0.08); stroke-width: 8; }
      .circle-progress .fg { fill: none; stroke: var(--success); stroke-width: 8; stroke-linecap: round; transition: stroke-dasharray 0.6s; }
      .circle-progress .circle-text {
        font-size: 20px;
        font-weight: 700;
        fill: var(--text);
        transform: rotate(90deg);
        transform-origin: 50% 50%;
      }

      .training-status p { color: var(--text-secondary); font-size: 14px; margin: 8px 0 0; line-height: 1.5; }

      .sample-bars { display: flex; flex-direction: column; gap: 16px; }
      .sample-bar-group { }
      .sample-bar-header {
        display: flex;
        justify-content: space-between;
        font-size: 13px;
        font-weight: 500;
        margin-bottom: 6px;
      }
      .sample-bar-desc { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

      /* Charts */
      .chart-container {
        width: 100%;
        overflow: hidden;
        border-radius: 8px;
      }
      .chart-container canvas {
        display: block;
        width: 100%;
      }
      .chart-legend {
        display: flex;
        gap: 20px;
        margin-top: 12px;
        font-size: 12px;
        color: var(--text-secondary);
      }
      .legend-item { display: flex; align-items: center; gap: 6px; }
      .legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
      .legend-shade { width: 20px; height: 10px; border-radius: 3px; display: inline-block; }
      .chart-note { font-size: 12px; color: var(--text-muted); margin-top: 12px; }

      /* Predictions */
      .predictions-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 16px;
      }
      .prediction-card {
        background: rgba(10,132,255,0.08);
        border-radius: 8px;
        padding: 16px;
        text-align: center;
      }
      .prediction-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
      .prediction-value { font-size: 24px; font-weight: 700; color: var(--blue); }

      /* Empty state */
      .empty-state {
        text-align: center;
        padding: 80px 20px;
      }
      .empty-state h2 { margin: 16px 0 8px; font-size: 20px; }
      .empty-state p { color: var(--text-secondary); }

      /* Engine badge and prediction accuracy */
      .engine-badge {
        display: inline-block;
        margin-top: 16px;
        padding: 6px 14px;
        background: rgba(10,132,255,0.1);
        border: 1px solid rgba(10,132,255,0.2);
        border-radius: 8px;
        font-size: 13px;
        color: var(--blue);
        font-weight: 500;
      }
      .prediction-accuracy {
        margin-top: 10px;
        font-size: 14px;
        color: var(--text-secondary);
      }
      .prediction-accuracy strong { color: var(--text); }

      /* Responsive */
      @media (max-width: 600px) {
        .container { padding: 16px; }
        .header h1 { font-size: 22px; }
        .room-grid { grid-template-columns: 1fr; }
        .stats-row { grid-template-columns: repeat(2, 1fr); }
        .params-grid { grid-template-columns: 1fr; }
        .training-overall { flex-direction: column; text-align: center; }
      }
    `;
  }
}

customElements.define("predictive-heating-panel", PredictiveHeatingPanel);
