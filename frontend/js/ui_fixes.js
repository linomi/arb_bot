/* Runtime fixes loaded after main.js */
(function () {
  function ensureEquityPanel() {
    if (document.getElementById("equity-canvas")) return;
    const tradesPanel = document.querySelector(".trades-panel");
    if (!tradesPanel || !tradesPanel.parentNode) return;
    const panel = document.createElement("div");
    panel.className = "panel equity-panel";
    panel.innerHTML = `
      <div class="panel-head">
        <h2 id="equity-title">Equity Curve</h2>
        <span class="hint">Cumulative realized cash PnL</span>
      </div>
      <div class="equity-wrap" style="padding:8px 12px 12px;height:200px">
        <canvas id="equity-canvas"></canvas>
      </div>
      <div class="equity-empty" id="equity-empty" style="padding:0 14px 12px" hidden>No closed trades yet for this group.</div>
    `;
    tradesPanel.parentNode.insertBefore(panel, tradesPanel);
  }

  function wireManualButtons() {
    document.querySelectorAll(".method-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const method = btn.dataset.method;
        if (typeof state !== "undefined") state.initMethod = method;
        document.querySelectorAll(".method-btn").forEach((b) => b.classList.toggle("active", b === btn));
        const panel = document.getElementById("manual-group-panel");
        const autoActions = document.getElementById("init-auto-actions");
        const initForm = document.getElementById("init-form");
        if (!panel) return;
        const isManual = method === "manual";
        panel.hidden = !isManual;
        panel.style.display = isManual ? "block" : "none";
        panel.classList.toggle("is-open", isManual);
        if (autoActions) autoActions.style.display = isManual ? "none" : "flex";
        if (initForm) initForm.style.display = isManual ? "none" : "";
        if (isManual && typeof loadLiquidSymbols === "function") loadLiquidSymbols();
      });
    });
  }

  async function refreshEquity(groupId, trades) {
    ensureEquityPanel();
    try {
      if (API.equityCurve) {
        const points = await API.equityCurve(groupId);
        if (typeof renderEquityCurvePoints === "function") {
          renderEquityCurvePoints("equity-canvas", points);
          return;
        }
      }
    } catch (e) {
      console.warn("equity_curve endpoint", e);
    }
    if (typeof renderEquityChart === "function") {
      renderEquityChart("equity-canvas", trades);
    }
  }

  function patchSelectGroup() {
    if (!window.API || API._equityPatched) return;
    const orig = API.listTrades.bind(API);
    API.listTrades = async function (id) {
      const trades = await orig(id);
      try {
        await refreshEquity(id, trades);
        const g = (typeof state !== "undefined" && state.groups)
          ? state.groups.find((x) => x.id === id) : null;
        const title = document.getElementById("equity-title");
        if (title && g) title.textContent = "Equity Curve — " + g.name;
      } catch (e) {
        console.warn("equity patch", e);
      }
      return trades;
    };
    API._equityPatched = true;
  }

  function boot() {
    ensureEquityPanel();
    wireManualButtons();
    patchSelectGroup();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
  setTimeout(patchSelectGroup, 500);
  setTimeout(patchSelectGroup, 2000);
})();
