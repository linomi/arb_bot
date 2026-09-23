/* Equity curve — live starts from account balance; paper from 0 */
(function () {
  let equityChart = null;

  function tradePnl(t) {
    // Live: only exchange realized
    if (t.mode === "live") {
      if (t.realized_pnl != null && isFinite(Number(t.realized_pnl))) return Number(t.realized_pnl);
      return null;
    }
    if (t.realized_pnl != null && isFinite(Number(t.realized_pnl))) return Number(t.realized_pnl);
    if (t.pnl != null && isFinite(Number(t.pnl))) return Number(t.pnl);
    return null;
  }

  function label(iso) {
    if (!iso) return "Start";
    if (window.TehranTime) return window.TehranTime.shortLabel(iso);
    if (typeof window._shortLabel === "function") return window._shortLabel(iso);
    return String(iso || "").slice(5, 16);
  }

  window.renderEquityChart = function (canvasId, trades) {
    const closed = (trades || [])
      .filter((t) => t.status === "closed" && t.close_time != null && tradePnl(t) != null)
      .slice()
      .sort((a, b) => {
        const ta = window.TehranTime ? window.TehranTime.parseAsUtc(a.close_time) : new Date(a.close_time);
        const tb = window.TehranTime ? window.TehranTime.parseAsUtc(b.close_time) : new Date(b.close_time);
        return (ta || 0) - (tb || 0);
      });

    const points = [];
    let cum = 0;
    closed.forEach((t) => {
      cum += tradePnl(t);
      points.push({ time: t.close_time, equity: cum, cum_pnl: cum });
    });
    window.renderEquityCurvePoints(canvasId, points, { startEquity: 0 });
  };

  /**
   * points: array of {time, equity?, cum_pnl?, pnl?}
   * meta: { startEquity, accountBalance, source }
   */
  window.renderEquityCurvePoints = function (canvasId, points, meta) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === "undefined") {
      console.warn("renderEquityCurvePoints: missing canvas or Chart.js");
      return;
    }

    if (equityChart) {
      try { equityChart.destroy(); } catch (e) {}
      equityChart = null;
    }

    canvas.style.width = "100%";
    canvas.style.height = "180px";

    const emptyEl = document.getElementById("equity-empty");
    const series = points || [];
    const startEq = meta && meta.startEquity != null ? Number(meta.startEquity) : null;

    if (!series.length && (startEq == null || !isFinite(startEq))) {
      if (emptyEl) emptyEl.hidden = false;
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    if (emptyEl) emptyEl.hidden = true;

    // Prefer equity field; else rebuild from start + cum_pnl
    let labels = [];
    let values = [];
    if (series.length && series[0].is_start) {
      labels = series.map((p) => (p.is_start || !p.time) ? "Start" : label(p.time));
      values = series.map((p) => Number(p.equity != null ? p.equity : p.cum_pnl));
    } else if (startEq != null && isFinite(startEq)) {
      labels = ["Start"].concat(series.map((p) => label(p.time)));
      let eq = startEq;
      values = [eq];
      series.forEach((p) => {
        const step = p.pnl != null ? Number(p.pnl) : (p.cum_pnl != null ? null : 0);
        if (p.equity != null) eq = Number(p.equity);
        else if (step != null) eq += step;
        else if (p.cum_pnl != null) eq = startEq + Number(p.cum_pnl);
        values.push(eq);
      });
    } else {
      labels = ["Start"].concat(series.map((p) => label(p.time)));
      values = [0].concat(series.map((p) => Number(p.equity != null ? p.equity : p.cum_pnl)));
    }

    const last = values[values.length - 1];
    const first = values[0];
    const lineColor = last >= first ? "#6b9a6b" : "#b85c4a";
    const isLive = meta && String(meta.source || "").includes("exchange");

    equityChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: isLive ? "Account equity (exchange PnL)" : "Cum. model PnL",
          data: values,
          borderColor: lineColor,
          backgroundColor: last >= first ? "rgba(107,154,107,0.12)" : "rgba(184,92,74,0.12)",
          borderWidth: 2,
          pointRadius: values.length <= 40 ? 3 : 0,
          pointBackgroundColor: lineColor,
          tension: 0.15,
          fill: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (item) => {
                const v = Number(item.raw);
                return isLive ? `Equity: ${v.toFixed(0)} IRT` : `Cum. PnL: ${v.toFixed(4)}`;
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: "#c4b5a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 6, font: { size: 9 } },
            grid: { color: "#4a3c30" },
          },
          y: {
            ticks: { color: "#c4b5a0", font: { size: 9 } },
            grid: { color: "#4a3c30" },
          },
        },
      },
    });

    const hint = document.querySelector(".equity-panel .hint");
    if (hint) {
      if (isLive) {
        const bal = meta.accountBalance != null ? Number(meta.accountBalance).toFixed(0) : "?";
        const start = startEq != null ? Number(startEq).toFixed(0) : "?";
        hint.textContent =
          `Live · start≈${start} IRT (from wallet) · now free≈${bal} IRT · exchange position.PNL only · Tehran time`;
      } else {
        hint.textContent = "Paper · cumulative model PnL from 0 · times in Asia/Tehran (Jalali)";
      }
    }
  };
})();
