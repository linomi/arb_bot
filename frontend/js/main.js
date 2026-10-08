const state = {
  activeTab: "dashboard",
  groups: [],
  selectedGroupId: null,
  selectedFit: null,
  backboneCfg: null,
  initCfg: null,
  systemCfg: null,
  initMethod: "random",
  botPollTimer: null,
  fitPollTimer: null,
  lastFitAt: {},
  lastFitData: {},
  liquidSymbols: [],
  selectedSymbols: new Set(),
  activeExchange: "nobitex",
};

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab === "dashboard") {
    refreshDashboard();
    startFitPoll();
  } else {
    stopFitPoll();
  }
  if (tab === "init") refreshInitTab();
  if (tab === "settings") refreshSettingsTab();
}

function startFitPoll() {
  stopFitPoll();
  const tick = async () => {
    if (state.activeTab !== "dashboard" || !state.selectedGroupId) return;
    await selectGroup(state.selectedGroupId, false);
  };
  state.fitPollTimer = setInterval(tick, 5000);
}
function stopFitPoll() {
  if (state.fitPollTimer) {
    clearInterval(state.fitPollTimer);
    state.fitPollTimer = null;
  }
}

function FIELD_HINTS() {
  return {
    target_profit_rate: t("hint.target_profit_rate"),
    stationarity_method: t("hint.stationarity_method"),
    entry_retry_cooldown_sec: t("hint.entry_retry_cooldown_sec"),
    max_holding_hours: t("hint.max_holding_hours"),
    try_all_dependents: t("hint.try_all_dependents"),
    fit_log_interval_sec: t("hint.fit_log_interval_sec"),
    half_life_max_fraction: t("hint.half_life_max_fraction"),
    require_oos_positive: t("hint.require_oos_positive"),
    max_open_trades: t("hint.max_open_trades"),
    max_total_gross_notional: t("hint.max_total_gross_notional"),
    data_staleness_mult: t("hint.data_staleness_mult"),
    max_entry_scale: t("hint.max_entry_scale"),
  };
}

// Section layout for the (long) backbone form. Keys not listed fall into "other".
const BACKBONE_SECTIONS = [
  ["signal", ["window_size", "sampling_time", "z_entry", "z_close", "z_stop_loss", "max_holding_hours"]],
  ["stats", ["stationarity_method", "adf_alpha", "kpss_alpha", "half_life_max_fraction"]],
  ["costs", ["fee_rate", "slippage_rate", "target_profit_rate", "funding_rate_estimate", "expected_holding_funding_intervals"]],
  ["sizing", ["trade_notional", "max_entry_scale", "max_open_trades", "max_total_gross_notional",
              "liquidation_proximity_fraction", "entry_retry_cooldown_sec", "data_staleness_mult"]],
  ["xt", ["xt_leverage", "xt_margin_mode"]],
  ["init_opts", ["try_all_dependents", "fit_log_interval_sec"]],
];
// Keys never shown as a generic field (they have their own control).
const HIDDEN_FORM_KEYS = { "init-form": ["method"] };

function renderParamForm(formEl, data) {
  if (!formEl) return;
  formEl.innerHTML = "";
  const hidden = HIDDEN_FORM_KEYS[formEl.id] || [];
  const entries = Object.entries(data || {}).filter(([k]) => !hidden.includes(k));

  const buildField = (key, value) => {
    const label = document.createElement("label");
    const isNumber = typeof value === "number";
    label.dataset.type = isNumber ? "number" : "string";
    label.innerHTML = `${_prettyLabel(key)}`;
    let input;
    if (typeof value === "boolean") {
      input = document.createElement("select");
      input.innerHTML = `<option value="true">${t("bool.true")}</option><option value="false">${t("bool.false")}</option>`;
      input.value = String(value);
      label.dataset.type = "boolean";
    } else {
      input = document.createElement("input");
      input.type = isNumber ? "number" : "text";
      if (isNumber) input.step = Number.isInteger(value) ? "1" : "any";
      input.value = value;
    }
    input.name = key;
    label.appendChild(input);
    const _hints = FIELD_HINTS();
    if (_hints[key]) {
      const hint = document.createElement("span");
      hint.className = "field-hint";
      hint.textContent = _hints[key];
      label.appendChild(hint);
    }
    return label;
  };

  if (formEl.id === "backbone-form") {
    const byKey = Object.fromEntries(entries);
    const used = new Set();
    const addSection = (sec, keys) => {
      const present = keys.filter((k) => k in byKey);
      if (!present.length) return;
      const h = document.createElement("div");
      h.className = "form-section";
      h.textContent = t("form.sec." + sec);
      formEl.appendChild(h);
      present.forEach((k) => { used.add(k); formEl.appendChild(buildField(k, byKey[k])); });
    };
    BACKBONE_SECTIONS.forEach(([sec, keys]) => addSection(sec, keys));
    addSection("other", entries.map(([k]) => k).filter((k) => !used.has(k)));
    return;
  }
  entries.forEach(([key, value]) => formEl.appendChild(buildField(key, value)));
}

function collectFormData(formEl) {
  const out = {};
  if (!formEl) return out;
  formEl.querySelectorAll("label").forEach((label) => {
    const input = label.querySelector("input,select,textarea");
    if (!input) return;
    const key = input.name;
    if (label.dataset.type === "number") out[key] = parseFloat(input.value);
    else if (label.dataset.type === "boolean") out[key] = input.value === "true";
    else out[key] = input.value;
  });
  return out;
}

function _prettyLabel(key) {
  const pk = "param." + key;
  if (typeof t === "function") {
    const tr = t(pk);
    if (tr && tr !== pk) return tr;
  }
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

async function refreshBotState() {
  const s = await API.botState();
  state.tradingMode = s.trading_mode;
  const btn = document.getElementById("bot-toggle");
  if (!btn) return;
  btn.classList.toggle("running", s.is_running);
  btn.classList.toggle("idle", !s.is_running);
  const label = btn.querySelector(".label");
  if (label) label.textContent = s.is_running ? t("bot.active") : t("bot.inactive");
  const modeSel = document.getElementById("trading-mode-select");
  if (modeSel) modeSel.value = s.trading_mode;
  const exSel = document.getElementById("exchange-select");
  if (exSel && s.exchange) {
    exSel.value = s.exchange;
    state.activeExchange = s.exchange;
  }
  if (s.pause_until) {
    console.warn("Bot rate-limit pause until", s.pause_until);
  }
}

document.getElementById("bot-toggle").addEventListener("click", async () => {
  const running = document.getElementById("bot-toggle").classList.contains("running");
  if (running) await API.botStop(); else await API.botStart();
  refreshBotState();
});

document.getElementById("trading-mode-select").addEventListener("change", async (e) => {
  const mode = e.target.value;
  if (mode === "live") {
    const status = await API.credStatus(state.activeExchange || "nobitex");
    if (!status.configured) {
      alert(t("confirm.live.no.cred", { ex: state.activeExchange || "nobitex" }));
      e.target.value = "paper";
      return;
    }
    if (!confirm(t("confirm.live"))) {
      e.target.value = "paper";
      return;
    }
  }
  await API.botMode(mode);
  refreshBotState();
});

const exchangeSel = document.getElementById("exchange-select");
if (exchangeSel) exchangeSel.addEventListener("change", async (e) => {
  const ex = e.target.value;
  if (!confirm(t("confirm.exchange", { ex }))) {
    e.target.value = state.activeExchange || "nobitex";
    return;
  }
  await API.botExchange(ex);
  state.activeExchange = ex;
  // Refresh credential status for the new exchange
  if (state.activeTab === "settings") refreshSettingsTab();
  refreshBotState();
});

async function refreshDashboard() {
  await loadGroupList();
  await loadPerfTable();
}

async function loadGroupList() {
  const groups = await API.listGroups();
  state.groups = groups;
  renderGroupList();
  if (!state.selectedGroupId && groups.length) {
    selectGroup(groups[0].id, true);
  } else if (state.selectedGroupId) {
    renderGroupList();
  }
}

function renderGroupList() {
  const filterEl = document.getElementById("group-search");
  const filter = filterEl ? filterEl.value.trim().toLowerCase() : "";
  const listEl = document.getElementById("group-list");
  if (!listEl) return;
  listEl.innerHTML = "";
  state.groups
    .filter((g) => !filter || g.name.toLowerCase().includes(filter) || g.symbols.join(",").toLowerCase().includes(filter))
    .forEach((g) => {
      const div = document.createElement("div");
      div.className = "group-item" + (g.id === state.selectedGroupId ? " selected" : "");
      div.innerHTML = `
        <div class="group-item-main">
          <span class="g-name">${g.name}</span>
          <span class="g-symbols">${g.symbols.join(" / ")}</span>
          <span class="g-status ${g.status}">${g.status}</span>
        </div>
      `;
      div.addEventListener("click", () => selectGroup(g.id));
      listEl.appendChild(div);
    });
}

async function selectGroup(groupId, force = true) {
  state.selectedGroupId = groupId;
  renderGroupList();
  const group = state.groups.find((g) => g.id === groupId);
  if (!group) return;

  const plotTitle = document.getElementById("plot-title");
  if (plotTitle) plotTitle.textContent = t("plot.title.group", { name: group.name });
  const plotMeta = document.getElementById("plot-meta");
  if (plotMeta) plotMeta.textContent = t("plot.fitting");

  const backboneCfg = state.backboneCfg || (await API.getConfigSection("backbone"));
  state.backboneCfg = backboneCfg;
  const samplingMs = Math.max(5, Number(backboneCfg.sampling_time) || 60) * 1000;

  const lastAt = state.lastFitAt[groupId] || 0;
  const age = Date.now() - lastAt;
  let fit = state.lastFitData[groupId];

  if (force || !fit || age >= samplingMs) {
    try {
      fit = await API.liveFit(groupId);
      state.lastFitAt[groupId] = Date.now();
      state.lastFitData[groupId] = fit;
    } catch (e) {
      if (plotMeta) plotMeta.textContent = t("plot.fit.failed", { error: e.message || e });
      return;
    }
  }

  state.selectedFit = fit;
  const trades = await API.listTrades(groupId);
  renderTradesTable(trades);
  renderResidualChart("residual-chart", fit, backboneCfg, trades, null);

  try {
    const curve = await API.equityCurve(groupId);
    const points = (curve && curve.points) ? curve.points : [];
    const emptyEl = document.getElementById("equity-empty");
    if (!points.length) {
      if (emptyEl) emptyEl.hidden = false;
    } else {
      if (emptyEl) emptyEl.hidden = true;
      if (typeof renderEquityCurvePoints === "function") {
        renderEquityCurvePoints("equity-canvas", points, {
          startEquity: curve.start_equity,
          accountBalance: curve.account_balance,
          source: curve.source,
        });
      }
    }
  } catch (e) {
    console.warn("equity curve", e);
  }

  const bars = fit.bars_used != null ? fit.bars_used : "?";
  const res = fit.resolution || "?";
  if (plotMeta) {
    plotMeta.innerHTML = t("plot.meta.live", {
      adf: fmtNum(fit.adf_pvalue, 3),
      kpss: fmtNum(fit.kpss_pvalue, 3),
      flag: `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? t("plot.stationary") : t("plot.rejected")}</span>`,
      bars, res, fitted: fmtTime(fit.fitted_at),
    });
  }
}

async function loadPerfTable() {
  const [rows, diag] = await Promise.all([
    API.allPerformance(),
    API.botDiagnostics().catch(() => ({ groups: [] })),
  ]);
  const byId = {};
  ((diag && diag.groups) || []).forEach((d) => { byId[d.group_id] = d; });
  (rows || []).forEach((r) => {
    const d = byId[r.group_id != null ? r.group_id : r.id];
    r.diag = d || null;
    r.diag_stage = d ? d.stage : "";
  });
  renderPerfTable(rows, showGroupDetail);
  wirePerfTableSorting(showGroupDetail);
}

async function showGroupDetail(row) {
  const detail = document.getElementById("group-detail");
  if (!detail) return;
  if (typeof destroyRadarChart === "function") destroyRadarChart();
  detail.innerHTML = `
    <div class="detail-title">${_esc(row.name)}</div>
    <div class="detail-sub">${(row.symbols || []).map(_esc).join(" / ")}</div>
    <div class="detail-metrics">
      ${t("perf.th.trades")}: <b>${row.total_trades ?? 0}</b><br/>
      ${t("perf.th.win_rate")}: <b>${fmtPct(row.win_rate)}</b><br/>
      ${t("perf.th.net_profit")}: <b>${fmtNum(row.total_net_profit)}</b><br/>
      ${t("perf.th.profit_factor")}: <b>${fmtPF(row.profit_factor, row.total_trades)}</b>
    </div>
    <div class="perf-radar-wrap">
      <div class="perf-radar-title">${t("perf.radar.title")}</div>
      <div class="perf-radar-box"><canvas id="perf-radar"></canvas></div>
      <div class="perf-radar-hint">${t("perf.radar.hint")}</div>
    </div>
  `;
  if (typeof renderRadarChart === "function") renderRadarChart("perf-radar", row);
  if (row.group_id) {
    selectGroup(row.group_id, true);
  }
}

function _collectPerfFiltersFromUI() {
  clearPerfFilters();
  const searchEl = document.getElementById("filter-search");
  if (searchEl && searchEl.value.trim()) setPerfFilter("_search", "contains", searchEl.value.trim());
  const statusEl = document.getElementById("filter-status");
  if (statusEl && statusEl.value) setPerfFilter("status", "=", statusEl.value);
  const pnlVal = document.getElementById("filter-pnl-val");
  if (pnlVal && pnlVal.value !== "") {
    setPerfFilter("total_net_profit", document.getElementById("filter-pnl-op").value, pnlVal.value);
  }
  const pfVal = document.getElementById("filter-pf-val");
  if (pfVal && pfVal.value !== "") {
    setPerfFilter("profit_factor", document.getElementById("filter-pf-op").value, pfVal.value);
  }
  const tradesVal = document.getElementById("filter-trades-val");
  if (tradesVal && tradesVal.value !== "") {
    setPerfFilter("total_trades", document.getElementById("filter-trades-op").value, tradesVal.value);
  }
}

// Filters apply as you type / change (no Apply button) and survive the 15 s refresh.
const _PERF_FILTER_IDS = [
  "filter-search", "filter-status", "filter-pnl-op", "filter-pnl-val",
  "filter-pf-op", "filter-pf-val", "filter-trades-op", "filter-trades-val",
];
_PERF_FILTER_IDS.forEach((id) => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
    _collectPerfFiltersFromUI();
    applyPerfFiltersAndRedraw(showGroupDetail);
  });
});
const clearBtn = document.getElementById("perf-filter-clear");
if (clearBtn) clearBtn.addEventListener("click", () => {
  _PERF_FILTER_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el && el.tagName !== "SELECT") el.value = "";
    else if (el && id === "filter-status") el.value = "";
  });
  clearPerfFilters();
  applyPerfFiltersAndRedraw(showGroupDetail);
});

const expandAllBtn = document.getElementById("perf-expand-all");
if (expandAllBtn) expandAllBtn.addEventListener("click", () => togglePerfExpandAll(showGroupDetail));

// -------------------- Initialization tab --------------------
document.querySelectorAll(".method-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".method-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.initMethod = btn.dataset.method;
    _syncManualPanelVisibility();
  });
});

function _syncManualPanelVisibility() {
  const isManual = state.initMethod === "manual";
  const panel = document.getElementById("manual-group-panel");
  const autoActions = document.getElementById("init-auto-actions");
  if (panel) {
    panel.hidden = !isManual;
    panel.style.display = isManual ? "block" : "none";
    panel.classList.toggle("is-open", isManual);
  }
  if (autoActions) autoActions.style.display = isManual ? "none" : "flex";
  if (isManual) loadLiquidSymbols();
}

async function refreshInitTab() {
  const [backbone, init] = await Promise.all([
    API.getConfigSection("backbone"),
    API.getConfigSection("init"),
  ]);
  state.backboneCfg = backbone;
  state.initCfg = init;
  state.initMethod = (init && init.method) || state.initMethod || "random";
  if (state.initMethod === "manual") {
    /* keep manual if user selected it */
  }
  document.querySelectorAll(".method-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.method === state.initMethod)
  );
  _syncManualPanelVisibility();
  renderParamForm(document.getElementById("backbone-form"), backbone);
  renderParamForm(document.getElementById("init-form"), init);
  await loadCandidatesTable();
}

async function loadCandidatesTable() {
  const groups = await API.listGroups();
  renderCandidatesTable(
    groups,
    async (id) => { await API.setGroupStatus(id, "active"); await loadCandidatesTable(); await loadPerfTable(); },
    async (id) => { await API.setGroupStatus(id, "inactive"); await loadCandidatesTable(); await loadPerfTable(); },
    async (id, name) => {
      if (!confirm(t("confirm.delete.group", { name }))) return;
      await API.deleteGroup(id);
      await loadCandidatesTable();
      await loadPerfTable();
    }
  );
}

function showInitProgress(visible) {
  const wrap = document.getElementById("init-progress-wrap");
  if (!wrap) return;
  wrap.hidden = !visible;
  wrap.classList.remove("done", "error");
}

function updateInitProgressUI(p) {
  const wrap = document.getElementById("init-progress-wrap");
  if (!wrap) return;
  wrap.hidden = false;
  wrap.classList.toggle("done", p.phase === "done");
  wrap.classList.toggle("error", p.phase === "error" || !!p.error);

  const phaseEl = document.getElementById("init-progress-phase");
  const pctEl = document.getElementById("init-progress-pct");
  const bar = document.getElementById("init-progress-bar");
  const msg = document.getElementById("init-progress-msg");

  const phaseLabels = {
    idle: t("init.phase.idle"),
    starting: t("init.phase.starting"),
    fetch_symbols: t("init.phase.fetch_symbols"),
    fetch_ohlc: t("init.phase.fetch_ohlc"),
    backtest: t("init.phase.backtest"),
    persist: t("init.phase.persist"),
    done: t("init.phase.done"),
    error: t("init.phase.error"),
  };
  if (phaseEl) phaseEl.textContent = phaseLabels[p.phase] || p.phase || "…";
  const pct = Math.round(Number(p.percent) || 0);
  if (pctEl) pctEl.textContent = pct + "%";
  if (bar) bar.style.width = pct + "%";
  if (msg) {
    let line = p.message || "";
    if (p.total && p.current != null && p.phase === "backtest") {
      line = p.message || t("init.backtest.progress", { current: p.current, total: p.total });
    }
    if (p.error) line = p.error;
    msg.textContent = line;
  }
}

function setInitStatus(text) {
  const el = document.getElementById("init-status");
  if (el) el.textContent = text || "";
}

async function saveInitConfig() {
  const backboneData = collectFormData(document.getElementById("backbone-form"));
  const initData = collectFormData(document.getElementById("init-form"));
  initData.method = state.initMethod === "manual" ? "random" : state.initMethod;
  await API.updateConfigSection("backbone", backboneData);
  await API.updateConfigSection("init", initData);
  state.backboneCfg = backboneData;
}

const saveCfg = document.getElementById("save-config-btn");
if (saveCfg) saveCfg.addEventListener("click", async () => {
  try {
    await saveInitConfig();
    setInitStatus(t("init.saved"));
  } catch (e) {
    setInitStatus(t("init.error", { error: e.message || e }));
  }
});

const runInit = document.getElementById("run-init-btn");
if (runInit) runInit.addEventListener("click", async () => {
  if (state.initMethod === "manual") {
    setInitStatus(t("init.use.manual"));
    return;
  }
  runInit.disabled = true;
  // The run uses the SAVED config on the server, so save what is on screen first
  // (previously edited-but-unsaved values were silently ignored).
  try {
    await saveInitConfig();
  } catch (e) {
    runInit.disabled = false;
    setInitStatus(t("init.error", { error: e.message || e }));
    return;
  }
  showInitProgress(true);
  setInitStatus("");
  updateInitProgressUI({ phase: "starting", message: t("init.starting"), percent: 0, running: true });

  try {
    await API.runInit(state.initMethod || "random", true);
  } catch (e) {
    runInit.disabled = false;
    updateInitProgressUI({ phase: "error", message: e.message, percent: 0, running: false, error: e.message });
    setInitStatus(t("init.error", { error: e.message }));
    return;
  }

  const poll = async () => {
    try {
      const p = await API.initProgress();
      updateInitProgressUI(p);
      if (p.running) {
        setTimeout(poll, 400);
        return;
      }
      runInit.disabled = false;
      if (p.phase === "error" || p.error) {
        setInitStatus(t("init.error", { error: p.error || p.message || "unknown" }));
        return;
      }
      const r = p.result || {};
      setInitStatus(t("init.done", {
        method: r.method || state.initMethod,
        symbols: r.liquid_symbols_considered ?? "?",
        candidates: r.candidates_evaluated ?? "?",
        groups: (r.groups_created || []).length,
      }));
      await loadCandidatesTable();
    } catch (e) {
      runInit.disabled = false;
      setInitStatus(t("init.error.poll", { error: e.message }));
      updateInitProgressUI({ phase: "error", message: e.message, percent: 0, running: false, error: e.message });
    }
  };
  setTimeout(poll, 300);
});

// -------------------- manual group entry --------------------
async function loadLiquidSymbols() {
  const box = document.getElementById("symbol-checklist");
  if (box) box.innerHTML = "<div class=\"sym-loading\">" + t("init.loading.symbols") + "</div>";
  try {
    const data = await API.listLiquidSymbols(null, state.activeExchange || "nobitex");
    // API returns { symbols: [...], quote, top_n } — not a bare array
    const raw = data && data.symbols != null ? data.symbols : data;
    state.liquidSymbols = Array.isArray(raw)
      ? raw.map((s) => (typeof s === "string" ? s : (s && (s.symbol || s.name)) || "")).filter(Boolean)
      : [];
    if (!state.liquidSymbols.length) {
      setInitStatus(t("init.no.symbols"));
    } else {
      setInitStatus(t("init.loaded.symbols", { n: state.liquidSymbols.length }));
    }
    renderSymbolChecklist();
  } catch (e) {
    console.warn("loadLiquidSymbols", e);
    if (box) box.innerHTML = "<div class=\"sym-loading\">" + t("init.fail.symbols") + "</div>";
    setInitStatus(t("init.could.not.symbols", { error: e.message || e }));
  }
}

function renderSymbolChecklist() {
  const searchEl = document.getElementById("symbol-search");
  const filter = (searchEl && searchEl.value || "").trim().toLowerCase();
  const box = document.getElementById("symbol-checklist");
  if (!box) return;
  box.innerHTML = "";
  const list = state.liquidSymbols.filter((s) => !filter || String(s).toLowerCase().includes(filter));
  if (!list.length) {
    box.innerHTML = "<div class=\"sym-loading\">" + (state.liquidSymbols.length ? "No match for filter." : "No symbols available.") + "</div>";
    _refreshSelectedSymbolsUI();
    return;
  }
  list.forEach((sym) => {
    const label = document.createElement("label");
    label.className = "symbol-check-item sym-check";
    const checked = state.selectedSymbols.has(sym);
    label.innerHTML = `<input type="checkbox" value="${sym}" ${checked ? "checked" : ""} /> <span>${sym}</span>`;
    label.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) state.selectedSymbols.add(sym);
      else state.selectedSymbols.delete(sym);
      _refreshSelectedSymbolsUI();
    });
    box.appendChild(label);
  });
  _refreshSelectedSymbolsUI();
}

function _refreshSelectedSymbolsUI() {
  const arr = [...state.selectedSymbols];
  const lab = document.getElementById("selected-symbols-label");
  if (lab) lab.textContent = arr.length ? arr.join(", ") : "none";

  const dep = document.getElementById("manual-dependent");
  if (!dep) return;
  const prev = dep.value;
  dep.innerHTML = arr.map((s) => `<option value="${s}">${s}</option>`).join("");
  if (arr.includes(prev)) dep.value = prev;
  else if (arr.length) dep.value = arr[0];
}

const symbolSearch = document.getElementById("symbol-search");
if (symbolSearch) {
  symbolSearch.addEventListener("input", renderSymbolChecklist);
}

const createManualBtn = document.getElementById("create-manual-group-btn");
if (createManualBtn) {
  createManualBtn.addEventListener("click", async () => {
    const symbols = [...state.selectedSymbols];
    if (symbols.length < 2) {
      setInitStatus("Select at least 2 symbols.");
      return;
    }
    const nameEl = document.getElementById("manual-group-name");
    const name = ((nameEl && nameEl.value) || "").trim()
      || `manual-${symbols.join("-")}`.slice(0, 80);
    const depEl = document.getElementById("manual-dependent");
    const dependent = (depEl && depEl.value) || symbols[0];
    if (!dependent || !symbols.includes(dependent)) {
      setInitStatus("Dependent symbol must be one of the selected symbols.");
      return;
    }
    try {
      const g = await API.createManualGroup({
        name,
        symbols,
        dependent_symbol: dependent,
        activate: true,
        exchange: state.activeExchange || "nobitex",
      });
      setInitStatus(`Created manual group #${g.id}: ${g.name} (${(g.symbols || symbols).join(", ")})`);
      state.selectedSymbols.clear();
      if (nameEl) nameEl.value = "";
      renderSymbolChecklist();
      await loadCandidatesTable();
      await loadPerfTable();
    } catch (e) {
      setInitStatus("Error creating group: " + (e.message || e));
    }
  });
}

function _syncCredFieldsForExchange() {
  const exSel = document.getElementById("cred-exchange-select");
  const authSel = document.getElementById("auth-method-select");
  const ex = (exSel && exSel.value) || state.activeExchange || "nobitex";
  if (!authSel) return;
  // Populate auth methods per exchange
  if (ex === "xt") {
    authSel.innerHTML = '<option value="key_secret">API Key + Secret (HMAC)</option>';
    authSel.value = "key_secret";
  } else {
    authSel.innerHTML =
      '<option value="token">API Token</option>' +
      '<option value="key_signature">API Key + Ed25519 Secret</option>';
  }
  const method = authSel.value;
  const t = document.getElementById("cred-fields-token");
  const k = document.getElementById("cred-fields-key");
  const hint = document.getElementById("cred-secret-hint");
  if (t) t.style.display = method === "token" ? "" : "none";
  if (k) k.style.display = method === "token" ? "none" : "";
  if (hint) {
    if (method === "key_secret") {
      hint.textContent = "XT HMAC secret (plain string, not PEM)";
    } else if (method === "key_signature") {
      hint.textContent = "Nobitex secretKey base64 or PEM Ed25519";
    }
  }
}

async function refreshSettingsTab() {
  const exSel = document.getElementById("cred-exchange-select");
  const ex = (exSel && exSel.value) || state.activeExchange || "nobitex";
  if (exSel) exSel.value = ex;
  _syncCredFieldsForExchange();
  try {
    const status = await API.credStatus(ex);
    const el = document.getElementById("cred-status");
    if (el) {
      el.textContent = status.configured
        ? t("settings.cred.ok", { ex, method: status.auth_method || "?" })
        : t("settings.cred.none", { ex });
    }
  } catch (e) {}
  try {
    const sys = await API.getConfigSection("system");
    state.systemCfg = sys;
    const form = document.getElementById("system-form");
    if (form) renderParamForm(form, sys);
  } catch (e) {}
}

const credExSel = document.getElementById("cred-exchange-select");
if (credExSel) credExSel.addEventListener("change", () => refreshSettingsTab());

const credForm = document.getElementById("cred-form");
if (credForm) credForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(credForm);
  const payload = Object.fromEntries(fd.entries());
  const authSel = document.getElementById("auth-method-select");
  const exSel = document.getElementById("cred-exchange-select");
  if (authSel) payload.auth_method = authSel.value;
  if (exSel) payload.exchange = exSel.value;
  await API.saveCred(payload);
  refreshSettingsTab();
  alert(t("settings.cred.saved", { ex: payload.exchange || "nobitex" }));
});

const delCred = document.getElementById("delete-cred-btn");
if (delCred) delCred.addEventListener("click", async () => {
  const exSel = document.getElementById("cred-exchange-select");
  const ex = (exSel && exSel.value) || "nobitex";
  if (!confirm(t("settings.cred.remove.confirm", { ex }))) return;
  await API.deleteCred(ex);
  refreshSettingsTab();
});

const authSel = document.getElementById("auth-method-select");
if (authSel) authSel.addEventListener("change", () => _syncCredFieldsForExchange());

const saveSys = document.getElementById("save-system-btn");
if (saveSys) saveSys.addEventListener("click", async () => {
  const data = collectFormData(document.getElementById("system-form"));
  await API.updateConfigSection("system", data);
  alert(t("settings.system.saved"));
});

window.__onLanguageChange = async function () {
  try {
    if (typeof i18n !== "undefined") i18n.applyStatic();
    await refreshBotState();
    if (state.activeTab === "dashboard") await refreshDashboard();
    if (state.activeTab === "init") await refreshInitTab();
    if (state.activeTab === "settings") await refreshSettingsTab();
  } catch (e) { console.warn("lang change", e); }
};

(async function boot() {
  await refreshBotState();
  await refreshDashboard();
  startFitPoll();
  state.botPollTimer = setInterval(() => {
    refreshBotState();
    if (state.activeTab === "dashboard") refreshDashboard();
  }, 15000);
})();
