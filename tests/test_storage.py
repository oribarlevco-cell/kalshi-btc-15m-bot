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


def test_insert_and_read_calibration_snapshots(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))

    storage.insert_calibration_snapshot("last", n=10, brier_score=0.15, directional_accuracy=0.7)
    storage.insert_calibration_snapshot("last", n=15, brier_score=0.12, directional_accuracy=0.73)
    storage.insert_calibration_snapshot("initial", n=8, brier_score=0.20, directional_accuracy=0.6)

    last_rows = storage.recent_calibration_snapshots("last")
    assert len(last_rows) == 2
    assert last_rows[0][1] == 15  # most recent first

    initial_rows = storage.recent_calibration_snapshots("initial")
    assert len(initial_rows) == 1
    storage.close()


def test_orders_migration_backfills_existing_rows_as_model(tmp_path):
    db_path = str(tmp_path / "old_orders.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            count INTEGER NOT NULL,
            cost_dollars REAL NOT NULL,
            client_order_id TEXT,
            kalshi_order_id TEXT,
            status TEXT,
            placed_at_utc TEXT NOT NULL,
            rationale TEXT,
            fill_count REAL,
            average_fill_price REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO orders (ticker, direction, side, price, count, cost_dollars, placed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("OLD-TICKER", "yes", "bid", 0.5, 1, 0.5, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    storage = Storage(db_path)

    row = storage._conn.execute("SELECT strategy FROM orders WHERE ticker = ?", ("OLD-TICKER",)).fetchone()
    assert row[0] == "model"
    storage.close()


def test_has_order_for_strategy(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    storage.insert_order(
        {
            "ticker": "T1",
            "direction": "yes",
            "side": "bid",
            "price": 0.5,
            "count": 1,
            "cost_dollars": 0.5,
            "strategy": "momentum",
        }
    )

    assert storage.has_order_for_strategy("T1", "momentum") is True
    assert storage.has_order_for_strategy("T1", "favorite") is False
    assert storage.has_order_for_strategy("T2", "momentum") is False
    storage.close()


def test_count_open_positions_for_strategy(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    for ticker in ("T1", "T2", "T3"):
        storage.insert_order(
            {
                "ticker": ticker,
                "direction": "yes",
                "side": "bid",
                "price": 0.5,
                "count": 1,
                "cost_dollars": 0.5,
                "strategy": "momentum",
            }
        )
    # settle T1 -- should no longer count as "open"
    storage._conn.execute(
        "INSERT INTO market_lifecycle (ticker, actual_result) VALUES (?, ?)", ("T1", "yes")
    )
    storage._conn.commit()

    assert storage.count_open_positions_for_strategy("momentum") == 2
    assert storage.count_open_positions_for_strategy("favorite") == 0
    storage.close()


def test_market_lifecycle_migration_adds_trend_columns_without_data_loss(tmp_path):
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    # Pre-Phase-6 shape (everything through Phase 5, missing only trend_*).
    conn.execute(
        """
        CREATE TABLE market_lifecycle (
            ticker TEXT PRIMARY KEY,
            event_ticker TEXT,
            series_ticker TEXT,
            floor_strike REAL,
            close_time_utc TEXT,
            opened_at_utc TEXT,
            btc_price_at_open REAL,
            initial_probability_yes REAL,
            initial_confidence REAL,
            initial_sample_count INTEGER,
            closed_logged_at_utc TEXT,
            actual_result TEXT,
            settlement_value REAL,
            final_yes_bid REAL,
            final_yes_ask REAL,
            final_no_bid REAL,
            final_no_ask REAL,
            final_last_price REAL,
            final_btc_price REAL,
            last_probability_yes REAL,
            last_confidence REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO market_lifecycle (ticker, floor_strike, initial_probability_yes) VALUES (?, ?, ?)",
        ("OLD-TICKER", 50000.0, 0.6),
    )
    conn.commit()
    conn.close()

    storage = Storage(db_path)

    cols = {row[1] for row in storage._conn.execute("PRAGMA table_info(market_lifecycle)").fetchall()}
    assert {"trend_state", "trend_ema9", "trend_ema21", "trend_rsi14"} <= cols

    row = storage._conn.execute(
        "SELECT floor_strike, initial_probability_yes, trend_state FROM market_lifecycle WHERE ticker = ?",
        ("OLD-TICKER",),
    ).fetchone()
    assert row == (50000.0, 0.6, None)
    storage.close()


def test_record_divergence_event_only_keeps_first_occurrence(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))

    storage.record_divergence_event("T1", 51000.0, 50000.0, 0.20, "yes", "no")
    storage.record_divergence_event("T1", 52000.0, 50000.0, 0.10, "yes", "no")  # should be ignored

    row = storage._conn.execute(
        "SELECT btc_price, spot_direction FROM divergence_events WHERE ticker = ?", ("T1",)
    ).fetchone()
    assert row == (51000.0, "yes")
    count = storage._conn.execute("SELECT COUNT(*) FROM divergence_events WHERE ticker = ?", ("T1",)).fetchone()[0]
    assert count == 1
    storage.close()


def test_finalize_divergence_event_sets_result_once(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    storage.record_divergence_event("T1", 51000.0, 50000.0, 0.20, "yes", "no")

    storage.finalize_divergence_event("T1", "yes")
    storage.finalize_divergence_event("T1", "no")  # already finalized -- should not overwrite

    row = storage._conn.execute("SELECT actual_result FROM divergence_events WHERE ticker = ?", ("T1",)).fetchone()
    assert row[0] == "yes"
    storage.close()


def test_finalize_divergence_event_noop_when_no_event(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    storage.finalize_divergence_event("NEVER-DIVERGED", "yes")  # should not raise or insert anything
    count = storage._conn.execute("SELECT COUNT(*) FROM divergence_events").fetchone()[0]
    assert count == 0
    storage.close()


def test_record_trend_state_fills_once(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    storage._conn.execute("INSERT INTO market_lifecycle (ticker) VALUES (?)", ("T1",))
    storage._conn.commit()

    storage.record_trend_state("T1", "bull", 100.5, 99.2, 58.0)
    storage.record_trend_state("T1", "bear", 1.0, 2.0, 3.0)  # should not overwrite

    row = storage._conn.execute(
        "SELECT trend_state, trend_ema9, trend_ema21, trend_rsi14 FROM market_lifecycle WHERE ticker = ?", ("T1",)
    ).fetchone()
    assert row == ("bull", 100.5, 99.2, 58.0)
    storage.close()


def test_trend_state_for(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    storage._conn.execute("INSERT INTO market_lifecycle (ticker) VALUES (?)", ("T1",))
    storage._conn.commit()
    storage.record_trend_state("T1", "bear", 1.0, 2.0, 40.0)

    assert storage.trend_state_for("T1") == ("bear", 40.0)
    assert storage.trend_state_for("UNKNOWN") is None
    storage.close()
