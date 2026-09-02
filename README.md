# Kalshi 15-Minute BTC Market Bot

Monitors Kalshi's `KXBTC15M` series — Bitcoin up/down contracts that roll over
every 15 minutes, 24/7 — pulls live market data, generates a real-time
prediction from an independent BTC price feed, stores everything locally in
SQLite, and (optionally) proposes **paper trades** on Kalshi's demo account
that you confirm one at a time.

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
  `predictions`, `orders`) and read/write helpers.
- `src/poller.py` — the polling loop: discover → snapshot → store →
  summarize → (optional) `on_snapshot` hook, plus periodic backfill of
  settled markets.
- `src/summary.py` — formats a one-line summary of the current market.
- `src/dashboard.py` — one-shot script that writes `docs/data.json` for the
  live dashboard (see below); reuses the same `predictor.py` logic as `--predict`.

## Live dashboard

`docs/` is a small static page (plain HTML/CSS/JS, no framework) that renders
the current market as a mini trading interface: BTC price vs. strike, a
countdown to close, the model's probability as a bar, and YES/NO price tiles.

**It's read-only.** The YES/NO tiles are styled like buttons but aren't —
they don't call any API and can't place a trade. If we wire up clickable
paper trades later, that'll be a separate, explicitly-confirmed change.

A scheduled GitHub Actions workflow ([.github/workflows/pages.yml](.github/workflows/pages.yml))
runs `python -m src.dashboard` roughly every 2 minutes, commits the refreshed
`docs/data.json`, and deploys `docs/` to GitHub Pages. The page itself also
polls `data.json` every 20s and runs its own client-side countdown in
between, so the timer doesn't visibly jump. Because `src/dashboard.py` runs
fresh each time (no long-lived process to build up a price history like
`--predict` has), it bootstraps a handful of BTC price samples a few seconds
apart at the start of each run before predicting.

To view it locally: `python3 -m http.server 8000 --directory docs`, then
open `http://localhost:8000`.

**One-time setup on GitHub** (I can't do this part — it's a repo settings
change): go to the repo's **Settings → Pages** and set **Source** to
**GitHub Actions**. After that, the workflow above publishes the page
automatically; find its URL under Settings → Pages once it's deployed.

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
| `TRADING_ENABLED` | no (default `false`) | Must be `true`, **in addition to** `KALSHI_ENV=demo` and the `--trade` flag, for any order to ever be proposed |
| `MAX_ORDER_COST_DOLLARS` | no (default `5.0`) | Caps the size of each proposed paper order |
| `TRADE_WINDOW_MIN_SECONDS` / `TRADE_WINDOW_MAX_SECONDS` | no (default `60`/`780`) | Only propose trades when time remaining in the 15-min window falls in this range |

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
```

Data lands in the SQLite file at `DB_PATH` (default
`data/kalshi_btc15m.db`), which is gitignored. `--trade` requires
`TRADING_ENABLED=true` in `.env` on top of the `--trade` flag itself, plus
demo credentials — run without it and the bot tells you exactly which
condition is missing instead of guessing what you meant.

## Safety

- **Paper money only.** `--trade` places orders exclusively against Kalshi's
  `demo` environment. If `KALSHI_ENV=prod`, `--trade` refuses to start —
  there is no code path in this repository that can submit a real order.
- **Every order is confirmed by you, individually, in the terminal.** The
  bot never places an order without an explicit `y` in response to a printed
  proposal (ticker, direction, price, count, cost, current demo balance,
  model rationale). Declining, or anything other than `y`, skips it.
- **Three separate gates before any proposal is even shown:**
  `TRADING_ENABLED=true` in `.env`, the `--trade` CLI flag, and
  `KALSHI_ENV=demo` — all three, every time.
- At most one proposal per 15-min market, and it's skipped automatically (no
  prompt) if your demo balance can't cover it or you already hold a position
  in that market.
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
- The prediction model is intentionally simple (driftless, single-feed
  volatility estimate); a next step would be backtesting it against the
  `settled_outcomes` table this bot already collects.
