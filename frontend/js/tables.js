const PerfTableState = {
  rows: [],
  filtered: [],
  sortKey: "total_pnl",
  sortDir: "desc",
  filters: {},
};

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
    const gid = r.group_id != null ? r.group_id : r.id;
    const isActive = String(r.status).toLowerCase() === "active";
    const toggleLabel = isActive ? "Deactivate" : "Activate";
    const toggleCls = isActive ? "btn-secondary perf-toggle" : "btn-primary perf-toggle";

    const tr = document.createElement("tr");
    if (typeof state !== "undefined" && state.selectedGroupId === gid) {
      tr.classList.add("selected-row");
    }
    tr.innerHTML = `
      <td>${r.name}</td>
      <td><span class="g-status ${r.status}">${r.status}</span></td>
      <td>${r.trade_count}</td>
      <td>${fmtPct(r.win_rate)}</td>
      <td class="${pnlClass(r.total_pnl)}">${fmtNum(r.total_pnl)}</td>
      <td class="${pnlClass(r.avg_pnl)}">${fmtNum(r.avg_pnl)}</td>
      <td>${fmtNum(r.max_drawdown)}</td>
      <td>${fmtNum(r.sharpe_ratio, 2)}</td>
      <td>${fmtNum(r.sortino_ratio, 2)}</td>
      <td>${fmtNum(r.profit_factor, 2)}</td>
      <td>${fmtNum(r.avg_holding_hours, 1)}</td>
      <td class="col-actions">
        <div class="perf-actions">
          <button type="button" class="${toggleCls}" data-id="${gid}" data-status="${r.status}">${toggleLabel}</button>
          <button type="button" class="btn-danger perf-del" data-id="${gid}" title="Delete group permanently">Remove</button>
        </div>
      </td>
    `;

    tr.addEventListener("click", (e) => {
      if (e.target.closest(".perf-del, .perf-toggle")) return;
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
  (trades || []).forEach((t) => {
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
