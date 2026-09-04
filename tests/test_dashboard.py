from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import src.dashboard as dashboard_module
from src.markets import MarketSnapshot
from src.predictor import Prediction
from src.trend import TrendState
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _no_real_trend_fetch(monkeypatch):
    monkeypatch.setattr(dashboard_module, "fetch_trend_state", lambda url: TrendState(None, None, None, None))


def _snapshot() -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        event_ticker="EVT",
        status="active",
        yes_bid=0.55,
        yes_ask=0.60,
        no_bid=0.38,
        no_ask=0.45,
        last_price=0.55,
        volume=493,
        volume_24h=1200,
        open_interest=454,
        floor_strike=76988.45,
        strike_type="greater_or_equal",
        close_time=now + timedelta(minutes=2),
        pulled_at=now,
    )


def _prediction() -> Prediction:
    return Prediction(
        ticker="KXBTC15M-TEST",
        btc_price=77342.78,
        floor_strike=76988.45,
        probability_yes=0.64,
        confidence=0.28,
        sample_count=5,
        rationale="test rationale",
    )


class FakePriceFeed:
    def __init__(self, url=None):
        self._count = 0

    def fetch_and_record(self):
        self._count += 1

    @property
    def sample_count(self):
        return self._count

    def latest_price(self):
        return 77342.78


def test_no_active_market_returns_error(monkeypatch):
    monkeypatch.setattr(dashboard_module, "discover_active_market", lambda client, ticker: None)
    settings = make_settings()

    data = dashboard_module.build_dashboard_data(settings)

    assert data["error"] == "no_active_market"
    assert data["series_ticker"] == settings.series_ticker
    assert "ticker" not in data


def test_full_payload_with_prediction(monkeypatch):
    snapshot = _snapshot()
    monkeypatch.setattr(dashboard_module, "discover_active_market", lambda client, ticker: {"ticker": snapshot.ticker})
    monkeypatch.setattr(dashboard_module, "get_snapshot", lambda client, ticker: snapshot)
    monkeypatch.setattr(dashboard_module, "predict", lambda snap, feed, settings: _prediction())
    monkeypatch.setattr(dashboard_module, "PriceFeed", FakePriceFeed)
    monkeypatch.setattr(dashboard_module.time, "sleep", lambda s: None)
    settings = make_settings(min_samples_for_prediction=5)

    data = dashboard_module.build_dashboard_data(settings)

    assert "error" not in data
    assert data["ticker"] == "KXBTC15M-TEST"
    assert data["floor_strike"] == 76988.45
    assert data["yes_ask"] == 0.60
    assert data["no_ask"] == 0.45
    assert data["volume"] == 493
    assert data["open_interest"] == 454
    assert data["btc_price"] == 77342.78
    assert data["probability_yes"] == 0.64
    assert data["sample_count"] == 5
    assert data["divergence"] is None  # yes_bid=0.55 isn't confident either way
    assert data["trend_state"] is None  # no real network call in tests


def test_divergence_populated_when_conditions_confidently_disagree(monkeypatch):
    snapshot = _snapshot()
    snapshot.yes_bid = 0.20  # confident "no", but spot (77342.78) is above strike (76988.45) -> "yes"
    monkeypatch.setattr(dashboard_module, "discover_active_market", lambda client, ticker: {"ticker": snapshot.ticker})
    monkeypatch.setattr(dashboard_module, "get_snapshot", lambda client, ticker: snapshot)
    monkeypatch.setattr(dashboard_module, "predict", lambda snap, feed, settings: _prediction())
    monkeypatch.setattr(dashboard_module, "PriceFeed", FakePriceFeed)
    monkeypatch.setattr(dashboard_module.time, "sleep", lambda s: None)
    settings = make_settings(min_samples_for_prediction=5)

    data = dashboard_module.build_dashboard_data(settings)

    assert data["divergence"] == {"is_diverging": True, "spot_direction": "yes", "market_direction": "no"}


def test_divergence_not_populated_when_market_illiquid(monkeypatch):
    snapshot = _snapshot()
    snapshot.yes_bid = 0.20
    snapshot.volume = 0
    monkeypatch.setattr(dashboard_module, "discover_active_market", lambda client, ticker: {"ticker": snapshot.ticker})
    monkeypatch.setattr(dashboard_module, "get_snapshot", lambda client, ticker: snapshot)
    monkeypatch.setattr(dashboard_module, "predict", lambda snap, feed, settings: _prediction())
    monkeypatch.setattr(dashboard_module, "PriceFeed", FakePriceFeed)
    monkeypatch.setattr(dashboard_module.time, "sleep", lambda s: None)
    settings = make_settings(min_samples_for_prediction=5, divergence_min_volume=10.0)

    data = dashboard_module.build_dashboard_data(settings)

    assert data["divergence"] is None


def test_trend_fetch_failure_leaves_trend_fields_none(monkeypatch):
    snapshot = _snapshot()
    monkeypatch.setattr(dashboard_module, "discover_active_market", lambda client, ticker: {"ticker": snapshot.ticker})
    monkeypatch.setattr(dashboard_module, "get_snapshot", lambda client, ticker: snapshot)
    monkeypatch.setattr(dashboard_module, "predict", lambda snap, feed, settings: _prediction())
    monkeypatch.setattr(dashboard_module, "PriceFeed", FakePriceFeed)
    monkeypatch.setattr(dashboard_module.time, "sleep", lambda s: None)

    def failing_fetch(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(dashboard_module, "fetch_trend_state", failing_fetch)
    settings = make_settings(min_samples_for_prediction=5)

    data = dashboard_module.build_dashboard_data(settings)  # should not raise

    assert data["trend_state"] is None
    assert data["trend_rsi14"] is None


def test_missing_prediction_falls_back_to_latest_price(monkeypatch):
    snapshot = _snapshot()
    monkeypatch.setattr(dashboard_module, "discover_active_market", lambda client, ticker: {"ticker": snapshot.ticker})
    monkeypatch.setattr(dashboard_module, "get_snapshot", lambda client, ticker: snapshot)
    monkeypatch.setattr(dashboard_module, "predict", lambda snap, feed, settings: None)
    monkeypatch.setattr(dashboard_module, "PriceFeed", FakePriceFeed)
    monkeypatch.setattr(dashboard_module.time, "sleep", lambda s: None)
    settings = make_settings(min_samples_for_prediction=5)

    data = dashboard_module.build_dashboard_data(settings)

    assert data["probability_yes"] is None
    assert data["btc_price"] == 77342.78
    assert data["rationale"] is None
