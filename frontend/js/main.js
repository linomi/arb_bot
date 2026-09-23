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

  document.getElementById("plot-title").textContent = `Residual / Z-Score — ${group.name}`;
  document.getElementById("plot-meta").textContent = "Fitting OLS on the latest window…";

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
      document.getElementById("plot-meta").textContent =
        "Could not fit residual: " + (e.message || e);
      return;
    }
  }

  state.selectedFit = fit;
  const trades = await API.listTrades(groupId);
  renderTradesTable(trades);
  renderResidualChart("residual-chart", fit, backboneCfg, trades, null);

  // Equity from API — live starts at account balance
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
  document.getElementById("plot-meta").innerHTML =
    `Live window OLS · ADF p=${fmtNum(fit.adf_pvalue, 3)} · KPSS p=${fmtNum(fit.kpss_pvalue, 3)} · ` +
    `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? "STATIONARY" : "REJECTED"}</span> · ` +
    `${bars} bars @ ${res} · fitted ${fmtTime(fit.fitted_at)}`;
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
}

const applyBtn = document.getElementById("perf-filter-apply");
if (applyBtn) applyBtn.addEventListener("click", () => {
  _collectPerfFiltersFromUI();
  applyPerfFiltersAndRedraw(showGroupDetail);
});
const clearBtn = document.getElementById("perf-filter-clear");
if (clearBtn) clearBtn.addEventListener("click", () => {
  clearPerfFilters();
  applyPerfFiltersAndRedraw(showGroupDetail);
});

document.querySelectorAll(".method-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".method-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.initMethod = btn.dataset.method;
  });
});

async function refreshInitTab() {
  const [backbone, init] = await Promise.all([
    API.getConfigSection("backbone"),
    API.getConfigSection("init"),
  ]);
  state.backboneCfg = backbone;
  state.initCfg = init;
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

const saveCfg = document.getElementById("save-config-btn");
if (saveCfg) saveCfg.addEventListener("click", async () => {
  const backboneData = collectFormData(document.getElementById("backbone-form"));
  const initData = collectFormData(document.getElementById("init-form"));
  await API.updateConfigSection("backbone", backboneData);
  await API.updateConfigSection("init", initData);
  state.backboneCfg = backboneData;
  alert("Parameters saved.");
});

const runInit = document.getElementById("run-init-btn");
if (runInit) runInit.addEventListener("click", async () => {
  runInit.disabled = true;
  try {
    await API.runInit(state.initMethod || "random", true);
    const poll = async () => {
      const p = await API.initProgress();
      if (p.running) { setTimeout(poll, 500); return; }
      runInit.disabled = false;
      await loadCandidatesTable();
    };
    setTimeout(poll, 300);
  } catch (e) {
    runInit.disabled = false;
    alert(e.message);
  }
});

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
  payload.auth_method = document.getElementById("auth-method-select").value;
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
