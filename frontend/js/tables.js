const PerfTableState = {
  rows: [],
  filtered: [],
  sortKey: "total_pnl",
  sortDir: "desc",
  filters: {}, // key -> {op, value}  e.g. {total_pnl: {op:">", value:0.1}, status:{op:"=", value:"active"}}
};

/* ---------------- Persian (Jalali / Shamsi) date helpers ---------------- */
function _toJalali(gy, gm, gd) {
  const g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
  let gy2 = gm > 2 ? gy + 1 : gy;
  let days =
    355666 +
    365 * gy +
    Math.floor((gy2 + 3) / 4) -
    Math.floor((gy2 + 99) / 100) +
    Math.floor((gy2 + 399) / 400) +
    gd +
    g_d_m[gm - 1];
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

function fmtTime(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso).slice(0, 16);
  const [jy, jm, jd] = _toJalali(d.getFullYear(), d.getMonth() + 1, d.getDate());
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${jy}/${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")} ${hh}:${mi}`;
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
  const sorted = [...filtered].sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av === bv) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    const cmp = av > bv ? 1 : -1;
    return sortDir === "asc" ? cmp : -cmp;
  });

  const tbody = document.querySelector("#perf-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  sorted.forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${r.name}</td>
      <td>${r.status}</td>
      <td>${r.trade_count}</td>
      <td>${fmtPct(r.win_rate)}</td>
      <td class="${pnlClass(r.total_pnl)}">${fmtNum(r.total_pnl)}</td>
      <td class="${pnlClass(r.avg_pnl)}">${fmtNum(r.avg_pnl)}</td>
      <td>${fmtNum(r.max_drawdown)}</td>
      <td>${fmtNum(r.sharpe_ratio, 2)}</td>
      <td>${fmtNum(r.sortino_ratio, 2)}</td>
      <td>${fmtNum(r.profit_factor, 2)}</td>
      <td>${fmtNum(r.avg_holding_hours, 1)}</td>
    `;
    tr.addEventListener("click", () => onRowClick(r));
    tbody.appendChild(tr);
  });
  _updateSortIcons();
}

function _updateSortIcons() {
  document.querySelectorAll("#perf-table thead th[data-key]").forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.key === PerfTableState.sortKey) {
      th.classList.add(PerfTableState.sortDir === "asc" ? "sort-asc" : "sort-desc");
    }
  });
}

function wirePerfTableSorting(onRowClick) {
  document.querySelectorAll("#perf-table thead th[data-key]").forEach((th) => {
    th.classList.add("sortable");
    // avoid double-binding
    if (th.dataset.wired) return;
    th.dataset.wired = "1";
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (PerfTableState.sortKey === key) {
        PerfTableState.sortDir = PerfTableState.sortDir === "asc" ? "desc" : "asc";
      } else {
        PerfTableState.sortKey = key;
        PerfTableState.sortDir = "desc";
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
  trades.forEach((t) => {
    const tr = document.createElement("tr");
    const dirCls = t.direction === "long_residual" ? "dir-long" : "dir-short";
    const reasonCls = t.close_reason ? `reason-${t.close_reason}` : "";
    tr.innerHTML = `
      <td class="${dirCls}">${t.direction === "long_residual" ? "LONG" : "SHORT"}</td>
      <td>${fmtTime(t.entry_time)}</td>
      <td>${fmtTime(t.close_time)}</td>
      <td class="${reasonCls}">${t.close_reason || (t.status === "open" ? "open" : "--")}</td>
      <td>${fmtNum(t.entry_z, 2)}</td>
      <td>${fmtNum(t.close_z, 2)}</td>
      <td class="${pnlClass(t.pnl)}">${fmtNum(t.pnl)}</td>
      <td>${t.mode}</td>
    `;
    tbody.appendChild(tr);
  });
}

function renderCandidatesTable(groups, onActivate, onDeactivate, onDelete) {
  const tbody = document.querySelector("#candidates-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  groups.forEach((g) => {
    const m = g.backtest_metrics || {};
    const tr = document.createElement("tr");
    const actionLabel = g.status === "active" ? "Deactivate" : "Activate";
    tr.innerHTML = `
      <td>${g.name}</td>
      <td>${g.source}</td>
      <td>${g.sector || "--"}</td>
      <td>${g.symbols.join(", ")}</td>
      <td>${fmtNum(m.sharpe_ratio, 2)}</td>
      <td class="${pnlClass(m.total_pnl)}">${fmtNum(m.total_pnl)}</td>
      <td>${fmtNum(m.max_drawdown)}</td>
      <td>${fmtPct(m.win_rate)}</td>
      <td>${m.trade_count ?? "--"}</td>
      <td class="g-status ${g.status}">${g.status}</td>
      <td class="actions-cell">
        <button class="btn-secondary toggle-btn" data-id="${g.id}">${actionLabel}</button>
        <button class="btn-danger delete-btn" data-id="${g.id}" title="Delete group permanently">Delete</button>
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
