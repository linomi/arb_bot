/*
 * Residual chart: TradingView Lightweight Charts (vendored, v5) + Chart.js radar
 * (brown/cream terminal theme).
 *
 * `residualChart` is a small state wrapper (chart, series, markers, overlays, destroy()),
 * kept as a module-level global because backbone_overrides.js calls residualChart.destroy().
 */
let residualChart = null;
let radarChart = null;

function _syncResidualGlobal() {
  if (typeof window !== "undefined") window.residualChart = residualChart;
}
function _syncRadarGlobal() {
  if (typeof window !== "undefined") window.radarChart = radarChart;
}

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

const RC_MARKER_STYLE = {
  entry: { shape: "circle", color: CHART_COLORS.entryMark, glyph: "\u25CF", label: "Entry", size: 1.4 },
  close: { shape: "square", color: CHART_COLORS.closeMark, glyph: "\u25A0", label: "Close", size: 1.4 },
  stop: { shape: "arrowDown", color: CHART_COLORS.stopMark, glyph: "\u25BC", label: "Stop", size: 1.4 },
};

function _shortLabel(iso) {
  if (typeof window !== "undefined" && window.TehranTime) {
    return window.TehranTime.shortLabel(iso);
  }
  if (typeof window !== "undefined" && typeof window._shortLabel === "function" && window._shortLabel !== _shortLabel) {
    try { return window._shortLabel(iso); } catch (e) {}
  }
  if (!iso) return "";
  return String(iso).slice(5, 16);
}

function _parseTs(iso) {
  if (typeof window !== "undefined" && window.TehranTime) {
    const d = window.TehranTime.parseAsUtc(iso);
    return d ? d.getTime() : NaN;
  }
  return new Date(iso).getTime();
}

function _destroyChart(chart) {
  if (!chart) return null;
  try { chart.destroy(); } catch (e) {}
  return null;
}

/* ------------------------------------------------------------------ */
/* Residual chart (Lightweight Charts)                                 */
/* ------------------------------------------------------------------ */

function _rcEsc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** Backend timestamps are naive UTC -> integer UTC seconds (what the library wants). */
function _rcTimeSec(iso) {
  const ms = _parseTs(iso);
  return isFinite(ms) ? Math.floor(ms / 1000) : NaN;
}

/** [[iso, value], ...] -> sorted, unique-time [{time, value}] with NaNs dropped. */
function _rcBuildPoints(series) {
  const pts = [];
  for (const p of series || []) {
    if (!p) continue;
    const t = _rcTimeSec(p[0]);
    const v = Number(p[1]);
    if (!isFinite(t) || !isFinite(v)) continue;
    pts.push({ time: t, value: v });
  }
  pts.sort((a, b) => a.time - b.time);
  const out = [];
  for (const pt of pts) {
    if (out.length && out[out.length - 1].time === pt.time) out[out.length - 1] = pt;
    else out.push(pt);
  }
  return out;
}

function _rcBarSpacing(pts) {
  if (pts.length < 2) return 60;
  const d = [];
  for (let i = 1; i < pts.length; i++) d.push(pts[i].time - pts[i - 1].time);
  d.sort((a, b) => a - b);
  return d[Math.floor(d.length / 2)] || 60;
}

function _rcNearestIndex(pts, tSec) {
  let lo = 0;
  let hi = pts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].time < tSec) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(pts[lo - 1].time - tSec) <= Math.abs(pts[lo].time - tSec)) return lo - 1;
  return lo;
}

/**
 * Trade markers, snapped to the nearest bar. Trades further than ~1.5 bar-spacings outside
 * this fit's window are skipped (the old Chart.js code piled them onto the first/last bar).
 */
function _rcBuildMarkers(pts, trades) {
  const tol = _rcBarSpacing(pts) * 1.5;
  const markers = [];
  const byId = new Map();
  const byTime = new Map();
  const add = (kind, trade, iso, residual) => {
    if (iso == null || residual == null) return;
    const tSec = _rcTimeSec(iso);
    const price = Number(residual);
    if (!isFinite(tSec) || !isFinite(price)) return;
    const bar = pts[_rcNearestIndex(pts, tSec)];
    if (!bar || Math.abs(bar.time - tSec) > tol) return;
    const style = RC_MARKER_STYLE[kind];
    const id = `${kind}:${trade.id}`;
    markers.push({
      time: bar.time,
      position: "atPriceMiddle",
      price,
      shape: style.shape,
      color: style.color,
      size: style.size,
      id,
    });
    const info = { id, kind, trade, price, time: bar.time };
    byId.set(id, info);
    if (!byTime.has(bar.time)) byTime.set(bar.time, []);
    byTime.get(bar.time).push(info);
  };
  (trades || []).forEach((t) => {
    if (!t) return;
    add("entry", t, t.entry_time, t.entry_residual);
    add(t.close_reason === "stop_loss" ? "stop" : "close", t, t.close_time, t.close_residual);
  });
  markers.sort((a, b) => a.time - b.time);
  return { markers, byId, byTime };
}

function _rcDecimals(std) {
  const s = Math.abs(Number(std));
  if (!isFinite(s) || s === 0) return 4;
  return Math.max(0, Math.min(8, Math.ceil(-Math.log10(s)) + 2));
}

function _rcFmtTime(tSec, full) {
  const d = new Date(Number(tSec) * 1000);
  if (typeof window !== "undefined" && window.TehranTime) {
    return full ? window.TehranTime.fmtTime(d) : window.TehranTime.shortLabel(d);
  }
  return d.toISOString().slice(5, 16).replace("T", " ");
}

function _rcTickFormatter(time, tickMarkType) {
  const label = _rcFmtTime(time, false); // "MM/DD HH:mm" (Jalali, Asia/Tehran)
  const sp = label.indexOf(" ");
  if (sp < 0) return label;
  const TMT = (typeof LightweightCharts !== "undefined" && LightweightCharts.TickMarkType) || {};
  const isTime = tickMarkType === (TMT.Time !== undefined ? TMT.Time : 3)
    || tickMarkType === (TMT.TimeWithSeconds !== undefined ? TMT.TimeWithSeconds : 4);
  return isTime ? label.slice(sp + 1) : label.slice(0, sp);
}

function _rcLevelSpecs(mean, std, zEntry, zClose, zStop, narrow) {
  const LS = LightweightCharts.LineStyle;
  const specs = [{ price: mean, color: "rgba(196,181,160,0.35)", style: LS.Solid, title: "", axis: false }];
  if (!(std > 0)) return specs;
  const z = (v) => String(Number(v));
  // Close levels sit close together (and near the last-value tag), so they get no axis label;
  // they are identified in the legend. Titles are dropped on narrow screens to keep the plot clear.
  [
    [zEntry, CHART_COLORS.entryLine, LS.Dashed, "Entry", true],
    [zClose, CHART_COLORS.closeLine, LS.Dotted, "Close", false],
    [zStop, CHART_COLORS.stopLine, LS.SparseDotted, "Stop", true],
  ].forEach(([zv, color, style, name, axis]) => {
    specs.push({ price: mean + zv * std, color, style, axis, title: narrow ? "" : `${name} +${z(zv)}\u03C3` });
    specs.push({ price: mean - zv * std, color, style, axis, title: narrow ? "" : `${name} \u2212${z(zv)}\u03C3` });
  });
  return specs;
}

function _rcIsNarrow(rc) {
  return rc.container.clientWidth > 0 && rc.container.clientWidth < 560;
}

function _rcBuildLines(rc) {
  const d = rc.data;
  if (!d) return;
  rc.priceLines.forEach((pl) => { try { rc.series.removePriceLine(pl); } catch (e) {} });
  rc.narrow = _rcIsNarrow(rc);
  rc.priceLines = _rcLevelSpecs(d.mean, d.std, d.zEntry, d.zClose, d.zStop, rc.narrow).map((sp) =>
    rc.series.createPriceLine({
      price: sp.price,
      color: sp.color,
      lineWidth: 1,
      lineStyle: sp.style,
      axisLabelVisible: sp.axis,
      title: sp.title,
    })
  );
}

function _rcRenderLegend(rc, d) {
  const lineKey = (color, label, cls) =>
    `<span class="rc-key${cls ? " " + cls : ""}"><i class="rc-line" style="background:${color}"></i>${_rcEsc(label)}</span>`;
  const parts = [lineKey(CHART_COLORS.residual, "Residual")];
  if (d.std > 0) {
    parts.push(lineKey(CHART_COLORS.entryLine, `Entry \u00B1${d.zEntry}\u03C3`, "rc-key-level"));
    parts.push(lineKey(CHART_COLORS.closeLine, `Close \u00B1${d.zClose}\u03C3`, "rc-key-level"));
    parts.push(lineKey(CHART_COLORS.stopLine, `Stop \u00B1${d.zStop}\u03C3`, "rc-key-level"));
  }
  ["entry", "close", "stop"].forEach((k) => {
    if (d.markerInfo.markers.some((m) => m.id.startsWith(k + ":"))) {
      const st = RC_MARKER_STYLE[k];
      parts.push(
        `<span class="rc-key"><i class="rc-glyph" style="color:${st.color}">${st.glyph}</i>${st.label}</span>`
      );
    }
  });
  rc.legendEl.innerHTML =
    `<div class="rc-legend-items">${parts.join("")}</div>` +
    `<button type="button" class="rc-fit" title="Fit all bars">Fit</button>`;
  const btn = rc.legendEl.querySelector(".rc-fit");
  if (btn) {
    btn.addEventListener("click", () => {
      rc.userMoved = false;
      try { rc.chart.timeScale().fitContent(); } catch (e) {}
    });
  }
}

function _rcHideTip(rc) {
  if (rc.tipEl) rc.tipEl.style.display = "none";
}

function _rcOnMove(rc, param) {
  if (rc.destroyed || !rc.data) return;
  if (!param || !param.point || param.time === undefined || param.point.x < 0 || param.point.y < 0) {
    _rcHideTip(rc);
    return;
  }
  const sd = param.seriesData && param.seriesData.get(rc.series);
  if (!sd || sd.value == null) {
    _rcHideTip(rc);
    return;
  }
  const d = rc.data;
  const dec = Math.min(8, d.decimals + 1);
  const z = d.std > 0 ? (sd.value - d.mean) / d.std : null;
  let html = `<div class="rc-t-time">${_rcEsc(_rcFmtTime(param.time, true))}</div>` +
    `<div>Residual <b>${sd.value.toFixed(dec)}</b></div>`;
  if (z != null) html += `<div>z <b>${(z >= 0 ? "+" : "") + z.toFixed(2)}</b></div>`;
  (d.markerInfo.byTime.get(param.time) || []).forEach((m) => {
    const st = RC_MARKER_STYLE[m.kind];
    const pnl = m.trade.pnl;
    html += `<div class="rc-t-marker" style="color:${st.color}">${st.label.toUpperCase()} \u00B7 #${
      _rcEsc(m.trade.id)} \u00B7 pnl ${pnl != null ? Number(pnl).toFixed(4) : "open"}</div>`;
  });
  const tip = rc.tipEl;
  tip.innerHTML = html;
  tip.style.display = "block";
  const cw = rc.container.clientWidth;
  const ch = rc.container.clientHeight;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let left = param.point.x + 14;
  let top = param.point.y + 14;
  if (left + tw > cw) left = Math.max(0, param.point.x - 14 - tw);
  if (top + th > ch) top = Math.max(0, param.point.y - 14 - th);
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function _rcOnClick(rc, param) {
  if (rc.destroyed || !rc.data || !rc.onMarkerClick || !param) return;
  const d = rc.data;
  let hit = null;
  if (param.hoveredObjectId != null) hit = d.markerInfo.byId.get(String(param.hoveredObjectId)) || null;
  if (!hit && param.time !== undefined && param.point) {
    let bestD = Infinity;
    (d.markerInfo.byTime.get(param.time) || []).forEach((m) => {
      const y = rc.series.priceToCoordinate(m.price);
      if (y == null) return;
      const dist = Math.abs(y - param.point.y);
      if (dist < bestD) { bestD = dist; hit = m; }
    });
    if (bestD > 12) hit = null;
  }
  if (hit) rc.onMarkerClick(hit.trade);
}

function _rcCreate(el, key) {
  const LW = LightweightCharts;
  const fontVar = getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim();
  const grid = "rgba(74,60,48,0.55)";
  const chart = LW.createChart(el, {
    autoSize: true,
    layout: {
      background: { type: LW.ColorType.Solid, color: "transparent" },
      textColor: CHART_COLORS.text,
      fontSize: 11,
      fontFamily: fontVar || "ui-monospace, Menlo, monospace",
    },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    crosshair: {
      mode: LW.CrosshairMode.Normal,
      vertLine: { color: "rgba(196,181,160,0.55)", width: 1, style: LW.LineStyle.Dashed, labelBackgroundColor: "#3a2e24" },
      horzLine: { color: "rgba(196,181,160,0.55)", width: 1, style: LW.LineStyle.Dashed, labelBackgroundColor: "#3a2e24" },
    },
    rightPriceScale: { borderColor: CHART_COLORS.grid, scaleMargins: { top: 0.14, bottom: 0.08 } },
    timeScale: {
      borderColor: CHART_COLORS.grid,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 3,
      tickMarkFormatter: _rcTickFormatter,
    },
    localization: { timeFormatter: (t) => _rcFmtTime(t, true) },
  });

  const rc = {
    key,
    container: el,
    chart,
    series: null,
    markersApi: null,
    priceLines: [],
    data: null,
    sig: null,
    hasData: false,
    lastTime: null,
    userMoved: false,
    onMarkerClick: null,
    destroyed: false,
    levelRange: null,
    narrow: false,
    legendEl: document.createElement("div"),
    tipEl: document.createElement("div"),
  };
  rc.legendEl.className = "rc-legend";
  rc.tipEl.className = "rc-tooltip";
  rc.tipEl.style.display = "none";
  el.appendChild(rc.legendEl);
  el.appendChild(rc.tipEl);

  rc.series = chart.addSeries(LW.LineSeries, {
    color: CHART_COLORS.residual,
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
    crosshairMarkerRadius: 4,
    // Price lines don't take part in autoscale; keep the stop levels in view like the old chart did.
    autoscaleInfoProvider: (base) => {
      const b = base();
      const r = rc.levelRange;
      if (!r) return b;
      if (!b || !b.priceRange) return { priceRange: { minValue: r.lo, maxValue: r.hi } };
      return {
        priceRange: {
          minValue: Math.min(b.priceRange.minValue, r.lo),
          maxValue: Math.max(b.priceRange.maxValue, r.hi),
        },
        margins: b.margins,
      };
    },
  });
  rc.markersApi = LW.createSeriesMarkers(rc.series, []);

  rc._onMove = (p) => _rcOnMove(rc, p);
  rc._onClick = (p) => _rcOnClick(rc, p);
  chart.subscribeCrosshairMove(rc._onMove);
  chart.subscribeClick(rc._onClick);
  rc._onSize = () => {
    if (rc.hasData && _rcIsNarrow(rc) !== rc.narrow) _rcBuildLines(rc);
    if (!rc.userMoved && rc.hasData) chart.timeScale().fitContent();
  };
  chart.timeScale().subscribeSizeChange(rc._onSize);

  // Remember that the user panned/zoomed so periodic refreshes don't snap the view back.
  let downX = null;
  rc._onPointerDown = (e) => { downX = e.clientX; };
  rc._onPointerMove = (e) => {
    if (downX != null && e.buttons && Math.abs(e.clientX - downX) > 3) rc.userMoved = true;
  };
  rc._onPointerUp = () => { downX = null; };
  rc._onWheel = () => { rc.userMoved = true; };
  rc._onLeave = () => _rcHideTip(rc);
  el.addEventListener("pointerdown", rc._onPointerDown);
  el.addEventListener("pointermove", rc._onPointerMove);
  window.addEventListener("pointerup", rc._onPointerUp);
  el.addEventListener("wheel", rc._onWheel, { passive: true });
  el.addEventListener("mouseleave", rc._onLeave);

  rc.destroy = function () {
    if (rc.destroyed) return;
    rc.destroyed = true;
    try { chart.unsubscribeCrosshairMove(rc._onMove); } catch (e) {}
    try { chart.unsubscribeClick(rc._onClick); } catch (e) {}
    try { chart.timeScale().unsubscribeSizeChange(rc._onSize); } catch (e) {}
    el.removeEventListener("pointerdown", rc._onPointerDown);
    el.removeEventListener("pointermove", rc._onPointerMove);
    window.removeEventListener("pointerup", rc._onPointerUp);
    el.removeEventListener("wheel", rc._onWheel);
    el.removeEventListener("mouseleave", rc._onLeave);
    try { rc.markersApi.detach(); } catch (e) {}
    try { chart.remove(); } catch (e) {}
    if (rc.legendEl.parentNode) rc.legendEl.parentNode.removeChild(rc.legendEl);
    if (rc.tipEl.parentNode) rc.tipEl.parentNode.removeChild(rc.tipEl);
  };
  return rc;
}

function _rcApply(rc, d) {
  const ts = rc.chart.timeScale();
  const prevRange = rc.hasData ? ts.getVisibleRange() : null;
  const prevLast = rc.lastTime;

  rc.data = d;
  rc.levelRange = d.std > 0
    ? { lo: d.mean - d.zStop * d.std, hi: d.mean + d.zStop * d.std }
    : null;

  const dec = d.decimals;
  rc.series.applyOptions({
    priceFormat: { type: "custom", minMove: Math.pow(10, -dec), formatter: (p) => Number(p).toFixed(dec) },
  });
  rc.series.setData(d.pts);

  _rcBuildLines(rc);
  rc.markersApi.setMarkers(d.markerInfo.markers);
  _rcRenderLegend(rc, d);

  const newLast = d.pts[d.pts.length - 1].time;
  if (rc.userMoved && prevRange) {
    let { from, to } = prevRange;
    // If the user was parked at the live edge, keep following it as new bars arrive.
    if (prevLast != null && newLast > prevLast && to >= prevLast - d.spacing * 0.5) {
      const shift = newLast - prevLast;
      from += shift;
      to += shift;
    }
    try { ts.setVisibleRange({ from, to }); } catch (e) { ts.fitContent(); }
  } else {
    ts.fitContent();
  }
  rc.hasData = true;
  rc.lastTime = newLast;
}

function _rcDestroy() {
  if (residualChart) {
    try { residualChart.destroy(); } catch (e) {}
  }
  residualChart = null;
  _syncResidualGlobal();
  return null;
}

/**
 * Same contract as the old Chart.js renderer. `canvasId` is now the id of a <div>
 * (a stale <canvas> with that id is swapped for a div automatically).
 * Pass `fit._pinned_trade_id` to show a stored fit for a trade; it gets its own view state.
 */
function renderResidualChart(canvasId, fit, backboneCfg, trades, onMarkerClick) {
  let el = document.getElementById(canvasId);
  if (!el) return;
  const pts = _rcBuildPoints(fit && fit.residual_series);
  if (!pts.length) {
    residualChart = _rcDestroy();
    return;
  }
  if (typeof LightweightCharts === "undefined") {
    console.error("Lightweight Charts failed to load; residual chart unavailable");
    return;
  }
  if (el.tagName === "CANVAS") {
    const div = document.createElement("div");
    div.id = el.id;
    el.replaceWith(div);
    el = div;
  }

  const cfg = backboneCfg || {};
  const mean = Number(fit.resid_mean) || 0;
  const std = Number(fit.resid_std) || 0;
  const zEntry = Number(cfg.z_entry) || 2;
  const zClose = Number(cfg.z_close) || 0.5;
  const zStop = Number(cfg.z_stop_loss) || 3.5;
  const markerInfo = _rcBuildMarkers(pts, trades);
  const d = {
    pts, mean, std, zEntry, zClose, zStop, markerInfo,
    decimals: _rcDecimals(std),
    spacing: _rcBarSpacing(pts),
  };
  const key = `${fit.group_id != null ? fit.group_id : (fit.group_name || "")}:${
    fit._pinned_trade_id != null ? fit._pinned_trade_id : "live"}`;
  const last = pts[pts.length - 1];
  const sig = JSON.stringify([
    key, pts.length, pts[0].time, last.time, last.value, mean, std, zEntry, zClose, zStop,
    markerInfo.markers.map((m) => [m.id, m.time, m.price]),
  ]);

  let rc = residualChart;
  if (rc && (rc.destroyed || rc.container !== el || !el.isConnected || rc.key !== key)) {
    rc = residualChart = _rcDestroy();
  }
  if (rc && rc.sig === sig) {
    // Periodic refresh with identical data: nothing to redraw (keeps zoom/pan untouched).
    rc.onMarkerClick = onMarkerClick || null;
    residualChart = rc;
    _syncResidualGlobal();
    return;
  }
  if (!rc) rc = residualChart = _rcCreate(el, key);
  rc.onMarkerClick = onMarkerClick || null;
  _rcApply(rc, d);
  rc.sig = sig;
  residualChart = rc;
  _syncResidualGlobal();
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


/* Explicit window exports (classic scripts share lexical scope, but keep these for clarity). */
if (typeof window !== "undefined") {
  window.renderResidualChart = renderResidualChart;
  window.renderRadarChart = renderRadarChart;
  window.residualChart = residualChart;
  window.radarChart = radarChart;
}
