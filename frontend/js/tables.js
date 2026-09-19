const PerfTableState = { rows: [], sortKey: "total_pnl", sortDir: "desc" };

function fmtNum(v, digits = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  if (!isFinite(v)) return v > 0 ? "+inf" : "-inf";
  return Number(v).toFixed(digits);
}
function fmtPct(v) {
  if (v === null || v === undefined) return "--";
  return (v * 100).toFixed(1) + "%";
}
function fmtTime(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function pnlClass(v) {
  if (v === null || v === undefined) return "";
  return v >= 0 ? "num-pos" : "num-neg";
}

function renderPerfTable(rows, onRowClick) {
  PerfTableState.rows = rows;
  _redrawPerfTable(onRowClick);
}

function _redrawPerfTable(onRowClick) {
  const { rows, sortKey, sortDir } = PerfTableState;
  const sorted = [...rows].sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av === bv) return 0;
    const cmp = av > bv ? 1 : -1;
    return sortDir === "asc" ? cmp : -cmp;
  });

  const tbody = document.querySelector("#perf-table tbody");
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
}

function wirePerfTableSorting(onRowClick) {
  document.querySelectorAll("#perf-table thead th[data-key]").forEach((th) => {
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
}

function renderTradesTable(trades) {
  const tbody = document.querySelector("#trades-table tbody");
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
