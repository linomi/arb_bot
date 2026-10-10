# Stat-Arb Bot (Nobitex margin + XT.com USDT-M perpetuals)

Statistical-arbitrage bot with a built-in web UI. It builds a spread from a
group of correlated symbols (OLS on raw prices), screens it for stationarity,
trades the z-score of the residual (enter at the extremes, exit on reversion
or stop), and runs headless in the background. Paper trading is the default;
live trading is available on **Nobitex** (IRT margin) and **XT.com** (USDT-M
perpetual swaps). One exchange is active at a time. State lives in SQLite.

> **Money warning.** Live mode places real orders. Read
> [section 8](#8-going-live-on-xt-checklist) first, start tiny, and use a
> dedicated API key without withdrawal permission.

## 1. How the strategy works

For a group of symbols, one is the *dependent* `Y` and the rest are
regressors `X1..Xk`.

1. **Fit.** OLS of `Y` on `X` over the last `window_size` bars gives betas
   and an intercept. The residual `e = Y - (a + Σ b·X)` is the spread.
2. **Screen.** The residual must pass a stationarity gate
   (`stationarity_method`: `engle_granger`, the default, or legacy
   `adf_kpss`), plus a half-life filter (`half_life_max_fraction`). Groups
   with more than 5 regressors use a Monte-Carlo p-value.
3. **Signal.** `z = (e - mean) / std` from the fit window.
   - Enter when `|z| >= z_entry` (short the residual when z is high, long
     when low).
   - Exit at `|z| <= z_close`, or stop at `|z| >= z_stop_loss`, or after
     `max_holding_hours`.
   - Betas, mean and std are **frozen** from entry until exit.
4. **Gates before an entry** (each is shown in the *Entry status* column and
   in the `entry scan:` log line): stationarity, half-life, expected profit
   vs. costs (`target_profit_rate`), per-group cooldown after a failed entry,
   portfolio caps (`max_open_trades`, `max_total_gross_notional`), stale
   data, symbols already used by another open trade, and exchange minimums /
   free balance.
5. **Sizing.** Each leg is `trade_notional`-scaled by its beta so the
   basket is beta-weighted; legs are lifted to the exchange minimum when
   needed (capped by `max_entry_scale`) and scaled down to the free balance.
6. **Execution.** Legs are sent one after another as market orders. If one
   fails after others filled, the filled legs are rolled back. Closes use
   reduce-only orders for the actual open size.

## 2. Requirements

- Python 3.11+
- Network access to the exchange you use (`apiv2.nobitex.ir` and/or
  XT.com's public and futures APIs through `ccxt`)

## 3. Install and configure

```bash
cd arb_bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the key into .env as ENCRYPTION_KEY=...
```

Environment variables (`.env`):

| Variable | Purpose |
|---|---|
| `ENCRYPTION_KEY` | Required to save API credentials (Fernet key). |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | Enable the login screen (session cookie, 5 days; HTTP Basic also works for scripts). |
| `SESSION_SECRET` | Optional cookie-signing secret (defaults to `ENCRYPTION_KEY`). |
| `SESSION_HTTPS_ONLY` | `1` to mark the cookie secure behind HTTPS. |
| `HOST` / `APP_PORT` | Bind address (default `127.0.0.1:8000`). Binding to a non-loopback host requires auth to be set. |
| `NOBITEX_BASE_URL` | Mainnet by default; point at the testnet for plumbing tests. |
| `DATABASE_URL` | Defaults to `sqlite:///data/arb_bot.db`. |

## 4. Run

```bash
python run.py
```

Open **http://localhost:8000**. Backend and UI are one process. The trading
loop starts with the server, in **paper** mode and **inactive**; press
ACTIVE in the top bar to start trading. It keeps running with the browser
closed. For a server, run it under systemd.

## 5. Using the UI

1. **Initialization tab.** Edit the Backbone and Initialization parameters,
   pick **Random Groups** or **Sector-Based**, and run. It pulls liquid
   markets from the active exchange, backtests candidate groups with the
   same backbone logic as the live engine, keeps the best by Sharpe/PnL and
   saves them as groups. You can also create a **manual group**.
2. **Backbone tab.**
   - *Config* is split into **Basic** and a collapsible **Advanced**
     section.
   - *Residual chart*: full history (`chart_history_bars`) with the betas of
     the current fit; bars before the fit window are dimmed. Scroll or zoom
     to the left and older candles are fetched on demand. Click an entry or
     close marker to redraw the same chart with the betas that trade was
     entered with (the series morphs between the two); "Back to live"
     returns. Threshold lines mark entry / close / stop.
   - *Equity curve*: start balance plus cumulative PnL, with return %, max
     drawdown %, current/peak margin in use and an optional margin-in-use
     line. Paper mode starts from `paper_start_balance`; live mode starts
     from the wallet balance.
   - *Trade log*: per trade, the gross notional (sum of all legs) and the
     **margin** (gross / leverage: `xt_leverage` on XT, 1x on Nobitex),
     PnL and close reason. **Clear history** deletes this group's finished
     trades for the current mode (open and partially-failed trades are never
     deleted).
   - *Performance comparison*: Win Rate, Profit Factor, Net Profit, Expected
     Payoff and Max Drawdown per group, with the rest (gross profit/loss,
     absolute drawdown, streaks, long/short counts) under "Show all
     metrics", plus a radar chart.
3. **Settings & Credentials tab.** Save API credentials per exchange. Live
   mode is refused without them.
4. **Top bar.** ACTIVE/INACTIVE, Paper/Live (asks for confirmation) and the
   exchange selector. Language: English or Persian.

## 6. Configuration reference

Defaults live in `config/default_config.yaml` and only seed the database;
after that, edit everything in the UI. Missing keys in an existing install
are filled in automatically.

**Basic**

| Key | Meaning |
|---|---|
| `window_size` | Bars used to fit the OLS. |
| `sampling_time` | Seconds between bot cycles (also the candle size). |
| `z_entry` / `z_close` / `z_stop_loss` | Thresholds; require `stop > entry > close`. |
| `stationarity_method` | `engle_granger` (recommended) or `adf_kpss`. |
| `paper_start_balance` | Paper mode: starting capital for the equity curve. |
| `trade_notional` | Basket size before beta-scaling. |
| `fee_rate`, `slippage_rate` | Cost model per side. |
| `target_profit_rate` | Minimum expected net profit, as a fraction of gross notional, to allow an entry (0 disables). |

**Advanced**

| Key | Meaning |
|---|---|
| `adf_alpha`, `kpss_alpha`, `half_life_max_fraction` | Statistical screens. |
| `max_holding_hours` | Time stop (0 = off). |
| `max_entry_scale` | Max inflation of a basket to reach exchange minimums (0 = off). |
| `max_open_trades`, `max_total_gross_notional` | Portfolio caps (0 = off). |
| `liquidation_proximity_fraction` | Force-close when mark is this close to liquidation (0 = off). |
| `entry_retry_cooldown_sec` | Pause for a group after a failed/rolled-back entry. |
| `data_staleness_mult` | Skip entries when a bar is older than this x `sampling_time`. |
| `funding_rate_estimate`, `expected_holding_funding_intervals` | Funding cost assumed in the profit gate. |
| `xt_leverage`, `xt_margin_mode` | XT leverage (applied per symbol, both sides, before the first order) and `isolated`/`cross`. |
| `chart_history_bars`, `fit_log_interval_sec`, `try_all_dependents` | Display / logging / init options. |

## 7. Project layout

```
backend/
  main.py, auth.py, security.py, db.py, models.py, schemas.py
  config_service.py      config sections stored in the DB (+ validation)
  strategy/              ols, stats_tests, half_life, zscore, sizing, pnl, metrics
  engine/
    bot_engine.py        the always-on trading loop (scan, entry, exit, reconcile, risk exits)
    backtester.py        bar-by-bar replay used by initialization
    init_methods.py      random / sector candidate search
    xt_hooks.py          XT rate-limit pause, min notional, balance helpers
  exchange/
    nobitex.py, xt.py    live clients (xt.py uses ccxt, swap/linear, USDT-M)
    paper.py             paper simulator over a real market-data client
    factory.py           picks the client for the active exchange and mode
  routers/               auth, config, groups, init, bot, credentials, manual, symbols
frontend/                vanilla HTML/CSS/JS, no build step
                         (Lightweight Charts for the residual chart, Chart.js for the rest)
config/default_config.yaml
data/arb_bot.db          SQLite (created on first run)
tests/                   pytest suite (run: python -m pytest -q)
```

## 8. Going live on XT: checklist

1. **API key.** On XT create a key with *futures trade* and *read*
   permission, **no withdrawal**, and whitelist your server's IP. Prefer a
   dedicated subaccount funded only with what you can lose. Save it in
   Settings & Credentials.
2. **Minimums.** XT limits are per contract. Read them from
   `https://fapi.xt.com/future/market/v3/public/symbol/list`
   (`minQty`, `minNotional`, `contractSize`, `quantityPrecision`). Every leg
   must clear its symbol's `minNotional`, so a 2-leg basket is realistically
   25-40 USDT gross, and about half of that as margin at 2x leverage.
3. **Conservative config.** Two-symbol groups, `xt_leverage` 2,
   `xt_margin_mode` isolated, `max_open_trades` 1-2,
   `max_total_gross_notional` about 3x `trade_notional`,
   `max_holding_hours` set (24-48), `fee_rate` about 0.0006 (confirm your
   tier), `slippage_rate` 0.0005 or more.
4. **Switch to Live and start one small group.** Watch the log for
   `entry scan:` lines and the *Entry status* column to see why entries are
   skipped.
5. **Verify the first trade on XT:** leverage and margin mode applied on
   both LONG and SHORT sides, both legs filled, no stray positions after
   close; compare the bot's PnL with XT's.
6. Scale up only after several clean round trips.

Safety behaviour you can rely on (and should still verify): leverage/margin
setup is fail-closed (an order is never sent if leverage could not be set);
closes are reduce-only; entries below the exchange minimum after contract
rounding are refused; a rate-limit response pauses the bot for 10 minutes;
open positions on the exchange that the bot does not know about are flagged
as orphans and block entries on their symbols (use a dedicated account);
a position near liquidation is closed.

## 9. Caveats

- Paper PnL is a model (beta-weighted cash PnL with the configured fee and
  slippage); live PnL comes from the exchange when available and otherwise
  from fill prices. They will differ.
- XT funding (every 8 hours) is only approximated by
  `funding_rate_estimate`.
- Markets are fetched through `ccxt` and cached for an hour; limits and
  leverage tiers can change on the exchange side.
- The Nobitex client has been run against the live API only lightly; test
  with small sizes first. Ed25519 signature auth is scaffolded but unused;
  token auth is the supported path.
- Backtests in Initialization are in-sample and optimistic; treat them as a
  ranking, not a forecast.
- `docs/RATE_LIMIT_AUDIT.md` records the exchange rate-limit review.
