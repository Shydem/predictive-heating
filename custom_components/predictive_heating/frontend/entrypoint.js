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
    // Detail view tabs. Persisted per-entry so refreshes don't bounce
    // the user back to Overview.
    this._activeTab = "overview";
    this._tabByEntry = {};
    // Transient status messages for advanced actions (recompute / simulate).
    this._actionStatus = null;
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
      this._roomDetailError = null;
    } catch (e) {
      console.error("Failed to load room detail:", e);
      // Keep the user in detail mode but show an error — otherwise clicks
      // on the card silently do nothing, which looks broken.
      const overview = (this._rooms || []).find((r) => r.entry_id === entryId);
      this._selectedRoom = entryId;
      this._roomDetail = {
        entry_id: entryId,
        room_name: (overview && overview.room_name) || "Room",
        _error:
          (e && (e.message || e.error || e.code)) || "Unknown error",
      };
      this._roomDetailError = e;
    }
    this._render();
  }

  // ── Control actions ───────────────────────────────────────
  async _setTemperature(entryId, temperature) {
    try {
      await this._hass.callWS({
        type: "predictive_heating/set_temperature",
        entry_id: entryId,
        temperature,
      });
      await this._refresh();
    } catch (e) {
      console.error("Failed to set temperature:", e);
      alert("Could not set temperature: " + (e.message || e));
    }
  }

  async _adjustTemperature(entryId, delta) {
    const room =
      (this._selectedRoom && this._roomDetail && this._roomDetail.entry_id === entryId)
        ? this._roomDetail
        : this._rooms.find((r) => r.entry_id === entryId);
    if (!room) return;
    const current = Number(room.target_temp ?? 20);
    const next = Math.round((current + delta) * 2) / 2; // snap to 0.5
    await this._setTemperature(entryId, next);
  }

  async _setPreset(entryId, preset) {
    try {
      await this._hass.callWS({
        type: "predictive_heating/set_preset",
        entry_id: entryId,
        preset_mode: preset,
      });
      await this._refresh();
    } catch (e) {
      console.error("Failed to set preset:", e);
      alert("Could not set preset: " + (e.message || e));
    }
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
    // Restore the last tab the user was on for this room.
    this._activeTab = this._tabByEntry[entryId] || "overview";
    this._loadRoomDetail(entryId);
  }

  _goBack() {
    this._selectedRoom = null;
    this._roomDetail = null;
    this._roomDetailError = null;
    this._actionStatus = null;
    this._render();
  }

  _setTab(tab) {
    this._activeTab = tab;
    if (this._selectedRoom) {
      this._tabByEntry[this._selectedRoom] = tab;
    }
    this._actionStatus = null;
    this._render();
  }

  // ── Advanced actions (tab-driven) ─────────────────────────
  async _doRecompute(entryId) {
    this._actionStatus = { kind: "info", text: "Recomputing…" };
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "predictive_heating/recompute",
        entry_id: entryId,
      });
      const p = (result && result.params) || {};
      const bits = [];
      if (p.heat_loss_coeff != null) bits.push(`H=${p.heat_loss_coeff} W/K`);
      if (p.heating_power != null) bits.push(`P=${p.heating_power} W`);
      if (p.thermal_mass != null) bits.push(`C=${p.thermal_mass} kJ/K`);
      this._actionStatus = {
        kind: "ok",
        text: bits.length
          ? `Recomputed — ${bits.join(" · ")}`
          : "Recomputed thermal parameters.",
      };
      await this._refresh();
    } catch (e) {
      console.error("Recompute failed:", e);
      this._actionStatus = {
        kind: "error",
        text: "Recompute failed: " + (e.message || e.error || e),
      };
      this._render();
    }
  }

  async _doSimulate(entryId) {
    this._actionStatus = { kind: "info", text: "Running 24 h simulation…" };
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "predictive_heating/simulate",
        entry_id: entryId,
      });
      const steps = (result && result.steps) || 0;
      this._actionStatus = {
        kind: "ok",
        text: `Simulated ${steps} steps — chart updated below.`,
      };
      await this._refresh();
    } catch (e) {
      console.error("Simulate failed:", e);
      this._actionStatus = {
        kind: "error",
        text: "Simulation failed: " + (e.message || e.error || e),
      };
      this._render();
    }
  }

  async _setOverride(entryId, on) {
    try {
      await this._hass.callWS({
        type: "predictive_heating/set_override",
        entry_id: entryId,
        on: !!on,
      });
      await this._refresh();
    } catch (e) {
      console.error("Override toggle failed:", e);
      alert("Could not toggle override: " + (e.message || e));
    }
  }

  async _setCouplingEnabled(entryId, neighbourEntryId, enabled) {
    try {
      await this._hass.callWS({
        type: "predictive_heating/set_coupling_enabled",
        entry_id: entryId,
        neighbour_entry_id: neighbourEntryId,
        enabled: !!enabled,
      });
      await this._refresh();
    } catch (e) {
      console.error("Coupling toggle failed:", e);
      alert("Could not toggle coupling: " + (e.message || e));
    }
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
      card.addEventListener("click", (ev) => {
        // Don't navigate when clicking inline controls.
        if (ev.target.closest('[data-action="stop"]')) return;
        if (ev.target.closest(".step-btn")) return;
        if (ev.target.closest(".preset-chip")) return;
        this._selectRoom(card.dataset.entryId);
      });
    });

    // Inline step buttons (+/-) on cards and in the detail view.
    root.querySelectorAll('[data-action="temp-up"]').forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._adjustTemperature(btn.dataset.entryId, 0.5);
      });
    });
    root.querySelectorAll('[data-action="temp-down"]').forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._adjustTemperature(btn.dataset.entryId, -0.5);
      });
    });

    // Preset chips.
    root.querySelectorAll('[data-action="preset"]').forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._setPreset(btn.dataset.entryId, btn.dataset.preset);
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
    const state = room.model_state || "unknown";
    const calibrated = state === "calibrated";
    const stateIcon = calibrated ? "mdi:check-circle" : "mdi:progress-clock";
    const stateColor = calibrated
      ? "var(--success-color, var(--label-badge-green, #4caf50))"
      : "var(--warning-color, var(--label-badge-yellow, #ff9800))";
    const progressPct = Math.min(100, this._num(room.learning_progress, 0));

    const isHeating = room.zone_is_heating || room.hvac_action === "heating";
    const coHeated = room.co_heated_by_zone === true;
    const windowOpen = room.window_open === true;
    const hasSchedule = !!room.schedule_entity;
    const scheduleOn =
      room.schedule_state === "on" || room.schedule_state === true;

    const card = document.createElement("article");
    card.className = `room-card ${isHeating ? "is-heating" : ""} ${
      coHeated ? "is-co-heated" : ""
    } ${windowOpen ? "is-window-open" : ""}`;
    card.dataset.entryId = room.entry_id;

    const coHeatBanner = coHeated
      ? `<div class="co-heat-banner" title="The thermostat is firing for another room in the zone — this room receives some heat as a side effect.">
          <ha-icon icon="mdi:link-variant"></ha-icon>
          <span>Co-heated because <strong>${this._escape(room.zone_leader_room || "")}</strong> is below target</span>
        </div>`
      : "";

    const windowBanner = windowOpen
      ? `<div class="window-banner" title="Heating is paused because a window/door sensor reports open.">
          <ha-icon icon="mdi:window-open-variant"></ha-icon>
          <span>Window/door open — heating paused</span>
        </div>`
      : "";

    const preset = room.preset_mode || null;
    const presetChips = ["comfort", "eco", "away", "sleep"]
      .map((p) => {
        const active = preset === p;
        return `<button class="preset-chip ${active ? "active" : ""}"
                       data-action="preset"
                       data-entry-id="${room.entry_id}"
                       data-preset="${p}"
                       title="Set preset: ${p}">${p}</button>`;
      })
      .join("");

    const heatPowerW = this._num(room.heat_power_w, null);
    const heatPowerStr =
      heatPowerW != null && !Number.isNaN(heatPowerW)
        ? `${Math.round(heatPowerW)} W`
        : null;

    const statusChips = [];
    if (hasSchedule) {
      statusChips.push(
        `<span class="chip schedule ${scheduleOn ? "on" : "off"}" title="Following ${this._escape(
          room.schedule_entity
        )}">
          <ha-icon icon="mdi:calendar-clock"></ha-icon>
          Schedule ${scheduleOn ? "ON" : "OFF"}
        </span>`
      );
    }
    if (heatPowerStr) {
      statusChips.push(
        `<span class="chip" title="Heat delivered right now (from gas-meter derivative × efficiency × heat share)">
          <ha-icon icon="mdi:fire"></ha-icon>
          ${heatPowerStr}
        </span>`
      );
    }
    if (room.climate_entity_id) {
      statusChips.push(
        `<span class="chip mono" title="The thermostat this room controls">
          <ha-icon icon="mdi:thermostat"></ha-icon>
          ${this._escape(String(room.climate_entity_id).replace(/^climate\./, ""))}
        </span>`
      );
    }

    card.innerHTML = `
      <header class="room-card-header">
        <span class="room-name">${this._escape(room.room_name || "Room")}</span>
        <span class="model-badge" style="--badge-color:${stateColor}">
          <ha-icon icon="${stateIcon}"></ha-icon>
          ${this._escape(state)}
        </span>
      </header>

      ${coHeatBanner}
      ${windowBanner}

      <div class="temp-display">
        <div class="temp-block">
          <ha-icon icon="mdi:thermometer"></ha-icon>
          <span class="temp-value">${this._fmt(room.current_temp, "°")}</span>
          <span class="temp-label">Indoor</span>
        </div>
        <div class="temp-block target-block">
          <ha-icon icon="mdi:target"></ha-icon>
          <div class="target-controls" data-action="stop">
            <button class="step-btn"
                    data-action="temp-down"
                    data-entry-id="${room.entry_id}"
                    title="Decrease target by 0.5 °C">−</button>
            <span class="temp-value">${this._fmt(room.target_temp, "°")}</span>
            <button class="step-btn"
                    data-action="temp-up"
                    data-entry-id="${room.entry_id}"
                    title="Increase target by 0.5 °C">+</button>
          </div>
          <span class="temp-label">Target</span>
        </div>
        <div class="temp-block">
          <ha-icon icon="mdi:snowflake"></ha-icon>
          <span class="temp-value">${this._fmt(room.outdoor_temp, "°")}</span>
          <span class="temp-label">Outdoor</span>
        </div>
      </div>

      <div class="preset-row" data-action="stop">${presetChips}</div>

      <div class="progress-section">
        <div class="progress-header">
          <span>Learning progress</span>
          <span class="progress-pct">${progressPct}%</span>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" style="width:${progressPct}%;background:${stateColor}"></div>
        </div>
        <div class="progress-detail">
          <span>Idle: ${this._num(room.idle_samples, 0)}/${this._num(room.min_idle, 0)}</span>
          <span>Active: ${this._num(room.active_samples, 0)}/${this._num(room.min_active, 0)}</span>
        </div>
      </div>

      ${
        statusChips.length
          ? `<div class="status-chips">${statusChips.join("")}</div>`
          : ""
      }

      <footer class="room-card-footer">
        <span class="hvac-state ${isHeating ? "heating" : "idle"}">
          <ha-icon icon="${
            isHeating ? "mdi:fire" : "mdi:power-sleep"
          }"></ha-icon>
          ${isHeating ? "Heating" : "Idle"}
        </span>
        <span class="heat-loss" title="Heat loss coefficient">
          H = ${this._fix(room.heat_loss_coeff, 1)} W/K
        </span>
      </footer>
    `;
    return card;
  }

  // Escape HTML for anywhere we interpolate user/entity strings.
  _escape(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
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

  // ─── Detail view (tabbed) ─────────────────────────────────
  _renderRoomDetail(container) {
    const d = this._roomDetail;
    if (!d) return;

    const detail = document.createElement("div");
    detail.className = "detail-view";

    // Error state — always show something actionable instead of a blank panel.
    if (d._error) {
      detail.innerHTML = `
        <div class="section error-section">
          <h2><ha-icon icon="mdi:alert-circle-outline"></ha-icon>Could not load room</h2>
          <p class="hint">
            Something went wrong while loading this room's thermal model.
            The rest of the dashboard is unaffected. You can retry below.
          </p>
          <pre class="error-detail">${this._escape(d._error)}</pre>
          <div class="error-actions">
            <button class="primary-btn" id="retry-room-btn">
              <ha-icon icon="mdi:refresh"></ha-icon>
              Retry
            </button>
            <button class="secondary-btn" id="back-from-error-btn">
              <ha-icon icon="mdi:arrow-left"></ha-icon>
              Back to rooms
            </button>
          </div>
        </div>
      `;
      container.appendChild(detail);
      this.shadowRoot
        .getElementById("retry-room-btn")
        ?.addEventListener("click", () => this._loadRoomDetail(d.entry_id));
      this.shadowRoot
        .getElementById("back-from-error-btn")
        ?.addEventListener("click", () => this._goBack());
      return;
    }

    // Normalize defensive access — backend does the same, but belt-and-braces.
    const params = d.params || {};
    const predictions = d.predictions || null;
    const solar = d.solar_calc || null;
    const entryId = d.entry_id;

    // ── Stats row + spike banner (always visible) ──
    const heatingNow =
      d.hvac_action === "heating" || d.zone_is_heating === true;
    detail.innerHTML = `
      <div class="stats-row">
        ${this._statCard("mdi:thermometer", this._fmt(d.current_temp, "°C"), "Indoor")}
        ${this._statCard("mdi:target", this._fmt(d.target_temp, "°C"), "Target")}
        ${this._statCard("mdi:snowflake", this._fmt(d.outdoor_temp, "°C"), "Outdoor")}
        ${this._statCard(
          heatingNow ? "mdi:fire" : "mdi:power-sleep",
          this._escape(d.hvac_action || "idle"),
          "Action"
        )}
      </div>
    `;

    // Spike indicator — gas pulse that has been filtered out (cooking, shower, etc.).
    if (d.spike && d.spike.in_spike) {
      const raw = this._num(d.spike.raw_power_w, 0);
      const eff = this._num(d.spike.effective_power_w, 0);
      detail.innerHTML += `
        <div class="spike-banner" title="The heat source sees a short-lived power pulse that looks like cooking or a shower, not space heating. Learning is paused during the spike.">
          <ha-icon icon="mdi:chart-bell-curve-cumulative"></ha-icon>
          <div class="spike-text">
            <strong>Gas spike detected — learning paused</strong>
            <span>Raw power ${Math.round(raw)} W · effective ${Math.round(eff)} W · total events ${this._num(d.spike.spike_events, 0)}</span>
          </div>
        </div>
      `;
    }

    // ── Controls (temperature + presets + override) ──
    const preset = d.preset_mode || null;
    const presetModes = Array.isArray(d.preset_modes) && d.preset_modes.length
      ? d.preset_modes
      : ["comfort", "eco", "away", "sleep"];
    const presetChips = presetModes
      .map(
        (p) => `<button class="preset-chip ${preset === p ? "active" : ""}"
                       data-action="preset"
                       data-entry-id="${entryId}"
                       data-preset="${this._escape(p)}">${this._escape(p)}</button>`
      )
      .join("");

    const overrideOn = d.override_on === true;
    detail.innerHTML += `
      <div class="section controls-section">
        <h2><ha-icon icon="mdi:tune"></ha-icon>Control</h2>
        <div class="big-target">
          <button class="big-step-btn"
                  data-action="temp-down"
                  data-entry-id="${entryId}"
                  title="Decrease target by 0.5 °C">−</button>
          <div class="big-target-value">
            <span class="big-target-number">${this._fmt(d.target_temp, "")}</span>
            <span class="big-target-unit">°C target</span>
          </div>
          <button class="big-step-btn"
                  data-action="temp-up"
                  data-entry-id="${entryId}"
                  title="Increase target by 0.5 °C">+</button>
        </div>
        <div class="preset-row">${presetChips}</div>
        <div class="override-row ${overrideOn ? "on" : ""}">
          <div class="override-text">
            <ha-icon icon="${overrideOn ? "mdi:account-clock" : "mdi:account-clock-outline"}"></ha-icon>
            <div>
              <strong>Override${overrideOn ? " — ACTIVE" : ""}</strong>
              <span class="hint-inline">
                ${overrideOn
                  ? "Room pinned to comfort preset. Schedule &amp; away-grace are ignored."
                  : "Force comfort preset regardless of schedule/presence (great for WFH days)."}
              </span>
            </div>
          </div>
          <button class="toggle-btn ${overrideOn ? "on" : "off"}"
                  id="override-toggle"
                  data-entry-id="${entryId}"
                  title="${overrideOn ? "Turn override OFF" : "Turn override ON"}">
            <span class="toggle-thumb"></span>
          </button>
        </div>
        ${
          d.climate_entity_id
            ? `<div class="kv subtle">
                <span>Thermostat entity</span>
                <strong class="mono">${this._escape(d.climate_entity_id)}</strong>
              </div>`
            : ""
        }
      </div>
    `;

    // ── Tab strip ──
    const tabs = [
      { id: "overview", label: "Overview", icon: "mdi:home-thermometer-outline" },
      { id: "training", label: "Training", icon: "mdi:school" },
      { id: "predictions", label: "Predictions", icon: "mdi:crystal-ball" },
      {
        id: "couplings",
        label: `Couplings${d.couplings && d.couplings.length ? ` · ${d.couplings.length}` : ""}`,
        icon: "mdi:link-variant",
      },
    ];
    const tabBar = tabs
      .map(
        (t) => `<button class="tab-btn ${this._activeTab === t.id ? "active" : ""}"
                       data-tab="${t.id}">
          <ha-icon icon="${t.icon}"></ha-icon>
          <span>${t.label}</span>
        </button>`
      )
      .join("");
    detail.innerHTML += `
      <div class="tab-bar" role="tablist">
        ${tabBar}
      </div>
    `;

    // ── Tab body ──
    const tabBody = document.createElement("div");
    tabBody.className = "tab-body";
    detail.appendChild(tabBody);

    switch (this._activeTab) {
      case "training":
        this._renderTrainingTab(tabBody, d);
        break;
      case "predictions":
        this._renderPredictionsTab(tabBody, d);
        break;
      case "couplings":
        this._renderCouplingsTab(tabBody, d);
        break;
      case "overview":
      default:
        this._renderOverviewTab(tabBody, d);
        break;
    }

    container.appendChild(detail);

    // Bind tab strip. Query from `detail` (not shadowRoot) because
    // `container` has not been appended to the shadow tree yet.
    detail.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => this._setTab(btn.dataset.tab));
    });

    // Bind override toggle.
    const overrideBtn = detail.querySelector("#override-toggle");
    if (overrideBtn) {
      overrideBtn.addEventListener("click", () =>
        this._setOverride(
          overrideBtn.dataset.entryId,
          !(d.override_on === true)
        )
      );
    }

    // Bind tab-specific action buttons.
    const recomputeBtn = detail.querySelector("#recompute-btn");
    if (recomputeBtn) {
      recomputeBtn.addEventListener("click", () =>
        this._doRecompute(recomputeBtn.dataset.entryId)
      );
    }
    const simulateBtn = detail.querySelector("#simulate-btn");
    if (simulateBtn) {
      simulateBtn.addEventListener("click", () =>
        this._doSimulate(simulateBtn.dataset.entryId)
      );
    }

    // Bind coupling toggles.
    detail.querySelectorAll(".coupling-toggle").forEach((btn) => {
      btn.addEventListener("click", () => {
        const wasOn = btn.classList.contains("on");
        this._setCouplingEnabled(
          btn.dataset.entryId,
          btn.dataset.neighbourId,
          !wasOn
        );
      });
    });

    // Draw any charts that were added.
    requestAnimationFrame(() => {
      if (this._activeTab === "training") {
        this._drawLearningChart(d);
        this._drawErrorChart(d);
      } else if (this._activeTab === "predictions") {
        this._drawTempChart(d);
        this._drawPredictionOverlayChart(d);
        this._drawSimulationChart(d);
      }
    });
  }

  // ─── Tab: Overview ────────────────────────────────────────
  _renderOverviewTab(container, d) {
    const solar = d.solar_calc || null;
    const params = d.params || {};
    let html = "";

    // Zone info (only if multi-room)
    if (d.zone_rooms && d.zone_rooms.length > 1) {
      const others = d.zone_rooms.filter((n) => n !== d.room_name);
      const coHeat = d.co_heated_by_zone === true;
      html += `
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
                <strong>${this._fmt(d.zone_setpoint, " °C")}</strong>
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

    // Schedule card
    const sched = d.schedule || null;
    if (sched && sched.entity_id) {
      const isOn = sched.state === "on" || sched.state === true;
      html += `
        <div class="section schedule-section">
          <h2><ha-icon icon="mdi:calendar-clock"></ha-icon>Schedule</h2>
          <p class="hint">
            The schedule picks a preset; the preset's number entity supplies the
            actual target °C. Change the preset temperatures from HA settings.
          </p>
          <div class="schedule-grid">
            <div class="schedule-block">
              <div class="schedule-label">Entity</div>
              <div class="schedule-value mono">${this._escape(sched.entity_id)}</div>
            </div>
            <div class="schedule-block ${isOn ? "on" : "off"}">
              <div class="schedule-label">State</div>
              <div class="schedule-value">
                <ha-icon icon="${isOn ? "mdi:toggle-switch" : "mdi:toggle-switch-off-outline"}"></ha-icon>
                ${isOn ? "ON" : "OFF"}
              </div>
            </div>
            <div class="schedule-block">
              <div class="schedule-label">Temp when ON</div>
              <div class="schedule-value">${this._fmt(sched.on_temp, " °C")}</div>
            </div>
            <div class="schedule-block">
              <div class="schedule-label">Temp when OFF</div>
              <div class="schedule-value">${this._fmt(sched.off_temp, " °C")}</div>
            </div>
            ${
              sched.override_temp != null
                ? `<div class="schedule-block highlight">
                    <div class="schedule-label">Slot override</div>
                    <div class="schedule-value">${this._fmt(sched.override_temp, " °C")}</div>
                    <div class="schedule-sublabel">From the active schedule slot</div>
                  </div>`
                : ""
            }
          </div>
        </div>
      `;
    }

    // Windows
    const winSensors = Array.isArray(d.window_sensors) ? d.window_sensors : [];
    if (winSensors.length > 0 || d.window_open != null) {
      const open = d.window_open === true;
      const items = winSensors
        .map(
          (w) => `<li class="window-item ${w.state === "on" ? "open" : ""}">
            <ha-icon icon="${
              w.state === "on" ? "mdi:window-open-variant" : "mdi:window-closed-variant"
            }"></ha-icon>
            <span class="mono">${this._escape(w.entity_id || "")}</span>
            <span class="window-state">${this._escape(w.state || "unknown")}</span>
            ${
              w.friendly_name
                ? `<span class="window-friendly">${this._escape(w.friendly_name)}</span>`
                : ""
            }
          </li>`
        )
        .join("");
      html += `
        <div class="section window-section ${open ? "open" : ""}">
          <h2>
            <ha-icon icon="${
              open ? "mdi:window-open-variant" : "mdi:window-closed-variant"
            }"></ha-icon>
            Window / door sensors
          </h2>
          <p class="hint">
            When any sensor here reports <code>on</code>, heating for this room
            is paused. Learning is also paused during open-window events.
          </p>
          ${
            items
              ? `<ul class="window-list">${items}</ul>`
              : `<p class="empty-inline">No sensors configured.</p>`
          }
        </div>
      `;
    }

    // Heat delivery
    if (d.gas_meter_sensor || d.heat_power_w != null) {
      const kw = this._num(d.heat_power_w, 0) / 1000;
      html += `
        <div class="section">
          <h2><ha-icon icon="mdi:fire"></ha-icon>Heat delivery</h2>
          <p class="hint">
            Heat delivery is computed from the gas-meter derivative × boiler
            efficiency × this room's heat share. Works the same when the meter
            is a heat-pump electricity meter × COP.
          </p>
          <div class="params-grid">
            ${this._paramCard(
              "Current heat delivered",
              this._fix(d.heat_power_w, 0),
              "W",
              kw >= 1
                ? `≈ ${this._fix(kw, 2)} kW right now.`
                : "Low or idle — waiting for the boiler to fire."
            )}
            ${this._paramCard(
              "Gas meter",
              d.gas_meter_sensor
                ? this._escape(d.gas_meter_sensor)
                : "—",
              "",
              "Cumulative gas consumption (m³)"
            )}
            ${this._paramCard(
              "Boiler efficiency",
              this._fix(d.boiler_efficiency, 2),
              "",
              "Fraction of gas energy that reaches the house"
            )}
            ${this._paramCard(
              "Heat share",
              this._fix(d.heat_share, 2),
              "",
              "Fraction of boiler heat attributed to this room"
            )}
          </div>
        </div>
      `;
    }

    // Nudge history
    const nudges = Array.isArray(d.nudge_history) ? d.nudge_history : [];
    if (nudges.length > 0) {
      const reasonLabels = {
        initial: { icon: "mdi:play", label: "Initial setpoint" },
        nudge_up_room_cold: { icon: "mdi:arrow-up-bold", label: "Nudged up — room cold" },
        nudge_down_overshoot: { icon: "mdi:arrow-down-bold", label: "Nudged down — overshoot" },
        drift_back_to_target: { icon: "mdi:arrow-down", label: "Drifting back to target" },
        hold_at_target: { icon: "mdi:pause", label: "Holding at target" },
      };
      const rows = nudges
        .slice()
        .reverse()
        .slice(0, 20)
        .map((n) => {
          const info = reasonLabels[n.reason] || {
            icon: "mdi:information-outline",
            label: n.reason || "—",
          };
          const ts = n.timestamp
            ? new Date(this._num(n.timestamp, 0) * 1000).toLocaleString()
            : "—";
          return `<li class="nudge-item">
            <ha-icon icon="${info.icon}"></ha-icon>
            <div class="nudge-main">
              <div class="nudge-title">${this._escape(info.label)}
                <span class="nudge-sp">→ ${this._fmt(n.setpoint, " °C")}</span>
              </div>
              <div class="nudge-meta">
                ${this._escape(ts)}
                ${n.leader ? ` · leader: <strong>${this._escape(n.leader)}</strong>` : ""}
                ${n.target != null ? ` · target ${this._fmt(n.target, "°")}` : ""}
                ${n.current != null ? ` · indoor ${this._fmt(n.current, "°")}` : ""}
              </div>
            </div>
          </li>`;
        })
        .join("");
      html += `
        <div class="section">
          <h2><ha-icon icon="mdi:history"></ha-icon>Setpoint nudge history</h2>
          <p class="hint">
            Recent decisions by the zone controller. Small setpoint offsets
            (≤1 °C) preserve OpenTherm modulation.
          </p>
          <ul class="nudge-list">${rows}</ul>
        </div>
      `;
    }

    // Solar
    if (solar) {
      const sc = solar;
      const sgf = this._num(params.solar_gain_factor, 0);
      const ghi = this._num(sc.ghi_w_m2, 0);
      const absorbed = (ghi * sgf).toFixed(0);
      html += `
        <div class="section">
          <h2><ha-icon icon="mdi:weather-sunny"></ha-icon>Solar irradiance</h2>
          <p class="hint">
            Clear-sky model from sun elevation, reduced by cloud cover from a
            weather entity.
          </p>
          <div class="solar-grid">
            <div class="solar-block">
              <div class="solar-label">Sun elevation</div>
              <div class="solar-value">${this._fix(sc.sun_elevation_deg, 1)}°</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Sun azimuth</div>
              <div class="solar-value">${this._fix(sc.sun_azimuth_deg, 1)}°</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Weather entity</div>
              <div class="solar-value mono">${this._escape(sc.weather_entity || "—")}</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Condition</div>
              <div class="solar-value">${this._escape(sc.weather_condition || "—")}</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Cloud cover</div>
              <div class="solar-value">${this._fix(sc.cloud_coverage_pct, 0)} %</div>
              <div class="solar-sublabel">from ${this._escape(sc.cloud_source || "—")}</div>
            </div>
            <div class="solar-block">
              <div class="solar-label">Clear-sky GHI</div>
              <div class="solar-value">${this._fix(sc.ghi_clear_sky_w_m2, 0)} W/m²</div>
              <div class="solar-sublabel">Haurwitz model · cos(elevation)</div>
            </div>
            <div class="solar-block highlight">
              <div class="solar-label">Adjusted GHI</div>
              <div class="solar-value">${this._fix(sc.ghi_w_m2, 0)} W/m²</div>
              <div class="solar-sublabel">× (1 − 0.75·cloud<sup>3.4</sup>) = factor ${this._escape(String(sc.cloud_factor ?? "—"))}</div>
            </div>
            <div class="solar-block highlight">
              <div class="solar-label">Heat absorbed by room</div>
              <div class="solar-value">~${absorbed} W</div>
              <div class="solar-sublabel">GHI × gain factor (${this._fix(sgf, 2)})</div>
            </div>
          </div>
        </div>
      `;
    }

    container.innerHTML = html || `
      <div class="section empty-section">
        <p class="empty-inline">Nothing to show yet — configure a schedule, window sensors, or a gas meter from the options dialog to populate this tab.</p>
      </div>
    `;
  }

  // ─── Tab: Training ────────────────────────────────────────
  _renderTrainingTab(container, d) {
    const params = d.params || {};
    const progressPct = Math.min(100, this._num(d.learning_progress, 0));
    const minIdle = this._num(d.min_idle, 1);
    const minActive = this._num(d.min_active, 1);
    const idleSamples = this._num(d.idle_samples, 0);
    const activeSamples = this._num(d.active_samples, 0);
    const idlePct = Math.min(100, (idleSamples / (minIdle || 1)) * 100);
    const activePct = Math.min(100, (activeSamples / (minActive || 1)) * 100);
    const stateColor =
      d.model_state === "calibrated"
        ? "var(--success-color, #4caf50)"
        : "var(--warning-color, #ff9800)";

    let html = `
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
                  ? "Thermal model is calibrated — predictive control is active."
                  : "The model is still learning. Hysteresis control is used as fallback."
              }</p>
              <div class="sample-counts">
                <span><strong>${this._num(d.total_updates, 0)}</strong> total EKF updates</span>
                <span><strong>${this._num(d.observations && d.observations.length, 0)}</strong> stored observations</span>
              </div>
            </div>
          </div>

          <div class="sample-bars">
            <div class="sample-bar-group">
              <div class="sample-bar-header">
                <span>Idle observations</span>
                <span>${idleSamples} / ${minIdle}</span>
              </div>
              <div class="progress-bar large">
                <div class="progress-fill" style="width:${idlePct}%;background:var(--info-color, var(--primary-color))"></div>
              </div>
              <div class="sample-bar-desc">Collected when heating is off — used to learn heat loss rate (H).</div>
            </div>
            <div class="sample-bar-group">
              <div class="sample-bar-header">
                <span>Active observations</span>
                <span>${activeSamples} / ${minActive}</span>
              </div>
              <div class="progress-bar large">
                <div class="progress-fill" style="width:${activePct}%;background:var(--accent-color, #ff9800)"></div>
              </div>
              <div class="sample-bar-desc">Collected during heating — used to learn heating power (P).</div>
            </div>
          </div>
        </div>
      </div>

      <div class="section">
        <h2><ha-icon icon="mdi:tune-vertical"></ha-icon>Thermal model parameters</h2>
        <div class="params-grid">
          ${this._paramCard(
            "Heat loss coefficient (H)",
            this._fix(params.heat_loss_coeff, 1),
            "W/K",
            "How fast the room loses heat per °C indoor/outdoor difference"
          )}
          ${this._paramCard(
            "Thermal mass (C)",
            this._fix(params.thermal_mass, 0),
            "kJ/K",
            "Stored energy capacity — higher means slower to heat or cool"
          )}
          ${this._paramCard(
            "Heating power",
            this._fix(params.heating_power, 0),
            "W",
            "Effective heat delivered to this room when the system is firing"
          )}
          ${this._paramCard(
            "Solar gain factor",
            this._fix(params.solar_gain_factor, 2),
            "",
            "Fraction of incoming solar irradiance that warms the room"
          )}
        </div>
        <div class="badge-row">
          <span class="info-badge">
            <ha-icon icon="${d.uses_ekf ? "mdi:flash" : "mdi:chart-line"}"></ha-icon>
            ${d.uses_ekf ? "Extended Kalman Filter" : "Simple estimator"}
          </span>
          ${
            d.mean_prediction_error != null
              ? `<span class="info-badge ${
                  this._num(d.mean_prediction_error, 1) < 0.5 ? "ok" : ""
                }">
                  <ha-icon icon="mdi:bullseye-arrow"></ha-icon>
                  Prediction error ±${this._fix(d.mean_prediction_error, 3)} °C
                  ${
                    this._num(d.mean_prediction_error, 1) < 0.5
                      ? ""
                      : "(target: &lt; 0.5 °C)"
                  }
                </span>`
              : ""
          }
        </div>
      </div>

      <div class="section action-section">
        <h2><ha-icon icon="mdi:calculator-variant"></ha-icon>Recompute</h2>
        <p class="hint">
          Replay every stored observation through a fresh EKF. Use this after
          a bad period of data (e.g. a broken sensor now fixed) has dragged
          the estimates off.
        </p>
        <div class="action-row">
          <button class="primary-btn" id="recompute-btn" data-entry-id="${d.entry_id}">
            <ha-icon icon="mdi:calculator-variant"></ha-icon>
            Recompute thermal properties
          </button>
          ${this._renderStatusBadge()}
        </div>
      </div>

      <div class="section">
        <h2><ha-icon icon="mdi:trending-up"></ha-icon>Heat loss learning history</h2>
        <div class="chart-container">
          <canvas id="learning-chart" width="800" height="200"></canvas>
        </div>
        <p class="hint">Evolution of the heat loss coefficient (H) as more observations arrive.</p>
      </div>
    `;

    if (d.prediction_error_history && d.prediction_error_history.length > 1) {
      html += `
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

    container.innerHTML = html;
  }

  // ─── Tab: Predictions ─────────────────────────────────────
  _renderPredictionsTab(container, d) {
    const predictions = d.predictions || null;
    const lastDTObs = d.last_dT_observed;
    const lastDTPred = d.last_dT_predicted;
    const dTError =
      lastDTObs != null && lastDTPred != null
        ? Math.abs(this._num(lastDTObs, 0) - this._num(lastDTPred, 0))
        : null;

    let html = "";

    if (d.model_state === "calibrated" && predictions) {
      html += `
        <div class="section">
          <h2><ha-icon icon="mdi:crystal-ball"></ha-icon>Short-range predictions</h2>
          <div class="predictions-grid">
            <div class="prediction-card">
              <div class="prediction-label">Temp in 1 h (heating off)</div>
              <div class="prediction-value">${this._fmt(predictions.temp_1h_off, "°C")}</div>
            </div>
            <div class="prediction-card">
              <div class="prediction-label">Temp in 1 h (heating on)</div>
              <div class="prediction-value">${this._fmt(predictions.temp_1h_on, "°C")}</div>
            </div>
            <div class="prediction-card">
              <div class="prediction-label">Time to reach target</div>
              <div class="prediction-value">${
                predictions.time_to_target != null
                  ? this._fix(predictions.time_to_target, 0) + " min"
                  : "N/A"
              }</div>
            </div>
          </div>
        </div>
      `;
    } else {
      html += `
        <div class="section">
          <h2><ha-icon icon="mdi:crystal-ball"></ha-icon>Short-range predictions</h2>
          <p class="hint">
            Predictions appear here once the thermal model has enough data to
            calibrate (see the Training tab for progress).
          </p>
        </div>
      `;
    }

    // Last observed vs predicted ΔT (sanity check)
    if (lastDTObs != null || lastDTPred != null) {
      const errColor =
        dTError != null && dTError < 0.05
          ? "var(--success-color, #4caf50)"
          : dTError != null && dTError < 0.15
          ? "var(--warning-color, #ff9800)"
          : "var(--error-color, #f44336)";
      html += `
        <div class="section">
          <h2><ha-icon icon="mdi:sigma"></ha-icon>Latest step (ΔT per update)</h2>
          <p class="hint">
            On every controller tick, the model predicts how much the indoor
            temperature will change until the next tick and compares it to what
            actually happened.
          </p>
          <div class="predictions-grid">
            <div class="prediction-card">
              <div class="prediction-label">Observed ΔT</div>
              <div class="prediction-value">${this._fix(lastDTObs, 3)} °C</div>
            </div>
            <div class="prediction-card">
              <div class="prediction-label">Predicted ΔT</div>
              <div class="prediction-value">${this._fix(lastDTPred, 3)} °C</div>
            </div>
            <div class="prediction-card" style="color: ${errColor}">
              <div class="prediction-label">Abs. error</div>
              <div class="prediction-value" style="color: ${errColor}">${
                dTError != null ? this._fix(dTError, 3) + " °C" : "—"
              }</div>
            </div>
          </div>
        </div>
      `;
    }

    // 8-hour-ago overlay
    const history = Array.isArray(d.prediction_history) ? d.prediction_history : [];
    const hasOverlay = history.length > 0 && Array.isArray(d.observations) && d.observations.length > 1;
    if (hasOverlay) {
      html += `
        <div class="section">
          <h2><ha-icon icon="mdi:chart-timeline-variant"></ha-icon>Prediction vs. reality (8 h ago)</h2>
          <p class="hint">
            Gray line: the model's forecast of the next 8 h, frozen at that
            point in time. Blue line: what actually happened. The closer they
            track, the more accurate the model.
          </p>
          <div class="chart-container">
            <canvas id="prediction-overlay-chart" width="800" height="260"></canvas>
          </div>
          <div class="chart-legend">
            <span class="legend-item"><span class="legend-dot" style="background:var(--primary-color)"></span>Observed indoor</span>
            <span class="legend-item"><span class="legend-dot" style="background:var(--secondary-text-color, #888)"></span>Forecast from 8 h ago</span>
          </div>
        </div>
      `;
    }

    // 24h simulation
    html += `
      <div class="section action-section">
        <h2><ha-icon icon="mdi:chart-timeline-variant-shimmer"></ha-icon>24 h simulation</h2>
        <p class="hint">
          Run the full thermal-model trajectory for the next 24 h using the
          current schedule, weather forecast and solar forecast. The result is
          cached so it stays visible between refreshes.
        </p>
        <div class="action-row">
          <button class="primary-btn" id="simulate-btn" data-entry-id="${d.entry_id}">
            <ha-icon icon="mdi:play"></ha-icon>
            Simulate next 24 h
          </button>
          ${this._renderStatusBadge()}
        </div>
    `;

    const sim = d.last_simulation;
    if (sim && Array.isArray(sim.trajectory) && sim.trajectory.length > 1) {
      const finishedAt = sim.timestamp
        ? new Date(this._num(sim.timestamp, 0) * 1000).toLocaleString()
        : null;
      html += `
        <div class="sim-meta">
          ${finishedAt ? `<span>Last run: <strong>${this._escape(finishedAt)}</strong></span>` : ""}
          <span>${sim.trajectory.length} steps · horizon ${this._fix(sim.horizon_hours || 24, 0)} h</span>
          ${sim.total_cost != null ? `<span>Total cost: <strong>€${this._fix(sim.total_cost, 2)}</strong></span>` : ""}
          ${sim.total_energy_kwh != null ? `<span>Energy: <strong>${this._fix(sim.total_energy_kwh, 2)} kWh</strong></span>` : ""}
        </div>
        <div class="chart-container">
          <canvas id="simulation-chart" width="800" height="260"></canvas>
        </div>
        <div class="chart-legend">
          <span class="legend-item"><span class="legend-dot" style="background:var(--primary-color)"></span>Predicted indoor</span>
          <span class="legend-item"><span class="legend-dot" style="background:var(--accent-color, #ff9800)"></span>Target</span>
          <span class="legend-item"><span class="legend-shade" style="background:var(--error-color, #f44336);opacity:0.2"></span>Heating</span>
        </div>
      `;
    } else {
      html += `
        <p class="empty-inline" style="margin-top: 12px;">
          No simulation has been run yet. Click the button above to produce a
          24 h forecast.
        </p>
      `;
    }
    html += `</div>`;

    // Temperature history chart (stays in Predictions — it's the
    // canonical "what has the room been doing" view).
    html += `
      <div class="section">
        <h2><ha-icon icon="mdi:chart-line"></ha-icon>Recent temperature history</h2>
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
    `;

    container.innerHTML = html;
  }

  // ─── Tab: Couplings ───────────────────────────────────────
  _renderCouplingsTab(container, d) {
    const couplings = Array.isArray(d.couplings) ? d.couplings : [];
    if (couplings.length === 0) {
      container.innerHTML = `
        <div class="section">
          <h2><ha-icon icon="mdi:link-variant-off"></ha-icon>No couplings configured</h2>
          <p class="hint">
            Couplings let you model the heat that flows through doors, hallways
            or partition walls into neighbouring rooms. They're optional — the
            single-room model still works without them. Add one via
            <strong>Settings → Devices &amp; Services → Predictive Heating →
            Configure</strong>.
          </p>
          <p class="hint">
            A coupling is <em>U·(T_neighbour − T_room)</em> treated as an
            external heat term, letting the heat loss estimate stay honest even
            when the rooms aren't thermally isolated.
          </p>
        </div>
      `;
      return;
    }

    const entryId = d.entry_id;
    const currentTemp = d.current_temp;
    const rows = couplings
      .map((c) => {
        const nbId = c.neighbour_entry_id || "";
        const nbName = c.neighbour_name || "Unknown room";
        const nbTemp = c.neighbour_temp;
        const u = this._num(c.u_value, 0);
        const enabled = c.enabled !== false;
        const dT =
          currentTemp != null && nbTemp != null
            ? this._num(nbTemp, 0) - this._num(currentTemp, 0)
            : null;
        const flowW = dT != null ? u * dT : null;
        const arrow =
          dT == null
            ? "mdi:swap-horizontal"
            : dT > 0
            ? "mdi:arrow-right-bold"
            : "mdi:arrow-left-bold";
        const flowLabel =
          flowW == null
            ? "—"
            : `${flowW > 0 ? "+" : ""}${flowW.toFixed(0)} W`;
        const flowHint =
          dT == null
            ? "Waiting for neighbour temperature"
            : dT > 0
            ? `Heat flowing IN from ${this._escape(nbName)} (they're warmer)`
            : dT < 0
            ? `Heat leaking OUT to ${this._escape(nbName)} (you're warmer)`
            : "Neighbour at same temperature";
        return `
          <div class="coupling-row ${enabled ? "" : "disabled"}">
            <div class="coupling-info">
              <div class="coupling-name">
                <ha-icon icon="${enabled ? "mdi:door-open" : "mdi:door-closed"}"></ha-icon>
                <strong>${this._escape(nbName)}</strong>
                <span class="coupling-uval">U = ${this._fix(u, 1)} W/K</span>
              </div>
              <div class="coupling-flow">
                <ha-icon icon="${arrow}"></ha-icon>
                <span class="coupling-flow-value">${flowLabel}</span>
                <span class="coupling-flow-hint">${flowHint}</span>
              </div>
              <div class="coupling-sub">
                Neighbour: ${nbTemp != null ? this._fix(nbTemp, 1) + " °C" : "—"}
                · ΔT: ${dT != null ? this._fix(dT, 1) + " K" : "—"}
              </div>
            </div>
            <button class="toggle-btn coupling-toggle ${enabled ? "on" : "off"}"
                    data-entry-id="${entryId}"
                    data-neighbour-id="${this._escape(nbId)}"
                    title="${enabled ? "Disable this coupling" : "Enable this coupling"}">
              <span class="toggle-thumb"></span>
            </button>
          </div>
        `;
      })
      .join("");

    container.innerHTML = `
      <div class="section">
        <h2><ha-icon icon="mdi:link-variant"></ha-icon>Thermal couplings</h2>
        <p class="hint">
          Each row is a door / partition between this room and a neighbour.
          <strong>U</strong> is how much heat (W) flows per °C temperature
          difference. Disable one to simulate "door closed" or to A/B test
          whether it matters.
        </p>
        <div class="coupling-list">${rows}</div>
      </div>
    `;
  }

  _renderStatusBadge() {
    if (!this._actionStatus) return "";
    const kind = this._actionStatus.kind || "info";
    const icon =
      kind === "ok"
        ? "mdi:check-circle"
        : kind === "error"
        ? "mdi:alert-circle"
        : "mdi:progress-clock";
    return `
      <span class="status-badge ${this._escape(kind)}">
        <ha-icon icon="${icon}"></ha-icon>
        ${this._escape(this._actionStatus.text || "")}
      </span>
    `;
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
    if (v == null || Number.isNaN(v)) return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return n.toFixed(1) + suffix;
  }

  // Formats a number with N decimals, or returns a placeholder.
  // Never throws, never produces NaN or "undefined".
  _fix(v, decimals = 1, fallback = "—") {
    if (v == null || Number.isNaN(v)) return fallback;
    const n = Number(v);
    if (!Number.isFinite(n)) return fallback;
    return n.toFixed(decimals);
  }

  _num(v, fallback = 0) {
    if (v == null) return fallback;
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
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

  // Plot the observed indoor trace against the model's forecast taken
  // ~8 h earlier, so the user can see "was the model right?" at a glance.
  _drawPredictionOverlayChart(data) {
    const canvas = this.shadowRoot.getElementById("prediction-overlay-chart");
    if (!canvas) return;
    const obs = Array.isArray(data.observations) ? data.observations : [];
    const history = Array.isArray(data.prediction_history)
      ? data.prediction_history
      : [];
    if (obs.length < 2 || history.length === 0) return;

    // Pick the forecast from as close to 8h ago as we have.
    const nowTs = obs[obs.length - 1].timestamp;
    const target = nowTs - 8 * 3600;
    let best = history[0];
    let bestDiff = Math.abs(this._num(history[0].ts, 0) - target);
    for (const snap of history) {
      const d = Math.abs(this._num(snap.ts, 0) - target);
      if (d < bestDiff) {
        best = snap;
        bestDiff = d;
      }
    }
    if (!best || !Array.isArray(best.trajectory) || best.trajectory.length < 2) {
      return;
    }

    const ctx = canvas.getContext("2d");
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * (window.devicePixelRatio || 1);
    canvas.height = 260 * (window.devicePixelRatio || 1);
    canvas.style.width = rect.width + "px";
    canvas.style.height = "260px";
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);

    const W = rect.width;
    const H = 260;
    const pad = { top: 20, right: 20, bottom: 40, left: 50 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    const forecastT0 = this._num(best.ts, 0);
    const horizon = this._num(best.horizon_hours, 8);
    const tMin = forecastT0;
    const tMax = forecastT0 + horizon * 3600;
    const tRange = tMax - tMin || 1;

    const relevantObs = obs.filter(
      (o) => o.timestamp >= tMin && o.timestamp <= tMax
    );

    // Value range.
    let yMin = Infinity,
      yMax = -Infinity;
    for (const o of relevantObs) {
      yMin = Math.min(yMin, o.t_indoor);
      yMax = Math.max(yMax, o.t_indoor);
    }
    for (const pt of best.trajectory) {
      yMin = Math.min(yMin, this._num(pt.temperature, 0));
      yMax = Math.max(yMax, this._num(pt.temperature, 0));
    }
    if (!Number.isFinite(yMin) || !Number.isFinite(yMax)) return;
    yMin = Math.floor(yMin - 1);
    yMax = Math.ceil(yMax + 1);
    const yRange = yMax - yMin || 1;

    const toX = (t) => pad.left + ((t - tMin) / tRange) * plotW;
    const toY = (v) => pad.top + (1 - (v - yMin) / yRange) * plotH;

    const txtCol = this._themeColor("--secondary-text-color", "#888");
    const gridCol = this._themeColor("--divider-color", "rgba(0,0,0,0.08)");
    const indoor = this._themeColor("--primary-color", "#03a9f4");
    const forecast = this._themeColor("--secondary-text-color", "#888");

    // Grid + Y labels
    ctx.strokeStyle = gridCol;
    ctx.lineWidth = 1;
    ctx.font = "11px var(--paper-font-body1_-_font-family, sans-serif)";
    for (let i = 0; i <= 5; i++) {
      const v = yMin + (yRange * i) / 5;
      const y = toY(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = txtCol;
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(1) + "°", pad.left - 8, y + 4);
    }

    // X labels — hours from forecast start.
    ctx.textAlign = "center";
    ctx.fillStyle = txtCol;
    for (let h = 0; h <= horizon; h += Math.max(1, Math.round(horizon / 4))) {
      const t = forecastT0 + h * 3600;
      const x = toX(t);
      const lbl = h === 0 ? "T−8h" : h === horizon ? "now" : `+${h}h`;
      ctx.fillText(lbl, x, H - pad.bottom + 20);
    }

    // Forecast line (dashed, gray)
    ctx.strokeStyle = forecast;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    for (let i = 0; i < best.trajectory.length; i++) {
      const pt = best.trajectory[i];
      const t = forecastT0 + this._num(pt.t, 0) * 3600;
      const v = this._num(pt.temperature, 0);
      const x = toX(t);
      const y = toY(v);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.setLineDash([]);

    // Observed line (solid, primary)
    if (relevantObs.length >= 2) {
      ctx.strokeStyle = indoor;
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let i = 0; i < relevantObs.length; i++) {
        const o = relevantObs[i];
        const x = toX(o.timestamp);
        const y = toY(o.t_indoor);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }

  // Plot the 24 h predictive simulation trajectory (indoor temp, heating
  // on/off shading, target line).
  _drawSimulationChart(data) {
    const canvas = this.shadowRoot.getElementById("simulation-chart");
    if (!canvas) return;
    const sim = data.last_simulation;
    if (!sim || !Array.isArray(sim.trajectory) || sim.trajectory.length < 2) {
      return;
    }

    const ctx = canvas.getContext("2d");
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * (window.devicePixelRatio || 1);
    canvas.height = 260 * (window.devicePixelRatio || 1);
    canvas.style.width = rect.width + "px";
    canvas.style.height = "260px";
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);

    const W = rect.width;
    const H = 260;
    const pad = { top: 20, right: 20, bottom: 40, left: 50 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    const traj = sim.trajectory;
    const horizon =
      this._num(sim.horizon_hours, 0) ||
      this._num(traj[traj.length - 1].t, 24);
    const targetTemp = data.target_temp;

    let yMin = Infinity,
      yMax = -Infinity;
    for (const pt of traj) {
      const v = this._num(pt.temperature, 0);
      yMin = Math.min(yMin, v);
      yMax = Math.max(yMax, v);
    }
    if (targetTemp != null) {
      yMin = Math.min(yMin, this._num(targetTemp, 0));
      yMax = Math.max(yMax, this._num(targetTemp, 0));
    }
    yMin = Math.floor(yMin - 1);
    yMax = Math.ceil(yMax + 1);
    const yRange = yMax - yMin || 1;

    const toX = (t) => pad.left + (t / horizon) * plotW;
    const toY = (v) => pad.top + (1 - (v - yMin) / yRange) * plotH;

    const txtCol = this._themeColor("--secondary-text-color", "#888");
    const gridCol = this._themeColor("--divider-color", "rgba(0,0,0,0.08)");
    const indoor = this._themeColor("--primary-color", "#03a9f4");
    const target = this._themeColor("--accent-color", "#ff9800");
    const heatCol = this._themeColor("--error-color", "#f44336");

    ctx.strokeStyle = gridCol;
    ctx.lineWidth = 1;
    ctx.font = "11px var(--paper-font-body1_-_font-family, sans-serif)";
    for (let i = 0; i <= 5; i++) {
      const v = yMin + (yRange * i) / 5;
      const y = toY(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = txtCol;
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(1) + "°", pad.left - 8, y + 4);
    }

    // X labels — every 4h
    ctx.textAlign = "center";
    ctx.fillStyle = txtCol;
    const step = horizon <= 12 ? 2 : 4;
    for (let h = 0; h <= horizon; h += step) {
      const x = toX(h);
      ctx.fillText(`+${h}h`, x, H - pad.bottom + 20);
    }

    // Heating shading — where heating_fraction > 0.05.
    ctx.fillStyle = this._withAlpha(heatCol, 0.15);
    for (let i = 0; i < traj.length - 1; i++) {
      const hf = this._num(traj[i].heating_fraction, 0);
      if (hf > 0.05) {
        const x1 = toX(this._num(traj[i].t, 0));
        const x2 = toX(this._num(traj[i + 1].t, 0));
        ctx.fillRect(x1, pad.top, x2 - x1, plotH);
      }
    }

    // Target line
    if (targetTemp != null) {
      ctx.strokeStyle = target;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      const ty = toY(this._num(targetTemp, 0));
      ctx.moveTo(pad.left, ty);
      ctx.lineTo(W - pad.right, ty);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Indoor forecast line
    ctx.strokeStyle = indoor;
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < traj.length; i++) {
      const x = toX(this._num(traj[i].t, 0));
      const y = toY(this._num(traj[i].temperature, 0));
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
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

      /* ── Inline controls (card + detail) ───────────────── */
      .target-block { gap: 4px; }
      .target-controls {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .step-btn {
        width: 26px;
        height: 26px;
        border: 1px solid var(--divider-color, rgba(0,0,0,0.15));
        background: var(--card-background-color, white);
        color: var(--primary-text-color);
        border-radius: 50%;
        font-size: 18px;
        line-height: 1;
        cursor: pointer;
        transition: background-color 0.15s, transform 0.1s;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0;
      }
      .step-btn:hover {
        background: color-mix(in srgb, var(--primary-color) 12%, transparent);
      }
      .step-btn:active { transform: scale(0.95); }

      .preset-row {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin: 4px 0 12px;
      }
      .preset-chip {
        padding: 4px 12px;
        font-size: 12px;
        font-weight: 500;
        border-radius: 14px;
        border: 1px solid var(--divider-color, rgba(0,0,0,0.15));
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        color: var(--secondary-text-color);
        cursor: pointer;
        text-transform: capitalize;
        transition: background-color 0.15s, color 0.15s;
      }
      .preset-chip:hover {
        background: color-mix(in srgb, var(--primary-color) 10%, transparent);
        color: var(--primary-text-color);
      }
      .preset-chip.active {
        background: var(--primary-color);
        color: var(--text-primary-color, white);
        border-color: var(--primary-color);
      }

      .status-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 4px;
        margin-bottom: 10px;
      }
      .chip {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 8px;
        font-size: 11px;
        border-radius: 10px;
        background: var(--secondary-background-color, rgba(0,0,0,0.04));
        color: var(--secondary-text-color);
      }
      .chip.mono { font-family: var(--code-font-family, monospace); font-size: 10px; }
      .chip ha-icon { --mdc-icon-size: 12px; }
      .chip.schedule.on { color: var(--success-color, #4caf50); }
      .chip.schedule.off { color: var(--secondary-text-color); }

      .window-banner {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px 10px;
        margin-bottom: 12px;
        font-size: 12px;
        background: color-mix(in srgb, var(--warning-color, #ff9800) 12%, transparent);
        border-left: 3px solid var(--warning-color, #ff9800);
        border-radius: 4px;
        color: var(--primary-text-color);
      }
      .window-banner ha-icon {
        --mdc-icon-size: 16px;
        color: var(--warning-color, #ff9800);
      }
      .room-card.is-window-open {
        border-color: var(--warning-color, #ff9800);
      }

      /* ── Detail: big controls ──────────────────────────── */
      .controls-section .hint { margin-bottom: 14px; }
      .big-target {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 16px;
        margin: 12px 0 14px;
      }
      .big-step-btn {
        width: 48px;
        height: 48px;
        border: 1px solid var(--divider-color, rgba(0,0,0,0.15));
        background: var(--card-background-color, white);
        color: var(--primary-text-color);
        border-radius: 50%;
        font-size: 28px;
        line-height: 1;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0;
        transition: background-color 0.15s, transform 0.1s;
      }
      .big-step-btn:hover {
        background: color-mix(in srgb, var(--primary-color) 15%, transparent);
      }
      .big-step-btn:active { transform: scale(0.95); }
      .big-target-value {
        display: flex;
        flex-direction: column;
        align-items: center;
        min-width: 120px;
      }
      .big-target-number {
        font-size: 44px;
        font-weight: 500;
        line-height: 1;
        color: var(--primary-color);
      }
      .big-target-unit {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-top: 4px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .kv.subtle {
        border-bottom: none;
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-top: 8px;
      }
      .kv .mono { font-family: var(--code-font-family, monospace); font-size: 12px; }

      /* ── Detail: schedule section ──────────────────────── */
      .schedule-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 10px;
      }
      .schedule-block {
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 8px;
        padding: 12px;
      }
      .schedule-block.highlight {
        background: color-mix(in srgb, var(--primary-color) 10%, transparent);
        border-left: 3px solid var(--primary-color);
      }
      .schedule-block.on { border-left: 3px solid var(--success-color, #4caf50); }
      .schedule-block.off { border-left: 3px solid var(--secondary-text-color); }
      .schedule-label {
        font-size: 11px;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .schedule-value {
        font-size: 16px;
        font-weight: 500;
        margin-top: 4px;
        display: inline-flex;
        align-items: center;
        gap: 4px;
      }
      .schedule-value.mono {
        font-family: var(--code-font-family, monospace);
        font-size: 12px;
      }
      .schedule-value ha-icon { --mdc-icon-size: 18px; }
      .schedule-sublabel {
        font-size: 11px;
        color: var(--secondary-text-color);
        margin-top: 4px;
      }

      /* ── Detail: window section ────────────────────────── */
      .window-section.open h2 ha-icon {
        color: var(--warning-color, #ff9800);
      }
      .window-list {
        list-style: none;
        padding: 0;
        margin: 0;
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .window-item {
        display: grid;
        grid-template-columns: auto 1fr auto auto;
        align-items: center;
        gap: 10px;
        padding: 10px 12px;
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 8px;
        font-size: 13px;
      }
      .window-item.open {
        background: color-mix(in srgb, var(--warning-color, #ff9800) 12%, transparent);
      }
      .window-item ha-icon { --mdc-icon-size: 20px; }
      .window-item.open ha-icon { color: var(--warning-color, #ff9800); }
      .window-state {
        text-transform: uppercase;
        font-size: 11px;
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--divider-color, rgba(0,0,0,0.08));
      }
      .window-item.open .window-state {
        background: var(--warning-color, #ff9800);
        color: white;
      }
      .window-friendly {
        font-size: 12px;
        color: var(--secondary-text-color);
      }
      .empty-inline {
        color: var(--secondary-text-color);
        font-size: 13px;
        margin: 0;
      }

      /* ── Detail: nudge history ─────────────────────────── */
      .nudge-list {
        list-style: none;
        padding: 0;
        margin: 0;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .nudge-item {
        display: grid;
        grid-template-columns: auto 1fr;
        align-items: start;
        gap: 10px;
        padding: 8px 10px;
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 6px;
      }
      .nudge-item > ha-icon {
        --mdc-icon-size: 18px;
        color: var(--primary-color);
        margin-top: 2px;
      }
      .nudge-main { display: flex; flex-direction: column; gap: 2px; }
      .nudge-title {
        font-size: 13px;
        font-weight: 500;
      }
      .nudge-sp {
        font-weight: 400;
        color: var(--primary-color);
        margin-left: 6px;
      }
      .nudge-meta {
        font-size: 11px;
        color: var(--secondary-text-color);
      }

      /* ── Detail: error fallback ────────────────────────── */
      .error-section h2 ha-icon { color: var(--error-color, #f44336); }
      .error-detail {
        background: var(--secondary-background-color, rgba(0,0,0,0.04));
        padding: 10px 12px;
        border-radius: 6px;
        white-space: pre-wrap;
        word-break: break-word;
        font-size: 12px;
        font-family: var(--code-font-family, monospace);
        color: var(--error-color, #f44336);
        margin: 8px 0 16px;
      }
      .error-actions { display: flex; gap: 8px; flex-wrap: wrap; }
      .primary-btn, .secondary-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 8px 16px;
        border-radius: 18px;
        font-size: 13px;
        cursor: pointer;
        border: 1px solid transparent;
      }
      .primary-btn {
        background: var(--primary-color);
        color: var(--text-primary-color, white);
        border-color: var(--primary-color);
      }
      .secondary-btn {
        background: transparent;
        color: var(--primary-text-color);
        border-color: var(--divider-color, rgba(0,0,0,0.15));
      }
      .primary-btn ha-icon, .secondary-btn ha-icon { --mdc-icon-size: 16px; }

      /* ── Tab bar ─────────────────────────────────────── */
      .tab-bar {
        display: flex;
        gap: 4px;
        background: var(--card-background-color, white);
        border: 1px solid var(--divider-color, rgba(0,0,0,0.08));
        border-radius: var(--ha-card-border-radius, 12px);
        padding: 4px;
        margin-bottom: 16px;
        overflow-x: auto;
      }
      .tab-btn {
        flex: 1 1 auto;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        padding: 10px 14px;
        background: transparent;
        border: none;
        border-radius: 10px;
        cursor: pointer;
        color: var(--secondary-text-color);
        font-size: 13px;
        font-weight: 500;
        transition: background-color 0.15s, color 0.15s;
        white-space: nowrap;
      }
      .tab-btn ha-icon { --mdc-icon-size: 18px; }
      .tab-btn:hover {
        background: color-mix(in srgb, var(--primary-color) 8%, transparent);
        color: var(--primary-text-color);
      }
      .tab-btn.active {
        background: var(--primary-color);
        color: var(--text-primary-color, white);
      }
      .tab-btn.active ha-icon { color: inherit; }

      .tab-body {
        display: block;
      }
      .empty-section .empty-inline {
        font-size: 14px;
        padding: 12px 0;
      }

      /* ── Spike banner ───────────────────────────────── */
      .spike-banner {
        display: flex;
        gap: 10px;
        align-items: center;
        padding: 10px 14px;
        margin-bottom: 16px;
        border-radius: var(--ha-card-border-radius, 12px);
        background: color-mix(in srgb, var(--warning-color, #ff9800) 14%, transparent);
        border-left: 4px solid var(--warning-color, #ff9800);
        color: var(--primary-text-color);
      }
      .spike-banner ha-icon {
        --mdc-icon-size: 24px;
        color: var(--warning-color, #ff9800);
        flex-shrink: 0;
      }
      .spike-text { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
      .spike-text span {
        font-size: 11px;
        color: var(--secondary-text-color);
      }

      /* ── Override toggle row ─────────────────────────── */
      .override-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        padding: 12px 14px;
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 10px;
        margin-top: 4px;
      }
      .override-row.on {
        background: color-mix(in srgb, var(--primary-color) 10%, transparent);
        border-left: 3px solid var(--primary-color);
      }
      .override-text {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 13px;
      }
      .override-text ha-icon {
        --mdc-icon-size: 22px;
        color: var(--primary-color);
        flex-shrink: 0;
      }
      .override-text > div { display: flex; flex-direction: column; gap: 2px; }
      .hint-inline {
        font-size: 11px;
        color: var(--secondary-text-color);
      }

      /* ── Toggle switch (reused for override + couplings) ─ */
      .toggle-btn {
        position: relative;
        width: 44px;
        height: 24px;
        border-radius: 12px;
        background: var(--divider-color, rgba(0,0,0,0.15));
        border: none;
        cursor: pointer;
        flex-shrink: 0;
        transition: background-color 0.2s;
        padding: 0;
      }
      .toggle-btn.on {
        background: var(--primary-color);
      }
      .toggle-thumb {
        position: absolute;
        top: 2px;
        left: 2px;
        width: 20px;
        height: 20px;
        border-radius: 50%;
        background: white;
        box-shadow: 0 1px 3px rgba(0,0,0,0.2);
        transition: transform 0.2s;
      }
      .toggle-btn.on .toggle-thumb {
        transform: translateX(20px);
      }

      /* ── Sample counts inside training ──────────────── */
      .sample-counts {
        display: flex;
        flex-wrap: wrap;
        gap: 14px;
        margin-top: 10px;
        font-size: 12px;
        color: var(--secondary-text-color);
      }
      .sample-counts strong { color: var(--primary-text-color); }

      /* ── Action sections (recompute / simulate) ─────── */
      .action-section .hint { margin-bottom: 14px; }
      .action-row {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
      }
      .status-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 12px;
        border-radius: 14px;
        font-size: 12px;
        background: var(--secondary-background-color, rgba(0,0,0,0.04));
        color: var(--secondary-text-color);
      }
      .status-badge ha-icon { --mdc-icon-size: 14px; }
      .status-badge.ok {
        background: color-mix(in srgb, var(--success-color, #4caf50) 14%, transparent);
        color: var(--success-color, #4caf50);
      }
      .status-badge.error {
        background: color-mix(in srgb, var(--error-color, #f44336) 14%, transparent);
        color: var(--error-color, #f44336);
      }
      .status-badge.info {
        background: color-mix(in srgb, var(--primary-color) 12%, transparent);
        color: var(--primary-color);
      }

      /* ── Simulation meta line ───────────────────────── */
      .sim-meta {
        display: flex;
        flex-wrap: wrap;
        gap: 16px;
        font-size: 12px;
        color: var(--secondary-text-color);
        margin: 0 0 10px;
        padding: 8px 12px;
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 6px;
      }
      .sim-meta strong { color: var(--primary-text-color); }

      /* ── Couplings ──────────────────────────────────── */
      .coupling-list {
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .coupling-row {
        display: grid;
        grid-template-columns: 1fr auto;
        align-items: center;
        gap: 12px;
        padding: 12px 14px;
        background: var(--secondary-background-color, rgba(0,0,0,0.03));
        border-radius: 10px;
      }
      .coupling-row.disabled {
        opacity: 0.55;
      }
      .coupling-info { display: flex; flex-direction: column; gap: 4px; }
      .coupling-name {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 14px;
      }
      .coupling-name ha-icon {
        --mdc-icon-size: 20px;
        color: var(--primary-color);
      }
      .coupling-uval {
        font-size: 11px;
        color: var(--secondary-text-color);
        background: var(--card-background-color, white);
        padding: 1px 8px;
        border-radius: 10px;
      }
      .coupling-flow {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
      }
      .coupling-flow ha-icon { --mdc-icon-size: 16px; }
      .coupling-flow-value {
        font-weight: 500;
        min-width: 56px;
      }
      .coupling-flow-hint {
        color: var(--secondary-text-color);
        font-size: 12px;
      }
      .coupling-sub {
        font-size: 11px;
        color: var(--secondary-text-color);
      }

      /* Responsive */
      @media (max-width: 600px) {
        .container { padding: 12px; }
        .room-grid { grid-template-columns: 1fr; }
        .stats-row { grid-template-columns: repeat(2, 1fr); }
        .params-grid { grid-template-columns: 1fr; }
        .training-overall { flex-direction: column; text-align: center; }
        .solar-grid { grid-template-columns: 1fr 1fr; }
        .schedule-grid { grid-template-columns: 1fr 1fr; }
        .big-target-number { font-size: 36px; }
        .big-step-btn { width: 44px; height: 44px; font-size: 24px; }
        .tab-btn span { display: none; }
        .tab-btn { padding: 10px; }
        .override-row { flex-wrap: wrap; }
        .coupling-flow { flex-wrap: wrap; }
      }
    `;
  }
}

customElements.define("predictive-heating-panel", PredictiveHeatingPanel);
