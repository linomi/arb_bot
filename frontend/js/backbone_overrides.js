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
    const backboneCfg = state.backboneCfg || (await API.getConfigSection("backbone"));
    state.backboneCfg = backboneCfg;
    const samplingMs = Math.max(5, Number(backboneCfg.sampling_time) || 60) * 1000;

    // The OLS only changes when a new candle opens, so refit once per candle
    // bucket (not on a timer). The 5 s poll just checks for new candles / trades.
    const bucket = Math.floor(Date.now() / samplingMs);
    state.fitBucket = state.fitBucket || {};
    state.tradeSig = state.tradeSig || {};
    let fit = state.lastFitData[groupId];
    const needFit = !fit || state.fitBucket[groupId] !== bucket;

    // Do not overlap polls with an in-flight selection of the same group.
    if (force === false && state.selBusy === groupId) return;
    const mySeq = (state.selSeq = (state.selSeq || 0) + 1);
    state.selBusy = groupId;
    const stale = () => state.selSeq !== mySeq || state.selectedGroupId !== groupId;

    try {
      if (force !== false) {
        document.getElementById("plot-title").textContent = t("plot.title.group", { name: gname });
        const eqTitle = document.getElementById("equity-title");
        if (eqTitle) eqTitle.textContent = t("equity.title.group", { name: gname });
      }

      const fitPromise = needFit
        ? (async () => {
            document.getElementById("plot-meta").textContent = t("plot.fitting");
            return API.liveFit(groupId);
          })()
        : Promise.resolve(fit);
      const tradesPromise = API.listTrades(groupId);

      try {
        fit = await fitPromise;
      } catch (e) {
        if (stale()) return;
        if (typeof residualChart !== "undefined" && residualChart) {
          residualChart.destroy();
          residualChart = null;
        }
        document.getElementById("plot-meta").textContent =
          t("plot.fit.failed", { error: e.message || e });
        const tbody = document.querySelector("#trades-table tbody");
        if (tbody) tbody.innerHTML = "";
        const bd = document.getElementById("beta-diagram");
        if (bd) {
          bd.innerHTML =
            `<div class="beta-group-title">${gname}</div>` +
            `<div class="detail-empty" style="padding:12px">${t("beta.fit.failed")}</div>`;
        }
        return;
      }
      if (needFit) {
        state.fitBucket[groupId] = bucket;
        state.lastFitAt[groupId] = Date.now();
        state.lastFitData[groupId] = fit;
      }
      const trades = await tradesPromise;
      if (stale()) return;

      // Nothing new (same candle, same trades, same selection): leave the DOM alone.
      const sig = JSON.stringify((trades || []).map((x) => [x.id, x.status, x.close_time, x.pnl]));
      if (!needFit && force === false && state.tradeSig[groupId] === sig) return;
      state.tradeSig[groupId] = sig;

      if (fit && !fit.group_name) fit.group_name = gname;
      state.selectedFit = fit;
      renderTradesTable(trades);
      renderResidualChart("residual-chart", fit, backboneCfg, trades, (trade) => {
        _showFitForTrade(groupId, trade, trades);
      });

      const modeLabel = (trades && trades[0] && trades[0].mode) || state.tradingMode || "";
      const bars = fit.bars_used != null ? fit.bars_used : "?";
      const res = fit.resolution || "?";
      document.getElementById("plot-meta").innerHTML = t("plot.meta.live.named", {
        name: gname,
        mode: modeLabel || "mode?",
        adf: fmtNum(fit.adf_pvalue, 3),
        kpss: fmtNum(fit.kpss_pvalue, 3),
        flag: `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? t("plot.stationary") : t("plot.rejected")}</span>`,
        bars, res, fitted: fmtTime(fit.fitted_at),
      });

      // Secondary panels load after the chart is visible, in parallel.
      await Promise.all([
        loadEquityForGroup(groupId),
        (async () => {
          if (typeof renderBetaDiagram === "function") renderBetaDiagram("beta-diagram", g, fit);
        })(),
      ]);
    } finally {
      if (state.selBusy === groupId) state.selBusy = null;
    }
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
    const back = ` · <a href="#" class="rc-back">${t("plot.back.live")}</a>`;
    if (trade.ols_fit_id == null) {
      if (meta) meta.innerHTML = t("plot.trade.no.fit", { id: _escHtml(trade.id) }) + back;
      _wireBackToLive(groupId);
      return;
    }
    let fit;
    try {
      fit = await API.getFit(groupId, trade.ols_fit_id);
    } catch (e) {
      if (meta) {
        meta.innerHTML = t("plot.trade.fit.missing", { id: _escHtml(trade.id), error: _escHtml(e.message || e) }) + back;
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
      meta.innerHTML = t("plot.trade.fit.meta", {
        name: _escHtml(gname),
        id: _escHtml(trade.id),
        adf: fmtNum(fit.adf_pvalue, 3),
        kpss: fmtNum(fit.kpss_pvalue, 3),
        flag: `<span class="${fit.passed ? "flag-pass" : "flag-fail"}">${fit.passed ? t("plot.stationary") : t("plot.rejected")}</span>`,
        fitted: fmtTime(fit.fitted_at),
      }) + back;
    }
    _wireBackToLive(groupId);
  };

  window.deleteGroupFromTable = async function (groupId, name) {
    if (!confirm(t("confirm.delete.group.perm", { name }))) return;
    try {
      await API.deleteGroup(groupId);
    } catch (e) {
      alert(t("alert.delete.failed", { error: e.message || e }));
      return;
    }
    if (state.selectedGroupId === groupId) {
      state.selectedGroupId = null;
      document.getElementById("plot-title").textContent = t("plot.title");
      document.getElementById("plot-meta").textContent = "";
      const eqTitle = document.getElementById("equity-title");
      if (eqTitle) eqTitle.textContent = t("equity.title");
      const bd = document.getElementById("beta-diagram");
      if (bd) {
        bd.innerHTML =
          `<div class="detail-empty" style="padding:12px">${t("beta.empty")}</div>`;
      }
    }
    await loadGroupList();
    await loadPerfTable();
  };

  window.toggleGroupStatusFromTable = async function (groupId, nextStatus, name) {
    try {
      await API.setGroupStatus(groupId, nextStatus);
    } catch (e) {
      alert(t("alert.status.failed", { error: e.message || e }));
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
