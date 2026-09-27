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

const FIELD_HINTS = {
  target_profit_rate: "0.01 = 1% of total position notional, net of fees; 0 disables",
  max_entry_scale: "Cap on leg_orders inflation to meet exchange min; large values mean the natural position was too small (thin book). 0 disables.",
};

function renderParamForm(formEl, data) {
  if (!formEl) return;
  formEl.innerHTML = "";
  Object.entries(data || {}).forEach(([key, value]) => {
    const label = document.createElement("label");
    const isNumber = typeof value === "number";
    label.dataset.type = isNumber ? "number" : "string";
    label.innerHTML = `${_prettyLabel(key)}`;
    let input;
    if (typeof value === "boolean") {
      input = document.createElement("select");
      input.innerHTML = `<option value="true">true</option><option value="false">false</option>`;
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
    if (FIELD_HINTS[key]) {
      const hint = document.createElement("span");
      hint.className = "field-hint";
      hint.textContent = FIELD_HINTS[key];
      label.appendChild(hint);
    }
    formEl.appendChild(label);
  });
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
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

async function refreshBotState() {
  const s = await API.botState();
  const btn = document.getElementById("bot-toggle");
  if (!btn) return;
  btn.classList.toggle("running", s.is_running);
  btn.classList.toggle("idle", !s.is_running);
  const label = btn.querySelector(".label");
  if (label) label.textContent = s.is_running ? "ACTIVE" : "INACTIVE";
  const modeSel = document.getElementById("trading-mode-select");
  if (modeSel) modeSel.value = s.trading_mode;
}

document.getElementById("bot-toggle").addEventListener("click", async () => {
  const running = document.getElementById("bot-toggle").classList.contains("running");
  if (running) await API.botStop(); else await API.botStart();
  refreshBotState();
});

document.getElementById("trading-mode-select").addEventListener("change", async (e) => {
  const mode = e.target.value;
  if (mode === "live") {
    const status = await API.credStatus();
    if (!status.configured) {
      alert("No live credentials saved yet. Add them under Settings & Credentials first.");
      e.target.value = "paper";
      return;
    }
    if (!confirm("Switching to LIVE mode will place real orders on your exchange account. Continue?")) {
      e.target.value = "paper";
      return;
    }
  }
  await API.botMode(mode);
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
  if (plotTitle) plotTitle.textContent = `Residual / Z-Score — ${group.name}`;
  const plotMeta = document.getElementById("plot-meta");
  if (plotMeta) plotMeta.textContent = "Fitting OLS on the latest window…";

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
      if (plotMeta) plotMeta.textContent = "Could not fit residual: " + (e.message || e);
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
    plotMeta.innerHTML =
      `Live window OLS · ADF p=${fmtNum(fit.adf_pvalue, 3)} · KPSS p=${fmtNum(fit.kpss_pvalue, 3)} · ` +
      `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? "STATIONARY" : "REJECTED"}</span> · ` +
      `${bars} bars @ ${res} · fitted ${fmtTime(fit.fitted_at)}`;
  }
}

async function loadPerfTable() {
  const rows = await API.allPerformance();
  renderPerfTable(rows, showGroupDetail);
  wirePerfTableSorting(showGroupDetail);
}

async function showGroupDetail(row) {
  const detail = document.getElementById("group-detail");
  if (!detail) return;
  detail.innerHTML = `
    <div class="detail-title">${row.name}</div>
    <div class="detail-sub">${(row.symbols || []).join(" / ")}</div>
    <div class="detail-metrics">
      Trades: <b>${row.trade_count}</b><br/>
      Win rate: <b>${fmtPct(row.win_rate)}</b><br/>
      Total PnL: <b>${fmtNum(row.total_pnl)}</b><br/>
      Sharpe: <b>${fmtNum(row.sharpe_ratio, 2)}</b>
    </div>
  `;
  if (row.group_id) {
    selectGroup(row.group_id, true);
  }
}

function _collectPerfFiltersFromUI() {
  clearPerfFilters();
  const statusEl = document.getElementById("filter-status");
  if (statusEl && statusEl.value) setPerfFilter("status", "=", statusEl.value);
  const pnlVal = document.getElementById("filter-pnl-val");
  if (pnlVal && pnlVal.value !== "") {
    setPerfFilter("total_pnl", document.getElementById("filter-pnl-op").value, pnlVal.value);
  }
  const sharpeVal = document.getElementById("filter-sharpe-val");
  if (sharpeVal && sharpeVal.value !== "") {
    setPerfFilter("sharpe_ratio", document.getElementById("filter-sharpe-op").value, sharpeVal.value);
  }
  const tradesVal = document.getElementById("filter-trades-val");
  if (tradesVal && tradesVal.value !== "") {
    setPerfFilter("trade_count", document.getElementById("filter-trades-op").value, tradesVal.value);
  }
}

const applyBtn = document.getElementById("perf-filter-apply");
if (applyBtn) applyBtn.addEventListener("click", () => {
  _collectPerfFiltersFromUI();
  applyPerfFiltersAndRedraw(showGroupDetail);
});
const clearBtn = document.getElementById("perf-filter-clear");
if (clearBtn) clearBtn.addEventListener("click", () => {
  ["filter-status", "filter-pnl-val", "filter-sharpe-val", "filter-trades-val"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
  clearPerfFilters();
  applyPerfFiltersAndRedraw(showGroupDetail);
});

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
      if (!confirm("Delete " + name + "?")) return;
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
    idle: "Idle",
    starting: "Starting",
    fetch_symbols: "Markets",
    fetch_ohlc: "Downloading",
    backtest: "Backtesting",
    persist: "Saving",
    done: "Done",
    error: "Error",
  };
  if (phaseEl) phaseEl.textContent = phaseLabels[p.phase] || p.phase || "…";
  const pct = Math.round(Number(p.percent) || 0);
  if (pctEl) pctEl.textContent = pct + "%";
  if (bar) bar.style.width = pct + "%";
  if (msg) {
    let line = p.message || "";
    if (p.total && p.current != null && p.phase === "backtest") {
      line = p.message || `Backtesting ${p.current}/${p.total}`;
    }
    if (p.error) line = p.error;
    msg.textContent = line;
  }
}

function setInitStatus(text) {
  const el = document.getElementById("init-status");
  if (el) el.textContent = text || "";
}

const saveCfg = document.getElementById("save-config-btn");
if (saveCfg) saveCfg.addEventListener("click", async () => {
  const backboneData = collectFormData(document.getElementById("backbone-form"));
  const initData = collectFormData(document.getElementById("init-form"));
  initData.method = state.initMethod === "manual" ? "random" : state.initMethod;
  await API.updateConfigSection("backbone", backboneData);
  await API.updateConfigSection("init", initData);
  state.backboneCfg = backboneData;
  setInitStatus("Parameters saved.");
});

const runInit = document.getElementById("run-init-btn");
if (runInit) runInit.addEventListener("click", async () => {
  if (state.initMethod === "manual") {
    setInitStatus("Use 'Create Manual Group' for the manual method.");
    return;
  }
  runInit.disabled = true;
  showInitProgress(true);
  setInitStatus("");
  updateInitProgressUI({ phase: "starting", message: "Starting…", percent: 0, running: true });

  try {
    await API.runInit(state.initMethod || "random", true);
  } catch (e) {
    runInit.disabled = false;
    updateInitProgressUI({ phase: "error", message: e.message, percent: 0, running: false, error: e.message });
    setInitStatus("Error: " + e.message);
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
        setInitStatus("Error: " + (p.error || p.message || "unknown"));
        return;
      }
      const r = p.result || {};
      setInitStatus(
        `Done. Method=${r.method || state.initMethod} · liquid symbols=${r.liquid_symbols_considered ?? "?"} · ` +
        `candidates kept=${r.candidates_evaluated ?? "?"} · groups created=${(r.groups_created || []).length}`
      );
      await loadCandidatesTable();
    } catch (e) {
      runInit.disabled = false;
      setInitStatus("Error polling progress: " + e.message);
      updateInitProgressUI({ phase: "error", message: e.message, percent: 0, running: false, error: e.message });
    }
  };
  setTimeout(poll, 300);
});

// -------------------- manual group entry --------------------
async function loadLiquidSymbols() {
  const box = document.getElementById("symbol-checklist");
  if (box) box.innerHTML = "<div class=\"sym-loading\">Loading liquid symbols…</div>";
  try {
    const data = await API.listLiquidSymbols();
    // API returns { symbols: [...], quote, top_n } — not a bare array
    const raw = data && data.symbols != null ? data.symbols : data;
    state.liquidSymbols = Array.isArray(raw)
      ? raw.map((s) => (typeof s === "string" ? s : (s && (s.symbol || s.name)) || "")).filter(Boolean)
      : [];
    if (!state.liquidSymbols.length) {
      setInitStatus("No liquid symbols returned. Check exchange connectivity / quote_currency in init config.");
    } else {
      setInitStatus(`Loaded ${state.liquidSymbols.length} liquid symbols.`);
    }
    renderSymbolChecklist();
  } catch (e) {
    console.warn("loadLiquidSymbols", e);
    if (box) box.innerHTML = "<div class=\"sym-loading\">Failed to load symbols.</div>";
    setInitStatus("Could not load symbols: " + (e.message || e));
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

async function refreshSettingsTab() {
  try {
    const status = await API.credStatus();
    const el = document.getElementById("cred-status");
    if (el) el.textContent = status.configured ? "Credentials configured." : "No credentials saved.";
  } catch (e) {}
  try {
    const sys = await API.getConfigSection("system");
    state.systemCfg = sys;
    const form = document.getElementById("system-form");
    if (form) renderParamForm(form, sys);
  } catch (e) {}
}

const credForm = document.getElementById("cred-form");
if (credForm) credForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(credForm);
  const payload = Object.fromEntries(fd.entries());
  const authSel = document.getElementById("auth-method-select");
  if (authSel) payload.auth_method = authSel.value;
  await API.saveCred(payload);
  refreshSettingsTab();
  alert("Credentials saved.");
});

const delCred = document.getElementById("delete-cred-btn");
if (delCred) delCred.addEventListener("click", async () => {
  if (!confirm("Remove credentials?")) return;
  await API.deleteCred();
  refreshSettingsTab();
});

const authSel = document.getElementById("auth-method-select");
if (authSel) authSel.addEventListener("change", () => {
  const isKey = authSel.value === "key_signature";
  const t = document.getElementById("cred-fields-token");
  const k = document.getElementById("cred-fields-key");
  if (t) t.style.display = isKey ? "none" : "";
  if (k) k.style.display = isKey ? "" : "none";
});

const saveSys = document.getElementById("save-system-btn");
if (saveSys) saveSys.addEventListener("click", async () => {
  const data = collectFormData(document.getElementById("system-form"));
  await API.updateConfigSection("system", data);
  alert("System saved.");
});

(async function boot() {
  await refreshBotState();
  await refreshDashboard();
  startFitPoll();
  state.botPollTimer = setInterval(() => {
    refreshBotState();
    if (state.activeTab === "dashboard") refreshDashboard();
  }, 15000);
})();
