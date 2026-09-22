/* Equity curve — labels via shared TehranTime */
(function () {
  let equityChart = null;

  function tradePnl(t) {
    if (t.realized_pnl != null && isFinite(Number(t.realized_pnl))) return Number(t.realized_pnl);
    if (t.pnl != null && isFinite(Number(t.pnl))) return Number(t.pnl);
    return null;
  }

  function label(iso) {
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
      points.push({ time: t.close_time, cum_pnl: cum });
    });
    window.renderEquityCurvePoints(canvasId, points);
  };

  window.renderEquityCurvePoints = function (canvasId, points) {
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
    if (!series.length) {
      if (emptyEl) emptyEl.hidden = false;
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    if (emptyEl) emptyEl.hidden = true;

    const labels = ["Start"].concat(series.map((p) => label(p.time)));
    const values = [0].concat(series.map((p) => Number(p.cum_pnl)));
    const last = values[values.length - 1];
    const lineColor = last >= 0 ? "#6b9a6b" : "#b85c4a";

    equityChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Cum. realized PnL",
          data: values,
          borderColor: lineColor,
          backgroundColor: last >= 0 ? "rgba(107,154,107,0.12)" : "rgba(184,92,74,0.12)",
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
              label: (item) => `Cum. realized PnL: ${Number(item.raw).toFixed(4)}`,
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
      hint.textContent = "Cumulative realized cash PnL · times in Asia/Tehran (Jalali)";
    }
  };
})();
