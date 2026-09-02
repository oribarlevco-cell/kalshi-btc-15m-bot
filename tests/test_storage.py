from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from src.markets import MarketSnapshot, OrderbookSummary
from src.predictor import Prediction
from src.storage import Storage


def _snapshot(ticker: str = "KXBTC15M-TEST") -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        ticker=ticker,
        event_ticker=f"{ticker}-EVT",
        status="open",
        yes_bid=0.40,
        yes_ask=0.44,
        no_bid=0.56,
        no_ask=0.60,
        last_price=0.42,
        volume=1200,
        volume_24h=5000,
        open_interest=800,
        floor_strike=77301.95,
        strike_type="greater_or_equal",
        close_time=now + timedelta(minutes=5),
        pulled_at=now,
    )


def test_insert_and_read_snapshot(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    snapshot = _snapshot()

    storage.insert_snapshot(snapshot)
    rows = storage.recent_snapshots(snapshot.ticker)

    assert len(rows) == 1
    assert rows[0]["ticker"] == snapshot.ticker
    assert rows[0]["yes_bid"] == 0.40
    storage.close()


def test_recent_snapshots_orders_newest_first(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    older = _snapshot()
    newer = _snapshot()
    newer.pulled_at = older.pulled_at + timedelta(seconds=30)

    storage.insert_snapshot(older)
    storage.insert_snapshot(newer)
    rows = storage.recent_snapshots(older.ticker)

    assert rows[0]["pulled_at_utc"] == newer.pulled_at.isoformat()
    storage.close()


def test_upsert_settled_outcome_dedupes_on_ticker(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    payload = {
        "ticker": "KXBTC15M-SETTLED",
        "event_ticker": "EVT",
        "close_time": datetime.now(timezone.utc).isoformat(),
        "result": "yes",
        "settlement_value_dollars": 1.0,
    }

    storage.upsert_settled_outcome(payload)
    storage.upsert_settled_outcome(payload)

    cursor = storage._conn.execute("SELECT COUNT(*) FROM settled_outcomes WHERE ticker = ?", (payload["ticker"],))
    assert cursor.fetchone()[0] == 1
    storage.close()


def _prediction(ticker: str = "KXBTC15M-TEST", probability_yes: float = 0.7) -> Prediction:
    return Prediction(
        ticker=ticker,
        btc_price=51000.0,
        floor_strike=50000.0,
        probability_yes=probability_yes,
        confidence=abs(probability_yes - 0.5) * 2,
        sample_count=5,
        rationale="test rationale",
    )


def test_migration_adds_new_prediction_columns_without_data_loss(tmp_path):
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            computed_at_utc TEXT NOT NULL,
            btc_price REAL,
            floor_strike REAL,
            probability_yes REAL,
            confidence REAL,
            sample_count INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO predictions "
        "(ticker, computed_at_utc, btc_price, floor_strike, probability_yes, confidence, sample_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("OLD-TICKER", "2026-01-01T00:00:00+00:00", 50000.0, 49000.0, 0.7, 0.4, 5),
    )
    conn.commit()
    conn.close()

    storage = Storage(db_path)

    cols = {row[1] for row in storage._conn.execute("PRAGMA table_info(predictions)").fetchall()}
    assert "sigma_per_sqrt_second" in cols
    assert "momentum_pct" in cols

    row = storage._conn.execute(
        "SELECT ticker, btc_price, sigma_per_sqrt_second, momentum_pct FROM predictions WHERE ticker = ?",
        ("OLD-TICKER",),
    ).fetchone()
    assert row == ("OLD-TICKER", 50000.0, None, None)
    storage.close()


def test_record_market_open_with_prediction_populates_open_side(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    snapshot = _snapshot()
    prediction = _prediction()

    storage.record_market_open(snapshot, prediction, "KXBTC15M")

    row = storage._conn.execute(
        "SELECT event_ticker, series_ticker, floor_strike, btc_price_at_open, initial_probability_yes, "
        "initial_confidence, initial_sample_count FROM market_lifecycle WHERE ticker = ?",
        (snapshot.ticker,),
    ).fetchone()
    assert row == (snapshot.event_ticker, "KXBTC15M", snapshot.floor_strike, 51000.0, 0.7, prediction.confidence, 5)
    storage.close()


def test_record_market_open_without_prediction_leaves_open_side_null(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    snapshot = _snapshot()

    storage.record_market_open(snapshot, None, "KXBTC15M")

    row = storage._conn.execute(
        "SELECT initial_probability_yes FROM market_lifecycle WHERE ticker = ?", (snapshot.ticker,)
    ).fetchone()
    assert row[0] is None
    storage.close()


def test_record_market_open_is_idempotent(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    snapshot = _snapshot()

    storage.record_market_open(snapshot, _prediction(probability_yes=0.7), "KXBTC15M")
    storage.record_market_open(snapshot, _prediction(probability_yes=0.9), "KXBTC15M")

    row = storage._conn.execute(
        "SELECT initial_probability_yes FROM market_lifecycle WHERE ticker = ?", (snapshot.ticker,)
    ).fetchone()
    assert row[0] == 0.7
    storage.close()


def test_fill_initial_prediction_if_missing_updates_null_row(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    snapshot = _snapshot()
    storage.record_market_open(snapshot, None, "KXBTC15M")

    storage.fill_initial_prediction_if_missing(snapshot.ticker, _prediction(probability_yes=0.6))

    row = storage._conn.execute(
        "SELECT initial_probability_yes FROM market_lifecycle WHERE ticker = ?", (snapshot.ticker,)
    ).fetchone()
    assert row[0] == 0.6
    storage.close()


def test_fill_initial_prediction_if_missing_does_not_overwrite_existing(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    snapshot = _snapshot()
    storage.record_market_open(snapshot, _prediction(probability_yes=0.7), "KXBTC15M")

    storage.fill_initial_prediction_if_missing(snapshot.ticker, _prediction(probability_yes=0.99))

    row = storage._conn.execute(
        "SELECT initial_probability_yes FROM market_lifecycle WHERE ticker = ?", (snapshot.ticker,)
    ).fetchone()
    assert row[0] == 0.7
    storage.close()


SETTLED_PAYLOAD = {
    "ticker": "KXBTC15M-SETTLED",
    "event_ticker": "KXBTC15M-EVT",
    "close_time": "2026-01-01T00:15:00+00:00",
    "floor_strike": 50000.0,
    "result": "yes",
    "settlement_value_dollars": "1.0000",
    "yes_bid_dollars": "0.0000",
    "yes_ask_dollars": "1.0000",
    "no_bid_dollars": "0.0000",
    "no_ask_dollars": "1.0000",
    "last_price_dollars": "1.0000",
}


def test_finalize_market_lifecycle_updates_existing_open_row(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    snapshot = _snapshot(ticker="KXBTC15M-SETTLED")
    storage.record_market_open(snapshot, _prediction(ticker="KXBTC15M-SETTLED", probability_yes=0.65), "KXBTC15M")
    storage.insert_prediction(_prediction(ticker="KXBTC15M-SETTLED", probability_yes=0.8))

    storage.finalize_market_lifecycle(SETTLED_PAYLOAD, "KXBTC15M")

    row = storage._conn.execute(
        "SELECT actual_result, settlement_value, final_yes_ask, last_probability_yes, initial_probability_yes "
        "FROM market_lifecycle WHERE ticker = ?",
        ("KXBTC15M-SETTLED",),
    ).fetchone()
    assert row == ("yes", 1.0, 1.0, 0.8, 0.65)
    storage.close()


def test_finalize_market_lifecycle_inserts_close_only_row_if_never_opened(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))

    storage.finalize_market_lifecycle(SETTLED_PAYLOAD, "KXBTC15M")

    row = storage._conn.execute(
        "SELECT actual_result, opened_at_utc, floor_strike FROM market_lifecycle WHERE ticker = ?",
        ("KXBTC15M-SETTLED",),
    ).fetchone()
    assert row == ("yes", None, 50000.0)
    storage.close()


def test_insert_orderbook_snapshot(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    summary = OrderbookSummary(
        ticker="KXBTC15M-TEST",
        yes_levels=[(0.44, 33.73)],
        no_levels=[],
        yes_depth_total=33.73,
        no_depth_total=0.0,
    )

    storage.insert_orderbook_snapshot(summary)

    row = storage._conn.execute(
        "SELECT ticker, yes_levels_json, no_levels_json, yes_depth_total, no_depth_total "
        "FROM orderbook_snapshots WHERE ticker = ?",
        ("KXBTC15M-TEST",),
    ).fetchone()
    assert row[0] == "KXBTC15M-TEST"
    assert json.loads(row[1]) == [[0.44, 33.73]]
    assert json.loads(row[2]) == []
    assert row[3] == 33.73
    assert row[4] == 0.0
    storage.close()
