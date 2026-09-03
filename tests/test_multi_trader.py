from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.backtest import StrategyResult
from src.markets import MarketSnapshot
from src.multi_trader import MultiTrader, stake_for_result
from src.storage import Storage
from tests.conftest import make_settings


def _demo_settings(db_path, **overrides):
    defaults = dict(db_path=db_path, env="demo", api_key_id="k", private_key_path="/fake")
    defaults.update(overrides)
    return make_settings(**defaults)


def _result(n=10, ci_low=0.4) -> StrategyResult:
    return StrategyResult(
        name="momentum",
        n=n,
        wins=int(n * 0.6),
        win_rate=0.6,
        ci_low=ci_low,
        ci_high=0.8,
        avg_pnl=0.1,
        low_confidence=n < 20,
    )


def test_stake_floor_when_no_result():
    settings = make_settings(max_order_cost_dollars=5.0)
    assert stake_for_result(None, settings) == 5.0


def test_stake_floor_below_tier1_threshold():
    settings = make_settings(max_order_cost_dollars=5.0, strategy_tier1_min_n=20, strategy_tier1_min_ci_lower=0.5)
    result = _result(n=10, ci_low=0.6)  # n too small
    assert stake_for_result(result, settings) == 5.0


def test_stake_tier1_when_threshold_met():
    settings = make_settings(
        max_order_cost_dollars=5.0,
        strategy_tier1_min_n=20,
        strategy_tier1_min_ci_lower=0.5,
        strategy_tier1_multiplier=2,
        strategy_tier2_min_n=50,
    )
    result = _result(n=25, ci_low=0.55)
    assert stake_for_result(result, settings) == 10.0


def test_stake_tier2_when_threshold_met():
    settings = make_settings(
        max_order_cost_dollars=5.0,
        strategy_tier1_min_n=20,
        strategy_tier1_min_ci_lower=0.5,
        strategy_tier1_multiplier=2,
        strategy_tier2_min_n=50,
        strategy_tier2_min_ci_lower=0.55,
        strategy_tier2_multiplier=4,
    )
    result = _result(n=60, ci_low=0.6)
    assert stake_for_result(result, settings) == 20.0


def test_stake_tier1_not_tier2_when_ci_low_between_thresholds():
    settings = make_settings(
        max_order_cost_dollars=5.0,
        strategy_tier1_min_n=20,
        strategy_tier1_min_ci_lower=0.5,
        strategy_tier1_multiplier=2,
        strategy_tier2_min_n=50,
        strategy_tier2_min_ci_lower=0.55,
        strategy_tier2_multiplier=4,
    )
    result = _result(n=60, ci_low=0.52)  # meets tier2 n, not tier2 ci_low
    assert stake_for_result(result, settings) == 10.0


def _snapshot(ticker="T1", yes_ask=0.6, no_ask=0.45) -> MarketSnapshot:
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
        close_time=now + timedelta(minutes=5),
        pulled_at=now,
    )


class FakeClient:
    def __init__(self, balance_dollars="100.00"):
        self._balance_dollars = balance_dollars
        self.post_calls = []

    def get(self, path, params=None):
        if path == "/portfolio/balance":
            return {"balance_dollars": self._balance_dollars}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, json_body=None):
        self.post_calls.append((path, json_body))
        return {"order_id": "order-1", "client_order_id": json_body["client_order_id"], "fill_count": "1"}


def _seed_open_market(storage, ticker="T1", initial_probability_yes=0.7, momentum_pct=0.02):
    now = datetime.now(timezone.utc)
    storage._conn.execute(
        "INSERT INTO market_lifecycle (ticker, initial_probability_yes) VALUES (?, ?)",
        (ticker, initial_probability_yes),
    )
    storage._conn.execute(
        "INSERT INTO snapshots (ticker, event_ticker, status, yes_bid, yes_ask, no_bid, no_ask, last_price, "
        "volume, volume_24h, open_interest, close_time_utc, pulled_at_utc) "
        "VALUES (?, 'EVT', 'open', 0.55, 0.6, 0.4, 0.45, 0.55, 10, 10, 5, ?, ?)",
        (ticker, now.isoformat(), now.isoformat()),
    )
    storage._conn.execute(
        "INSERT INTO predictions (ticker, computed_at_utc, momentum_pct) VALUES (?, ?, ?)",
        (ticker, now.isoformat(), momentum_pct),
    )
    storage._conn.commit()


def test_disabled_by_default_places_nothing(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    _seed_open_market(storage)
    settings = _demo_settings(str(tmp_path / "test.db"), multi_strategy_trading_enabled=False)
    client = FakeClient()
    trader = MultiTrader(client, storage, settings, enabled=True)  # even with enabled=True at call site

    trader.on_snapshot(_snapshot())

    assert client.post_calls == []
    storage.close()


def test_enabled_places_orders_for_all_three_strategies_without_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should never prompt")))
    storage = Storage(str(tmp_path / "test.db"))
    # model=yes (0.7>0.5), momentum=yes (0.02>0) -> agreement=yes too
    _seed_open_market(storage, initial_probability_yes=0.7, momentum_pct=0.02)
    settings = _demo_settings(str(tmp_path / "test.db"), multi_strategy_trading_enabled=True)
    client = FakeClient()
    trader = MultiTrader(client, storage, settings, enabled=True)

    trader.on_snapshot(_snapshot())

    orders = storage._conn.execute("SELECT strategy FROM orders").fetchall()
    assert {r[0] for r in orders} == {"momentum", "favorite", "agreement"}
    assert len(client.post_calls) == 3
    storage.close()


def test_does_not_double_order_same_strategy_same_ticker(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    _seed_open_market(storage)
    settings = _demo_settings(str(tmp_path / "test.db"), multi_strategy_trading_enabled=True)
    client = FakeClient()
    trader = MultiTrader(client, storage, settings, enabled=True)

    trader.on_snapshot(_snapshot())
    first_call_count = len(client.post_calls)
    trader.on_snapshot(_snapshot())  # second tick, same ticker

    assert len(client.post_calls) == first_call_count  # no new orders
    storage.close()


def test_different_strategies_independent_on_same_ticker(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    _seed_open_market(storage, initial_probability_yes=0.7, momentum_pct=0.02)
    settings = _demo_settings(str(tmp_path / "test.db"), multi_strategy_trading_enabled=True)
    client = FakeClient()
    trader = MultiTrader(client, storage, settings, enabled=True)

    trader.on_snapshot(_snapshot())

    assert storage.has_order_for_strategy("T1", "momentum")
    assert storage.has_order_for_strategy("T1", "favorite")
    assert storage.has_order_for_strategy("T1", "agreement")
    assert not storage.has_order_for_strategy("T1", "model")  # model isn't automated
    storage.close()


def test_prod_env_blocks_multi_trading(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    _seed_open_market(storage)
    settings = _demo_settings(str(tmp_path / "test.db"), multi_strategy_trading_enabled=True, env="prod")
    client = FakeClient()
    trader = MultiTrader(client, storage, settings, enabled=True)

    trader.on_snapshot(_snapshot())

    assert client.post_calls == []
    storage.close()
