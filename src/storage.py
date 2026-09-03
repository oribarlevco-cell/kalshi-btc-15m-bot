from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.markets import MarketSnapshot, OrderbookSummary
from src.predictor import Prediction


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)

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

CREATE TABLE IF NOT EXISTS market_lifecycle (
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
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_result ON market_lifecycle(actual_result);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    pulled_at_utc TEXT NOT NULL,
    yes_levels_json TEXT,
    no_levels_json TEXT,
    yes_depth_total REAL,
    no_depth_total REAL
);
CREATE INDEX IF NOT EXISTS idx_orderbook_ticker ON orderbook_snapshots(ticker);

CREATE TABLE IF NOT EXISTS calibration_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at_utc TEXT NOT NULL,
    which TEXT NOT NULL,
    n INTEGER NOT NULL,
    brier_score REAL,
    directional_accuracy REAL
);
CREATE INDEX IF NOT EXISTS idx_calibration_snapshots_which ON calibration_snapshots(which, computed_at_utc);
"""


class Storage:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS is a no-op on a pre-existing table, so
        columns added to `predictions` after it first shipped need an
        explicit, idempotent ALTER TABLE here. Existing rows get NULL for
        the new columns -- nothing is lost."""
        existing_pred_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(predictions)").fetchall()}
        for column, decl in (("sigma_per_sqrt_second", "REAL"), ("momentum_pct", "REAL")):
            if column not in existing_pred_cols:
                self._conn.execute(f"ALTER TABLE predictions ADD COLUMN {column} {decl}")

        existing_order_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "strategy" not in existing_order_cols:
            self._conn.execute("ALTER TABLE orders ADD COLUMN strategy TEXT")
            # Every order placed before this column existed came from the
            # manual, model-confirmed --trade flow.
            self._conn.execute("UPDATE orders SET strategy = 'model' WHERE strategy IS NULL")

        self._conn.commit()

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
                ticker, computed_at_utc, btc_price, floor_strike, probability_yes, confidence, sample_count,
                sigma_per_sqrt_second, momentum_pct
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction.ticker,
                datetime.now(timezone.utc).isoformat(),
                prediction.btc_price,
                prediction.floor_strike,
                prediction.probability_yes,
                prediction.confidence,
                prediction.sample_count,
                prediction.sigma_per_sqrt_second,
                prediction.momentum_pct,
            ),
        )
        self._conn.commit()

    def insert_order(self, record: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO orders (
                ticker, direction, side, price, count, cost_dollars, client_order_id,
                kalshi_order_id, status, placed_at_utc, rationale, fill_count, average_fill_price, strategy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                record.get("strategy", "model"),
            ),
        )
        self._conn.commit()

    def has_order_for_strategy(self, ticker: str, strategy: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM orders WHERE ticker = ? AND strategy = ? LIMIT 1", (ticker, strategy)
        ).fetchone()
        return row is not None

    def count_open_positions_for_strategy(self, strategy: str) -> int:
        """Number of tickers this strategy holds an order on whose market
        hasn't settled yet (a reasonable proxy for "open" since IOC orders
        never rest, and Kalshi has no concept of our internal strategies)."""
        row = self._conn.execute(
            """
            SELECT COUNT(DISTINCT o.ticker) FROM orders o
            LEFT JOIN market_lifecycle m ON m.ticker = o.ticker
            WHERE o.strategy = ? AND (m.actual_result IS NULL)
            """,
            (strategy,),
        ).fetchone()
        return row[0] if row else 0

    def record_market_open(self, snapshot: MarketSnapshot, prediction: Prediction | None, series_ticker: str) -> None:
        """Log the first time we see a market, idempotently. `prediction` may
        be None if this is called before enough price samples exist yet --
        in that case the initial_* columns are filled in later via
        fill_initial_prediction_if_missing()."""
        self._conn.execute(
            """
            INSERT INTO market_lifecycle (
                ticker, event_ticker, series_ticker, floor_strike, close_time_utc,
                opened_at_utc, btc_price_at_open, initial_probability_yes, initial_confidence, initial_sample_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO NOTHING
            """,
            (
                snapshot.ticker,
                snapshot.event_ticker,
                series_ticker,
                snapshot.floor_strike,
                snapshot.close_time.isoformat(),
                datetime.now(timezone.utc).isoformat(),
                prediction.btc_price if prediction else None,
                prediction.probability_yes if prediction else None,
                prediction.confidence if prediction else None,
                prediction.sample_count if prediction else None,
            ),
        )
        self._conn.commit()

    def fill_initial_prediction_if_missing(self, ticker: str, prediction: Prediction) -> None:
        self._conn.execute(
            """
            UPDATE market_lifecycle
            SET btc_price_at_open = ?, initial_probability_yes = ?, initial_confidence = ?, initial_sample_count = ?
            WHERE ticker = ? AND initial_probability_yes IS NULL
            """,
            (prediction.btc_price, prediction.probability_yes, prediction.confidence, prediction.sample_count, ticker),
        )
        self._conn.commit()

    def finalize_market_lifecycle(self, payload: dict[str, Any], series_ticker: str) -> None:
        """Write the close-side of a market's lifecycle row from Kalshi's
        settled-market payload. Works even if record_market_open() was never
        called for this ticker (e.g. the bot wasn't running --predict when it
        opened) -- it just inserts a close-only row in that case."""
        ticker = payload["ticker"]
        settlement_value = _parse_float(payload.get("settlement_value_dollars"))
        result = payload.get("result") or ("yes" if (settlement_value or 0) > 0 else "no")

        latest = self._conn.execute(
            "SELECT btc_price, probability_yes, confidence FROM predictions "
            "WHERE ticker = ? ORDER BY computed_at_utc DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        final_btc_price, last_probability_yes, last_confidence = latest if latest else (None, None, None)

        self._conn.execute(
            """
            INSERT INTO market_lifecycle (
                ticker, event_ticker, series_ticker, floor_strike, close_time_utc,
                closed_logged_at_utc, actual_result, settlement_value,
                final_yes_bid, final_yes_ask, final_no_bid, final_no_ask, final_last_price,
                final_btc_price, last_probability_yes, last_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                closed_logged_at_utc=excluded.closed_logged_at_utc,
                actual_result=excluded.actual_result,
                settlement_value=excluded.settlement_value,
                final_yes_bid=excluded.final_yes_bid,
                final_yes_ask=excluded.final_yes_ask,
                final_no_bid=excluded.final_no_bid,
                final_no_ask=excluded.final_no_ask,
                final_last_price=excluded.final_last_price,
                final_btc_price=excluded.final_btc_price,
                last_probability_yes=excluded.last_probability_yes,
                last_confidence=excluded.last_confidence
            """,
            (
                ticker,
                payload.get("event_ticker", ""),
                series_ticker,
                payload.get("floor_strike"),
                payload.get("close_time"),
                datetime.now(timezone.utc).isoformat(),
                result,
                settlement_value,
                _parse_float(payload.get("yes_bid_dollars")),
                _parse_float(payload.get("yes_ask_dollars")),
                _parse_float(payload.get("no_bid_dollars")),
                _parse_float(payload.get("no_ask_dollars")),
                _parse_float(payload.get("last_price_dollars")),
                final_btc_price,
                last_probability_yes,
                last_confidence,
            ),
        )
        self._conn.commit()

    def insert_orderbook_snapshot(self, summary: OrderbookSummary) -> None:
        self._conn.execute(
            """
            INSERT INTO orderbook_snapshots (
                ticker, pulled_at_utc, yes_levels_json, no_levels_json, yes_depth_total, no_depth_total
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                summary.ticker,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(summary.yes_levels),
                json.dumps(summary.no_levels),
                summary.yes_depth_total,
                summary.no_depth_total,
            ),
        )
        self._conn.commit()

    def initial_probability_yes(self, ticker: str) -> float | None:
        row = self._conn.execute(
            "SELECT initial_probability_yes FROM market_lifecycle WHERE ticker = ?", (ticker,)
        ).fetchone()
        return row[0] if row else None

    def opening_quote(self, ticker: str) -> tuple[float | None, float | None, float | None, float | None] | None:
        """The earliest logged snapshot's yes_bid/yes_ask/no_bid/no_ask for a
        ticker -- used as the "market open" reference price for backtesting
        and live strategy direction, since these aren't duplicated onto
        market_lifecycle."""
        row = self._conn.execute(
            "SELECT yes_bid, yes_ask, no_bid, no_ask FROM snapshots "
            "WHERE ticker = ? ORDER BY pulled_at_utc ASC LIMIT 1",
            (ticker,),
        ).fetchone()
        return tuple(row) if row else None

    def opening_momentum_pct(self, ticker: str) -> float | None:
        row = self._conn.execute(
            "SELECT momentum_pct FROM predictions WHERE ticker = ? ORDER BY computed_at_utc ASC LIMIT 1", (ticker,)
        ).fetchone()
        return row[0] if row else None

    def insert_calibration_snapshot(
        self, which: str, n: int, brier_score: float | None, directional_accuracy: float | None
    ) -> None:
        self._conn.execute(
            "INSERT INTO calibration_snapshots (computed_at_utc, which, n, brier_score, directional_accuracy) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), which, n, brier_score, directional_accuracy),
        )
        self._conn.commit()

    def recent_calibration_snapshots(self, which: str, limit: int = 50) -> list[tuple]:
        cursor = self._conn.execute(
            "SELECT computed_at_utc, n, brier_score, directional_accuracy FROM calibration_snapshots "
            "WHERE which = ? ORDER BY computed_at_utc DESC LIMIT ?",
            (which, limit),
        )
        return cursor.fetchall()

    def recent_snapshots(self, ticker: str, limit: int = 50) -> list[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.execute(
            "SELECT * FROM snapshots WHERE ticker = ? ORDER BY pulled_at_utc DESC LIMIT ?",
            (ticker, limit),
        )
        return cursor.fetchall()
