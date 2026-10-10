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

const RC_OUTSIDE_COLOR = "rgba(217,148,90,0.5)";

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

/* ---- per-group raw price cache: residuals are recomputed from prices for whatever betas are shown ---- */
const _RC_PRICES = {};
const RC_CHUNK_BARS = 500;
const RC_MAX_BARS = 30000;

function _rcCache(gid) {
  return (_RC_PRICES[gid] = _RC_PRICES[gid] || { rows: new Map(), exhausted: false, loading: false });
}

function _rcMergePrices(gid, prices) {
  if (!prices || !prices.t || !prices.t.length) return 0;
  const cache = _rcCache(gid);
  let added = 0;
  const syms = Object.keys(prices.c || {});
  prices.t.forEach((t, i) => {
    let row = cache.rows.get(t);
    if (!row) { row = {}; cache.rows.set(t, row); added++; }
    syms.forEach((sy) => { row[sy] = prices.c[sy][i]; });
  });
  return added;
}

/** [[iso, residual]] for every cached bar under one fixed set of betas. */
function _rcResidualsFromCache(gid, fit) {
  const cache = _RC_PRICES[gid];
  const betas = fit.betas || {};
  const dep = fit.dependent_symbol;
  if (!cache || !dep || !Object.keys(betas).length) return null;
  const times = [...cache.rows.keys()].sort((a, b) => a - b);
  const syms = Object.keys(betas);
  const out = [];
  for (const t of times) {
    const row = cache.rows.get(t);
    let y = row[dep];
    if (y == null) continue;
    let x = Number(fit.intercept) || 0;
    let ok = true;
    for (const sy of syms) {
      const v = row[sy];
      if (v == null) { ok = false; break; }
      x += Number(betas[sy]) * v;
    }
    if (ok) out.push([new Date(t * 1000).toISOString(), y - x]);
  }
  return out.length ? out : null;
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
    // Draw on the line itself: under a different set of betas the trade's recorded residual
    // would float off it. The recorded value stays in `info.price`.
    const style = RC_MARKER_STYLE[kind];
    const id = `${kind}:${trade.id}`;
    markers.push({
      time: bar.time,
      position: "atPriceMiddle",
      price: bar.value,
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

/** Fit all bars, leaving room on the right so the level titles don't cover the latest bars. */
function _rcFit(rc) {
  const ts = rc.chart.timeScale();
  const n = rc.data ? rc.data.pts.length : 0;
  const w = rc.container.clientWidth;
  if (!n || !(w > 0)) { try { ts.fitContent(); } catch (e) {} return; }
  const labelPx = rc.narrow ? 0 : RC_LABEL_PX;
  const pad = labelPx > 0 && w > labelPx * 2 ? (n * labelPx) / (w - labelPx) : 0;
  try { ts.setVisibleLogicalRange({ from: -0.5, to: n - 1 + pad }); } catch (e) { ts.fitContent(); }
}

const RC_LABEL_PX = 104;

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
  if (d.winStart != null && d.pts[0].time < d.winStart) {
    parts.push(lineKey(RC_OUTSIDE_COLOR, t("plot.legend.outside"), "rc-key-level"));
  }
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
  if (rc.statusText) _rcSetStatus(rc, rc.statusText);
  const btn = rc.legendEl.querySelector(".rc-fit");
  if (btn) {
    btn.addEventListener("click", () => {
      rc.userMoved = false;
      _rcFit(rc);
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
    if (!rc.userMoved && rc.hasData) _rcFit(rc);
  };
  chart.timeScale().subscribeSizeChange(rc._onSize);
  rc._onRange = (r) => { _rcMaybeLoadOlder(rc, r); };
  chart.timeScale().subscribeVisibleLogicalRangeChange(rc._onRange);

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
    _rcStopMorph(rc);
    try { chart.unsubscribeCrosshairMove(rc._onMove); } catch (e) {}
    try { chart.unsubscribeClick(rc._onClick); } catch (e) {}
    try { chart.timeScale().unsubscribeSizeChange(rc._onSize); } catch (e) {}
    try { chart.timeScale().unsubscribeVisibleLogicalRangeChange(rc._onRange); } catch (e) {}
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

/** First index in a sorted [{time}] array whose time is >= t (or length). */
function _rcLowerBound(pts, t) {
  let lo = 0, hi = pts.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].time < t) lo = mid + 1; else hi = mid;
  }
  return lo;
}

const RC_MORPH_MS = 480;
const _rcEase = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

function _rcStopMorph(rc) {
  if (rc._morphRaf) { cancelAnimationFrame(rc._morphRaf); rc._morphRaf = null; }
}

/** Points of the new series with each value pulled back towards the old value at that time. */
function _rcMixPoints(newPts, oldMap, k) {
  if (k >= 1) return newPts;
  return newPts.map((p) => {
    const o = oldMap.get(p.time);
    return o === undefined ? p : { time: p.time, value: o + (p.value - o) * k, color: p.color };
  });
}

function _rcApply(rc, d, opts) {
  opts = opts || {};
  _rcStopMorph(rc);
  const ts = rc.chart.timeScale();
  const old = rc.hasData ? rc.data : null;
  // Logical (bar-index) range survives prepended history and value changes; time ranges don't
  // (getVisibleRange clamps to the loaded data, which made the view jump back after loading).
  let prevLogical = null;
  try { prevLogical = old ? ts.getVisibleLogicalRange() : null; } catch (e) { prevLogical = null; }
  const oldN = old ? old.pts.length : 0;
  const atEdge = !!(prevLogical && oldN && prevLogical.to >= oldN - 1 - 0.5);

  const oldMap = new Map();
  let changed = false;
  if (old && !opts.noMorph) {
    old.pts.forEach((p) => oldMap.set(p.time, p.value));
    let diffs = 0;
    d.pts.forEach((p) => {
      const o = oldMap.get(p.time);
      if (o !== undefined && Math.abs(o - p.value) > 1e-9 * (1 + Math.abs(o))) diffs++;
    });
    changed = diffs > 2; // a live bar updating its last value is not worth animating
  }
  const from = changed ? old : null;

  rc.data = d;
  rc.levelRange = d.std > 0
    ? { lo: d.mean - d.zStop * d.std, hi: d.mean + d.zStop * d.std }
    : null;

  const dec = d.decimals;
  rc.series.applyOptions({
    priceFormat: { type: "custom", minMove: Math.pow(10, -dec), formatter: (p) => Number(p).toFixed(dec) },
  });
  rc.series.setData(from ? _rcMixPoints(d.pts, oldMap, 0) : d.pts);
  if (from) {
    // Level lines start at the old levels and glide to the new ones with the series.
    rc.data = { ...d, mean: from.mean, std: from.std, zEntry: from.zEntry, zClose: from.zClose, zStop: from.zStop };
  }
  _rcBuildLines(rc);
  rc.markersApi.setMarkers(d.markerInfo.markers);
  _rcRenderLegend(rc, d);

  const newN = d.pts.length;
  const newLast = d.pts[newN - 1].time;
  if (rc.userMoved && prevLogical && old) {
    const prepended = _rcLowerBound(d.pts, old.pts[0].time);
    const appended = Math.max(0, newN - prepended - oldN);
    const shift = prepended + (atEdge ? appended : 0);
    try {
      ts.setVisibleLogicalRange({ from: prevLogical.from + shift, to: prevLogical.to + shift });
    } catch (e) { _rcFit(rc); }
  } else {
    _rcFit(rc);
  }
  rc.hasData = true;
  rc.lastTime = newLast;

  if (from) {
    const t0 = performance.now();
    const step = (now) => {
      if (rc.destroyed) return;
      const x = Math.min(1, (now - t0) / RC_MORPH_MS);
      const k = _rcEase(x);
      rc.series.setData(_rcMixPoints(d.pts, oldMap, k));
      const lerp = (a, b) => a + (b - a) * k;
      rc.data = x >= 1 ? d : {
        ...d, mean: lerp(from.mean, d.mean), std: lerp(from.std, d.std),
      };
      _rcBuildLines(rc);
      if (x < 1) rc._morphRaf = requestAnimationFrame(step);
      else { rc._morphRaf = null; rc.data = d; }
    };
    rc._morphRaf = requestAnimationFrame(step);
  }
}

function _rcDestroy() {
  if (residualChart) {
    try { residualChart.destroy(); } catch (e) {}
  }
  residualChart = null;
  _syncResidualGlobal();
  return null;
}

function _rcMakeData(fit, cfg, trades) {
  const gid = fit.group_id;
  if (fit.prices && gid != null) _rcMergePrices(gid, fit.prices);
  const fromCache = gid != null ? _rcResidualsFromCache(gid, fit) : null;
  const pts = _rcBuildPoints(fromCache || fit.residual_series_full || fit.residual_series);
  if (!pts.length) return null;
  const mean = Number(fit.resid_mean) || 0;
  const std = Number(fit.resid_std) || 0;
  const zEntry = Number(cfg.z_entry) || 2;
  const zClose = Number(cfg.z_close) || 0.5;
  const zStop = Number(cfg.z_stop_loss) || 3.5;
  // Bars before the fit window are drawn dimmer: the betas were estimated on the bars after it.
  const winStart = fit.window_start ? _rcTimeSec(fit.window_start) : NaN;
  if (isFinite(winStart) && pts[0].time < winStart) {
    pts.forEach((p) => { if (p.time < winStart) p.color = RC_OUTSIDE_COLOR; });
  }
  const markerInfo = _rcBuildMarkers(pts, trades);
  return {
    winStart: isFinite(winStart) ? winStart : null,
    pts, mean, std, zEntry, zClose, zStop, markerInfo,
    decimals: _rcDecimals(std),
    spacing: _rcBarSpacing(pts),
  };
}

function _rcSig(key, d) {
  const last = d.pts[d.pts.length - 1];
  return JSON.stringify([
    key, d.pts.length, d.pts[0].time, last.time, last.value, d.mean, d.std, d.zEntry, d.zClose, d.zStop,
    d.markerInfo.markers.map((m) => [m.id, m.time, m.price]),
  ]);
}

function _rcSetStatus(rc, text) {
  rc.statusText = text || "";
  let el = rc.legendEl.querySelector(".rc-status");
  if (!el) {
    el = document.createElement("span");
    el.className = "rc-status";
    const items = rc.legendEl.querySelector(".rc-legend-items");
    (items || rc.legendEl).appendChild(el);
  }
  el.textContent = rc.statusText;
}

/** Scrolled / zoomed near the left edge: pull one older chunk of prices and redraw. */
async function _rcMaybeLoadOlder(rc, range) {
  if (rc.destroyed || !range || !rc.userMoved || !rc.data || rc.gid == null) return;
  if (range.from > 25) return;
  const cache = _rcCache(rc.gid);
  if (cache.loading || cache.exhausted) return;
  if (cache.rows.size >= RC_MAX_BARS) { cache.exhausted = true; _rcSetStatus(rc, t("plot.history.start")); return; }
  if (typeof API === "undefined" || !API.olderPrices) return;
  cache.loading = true;
  _rcSetStatus(rc, t("plot.history.loading"));
  try {
    const first = Math.min(...cache.rows.keys());
    const chunk = await API.olderPrices(rc.gid, first, RC_CHUNK_BARS);
    const added = _rcMergePrices(rc.gid, chunk);
    if (!added || chunk.exhausted) cache.exhausted = true;
    if (rc.destroyed) return;
    const d = _rcMakeData(rc.fit, rc.cfg, rc.trades);
    if (d) {
      _rcApply(rc, d);
      rc.sig = _rcSig(`${rc.key}:${rc.variant}`, d);
    }
    _rcSetStatus(rc, cache.exhausted ? t("plot.history.start") : "");
  } catch (e) {
    console.warn("older history", e);
    if (!rc.destroyed) _rcSetStatus(rc, t("plot.history.failed"));
  } finally {
    cache.loading = false;
  }
}

/**
 * Same contract as the old Chart.js renderer. `canvasId` is now the id of a <div>
 * (a stale <canvas> with that id is swapped for a div automatically).
 * Pass `fit._pinned_trade_id` to show a stored fit for a trade; it gets its own view state.
 */
function renderResidualChart(canvasId, fit, backboneCfg, trades, onMarkerClick) {
  let el = document.getElementById(canvasId);
  if (!el) return;
  const cfg = backboneCfg || {};
  const d = _rcMakeData(fit, cfg, trades);
  if (!d) {
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
  // The chart instance is per group; switching betas (live <-> a trade's entry fit) keeps the
  // instance, so pan/zoom survive and the series morphs instead of being rebuilt.
  const key = `${fit.group_id != null ? fit.group_id : (fit.group_name || "")}`;
  const variant = fit._pinned_trade_id != null ? fit._pinned_trade_id : "live";
  const sig = _rcSig(`${key}:${variant}`, d);

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
  rc.gid = fit.group_id != null ? fit.group_id : null;
  rc.fit = fit;
  rc.cfg = cfg;
  rc.trades = trades;
  rc.variant = variant;
  _rcApply(rc, d);
  rc.sig = sig;
  const cache = rc.gid != null ? _RC_PRICES[rc.gid] : null;
  _rcSetStatus(rc, cache && cache.exhausted ? t("plot.history.start") : "");
  residualChart = rc;
  _syncResidualGlobal();
}

function destroyRadarChart() {
  radarChart = _destroyChart(radarChart);
  _syncRadarGlobal();
}

/**
 * Radar of one group's trade report. Every axis is scale-free and 0..1 (outer edge = better),
 * so groups with different trade sizes are comparable. Raw values show in the tooltip.
 */
function radarAxes(m) {
  m = m || {};
  const n = Number(m.total_trades) || 0;
  const gp = Number(m.gross_profit) || 0;
  const gl = Math.abs(Number(m.gross_loss) || 0);
  const turnover = gp + gl;
  const net = Number(m.total_net_profit) || 0;
  const pf = Number(m.profit_factor) || 0;
  const dd = Number(m.maximal_drawdown) || 0;
  const longs = Number(m.long_positions) || 0;
  const shorts = Number(m.short_positions) || 0;
  const hasData = n > 0;
  return [
    { label: t("perf.radar.win_rate"), v: _finite01(m.win_rate), raw: fmtPct(m.win_rate) },
    { label: t("perf.radar.profit_factor"), v: _finite01(Math.min(pf, 3) / 3), raw: fmtPF(m.profit_factor, n) },
    { label: t("perf.radar.net_edge"), v: turnover > 0 ? _finite01((net / turnover + 1) / 2) : 0, raw: fmtNum(net) },
    { label: t("perf.radar.drawdown"), v: hasData ? (turnover > 0 ? _finite01(1 - dd / turnover) : 1) : 0, raw: fmtNum(dd) },
    { label: t("perf.radar.streaks"), v: hasData ? _finite01(1 - (Number(m.consecutive_losses) || 0) / n) : 0, raw: String(Number(m.consecutive_losses) || 0) },
    { label: t("perf.radar.balance"), v: hasData ? _finite01(1 - Math.abs(longs - shorts) / n) : 0, raw: `${longs}L / ${shorts}S` },
  ];
}

function renderRadarChart(canvasId, metrics) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === "undefined") return;
  const axes = radarAxes(metrics);
  radarChart = _destroyChart(radarChart);
  radarChart = new Chart(canvas.getContext("2d"), {
    type: "radar",
    data: {
      labels: axes.map((a) => a.label),
      datasets: [
        {
          label: t("perf.radar.title"),
          data: axes.map((a) => a.v),
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
      layout: { padding: { left: 18, right: 18, top: 4, bottom: 4 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => `${axes[ctx.dataIndex].raw}  (${Math.round(ctx.parsed.r * 100)}%)`,
          },
        },
      },
      scales: {
        r: {
          angleLines: { color: CHART_COLORS.grid },
          grid: { color: CHART_COLORS.grid },
          pointLabels: { color: CHART_COLORS.text, font: { size: 10 }, padding: 4 },
          ticks: { display: false, stepSize: 0.25 },
          min: 0,
          max: 1,
        },
      },
    },
  });
  _syncRadarGlobal();
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
  window.destroyRadarChart = destroyRadarChart;
  window.residualChart = residualChart;
  window.radarChart = radarChart;
}
