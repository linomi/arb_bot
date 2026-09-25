# feat: profit-target entry gate + backtest gross-notional fix

## Summary

**Task A – Profit-target entry gate (`target_profit_rate`)**

Only open a position if a perfect close at `|z| = z_close` would leave net profit (after round-trip costs) ≥ `target_rate` of gross notional `G = P_y + Σ|β|P_x`, and the required entry z is not too close to the stop (`z_min ≤ z_stop − 0.75`).

If `target_profit_rate <= 0` the gate is disabled.

**Task B – Backtest gross-notional fix**

Use correct `G = P_y + Σ|β|P_x` instead of `P_y * (1 + Σ|β|)` for cost and ranking.

## Math

```
sigma_rel = resid_std / G
edge_frac = sigma_rel * (|z_now| - z_close)
cost_frac = 2 * cost_rate
net_frac  = edge_frac - cost_frac
z_min     = z_close + (cost_frac + target_rate) / sigma_rel
```

Accept when `net_frac >= target_rate` and `z_min <= z_stop - stop_margin`.

## Files

| File | Change |
|------|--------|
| `backend/strategy/pnl.py` | `gross_per_unit_y`, `entry_target_check` |
| `backend/engine/bot_engine.py` | Gate in `_check_entry` |
| `backend/engine/backtester.py` | Gate + correct G |
| `backend/engine/init_methods.py` | Pass `target_profit_rate` |
| `backend/config_service.py` | Validate `[0,1)`, additive merge |
| `backend/routers/init_router.py` | Backbone params |
| `config/default_config.yaml` | Default `0.01` |
| `frontend/js/main.js` | FIELD_HINTS |
| `tests/test_pnl_gate.py` | Unit tests |

## Compatibility

- Additive config: existing installs get the new key without overwriting user values.
- Gate off when `target_profit_rate <= 0`.
- No exit-logic changes.
