/* Chart.js residual + radar renderers (brown/cream terminal theme). */
let residualChart = null;
let radarChart = null;

const CHART_COLORS = {
  grid: "#4a3c30",
  text: "#c4b5a0",
  residual: "#d9945a",
  entryLine: "#c4783a",
  closeLine: "#6b9a6b",
  stopLine: "#c9a04a",
  entryMark: "#6b9a6b",
  closeMark: "#d9945a",
  stopMark: "#b85c4a",
};

function _constantSeries(n, value) {
  const v = Number(value);
  const out = new Array(n);
  for (let i = 0; i < n; i++) out[i] = isFinite(v) ? v : 0;
  return out;
}

function _shortLabel(iso) {
  if (typeof window !== "undefined" && typeof window._shortLabel === "function" && window._shortLabel !== _shortLabel) {
    try { return window._shortLabel(iso); } catch (e) {}
  }
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(5, 16);
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return `${mm}-${dd} ${hh}:${mi}`;
  } catch (e) {
    return String(iso).slice(0, 16);
  }
}

function _destroyChart(chart) {
  if (!chart) return null;
  try { chart.destroy(); } catch (e) {}
  return null;
}

function _emptyMarkArray(n) {
  return new Array(n).fill(null);
}

function renderResidualChart(canvasId, fit, backboneCfg, trades, onMarkerClick) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === "undefined") return;
  const series = (fit && fit.residual_series) || [];
  if (!series.length) {
    residualChart = _destroyChart(residualChart);
    return;
  }
  const labels = series.map((p) => _shortLabel(p[0]));
  const values = series.map((p) => Number(p[1]));
  const n = values.length;
  const cfg = backboneCfg || {};
  const mean = Number(fit.resid_mean) || 0;
  const std = Number(fit.resid_std) || 0;
  const zEntry = Number(cfg.z_entry) || 2;
  const zClose = Number(cfg.z_close) || 0.5;
  const zStop = Number(cfg.z_stop_loss) || 3.5;

  const entryMarks = _emptyMarkArray(n);
  const closeMarks = _emptyMarkArray(n);
  const stopMarks = _emptyMarkArray(n);
  const entryMeta = new Array(n);
  const closeMeta = new Array(n);
  const stopMeta = new Array(n);

  const tsList = series.map((p) => new Date(p[0]).getTime());
  const nearestIdx = (iso) => {
    const tms = new Date(iso).getTime();
    if (!isFinite(tms)) return -1;
    let best = 0, bestD = Infinity;
    for (let i = 0; i < tsList.length; i++) {
      const d = Math.abs(tsList[i] - tms);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  };

  (trades || []).forEach((t) => {
    if (t.entry_time != null && t.entry_residual != null) {
      const i = nearestIdx(t.entry_time);
      if (i >= 0) {
        entryMarks[i] = Number(t.entry_residual);
        entryMeta[i] = { kind: "entry", trade: t };
      }
    }
    if (t.close_time != null && t.close_residual != null) {
      const i = nearestIdx(t.close_time);
      if (i >= 0) {
        const isStop = t.close_reason === "stop_loss";
        if (isStop) {
          stopMarks[i] = Number(t.close_residual);
          stopMeta[i] = { kind: "stop", trade: t };
        } else {
          closeMarks[i] = Number(t.close_residual);
          closeMeta[i] = { kind: "close", trade: t };
        }
      }
    }
  });

  const hasEntry = entryMarks.some((v) => v != null);
  const hasClose = closeMarks.some((v) => v != null);
  const hasStop = stopMarks.some((v) => v != null);

  const datasets = [
    {
      label: "Residual",
      data: values,
      borderColor: CHART_COLORS.residual,
      backgroundColor: "rgba(217,148,90,0.08)",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.15,
      fill: false,
      order: 10,
    },
    {
      label: `Entry ±${zEntry}σ`,
      data: _constantSeries(n, mean + zEntry * std),
      borderColor: CHART_COLORS.entryLine,
      borderDash: [5, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      order: 5,
    },
    {
      label: `_entry_neg`,
      data: _constantSeries(n, mean - zEntry * std),
      borderColor: CHART_COLORS.entryLine,
      borderDash: [5, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      order: 5,
    },
    {
      label: `Close ±${zClose}σ`,
      data: _constantSeries(n, mean + zClose * std),
      borderColor: CHART_COLORS.closeLine,
      borderDash: [3, 3],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      order: 5,
    },
    {
      label: `_close_neg`,
      data: _constantSeries(n, mean - zClose * std),
      borderColor: CHART_COLORS.closeLine,
      borderDash: [3, 3],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      order: 5,
    },
    {
      label: `Stop ±${zStop}σ`,
      data: _constantSeries(n, mean + zStop * std),
      borderColor: CHART_COLORS.stopLine,
      borderDash: [2, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      order: 5,
    },
    {
      label: `_stop_neg`,
      data: _constantSeries(n, mean - zStop * std),
      borderColor: CHART_COLORS.stopLine,
      borderDash: [2, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      order: 5,
    },
  ];

  if (hasEntry) {
    datasets.push({
      label: "Entry",
      data: entryMarks,
      showLine: false,
      pointRadius: entryMarks.map((v) => (v == null ? 0 : 7)),
      pointHoverRadius: entryMarks.map((v) => (v == null ? 0 : 9)),
      pointStyle: "circle",
      pointBackgroundColor: CHART_COLORS.entryMark,
      pointBorderColor: "#1a1410",
      pointBorderWidth: 1,
      borderWidth: 0,
      order: 1,
      _markerMeta: entryMeta,
    });
  }
  if (hasClose) {
    datasets.push({
      label: "Close",
      data: closeMarks,
      showLine: false,
      pointRadius: closeMarks.map((v) => (v == null ? 0 : 7)),
      pointHoverRadius: closeMarks.map((v) => (v == null ? 0 : 9)),
      pointStyle: "rectRot",
      pointBackgroundColor: CHART_COLORS.closeMark,
      pointBorderColor: "#1a1410",
      pointBorderWidth: 1,
      borderWidth: 0,
      order: 1,
      _markerMeta: closeMeta,
    });
  }
  if (hasStop) {
    datasets.push({
      label: "Stop",
      data: stopMarks,
      showLine: false,
      pointRadius: stopMarks.map((v) => (v == null ? 0 : 7)),
      pointHoverRadius: stopMarks.map((v) => (v == null ? 0 : 9)),
      pointStyle: "triangle",
      pointBackgroundColor: CHART_COLORS.stopMark,
      pointBorderColor: "#1a1410",
      pointBorderWidth: 1,
      borderWidth: 0,
      order: 1,
      _markerMeta: stopMeta,
    });
  }

  residualChart = _destroyChart(residualChart);
  canvas.style.width = "100%";
  canvas.style.height = "300px";
  residualChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "nearest", intersect: true },
      plugins: {
        legend: {
          position: "top",
          align: "start",
          labels: {
            color: CHART_COLORS.text,
            boxWidth: 14,
            boxHeight: 10,
            padding: 10,
            font: { size: 11 },
            usePointStyle: true,
            filter: (item) => item.text && !item.text.startsWith("_"),
          },
        },
        tooltip: {
          callbacks: {
            label: (item) => {
              const lbl = item.dataset.label || "";
              if (item.dataset._markerMeta) {
                const meta = item.dataset._markerMeta[item.dataIndex];
                if (!meta) return "";
                const pnl = meta.trade.pnl;
                return `${meta.kind.toUpperCase()} · #${meta.trade.id} · pnl ${
                  pnl != null ? Number(pnl).toFixed(4) : "open"
                }`;
              }
              if (item.raw == null) return "";
              return `${lbl}: ${Number(item.raw).toFixed(5)}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: CHART_COLORS.text,
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 8,
            font: { size: 10 },
          },
          grid: { color: CHART_COLORS.grid },
        },
        y: {
          ticks: { color: CHART_COLORS.text, font: { size: 10 } },
          grid: { color: CHART_COLORS.grid },
        },
      },
      onClick: (evt, elements) => {
        if (!onMarkerClick || !residualChart) return;
        for (const el of elements) {
          const ds = residualChart.data.datasets[el.datasetIndex];
          if (ds && ds._markerMeta) {
            const meta = ds._markerMeta[el.index];
            if (meta && meta.trade) onMarkerClick(meta.trade);
            return;
          }
        }
      },
    },
  });
}

function renderRadarChart(canvasId, metrics) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === "undefined") return;
  canvas.style.width = "100%";
  canvas.style.height = "220px";
  const m = metrics || {};
  const norm = {
    "Win Rate": _finite01(m.win_rate),
    Sharpe: _squash(m.sharpe_ratio),
    Sortino: _squash(m.sortino_ratio),
    "Profit Factor": _squash((Number(m.profit_factor) || 0) / 3),
    "Low Drawdown": Number(m.max_drawdown) > 0
      ? _squash(1 / Number(m.max_drawdown))
      : Number(m.trade_count) > 0
      ? 1
      : 0.15,
    "Trade Freq": _squash((Number(m.trade_count) || 0) / 20),
  };
  if (Object.values(norm).every((v) => !v)) {
    Object.keys(norm).forEach((k) => {
      norm[k] = 0.12;
    });
  }
  radarChart = _destroyChart(radarChart);
  radarChart = new Chart(canvas.getContext("2d"), {
    type: "radar",
    data: {
      labels: Object.keys(norm),
      datasets: [
        {
          label: "Performance",
          data: Object.values(norm),
          backgroundColor: "rgba(196,120,58,0.25)",
          borderColor: CHART_COLORS.residual,
          borderWidth: 2,
          pointBackgroundColor: CHART_COLORS.residual,
          pointRadius: 3,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false } },
      scales: {
        r: {
          angleLines: { color: CHART_COLORS.grid },
          grid: { color: CHART_COLORS.grid },
          pointLabels: { color: CHART_COLORS.text, font: { size: 10 } },
          ticks: { display: false, maxTicksLimit: 3 },
          min: 0,
          max: 1,
        },
      },
    },
  });
}

function _finite01(x) {
  const v = Number(x);
  if (!isFinite(v) || v < 0) return 0;
  if (v > 1) return 1;
  return v;
}
function _squash(x) {
  if (!isFinite(x)) return 0.5;
  if (x <= 0) return 0;
  return x / (1 + x);
}
