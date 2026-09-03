from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import requests

DEFAULT_EMA_RSI_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=900"

TrendLabel = Literal["bull", "bear", "neutral"]


@dataclass(frozen=True)
class TrendState:
    state: TrendLabel | None
    ema9: float | None
    ema21: float | None
    rsi14: float | None


def ema(values: list[float], period: int) -> list[float | None]:
    """Standard exponential moving average. out[i] is None until index
    period-1, matching the reference implementation this was ported from."""
    if len(values) < period:
        return [None] * len(values)

    k = 2 / (period + 1)
    out: list[float | None] = [None] * len(values)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    # A flat series (no gains AND no losses) is neutral, not "maximally
    # overbought" -- the naive "avg_loss == 0 -> rs = 100" shortcut some RSI
    # ports use conflates "no losses" with "no losses AND no gains", which
    # skews a perfectly flat/quiet price series toward a near-100 reading.
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi(values: list[float], period: int) -> list[float | None]:
    """Wilder's smoothed RSI. out[i] is None until index `period`."""
    if len(values) <= period:
        return [None] * len(values)

    out: list[float | None] = [None] * len(values)
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff

    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)

    return out


def eval_trend(closes: list[float]) -> TrendState:
    """EMA9-vs-EMA21 crossover, confirmed by RSI14, using the same rule as
    the reference implementation this was ported from."""
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    r14 = rsi(closes, 14)

    i = len(closes) - 1
    if i < 0 or e9[i] is None or e21[i] is None or r14[i] is None:
        return TrendState(None, None, None, None)

    if e9[i] > e21[i] and r14[i] < 65:
        state: TrendLabel = "bull"
    elif e9[i] < e21[i] and r14[i] > 35:
        state = "bear"
    else:
        state = "neutral"

    return TrendState(state=state, ema9=e9[i], ema21=e21[i], rsi14=r14[i])


def fetch_trend_state(url: str = DEFAULT_EMA_RSI_CANDLES_URL, session: requests.Session | None = None) -> TrendState:
    """Coinbase candles are returned as [time, low, high, open, close,
    volume], most-recent-first. We only need `close`, oldest-to-newest."""
    session = session or requests
    response = session.get(url, timeout=10)
    response.raise_for_status()
    raw = response.json()
    closes = [candle[4] for candle in reversed(raw) if candle[4] and candle[4] > 0]
    return eval_trend(closes)
