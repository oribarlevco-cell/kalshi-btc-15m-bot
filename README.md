# Kalshi 15-Minute BTC Market Bot

Monitors Kalshi's `KXBTC15M` series — Bitcoin up/down contracts that roll over
every 15 minutes, 24/7 — pulls live and historical market data, and stores it
locally in SQLite for analysis.

**This bot is monitoring/data-collection only. It never places orders.** See
[`src/strategy.py`](src/strategy.py) for the placeholder where a future
trading strategy would plug in.

## How it works

- `src/kalshi_client.py` — REST client for Kalshi's public market-data API,
  with optional RSA-PSS request signing and exponential backoff on
  rate limits (429) / server errors.
- `src/markets.py` — finds the currently-active 15-min BTC market, fetches a
  snapshot (price, bid/ask, volume, open interest, time remaining), and pulls
  historical settled outcomes.
- `src/storage.py` — SQLite schema (`snapshots`, `settled_outcomes`) and
  read/write helpers.
- `src/poller.py` — the polling loop: discover → snapshot → store →
  summarize, plus periodic backfill of settled markets.
- `src/summary.py` — formats a one-line summary of the current market.
- `src/strategy.py` — placeholder hook for a future trading signal (returns
  `None`; never acted on).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Kalshi's market-data endpoints (`/series`, `/markets`, `/orderbook`, `/trades`)
are **public** — the bot runs with `.env` untouched, no API key required.

### Environment variables (`.env`)

| Variable | Required | Description |
|---|---|---|
| `KALSHI_ENV` | no (default `demo`) | `demo` or `prod` — selects the API base URL |
| `KALSHI_API_KEY_ID` | no | API key ID, only needed for authenticated endpoints |
| `KALSHI_PRIVATE_KEY_PATH` | no | Path to the PEM-encoded RSA private key paired with the key above |
| `POLL_INTERVAL_SECONDS` | no (default `20`) | Seconds between polls |
| `DB_PATH` | no (default `data/kalshi_btc15m.db`) | SQLite file location |
| `SERIES_TICKER` | no (default `KXBTC15M`) | Kalshi series to monitor |

To create an API key/RSA keypair (only needed for authenticated endpoints),
see [Kalshi's API Keys docs](https://docs.kalshi.com/getting_started/api_keys).
**Never commit the `.env` file or any `.pem` key — both are gitignored.**

## Running

```bash
# Single poll iteration (good for a smoke test)
python -m src.main --once

# Continuous polling loop
python -m src.main
```

Data lands in the SQLite file at `DB_PATH` (default
`data/kalshi_btc15m.db`), which is gitignored.

## Tests

```bash
pytest
ruff check .
```

## Roadmap

Trading strategy / signal generation plugs into `evaluate_signal()` in
[`src/strategy.py`](src/strategy.py). Wiring it up to actually place orders
is a deliberate, separate step — not part of this monitoring-only phase.
