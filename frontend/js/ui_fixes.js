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
        <h2 id="equity-title">${typeof t==="function"?t("equity.title"):"Equity Curve"}</h2>
        <span class="hint">${typeof t==="function"?t("equity.hint.cum"):"Cumulative realized cash PnL"}</span>
      </div>
      <div class="equity-wrap" style="padding:8px 12px 12px;height:200px">
        <canvas id="equity-canvas"></canvas>
      </div>
      <div class="equity-empty" id="equity-empty" style="padding:0 14px 12px" hidden>${typeof t==="function"?t("equity.empty"):"No closed trades yet for this group."}</div>
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

  function boot() {
    ensureEquityPanel();
    wireManualButtons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
