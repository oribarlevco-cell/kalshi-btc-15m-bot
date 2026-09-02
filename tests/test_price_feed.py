from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.price_feed import PriceFeed, PriceSample


class FakeResponse:
    def __init__(self, price: float):
        self._price = price

    def raise_for_status(self):
        pass

    def json(self):
        return {"price": str(self._price)}


class FakeSession:
    def __init__(self, prices):
        self._prices = list(prices)

    def get(self, url, timeout=None):
        return FakeResponse(self._prices.pop(0))


def test_fetch_and_record_appends_sample():
    feed = PriceFeed(url="https://example.invalid/ticker")
    session = FakeSession([50000.0])

    sample = feed.fetch_and_record(session=session)

    assert sample.price == 50000.0
    assert feed.sample_count == 1
    assert feed.latest_price() == 50000.0


def _feed_with_prices(prices: list[float], seconds_apart: float = 20.0) -> PriceFeed:
    """Build a PriceFeed with deterministic, evenly-spaced timestamps --
    avoids flakiness from relying on wall-clock resolution between calls."""
    feed = PriceFeed(url="https://example.invalid/ticker")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, price in enumerate(prices):
        feed._samples.append(PriceSample(ts=base + timedelta(seconds=i * seconds_apart), price=price))
    return feed


def test_volatility_none_with_fewer_than_two_samples():
    feed = _feed_with_prices([50000.0])

    assert feed.volatility_per_second() is None


def test_volatility_is_zero_for_constant_price():
    feed = _feed_with_prices([50000.0, 50000.0, 50000.0])

    vol = feed.volatility_per_second()
    assert vol == 0.0


def test_volatility_is_positive_for_varying_price():
    feed = _feed_with_prices([50000.0, 50100.0, 49950.0, 50200.0])

    vol = feed.volatility_per_second()
    assert vol is not None
    assert vol > 0


def test_max_samples_bounds_the_window():
    feed = PriceFeed(url="https://example.invalid/ticker", max_samples=3)
    session = FakeSession([1.0, 2.0, 3.0, 4.0])
    for _ in range(4):
        feed.fetch_and_record(session=session)

    assert feed.sample_count == 3
    assert feed.latest_price() == 4.0
