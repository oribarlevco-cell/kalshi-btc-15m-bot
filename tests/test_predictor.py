from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.markets import MarketSnapshot
from src.predictor import predict
from src.price_feed import PriceFeed, PriceSample
from tests.conftest import make_settings


def _snapshot(floor_strike=50000.0, strike_type="greater_or_equal", minutes_remaining=5.0) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        event_ticker="EVT",
        status="open",
        yes_bid=0.40,
        yes_ask=0.44,
        no_bid=0.56,
        no_ask=0.60,
        last_price=0.42,
        volume=100,
        volume_24h=1000,
        open_interest=50,
        floor_strike=floor_strike,
        strike_type=strike_type,
        close_time=now + timedelta(minutes=minutes_remaining),
        pulled_at=now,
    )


def _feed_with_varying_prices(prices: list[float]) -> PriceFeed:
    feed = PriceFeed(url="https://example.invalid/ticker")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, price in enumerate(prices):
        feed._samples.append(PriceSample(ts=base + timedelta(seconds=i * 20), price=price))
    return feed


def test_returns_none_without_floor_strike():
    snapshot = _snapshot(floor_strike=None)
    feed = _feed_with_varying_prices([50000, 50100, 49950, 50200, 50050])
    settings = make_settings()

    assert predict(snapshot, feed, settings) is None


def test_returns_none_for_unsupported_strike_type():
    snapshot = _snapshot(strike_type="less_than")
    feed = _feed_with_varying_prices([50000, 50100, 49950, 50200, 50050])
    settings = make_settings()

    assert predict(snapshot, feed, settings) is None


def test_returns_none_with_too_few_samples():
    snapshot = _snapshot()
    feed = _feed_with_varying_prices([50000, 50100])
    settings = make_settings(min_samples_for_prediction=5)

    assert predict(snapshot, feed, settings) is None


def test_returns_none_for_zero_volatility():
    snapshot = _snapshot()
    feed = _feed_with_varying_prices([50000, 50000, 50000, 50000, 50000])
    settings = make_settings(min_samples_for_prediction=5)

    assert predict(snapshot, feed, settings) is None


def test_deep_in_the_money_predicts_high_probability():
    snapshot = _snapshot(floor_strike=40000.0)
    feed = _feed_with_varying_prices([50000, 50010, 49995, 50005, 50000])
    settings = make_settings(min_samples_for_prediction=5)

    prediction = predict(snapshot, feed, settings)

    assert prediction is not None
    assert prediction.probability_yes > 0.99
    assert prediction.confidence > 0.98


def test_deep_out_of_the_money_predicts_low_probability():
    snapshot = _snapshot(floor_strike=60000.0)
    feed = _feed_with_varying_prices([50000, 50010, 49995, 50005, 50000])
    settings = make_settings(min_samples_for_prediction=5)

    prediction = predict(snapshot, feed, settings)

    assert prediction is not None
    assert prediction.probability_yes < 0.01


def test_at_the_money_predicts_near_coinflip():
    snapshot = _snapshot(floor_strike=50000.0)
    feed = _feed_with_varying_prices([50000, 50010, 49995, 50005, 50000])
    settings = make_settings(min_samples_for_prediction=5)

    prediction = predict(snapshot, feed, settings)

    assert prediction is not None
    assert 0.3 < prediction.probability_yes < 0.7


def test_prediction_includes_sigma_for_later_analysis():
    snapshot = _snapshot(floor_strike=40000.0)
    feed = _feed_with_varying_prices([50000, 50010, 49995, 50005, 50000])
    settings = make_settings(min_samples_for_prediction=5)

    prediction = predict(snapshot, feed, settings)

    assert prediction is not None
    assert prediction.sigma_per_sqrt_second > 0


def test_momentum_is_none_without_enough_window_history():
    # Fixture spans only 80s; default momentum_window_seconds (300) has no
    # sample old enough yet.
    snapshot = _snapshot(floor_strike=40000.0)
    feed = _feed_with_varying_prices([50000, 50010, 49995, 50005, 50000])
    settings = make_settings(min_samples_for_prediction=5, momentum_window_seconds=300)

    prediction = predict(snapshot, feed, settings)

    assert prediction is not None
    assert prediction.momentum_pct is None


class FakePriceFeed:
    """Lets us control sigma/price/momentum independently to prove momentum
    has no effect on the probability formula, without hand-crafting price
    sequences that happen to produce a specific volatility."""

    def __init__(self, price, sigma, momentum_pct, count):
        self._price = price
        self._sigma = sigma
        self._momentum_pct = momentum_pct
        self._count = count

    @property
    def sample_count(self):
        return self._count

    def latest_price(self):
        return self._price

    def volatility_per_second(self):
        return self._sigma

    def momentum(self, window_seconds):
        return self._momentum_pct


def test_momentum_does_not_affect_probability_yes():
    snapshot = _snapshot(floor_strike=48000.0)
    settings = make_settings(min_samples_for_prediction=5)

    feed_momentum_up = FakePriceFeed(price=50000.0, sigma=0.0002, momentum_pct=0.05, count=10)
    feed_momentum_down = FakePriceFeed(price=50000.0, sigma=0.0002, momentum_pct=-0.05, count=10)

    prediction_up = predict(snapshot, feed_momentum_up, settings)
    prediction_down = predict(snapshot, feed_momentum_down, settings)

    assert prediction_up is not None and prediction_down is not None
    assert prediction_up.probability_yes == prediction_down.probability_yes
    assert prediction_up.confidence == prediction_down.confidence
    assert prediction_up.momentum_pct == 0.05
    assert prediction_down.momentum_pct == -0.05
