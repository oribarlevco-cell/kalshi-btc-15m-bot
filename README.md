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
- `src/analytics.py` — builds the settled-windows/backtests/pattern-log/
  calibration payload from the local DB; shared by `live_server.py` and
  `publish_analytics.py` so both stay in sync.
- `src/publish_analytics.py` — writes `docs/analytics.json` and commits+
  pushes it; run periodically by `poller.run_forever` when
  `ANALYTICS_PUBLISH_ENABLED=true` (see [Live dashboard](#live-dashboard)).
- `src/live_server.py` — an optional local, token-gated, read-only HTTP
  server for real-time updates beyond what static publishing gives you
  (see [Live dashboard](#live-dashboard)).
- `src/multi_trader.py` — automated paper trading for momentum/favorite/
  agreement, no per-trade confirmation (see
  [Automated multi-strategy paper trading](#automated-multi-strategy-paper-trading)).

## Evaluating the model

Before ever considering real trading, `--predict`/`--trade` log a full
lifecycle for every market into `market_lifecycle`: open-side (earliest
prediction, BTC price at open, strike) and close-side (Kalshi's actual
settled result, final quoted prices, the model's latest pre-close
prediction) in one row per ticker. Orderbook depth beyond the top bid/ask,
and signals not yet fed into the prediction formula (realized volatility
`sigma_per_sqrt_second`, price momentum `momentum_pct`, a 15-min EMA9/EMA21/
RSI14 trend read, and divergence events — see below), are logged alongside
every prediction for later analysis.

Two of those logged-only signals get their own accuracy tracking (n, win
rate, 95% Wilson CI — same math as the backtest table, no P&L since neither
is a proposed trading strategy):

- **Divergence**: fires when the market's `yes_bid` confidently leans one
  way (above `DIVERGENCE_CONFIDENT_THRESHOLD`, default 0.65, or below
  `1 - threshold`) while BTC's spot price sits on the *other* side of the
  strike. Logged once per market the first time it's seen
  (`divergence_events`); once the market settles, whether that divergence's
  implied direction was actually correct gets scored. Shown live as an
  amber banner on the dashboard.
- **15-min EMA/RSI trend**: `EMA9` vs `EMA21` crossover plus `RSI14`,
  computed from Coinbase's 15-min candles (`EMA_RSI_CANDLES_URL`) once per
  new market (not every tick). Classified bull/bear/neutral using the same
  rule as the reference implementation this was adapted from
  (`ema9>ema21 and rsi<65` → bull, `ema9<ema21 and rsi>35` → bear, else
  neutral). **Not read by `predictor.predict()`** — same rigor as
  `momentum_pct` — it only ever gets wired into the live model, or offered
  as a `multi_trader` strategy, after its own settled-history Wilson CI
  lower bound clears a coinflip, and that would be a separate, explicit
  change.

Every settled window is also labeled by how it was captured: **observed**
(watched live — `--predict`/`--trade` was running and saw the market open)
vs. **backfill** (only ever seen after the fact, via
`poller.backfill_settled`). Shown as reduced-opacity pills on the dashboard
and an "observed X / backfilled Y" count in the pattern log.

```bash
python -m src.report_calibration            # uses each market's latest pre-close prediction
python -m src.report_calibration --which initial   # uses the earliest prediction instead
```

Prints a calibration table (of markets predicted ~70% YES, did ~70% actually
resolve YES?), the overall Brier score (0 = perfect, 0.25 = no better than a
coinflip), and directional accuracy. With little settled history yet, it
just says so rather than erroring.

## Historical backtesting (standalone)

`backtesting/backtest_engine.py` is a self-contained addition, separate from
everything above — it never touches `data/kalshi_btc15m.db`, never calls
Kalshi's API, and doesn't import `src/trader.py`/`src/orders.py`, so it can't
interfere with the live bot running via `nohup`. Instead of the settled
markets the bot has actually observed (what `report_calibration` above
scores), it replays `src.predictor.predict()` — the bot's real prediction
math — against months of real BTC price history pulled from Binance's free
public API, so you can evaluate the model over far more history than the
bot has accumulated live.

```bash
pip3 install -r backtesting/requirements.txt   # pandas/numpy, on top of the bot's own requirements.txt

python3 backtesting/backtest_engine.py                       # last 90 days, BTCUSDT
python3 backtesting/backtest_engine.py --days 30              # shorter window
python3 backtesting/backtest_engine.py --min-confidence 0.25  # override MIN_SIGNAL_CONFIDENCE for this run
```

Each run downloads 1-min BTCUSDT klines (caching them in `backtesting/data/`
so reruns only fetch the new tail), resamples to 15-min windows, and walks
each window tick-by-tick through a real `PriceFeed` and `predictor.predict()`
using the same confidence/trade-window gating `Trader._should_propose_trade`
uses live — so it only "trades" when the live bot would have. Results land
in `backtesting/results/` as two timestamped CSVs per run (so you can compare
runs over time): `..._trades.csv` (one row per simulated trade — direction,
model probability, entry price, fee, actual outcome, P&L) and
`..._summary.csv` (win rate, total P&L, fees, max drawdown, Sharpe).

**Read the caveats at the top of `backtest_engine.py` before trusting the
numbers** — Kalshi's real historical strikes and order-book quotes aren't
publicly available, so the script approximates each window's strike as its
open price and assumes trades fill at the model's own fair-value probability
(zero slippage/mispricing). That means results mainly show whether the
model's probabilities are *calibrated* against realized BTC moves, not
whether it can out-price Kalshi's actual market.

### Known finding: tail overconfidence

A 90-day run's calibration report shows the model's most extreme calls
overstate their own certainty: at 90-100% predicted confidence, only ~82%
actually land that way — and that bucket has the worst per-trade P&L of any
confidence band (the model's most confident trades lose the most money).
The 20-80% range is calibrated fine; only the tails are off.

`backtesting/calibration_fix.py` walk-forward tests two candidate fixes
(never touching `src/predictor.py` — this is backtest-only). Platt scaling
(smoothly compressing log-odds) looked good on a single train/test split but
turned out unstable under proper walk-forward validation — it only improved
the Brier score in 3 of 9 sequential out-of-sample folds; its apparent P&L
gain was mostly an artifact of this backtest's "fill at the model's own
probability" assumption (shrinking any confident claim mechanically lowers
the modeled cost, regardless of whether calibration actually improved). A
simple hard cap on the probability (clip to e.g. `[0.10, 0.90]`) did better:
it improved the Brier score in 9 of 9 folds, consistently, because the
miscalibration is concentrated at the extremes rather than being a smooth
log-odds problem — matching a targeted fix beats a smooth one here.

This is a real, reproducible signal but not a validated fix yet: the cap
bounds were a first guess, not tuned (tuning them against the same 9 folds
repeatedly would just be overfitting), and it only narrows the tail gap
(~13pts down to ~7.5pts), it doesn't close it. Promoting anything from this
into the live model would be a separate, deliberate change to
`src/predictor.py` — not something either script does automatically.

```bash
python3 backtesting/calibration_fix.py                                   # walk-forward Platt scaling
python3 backtesting/calibration_fix.py --method cap --cap-lo 0.1 --cap-hi 0.9
```

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
strike, 1-min/15-min momentum, and the 15-min EMA/RSI trend read; a
countdown that pulses red (and plays a one-time 3-tone chime) in the final
60 seconds of a window; a divergence banner when spot and the market
confidently disagree; the last 10 settled windows (backfilled ones shown at
reduced opacity); a 4-strategy backtest table (win rate ± 95% confidence
interval, average P&L, with low-sample-size rows visually flagged); a
pattern log (including the divergence/trend accuracy rows and an observed
vs. backfilled count); and a calibration chart with a trend indicator.

**It's read-only.** Nothing on the page places a trade — not the market
pricing tiles, not any button. "Export" downloads whatever's currently
shown as a JSON file; "clear" only resets this browser's own cached
settings and can't touch any real data (see
[Data source: static vs. live](#data-source-static-vs-live) for why that's
structurally true, not just a promise).

### Data source: static publish vs. live server

Everything the dashboard shows comes from `market_lifecycle`/`predictions`/
`orderbook_snapshots` — tables that only exist in your **local** SQLite file
(gitignored, never reaches GitHub). A GitHub Actions runner has no access to
it, so getting anything onto the public page always starts on your Mac. Two
independent layers publish to two separate files, so they never conflict:

1. **The live tile** (`docs/data.json`) — a scheduled GitHub Actions
   workflow ([.github/workflows/pages.yml](.github/workflows/pages.yml))
   runs `python -m src.dashboard` roughly every 5 minutes, straight from
   Kalshi's/Coinbase's public APIs (no local DB access needed for this
   part), and deploys `docs/` to GitHub Pages. Always on, zero setup.
   (A shorter cron was tried first but GitHub silently throttles/drops
   sub-5-minute schedules on free-tier private repos — if the countdown
   ever looks frozen, check the workflow's Actions tab, or re-trigger it
   manually with the "Run workflow" button.)
2. **Everything historical/aggregate** (`docs/analytics.json`) — recent
   settled windows, the backtest table, pattern log, and calibration trend
   don't need to be real-time, so instead of a live connection they're
   published periodically from your own already-running `--predict`/`--trade`
   process: set `ANALYTICS_PUBLISH_ENABLED=true` and it writes
   `docs/analytics.json` and commits+pushes it every
   `ANALYTICS_PUBLISH_INTERVAL_MINUTES` (default 10) — nothing extra to run.
   The plain GitHub Pages URL shows all of this with no live server and no
   ngrok. (Run `python -m src.publish_analytics` any time for an
   on-demand push instead of waiting for the timer.)

**The live server (`python -m src.live_server`) is optional**, and reserved
for the one thing the above genuinely can't do — a market tile that updates
faster than ~5 minutes. It's a real port reachable from the internet while
it's running (via your own tunnel, e.g. `ngrok http 8899`), so it's
deliberately narrow: binds `127.0.0.1` only (the tunnel is what exposes it,
not the process itself), requires a token (`LIVE_SERVER_TOKEN`) on every
request, sets CORS to your Pages origin only, and exposes exactly one
read-only endpoint — there is no path from the public page to any write.
Paste the tunnel URL into the page's gear-icon settings panel (stored only
in that browser's `localStorage`) and it polls your machine directly every
15s for everything, including analytics; it falls back to the static files
automatically if the live server/tunnel goes offline.

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
| `DIVERGENCE_CONFIDENT_THRESHOLD` | no (default `0.65`) | How confident `yes_bid` must be (above this, or below `1-this`) to count as a divergence when it disagrees with spot vs. strike |
| `EMA_RSI_CANDLES_URL` | no (default Coinbase BTC-USD 15-min candles) | Candle source for the logged-only EMA9/EMA21/RSI14 trend signal |
| `TRADING_ENABLED` | no (default `false`) | Must be `true`, **in addition to** `KALSHI_ENV=demo` and the `--trade` flag, for any order to ever be proposed |
| `MAX_ORDER_COST_DOLLARS` | no (default `5.0`) | Caps the size of each proposed paper order |
| `TRADE_WINDOW_MIN_SECONDS` / `TRADE_WINDOW_MAX_SECONDS` | no (default `60`/`780`) | Only propose trades when time remaining in the 15-min window falls in this range |
| `BACKUP_DIR` | no (default `data/backups`) | Where periodic DB backups/CSV exports go |
| `BACKUP_INTERVAL_HOURS` | no (default `24`) | How often `run_forever` backs up the DB |
| `ANALYTICS_PUBLISH_ENABLED` | no (default `false`) | Publish `docs/analytics.json` (settled windows/backtests/pattern log/calibration) to GitHub periodically |
| `ANALYTICS_PUBLISH_INTERVAL_MINUTES` | no (default `10`) | How often `run_forever` publishes analytics when enabled |
| `LIVE_SERVER_TOKEN` | required for `live_server.py` (optional feature) | Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
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

# One-off: publish docs/analytics.json now instead of waiting for the timer
python -m src.publish_analytics
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
