from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.markets import MarketSnapshot, discover_active_market


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class FakeClient:
    def __init__(self, pages):
        self._pages = pages

    def get_paginated(self, path, params=None, item_key="markets"):
        for page in self._pages:
            for item in page:
                yield item


def _market(ticker: str, minutes_to_close: float) -> dict:
    close_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_close)
    return {"ticker": ticker, "event_ticker": f"{ticker}-EVT", "close_time": _iso(close_time)}


def test_discover_active_market_picks_soonest_closing():
    markets = [_market("A", 10), _market("B", 3), _market("C", 7)]
    client = FakeClient([markets])

    active = discover_active_market(client, "KXBTC15M")

    assert active["ticker"] == "B"


def test_discover_active_market_filters_already_closed():
    markets = [_market("EXPIRED", -1), _market("LIVE", 5)]
    client = FakeClient([markets])

    active = discover_active_market(client, "KXBTC15M")

    assert active["ticker"] == "LIVE"


def test_discover_active_market_returns_none_when_no_open_markets():
    client = FakeClient([[]])

    assert discover_active_market(client, "KXBTC15M") is None


def test_discover_active_market_returns_none_when_all_expired():
    client = FakeClient([[_market("EXPIRED", -2)]])

    assert discover_active_market(client, "KXBTC15M") is None


def test_market_snapshot_time_remaining_and_probability():
    close_time = datetime.now(timezone.utc) + timedelta(minutes=5)
    payload = {
        "ticker": "KXBTC15M-TEST",
        "event_ticker": "EVT",
        "status": "open",
        "yes_bid_dollars": "0.4000",
        "yes_ask_dollars": "0.4400",
        "no_bid_dollars": "0.5600",
        "no_ask_dollars": "0.6000",
        "last_price_dollars": "0.4200",
        "volume_fp": "1200.00",
        "volume_24h_fp": "5000.00",
        "open_interest_fp": "800.00",
        "floor_strike": 77301.95,
        "strike_type": "greater_or_equal",
        "close_time": _iso(close_time),
    }

    snapshot = MarketSnapshot.from_market_payload(payload)

    assert snapshot.ticker == "KXBTC15M-TEST"
    assert 0 < snapshot.time_remaining_seconds <= 300
    assert snapshot.implied_probability == pytest.approx(42.0)
    assert snapshot.floor_strike == pytest.approx(77301.95)
    assert snapshot.strike_type == "greater_or_equal"
