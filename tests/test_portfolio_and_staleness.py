import pandas as pd
from backend.engine.bot_engine import BotEngine


def test_data_is_stale_true_for_old_bar():
    eng = BotEngine()
    idx = pd.date_range("2020-01-01", periods=5, freq="min")
    df = pd.DataFrame({"A": range(5)}, index=idx)
    assert eng._data_is_stale(df, {"sampling_time": 60, "data_staleness_mult": 3})


def test_data_is_stale_false_when_disabled():
    eng = BotEngine()
    idx = pd.date_range("2020-01-01", periods=5, freq="min")
    df = pd.DataFrame({"A": range(5)}, index=idx)
    assert not eng._data_is_stale(df, {"sampling_time": 60, "data_staleness_mult": 0})


def test_portfolio_caps_off():
    eng = BotEngine()
    class FakeDB:
        def query(self, *a, **k):
            class Q:
                def filter(self, *a, **k):
                    return self
                def all(self):
                    return []
            return Q()
    assert eng._portfolio_caps_exceeded(FakeDB(), {"max_open_trades": 0, "max_total_gross_notional": 0}, "paper") is None


def test_portfolio_max_trades():
    eng = BotEngine()
    class T:
        status = "open"
        mode = "paper"
        legs_entry = [{"qty": 1, "price": 100}]
    class FakeDB:
        def query(self, *a, **k):
            class Q:
                def filter(self, *a, **k):
                    return self
                def all(self):
                    return [T(), T()]
            return Q()
    msg = eng._portfolio_caps_exceeded(FakeDB(), {"max_open_trades": 2, "max_total_gross_notional": 0}, "paper")
    assert msg and "max_open_trades" in msg
