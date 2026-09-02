from __future__ import annotations

from datetime import datetime, timedelta, timezone

import src.trader as trader_module
from src.markets import MarketSnapshot
from src.predictor import Prediction
from src.trader import Trader
from tests.conftest import make_settings


def _snapshot(ticker="KXBTC15M-TEST", minutes_remaining=5.0, yes_ask=0.60, no_ask=0.45) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        ticker=ticker,
        event_ticker="EVT",
        status="open",
        yes_bid=0.55,
        yes_ask=yes_ask,
        no_bid=0.40,
        no_ask=no_ask,
        last_price=0.55,
        volume=100,
        volume_24h=1000,
        open_interest=50,
        floor_strike=50000.0,
        strike_type="greater_or_equal",
        close_time=now + timedelta(minutes=minutes_remaining),
        pulled_at=now,
    )


def _high_confidence_prediction(ticker="KXBTC15M-TEST", probability_yes=0.9) -> Prediction:
    return Prediction(
        ticker=ticker,
        btc_price=51000.0,
        floor_strike=50000.0,
        probability_yes=probability_yes,
        confidence=abs(probability_yes - 0.5) * 2,
        sample_count=10,
        rationale="test rationale",
    )


class FakePriceFeed:
    def fetch_and_record(self, session=None):
        pass


class FakeStorage:
    def __init__(self):
        self.predictions = []
        self.orders = []

    def insert_prediction(self, prediction):
        self.predictions.append(prediction)

    def insert_order(self, record):
        self.orders.append(record)


class FakeClient:
    def __init__(self, balance_dollars="100.00", positions=None):
        self._balance_dollars = balance_dollars
        self._positions = positions if positions is not None else []
        self.post_calls = []

    def get(self, path, params=None):
        if path == "/portfolio/balance":
            return {"balance_dollars": self._balance_dollars}
        if path == "/portfolio/positions":
            return {"market_positions": self._positions}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, json_body=None):
        self.post_calls.append((path, json_body))
        return {"order_id": "order-1", "client_order_id": json_body["client_order_id"], "fill_count": "1"}


def _trading_settings(**overrides):
    defaults = dict(trading_enabled=True, api_key_id="key", private_key_path="/fake/key.pem", env="demo")
    defaults.update(overrides)
    return make_settings(**defaults)


def test_predict_only_never_prompts(monkeypatch):
    monkeypatch.setattr(trader_module, "predict", lambda *a, **k: _high_confidence_prediction())
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    settings = _trading_settings()  # can_trade True, but enable_trading defaults False
    storage = FakeStorage()
    client = FakeClient()
    t = Trader(client, storage, settings, price_feed=FakePriceFeed(), enable_trading=False)

    t.on_snapshot(_snapshot())

    assert len(storage.predictions) == 1
    assert storage.orders == []


def test_trade_skipped_outside_time_window(monkeypatch):
    monkeypatch.setattr(trader_module, "predict", lambda *a, **k: _high_confidence_prediction())
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    settings = _trading_settings()
    storage = FakeStorage()
    client = FakeClient()
    t = Trader(client, storage, settings, price_feed=FakePriceFeed(), enable_trading=True)

    t.on_snapshot(_snapshot(minutes_remaining=0.2))  # 12s, below default 60s min

    assert storage.orders == []


def test_confirmed_order_is_placed_and_stored(monkeypatch):
    monkeypatch.setattr(trader_module, "predict", lambda *a, **k: _high_confidence_prediction(probability_yes=0.9))
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    settings = _trading_settings()
    storage = FakeStorage()
    client = FakeClient(balance_dollars="100.00", positions=[])
    t = Trader(client, storage, settings, price_feed=FakePriceFeed(), enable_trading=True)

    t.on_snapshot(_snapshot(yes_ask=0.60))

    assert len(client.post_calls) == 1
    _, body = client.post_calls[0]
    assert body["side"] == "bid"
    assert body["price"] == "0.6000"
    assert len(storage.orders) == 1
    assert storage.orders[0]["direction"] == "yes"


def test_declined_order_is_not_placed(monkeypatch):
    monkeypatch.setattr(trader_module, "predict", lambda *a, **k: _high_confidence_prediction(probability_yes=0.9))
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    settings = _trading_settings()
    storage = FakeStorage()
    client = FakeClient(balance_dollars="100.00", positions=[])
    t = Trader(client, storage, settings, price_feed=FakePriceFeed(), enable_trading=True)

    t.on_snapshot(_snapshot())

    assert client.post_calls == []
    assert storage.orders == []


def test_same_ticker_only_prompted_once(monkeypatch):
    monkeypatch.setattr(trader_module, "predict", lambda *a, **k: _high_confidence_prediction(probability_yes=0.9))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda *a: prompts.append(1) or "n")

    settings = _trading_settings()
    storage = FakeStorage()
    client = FakeClient(balance_dollars="100.00", positions=[])
    t = Trader(client, storage, settings, price_feed=FakePriceFeed(), enable_trading=True)

    snapshot = _snapshot()
    t.on_snapshot(snapshot)
    t.on_snapshot(snapshot)

    assert len(prompts) == 1


def test_insufficient_balance_skips_without_prompting(monkeypatch):
    monkeypatch.setattr(trader_module, "predict", lambda *a, **k: _high_confidence_prediction(probability_yes=0.9))
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    settings = _trading_settings()
    storage = FakeStorage()
    client = FakeClient(balance_dollars="0.01", positions=[])
    t = Trader(client, storage, settings, price_feed=FakePriceFeed(), enable_trading=True)

    t.on_snapshot(_snapshot())

    assert storage.orders == []


def test_existing_position_skips_without_prompting(monkeypatch):
    monkeypatch.setattr(trader_module, "predict", lambda *a, **k: _high_confidence_prediction(probability_yes=0.9))
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    settings = _trading_settings()
    storage = FakeStorage()
    client = FakeClient(balance_dollars="100.00", positions=[{"ticker": "KXBTC15M-TEST", "position_fp": "5"}])
    t = Trader(client, storage, settings, price_feed=FakePriceFeed(), enable_trading=True)

    t.on_snapshot(_snapshot())

    assert storage.orders == []


def test_low_confidence_prediction_is_not_traded(monkeypatch):
    monkeypatch.setattr(trader_module, "predict", lambda *a, **k: _high_confidence_prediction(probability_yes=0.52))
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    settings = _trading_settings()
    storage = FakeStorage()
    client = FakeClient()
    t = Trader(client, storage, settings, price_feed=FakePriceFeed(), enable_trading=True)

    t.on_snapshot(_snapshot())

    assert storage.orders == []
