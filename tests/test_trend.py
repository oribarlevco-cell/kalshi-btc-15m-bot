from __future__ import annotations

import math

import pytest

from src.trend import DEFAULT_EMA_RSI_CANDLES_URL, ema, eval_trend, fetch_trend_state, rsi


def _noisy_trend(n: int, drift_per_step: float) -> list[float]:
    """A realistic-ish trending series -- net drift with enough oscillation
    that RSI lands in a moderate range instead of pegging near 0/100 like a
    perfectly monotonic series would (real price data always has pullbacks)."""
    return [100.0 + i * drift_per_step + 3.0 * math.sin(i) for i in range(n)]


def test_ema_none_until_period_reached():
    values = [1.0, 2.0, 3.0]
    result = ema(values, period=5)
    assert result == [None, None, None]


def test_ema_first_value_is_simple_average():
    values = [1.0, 2.0, 3.0, 4.0]
    result = ema(values, period=3)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == pytest.approx((1 + 2 + 3) / 3)
    assert result[3] is not None


def test_rsi_strictly_rising_approaches_100():
    values = [100.0 + i for i in range(30)]  # monotonically increasing
    result = rsi(values, period=14)
    assert result[-1] == pytest.approx(100.0)


def test_rsi_strictly_falling_approaches_0():
    values = [100.0 - i for i in range(30)]  # monotonically decreasing
    result = rsi(values, period=14)
    assert result[-1] == pytest.approx(0.0)


def test_rsi_flat_series_is_50():
    values = [100.0] * 30
    result = rsi(values, period=14)
    assert result[-1] == pytest.approx(50.0)


def test_eval_trend_bull_when_ema9_above_ema21_and_rsi_moderate():
    values = _noisy_trend(40, drift_per_step=0.15)
    result = eval_trend(values)
    assert result.state == "bull"
    assert result.ema9 > result.ema21
    assert result.rsi14 < 65


def test_eval_trend_bear_when_ema9_below_ema21_and_rsi_moderate():
    values = _noisy_trend(40, drift_per_step=-0.3)
    result = eval_trend(values)
    assert result.state == "bear"
    assert result.ema9 < result.ema21
    assert result.rsi14 > 35


def test_eval_trend_none_with_insufficient_data():
    result = eval_trend([100.0, 101.0])
    assert result.state is None
    assert result.ema9 is None
    assert result.ema21 is None
    assert result.rsi14 is None


class FakeResponse:
    def __init__(self, candles):
        self._candles = candles

    def raise_for_status(self):
        pass

    def json(self):
        return self._candles


class FakeSession:
    def __init__(self, candles):
        self._candles = candles

    def get(self, url, timeout=None):
        return FakeResponse(self._candles)


def test_fetch_trend_state_parses_coinbase_shape_most_recent_first():
    # Coinbase candles: [time, low, high, open, close, volume], most-recent-first.
    # closes[i] here is oldest->newest; candle list below reverses that order
    # (index 0 = most recent), matching Coinbase's real ordering.
    closes = _noisy_trend(40, drift_per_step=0.15)
    candles = [[1000 + i * 900, 0, 0, 0, closes[len(closes) - 1 - i], 0] for i in range(len(closes))]
    session = FakeSession(candles)

    result = fetch_trend_state(DEFAULT_EMA_RSI_CANDLES_URL, session=session)

    assert result.state == "bull"  # closes rise oldest->newest once correctly reversed
