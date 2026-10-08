import datetime as dt

from backend.strategy.metrics import compute_group_performance


def _trades(pnls, dirs=None):
    t0 = dt.datetime(2026, 1, 1)
    out = []
    for i, p in enumerate(pnls):
        out.append({
            "pnl": p,
            "entry_time": t0 + dt.timedelta(hours=2 * i),
            "close_time": t0 + dt.timedelta(hours=2 * i + 1),
            "direction": (dirs[i] if dirs else "long_residual"),
        })
    return out


def test_trade_report_metrics():
    pnls = [5, 3, -2, -4, -1, 6, -3]
    dirs = ["long_residual", "short_residual", "long_residual", "short_residual",
            "long_residual", "short_residual", "long_residual"]
    m = compute_group_performance(_trades(pnls, dirs)).as_dict()
    assert m["total_trades"] == 7 and m["profit_trades"] == 3 and m["loss_trades"] == 4
    assert m["gross_profit"] == 14 and m["gross_loss"] == -10
    assert m["total_net_profit"] == 4 and abs(m["expected_payoff"] - 4 / 7) < 1e-9
    assert abs(m["profit_factor"] - 1.4) < 1e-9
    assert m["consecutive_wins"] == 2 and m["consecutive_losses"] == 3
    assert m["max_consec_profit"] == 8 and m["max_consec_profit_count"] == 2
    assert m["max_consec_loss"] == -7 and m["max_consec_loss_count"] == 3
    # equity: 0,5,8,6,2,1,7,4 -> max DD from 8 to 1 = 7; never below 0
    assert m["maximal_drawdown"] == 7 and m["absolute_drawdown"] == 0
    assert m["long_positions"] == 4 and m["short_positions"] == 3
    assert m["long_won"] == 1 and m["short_won"] == 2


def test_absolute_drawdown_when_equity_goes_negative():
    m = compute_group_performance(_trades([-3, -2, 4])).as_dict()
    assert m["absolute_drawdown"] == 5 and m["maximal_drawdown"] == 5


def test_empty_and_legacy_fields_still_present():
    m = compute_group_performance([]).as_dict()
    assert m["total_trades"] == 0 and m["gross_loss"] == 0
    m = compute_group_performance(_trades([1, -1])).as_dict()
    assert "sharpe_ratio" in m and "max_drawdown" in m  # init ranking still relies on these
