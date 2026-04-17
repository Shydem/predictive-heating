/**
 * Predictive Heating — Dashboard Panel
 *
 * Custom HA sidebar panel:
 *  • room cards grouped by heating zone
 *  • detailed thermal-model view per room
 *  • solar irradiance breakdown
 *  • orphan-cleanup tool
 *
 * Styling follows Home Assistant theme variables (no custom palette).
 * Icons are mdi:* via <ha-icon> (registered globally by HA core).
 *
 * Communicates with HA via hass.callWS().
 */

class PredictiveHeatingPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._rooms = [];
    this._orphans = [];
    this._selectedRoom = null;
    this._roomDetail = null;
    this._narrow = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._loadAll();
    }
  }

  set narrow(narrow) {
    this._narrow = narrow;
    if (this._initialized) this._render();
  }

  set panel(panel) {
    this._panel = panel;
  }

  async _loadAll() {
    await Promise.all([this._loadRooms(), this._loadOrphans()]);
    this._render();
    if (!this._refreshInterval) {
      this._refreshInterval = setInterval(() => this._refresh(), 30000);
    }
  }

  async _loadRooms() {
    try {
      const result = await this._hass.callWS({
        type: "predictive_heating/rooms",
      });
      this._rooms = result.rooms || [];
    } catch (e) {
      console.warn("Failed to load rooms:", e);
    }
  }

  async _loadOrphans() {
    try {
      const result = await this._hass.callWS({
        type: "predictive_heating/list_orphans",
      });
      this._orphans = result.orphans || [];
    } catch (e) {
      // Older backend without orphan API → ignore
      this._orphans = [];
    }
  }

  async _refresh() {
    await Promise.all([this._loadRooms(), this._loadOrphans()]);
    if (this._selectedRoom) {
      await this._loadRoomDetail(this._selectedRoom);
    } else {
      this._render();
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

  async _deleteOrphan(entryId) {
    if (
      !confirm(
        "Delete this stored thermal model? This cannot be undone — re-adding the room will start with a fresh model."
      )
    )
      return;
    try {
      await this._hass.callWS({
        type: "predictive_heating/delete_orphan",
        entry_id: entryId,
      });
      await this._loadOrphans();
      this._render();
    } catch (e) {
      console.error("Failed to delete orphan:", e);
      alert("Could not delete: " + (e.message || e));
    }
  }

  _selectRoom(entryId) {
    this._loadRoomDetail(entryId);
  }

  _goBack() {
    this._selectedRoom = null;
    this._roomDetail = null;
    this._render();
  }

  _toggleHaMenu() {
    // Bubble out of the shadow DOM so HA's main UI receives it.
    this.dispatchEvent(
      new Event("hass-toggle-menu", { bubbles: true, composed: true })
    );
  }

  disconnectedCallback() {
    if (this._refreshInterval) {
      clearInterval(this._refreshInterval);
      this._refreshInterval = null;
    }
  }

  // ─── Render ────────────────────────────────────────────────
  _render() {
    const root = this.shadowRoot;
    root.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = this._getStyles();
    root.appendChild(style);

    // App-bar style header (matches HA panels)
    const appBar = document.createElement("div");
    appBar.className = "app-bar";

    const detailMode = !!(this._selectedRoom && this._roomDetail);

    // Left: hamburger (always present so HA sidebar stays togglable)
    //  + back button when in detail view
    appBar.innerHTML = `
      <button class="icon-button menu" id="menu-btn" title="Menu">
        <ha-icon icon="mdi:menu"></ha-icon>
      </button>
      ${
        detailMode
          ? `<button class="icon-button" id="back-btn" title="Back">
              <ha-icon icon="mdi:arrow-left"></ha-icon>
            </button>`
          : ""
      }
      <div class="app-bar-title">
        ${
          detailMode
            ? this._roomDetail.room_name
            : "Predictive Heating"
        }
      </div>
      <button class="icon-button" id="refresh-btn" title="Refresh">
        <ha-icon icon="mdi:refresh"></ha-icon>
      </button>
    `;

    root.appendChild(appBar);

    const container = document.createElement("div");
    container.className = "container";

    if (detailMode) {
      this._renderRoomDetail(container);
    } else {
      this._renderOverview(container);
    }

    root.appendChild(container);

    // Bind buttons
    root.getElementById("menu-btn")?.addEventListener("click", () =>
      this._toggleHaMenu()
    );
    root.getElementById("back-btn")?.addEventListener("click", () =>
      this._goBack()
    );
    root.getElementById("refresh-btn")?.addEventListener("click", () =>
      this._refresh()
    );

    root.querySelectorAll(".room-card").forEach((card) => {
      card.addEventListener("click", () => {
        this._selectRoom(card.dataset.entryId);
      });
    });

    root.querySelectorAll(".orphan-delete").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._deleteOrphan(btn.dataset.entryId);
      });
    });
  }

  // ─── Overview ──────────────────────────────────────────────
  _renderOverview(container) {
    if (this._rooms.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <ha-icon icon="mdi:home-thermometer-outline"></ha-icon>
          <h2>No rooms configured</h2>
          <p>Add rooms via Settings → Devices &amp; Services → Predictive Heating</p>
        </div>
      `;
      this._renderOrphans(container);
      return;
    }

    // Group rooms by zone_id
    const zones = new Map();
    for (const room of this._rooms) {
      const key = room.zone_id || `__solo_${room.entry_id}`;
      if (!zones.has(key)) zones.set(key, []);
      zones.get(key).push(room);
    }

    for (const [zoneId, roomsInZone] of zones) {
      const isMultiRoom = roomsInZone.length > 1;
      const anyHeating = roomsInZone.some((r) => r.zone_is_heating);
      const leader =
        roomsInZone.find((r) => r.zone_leader_room)?.zone_leader_room || null;

      const zoneEl = document.createElement("section");
      zoneEl.className = `zone-group ${isMultiRoom ? "multi" : "solo"}`;

      if (isMultiRoom) {
        zoneEl.innerHTML = `
          <header class="zone-header">
            <ha-icon icon="mdi:link-variant" class="zone-icon"></ha-icon>
            <span class="zone-label">Heating zone</span>
            <span class="zone-thermostat">${zoneId.replace(/^climate\./, "")}</span>
            <span class="zone-state ${anyHeating ? "heating" : "idle"}">
              <ha-icon icon="${anyHeating ? "mdi:fire" : "mdi:power-sleep"}"></ha-icon>
              ${anyHeating ? "Heating" : "Idle"}
            </span>
          </header>
        `;
      }

      const grid = document.createElement("div");
      grid.className = "room-grid";
      for (const room of roomsInZone) {
        grid.appendChild(this._renderRoomCard(room, isMultiRoom));
      }
      zoneEl.appendChild(grid);
      container.appendChild(zoneEl);
    }

    this._renderOrphans(container);
  }

  _renderRoomCard(room, isMultiRoom) {
    const calibrated = room.model_state === "calibrated";
    const stateIcon = calibrated ? "mdi:check-circle" : "mdi:progress-clock";
    const stateColor = calibrated
      ? "var(--success-color, var(--label-badge-green, #4caf50))"
      : "var(--warning-color, var(--label-badge-yellow, #ff9800))";
    const progressPct = Math.min(100, room.learning_progress || 0);

    const isHeating = room.zone_is_heating || room.hvac_action === "heating";
    const coHeated = room.co_heated_by_zone === true;

    const card = document.createElement("article");
    card.className = `room-card ${isHeating ? "is-heating" : ""} ${
      coHeated ? "is-co-heated" : ""
    }`;
    card.dataset.entryId = room.entry_id;

    const coHeatBanner = coHeated
      ? `<div class="co-heat-banner" title="The thermostat is firing for another room in the zone — this room receives some heat as a side effect.">
          <ha-icon icon="mdi:link-variant"></ha-icon>
          <span>Co-heated because <strong>${room.zone_leader_room}</strong> is below target</span>
        </div>`
      : "";

    card.innerHTML = `
      <header class="room-card-header">
        <span class="room-name">${room.room_name}</span>
        <span class="model-badge" style="--badge-color:${stateColor}">
          <ha-icon icon="${stateIcon}"></ha-icon>
          ${room.model_state}
        </span>
      </header>

      ${coHeatBanner}

      <div class="temp-display">
        <div class="temp-block">
          <ha-icon icon="mdi:thermometer"></ha-icon>
          <span class="temp-value">${this._fmt(room.current_temp, "°")}</span>
          <span class="temp-label">Indoor</span>
        </div>
        <div class="temp-block">
          <ha-icon icon="mdi:target"></ha-icon>
          <span class="temp-value">${this._fmt(room.target_temp, "°")}</span>
          <span class="temp-label">Target</span>
        </div>
        <div class="temp-block">
          <ha-icon icon="mdi:snowflake"></ha-icon>
          <span class="temp-value">${this._fmt(room.outdoor_temp, "°")}</span>
          <span class="temp-label">Outdoor</span>
        </div>
      </div>

      <div class="progress-section">
        <div class="progress-header">
          <span>Learning progress</span>
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

      <footer class="room-card-footer">
        <span class="hvac-state ${isHeating ? "heating" : "idle"}">
          <ha-icon icon="${
            isHeating ? "mdi:fire" : "mdi:power-sleep"
          }"></ha-icon>
          ${isHeating ? "Heating" : "Idle"}
        </span>
        <span class="heat-loss" title="Heat loss coefficient">
          H = ${room.heat_loss_coeff.toFixed(1)} W/K
        </span>
      </footer>
    `;
    return card;
  }

  // ─── Orphan section ───────────────────────────────────────
  _renderOrphans(container) {
    if (!this._orphans || this._orphans.length === 0) return;

    const section = document.createElement("section");
    section.className = "orphan-section";
    section.innerHTML = `
      <header>
        <ha-icon icon="mdi:broom"></ha-icon>
        <h2>Stored thermal models without a room</h2>
      </header>
      <p class="hint">
        These are leftover model files from rooms that no longer exist. They are
        kept so re-adding a room could restore its learning. Delete any you
        don't need.
      </p>
      <div class="orphan-list">
        ${this._orphans
          .map((o) => {
            const date = new Date(o.modified * 1000);
            const dateStr = date.toLocaleString();
            return `
              <div class="orphan-item">
                <div>
                  <div class="orphan-name">${o.room_name}</div>
                  <div class="orphan-meta">
                    Last updated: ${dateStr} · ${(o.size_bytes / 1024).toFixed(1)} KB
                  </div>
                </div>
                <button class="orphan-delete" data-entry-id="${o.entry_id}">
                  <ha-icon icon="mdi:delete-outline"></ha-icon>
                  Delete
                </button>
              </div>
            `;
          })
          .join("")}
      </div>
    `;
    container.appendChild(section);
  }

  // ─── Detail view ──────────────────────────────────────────
  _renderRoomDetail(container) {
    const d = this._roomDetail;
    if (!d) return;

    const detail = document.createElement("div");
    detail.className = "detail-view";

    // ── Stats row ──
    const heatingNow =
      d.hvac_action === "heating" || d.zone_is_heating === true;
    detail.innerHTML = `
      <div class="stats-row">
        ${this._statCard("mdi:thermometer", this._fmt(d.current_temp, "°C"), "Indoor")}
        ${this._statCard("mdi:target", this._fmt(d.target_temp, "°C"), "Target")}
        ${this._statCard("mdi:snowflake", this._fmt(d.outdoor_temp, "°C"), "Outdoor")}
        ${this._statCard(
          heatingNow ? "mdi:fire" : "mdi:power-sleep",
          d.hvac_action || "idle",
          "Action"
        )}
      </div>
    `;

    // ── Zone status (only shown if multi-room) ──
    if (d.zone_rooms && d.zone_rooms.length > 1) {
      const others = d.zone_rooms.filter((n) => n !== d.room_name);
      const coHeat = d.co_heated_by_zone === true;
      detail.innerHTML += `
        <div class="section zone-card ${coHeat ? "co-heated" : ""}">
          <h2>
            <ha-icon icon="mdi:link-variant"></ha-icon>
            Heating zone
          </h2>
          <div class="zone-detail">
            <div>
              <div class="kv">
                <span>Thermostat</span>
                <strong>${(d.zone_id || "").replace(/^climate\./, "")}</strong>
              </div>
              <div class="kv">
                <span>Rooms in zone</span>
                <strong>${d.zone_rooms.join(", ")}</strong>
              </div>
              <div class="kv">
                <span>Currently leading</span>
                <strong>${d.zone_leader_room || "—"}</strong>
              </div>
              <div class="kv">
                <span>Zone setpoint</span>
                <strong>${
                  d.zone_setpoint != null ? d.zone_setpoint.toFixed(1) + " °C" : "—"
                }</strong>
              </div>
            </div>
            ${
              coHeat
                ? `<div class="co-heat-callout">
                    <ha-icon icon="mdi:information-outline"></ha-icon>
                    This room is being heated because <strong>${d.zone_leader_room}</strong>
                    needs heat — they share the same thermostat circuit. The boiler
                    is firing for the leader room and your radiators get warm as a
                    side effect.
                  </div>`
                : others.length
                ? `<div class="co-heat-callout neutral">
                    <ha-icon icon="mdi:information-outline"></ha-icon>
                    Sharing this thermostat with: ${others.join(", ")}.
                    When any of them needs heat, they all see &quot;heating&quot;.
                  </div>`
                : ""
            }
          </div>
        </div>
      `;
    }

    // ── Thermal Model Parameters ──
    detail.innerHTML += `
      <div class="section">
        <h2><ha-icon icon="mdi:tune-vertical"></ha-icon>Thermal model parameters</h2>
        <div class="params-grid">
          ${this._paramCard(
            "Heat loss coefficient (H)",
            d.params.heat_loss_coeff.toFixed(1),
            "W/K",
            "How fast the room loses heat per °C indoor/outdoor difference"
          )}
          ${this._paramCard(
            "Thermal mass (C)",
            d.params.thermal_mass.toFixed(0),
            "kJ/K",
            "Stored energy capacity — higher means slower to heat or cool"
          )}
          ${this._paramCard(
            "Heating power",
            d.params.heating_power.toFixed(0),
            "W",
            "Effective heat delivered to this room when the system is firing"
          )}
          ${this._paramCard(
            "Solar gain factor",
            d.params.solar_gain_factor.toFixed(2),
            "",
            "Fraction of incoming solar irradiance that warms the room"
          )}
        </div>
        <div class="badge-row">
          <span class="info-badge">
            <ha-icon icon="${d.uses_ekf ? "mdi:flash" : "mdi:chart-line"}"></ha-icon>
            ${d.uses_ekf ? "Extended Kalman Filter (v0.2)" : "Simple estimator (v0.1)"}
          </span>
          ${
            d.mean_prediction_error != null
              ? `<span class="info-badge ${
                  d.mean_prediction_error < 0.5 ? "ok" : ""
                }">
                  <ha-icon icon="mdi:bullseye-arrow"></ha-icon>
                  Prediction accuracy ±${d.mean_prediction_error.toFixed(3)} °C
                  ${
                    d.mean_prediction_error < 0.5
                      ? ""
                      : "(target: &lt; 0.5 °C)"
                  }
                </span>`
              : ""
          }
        </div>
      </div>
    `;

    // ── Solar diagnostics ──
    if (d.solar_calc) {
      const sc = d.solar_calc;
      const absorbed = (sc.ghi_w_m2 * d.params.solar_gain_factor).toFixed(0);
      detail.innerHTML += `
        <div class="section">
          <h2><ha-icon icon="mdi:weather-sunny"></ha-icon>Solar irradiance</h2>
          <p class="hint">
            Estimated using a clear-sky model from sun elevation, then reduced
            by cloud cover from a weather entity.
          </p>
          <div class="solar-grid">
            <div class="solar-block">
              <div class="solar-label">Sun elevation</div>
              <div class="solar-value">${
                sc.sun_elevation_deg != null ? sc.sun_elevation_deg.toFixed(1) + "°" : "—"
              }</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Sun azimuth</div>
              <div class="solar-value">${
                sc.sun_azimuth_deg != null ? sc.sun_azimuth_deg.toFixed(1) + "°" : "—"
              }</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Weather entity</div>
              <div class="solar-value mono">${sc.weather_entity || "—"}</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Condition</div>
              <div class="solar-value">${sc.weather_condition || "—"}</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Cloud cover</div>
              <div class="solar-value">${
                sc.cloud_coverage_pct != null
                  ? sc.cloud_coverage_pct.toFixed(0) + " %"
                  : "—"
              }</div>
              <div class="solar-sublabel">from ${sc.cloud_source}</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Clear-sky GHI</div>
              <div class="solar-value">${sc.ghi_clear_sky_w_m2.toFixed(0)} W/m²</div>
              <div class="solar-sublabel">Haurwitz model · cos(elevation)</div>
            </div>
            <div class="solar-block highlight">
              <div class="solar-label">Adjusted GHI</div>
              <div class="solar-value">${sc.ghi_w_m2.toFixed(0)} W/m²</div>
              <div class="solar-sublabel">× (1 − 0.75·cloud<sup>3.4</sup>) = factor ${sc.cloud_factor}</div>
            </div>
            <div class="solar-block highlight">
              <div class="solar-label">Heat absorbed by room</div>
              <div class="solar-value">~${absorbed} W</div>
              <div class="solar-sublabel">GHI × gain factor (${d.params.solar_gain_factor.toFixed(2)})</div>
            </div>
          </div>
        </div>
      `;
    }

    // ── Training Progress ──
    const progressPct = Math.min(100, d.learning_progress || 0);
    const idlePct = Math.min(100, (d.idle_samples / d.min_idle) * 100);
    const activePct = Math.min(100, (d.active_samples / d.min_active) * 100);
    const stateColor =
      d.model_state === "calibrated"
        ? "var(--success-color, #4caf50)"
        : "var(--warning-color, #ff9800)";

    detail.innerHTML += `
      <div class="section">
        <h2><ha-icon icon="mdi:school"></ha-icon>Training progress</h2>
        <div class="training-detail">
          <div class="training-overall">
            <div class="circle-progress" style="--progress: ${progressPct}">
              <svg viewBox="0 0 100 100">
                <circle class="bg" cx="50" cy="50" r="42"/>
                <circle class="fg" cx="50" cy="50" r="42"
                  style="stroke-dasharray: ${progressPct * 2.64} 264; stroke: ${stateColor}"/>
                <text x="50" y="50" text-anchor="middle" dominant-baseline="central" class="circle-text">${progressPct}%</text>
              </svg>
            </div>
            <div class="training-status">
              <span class="model-badge" style="--badge-color: ${stateColor}">
                <ha-icon icon="${
                  d.model_state === "calibrated"
                    ? "mdi:check-circle"
                    : "mdi:progress-clock"
                }"></ha-icon>
                ${d.model_state === "calibrated" ? "Calibrated" : "Learning"}
              </span>
              <p>${
                d.model_state === "calibrated"
                  ? "The thermal model is calibrated and predictions are active."
                  : "The model is still learning. Hysteresis control is used as fallback."
              }</p>
            </div>
          </div>

          <div class="sample-bars">
            <div class="sample-bar-group">
              <div class="sample-bar-header">
                <span>Idle observations</span>
                <span>${d.idle_samples} / ${d.min_idle}</span>
              </div>
              <div class="progress-bar large">
                <div class="progress-fill" style="width:${idlePct}%;background:var(--info-color, var(--primary-color))"></div>
              </div>
              <div class="sample-bar-desc">Collected when heating is off — used to learn heat loss rate.</div>
            </div>
            <div class="sample-bar-group">
              <div class="sample-bar-header">
                <span>Active observations</span>
                <span>${d.active_samples} / ${d.min_active}</span>
              </div>
              <div class="progress-bar large">
                <div class="progress-fill" style="width:${activePct}%;background:var(--accent-color, #ff9800)"></div>
              </div>
              <div class="sample-bar-desc">Collected during heating — used to learn heating power.</div>
            </div>
          </div>
        </div>
      </div>
    `;

    // ── Charts ──
    detail.innerHTML += `
      <div class="section">
        <h2><ha-icon icon="mdi:chart-line"></ha-icon>Temperature history</h2>
        <div class="chart-container">
          <canvas id="temp-chart" width="800" height="300"></canvas>
        </div>
        <div class="chart-legend">
          <span class="legend-item"><span class="legend-dot" style="background:var(--primary-color)"></span>Indoor</span>
          <span class="legend-item"><span class="legend-dot" style="background:var(--info-color, #03a9f4)"></span>Outdoor</span>
          <span class="legend-item"><span class="legend-dot" style="background:var(--accent-color, #ff9800)"></span>Target</span>
          <span class="legend-item"><span class="legend-shade" style="background:var(--error-color, #f44336);opacity:0.2"></span>Heating</span>
        </div>
      </div>

      <div class="section">
        <h2><ha-icon icon="mdi:trending-up"></ha-icon>Heat loss learning history</h2>
        <div class="chart-container">
          <canvas id="learning-chart" width="800" height="200"></canvas>
        </div>
        <p class="hint">Evolution of the estimated heat loss coefficient (H) as more observations arrive.</p>
      </div>
    `;

    if (d.prediction_error_history && d.prediction_error_history.length > 1) {
      detail.innerHTML += `
        <div class="section">
          <h2><ha-icon icon="mdi:bullseye-arrow"></ha-icon>Prediction accuracy over time</h2>
          <div class="chart-container">
            <canvas id="error-chart" width="800" height="200"></canvas>
          </div>
          <div class="chart-legend">
            <span class="legend-item"><span class="legend-dot" style="background:var(--error-color, #f44336)"></span>Mean abs. error</span>
            <span class="legend-item"><span class="legend-dot" style="background:var(--success-color, #4caf50)"></span>Calibration threshold (0.5 °C)</span>
          </div>
          <p class="hint">When mean error drops below 0.5 °C, the model auto-calibrates and predictive control engages.</p>
        </div>
      `;
    }

    // ── Predictions ──
    if (d.model_state === "calibrated" && d.predictions) {
      detail.innerHTML += `
        <div class="section">
          <h2><ha-icon icon="mdi:crystal-ball"></ha-icon>Predictions</h2>
          <div class="predictions-grid">
            <div class="prediction-card">
              <div class="prediction-label">Temp in 1 h (heating off)</div>
              <div class="prediction-value">${this._fmt(d.predictions.temp_1h_off, "°C")}</div>
            </div>
            <div class="prediction-card">
              <div class="prediction-label">Temp in 1 h (heating on)</div>
              <div class="prediction-value">${this._fmt(d.predictions.temp_1h_on, "°C")}</div>
            </div>
            <div class="prediction-card">
              <div class="prediction-label">Time to reach target</div>
              <div class="prediction-value">${
                d.predictions.time_to_target != null
                  ? d.predictions.time_to_target.toFixed(0) + " min"
                  : "N/A"
              }</div>
            </div>
          </div>
        </div>
      `;
    }

    container.appendChild(detail);

    requestAnimationFrame(() => {
      this._drawTempChart(d);
      this._drawLearningChart(d);
      this._drawErrorChart(d);
    });
  }

  // ─── Small renderers ──────────────────────────────────────
  _statCard(icon, value, label) {
    return `
      <div class="stat-card">
        <ha-icon icon="${icon}"></ha-icon>
        <div class="stat-value">${value}</div>
        <div class="stat-label">${label}</div>
      </div>
    `;
  }

  _paramCard(name, value, unit, desc) {
    return `
      <div class="param-card">
        <div class="param-name">${name}</div>
        <div class="param-value">${value}<span class="param-unit"> ${unit}</span></div>
        <div class="param-desc">${desc}</div>
      </div>
    `;
  }

  _fmt(v, suffix = "") {
    return v == null ? "—" : v.toFixed(1) + suffix;
  }

  // ─── Canvas charts ────────────────────────────────────────
  _themeColor(name, fallback) {
    try {
      const v = getComputedStyle(this).getPropertyValue(name);
      return v.trim() || fallback;
    } catch (e) {
      return fallback;
    }
  }

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

    let yMin = Infinity,
      yMax = -Infinity;
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

    const txtCol = this._themeColor("--secondary-text-color", "#888");
    const gridCol = this._themeColor("--divider-color", "rgba(0,0,0,0.08)");
    const indoor = this._themeColor("--primary-color", "#03a9f4");
    const outdoor = this._themeColor("--info-color", "#03a9f4");
    const target = this._themeColor("--accent-color", "#ff9800");
    const heatCol = this._themeColor("--error-color", "#f44336");

    // Grid + Y labels
    ctx.strokeStyle = gridCol;
    ctx.lineWidth = 1;
    const ySteps = 5;
    ctx.font = "11px var(--paper-font-body1_-_font-family, sans-serif)";
    for (let i = 0; i <= ySteps; i++) {
      const v = yMin + (yRange * i) / ySteps;
      const y = toY(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = txtCol;
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(1) + "°", pad.left - 8, y + 4);
    }

    // X labels
    ctx.textAlign = "center";
    ctx.fillStyle = txtCol;
    const tSteps = Math.min(6, obs.length);
    for (let i = 0; i <= tSteps; i++) {
      const t = tMin + (tRange * i) / tSteps;
      const x = toX(t);
      const date = new Date(t * 1000);
      ctx.fillText(
        date.getHours().toString().padStart(2, "0") +
          ":" +
          date.getMinutes().toString().padStart(2, "0"),
        x,
        H - pad.bottom + 20
      );
    }

    // Heating shading
    ctx.fillStyle = this._withAlpha(heatCol, 0.15);
    for (let i = 0; i < obs.length - 1; i++) {
      if (obs[i].heating_on) {
        const x1 = toX(obs[i].timestamp);
        const x2 = toX(obs[i + 1].timestamp);
        ctx.fillRect(x1, pad.top, x2 - x1, plotH);
      }
    }

    // Target line
    if (data.target_temp != null) {
      ctx.strokeStyle = target;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      const ty = toY(data.target_temp);
      ctx.moveTo(pad.left, ty);
      ctx.lineTo(W - pad.right, ty);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Outdoor line
    ctx.strokeStyle = outdoor;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (let i = 0; i < obs.length; i++) {
      const x = toX(obs[i].timestamp);
      const y = toY(obs[i].t_outdoor);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Indoor line
    ctx.strokeStyle = indoor;
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
    let yMin = Infinity,
      yMax = -Infinity;
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

    const txtCol = this._themeColor("--secondary-text-color", "#888");
    const gridCol = this._themeColor("--divider-color", "rgba(0,0,0,0.08)");
    const lineCol = this._themeColor("--success-color", "#4caf50");

    ctx.strokeStyle = gridCol;
    ctx.lineWidth = 1;
    ctx.font = "11px var(--paper-font-body1_-_font-family, sans-serif)";
    for (let i = 0; i <= 4; i++) {
      const v = yMin + (yRange * i) / 4;
      const y = toY(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = txtCol;
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(1), pad.left - 8, y + 4);
    }

    ctx.save();
    ctx.translate(14, H / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = txtCol;
    ctx.textAlign = "center";
    ctx.fillText("W/K", 0, 0);
    ctx.restore();

    ctx.fillStyle = txtCol;
    ctx.textAlign = "center";
    ctx.fillText("Sample #", W / 2, H - 4);

    ctx.strokeStyle = lineCol;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < hist.length; i++) {
      const x = toX(i);
      const y = toY(hist[i].value);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();

    ctx.fillStyle = lineCol;
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
    if (
      !canvas ||
      !data.prediction_error_history ||
      data.prediction_error_history.length < 2
    )
      return;

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
    const yMax = Math.max(1.0, ...hist.map((h) => h.value)) * 1.1;
    const yMin = 0;
    const yRange = yMax - yMin;

    const toX = (i) => pad.left + (i / (hist.length - 1)) * plotW;
    const toY = (v) => pad.top + (1 - (v - yMin) / yRange) * plotH;

    const txtCol = this._themeColor("--secondary-text-color", "#888");
    const gridCol = this._themeColor("--divider-color", "rgba(0,0,0,0.08)");
    const okCol = this._themeColor("--success-color", "#4caf50");
    const errCol = this._themeColor("--error-color", "#f44336");

    ctx.strokeStyle = gridCol;
    ctx.lineWidth = 1;
    ctx.font = "11px var(--paper-font-body1_-_font-family, sans-serif)";
    for (let i = 0; i <= 4; i++) {
      const v = yMin + (yRange * i) / 4;
      const y = toY(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = txtCol;
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(2) + "°", pad.left - 8, y + 4);
    }

    // Calibration threshold
    ctx.strokeStyle = okCol;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    const threshY = toY(0.5);
    ctx.moveTo(pad.left, threshY);
    ctx.lineTo(W - pad.right, threshY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Error line
    ctx.strokeStyle = errCol;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < hist.length; i++) {
      const x = toX(i);
      const y = toY(hist[i].value);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();

    ctx.save();
    ctx.translate(14, H / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = txtCol;
    ctx.textAlign = "center";
    ctx.fillText("°C error", 0, 0);
    ctx.restore();
  }

  _withAlpha(color, alpha) {
    // Best-effort: HA theme vars sometimes return rgb()/hex; fall back to translucent red
    if (!color) return `rgba(244,67,54,${alpha})`;
    if (color.startsWith("#") && color.length === 7) {
      const r = parseInt(color.slice(1, 3), 16);
      const g = parseInt(color.slice(3, 5), 16);
      const b = parseInt(color.slice(5, 7), 16);
      return `rgba(${r},${g},${b},${alpha})`;
    }
    if (color.startsWith("rgb(")) {
      return color.replace("rgb(", "rgba(").replace(")", `,${alpha})`);
    }
    return color;
  }

  // ─── Styles ───────────────────────────────────────────────
  _getStyles() {
    return `
      :host {
        display: block;
        background: var(--primary-background-color, #fafafa);
        color: var(--primary-text-color, #212121);
        min-height: 100vh;
        font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
      }

      /* ── App bar ─────────────────────────────────────── */
      .app-bar {
        position: sticky;
        top: 0;
        z-index: 5;
        height: 56px;
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 0 8px 0 4px;
        background: var(--app-header-background-color, var(--primary-color, #03a9f4));
        color: var(--app-header-text-color, white);
        box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,0.15));
      }
      .app-bar-title {
        flex: 1;
        font-size: 20px;
        font-weight: 400;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .icon-button {
        width: 40px;
        height: 40px;
        border: none;
        background: transparent;
        color: inherit;
        border-radius: 50%;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: background-color 0.15s;
      }
      .icon-button:hover { background: rgba(255,255,255,0.12); }
      .icon-button ha-icon { --mdc-icon-size: 24px; }

      /* ── Container ───────────────────────────────────── */
      .container {
        max-width: 1200px;
        margin: 0 auto;
        padding: 16px;
      }

      /* ── Zone groups ─────────────────────────────────── */
      .zone-group { margin-bottom: 24px; }
      .zone-group.multi {
        background: var(--card-background-color, white);
        border: 1px solid var(--divider-color, rgba(0,0,0,0.08));
        border-radius: var(--ha-card-border-radius, 12px);
        padding: 12px 12px 16px;
      }
      .zone-group.solo .room-grid { padding: 0; }

      .zone-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 4px 4px 12px;
        color: var(--secondary-text-color);
        font-size: 13px;
      }
      .zone-icon {
        --mdc-icon-size: 18px;
        color: var(--primary-color);
      }
      .zone-label { font-weight: 500; }
      .zone-thermostat {
        font-family: var(--code-font-family, monospace);
        background: var(--secondary-background-color, rgba(0,0,0,0.04));
        padding: 2px 6px;
        border-radius: 4px;
        font-size: 12px;
      }
      .zone-state {
        margin-left: auto;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-weight: 500;
      }
      .zone-state ha-icon { --mdc-icon-size: 16px; }
      .zone-state.heating { color: var(--error-color, #f44336); }
      .zone-state.idle { color: var(--secondary-text-color); }

      /* ── Room grid ───────────────────────────────────── */
      .room-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
        gap: 12px;
      }

      .room-card {
        background: var(--card-background-color, white);
        border: 1px solid var(--divider-color, rgba(0,0,0,0.08));
        border-radius: var(--ha-card-border-radius, 12px);
        padding: 16px;
        cursor: pointer;
        transition: box-shadow 0.15s, transform 0.15s;
        box-shadow: var(--ha-card-box-shadow, none);
      }
      .room-card:hover {
        box-shadow: 0 4px 14px rgba(0,0,0,0.1);
        transform: translateY(-1px);
      }
      .room-card.is-heating {
        border-color: var(--error-color, #f44336);
      }
      .room-card.is-co-heated {
        border-style: dashed;
      }

      .room-card-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 12px;
      }
      .room-name { font-size: 16px; font-weight: 500; }

      .model-badge {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 11px;
        font-weight: 500;
        padding: 3px 8px;
        border-radius: 12px;
        background: color-mix(in srgb, var(--badge-color, gray) 15%, transparent);
        color: var(--badge-color, gray);
        text-transform: capitalize;
      }
      .model-badge ha-icon { --mdc-icon-size: 14px; }

      .co-heat-banner {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px 10px;
        margin-bottom: 12px;
        font-size: 12px;
        background: color-mix(in srgb, var(--primary-color) 8%, transparent);
        border-left: 3px solid var(--primary-color);
        border-radius: 4px;
        color: var(--secondary-text-color);
      }
      .co-heat-banner strong { color: var(--primary-text-color); }
      .co-heat-banner ha-icon {
        --mdc-icon-size: 16px;
        color: var(--primary-color);
      }

      .temp-display {
        display: flex;
        justify-content: space-around;
        margin-bottom: 14px;
        text-align: center;
      }
      .temp-block {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 2px;
      }
      .temp-block ha-icon {
        --mdc-icon-size: 18px;
        color: var(--secondary-text-color);
      }
      .temp-value { font-size: 20px; font-weight: 500; }
      .temp-label {
        font-size: 11px;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }

      .progress-section { margin-bottom: 12px; }
      .progress-header {
        display: flex;
        justify-content: space-between;
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-bottom: 4px;
      }
      .progress-pct { font-weight: 500; color: var(--primary-text-color); }

      .progress-bar {
        height: 4px;
        background: var(--divider-color, rgba(0,0,0,0.08));
        border-radius: 2px;
        overflow: hidden;
      }
      .progress-bar.large { height: 6px; border-radius: 3px; }
      .progress-fill {
        height: 100%;
        border-radius: inherit;
        transition: width 0.6s ease;
      }
      .progress-detail {
        display: flex;
        justify-content: space-between;
        font-size: 11px;
        color: var(--secondary-text-color);
        margin-top: 2px;
      }

      .room-card-footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 12px;
        color: var(--secondary-text-color);
        padding-top: 10px;
        border-top: 1px solid var(--divider-color, rgba(0,0,0,0.08));
      }
      .hvac-state {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .hvac-state ha-icon { --mdc-icon-size: 14px; }
      .hvac-state.heating { color: var(--error-color, #f44336); }

      /* ── Detail view ─────────────────────────────────── */
      .stats-row {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 12px;
        margin-bottom: 16px;
      }
      .stat-card {
        background: var(--card-background-color, white);
        border: 1px solid var(--divider-color, rgba(0,0,0,0.08));
        border-radius: var(--ha-card-border-radius, 12px);
        padding: 14px;
        text-align: center;
      }
      .stat-card ha-icon {
        --mdc-icon-size: 22px;
        color: var(--secondary-text-color);
        margin-bottom: 4px;
      }
      .stat-value { font-size: 20px; font-weight: 500; }
      .stat-label {
        font-size: 11px;
        color: var(--secondary-text-color);
        margin-top: 2px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }

      .section {
        background: var(--card-background-color, white);
        border: 1px solid var(--divider-color, rgba(0,0,0,0.08));
        border-radius: var(--ha-card-border-radius, 12px);
        padding: 20px;
        margin-bottom: 16px;
      }
      .section h2 {
        margin: 0 0 16px;
        font-size: 16px;
        font-weight: 500;
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .section h2 ha-icon {
        --mdc-icon-size: 20px;
        color: var(--primary-color);
      }
      .hint {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin: -8px 0 12px;
      }

      /* Zone-detail card */
      .zone-card.co-heated { border-left: 4px solid var(--primary-color); }
      .kv {
        display: flex;
        justify-content: space-between;
        padding: 6px 0;
        border-bottom: 1px solid var(--divider-color, rgba(0,0,0,0.06));
        font-size: 13px;
      }
      .kv:last-child { border-bottom: none; }
      .kv span { color: var(--secondary-text-color); }
      .co-heat-callout {
        margin-top: 12px;
        padding: 10px 12px;
        background: color-mix(in srgb, var(--primary-color) 8%, transparent);
        border-left: 3px solid var(--primary-color);
        border-radius: 4px;
        font-size: 13px;
        line-height: 1.5;
        display: flex;
        gap: 8px;
        align-items: flex-start;
      }
      .co-heat-callout.neutral {
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-left-color: var(--secondary-text-color);
      }
      .co-heat-callout ha-icon {
        --mdc-icon-size: 18px;
        color: var(--primary-color);
        flex-shrink: 0;
      }

      /* Params */
      .params-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 12px;
      }
      .param-card {
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 8px;
        padding: 14px;
      }
      .param-name {
        font-size: 11px;
        color: var(--secondary-text-color);
        margin-bottom: 4px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .param-value { font-size: 22px; font-weight: 500; }
      .param-unit {
        font-size: 13px;
        font-weight: 400;
        color: var(--secondary-text-color);
      }
      .param-desc {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-top: 6px;
        line-height: 1.4;
      }

      .badge-row {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 14px;
      }
      .info-badge {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 12px;
        padding: 4px 10px;
        border-radius: 12px;
        background: var(--secondary-background-color, rgba(0,0,0,0.04));
        color: var(--secondary-text-color);
      }
      .info-badge ha-icon { --mdc-icon-size: 14px; }
      .info-badge.ok {
        color: var(--success-color, #4caf50);
        background: color-mix(in srgb, var(--success-color, #4caf50) 12%, transparent);
      }

      /* Solar grid */
      .solar-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        gap: 10px;
      }
      .solar-block {
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 8px;
        padding: 12px;
      }
      .solar-block.highlight {
        background: color-mix(in srgb, var(--primary-color) 8%, transparent);
        border-left: 3px solid var(--primary-color);
      }
      .solar-label {
        font-size: 11px;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .solar-value {
        font-size: 18px;
        font-weight: 500;
        margin-top: 4px;
      }
      .solar-value.mono { font-family: var(--code-font-family, monospace); font-size: 13px; }
      .solar-sublabel {
        font-size: 11px;
        color: var(--secondary-text-color);
        margin-top: 4px;
      }

      /* Training */
      .training-overall {
        display: flex;
        align-items: center;
        gap: 24px;
        margin-bottom: 20px;
      }
      .circle-progress { width: 96px; height: 96px; flex-shrink: 0; }
      .circle-progress svg { width: 100%; height: 100%; transform: rotate(-90deg); }
      .circle-progress .bg { fill: none; stroke: var(--divider-color, rgba(0,0,0,0.08)); stroke-width: 8; }
      .circle-progress .fg { fill: none; stroke-width: 8; stroke-linecap: round; transition: stroke-dasharray 0.6s; }
      .circle-progress .circle-text {
        font-size: 18px;
        font-weight: 500;
        fill: var(--primary-text-color);
        transform: rotate(90deg);
        transform-origin: 50% 50%;
      }
      .training-status p {
        color: var(--secondary-text-color);
        font-size: 13px;
        margin: 6px 0 0;
        line-height: 1.5;
      }

      .sample-bars { display: flex; flex-direction: column; gap: 14px; }
      .sample-bar-header {
        display: flex;
        justify-content: space-between;
        font-size: 13px;
        margin-bottom: 4px;
      }
      .sample-bar-desc {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-top: 4px;
      }

      /* Charts */
      .chart-container { width: 100%; overflow: hidden; border-radius: 6px; }
      .chart-container canvas { display: block; width: 100%; }
      .chart-legend {
        display: flex;
        gap: 16px;
        margin-top: 10px;
        font-size: 12px;
        color: var(--secondary-text-color);
        flex-wrap: wrap;
      }
      .legend-item { display: flex; align-items: center; gap: 6px; }
      .legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
      .legend-shade { width: 20px; height: 10px; border-radius: 3px; display: inline-block; }

      /* Predictions */
      .predictions-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 12px;
      }
      .prediction-card {
        background: color-mix(in srgb, var(--primary-color) 8%, transparent);
        border-radius: 8px;
        padding: 14px;
        text-align: center;
      }
      .prediction-label {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-bottom: 6px;
      }
      .prediction-value {
        font-size: 20px;
        font-weight: 500;
        color: var(--primary-color);
      }

      /* Empty state */
      .empty-state {
        text-align: center;
        padding: 64px 16px;
        color: var(--secondary-text-color);
      }
      .empty-state ha-icon {
        --mdc-icon-size: 64px;
        color: var(--secondary-text-color);
      }
      .empty-state h2 { margin: 12px 0 4px; font-size: 18px; }

      /* Orphan section */
      .orphan-section {
        background: var(--card-background-color, white);
        border: 1px solid var(--divider-color, rgba(0,0,0,0.08));
        border-radius: var(--ha-card-border-radius, 12px);
        padding: 16px 20px;
        margin-top: 24px;
      }
      .orphan-section header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 4px;
      }
      .orphan-section header ha-icon {
        --mdc-icon-size: 20px;
        color: var(--secondary-text-color);
      }
      .orphan-section h2 {
        margin: 0;
        font-size: 15px;
        font-weight: 500;
      }
      .orphan-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-top: 12px;
      }
      .orphan-item {
        display: flex;
        justify-content: space-between;
        align-items: center;
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 8px;
        padding: 10px 12px;
        font-size: 13px;
      }
      .orphan-name { font-weight: 500; }
      .orphan-meta {
        font-size: 11px;
        color: var(--secondary-text-color);
        margin-top: 2px;
      }
      .orphan-delete {
        background: transparent;
        border: 1px solid var(--error-color, #f44336);
        color: var(--error-color, #f44336);
        border-radius: 16px;
        padding: 4px 12px;
        font-size: 12px;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        transition: background-color 0.15s;
      }
      .orphan-delete ha-icon { --mdc-icon-size: 16px; }
      .orphan-delete:hover {
        background: color-mix(in srgb, var(--error-color, #f44336) 12%, transparent);
      }

      /* Responsive */
      @media (max-width: 600px) {
        .container { padding: 12px; }
        .room-grid { grid-template-columns: 1fr; }
        .stats-row { grid-template-columns: repeat(2, 1fr); }
        .params-grid { grid-template-columns: 1fr; }
        .training-overall { flex-direction: column; text-align: center; }
        .solar-grid { grid-template-columns: 1fr 1fr; }
      }
    `;
  }
}

customElements.define("predictive-heating-panel", PredictiveHeatingPanel);
