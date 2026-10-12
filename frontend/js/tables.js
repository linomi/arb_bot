const PerfTableState = {
  rows: [],
  filtered: [],
  sortKey: "total_net_profit",
  sortDir: "desc",
  filters: {},
  expanded: new Set(),
};

function statusLabel(st) {
  const key = "status." + String(st || "").toLowerCase();
  const tr = typeof t === "function" ? t(key) : key;
  return tr && tr !== key ? tr : String(st || "--");
}

/** Shared comparator: numbers numerically, text case-insensitively, null/NaN last. */
function _cmp(av, bv, dir) {
  const an = av == null || (typeof av === "number" && !isFinite(av));
  const bn = bv == null || (typeof bv === "number" && !isFinite(bv));
  if (an && bn) return 0;
  if (an) return 1;
  if (bn) return -1;
  let c;
  if (typeof av === "number" && typeof bv === "number") c = av - bv;
  else c = String(av).localeCompare(String(bv), undefined, { sensitivity: "base", numeric: true });
  return dir === "asc" ? c : -c;
}

function _setSortHeaders(tableId, key, dir) {
  document.querySelectorAll(`#${tableId} thead th[data-key]`).forEach((th) => {
    th.classList.add("sortable");
    th.classList.remove("sort-asc", "sort-desc");
    th.setAttribute("aria-sort", "none");
    if (th.dataset.key === key) {
      th.classList.add(dir === "asc" ? "sort-asc" : "sort-desc");
      th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
    }
  });
}

function fmtTime(iso) {
  if (typeof window !== "undefined" && window.TehranTime) return window.TehranTime.fmtTime(iso);
  if (!iso) return "--";
  return String(iso).slice(0, 16);
}

function fmtNum(v, digits = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  if (!isFinite(v)) return v > 0 ? "+inf" : "-inf";
  return Number(v).toFixed(digits);
}
function fmtPct(v) {
  if (v === null || v === undefined) return "--";
  return (v * 100).toFixed(1) + "%";
}
function pnlClass(v) {
  if (v === null || v === undefined) return "";
  return v >= 0 ? "num-pos" : "num-neg";
}

/** Live trades must use exchange realized_pnl only. */
function displayTradePnl(t) {
  if (!t) return null;
  if (t.mode === "live") {
    if (t.realized_pnl != null && isFinite(Number(t.realized_pnl))) return Number(t.realized_pnl);
    if (t.pnl != null && t.pnl_source === "exchange" && isFinite(Number(t.pnl))) return Number(t.pnl);
    return null;
  }
  if (t.pnl != null && isFinite(Number(t.pnl))) return Number(t.pnl);
  if (t.model_pnl != null && isFinite(Number(t.model_pnl))) return Number(t.model_pnl);
  return null;
}

const _DIAG_OK = new Set(["entered", "signal", "position_open"]);
const _DIAG_WARN = new Set(["waiting_z", "cooldown", "paused", "bot_stopped"]);
function diagBadge(d) {
  if (!d || !d.stage) return '<span class="diag-badge diag-none">--</span>';
  const key = "diag.stage." + d.stage;
  const label = t(key) !== key ? t(key) : d.stage;
  const cls = _DIAG_OK.has(d.stage) ? "diag-ok" : _DIAG_WARN.has(d.stage) ? "diag-warn" : "diag-bad";
  const lines = [];
  if (d.text) lines.push(d.text);
  (d.blockers || []).forEach((b) => lines.push("• " + b));
  if (d.z != null) lines.push("z=" + Number(d.z).toFixed(2) + (d.z_entry != null ? " / entry " + d.z_entry : ""));
  if (d.at) lines.push(fmtTime(d.at));
  return `<span class="diag-badge ${cls}" title="${_esc(lines.join("\n"))}">${_esc(label)}</span>`;
}

function renderPerfTable(rows, onRowClick) {
  PerfTableState.rows = rows || [];
  _applyPerfFilters();
  _redrawPerfTable(onRowClick);
  _updateSortIcons();
}

function _applyPerfFilters() {
  const filters = PerfTableState.filters;
  const keys = Object.keys(filters).filter((k) => filters[k] && filters[k].value !== "" && filters[k].value != null);
  if (!keys.length) {
    PerfTableState.filtered = [...PerfTableState.rows];
    return;
  }
  PerfTableState.filtered = PerfTableState.rows.filter((row) => {
    return keys.every((key) => {
      const f = filters[key];
      if (key === "_search") {
        const hay = `${row.name} ${(row.symbols || []).join(" ")}`.toLowerCase();
        return hay.includes(String(f.value).toLowerCase());
      }
      const raw = row[key];
      if (f.op === "=" || f.op === "eq") {
        return String(raw).toLowerCase() === String(f.value).toLowerCase();
      }
      const num = Number(raw);
      const target = Number(f.value);
      if (!isFinite(num) || !isFinite(target)) return false;
      if (f.op === ">") return num > target;
      if (f.op === ">=") return num >= target;
      if (f.op === "<") return num < target;
      if (f.op === "<=") return num <= target;
      return true;
    });
  });
}

function _redrawPerfTable(onRowClick) {
  const { filtered, sortKey, sortDir } = PerfTableState;
  const sorted = [...filtered].sort((a, b) => _cmp(a[sortKey], b[sortKey], sortDir));

  const tbody = document.querySelector("#perf-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const countEl = document.getElementById("perf-count");
  if (countEl) countEl.textContent = t("perf.count", { shown: sorted.length, total: PerfTableState.rows.length });
  if (!sorted.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="10" class="table-empty">${t(PerfTableState.rows.length ? "perf.empty.filtered" : "perf.empty")}</td>`;
    tbody.appendChild(tr);
  }
  sorted.forEach((r) => {
    const gid = r.group_id != null ? r.group_id : r.id;
    const isActive = String(r.status).toLowerCase() === "active";
    const toggleLabel = isActive ? t("perf.deactivate") : t("perf.activate");
    const toggleCls = isActive ? "btn-secondary perf-toggle" : "btn-primary perf-toggle";
    const srcNote = r.pnl_source === "exchange" ? "" : "";

    const tr = document.createElement("tr");
    if (typeof state !== "undefined" && state.selectedGroupId === gid) {
      tr.classList.add("selected-row");
    }
    const open = PerfTableState.expanded.has(gid);
    tr.innerHTML = `
      <td class="perf-name"><button type="button" class="perf-expand${open ? " open" : ""}" aria-expanded="${open}" title="${t("perf.expand.title")}">&#9656;</button>${_esc(r.name)}</td>
      <td><span class="g-status ${r.status}">${statusLabel(r.status)}</span></td>
      <td>${diagBadge(r.diag)}</td>
      <td>${r.total_trades ?? 0}</td>
      <td>${fmtPct(r.win_rate)}</td>
      <td>${fmtPF(r.profit_factor, r.total_trades)}</td>
      <td class="${pnlClass(r.total_net_profit)}">${fmtNum(r.total_net_profit)}</td>
      <td class="${pnlClass(r.expected_payoff)}">${fmtNum(r.expected_payoff)}</td>
      <td>${fmtNum(r.maximal_drawdown)}</td>
      <td class="col-actions">
        <div class="perf-actions">
          <button type="button" class="${toggleCls}" data-id="${gid}" data-status="${r.status}">${toggleLabel}</button>
          <button type="button" class="btn-danger perf-del" data-id="${gid}" title="${t("perf.remove.title")}">${t("perf.remove")}</button>
        </div>
      </td>
    `;

    tr.addEventListener("click", (e) => {
      if (e.target.closest(".perf-del, .perf-toggle, .perf-expand")) return;
      tbody.querySelectorAll("tr.selected-row").forEach((x) => x.classList.remove("selected-row"));
      tr.classList.add("selected-row");
      if (typeof onRowClick === "function") onRowClick(r);
    });

    const toggleBtn = tr.querySelector(".perf-toggle");
    if (toggleBtn) {
      toggleBtn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const next = isActive ? "inactive" : "active";
        if (typeof toggleGroupStatusFromTable === "function") {
          await toggleGroupStatusFromTable(gid, next, r.name);
        } else if (typeof API !== "undefined" && API.setGroupStatus) {
          await API.setGroupStatus(gid, next);
          if (typeof loadPerfTable === "function") await loadPerfTable();
          if (typeof loadGroupList === "function") await loadGroupList();
        }
      });
    }

    const delBtn = tr.querySelector(".perf-del");
    if (delBtn) {
      delBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (typeof deleteGroupFromTable === "function") {
          deleteGroupFromTable(gid, r.name);
        }
      });
    }

    tbody.appendChild(tr);

    const extra = document.createElement("tr");
    extra.className = "perf-extra";
    extra.hidden = !open;
    extra.innerHTML = `<td colspan="10">${perfExtraHtml(r)}</td>`;
    tbody.appendChild(extra);

    const expBtn = tr.querySelector(".perf-expand");
    expBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const nowOpen = extra.hidden;
      extra.hidden = !nowOpen;
      expBtn.classList.toggle("open", nowOpen);
      expBtn.setAttribute("aria-expanded", String(nowOpen));
      if (nowOpen) PerfTableState.expanded.add(gid); else PerfTableState.expanded.delete(gid);
      _syncExpandAll();
    });
  });
  _syncExpandAll();
  _updateSortIcons();
}

function fmtPF(v, trades) {
  if (!trades) return "--";
  if (v === null || v === undefined) return "--";
  return v >= 999 ? "∞" : fmtNum(v, 2);
}

/** Extra (collapsed-by-default) metrics for one group. */
function perfExtraHtml(r, more) {
  const n = (v) => (v == null ? "--" : String(v));
  const pct = (a, b) => (b ? " (" + ((100 * a) / b).toFixed(0) + "%)" : "");
  const withCount = (amt, cnt) => (cnt ? `${fmtNum(amt)} (${cnt})` : "--");
  const items = [
    ["perf.m.gross_profit", fmtNum(r.gross_profit), "num-pos"],
    ["perf.m.gross_loss", fmtNum(r.gross_loss), "num-neg"],
    ["perf.m.abs_dd", fmtNum(r.absolute_drawdown), ""],
    ["perf.m.profit_trades", n(r.profit_trades) + pct(r.profit_trades, r.total_trades), ""],
    ["perf.m.loss_trades", n(r.loss_trades) + pct(r.loss_trades, r.total_trades), ""],
    ["perf.m.consec_wins", n(r.consecutive_wins), ""],
    ["perf.m.consec_losses", n(r.consecutive_losses), ""],
    ["perf.m.consec_profit", withCount(r.max_consec_profit, r.max_consec_profit_count), "num-pos"],
    ["perf.m.consec_loss", withCount(r.max_consec_loss, r.max_consec_loss_count), "num-neg"],
    ["perf.m.long", n(r.long_positions) + (r.long_positions ? ` (${t("perf.m.won")} ${pct(r.long_won, r.long_positions).trim().replace(/[()]/g, "")})` : ""), ""],
    ["perf.m.short", n(r.short_positions) + (r.short_positions ? ` (${t("perf.m.won")} ${pct(r.short_won, r.short_positions).trim().replace(/[()]/g, "")})` : ""), ""],
  ];
  return `<div class="perf-extra-grid">${items.concat(more || [])
    .map(([k, v, cls]) => `<div class="pe-item"><span class="pe-label">${t(k)}</span><span class="pe-val ${cls}">${v}</span></div>`)
    .join("")}</div>`;
}

function _syncExpandAll() {
  const btn = document.getElementById("perf-expand-all");
  if (!btn) return;
  const total = PerfTableState.filtered.length;
  const allOpen = total > 0 && PerfTableState.filtered.every((r) => PerfTableState.expanded.has(r.group_id != null ? r.group_id : r.id));
  btn.textContent = allOpen ? t("perf.collapse.all") : t("perf.expand.all");
  btn.dataset.state = allOpen ? "open" : "closed";
}

function togglePerfExpandAll(onRowClick) {
  const btn = document.getElementById("perf-expand-all");
  const open = btn && btn.dataset.state !== "open";
  PerfTableState.filtered.forEach((r) => {
    const id = r.group_id != null ? r.group_id : r.id;
    if (open) PerfTableState.expanded.add(id); else PerfTableState.expanded.delete(id);
  });
  _redrawPerfTable(onRowClick);
}

function _updateSortIcons() {
  _setSortHeaders("perf-table", PerfTableState.sortKey, PerfTableState.sortDir);
}

function wirePerfTableSorting(onRowClick) {
  document.querySelectorAll("#perf-table thead th[data-key]").forEach((th) => {
    th.classList.add("sortable");
    if (th.dataset.wired) return;
    th.dataset.wired = "1";
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (PerfTableState.sortKey === key) {
        PerfTableState.sortDir = PerfTableState.sortDir === "asc" ? "desc" : "asc";
      } else {
        PerfTableState.sortKey = key;
        PerfTableState.sortDir = key === "name" || key === "status" ? "asc" : "desc";
      }
      _redrawPerfTable(onRowClick);
    });
  });
  _updateSortIcons();
}

function setPerfFilter(key, op, value) {
  if (value === "" || value == null) {
    delete PerfTableState.filters[key];
  } else {
    PerfTableState.filters[key] = { op, value };
  }
}

function clearPerfFilters() {
  PerfTableState.filters = {};
}

function applyPerfFiltersAndRedraw(onRowClick) {
  _applyPerfFilters();
  _redrawPerfTable(onRowClick);
}

const TradesTableState = { expanded: new Set() };

/** Price/qty with sensible significant digits (works for BTC at 60000 and SHIB at 0.00001). */
function fmtSig(v, sig = 6) {
  if (v == null || !isFinite(v)) return "--";
  const n = Number(v);
  if (n === 0) return "0";
  return String(parseFloat(n.toPrecision(sig)));
}

function _legFill(leg) {
  if (!leg) return null;
  const qty = leg.filled_qty != null ? leg.filled_qty : leg.qty;
  const px = leg.fill_price != null ? leg.fill_price : leg.price;
  if (qty == null && px == null) return null;
  return { qty: qty != null ? Number(qty) : null, px: px != null ? Number(px) : null };
}

/** Per-coin entry / close table for one trade (price, qty, size = qty x price). */
function tradeLegsHtml(tr0) {
  const entry = {}, close = {}, order = [], side = {};
  (tr0.legs_entry || []).forEach((l) => { entry[l.symbol] = _legFill(l); side[l.symbol] = l.side; order.push(l.symbol); });
  (tr0.legs_close || []).forEach((l) => { close[l.symbol] = _legFill(l); if (!order.includes(l.symbol)) order.push(l.symbol); });
  // Older trades only stored prices per symbol.
  Object.keys(tr0.entry_prices || {}).forEach((sym) => {
    if (!order.includes(sym)) order.push(sym);
    if (!entry[sym]) entry[sym] = { qty: null, px: Number(tr0.entry_prices[sym]) };
  });
  Object.keys(tr0.close_prices || {}).forEach((sym) => {
    if (!order.includes(sym)) order.push(sym);
    if (!close[sym]) close[sym] = { qty: null, px: Number(tr0.close_prices[sym]) };
  });
  if (!order.length) return `<div class="legs-empty">${t("trades.legs.none")}</div>`;
  const size = (f) => (f && f.qty != null && f.px != null ? fmtNum(f.qty * f.px, 2) : "--");
  const cells = (f) => (f
    ? `<td>${fmtSig(f.px)}</td><td>${fmtSig(f.qty, 8)}</td><td>${size(f)}</td>`
    : `<td>--</td><td>--</td><td>--</td>`);
  // Gross PnL of one leg (before fees): long gains when price rises, short when it falls.
  const legPnl = (sym) => {
    const e = entry[sym], c = close[sym], sd = side[sym];
    if (!e || !c || !sd || e.px == null || c.px == null) return null;
    const q = c.qty != null ? c.qty : e.qty;
    if (q == null) return null;
    return (sd === "buy" ? 1 : -1) * (c.px - e.px) * q;
  };
  let pnlTot = 0, pnlAny = false;
  const rows = order.map((sym) => {
    const sd = side[sym];
    const sdCell = sd ? `<span class="${sd === "buy" ? "dir-long" : "dir-short"}">${t(sd === "buy" ? "trades.legs.buy" : "trades.legs.sell")}</span>` : "--";
    const lp = legPnl(sym);
    if (lp != null) { pnlTot += lp; pnlAny = true; }
    return `<tr><td class="leg-sym">${_esc(sym)}</td><td>${sdCell}</td>${cells(entry[sym])}${cells(close[sym])}<td class="${pnlClass(lp)}">${lp != null ? fmtNum(lp, 4) : "--"}</td></tr>`;
  }).join("");
  const sumSize = (m, only) => {
    let tot = 0, any = false;
    order.forEach((sym) => {
      if (only && side[sym] !== only) return;
      const f = m[sym];
      if (f && f.qty != null && f.px != null) { tot += f.qty * f.px; any = true; }
    });
    return any ? fmtNum(tot, 2) : "--";
  };
  const foot = (label, only, cls, pnlCell) =>
    `<tr class="${cls || ""}"><td colspan="2">${label}</td><td></td><td></td><td>${sumSize(entry, only)}</td><td></td><td></td><td>${sumSize(close, only)}</td>${pnlCell || "<td></td>"}</tr>`;
  const hasSides = order.some((sym) => side[sym]);
  return `
    <table class="legs-table">
      <thead>
        <tr>
          <th rowspan="2">${t("trades.legs.symbol")}</th><th rowspan="2">${t("trades.legs.side")}</th>
          <th colspan="3" class="grp-entry">${t("trades.legs.entry")}</th>
          <th colspan="3" class="grp-close">${t("trades.legs.close")}</th>
          <th rowspan="2" title="${t("trades.legs.pnl.tip")}">${t("trades.legs.pnl")}</th>
        </tr>
        <tr>
          <th>${t("trades.legs.price")}</th><th>${t("trades.legs.qty")}</th><th>${t("trades.legs.size")}</th>
          <th>${t("trades.legs.price")}</th><th>${t("trades.legs.qty")}</th><th>${t("trades.legs.size")}</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
      <tfoot>
        ${hasSides ? foot(t("trades.legs.long_size"), "buy") + foot(t("trades.legs.short_size"), "sell") : ""}
        ${foot(t("trades.legs.total"), null, "foot-total", `<td class="${pnlClass(pnlAny ? pnlTot : null)}">${pnlAny ? fmtNum(pnlTot, 4) : "--"}</td>`)}
      </tfoot>
    </table>
    <div class="legs-note">${t("trades.legs.note")}</div>`;
}

function renderTradesTable(trades) {
  const tbody = document.querySelector("#trades-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  // NOTE: the row variable must not be called `t`; that shadows the i18n function
  // t() and made every row throw "t is not a function" (empty chart, empty log).
  (trades || []).forEach((tr0) => {
    const tr = document.createElement("tr");
    tr.className = "trade-row";
    const dirCls = tr0.direction === "long_residual" ? "dir-long" : "dir-short";
    const reasonCls = tr0.close_reason ? `reason-${tr0.close_reason}` : "";
    const p = displayTradePnl(tr0);
    let pnlCell;
    if (tr0.mode === "live" && p == null && tr0.status === "closed") {
      pnlCell = `<span title="${t("trades.pending.title")}">${t("trades.pending")}</span>`;
    } else {
      pnlCell = fmtNum(p);
    }
    const src = tr0.mode === "live" ? (p != null ? "exch" : "?") : "model";
    const open = TradesTableState.expanded.has(tr0.id);
    tr.innerHTML = `
      <td class="${dirCls}"><button type="button" class="perf-expand trade-expand${open ? " open" : ""}" aria-expanded="${open}" title="${t("trades.expand.title")}">&#9656;</button>${tr0.direction === "long_residual" ? t("trades.long") : t("trades.short")}</td>
      <td>${fmtTime(tr0.entry_time)}</td>
      <td>${fmtTime(tr0.close_time)}</td>
      <td class="${reasonCls}">${tr0.close_reason || (tr0.status === "open" ? t("trades.open") : "--")}</td>
      <td>${fmtNum(tr0.entry_z, 2)}</td>
      <td>${fmtNum(tr0.close_z, 2)}</td>
      <td title="${tr0.gross_notional != null ? t("trades.margin.tip", { gross: fmtNum(tr0.gross_notional, 2), lev: tr0.leverage || 1 }) : ""}">${tr0.margin_used != null ? fmtNum(tr0.margin_used, 2) : "--"}</td>
      <td class="${pnlClass(p)}">${pnlCell}</td>
      <td>${tr0.mode}/${src}</td>
    `;
    tbody.appendChild(tr);

    const extra = document.createElement("tr");
    extra.className = "trade-extra";
    extra.hidden = !open;
    extra.innerHTML = `<td colspan="9">${tradeLegsHtml(tr0)}</td>`;
    tbody.appendChild(extra);

    const toggle = () => {
      const nowOpen = extra.hidden;
      extra.hidden = !nowOpen;
      const btn = tr.querySelector(".trade-expand");
      btn.classList.toggle("open", nowOpen);
      btn.setAttribute("aria-expanded", String(nowOpen));
      if (nowOpen) TradesTableState.expanded.add(tr0.id); else TradesTableState.expanded.delete(tr0.id);
    };
    tr.addEventListener("click", toggle);
  });
}

const CandTableState = {
  rows: [], handlers: {}, sortKey: "total_net_profit", sortDir: "desc", wired: false, expanded: new Set(),
};
const _CAND_FILTER_IDS = ["cand-search", "cand-status", "cand-source", "cand-min-sharpe", "cand-min-pf",
  "cand-min-pnl", "cand-min-trades"];

/** Normalise a candidate group to the same field names the performance table uses, so the
 *  shared helpers (fmtPF, perfExtraHtml, ...) work on it unchanged. Backtest metrics come from the
 *  same GroupPerformance as live performance; older candidates only have the legacy keys. */
function _flattenCand(g) {
  const m = g.backtest_metrics || {};
  return {
    ...m,
    ...g,
    total_trades: m.total_trades || m.trade_count || 0,
    total_net_profit: m.total_net_profit != null && (m.total_net_profit !== 0 || m.total_pnl == null) ? m.total_net_profit : m.total_pnl,
    maximal_drawdown: m.maximal_drawdown != null && m.maximal_drawdown !== 0 ? m.maximal_drawdown : m.max_drawdown,
    expected_payoff: m.expected_payoff != null ? m.expected_payoff : m.avg_pnl,
    trade_count: m.trade_count,
    symbols_text: (g.symbols || []).join(", "),
  };
}

function _candFilterValues() {
  const v = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  return {
    search: v("cand-search").trim().toLowerCase(), status: v("cand-status"), source: v("cand-source"),
    minSharpe: v("cand-min-sharpe"), minPf: v("cand-min-pf"), minPnl: v("cand-min-pnl"), minTrades: v("cand-min-trades"),
  };
}

function _syncCandExpandAll(rows) {
  const btn = document.getElementById("cand-expand-all");
  if (!btn) return;
  const allOpen = rows.length > 0 && rows.every((r) => CandTableState.expanded.has(r.id));
  btn.textContent = allOpen ? t("perf.collapse.all") : t("perf.expand.all");
  btn.dataset.state = allOpen ? "open" : "closed";
}

function _wireCandTable() {
  if (CandTableState.wired) return;
  CandTableState.wired = true;
  document.querySelectorAll("#candidates-table thead th[data-key]").forEach((th) => {
    th.classList.add("sortable");
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (CandTableState.sortKey === key) {
        CandTableState.sortDir = CandTableState.sortDir === "asc" ? "desc" : "asc";
      } else {
        CandTableState.sortKey = key;
        CandTableState.sortDir = ["name", "source", "status"].includes(key) ? "asc" : "desc";
      }
      _drawCandTable();
    });
  });
  _CAND_FILTER_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener(el.tagName === "SELECT" ? "change" : "input", _drawCandTable);
  });
  const clr = document.getElementById("cand-filter-clear");
  if (clr) clr.addEventListener("click", () => {
    _CAND_FILTER_IDS.forEach((id) => { const el = document.getElementById(id); if (el) el.value = ""; });
    _drawCandTable();
  });
  const exp = document.getElementById("cand-expand-all");
  if (exp) exp.addEventListener("click", () => {
    const open = exp.dataset.state !== "open";
    CandTableState.rows.forEach((r) => { if (open) CandTableState.expanded.add(r.id); else CandTableState.expanded.delete(r.id); });
    _drawCandTable();
  });
}

function renderCandidatesTable(groups, onActivate, onDeactivate, onDelete) {
  CandTableState.rows = (groups || []).map(_flattenCand);
  CandTableState.handlers = { onActivate, onDeactivate, onDelete };
  _wireCandTable();
  _drawCandTable();
}

function _drawCandTable() {
  const tbody = document.querySelector("#candidates-table tbody");
  if (!tbody) return;
  const f = _candFilterValues();
  const { onActivate, onDeactivate, onDelete } = CandTableState.handlers;
  const num = (x) => (x === "" ? null : Number(x));
  const minS = num(f.minSharpe), minPf = num(f.minPf), minP = num(f.minPnl), minT = num(f.minTrades);
  const rows = CandTableState.rows.filter((g) => {
    if (f.search && !`${g.name} ${g.symbols_text}`.toLowerCase().includes(f.search)) return false;
    if (f.status && g.status !== f.status) return false;
    if (f.source && g.source !== f.source) return false;
    if (minS != null && !(Number(g.sharpe_ratio) >= minS)) return false;
    if (minPf != null && !(Number(g.profit_factor) >= minPf)) return false;
    if (minP != null && !(Number(g.total_net_profit) > minP)) return false;
    if (minT != null && !(Number(g.total_trades) >= minT)) return false;
    return true;
  }).sort((a, b) => _cmp(a[CandTableState.sortKey], b[CandTableState.sortKey], CandTableState.sortDir));

  _setSortHeaders("candidates-table", CandTableState.sortKey, CandTableState.sortDir);
  const countEl = document.getElementById("cand-count");
  if (countEl) countEl.textContent = t("perf.count", { shown: rows.length, total: CandTableState.rows.length });
  _syncCandExpandAll(rows);

  tbody.innerHTML = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="10" class="table-empty">${t(CandTableState.rows.length ? "perf.empty.filtered" : "init.cand.empty")}</td>`;
    tbody.appendChild(tr);
    return;
  }
  rows.forEach((g) => {
    const open = CandTableState.expanded.has(g.id);
    const isActive = g.status === "active";
    const actionLabel = isActive ? t("perf.deactivate") : t("perf.activate");
    const toggleCls = isActive ? "btn-secondary" : "btn-primary";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="perf-name"><button type="button" class="perf-expand${open ? " open" : ""}" aria-expanded="${open}" title="${t("perf.expand.title")}">&#9656;</button>${_esc(g.name)}<div class="cand-symbols">${_esc(g.symbols_text)}</div></td>
      <td><span class="g-status ${g.status}">${statusLabel(g.status)}</span></td>
      <td>${_esc(g.source || "--")}${g.sector ? ` · ${_esc(g.sector)}` : ""}</td>
      <td>${g.total_trades ?? 0}</td>
      <td>${fmtPct(g.win_rate)}</td>
      <td>${fmtPF(g.profit_factor, g.total_trades)}</td>
      <td class="${pnlClass(g.total_net_profit)}">${fmtNum(g.total_net_profit)}</td>
      <td class="${pnlClass(g.expected_payoff)}">${fmtNum(g.expected_payoff)}</td>
      <td>${fmtNum(g.maximal_drawdown)}</td>
      <td class="col-actions actions-cell">
        <div class="perf-actions">
          <button type="button" class="${toggleCls} toggle-btn" data-id="${g.id}">${actionLabel}</button>
          <button type="button" class="btn-danger delete-btn" data-id="${g.id}" title="${t("init.cand.delete.title")}">${t("init.cand.delete")}</button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);

    // Same extra-metrics block as the Backbone performance table, plus the candidate-only stats.
    const more = [
      ["perf.th.sharpe", fmtNum(g.sharpe_ratio, 2), ""],
      ["perf.th.sortino", fmtNum(g.sortino_ratio, 2), ""],
      ["perf.th.avg_hold", fmtNum(g.avg_holding_hours, 1), ""],
      ["perf.m.best_trade", fmtNum(g.best_trade), "num-pos"],
      ["perf.m.worst_trade", fmtNum(g.worst_trade), "num-neg"],
    ];
    const extra = document.createElement("tr");
    extra.className = "perf-extra";
    extra.hidden = !open;
    const grid = perfExtraHtml(g, more.map(([k, v, c]) => [k, v, c]));
    extra.innerHTML = `<td colspan="10">${grid}</td>`;
    tbody.appendChild(extra);

    const expBtn = tr.querySelector(".perf-expand");
    const toggleOpen = () => {
      const nowOpen = extra.hidden;
      extra.hidden = !nowOpen;
      expBtn.classList.toggle("open", nowOpen);
      expBtn.setAttribute("aria-expanded", String(nowOpen));
      if (nowOpen) CandTableState.expanded.add(g.id); else CandTableState.expanded.delete(g.id);
      _syncCandExpandAll(rows);
    };
    expBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleOpen(); });
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".toggle-btn, .delete-btn, .perf-expand")) return;
      toggleOpen();
    });
    tr.querySelector(".toggle-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      if (isActive) onDeactivate(g.id); else onActivate(g.id);
    });
    tr.querySelector(".delete-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      if (onDelete) onDelete(g.id, g.name);
    });
  });
}
