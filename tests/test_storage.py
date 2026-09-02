from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.markets import MarketSnapshot
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
