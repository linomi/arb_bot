/**
 * Overrides for Backbone tab after main.js loads.
 */
(function () {
  window.renderGroupList = function () {};
  const gs = document.getElementById("group-search");
  if (gs) gs.replaceWith(gs.cloneNode(true));

  window.loadGroupList = async function loadGroupList() {
    const groups = await API.listGroups();
    state.groups = groups;
    if (!state.selectedGroupId && groups.length) {
      await selectGroup(groups[0].id, true);
    }
  };

  window.selectGroup = async function selectGroup(groupId, force = true) {
    state.selectedGroupId = groupId;
    if (!state.groups.find((g) => g.id === groupId)) {
      try {
        state.groups = await API.listGroups();
      } catch (e) {}
    }
    const g = state.groups.find((x) => x.id === groupId);
    if (!g) return;

    document.getElementById("plot-title").textContent = `Residual / Z-Score — ${g.name}`;
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
        if (typeof residualChart !== "undefined" && residualChart) {
          residualChart.destroy();
          residualChart = null;
        }
        document.getElementById("plot-meta").textContent =
          "Could not fit residual: " + (e.message || e);
        const tbody = document.querySelector("#trades-table tbody");
        if (tbody) tbody.innerHTML = "";
        const bd = document.getElementById("beta-diagram");
        if (bd) bd.innerHTML = `<div class="detail-empty" style="padding:12px">Fit failed — betas unavailable.</div>`;
        return;
      }
    }

    state.selectedFit = fit;
    const trades = await API.listTrades(groupId);
    renderTradesTable(trades);
    renderResidualChart("residual-chart", fit, backboneCfg, trades, (trade) => {
      _showFitForTrade(groupId, trade, trades);
    });

    try {
      const closed = (trades || []).filter((t) => t.status === "closed" && t.pnl != null);
      const emptyEl = document.getElementById("equity-empty");
      if (!closed.length) {
        if (emptyEl) emptyEl.hidden = false;
      } else {
        if (emptyEl) emptyEl.hidden = true;
        if (typeof renderEquityChart === "function") renderEquityChart("equity-canvas", trades);
      }
    } catch (e) {}

    if (typeof renderBetaDiagram === "function") {
      renderBetaDiagram("beta-diagram", g, fit);
    }

    const bars = fit.bars_used != null ? fit.bars_used : "?";
    const res = fit.resolution || "?";
    document.getElementById("plot-meta").innerHTML =
      `Live window OLS · ADF p=${fmtNum(fit.adf_pvalue, 3)} · KPSS p=${fmtNum(fit.kpss_pvalue, 3)} · ` +
      `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? "STATIONARY" : "REJECTED"}</span> · ` +
      `${bars} bars @ ${res} · fitted ${fmtTime(fit.fitted_at)}`;
  };

  window.deleteGroupFromTable = async function (groupId, name) {
    if (!confirm(`Permanently delete group "${name}"?`)) return;
    try {
      await API.deleteGroup(groupId);
    } catch (e) {
      alert("Delete failed: " + (e.message || e));
      return;
    }
    if (state.selectedGroupId === groupId) {
      state.selectedGroupId = null;
      document.getElementById("plot-title").textContent = "Residual / Z-Score";
      document.getElementById("plot-meta").textContent = "";
      const bd = document.getElementById("beta-diagram");
      if (bd) {
        bd.innerHTML =
          `<div class="detail-empty" style="padding:12px">Select a group in the performance table to view betas.</div>`;
      }
    }
    await loadGroupList();
    await loadPerfTable();
  };

  window.toggleGroupStatusFromTable = async function (groupId, nextStatus, name) {
    try {
      await API.setGroupStatus(groupId, nextStatus);
    } catch (e) {
      alert("Status change failed: " + (e.message || e));
      return;
    }
    await loadGroupList();
    await loadPerfTable();
  };
})();
