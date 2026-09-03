"""
Backtesting Engine for the Kalshi BTC 15-min Prediction Bot
=============================================================

Standalone, read-only addition. This script never imports anything that
writes to the live bot's state (src/storage.py, src/trader.py, src/orders.py
are untouched) and never talks to Kalshi's API -- it only reads Binance's
public market-data endpoint and the bot's own prediction/config modules.
Nothing here can interfere with the live paper-trading bot running via nohup.

What this actually tests
-------------------------
`predict()` below is NOT a re-implemented copy of the live strategy -- it
calls `src.predictor.predict()` directly, fed by a real `src.price_feed.
PriceFeed` populated with historical samples. That function computes P(BTC
closes >= strike) as a driftless random walk / binary-option N(d2) formula
using realized volatility from recent price samples (see src/predictor.py
and src/price_feed.py for the exact math). Reusing the real modules means
this backtest can never silently drift out of sync with what the live bot
does.

IMPORTANT ASSUMPTIONS / LIMITATIONS -- read before trusting the numbers
------------------------------------------------------------------------
1. Strike price: Kalshi's actual historical KXBTC15M strikes aren't public.
   Each synthetic 15-min window's floor_strike is approximated as that
   window's OPEN price, matching Kalshi's real convention of setting the
   strike at/near spot when a window opens.
2. Volatility resolution: the live bot samples BTC roughly every ~20s
   (Coinbase ticker) to estimate realized volatility. Binance's free public
   API only goes down to 1-min candles, so volatility here is estimated
   from coarser samples than production uses -- expect some mismatch.
3. Fill price / edge: there's no public historical feed for Kalshi's actual
   yes/no bid-ask. This backtest assumes every trade fills at the MODEL'S
   OWN fair-value probability (zero mispricing, zero slippage). That means
   the P&L here measures calibration skill -- whether realized outcomes
   track the model's probability estimates -- NOT whether the live market
   misprices contracts relative to the model. Real paper-trading P&L (which
   trades against Kalshi's actual order book) will differ.
4. Fees use Kalshi's publicly documented per-contract fee formula (fee =
   ceil(0.07 * price * (1 - price) * 100) / 100 dollars per contract).
   Verify the current fee schedule for KXBTC15M on kalshi.com -- Kalshi
   varies the multiplier by market/series.

Dependencies (separate from the live bot's requirements.txt):
    pip3 install -r backtesting/requirements.txt

Run:
    python3 backtesting/backtest_engine.py
    python3 backtesting/backtest_engine.py --days 30 --min-confidence 0.2
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

# Make the project root importable so we can reuse the bot's real modules
# without copying their logic. Read-only imports -- nothing here writes to
# src/storage.py's DB or calls src/orders.py's Kalshi trading endpoints.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings, load_settings  # noqa: E402
from src.markets import MarketSnapshot  # noqa: E402
from src.predictor import SUPPORTED_STRIKE_TYPE  # noqa: E402
from src.predictor import predict as live_predict  # noqa: E402
from src.price_feed import PriceFeed, PriceSample  # noqa: E402

BACKTEST_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BACKTEST_DIR / "results"
CACHE_PATH = BACKTEST_DIR / "data" / "btc_1m_cache.csv"

# api.binance.com geo-blocks some regions (HTTP 451) -- e.g. it 451s US IPs
# and redirects US users to Binance.US, which mirrors the same public klines
# schema. Tried in order; on a 451 we fall back to the next host for the
# rest of the run instead of retrying a block that will never clear.
BINANCE_HOSTS = ["https://api.binance.com", "https://api.binance.us"]
BINANCE_MAX_LIMIT = 1000  # max klines per request

# Kalshi's publicly documented general per-contract trading fee multiplier.
# fee_dollars = ceil(KALSHI_FEE_RATE * count * price * (1 - price) * 100) / 100
# Some series use a different multiplier -- confirm KXBTC15M's current rate.
KALSHI_FEE_RATE = 0.07
CONTRACT_PAYOUT = 1.00  # Kalshi contracts settle at $1.00 if correct, $0 if not


# ---------------------------------------------------------------------
# 1. DATA LOADING -- real historical BTC data from Binance's public API
# ---------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    """A 451 (geo-block) won't clear on retry -- let it propagate immediately
    so the caller can fall back to the next host instead of burning retries."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code != 451
    return isinstance(exc, requests.exceptions.RequestException)


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=30),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
def _fetch_klines_batch(
    session: requests.Session, base_url: str, symbol: str, start_ms: int, end_ms: int
) -> list[list]:
    params = {
        "symbol": symbol,
        "interval": "1m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": BINANCE_MAX_LIMIT,
    }
    response = session.get(f"{base_url}/api/v3/klines", params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def _download_1m_klines(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Page through Binance's public klines endpoint (no API key needed) in
    1000-bar chunks to cover [start, end), falling back across BINANCE_HOSTS
    if the active one geo-blocks us."""
    session = requests.Session()
    rows: list[list] = []
    cursor_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step_ms = BINANCE_MAX_LIMIT * 60_000  # 1000 one-minute bars per request
    host_idx = 0

    while cursor_ms < end_ms:
        batch_end_ms = min(cursor_ms + step_ms, end_ms)
        while True:
            base_url = BINANCE_HOSTS[host_idx]
            try:
                batch = _fetch_klines_batch(session, base_url, symbol, cursor_ms, batch_end_ms)
                break
            except requests.exceptions.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 451 or host_idx + 1 >= len(BINANCE_HOSTS):
                    raise
                host_idx += 1
                print(f"{base_url} is geo-blocked from this network; falling back to {BINANCE_HOSTS[host_idx]}")
        if not batch:
            cursor_ms = batch_end_ms
            continue
        rows.extend(batch)
        cursor_ms = batch[-1][0] + 60_000  # next bar after the last one returned
        time.sleep(0.2)  # polite pacing for Binance's public rate limits

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ],
    )
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df.set_index("timestamp")[["open", "high", "low", "close", "volume"]].sort_index()


def fetch_binance_1m_klines(
    days: int = 90,
    symbol: str = "BTCUSDT",
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch at least the last `days` days of 1-min BTCUSDT bars from
    Binance's free public API. Caches to backtesting/data/btc_1m_cache.csv
    so repeat runs only download the new tail instead of re-fetching the
    whole window (90 days of 1-min bars is ~130k rows / ~130 requests)."""
    now = datetime.now(timezone.utc)
    range_start = now - timedelta(days=days)

    cached = pd.DataFrame()
    if use_cache and CACHE_PATH.exists():
        cached = pd.read_csv(CACHE_PATH, index_col="timestamp", parse_dates=["timestamp"])
        if cached.index.tz is None:
            cached.index = cached.index.tz_localize("UTC")

    fetch_start = range_start
    if not cached.empty:
        fetch_start = max(range_start, cached.index.max().to_pydatetime() + timedelta(minutes=1))

    fresh = pd.DataFrame()
    if fetch_start < now:
        print(f"Downloading BTCUSDT 1-min klines from Binance: {fetch_start} -> {now}")
        fresh = _download_1m_klines(symbol, fetch_start, now)
    else:
        print("Cache already covers the requested range; skipping download.")

    combined = pd.concat([cached, fresh]) if (not cached.empty or not fresh.empty) else fresh
    if combined.empty:
        raise RuntimeError("No BTC price data available (Binance returned nothing).")

    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined = combined[combined.index >= range_start]

    if use_cache:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        combined.to_csv(CACHE_PATH)

    return combined


def resample_to_15min(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-min OHLCV bars into 15-min bars aligned to Kalshi's
    KXBTC15M window boundaries."""
    return df.resample("15min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()


# ---------------------------------------------------------------------
# 2. TRADE SIMULATION -- reuses the live bot's real predict() logic
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class BacktestTrade:
    window_open: datetime
    window_close: datetime
    decision_time: datetime
    floor_strike: float
    btc_price_at_decision: float
    direction: str  # "yes" or "no"
    probability_yes: float
    confidence: float
    entry_price: float
    fee: float
    actual_close_price: float
    actual_result: str  # "yes" or "no"
    won: bool
    pnl: float


def kalshi_fee(price: float, count: int = 1, fee_rate: float = KALSHI_FEE_RATE) -> float:
    """Kalshi's publicly documented per-contract trading fee, rounded up to
    the next cent. See the module docstring's limitation #4."""
    raw_cents = fee_rate * count * price * (1 - price) * 100
    return math.ceil(raw_cents) / 100


def simulate_window(
    window_1m: pd.DataFrame,
    window_close: datetime,
    floor_strike: float,
    actual_close_price: float,
    settings: Settings,
) -> Optional[BacktestTrade]:
    """Replays one 15-min window tick-by-tick through a real PriceFeed and
    the live predict() function, using the same gating the live Trader uses
    (src/trader.py's _should_propose_trade): confidence threshold and the
    trade-window seconds-remaining band. Takes at most one trade per
    window -- the first tick that qualifies -- mirroring live behavior."""
    if window_1m.empty:
        return None

    feed = PriceFeed(url=settings.price_feed_url)
    actual_result = "yes" if actual_close_price >= floor_strike else "no"

    for ts, row in window_1m.iterrows():
        sample_time = ts.to_pydatetime()
        remaining = (window_close - sample_time).total_seconds()
        if remaining <= 0:
            break

        # Directly populate the same rolling buffer PriceFeed.fetch_and_record()
        # would build from live ticks -- avoids any network call in a backtest.
        feed._samples.append(PriceSample(ts=sample_time, price=float(row["close"])))

        if not (settings.trade_window_min_seconds <= remaining <= settings.trade_window_max_seconds):
            continue
        if feed.sample_count < settings.min_samples_for_prediction:
            continue

        snapshot = MarketSnapshot(
            ticker=f"BACKTEST-{window_close.isoformat()}",
            event_ticker="",
            status="open",
            yes_bid=None,
            yes_ask=None,
            no_bid=None,
            no_ask=None,
            last_price=None,
            volume=None,
            volume_24h=None,
            open_interest=None,
            floor_strike=floor_strike,
            strike_type=SUPPORTED_STRIKE_TYPE,
            close_time=window_close,
            pulled_at=sample_time,
        )

        prediction = live_predict(snapshot, feed, settings)
        if prediction is None or prediction.confidence < settings.min_signal_confidence:
            continue

        direction = "yes" if prediction.probability_yes > 0.5 else "no"
        # No historical Kalshi order book is available -- assume the fill
        # price is the model's own fair-value probability (see limitation #3).
        raw_entry = prediction.probability_yes if direction == "yes" else 1 - prediction.probability_yes
        entry_price = min(max(round(raw_entry, 2), 0.01), 0.99)  # Kalshi quotes in whole cents, 1-99
        fee = kalshi_fee(entry_price)
        won = direction == actual_result
        pnl = (CONTRACT_PAYOUT - entry_price - fee) if won else -(entry_price + fee)

        return BacktestTrade(
            window_open=window_1m.index[0].to_pydatetime(),
            window_close=window_close,
            decision_time=sample_time,
            floor_strike=floor_strike,
            btc_price_at_decision=float(row["close"]),
            direction=direction,
            probability_yes=prediction.probability_yes,
            confidence=prediction.confidence,
            entry_price=entry_price,
            fee=fee,
            actual_close_price=actual_close_price,
            actual_result=actual_result,
            won=won,
            pnl=pnl,
        )

    return None  # sat out this window -- no qualifying signal


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    windows_seen: int = 0

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.won) / len(self.trades)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fee for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        if not self.trades:
            return 0.0
        equity_curve = np.cumsum([t.pnl for t in self.trades])
        running_max = np.maximum.accumulate(equity_curve)
        return float(np.max(running_max - equity_curve))

    @property
    def sharpe_ratio(self) -> float:
        if not self.trades:
            return 0.0
        returns = np.array([t.pnl for t in self.trades])
        if returns.std() == 0:
            return 0.0
        return float(returns.mean() / returns.std())

    def summary(self) -> str:
        skipped = self.windows_seen - self.total_trades
        return (
            f"Windows seen:     {self.windows_seen}\n"
            f"Trades taken:     {self.total_trades}\n"
            f"Windows skipped:  {skipped} (no qualifying signal)\n"
            f"Win rate:         {self.win_rate:.1%}\n"
            f"Total P&L:        ${self.total_pnl:.2f} (contract units, model-fair-value fills)\n"
            f"Total fees paid:  ${self.total_fees:.2f}\n"
            f"Max drawdown:     ${self.max_drawdown:.2f}\n"
            f"Sharpe (approx):  {self.sharpe_ratio:.3f}\n"
        )


def calibration_report(trades: list[BacktestTrade], n_bins: int = 10) -> str:
    """Bucketed predicted-probability-of-YES vs. actual YES rate, plus Brier
    score and directional accuracy -- same math/format as
    src/report_calibration.py, so live and backtest calibration read the
    same way. This is the check that catches a model that's confidently
    wrong: a model can have a high win rate on the trades it actually takes
    (it only trades when confident) while still being miscalibrated -- e.g.
    claiming ~70% and landing near 50% within that bucket."""
    rows = [(t.probability_yes, 1 if t.actual_result == "yes" else 0) for t in trades]
    if not rows:
        return "No trades to calibrate."

    lines = [f"{'range':>12}  {'n':>5}  {'mean pred':>10}  {'actual yes%':>12}"]
    width = 1.0 / n_bins
    for i in range(n_bins):
        lo, hi = i * width, (i + 1) * width
        in_bucket = [(p, y) for p, y in rows if (lo <= p < hi) or (hi >= 1.0 and p == 1.0)]
        if not in_bucket:
            continue
        mean_p = sum(p for p, _ in in_bucket) / len(in_bucket)
        yes_rate = sum(y for _, y in in_bucket) / len(in_bucket)
        range_label = f"{lo * 100:4.0f}-{hi * 100:3.0f}%"
        lines.append(f"{range_label}  {len(in_bucket):5d}  {mean_p * 100:9.1f}%  {yes_rate * 100:11.1f}%")

    brier = sum((p - y) ** 2 for p, y in rows) / len(rows)
    directional = sum(1 for p, y in rows if (p >= 0.5) == (y == 1)) / len(rows)

    lines.append("")
    lines.append(f"n = {len(rows)}")
    lines.append(f"Brier score: {brier:.4f}  (0 = perfect, 0.25 = no better than a coinflip)")
    lines.append(f"Directional accuracy (>=50% called YES): {directional * 100:.1f}%")
    return "\n".join(lines)


def run_backtest(df_1m: pd.DataFrame, df_15m: pd.DataFrame, settings: Settings) -> BacktestResult:
    result = BacktestResult(windows_seen=len(df_15m))

    for window_open, bar in df_15m.iterrows():
        window_close = (window_open + pd.Timedelta(minutes=15)).to_pydatetime()
        window_1m = df_1m[(df_1m.index >= window_open) & (df_1m.index < window_open + pd.Timedelta(minutes=15))]

        trade = simulate_window(
            window_1m=window_1m,
            window_close=window_close,
            floor_strike=float(bar["open"]),
            actual_close_price=float(bar["close"]),
            settings=settings,
        )
        if trade is not None:
            result.trades.append(trade)

    return result


# ---------------------------------------------------------------------
# 3. RESULTS -- CSV per run, timestamped for run-over-run comparison
# ---------------------------------------------------------------------

TRADE_CSV_FIELDS = [
    "window_open", "window_close", "decision_time", "floor_strike",
    "btc_price_at_decision", "direction", "probability_yes", "confidence",
    "entry_price", "fee", "actual_close_price", "actual_result", "won", "pnl",
]


def save_results_csv(result: BacktestResult) -> tuple[Path, Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    trades_path = RESULTS_DIR / f"backtest_{stamp}_trades.csv"
    with trades_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_FIELDS)
        writer.writeheader()
        for t in result.trades:
            writer.writerow({
                "window_open": t.window_open.isoformat(),
                "window_close": t.window_close.isoformat(),
                "decision_time": t.decision_time.isoformat(),
                "floor_strike": t.floor_strike,
                "btc_price_at_decision": t.btc_price_at_decision,
                "direction": t.direction,
                "probability_yes": t.probability_yes,
                "confidence": t.confidence,
                "entry_price": t.entry_price,
                "fee": t.fee,
                "actual_close_price": t.actual_close_price,
                "actual_result": t.actual_result,
                "won": t.won,
                "pnl": round(t.pnl, 4),
            })

    summary_path = RESULTS_DIR / f"backtest_{stamp}_summary.csv"
    with summary_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "run_timestamp_utc", "windows_seen", "trades_taken", "win_rate",
            "total_pnl", "total_fees", "max_drawdown", "sharpe_ratio",
        ])
        writer.writerow([
            stamp, result.windows_seen, result.total_trades, round(result.win_rate, 4),
            round(result.total_pnl, 4), round(result.total_fees, 4),
            round(result.max_drawdown, 4), round(result.sharpe_ratio, 4),
        ])

    return trades_path, summary_path


# ---------------------------------------------------------------------
# 4. RUN IT
# ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the Kalshi BTC 15-min bot's live prediction model.")
    parser.add_argument("--days", type=int, default=90, help="Days of BTC history to backtest (default: 90)")
    parser.add_argument("--symbol", default="BTCUSDT", help="Binance symbol (default: BTCUSDT)")
    parser.add_argument("--no-cache", action="store_true", help="Ignore/skip the local Binance data cache")
    parser.add_argument(
        "--min-confidence", type=float, default=None,
        help="Override MIN_SIGNAL_CONFIDENCE for this run only (default: value from .env / settings)",
    )
    args = parser.parse_args()

    settings = load_settings()
    if args.min_confidence is not None:
        settings = Settings(**{**settings.__dict__, "min_signal_confidence": args.min_confidence})

    df_1m = fetch_binance_1m_klines(days=args.days, symbol=args.symbol, use_cache=not args.no_cache)
    print(f"Loaded {len(df_1m)} 1-min bars: {df_1m.index.min()} -> {df_1m.index.max()}")

    df_15m = resample_to_15min(df_1m)
    print(f"Resampled to {len(df_15m)} 15-min windows\n")

    result = run_backtest(df_1m, df_15m, settings)

    print("=" * 55)
    print("BACKTEST RESULTS")
    print("=" * 55)
    print(result.summary())

    print("=" * 55)
    print("CALIBRATION REPORT")
    print("=" * 55)
    print(calibration_report(result.trades))
    print()

    trades_path, summary_path = save_results_csv(result)
    print(f"Trade log:  {trades_path.relative_to(PROJECT_ROOT)}")
    print(f"Run summary: {summary_path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
