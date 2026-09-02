from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.markets import MarketSnapshot
from src.predictor import Prediction

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    event_ticker TEXT,
    status TEXT,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    last_price REAL,
    volume REAL,
    volume_24h REAL,
    open_interest REAL,
    close_time_utc TEXT NOT NULL,
    pulled_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker ON snapshots(ticker);
CREATE INDEX IF NOT EXISTS idx_snapshots_pulled_at ON snapshots(pulled_at_utc);

CREATE TABLE IF NOT EXISTS settled_outcomes (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    close_time_utc TEXT NOT NULL,
    result TEXT,
    settlement_value REAL,
    pulled_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    computed_at_utc TEXT NOT NULL,
    btc_price REAL,
    floor_strike REAL,
    probability_yes REAL,
    confidence REAL,
    sample_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);

CREATE TABLE IF NOT EXISTS orders (
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
);
CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
"""


class Storage:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert_snapshot(self, snapshot: MarketSnapshot) -> None:
        self._conn.execute(
            """
            INSERT INTO snapshots (
                ticker, event_ticker, status, yes_bid, yes_ask, no_bid, no_ask,
                last_price, volume, volume_24h, open_interest, close_time_utc, pulled_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.ticker,
                snapshot.event_ticker,
                snapshot.status,
                snapshot.yes_bid,
                snapshot.yes_ask,
                snapshot.no_bid,
                snapshot.no_ask,
                snapshot.last_price,
                snapshot.volume,
                snapshot.volume_24h,
                snapshot.open_interest,
                snapshot.close_time.isoformat(),
                snapshot.pulled_at.isoformat(),
            ),
        )
        self._conn.commit()

    def upsert_settled_outcome(self, payload: dict[str, Any]) -> None:
        raw_settlement_value = payload.get("settlement_value_dollars")
        settlement_value = float(raw_settlement_value) if raw_settlement_value not in (None, "") else None
        result = payload.get("result") or ("yes" if (settlement_value or 0) > 0 else "no")
        self._conn.execute(
            """
            INSERT INTO settled_outcomes (ticker, event_ticker, close_time_utc, result, settlement_value, pulled_at_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                result=excluded.result,
                settlement_value=excluded.settlement_value,
                pulled_at_utc=excluded.pulled_at_utc
            """,
            (
                payload["ticker"],
                payload.get("event_ticker", ""),
                payload["close_time"],
                result,
                settlement_value,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def insert_prediction(self, prediction: Prediction) -> None:
        self._conn.execute(
            """
            INSERT INTO predictions (
                ticker, computed_at_utc, btc_price, floor_strike, probability_yes, confidence, sample_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction.ticker,
                datetime.now(timezone.utc).isoformat(),
                prediction.btc_price,
                prediction.floor_strike,
                prediction.probability_yes,
                prediction.confidence,
                prediction.sample_count,
            ),
        )
        self._conn.commit()

    def insert_order(self, record: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO orders (
                ticker, direction, side, price, count, cost_dollars, client_order_id,
                kalshi_order_id, status, placed_at_utc, rationale, fill_count, average_fill_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["ticker"],
                record["direction"],
                record["side"],
                record["price"],
                record["count"],
                record["cost_dollars"],
                record.get("client_order_id"),
                record.get("kalshi_order_id"),
                record.get("status"),
                record.get("placed_at_utc") or datetime.now(timezone.utc).isoformat(),
                record.get("rationale"),
                record.get("fill_count"),
                record.get("average_fill_price"),
            ),
        )
        self._conn.commit()

    def recent_snapshots(self, ticker: str, limit: int = 50) -> list[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.execute(
            "SELECT * FROM snapshots WHERE ticker = ? ORDER BY pulled_at_utc DESC LIMIT ?",
            (ticker, limit),
        )
        return cursor.fetchall()
