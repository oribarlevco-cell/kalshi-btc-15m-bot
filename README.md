# Kalshi 15-Minute BTC Market Bot

Monitors Kalshi's `KXBTC15M` series — Bitcoin up/down contracts that roll over
every 15 minutes, 24/7 — pulls live market data, generates a real-time
prediction from an independent BTC price feed, stores everything locally in
SQLite, and can either propose **paper trades** you confirm one at a time
(`--trade`) or run up to three additional strategies fully automatically
(`multi_trader.py`, also paper-only — see below). A live dashboard on
GitHub Pages shows all of it: market pricing vs. the model's own prediction,
recent results, strategy backtests with confidence intervals, and a
calibration trend.

**This bot only ever trades paper money on Kalshi's `demo` environment.**
There is no code path in this repo that places a real, prod, real-money
order — see [Safety](#safety) below.

## How it works

- `src/kalshi_client.py` — REST client for Kalshi's API: RSA-PSS request
  signing, exponential backoff on rate limits (429) / server errors, GET/POST/DELETE.
- `src/markets.py` — finds the currently-active 15-min BTC market and fetches
  a snapshot (price, bid/ask, volume, open interest, time remaining, strike).
- `src/price_feed.py` — polls a free public BTC-USD feed (Coinbase) to build
  a rolling window of recent prices, used to estimate short-term volatility.
- `src/predictor.py` — turns a market snapshot + price history into a
  real-time `Prediction` (see [The prediction model](#the-prediction-model)).
- `src/orders.py` — Kalshi's authenticated trading endpoints: balance,
  positions, place/cancel order.
- `src/trader.py` — `Trader.on_snapshot()`: always logs a prediction; when
  trading is enabled, proposes at most one order per 15-min market and
  **blocks on your explicit `y/N` confirmation** before ever calling
  `orders.place_order`.
- `src/storage.py` — SQLite schema (`snapshots`, `settled_outcomes`,
  `predictions`, `orders`, `market_lifecycle`, `orderbook_snapshots`) and
  read/write helpers.
- `src/poller.py` — the polling loop: discover → snapshot → store →
  summarize → (optional) `on_snapshot` hook, plus periodic backfill of
  settled markets and periodic DB backups.
- `src/summary.py` — formats a one-line summary of the current market.
- `src/dashboard.py` — one-shot script that writes `docs/data.json`, the
  static fallback for the live dashboard's market tile (see below); reuses
  the same `predictor.py` logic as `--predict`.
- `src/report_calibration.py` — compares predictions to actual outcomes (see
  [Evaluating the model](#evaluating-the-model)).
- `src/backup.py` — SQLite backups + CSV export (see [Backups](#backups)).
- `src/backtest.py` — the single source of truth for what each of the 4
  strategies (model/favorite/momentum/agreement) would bet and why, used
  identically by the historical backtest, the dashboard, and the live
  `multi_trader.py` so they never drift apart.
- `src/live_server.py` — a local, token-gated, read-only HTTP server the
  live dashboard polls directly (see [Live dashboard](#live-dashboard)).
- `src/multi_trader.py` — automated paper trading for momentum/favorite/
  agreement, no per-trade confirmation (see
  [Automated multi-strategy paper trading](#automated-multi-strategy-paper-trading)).

## Evaluating the model

Before ever considering real trading, `--predict`/`--trade` log a full
lifecycle for every market into `market_lifecycle`: open-side (earliest
prediction, BTC price at open, strike) and close-side (Kalshi's actual
settled result, final quoted prices, the model's latest pre-close
prediction) in one row per ticker. Orderbook depth beyond the top bid/ask,
and two extra signals not yet fed into the prediction formula (realized
volatility `sigma_per_sqrt_second`, and price momentum `momentum_pct`), are
logged alongside every prediction for later analysis.

```bash
python -m src.report_calibration            # uses each market's latest pre-close prediction
python -m src.report_calibration --which initial   # uses the earliest prediction instead
```

Prints a calibration table (of markets predicted ~70% YES, did ~70% actually
resolve YES?), the overall Brier score (0 = perfect, 0.25 = no better than a
coinflip), and directional accuracy. With little settled history yet, it
just says so rather than erroring.

## Backups

`data/kalshi_btc15m.db` is local-only (see [Data safety](#data-safety)) and
grows the only real record of this evaluation, so it's backed up
automatically: `poller.run_forever` copies it (via SQLite's online backup
API, safe against a live/writing connection) to `data/backups/` every
`BACKUP_INTERVAL_HOURS` (default 24h), keeping the 10 most recent. Run it
manually any time with `python -m src.backup` (add `--csv` to also export
every table to CSV, `--keep N` to change retention).

## Live dashboard

`docs/` is a static page (plain HTML/CSS/JS, no framework) that renders the
current market as a mini trading interface, plus everything below it:
market pricing (Kalshi's own live bid/ask) and the model's prediction shown
**separately and clearly labeled**, so they're never confused; BTC price vs.
strike and 1-min/15-min momentum; a countdown; the last 10 settled windows;
a 4-strategy backtest table (win rate ± 95% confidence interval, average
P&L, with low-sample-size rows visually flagged); a pattern log; and a
calibration chart with a trend indicator.

**It's read-only.** Nothing on the page places a trade — not the market
pricing tiles, not any button. "Export" downloads whatever's currently
shown as a JSON file; "clear" only resets this browser's own cached
settings and can't touch any real data (see
[Data source: static vs. live](#data-source-static-vs-live) for why that's
structurally true, not just a promise).

### Data source: static vs. live

Backtests, the pattern log, and calibration all come from
`market_lifecycle`/`predictions`/`orderbook_snapshots` — tables that only
exist in your **local** SQLite file (gitignored, never reaches GitHub). A
GitHub Actions runner has no access to it, so there are two layers:

1. **Static fallback** (always on): a scheduled GitHub Actions workflow
   ([.github/workflows/pages.yml](.github/workflows/pages.yml)) runs
   `python -m src.dashboard` roughly every 2 minutes, commits the refreshed
   `docs/data.json` (market tile only — no local DB access), and deploys
   `docs/` to GitHub Pages.
2. **Live** (optional, opt-in): run `python -m src.live_server` on your own
   Mac (needs `LIVE_SERVER_TOKEN` set in `.env` — see below), then tunnel it
   with something like `ngrok http 8899`. Paste the resulting URL into the
   page's gear-icon settings panel (stored only in that browser's
   `localStorage`). The page then polls your machine directly every 15s for
   everything, including the analytics sections; it falls back to the
   static tile automatically if the live server/tunnel goes offline.

**The live server is genuinely a port reachable from the internet while
it's running**, so it's deliberately narrow: binds `127.0.0.1` only (the
tunnel is what exposes it, not the process itself), requires the token on
every request, sets CORS to your Pages origin only, and exposes exactly one
read-only endpoint — there is no path from the public page to any write.

To view the page locally without any of this: `python3 -m http.server 8000
--directory docs`, then open `http://localhost:8000`.

**One-time setup on GitHub** (I can't do this part — it's a repo settings
change): go to the repo's **Settings → Pages** and set **Source** to
**GitHub Actions**. After that, the workflow above publishes the page
automatically; find its URL under Settings → Pages once it's deployed.

## Automated multi-strategy paper trading

`python -m src.multi_trader` runs **momentum**, **favorite**, and
**agreement** automatically — no per-trade confirmation, unlike `--trade`.
This was an explicit, deliberate choice: **paper/demo money only**, real
money is out of scope and there's no code path toward it here.

- **model** — Kalshi's own market-implied favorite at open
- **favorite** — bets whichever side the market already favors at open
- **momentum** — bets the recent BTC momentum direction
- **agreement** — only bets when the model and momentum agree

Position size starts at the same floor as `--trade`
(`MAX_ORDER_COST_DOLLARS`) and only increases once a strategy's *own*
backtested win rate clears an evidence bar — a 95% Wilson confidence
interval lower bound above a coinflip, at a real sample size — not on a
timer or a feeling:

| Tier | Condition | Stake |
|---|---|---|
| 0 (floor) | default | `MAX_ORDER_COST_DOLLARS` |
| 1 | n ≥ `STRATEGY_TIER1_MIN_N` (20) and 95% CI lower bound ≥ `STRATEGY_TIER1_MIN_CI_LOWER` (0.50) | `STRATEGY_TIER1_MULTIPLIER`× floor (2×) |
| 2 | n ≥ `STRATEGY_TIER2_MIN_N` (50) and 95% CI lower bound ≥ `STRATEGY_TIER2_MIN_CI_LOWER` (0.55) | `STRATEGY_TIER2_MULTIPLIER`× floor (4×) |

Every safety invariant from `--trade` still applies (`KALSHI_ENV=demo`,
credentials required) plus its own separate opt-in,
`MULTI_STRATEGY_TRADING_ENABLED` — independent of `TRADING_ENABLED`, so
turning this on is always a deliberate, distinct choice.
`KALSHI_ENV=prod` hard-blocks it exactly like it blocks `--trade`. Each
strategy tracks its own positions (a `strategy` column on `orders`) and
caps itself at `MULTI_STRATEGY_MAX_CONCURRENT_POSITIONS` (default 5) open
positions at a time, independently of the other strategies.

## The prediction model

Kalshi's `KXBTC15M` markets settle on: is BTC's price at close >= the
market's `floor_strike` (a reference price set from the window's own open)?
The real settlement source is a 60-second average of CF Benchmarks' Bitcoin
Real-Time Index (BRTI), which requires a paid license and isn't used here.

Instead, this bot polls a free public BTC-USD feed (Coinbase) and estimates
the probability of finishing above the strike using the standard
"probability a random walk finishes above a barrier" formula — the same math
behind a binary/digital option's delta:

```
P(yes) = Φ(d2),   d2 = ln(current_price / floor_strike) / (σ * sqrt(seconds_remaining))
```

where `σ` is realized volatility estimated from the bot's own recent price
samples (stdev of log returns, scaled per second) and `Φ` is the standard
normal CDF. It assumes zero drift over the short horizon — there's no
reliable short-term BTC drift signal to exploit at this timescale.

**This is a documented statistical approximation, not investment advice.**
Coinbase's spot price is a proxy for BRTI, not the real settlement feed, and
short-horizon crypto volatility is famously unstable. Treat predictions as a
starting point, not a guarantee.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Kalshi's market-data endpoints (`/series`, `/markets`, `/orderbook`, `/trades`)
are **public** — `--once`/monitoring mode and `--predict` run with `.env`
untouched, no API key required. `--trade` requires Kalshi **demo** API
credentials (see [Safety](#safety)).

### Environment variables (`.env`)

| Variable | Required | Description |
|---|---|---|
| `KALSHI_ENV` | no (default `demo`) | `demo` or `prod` — selects the API base URL. `--trade` refuses to run unless this is `demo`. |
| `KALSHI_API_KEY_ID` | no (required for `--trade`) | API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | no (required for `--trade`) | Path to the PEM-encoded RSA private key paired with the key above |
| `POLL_INTERVAL_SECONDS` | no (default `20`) | Seconds between polls |
| `DB_PATH` | no (default `data/kalshi_btc15m.db`) | SQLite file location |
| `SERIES_TICKER` | no (default `KXBTC15M`) | Kalshi series to monitor |
| `PRICE_FEED_URL` | no (default Coinbase BTC-USD ticker) | External BTC price source for predictions |
| `MIN_SAMPLES_FOR_PREDICTION` | no (default `5`) | Minimum price samples before predicting |
| `MIN_SIGNAL_CONFIDENCE` | no (default `0.15`) | Only propose a trade when `abs(P(yes)-0.5)*2` is at least this |
| `MOMENTUM_WINDOW_SECONDS` | no (default `300`) | Lookback window for the (currently unused-by-the-formula) momentum signal |
| `TRADING_ENABLED` | no (default `false`) | Must be `true`, **in addition to** `KALSHI_ENV=demo` and the `--trade` flag, for any order to ever be proposed |
| `MAX_ORDER_COST_DOLLARS` | no (default `5.0`) | Caps the size of each proposed paper order |
| `TRADE_WINDOW_MIN_SECONDS` / `TRADE_WINDOW_MAX_SECONDS` | no (default `60`/`780`) | Only propose trades when time remaining in the 15-min window falls in this range |
| `BACKUP_DIR` | no (default `data/backups`) | Where periodic DB backups/CSV exports go |
| `BACKUP_INTERVAL_HOURS` | no (default `24`) | How often `run_forever` backs up the DB |
| `LIVE_SERVER_TOKEN` | required for `live_server.py` | Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `LIVE_SERVER_PORT` | no (default `8899`) | Local-only port the live server binds |
| `LIVE_SERVER_ALLOWED_ORIGIN` | no (default your Pages origin) | CORS is locked to exactly this origin |
| `LIVE_SERVER_REFRESH_SECONDS` | no (default `15`) | How often the live server recomputes its cached payload |
| `CALIBRATION_SNAPSHOT_INTERVAL_MINUTES` | no (default `15`) | How often a calibration trend point is recorded |
| `MULTI_STRATEGY_TRADING_ENABLED` | no (default `false`) | Separate opt-in for `multi_trader.py`'s automated (unconfirmed) paper trading |
| `STRATEGY_TIER1_MIN_N` / `STRATEGY_TIER1_MIN_CI_LOWER` | no (default `20` / `0.50`) | Tier-1 stake threshold, see [Automated multi-strategy paper trading](#automated-multi-strategy-paper-trading) |
| `STRATEGY_TIER2_MIN_N` / `STRATEGY_TIER2_MIN_CI_LOWER` | no (default `50` / `0.55`) | Tier-2 stake threshold |
| `STRATEGY_TIER1_MULTIPLIER` / `STRATEGY_TIER2_MULTIPLIER` | no (default `2` / `4`) | Stake multipliers at each tier |
| `MULTI_STRATEGY_MAX_CONCURRENT_POSITIONS` | no (default `5`) | Per-strategy cap on simultaneous open positions |

To create an API key/RSA keypair (needed for `--trade`, using your Kalshi
**demo** account), see
[Kalshi's API Keys docs](https://docs.kalshi.com/getting_started/api_keys).
**Never commit the `.env` file or any `.pem` key — both are gitignored.**

## Running

```bash
# Single poll iteration (good for a smoke test)
python -m src.main --once

# Continuous monitoring loop (no predictions, no trading)
python -m src.main

# Also compute and log real-time predictions, no trading
python -m src.main --predict

# Also propose paper trades on Kalshi demo, confirmed one at a time
python -m src.main --trade

# Serve the live dashboard payload locally (needs LIVE_SERVER_TOKEN set)
python -m src.live_server

# Automated paper trading for momentum/favorite/agreement, no confirmation
python -m src.multi_trader
```

Data lands in the SQLite file at `DB_PATH` (default
`data/kalshi_btc15m.db`), which is gitignored. `--trade` requires
`TRADING_ENABLED=true` in `.env` on top of the `--trade` flag itself, plus
demo credentials — run without it and the bot tells you exactly which
condition is missing instead of guessing what you meant.

## Data safety

`data/kalshi_btc15m.db` and everything under `data/backups/` are excluded
via `.gitignore` (confirmed: `*.db` anywhere in the tree, plus an explicit
`data/backups/` line for the CSV exports) — this history never gets pushed
to GitHub. See [Backups](#backups) for how it's protected locally.

## Safety

- **Paper money only, everywhere in this repo.** Both `--trade` and
  `multi_trader.py` place orders exclusively against Kalshi's `demo`
  environment. If `KALSHI_ENV=prod`, both refuse to start — there is no
  code path in this repository that can submit a real order.
- **`--trade` confirms every order individually, in the terminal.** The bot
  never places an order without an explicit `y` in response to a printed
  proposal (ticker, direction, price, count, cost, current demo balance,
  model rationale). Declining, or anything other than `y`, skips it. Three
  separate gates before any proposal is even shown: `TRADING_ENABLED=true`
  in `.env`, the `--trade` CLI flag, and `KALSHI_ENV=demo`.
- **`multi_trader.py` is automated — no per-trade confirmation — but still
  paper-only**, with its own separate opt-in (`MULTI_STRATEGY_TRADING_ENABLED`,
  independent of `TRADING_ENABLED`) plus the same demo/credentials gates.
  Position sizing only escalates against real, evidence-based backtest
  thresholds (see [Automated multi-strategy paper trading](#automated-multi-strategy-paper-trading)) —
  never "as we feel like it."
- At most one order per strategy per 15-min market, and it's skipped
  automatically (no prompt, and for `multi_trader.py` no order at all) if
  your demo balance can't cover it or that strategy already holds a
  position in that market.
- Moving beyond paper trading to real money would be a deliberate, separate
  change to this codebase later — not a config flip.

## Tests

```bash
pytest
ruff check .
```

## Roadmap

- Real BRTI data (vs. the Coinbase proxy used now) would need a CF
  Benchmarks license.
- `predict()`'s own probability formula is intentionally simple (driftless,
  single-feed volatility estimate) and still doesn't use momentum or
  orderbook depth directly — `momentum` is evaluated as its own independent
  strategy (see backtests), and `orderbook_snapshots` is logged but not yet
  used by anything. `report_calibration.py` and the dashboard's backtest
  table are how to judge whether any of this actually helps before changing
  the core formula.
