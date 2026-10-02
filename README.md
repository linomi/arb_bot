# Stat-Arb Bot (Nobitex)

Statistical-arbitrage bot: OLS spread construction, ADF+KPSS stationarity
screening, z-score mean-reversion entries/exits, paper trading by default,
optional live trading against Nobitex. Full web UI (dashboard + init +
settings), SQLite storage (no CSVs), background trading loop that survives
the browser being closed.

## 1. Requirements

- Python 3.11+ (3.10 probably works too, untested)
- Internet access from wherever you run this (to reach `apiv2.nobitex.ir`
  for market data, and for real orders in live mode)

## 2. Install

```bash
cd arb_bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configure

```bash
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the printed key into .env as ENCRYPTION_KEY=...
```

Leave `NOBITEX_BASE_URL` as the mainnet URL, or point it at
`https://testnetapiv2.nobitex.ir` while you're testing live-order plumbing.

### Basic authentication (recommended)

To protect the UI and API, set both values in `.env`:

```bash
AUTH_USERNAME=admin
AUTH_PASSWORD=your-strong-password
# Optional (defaults to ENCRYPTION_KEY):
# SESSION_SECRET=another-random-string
```

After restart you will see a login screen. A signed session cookie is issued
and stays valid for **5 days**. HTTP Basic Auth also works for scripts
(`curl -u user:pass ...`). If the two variables are left empty, auth stays
disabled (previous behaviour).

## 4. Run

```bash
python run.py
```

Open **http://localhost:8000** in a browser. That's the whole app --
backend + UI are served from the same process.

The trading loop starts automatically with the server (in **paper** mode,
**inactive**, per the safety defaults) and keeps running in the background
regardless of whether a browser tab is open. Use the ACTIVE/INACTIVE button
in the top bar to actually turn trading on.

## 5. First run walkthrough

1. **Initialization tab** -- review/edit the Backbone and Initialization
   parameters (every parameter from the spec is editable here, no hidden
   limits). Pick **Random Groups** or **Sector-Based**, then click
   **Run Initialization**. This:
   - pulls the top-N most liquid markets from Nobitex,
   - builds candidate groups (random combinations, or same-sector
     combinations from `backend/sectors.py`),
   - backtests each candidate through the exact same backbone logic the
     live engine uses,
   - keeps the top-N by a Sharpe/PnL score,
   - saves them as `Group` rows (active by default) in `data/arb_bot.db`.
2. **Backbone tab** -- pick a group on the left. You'll see the residual
   plot with the z-entry/z-close/z-stop-loss bands, and trade markers once
   the bot has run a few cycles. Click a marker to see the frozen OLS that
   produced that specific trade. The trade log and the sortable performance
   comparison table (bottom) update from the same data.
3. Flip the top-bar toggle to **ACTIVE** to start trading (paper mode by
   default -- no real orders, just simulated fills against real market
   prices).
4. **Settings & Credentials tab** -- only needed for live trading. Paste
   either an API **token** (`Authorization: Token <token>`, the simple
   path) or a Key+Ed25519 secret pair if your account requires signature
   auth. Then switch the top-bar Mode dropdown to **Live** (this asks for
   confirmation).

## 6. Project layout

```
backend/
  main.py              FastAPI app, startup wiring, SessionMiddleware
  auth.py              username/password + 5-day session cookie
  models.py            SQLAlchemy models (Group, OLSFit, Trade, Credential, BotState)
  db.py                SQLite engine/session
  config_service.py    reads/writes config sections stored in the DB
  security.py          Fernet encryption for credentials at rest
  sectors.py           heuristic sector map for the sector-based init method
  schemas.py           pydantic request/response models
  strategy/
    ols.py             OLS fit + frozen-fit residual calc
    stats_tests.py     ADF + KPSS stationarity screen
    zscore.py          entry/close/stop-loss decision rules
    metrics.py         win rate / Sharpe / Sortino / max DD / etc.
  engine/
    backtester.py      bar-by-bar replay of the backbone logic
    init_methods.py    random-group and sector-based candidate search + pruning
    bot_engine.py       the actual always-on trading loop
  exchange/
    base.py            shared client interface
    nobitex.py         real Nobitex REST client (market data + orders)
    paper.py           paper-trading simulator (real prices, simulated fills)
    factory.py         picks paper vs live client from current settings
  routers/             FastAPI route handlers (auth/config/groups/init/bot/credentials)
frontend/
  index.html, css/, js/  vanilla HTML/CSS/JS UI (Chart.js from cdnjs), no build step
config/default_config.yaml  seeds the DB on first run only
data/arb_bot.db              SQLite database (created on first run)
```

## 7. Notes, caveats, and things to double-check before real money

- **The Nobitex client was written directly from the OpenAPI docs you
  provided and has never been exercised against the live API** (this
  sandbox has no network access). Before live trading: smoke-test
  `backend/exchange/nobitex.py` by hand against
  `https://testnetapiv2.nobitex.ir`, especially `place_order`'s field
  names/units and the `/market/stats` key format used by
  `get_liquid_symbols` (assumed to be `"btc-rls"`-style keys -- adjust
  `_stat_key_to_symbol` if your account's response differs).
- **Signature auth (Ed25519) is scaffolded but not wired up**
  (`sign_ed25519` in `nobitex.py`). Token auth
  (`Authorization: Token <token>`) is implemented and is the simpler path
  if your API key supports it.
- **Position sizing** is a simple `trade_notional / price` per leg, not
  beta-weighted. For real trading you'll likely want to size the
  independent legs by their OLS beta so the position is actually
  dollar/beta-neutral -- the hook is `BotEngine._opening_side` /
  `_check_entry` in `bot_engine.py`.
- **PnL accounting** in both the backtester and the live engine is a
  unit-notional log-spread approximation (good for ranking/comparing
  groups and for a paper-trading feel); it is not a full ledger of actual
  fills, partial fills, or funding costs.
- **Sector map** (`backend/sectors.py`) is a small hand-maintained
  dictionary, not pulled from a live source -- extend it as you add
  markets.
- The dependent symbol for a group is currently just "the first symbol in
  the group." Swap in a different selection rule in `init_methods.py` /
  the group-creation code if you want it chosen differently (e.g. most
  liquid, or lowest ADF p-value across choices).
- **Paper mode is the default and stays the default across restarts**
  (`BotState.trading_mode`) until you explicitly switch it from the UI.
