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
  lastFitAt: {},       // groupId -> timestamp ms of last live-fit
  lastFitData: {},     // groupId -> fit payload (cache)
  liquidSymbols: [],   // for manual entry
  selectedSymbols: new Set(),
};

// ------------------------------------------------------------------ tabs
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
  // Refresh residual only at the configured sampling interval (not every few seconds).
  const tick = async () => {
    if (state.activeTab !== "dashboard" || !state.selectedGroupId) return;
    const st = (state.backboneCfg && state.backboneCfg.sampling_time) || 60;
    // force=false → respects cache / sampling_time gate inside selectGroup
    await selectGroup(state.selectedGroupId, false);
  };
  // Check every 5s whether sampling_time has elapsed; actual network only when due.
  state.fitPollTimer = setInterval(tick, 5000);
}
function stopFitPoll() {
  if (state.fitPollTimer) {
    clearInterval(state.fitPollTimer);
    state.fitPollTimer = null;
  }
}

// --------------------------------------------------------------- generic form
function renderParamForm(formEl, data) {
  formEl.innerHTML = "";
  Object.entries(data).forEach(([key, value]) => {
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
    formEl.appendChild(label);
  });
}

function collectFormData(formEl) {
  const out = {};
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

// ------------------------------------------------------------------ bot control
async function refreshBotState() {
  const s = await API.botState();
  const btn = document.getElementById("bot-toggle");
  btn.classList.toggle("running", s.is_running);
  btn.classList.toggle("idle", !s.is_running);
  btn.querySelector(".label").textContent = s.is_running ? "ACTIVE" : "INACTIVE";
  document.getElementById("trading-mode-select").value = s.trading_mode;
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

// ------------------------------------------------------------------ dashboard
async function refreshDashboard() {
  await loadGroupList();
  await loadPerfTable();
}

async function loadGroupList() {
  const groups = await API.listGroups();
  state.groups = groups;
  renderGroupList();
  if (!state.selectedGroupId && groups.length) {
    // First selection: force a live fit
    selectGroup(groups[0].id, true);
  } else if (state.selectedGroupId) {
    // List refresh only — re-render selection highlight; plot updates
    // on its own cadence (sampling_time), not on every list poll.
    renderGroupList();
  }
}

function renderGroupList() {
  const filter = document.getElementById("group-search").value.trim().toLowerCase();
  const listEl = document.getElementById("group-list");
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
        <button class="btn-danger group-delete-btn" title="Delete group permanently" data-id="${g.id}">×</button>
      `;
      div.querySelector(".group-item-main").addEventListener("click", () => selectGroup(g.id));
      div.querySelector(".group-delete-btn").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`Permanently delete group "${g.name}"?`)) return;
        await API.deleteGroup(g.id);
        if (state.selectedGroupId === g.id) {
          state.selectedGroupId = null;
          document.getElementById("plot-title").textContent = "Residual / Z-Score";
          document.getElementById("plot-meta").textContent = "";
          if (typeof residualChart !== "undefined" && residualChart) {
            residualChart.destroy();
            residualChart = null;
          }
        }
        await loadGroupList();
        await loadPerfTable();
      });
      listEl.appendChild(div);
    });
}
document.getElementById("group-search").addEventListener("input", renderGroupList);

async function selectGroup(groupId, force = true) {
  state.selectedGroupId = groupId;
  renderGroupList();
  const group = state.groups.find((g) => g.id === groupId);
  if (!group) return;

  document.getElementById("plot-title").textContent = `Residual / Z-Score — ${group.name}`;
  document.getElementById("plot-meta").textContent = "Fitting OLS on the latest window…";

  const backboneCfg = state.backboneCfg || (await API.getConfigSection("backbone"));
  state.backboneCfg = backboneCfg;
  const samplingMs = Math.max(5, Number(backboneCfg.sampling_time) || 60) * 1000;

  // Only hit the network when forced (user click) or when the sampling interval elapsed.
  const lastAt = state.lastFitAt[groupId] || 0;
  const age = Date.now() - lastAt;
  let fit = state.lastFitData[groupId];

  if (force || !fit || age >= samplingMs) {
    try {
      fit = await API.liveFit(groupId);
      state.lastFitAt[groupId] = Date.now();
      state.lastFitData[groupId] = fit;
    } catch (e) {
      if (typeof residualChart !== "undefined" && residualChart) {
        residualChart.destroy();
        residualChart = null;
      }
      document.getElementById("plot-meta").textContent =
        "Could not fit residual: " + (e.message || e) +
        " — check network / symbol OHLC availability for this group.";
      const tbody = document.querySelector("#trades-table tbody");
      if (tbody) tbody.innerHTML = "";
      return;
    }
  }

  state.selectedFit = fit;

  const trades = await API.listTrades(groupId);
  renderTradesTable(trades);

  renderResidualChart("residual-chart", fit, backboneCfg, trades, (trade) => {
    _showFitForTrade(groupId, trade, trades);
  });

  const bars = fit.bars_used != null ? fit.bars_used : "?";
  const res = fit.resolution || "?";
  document.getElementById("plot-meta").innerHTML =
    `Live window OLS · ADF p=${fmtNum(fit.adf_pvalue, 3)} · KPSS p=${fmtNum(fit.kpss_pvalue, 3)} · ` +
    `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? "STATIONARY" : "REJECTED"}</span> · ` +
    `${bars} bars @ ${res} · fitted ${fmtTime(fit.fitted_at)}`;
}

async function _showFitForTrade(groupId, trade, trades) {
  const fits = await API.listFits(groupId);
  const fit = fits.find((f) => f.id === trade.ols_fit_id);
  if (!fit) return;
  state.selectedFit = fit;
  renderResidualChart("residual-chart", fit, state.backboneCfg, trades, (t) => _showFitForTrade(groupId, t, trades));
  document.getElementById("plot-meta").innerHTML =
    `Showing OLS fitted at entry of trade #${trade.id} (${fmtTime(fit.fitted_at)}) · ` +
    `ADF p=${fmtNum(fit.adf_pvalue, 3)} · KPSS p=${fmtNum(fit.kpss_pvalue, 3)}`;
}

async function loadPerfTable() {
  const rows = await API.allPerformance();
  renderPerfTable(rows, showGroupDetail);
  wirePerfTableSorting(showGroupDetail);
}

async function showGroupDetail(row) {
  const detail = document.getElementById("group-detail");
  const liveNote = (row.trade_count || 0) === 0
    ? `<div class="detail-sub" style="color:var(--text-lo)">No closed live/paper trades yet — metrics are zero until the bot trades.</div>`
    : `<div class="detail-sub">Live / paper trades only (not backtest).</div>`;
  detail.innerHTML = `
    <div class="detail-title">${row.name}</div>
    <div class="detail-sub">${(row.symbols || []).join(" / ")} · ${row.source || ""}${row.sector ? " · " + row.sector : ""}</div>
    ${liveNote}
    <div class="radar-wrap"><canvas id="radar-canvas"></canvas></div>
    <div class="equity-section">
      <div class="equity-title">Equity curve</div>
      <div class="equity-wrap"><canvas id="equity-canvas"></canvas></div>
      <div class="equity-empty" id="equity-empty" hidden>No closed trades yet.</div>
    </div>
    <div class="detail-metrics">
      Trades: <b>${row.trade_count}</b><br/>
      Win rate: <b>${fmtPct(row.win_rate)}</b><br/>
      Total PnL: <b>${fmtNum(row.total_pnl)}</b><br/>
      Avg PnL: <b>${fmtNum(row.avg_pnl)}</b><br/>
      Max drawdown: <b>${fmtNum(row.max_drawdown)}</b><br/>
      Sharpe: <b>${fmtNum(row.sharpe_ratio, 2)}</b> &nbsp; Sortino: <b>${fmtNum(row.sortino_ratio, 2)}</b><br/>
      Profit factor: <b>${fmtNum(row.profit_factor, 2)}</b><br/>
      Avg holding time: <b>${fmtNum(row.avg_holding_hours, 1)} h</b><br/>
      Best / Worst trade: <b>${fmtNum(row.best_trade)}</b> / <b>${fmtNum(row.worst_trade)}</b>
    </div>
  `;
  requestAnimationFrame(() => renderRadarChart("radar-canvas", row));
  if (row.group_id) {
    selectGroup(row.group_id, true);
    // Load trades for equity chart
    try {
      const trades = await API.listTrades(row.group_id);
      const closed = (trades || []).filter((t) => t.status === "closed" && t.pnl != null);
      const emptyEl = document.getElementById("equity-empty");
      if (!closed.length) {
        if (emptyEl) emptyEl.hidden = false;
      } else {
        if (emptyEl) emptyEl.hidden = true;
        requestAnimationFrame(() => renderEquityChart("equity-canvas", trades));
      }
    } catch (e) {
      console.warn("equity chart load failed", e);
    }
  }
}

// -------------------- performance table filters --------------------
function _collectPerfFiltersFromUI() {
  clearPerfFilters();
  const status = document.getElementById("filter-status").value;
  if (status) setPerfFilter("status", "=", status);

  const pnlVal = document.getElementById("filter-pnl-val").value;
  if (pnlVal !== "") {
    setPerfFilter("total_pnl", document.getElementById("filter-pnl-op").value, pnlVal);
  }
  const sharpeVal = document.getElementById("filter-sharpe-val").value;
  if (sharpeVal !== "") {
    setPerfFilter("sharpe_ratio", document.getElementById("filter-sharpe-op").value, sharpeVal);
  }
  const tradesVal = document.getElementById("filter-trades-val").value;
  if (tradesVal !== "") {
    setPerfFilter("trade_count", document.getElementById("filter-trades-op").value, tradesVal);
  }
}

document.getElementById("perf-filter-apply").addEventListener("click", () => {
  _collectPerfFiltersFromUI();
  applyPerfFiltersAndRedraw(showGroupDetail);
});
document.getElementById("perf-filter-clear").addEventListener("click", () => {
  document.getElementById("filter-status").value = "";
  document.getElementById("filter-pnl-val").value = "";
  document.getElementById("filter-sharpe-val").value = "";
  document.getElementById("filter-trades-val").value = "";
  clearPerfFilters();
  applyPerfFiltersAndRedraw(showGroupDetail);
});

// -------------------------------------------------------------------- init tab
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
  if (panel) panel.hidden = !isManual;
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
  state.initMethod = init.method || "random";
  if (state.initMethod === "manual") state.initMethod = "random"; // config may not store manual
  document.querySelectorAll(".method-btn").forEach((b) => b.classList.toggle("active", b.dataset.method === state.initMethod));
  _syncManualPanelVisibility();

  renderParamForm(document.getElementById("backbone-form"), backbone);
  renderParamForm(document.getElementById("init-form"), init);

  await loadCandidatesTable();
}

document.getElementById("save-config-btn").addEventListener("click", async () => {
  const backboneData = collectFormData(document.getElementById("backbone-form"));
  const initData = collectFormData(document.getElementById("init-form"));
  initData.method = state.initMethod === "manual" ? "random" : state.initMethod;
  await API.updateConfigSection("backbone", backboneData);
  await API.updateConfigSection("init", initData);
  setInitStatus("Parameters saved.");
});

document.getElementById("run-init-btn").addEventListener("click", async () => {
  if (state.initMethod === "manual") {
    setInitStatus("Use 'Create Manual Group' for the manual method.");
    return;
  }
  const btn = document.getElementById("run-init-btn");
  btn.disabled = true;
  showInitProgress(true);
  setInitStatus("");
  updateInitProgressUI({ phase: "starting", message: "Starting…", percent: 0, running: true });

  try {
    await API.runInit(state.initMethod, true);
  } catch (e) {
    btn.disabled = false;
    updateInitProgressUI({ phase: "error", message: e.message, percent: 0, running: false, error: e.message });
    setInitStatus("Error: " + e.message);
    return;
  }

  // Poll until the background job finishes.
  const poll = async () => {
    try {
      const p = await API.initProgress();
      updateInitProgressUI(p);
      if (p.running) {
        setTimeout(poll, 400);
        return;
      }
      btn.disabled = false;
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
      btn.disabled = false;
      setInitStatus("Error polling progress: " + e.message);
      updateInitProgressUI({ phase: "error", message: e.message, percent: 0, running: false });
    }
  };
  setTimeout(poll, 300);
});

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
    msg.textContent = line;
  }
}

function setInitStatus(msg) {
  document.getElementById("init-status").textContent = msg;
}

async function loadCandidatesTable() {
  const groups = await API.listGroups();
  renderCandidatesTable(
    groups,
    async (id) => { await API.setGroupStatus(id, "active"); loadCandidatesTable(); },
    async (id) => { await API.setGroupStatus(id, "inactive"); loadCandidatesTable(); },
    async (id, name) => {
      if (!confirm(`Permanently delete group "${name}"? This removes fits and trades too.`)) return;
      await API.deleteGroup(id);
      if (state.selectedGroupId === id) state.selectedGroupId = null;
      await loadCandidatesTable();
      if (state.activeTab === "dashboard") refreshDashboard();
    },
  );
}

// -------------------- manual group entry --------------------
async function loadLiquidSymbols() {
  try {
    const data = await API.listLiquidSymbols();
    state.liquidSymbols = data.symbols || data || [];
    renderSymbolChecklist();
  } catch (e) {
    setInitStatus("Could not load symbols: " + e.message);
  }
}

function renderSymbolChecklist() {
  const filter = (document.getElementById("symbol-search").value || "").trim().toLowerCase();
  const box = document.getElementById("symbol-checklist");
  if (!box) return;
  box.innerHTML = "";
  const list = state.liquidSymbols.filter((s) => !filter || s.toLowerCase().includes(filter));
  list.forEach((sym) => {
    const id = "sym-" + sym;
    const label = document.createElement("label");
    label.className = "symbol-check-item";
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
  const label = document.getElementById("selected-symbols-label");
  if (label) label.textContent = arr.length ? arr.join(", ") : "none";

  const dep = document.getElementById("manual-dependent");
  if (!dep) return;
  const prev = dep.value;
  dep.innerHTML = arr.map((s) => `<option value="${s}">${s}</option>`).join("");
  if (arr.includes(prev)) dep.value = prev;
  else if (arr.length) dep.value = arr[0];
}

document.getElementById("symbol-search").addEventListener("input", renderSymbolChecklist);

document.getElementById("create-manual-group-btn").addEventListener("click", async () => {
  const symbols = [...state.selectedSymbols];
  if (symbols.length < 2) {
    setInitStatus("Select at least 2 symbols.");
    return;
  }
  const name = (document.getElementById("manual-group-name").value || "").trim()
    || `manual-${symbols.join("-")}`.slice(0, 80);
  const dependent = document.getElementById("manual-dependent").value || symbols[0];
  try {
    const g = await API.createManualGroup({ name, symbols, dependent_symbol: dependent, activate: true });
    setInitStatus(`Created manual group #${g.id}: ${g.name} (${g.symbols.join(", ")})`);
    state.selectedSymbols.clear();
    document.getElementById("manual-group-name").value = "";
    renderSymbolChecklist();
    await loadCandidatesTable();
  } catch (e) {
    setInitStatus("Error creating group: " + e.message);
  }
});

// ---------------------------------------------------------------- settings tab
async function refreshSettingsTab() {
  const system = await API.getConfigSection("system");
  state.systemCfg = system;
  renderParamForm(document.getElementById("system-form"), system);

  const status = await API.credStatus();
  const el = document.getElementById("cred-status");
  el.innerHTML = status.configured
    ? `<span class="yes">Configured</span> (${status.auth_method})`
    : `<span class="no">Not configured</span>`;
}

document.getElementById("save-system-btn").addEventListener("click", async () => {
  const data = collectFormData(document.getElementById("system-form"));
  await API.updateConfigSection("system", data);
});

document.getElementById("auth-method-select").addEventListener("change", (e) => {
  const isToken = e.target.value === "token";
  document.getElementById("cred-fields-token").style.display = isToken ? "block" : "none";
  document.getElementById("cred-fields-key").style.display = isToken ? "none" : "block";
});

document.getElementById("cred-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const payload = {
    auth_method: form.auth_method.value,
    token: form.token ? form.token.value : undefined,
    api_key: form.api_key ? form.api_key.value : undefined,
    api_secret_pem: form.api_secret_pem ? form.api_secret_pem.value : undefined,
  };
  try {
    await API.saveCred(payload);
    refreshSettingsTab();
    form.reset();
  } catch (err) {
    alert("Failed to save credentials: " + err.message);
  }
});

document.getElementById("delete-cred-btn").addEventListener("click", async () => {
  if (!confirm("Remove saved exchange credentials?")) return;
  await API.deleteCred();
  refreshSettingsTab();
});

// ------------------------------------------------------------------------ boot
(async function boot() {
  await refreshBotState();
  await refreshDashboard();
  startFitPoll();
  state.botPollTimer = setInterval(() => {
    refreshBotState();
    if (state.activeTab === "dashboard") refreshDashboard();
  }, 15000);
})();
