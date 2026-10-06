const PerfTableState = {
  rows: [],
  filtered: [],
  sortKey: "total_pnl",
  sortDir: "desc",
  filters: {},
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
    tr.innerHTML = `<td colspan="12" class="table-empty">${t(PerfTableState.rows.length ? "perf.empty.filtered" : "perf.empty")}</td>`;
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
    tr.innerHTML = `
      <td>${r.name}</td>
      <td><span class="g-status ${r.status}">${statusLabel(r.status)}</span></td>
      <td>${r.trade_count}</td>
      <td>${fmtPct(r.win_rate)}</td>
      <td class="${pnlClass(r.total_pnl)}">${fmtNum(r.total_pnl)}</td>
      <td class="${pnlClass(r.avg_pnl)}">${fmtNum(r.avg_pnl)}</td>
      <td>${fmtNum(r.max_drawdown)}</td>
      <td>${fmtNum(r.sharpe_ratio, 2)}</td>
      <td>${fmtNum(r.sortino_ratio, 2)}</td>
      <td>${r.profit_factor >= 999 ? "∞" : fmtNum(r.profit_factor, 2)}</td>
      <td>${fmtNum(r.avg_holding_hours, 1)}</td>
      <td class="col-actions">
        <div class="perf-actions">
          <button type="button" class="${toggleCls}" data-id="${gid}" data-status="${r.status}">${toggleLabel}</button>
          <button type="button" class="btn-danger perf-del" data-id="${gid}" title="${t("perf.remove.title")}">${t("perf.remove")}</button>
        </div>
      </td>
    `;

    tr.addEventListener("click", (e) => {
      if (e.target.closest(".perf-del, .perf-toggle")) return;
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
  });
  _updateSortIcons();
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

function renderTradesTable(trades) {
  const tbody = document.querySelector("#trades-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  // NOTE: the row variable must not be called `t`; that shadows the i18n function
  // t() and made every row throw "t is not a function" (empty chart, empty log).
  (trades || []).forEach((tr0) => {
    const tr = document.createElement("tr");
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
    tr.innerHTML = `
      <td class="${dirCls}">${tr0.direction === "long_residual" ? t("trades.long") : t("trades.short")}</td>
      <td>${fmtTime(tr0.entry_time)}</td>
      <td>${fmtTime(tr0.close_time)}</td>
      <td class="${reasonCls}">${tr0.close_reason || (tr0.status === "open" ? t("trades.open") : "--")}</td>
      <td>${fmtNum(tr0.entry_z, 2)}</td>
      <td>${fmtNum(tr0.close_z, 2)}</td>
      <td class="${pnlClass(p)}">${pnlCell}</td>
      <td>${tr0.mode}/${src}</td>
    `;
    tbody.appendChild(tr);
  });
}

const CandTableState = {
  rows: [], handlers: {}, sortKey: "sharpe_ratio", sortDir: "desc", wired: false,
};

function _flattenCand(g) {
  const m = g.backtest_metrics || {};
  return {
    ...g,
    sharpe_ratio: m.sharpe_ratio, total_pnl: m.total_pnl, max_drawdown: m.max_drawdown,
    win_rate: m.win_rate, trade_count: m.trade_count,
    symbols_text: (g.symbols || []).join(", "),
  };
}

function _candFilterValues() {
  const v = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  return {
    search: v("cand-search").trim().toLowerCase(), status: v("cand-status"), source: v("cand-source"),
    minSharpe: v("cand-min-sharpe"), minPnl: v("cand-min-pnl"), minTrades: v("cand-min-trades"),
  };
}

function _wireCandTable() {
  if (CandTableState.wired) return;
  CandTableState.wired = true;
  document.querySelectorAll("#candidates-table thead th[data-key]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (CandTableState.sortKey === key) {
        CandTableState.sortDir = CandTableState.sortDir === "asc" ? "desc" : "asc";
      } else {
        CandTableState.sortKey = key;
        CandTableState.sortDir = ["name", "source", "sector", "symbols_text", "status"].includes(key) ? "asc" : "desc";
      }
      _drawCandTable();
    });
  });
  ["cand-search", "cand-status", "cand-source", "cand-min-sharpe", "cand-min-pnl", "cand-min-trades"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener(el.tagName === "SELECT" ? "change" : "input", _drawCandTable);
  });
  const clr = document.getElementById("cand-filter-clear");
  if (clr) clr.addEventListener("click", () => {
    ["cand-search", "cand-status", "cand-source", "cand-min-sharpe", "cand-min-pnl", "cand-min-trades"].forEach((id) => {
      const el = document.getElementById(id); if (el) el.value = "";
    });
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
  const minS = num(f.minSharpe), minP = num(f.minPnl), minT = num(f.minTrades);
  const rows = CandTableState.rows.filter((g) => {
    if (f.search && !`${g.name} ${g.symbols_text}`.toLowerCase().includes(f.search)) return false;
    if (f.status && g.status !== f.status) return false;
    if (f.source && g.source !== f.source) return false;
    if (minS != null && !(Number(g.sharpe_ratio) >= minS)) return false;
    if (minP != null && !(Number(g.total_pnl) > minP)) return false;
    if (minT != null && !(Number(g.trade_count) >= minT)) return false;
    return true;
  }).sort((a, b) => _cmp(a[CandTableState.sortKey], b[CandTableState.sortKey], CandTableState.sortDir));

  _setSortHeaders("candidates-table", CandTableState.sortKey, CandTableState.sortDir);
  const countEl = document.getElementById("cand-count");
  if (countEl) countEl.textContent = t("perf.count", { shown: rows.length, total: CandTableState.rows.length });

  tbody.innerHTML = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="11" class="table-empty">${t(CandTableState.rows.length ? "perf.empty.filtered" : "init.cand.empty")}</td>`;
    tbody.appendChild(tr);
    return;
  }
  rows.forEach((g) => {
    const tr = document.createElement("tr");
    const actionLabel = g.status === "active" ? t("perf.deactivate") : t("perf.activate");
    const toggleCls = g.status === "active" ? "btn-secondary" : "btn-primary";
    tr.innerHTML = `
      <td>${g.name}</td>
      <td>${g.source}</td>
      <td>${g.sector || "--"}</td>
      <td>${g.symbols_text}</td>
      <td>${fmtNum(g.sharpe_ratio, 2)}</td>
      <td class="${pnlClass(g.total_pnl)}">${fmtNum(g.total_pnl)}</td>
      <td>${fmtNum(g.max_drawdown)}</td>
      <td>${fmtPct(g.win_rate)}</td>
      <td>${g.trade_count ?? "--"}</td>
      <td><span class="g-status ${g.status}">${statusLabel(g.status)}</span></td>
      <td class="actions-cell">
        <button class="${toggleCls} toggle-btn" data-id="${g.id}">${actionLabel}</button>
        <button class="btn-danger delete-btn" data-id="${g.id}" title="${t("init.cand.delete.title")}">${t("init.cand.delete")}</button>
      </td>
    `;
    tbody.appendChild(tr);
    tr.querySelector(".toggle-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      if (g.status === "active") onDeactivate(g.id); else onActivate(g.id);
    });
    tr.querySelector(".delete-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      if (onDelete) onDelete(g.id, g.name);
    });
  });
}
