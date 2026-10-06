# Rate limit audit (Nobitex & XT)

Values in `backend/exchange/rate_limit.py` as of this PR.

## Nobitex (from code)

| Key | Limit | Period | Notes |
|-----|-------|--------|-------|
| udf_history | 5000 / 60s | market data | Intentionally loose for init fan-out |
| market_stats | 500 / 60s | | |
| margin_markets_list | 500 / 60s | | |
| margin_orders_add | 280 / 600s | trading | |
| positions_list | 25 / 600s | | Conservative vs typical docs |
| positions_status | 90 / 600s | | |
| positions_close | 90 / 600s | | |
| wallets_transfer | 8 / 60s | | |
| wallets_list | 30 / 60s | | |
| delegation_limit | 10 / 60s | | |

**Could not verify against live OpenAPI** in this environment. Official docs change; treat code values as conservative bot-side caps.

## XT (ccxt)

XT limits are enforced by ccxt / exchange headers. Bot pauses on `RateLimitExceeded` via `xt_hooks`. No separate XT table in `rate_limit.py`.

## Recommendation

- Re-check current Nobitex OpenAPI before raising LIVE_TRADING_LIMITS.
- XT: prefer exchange-reported remaining quota when available in ccxt responses.
