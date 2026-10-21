"""Legs of a basket are sent concurrently, then confirmed; failures roll the basket back."""
import asyncio
import time

import pytest

from backend.engine.bot_engine import BotEngine, PartialLegsError

A, B = "AAA/USDT:USDT", "BBB/USDT:USDT"


class FakeClient:
    """Stands in for a paper client (no XT specifics) with controllable behaviour per symbol."""
    def __init__(self, delay=0.2, fail=(), fill_ratio=None):
        self.delay, self.fail, self.calls, self.fill_ratio = delay, set(fail), [], fill_ratio or {}
        self.closed = []

    async def place_order(self, symbol, side, amount, **kw):
        self.calls.append((symbol, side, amount, time.perf_counter(), bool(kw.get("reduce_only"))))
        await asyncio.sleep(self.delay)
        if symbol in self.fail:
            raise RuntimeError("rejected")
        return {"status": "ok", "amount": amount, "fill_price": 1.0, "order": {"id": f"o-{symbol}"}}


def _legs():
    return [{"symbol": A, "side": "buy", "qty": 10.0, "price": 1.0},
            {"symbol": B, "side": "sell", "qty": 8.0, "price": 1.0}]


def _run(c):
    return asyncio.run(c)


def test_both_legs_are_sent_at_the_same_time():
    c = FakeClient(delay=0.3)
    t0 = time.perf_counter()
    out = _run(BotEngine()._place_legs(c, _legs(), is_close=False))
    total = time.perf_counter() - t0
    assert [l["symbol"] for l in out] == [A, B] and not any(l.get("failed") for l in out)
    starts = sorted(x[3] for x in c.calls)
    assert starts[1] - starts[0] < 0.05          # sent together, not one after the other
    assert total < 0.55                          # ~one round trip, not two


def test_rejected_leg_fails_the_basket_and_rolls_back_the_other_leg():
    c = FakeClient(delay=0.05, fail={B})
    with pytest.raises(PartialLegsError) as e:
        _run(BotEngine()._place_legs(c, _legs(), is_close=False))
    assert B in str(e.value) and "rolled back" in str(e.value)
    # the filled leg A was closed again with the opposite side
    assert any(x[0] == A and x[1] == "sell" for x in c.calls)


def test_all_legs_rejected_raises_without_rollback_orders():
    c = FakeClient(delay=0.01, fail={A, B})
    with pytest.raises(PartialLegsError):
        _run(BotEngine()._place_legs(c, _legs(), is_close=False))
    assert len(c.calls) == 2                      # just the two attempts, nothing to unwind


def test_closing_returns_failed_legs_instead_of_raising():
    c = FakeClient(delay=0.01, fail={B})
    legs = [{**l, "side": "sell" if l["side"] == "buy" else "buy"} for l in _legs()]
    out = _run(BotEngine()._place_legs(c, legs, is_close=True))
    assert [bool(l.get("failed")) for l in out] == [False, True]


def test_entry_fill_checks_flag_unfilled_and_partial_legs():
    out = [
        {"symbol": A, "fill_source": "exchange_order", "requested_qty": 10.0, "filled_qty": 0.0},
        {"symbol": B, "fill_source": "exchange_order", "requested_qty": 10.0, "filled_qty": 5.0},
        {"symbol": "C", "fill_source": "exchange_order", "requested_qty": 10.0, "order_qty": 9.0, "filled_qty": 9.0},
        {"symbol": "D", "requested_qty": 10.0, "filled_qty": 1.0},          # no exchange record: not judged
    ]
    BotEngine._check_entry_fills(out)
    assert out[0]["failed"] and "nothing filled" in out[0]["error"]
    assert out[1]["failed"] and out[1]["partial_fill"]
    assert not out[2].get("failed") and not out[3].get("failed")   # sent size (order_qty) is what counts


def test_partially_filled_leg_is_rolled_back_too():
    class PartialClient(FakeClient):
        async def place_order(self, symbol, side, amount, **kw):
            r = await super().place_order(symbol, side, amount, **kw)
            return r
    c = PartialClient(delay=0.01)
    legs = _legs()
    eng = BotEngine()
    orig = eng._place_one_leg

    async def fake_one(client, leg, *, is_close):
        r = await orig(client, leg, is_close=is_close)
        if leg["symbol"] == B and not is_close:
            r = {**r, "fill_source": "exchange_order", "requested_qty": 8.0, "filled_qty": 2.0}
        else:
            r = {**r, "fill_source": "exchange_order", "requested_qty": r["requested_qty"], "filled_qty": r["requested_qty"]}
        return r
    eng._place_one_leg = fake_one
    with pytest.raises(PartialLegsError):
        _run(eng._place_legs(c, legs, is_close=False))
    rolled = [(x[0], x[1]) for x in c.calls[2:]]
    assert (A, "sell") in rolled and (B, "buy") in rolled      # both the full and the partial leg are unwound
