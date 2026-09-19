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
  try {
    chart.destroy();
  } catch (e) { /* ignore */ }
  return null;
}

/**
 * Residual / z-score chart for a live or stored OLS fit.
 * fit.residual_series: [[isoTimestamp, value], ...]
 */
function renderResidualChart(canvasId, fit, backboneCfg, trades, onMarkerClick) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) {
    console.error("renderResidualChart: canvas not found", canvasId);
    return;
  }
  if (typeof Chart === "undefined") {
    console.error("renderResidualChart: Chart.js not loaded");
    return;
  }

  const series = (fit && fit.residual_series) || [];
  if (!series.length) {
    residualChart = _destroyChart(residualChart);
    const ctx0 = canvas.getContext("2d");
    ctx0.clearRect(0, 0, canvas.width, canvas.height);
    console.warn("renderResidualChart: empty residual_series");
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

  // Map trade markers onto the nearest residual index (category axis).
  const markerData = [];
  const markerMeta = [];
  const markerColors = [];
  const markerStyles = [];
  const tsList = series.map((p) => new Date(p[0]).getTime());

  (trades || []).forEach((t) => {
    const pushMark = (iso, y, kind) => {
      if (iso == null || y == null) return;
      const tms = new Date(iso).getTime();
      if (!isFinite(tms)) return;
      let best = 0;
      let bestD = Infinity;
      for (let i = 0; i < tsList.length; i++) {
        const d = Math.abs(tsList[i] - tms);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      }
      // Sparse array aligned with labels
      while (markerData.length < n) markerData.push(null);
      markerData[best] = Number(y);
      markerMeta[best] = { kind, trade: t };
      markerColors[best] =
        kind === "entry" ? CHART_COLORS.entryMark :
        kind === "stop" ? CHART_COLORS.stopMark : CHART_COLORS.closeMark;
      markerStyles[best] =
        kind === "entry" ? "circle" :
        kind === "stop" ? "triangle" : "rectRot";
    };
    pushMark(t.entry_time, t.entry_residual, "entry");
    if (t.close_time) {
      pushMark(t.close_time, t.close_residual, t.close_reason === "stop_loss" ? "stop" : "close");
    }
  });
  while (markerData.length < n) markerData.push(null);

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
    },
    {
      label: "+z entry",
      data: _constantSeries(n, mean + zEntry * std),
      borderColor: CHART_COLORS.entryLine,
      borderDash: [4, 3],
      borderWidth: 1,
      pointRadius: 0,
      fill: false,
    },
    {
      label: "-z entry",
      data: _constantSeries(n, mean - zEntry * std),
      borderColor: CHART_COLORS.entryLine,
      borderDash: [4, 3],
      borderWidth: 1,
      pointRadius: 0,
      fill: false,
    },
    {
      label: "+z close",
      data: _constantSeries(n, mean + zClose * std),
      borderColor: CHART_COLORS.closeLine,
      borderDash: [2, 2],
      borderWidth: 1,
      pointRadius: 0,
      fill: false,
    },
    {
      label: "-z close",
      data: _constantSeries(n, mean - zClose * std),
      borderColor: CHART_COLORS.closeLine,
      borderDash: [2, 2],
      borderWidth: 1,
      pointRadius: 0,
      fill: false,
    },
    {
      label: "+z stop",
      data: _constantSeries(n, mean + zStop * std),
      borderColor: CHART_COLORS.stopLine,
      borderDash: [1, 3],
      borderWidth: 1,
      pointRadius: 0,
      fill: false,
    },
    {
      label: "-z stop",
      data: _constantSeries(n, mean - zStop * std),
      borderColor: CHART_COLORS.stopLine,
      borderDash: [1, 3],
      borderWidth: 1,
      pointRadius: 0,
      fill: false,
    },
  ];

  if (markerMeta.some(Boolean)) {
    datasets.push({
      label: "Trades",
      data: markerData,
      showLine: false,
      pointRadius: markerData.map((v) => (v == null ? 0 : 6)),
      pointHoverRadius: markerData.map((v) => (v == null ? 0 : 8)),
      pointStyle: markerStyles.map((s) => s || "circle"),
      pointBackgroundColor: markerColors.map((c) => c || CHART_COLORS.entryMark),
      pointBorderColor: "#1a1410",
      borderWidth: 0,
      _markerMeta: markerMeta,
    });
  }

  residualChart = _destroyChart(residualChart);

  // Ensure layout size before Chart.js measures the canvas
  canvas.style.width = "100%";
  canvas.style.height = "300px";

  residualChart = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: {
            color: CHART_COLORS.text,
            boxWidth: 12,
            font: { size: 10 },
            filter: (item) => item.text === "Residual" || item.text === "Trades",
          },
        },
        tooltip: {
          callbacks: {
            label: (item) => {
              if (item.dataset.label === "Trades") {
                const meta = item.dataset._markerMeta[item.dataIndex];
                if (!meta) return "";
                const pnl = meta.trade.pnl;
                return `${meta.kind.toUpperCase()} · #${meta.trade.id} · pnl ${pnl != null ? Number(pnl).toFixed(4) : "open"}`;
              }
              return `${item.dataset.label}: ${Number(item.raw).toFixed(5)}`;
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
          if (ds.label === "Trades" && ds._markerMeta) {
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
  if (!canvas) {
    console.error("renderRadarChart: canvas not found", canvasId);
    return;
  }
  if (typeof Chart === "undefined") {
    console.error("renderRadarChart: Chart.js not loaded");
    return;
  }

  canvas.style.width = "100%";
  canvas.style.height = "220px";

  const m = metrics || {};
  const norm = {
    "Win Rate": _finite01(m.win_rate),
    Sharpe: _squash(m.sharpe_ratio),
    Sortino: _squash(m.sortino_ratio),
    "Profit Factor": _squash((Number(m.profit_factor) || 0) / 3),
    "Low Drawdown":
      Number(m.max_drawdown) > 0
        ? _squash(1 / Number(m.max_drawdown))
        : Number(m.trade_count) > 0
          ? 1
          : 0.15,
    "Trade Freq": _squash((Number(m.trade_count) || 0) / 20),
  };

  // Avoid a completely flat zero spider — nudge so the chart is visible even with no trades.
  const vals = Object.values(norm);
  if (vals.every((v) => !v)) {
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

/** Build a synthetic fit payload for UI smoke-tests (no backend). */
function makeDummyFit(n = 60) {
  const now = Date.now();
  const residual_series = [];
  for (let i = 0; i < n; i++) {
    const t = new Date(now - (n - i) * 60_000).toISOString();
    const v = Math.sin(i / 6) * 0.02 + (Math.random() - 0.5) * 0.004;
    residual_series.push([t, v]);
  }
  const values = residual_series.map((p) => p[1]);
  const mean = values.reduce((a, b) => a + b, 0) / n;
  const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(1, n - 1);
  const std = Math.sqrt(variance);
  return {
    id: -1,
    group_id: -1,
    fitted_at: new Date().toISOString(),
    window_start: residual_series[0][0],
    window_end: residual_series[n - 1][0],
    betas: { X: 0.8 },
    intercept: 0.0,
    resid_mean: mean,
    resid_std: std,
    adf_stat: -3.2,
    adf_pvalue: 0.02,
    kpss_stat: 0.1,
    kpss_pvalue: 0.1,
    passed: true,
    residual_series,
    resolution: "1",
    bars_used: n,
    sampling_time: 60,
  };
}
