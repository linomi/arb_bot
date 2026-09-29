/**
 * Overrides for Backbone tab after main.js loads.
 * Trades / performance / equity follow current bot mode (server default).
 * Live equity starts from wallet balance via /equity_curve API.
 */
(function () {
  window.renderGroupList = function () {};
  const gs = document.getElementById("group-search");
  if (gs) gs.replaceWith(gs.cloneNode(true));

  async function loadEquityForGroup(groupId) {
    const emptyEl = document.getElementById("equity-empty");
    try {
      const curve = await API.equityCurve(groupId);
      const points = (curve && curve.points) ? curve.points : (Array.isArray(curve) ? curve : []);
      if (!points.length) {
        if (emptyEl) emptyEl.hidden = false;
        return;
      }
      if (emptyEl) emptyEl.hidden = true;
      const meta = {
        startEquity: curve && curve.start_equity != null ? Number(curve.start_equity) : 0,
        accountBalance: curve && curve.account_balance != null ? Number(curve.account_balance) : null,
        source: (curve && curve.source) || "model_cum_pnl",
      };
      if (typeof renderEquityCurvePoints === "function") {
        renderEquityCurvePoints("equity-canvas", points, meta);
      }
    } catch (e) {
      console.warn("equity_curve API failed, fallback trades", e);
      try {
        const trades = await API.listTrades(groupId);
        if (typeof renderEquityChart === "function") renderEquityChart("equity-canvas", trades);
      } catch (e2) {}
    }
  }

  window.loadGroupList = async function loadGroupList() {
    const groups = await API.listGroups();
    state.groups = groups;
    if (!state.selectedGroupId && groups.length) {
      await selectGroup(groups[0].id, true);
    }
  };

  window.selectGroup = async function selectGroup(groupId, force = true) {
    // A marker click pins the stored fit a trade was based on. The 5s poll (force=false)
    // must not replace it; any explicit selection (force=true) or "Back to live fit" unpins.
    if (state.pinnedFit) {
      if (force === false && state.pinnedFit.groupId === groupId) return;
      state.pinnedFit = null;
    }
    state.selectedGroupId = groupId;
    if (!state.groups.find((g) => g.id === groupId)) {
      try {
        state.groups = await API.listGroups();
      } catch (e) {}
    }
    const g = state.groups.find((x) => x.id === groupId);
    if (!g) return;

    const gname = g.name || `group-${groupId}`;
    document.getElementById("plot-title").textContent = `Residual / Z-Score — ${gname}`;
    const eqTitle = document.getElementById("equity-title");
    if (eqTitle) eqTitle.textContent = `Equity Curve — ${gname}`;
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
        if (bd) {
          bd.innerHTML =
            `<div class="beta-group-title">${gname}</div>` +
            `<div class="detail-empty" style="padding:12px">Fit failed — betas unavailable.</div>`;
        }
        return;
      }
    }

    if (fit && !fit.group_name) fit.group_name = gname;

    state.selectedFit = fit;
    const trades = await API.listTrades(groupId);
    renderTradesTable(trades);
    renderResidualChart("residual-chart", fit, backboneCfg, trades, (trade) => {
      _showFitForTrade(groupId, trade, trades);
    });

    // Live: start from account balance via dedicated API (not from zero)
    await loadEquityForGroup(groupId);

    if (typeof renderBetaDiagram === "function") {
      renderBetaDiagram("beta-diagram", g, fit);
    }

    const modeLabel = (trades && trades[0] && trades[0].mode) || state.tradingMode || "";
    const bars = fit.bars_used != null ? fit.bars_used : "?";
    const res = fit.resolution || "?";
    document.getElementById("plot-meta").innerHTML =
      `<b>${gname}</b> · <span class="mode-tag">${modeLabel || "mode?"}</span> · Live window OLS · ADF p=${fmtNum(fit.adf_pvalue, 3)} · KPSS p=${fmtNum(fit.kpss_pvalue, 3)} · ` +
      `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? "STATIONARY" : "REJECTED"}</span> · ` +
      `${bars} bars @ ${res} · fitted ${fmtTime(fit.fitted_at)}`;
  };

  function _escHtml(x) {
    return String(x).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function _wireBackToLive(groupId) {
    const a = document.querySelector("#plot-meta .rc-back");
    if (!a) return;
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      state.pinnedFit = null;
      selectGroup(groupId, true);
    });
  }

  /**
   * Residual-chart marker click: show the stored fit this trade was based on
   * (its own residual window, mean/std and z-bands), pinned until "Back to live fit".
   */
  window._showFitForTrade = async function (groupId, trade, trades) {
    const meta = document.getElementById("plot-meta");
    if (!trade) return;
    const g = (state.groups || []).find((x) => x.id === groupId);
    const gname = (g && g.name) || `group-${groupId}`;
    const back = ` · <a href="#" class="rc-back">Back to live fit</a>`;
    if (trade.ols_fit_id == null) {
      if (meta) meta.innerHTML = `Trade #${_escHtml(trade.id)} has no stored fit${back}`;
      _wireBackToLive(groupId);
      return;
    }
    let fit;
    try {
      fit = await API.getFit(groupId, trade.ols_fit_id);
    } catch (e) {
      if (meta) {
        meta.innerHTML = `Fit for trade #${_escHtml(trade.id)} is not available ` +
          `(${_escHtml(e.message || e)})${back}`;
      }
      _wireBackToLive(groupId);
      return;
    }
    const backboneCfg = state.backboneCfg || (await API.getConfigSection("backbone"));
    state.backboneCfg = backboneCfg;
    fit.group_name = gname;
    fit._pinned_trade_id = trade.id;
    state.pinnedFit = { groupId, tradeId: trade.id };
    renderResidualChart("residual-chart", fit, backboneCfg, trades, (t) => {
      window._showFitForTrade(groupId, t, trades);
    });
    if (meta) {
      meta.innerHTML =
        `<b>${_escHtml(gname)}</b> · <span class="mode-tag">fit used by trade #${_escHtml(trade.id)}</span> · ` +
        `ADF p=${fmtNum(fit.adf_pvalue, 3)} · KPSS p=${fmtNum(fit.kpss_pvalue, 3)} · ` +
        `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? "STATIONARY" : "REJECTED"}</span> · ` +
        `fitted ${fmtTime(fit.fitted_at)}${back}`;
    }
    _wireBackToLive(groupId);
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
      const eqTitle = document.getElementById("equity-title");
      if (eqTitle) eqTitle.textContent = "Equity Curve";
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

  // Override showGroupDetail equity section when detail panel is used
  const _origShow = window.showGroupDetail;
  window.showGroupDetail = async function (row) {
    if (typeof _origShow === "function") {
      await _origShow(row);
    }
    if (row && row.group_id) {
      await loadEquityForGroup(row.group_id);
    }
  };

  const modeSelect = document.getElementById("trading-mode-select");
  if (modeSelect && !modeSelect.dataset.modeRefreshWired) {
    modeSelect.dataset.modeRefreshWired = "1";
    modeSelect.addEventListener("change", async () => {
      setTimeout(async () => {
        try {
          const s = await API.botState();
          state.tradingMode = s.trading_mode;
        } catch (e) {}
        if (typeof loadPerfTable === "function") await loadPerfTable();
        if (state.selectedGroupId) await selectGroup(state.selectedGroupId, false);
      }, 200);
    });
  }
})();
