# fix: Rial/Toman unit mismatch, real fill prices, max_entry_scale liquidity guard

## Task A — Rial/Toman unit mismatch

**Evidence:** Across 40 live legs, `position.entryPrice / logged decision price ≈ 10.0`
consistently (not random slippage). Market-data endpoints return Toman-scale values;
margin/position/order endpoints report Rial (1 Toman = 10 Rial).

**Docs check:** Official Nobitex docs state Rial for rial markets everywhere
(https://apidocs.nobitex.ir/). Live APIs diverge: `/market/udf/history` and
`/market/stats` behave as Toman; `/positions/*` and `/margin/orders/add` as Rial.
No live credentials were available in this environment for a fresh probe; the
batch of live trades is the confirmation.

**Fix:** Single conversion inside `backend/exchange/nobitex.py`:
- Module constant `NOBITEX_MARKET_DATA_SCALE = 10`
- Applied in `get_ohlc` (o/h/l/c) and `get_last_price` only
- No second conversion in sizing/bot_engine/pnl

**Persisted data warning:** Historical OHLC-derived OLS fits, residual series,
and `entry_prices` stored under the old (Toman) scale will not mix cleanly with
new Rial-scale prices. Open trades that resume a frozen fit against fresh OHLC
will see residual/z discontinuity. **No automatic migration** — operators should
close open live positions or re-init groups after deploy.

## Task B — Capture real fill price

- After entry `resolve_position_id`, poll `get_position` for `entryPrice` → `leg["fill_price"]`
- Keep `decision_price` (former `price`) for slippage diagnostics
- After `close_position`, poll for `exitPrice` / mark the same way
- Fallback: `fill_price = decision_price`, `fill_price_estimated: true`
- `legs_gross_notional` (pnl.py + sizing.py) prefers `fill_price`
- INFO log: `(fill − decision) / decision` per leg
- Backtester unchanged (no live fills)

## Task C — max_entry_scale liquidity guard

- `leg_orders(..., max_scale=None)` raises `ExcessiveScalingError` when scale > max
- Config: `backbone.max_entry_scale` default **3.0** (additive merge, 0 disables)
- Why 3.0: sample scales were ≈1.3 (profitable BCH/TRX), 5.9 / 7.3 / 9.3 (losing
  thin small-caps). Default **rejects 3 of 4** sampled historical round-trips
  (the loss-makers) and keeps the liquid 1.3x case. Tradeoff: fewer entries on
  thin books, less adverse selection from multi-leg market impact.
- Wired into `_check_entry` (INFO skip, not `_group_errors`) and `backtest_group`
- Frontend FIELD_HINTS updated

## Files touched

| File | Change |
|------|--------|
| `backend/exchange/nobitex.py` | `NOBITEX_MARKET_DATA_SCALE`, scale OHLC + last price |
| `backend/engine/bot_engine.py` | fill_price capture, max_entry_scale gate |
| `backend/strategy/sizing.py` | `max_scale`, `ExcessiveScalingError`, fill_price in gross |
| `backend/strategy/pnl.py` | `legs_gross_notional` prefers fill_price |
| `backend/engine/backtester.py` | `max_entry_scale` param |
| `backend/engine/init_methods.py` | plumb max_entry_scale / trade_notional |
| `backend/routers/init_router.py` | backbone params |
| `backend/config_service.py` | validate max_entry_scale |
| `config/default_config.yaml` | default 3.0 |
| `frontend/js/main.js` | FIELD_HINTS |
| `tests/test_*.py` | scale, sizing, fill_price, existing pnl gate |

## Acceptance

- Existing `tests/test_pnl_gate.py` unchanged in behaviour
- Only `nobitex.py` performs Rial/Toman conversion
- PnL uses fill_price when present, price otherwise
- Default max_entry_scale does not change behaviour for scale ≤ 3
