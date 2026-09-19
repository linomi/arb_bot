/* Equity curve + Jalali labels (extends charts.js) */
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

  window._shortLabel = function (iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso).slice(5, 16);
      const [jy, jm, jd] = toJalali(d.getFullYear(), d.getMonth() + 1, d.getDate());
      const hh = String(d.getHours()).padStart(2, "0");
      const mi = String(d.getMinutes()).padStart(2, "0");
      return `${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")} ${hh}:${mi}`;
    } catch (e) {
      return String(iso).slice(0, 16);
    }
  };

  let equityChart = null;

  window.renderEquityChart = function (canvasId, trades) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === "undefined") return;

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
    if (!closed.length) return;

    let cum = 0;
    const labels = [];
    const values = [];
    closed.forEach((t) => {
      cum += Number(t.pnl) || 0;
      labels.push(window._shortLabel(t.close_time));
      values.push(cum);
    });

    const last = values[values.length - 1];
    const lineColor = last >= 0 ? "#6b9a6b" : "#b85c4a";

    equityChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Equity",
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
          tooltip: { callbacks: { label: (item) => `Equity: ${Number(item.raw).toFixed(4)}` } },
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
  };
})();
