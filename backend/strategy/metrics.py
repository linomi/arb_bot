"""
Performance metrics from a group's closed trades.

PnL inputs are absolute cash numbers (raw-price residual cash for live/backtest).

Notes on definitions (ranking-oriented, not institutional risk):
  - sharpe / sortino: mean(trade PnL) / std — *per trade*, not annualized
  - max_drawdown: peak-to-trough on the trade-by-trade equity curve starting at 0
  - profit_factor: gross_wins / |gross_losses|; capped at 999 when no losses

Trade-report style metrics (MetaTrader naming):
  - gross_profit / gross_loss (loss is negative), total_net_profit, expected_payoff
  - absolute_drawdown: deepest equity below the starting 0 (positive magnitude)
  - maximal_drawdown: largest peak-to-trough drop of the trade-by-trade equity curve
  - consecutive_wins / consecutive_losses: longest winning / losing streak (trades)
  - max_consec_profit(+_count): largest summed profit of a winning streak and its length
  - max_consec_loss(+_count): largest summed loss of a losing streak and its length
  - long_positions / short_positions: trades by direction (+ how many of each won)
"""
from dataclasses import dataclass, asdict
import numpy as np


@dataclass
class GroupPerformance:
    trade_count: int
    win_rate: float          # fraction, 0..1
    total_pnl: float
    avg_pnl: float
    max_drawdown: float      # positive magnitude from peak equity (start=0)
    sharpe_ratio: float
    sortino_ratio: float
    profit_factor: float
    avg_holding_hours: float
    best_trade: float
    worst_trade: float
    # --- trade-report metrics (defaults keep older call sites valid) ---
    gross_profit: float = 0.0
    gross_loss: float = 0.0              # negative or 0
    total_net_profit: float = 0.0
    expected_payoff: float = 0.0
    absolute_drawdown: float = 0.0
    maximal_drawdown: float = 0.0
    profit_trades: int = 0
    loss_trades: int = 0
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    max_consec_profit: float = 0.0
    max_consec_profit_count: int = 0
    max_consec_loss: float = 0.0         # negative or 0
    max_consec_loss_count: int = 0
    total_trades: int = 0
    long_positions: int = 0
    short_positions: int = 0
    long_won: int = 0
    short_won: int = 0

    def as_dict(self):
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                d[k] = 999.0 if (k == "profit_factor" and v == float("inf")) else 0.0
        return d


def _streaks(pnls: np.ndarray) -> dict:
    """Longest win/loss streaks (by count) and the biggest summed win/loss streaks."""
    best = {"wins": 0, "losses": 0, "profit": 0.0, "profit_n": 0, "loss": 0.0, "loss_n": 0}
    run_sign, run_n, run_sum = 0, 0, 0.0

    def flush():
        if run_sign > 0:
            best["wins"] = max(best["wins"], run_n)
            if run_sum > best["profit"]:
                best["profit"], best["profit_n"] = run_sum, run_n
        elif run_sign < 0:
            best["losses"] = max(best["losses"], run_n)
            if run_sum < best["loss"]:
                best["loss"], best["loss_n"] = run_sum, run_n

    for v in pnls:
        sign = 1 if v > 0 else -1
        if sign == run_sign:
            run_n += 1
            run_sum += float(v)
        else:
            flush()
            run_sign, run_n, run_sum = sign, 1, float(v)
    flush()
    return best


def compute_group_performance(closed_trades: list[dict]) -> GroupPerformance:
    """
    closed_trades: list of dicts with at least:
      pnl (float), entry_time (datetime), close_time (datetime)
    optional: direction ("long_residual" | "short_residual")
    Trades must be in chronological order for the streak / drawdown metrics.
    """
    if not closed_trades:
        return GroupPerformance(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    closed_trades = sorted(
        closed_trades,
        key=lambda t: (t.get("close_time") is None, t.get("close_time") or 0),
    ) if all(t.get("close_time") is not None for t in closed_trades) else closed_trades

    pnls = np.array([t["pnl"] for t in closed_trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    trade_count = len(pnls)
    win_rate = float(len(wins) / trade_count) if trade_count else 0.0
    total_pnl = float(np.sum(pnls))
    avg_pnl = float(np.mean(pnls))

    # Equity starts at 0 so early losses correctly contribute to drawdown.
    equity_curve = np.concatenate([[0.0], np.cumsum(pnls)])
    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = running_max - equity_curve
    max_drawdown = float(np.max(drawdowns)) if len(drawdowns) else 0.0

    std = np.std(pnls, ddof=1) if trade_count > 1 else 0.0
    sharpe = float(np.mean(pnls) / std) if std > 0 else 0.0

    # Standard downside deviation (target 0): sqrt(mean(min(pnl, 0)^2)) over ALL
    # trades. The old std-of-losses was ~0 for consistent small losses and
    # inflated Sortino for exactly the groups that lose steadily.
    downside_std = float(np.sqrt(np.mean(np.minimum(pnls, 0.0) ** 2)))
    sortino = float(np.mean(pnls) / downside_std) if downside_std > 0 else 0.0

    gross_profit = float(np.sum(wins)) if len(wins) else 0.0
    gross_loss = float(-np.sum(losses)) if len(losses) else 0.0
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
        try:
            v = float(x)
            if v != v or v in (float("inf"), float("-inf")):
                return default
            return v
        except (TypeError, ValueError):
            return default

    st = _streaks(pnls)
    longs = [t for t in closed_trades if str(t.get("direction", "")).startswith("long")]
    shorts = [t for t in closed_trades if str(t.get("direction", "")).startswith("short")]
    min_equity = float(np.min(equity_curve))

    return GroupPerformance(
        gross_profit=_finite(gross_profit),
        gross_loss=_finite(-gross_loss),
        total_net_profit=_finite(total_pnl),
        expected_payoff=_finite(avg_pnl),
        absolute_drawdown=_finite(max(0.0, -min_equity)),
        maximal_drawdown=_finite(max_drawdown),
        profit_trades=int(len(wins)),
        loss_trades=int(len(losses)),
        consecutive_wins=int(st["wins"]),
        consecutive_losses=int(st["losses"]),
        max_consec_profit=_finite(st["profit"]),
        max_consec_profit_count=int(st["profit_n"]),
        max_consec_loss=_finite(st["loss"]),
        max_consec_loss_count=int(st["loss_n"]),
        total_trades=int(trade_count),
        long_positions=len(longs),
        short_positions=len(shorts),
        long_won=sum(1 for t in longs if t["pnl"] > 0),
        short_won=sum(1 for t in shorts if t["pnl"] > 0),
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
