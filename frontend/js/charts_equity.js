/* Equity curve + Jalali labels in Asia/Tehran
 * Equity = current trade_notional + sum(scaled PnL).
 * API already returns pnl scaled by current notional. */
(function () {
  function toJalali(gy, gm, gd) {
    const g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
    let gy2 = gm > 2 ? gy + 1 : gy;
    let days =
      355666 + 365 * gy + Math.floor((gy2 + 3) / 4) - Math.floor((gy2 + 99) / 100) +
      Math.floor((gy2 + 399) / 400) + gd + g_d_m[gm - 1];
    let jy = -1595 + 33 * Math.floor(days / 12053);
    days %= 12053;
    jy += 4 * Math.floor(days / 1461);
    days %= 1461;
    if (days > 365) {
      jy += Math.floor((days - 1) / 365);
      days = (days - 1) % 365;
    }
    const jm = days < 186 ? 1 + Math.floor(days / 31) : 7 + Math.floor((days - 186) / 30);
    const jd = 1 + (days < 186 ? days % 31 : (days - 186) % 30);
    return [jy, jm, jd];
  }

  function tehranParts(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Tehran",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    const parts = {};
    for (const p of fmt.formatToParts(d)) {
      if (p.type !== "literal") parts[p.type] = p.value;
    }
    let hour = parts.hour === "24" ? "00" : parts.hour;
    return {
      gy: parseInt(parts.year, 10),
      gm: parseInt(parts.month, 10),
      gd: parseInt(parts.day, 10),
      hh: hour,
      mi: parts.minute,
    };
  }

  window._shortLabel = function (iso) {
    if (!iso) return "";
    try {
      const tp = tehranParts(iso);
      if (!tp) return String(iso).slice(5, 16);
      const [jy, jm, jd] = toJalali(tp.gy, tp.gm, tp.gd);
      return `${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")} ${tp.hh}:${tp.mi}`;
    } catch (e) {
      return String(iso).slice(0, 16);
    }
  };

  let equityChart = null;

  function resolveNotional(trades, explicit) {
    if (explicit != null && isFinite(Number(explicit))) return Number(explicit);
    // Prefer notional attached to trade payload from API
    if (trades && trades.length) {
      for (const t of trades) {
        if (t.trade_notional != null && isFinite(Number(t.trade_notional))) {
          return Number(t.trade_notional);
        }
      }
    }
    if (window.__tradeNotional != null && isFinite(Number(window.__tradeNotional))) {
      return Number(window.__tradeNotional);
    }
    try {
      if (typeof state !== "undefined" && state.backboneCfg) {
        const n = Number(state.backboneCfg.trade_notional);
        if (isFinite(n) && n > 0) return n;
      }
    } catch (e) {}
    return 100;
  }

  window.renderEquityChart = function (canvasId, trades, startNotional) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === "undefined") {
      console.warn("renderEquityChart: missing canvas or Chart.js");
      return;
    }

    const notional = resolveNotional(trades, startNotional);
    window.__tradeNotional = notional;

    const closed = (trades || [])
      .filter((t) => t.status === "closed" && t.close_time != null && t.pnl != null)
      .slice()
      .sort((a, b) => new Date(a.close_time) - new Date(b.close_time));

    if (equityChart) {
      try { equityChart.destroy(); } catch (e) {}
      equityChart = null;
    }

    canvas.style.width = "100%";
    canvas.style.height = "180px";

    const emptyEl = document.getElementById("equity-empty");
    if (!closed.length) {
      if (emptyEl) emptyEl.hidden = false;
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    if (emptyEl) emptyEl.hidden = true;

    // API pnl is already scaled by current trade_notional
    const labels = ["Start"];
    const values = [notional];
    let cum = notional;
    closed.forEach((t) => {
      cum += Number(t.pnl) || 0;
      labels.push(window._shortLabel(t.close_time));
      values.push(cum);
    });

    const last = values[values.length - 1];
    const lineColor = last >= notional ? "#6b9a6b" : "#b85c4a";

    equityChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Equity",
          data: values,
          borderColor: lineColor,
          backgroundColor: last >= notional ? "rgba(107,154,107,0.12)" : "rgba(184,92,74,0.12)",
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
                const eq = Number(item.raw);
                const pnl = eq - notional;
                return `Equity: ${eq.toFixed(4)}  (PnL ${pnl >= 0 ? "+" : ""}${pnl.toFixed(4)}) · notional ${notional}`;
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
            suggestedMin: Math.min(notional * 0.9, Math.min(...values)),
          },
        },
      },
    });

    const hint = document.querySelector(".equity-panel .hint");
    if (hint) {
      hint.textContent = `Starts at trade notional (${notional}) + cumulative PnL`;
    }
  };

  async function loadNotional() {
    if (!window.API || typeof API.getConfigSection !== "function") return;
    try {
      const data = await API.getConfigSection("backbone");
      const n = Number(data && data.trade_notional);
      if (isFinite(n) && n > 0) {
        window.__tradeNotional = n;
        if (typeof state !== "undefined") {
          state.backboneCfg = data;
        }
      }
    } catch (e) {}
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadNotional);
  } else {
    loadNotional();
  }
  setTimeout(loadNotional, 800);
})();
