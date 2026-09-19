"""
Performance metrics computed from a group's closed trades, used by:
  - the sortable performance-comparison table
  - the per-group radar chart
  - init-time backtest pruning (top-N selection)
"""
from dataclasses import dataclass, asdict
import numpy as np


@dataclass
class GroupPerformance:
    trade_count: int
    win_rate: float          # fraction, 0..1
    total_pnl: float
    avg_pnl: float
    max_drawdown: float      # positive number = drawdown magnitude
    sharpe_ratio: float
    sortino_ratio: float
    profit_factor: float
    avg_holding_hours: float
    best_trade: float
    worst_trade: float

    def as_dict(self):
        d = asdict(self)
        # Final JSON-safety net (covers any path that still produces inf/NaN).
        for k, v in list(d.items()):
            if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                d[k] = 999.0 if (k == "profit_factor" and v == float("inf")) else 0.0
        return d


def compute_group_performance(closed_trades: list[dict]) -> GroupPerformance:
    """
    closed_trades: list of dicts with at least:
      pnl (float), entry_time (datetime), close_time (datetime)
    """
    if not closed_trades:
        return GroupPerformance(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    pnls = np.array([t["pnl"] for t in closed_trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    trade_count = len(pnls)
    win_rate = float(len(wins) / trade_count) if trade_count else 0.0
    total_pnl = float(np.sum(pnls))
    avg_pnl = float(np.mean(pnls))

    equity_curve = np.cumsum(pnls)
    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = running_max - equity_curve
    max_drawdown = float(np.max(drawdowns)) if len(drawdowns) else 0.0

    std = np.std(pnls, ddof=1) if trade_count > 1 else 0.0
    sharpe = float(np.mean(pnls) / std) if std > 0 else 0.0

    downside_std = np.std(losses, ddof=1) if len(losses) > 1 else 0.0
    sortino = float(np.mean(pnls) / downside_std) if downside_std > 0 else 0.0

    gross_profit = float(np.sum(wins)) if len(wins) else 0.0
    gross_loss = float(-np.sum(losses)) if len(losses) else 0.0
    # JSON cannot encode ±inf / NaN. Cap profit factor at a large finite
    # value when there are wins and zero losses (classic "all winners").
    if gross_loss > 0:
        profit_factor = float(gross_profit / gross_loss)
    elif gross_profit > 0:
        profit_factor = 999.0
    else:
        profit_factor = 0.0

    holding_hours = []
    for t in closed_trades:
        if t.get("entry_time") and t.get("close_time"):
            delta = (t["close_time"] - t["entry_time"]).total_seconds() / 3600.0
            holding_hours.append(delta)
    avg_holding_hours = float(np.mean(holding_hours)) if holding_hours else 0.0

    def _finite(x: float, default: float = 0.0) -> float:
        """Guarantee a JSON-safe float (no NaN / ±inf)."""
        try:
            v = float(x)
            if v != v or v in (float("inf"), float("-inf")):  # NaN or inf
                return default
            return v
        except (TypeError, ValueError):
            return default

    return GroupPerformance(
        trade_count=trade_count,
        win_rate=_finite(win_rate),
        total_pnl=_finite(total_pnl),
        avg_pnl=_finite(avg_pnl),
        max_drawdown=_finite(max_drawdown),
        sharpe_ratio=_finite(sharpe),
        sortino_ratio=_finite(sortino),
        profit_factor=_finite(profit_factor),
        avg_holding_hours=_finite(avg_holding_hours),
        best_trade=_finite(float(np.max(pnls))),
        worst_trade=_finite(float(np.min(pnls))),
    )
