from __future__ import annotations

import sqlite3

from src.analytics import build_analytics_payload, build_calibration, build_recent_settled
from src.storage import Storage
from tests.conftest import make_settings


def test_build_recent_settled(tmp_path):
    db_path = str(tmp_path / "test.db")
    storage = Storage(db_path)
    storage._conn.execute(
        "INSERT INTO market_lifecycle (ticker, actual_result, closed_logged_at_utc) VALUES (?, ?, ?)",
        ("T1", "yes", "2026-01-01T00:15:00+00:00"),
    )
    storage._conn.execute(
        "INSERT INTO market_lifecycle (ticker, actual_result, closed_logged_at_utc) VALUES (?, ?, ?)",
        ("T2", "no", "2026-01-01T00:30:00+00:00"),
    )
    storage._conn.commit()
    storage.close()

    conn = sqlite3.connect(db_path)
    settled = build_recent_settled(conn)
    conn.close()

    assert settled[0]["ticker"] == "T2"  # most recent first
    assert settled[0]["result"] == "no"
    assert len(settled) == 2


def test_build_calibration_empty_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    Storage(db_path).close()

    calibration = build_calibration(db_path)

    assert calibration == {
        "initial": {"n": 0, "buckets": [], "brier_score": None, "directional_accuracy": None},
        "last": {"n": 0, "buckets": [], "brier_score": None, "directional_accuracy": None},
    }


def test_build_calibration_with_data(tmp_path):
    db_path = str(tmp_path / "test.db")
    storage = Storage(db_path)
    storage._conn.execute(
        "INSERT INTO market_lifecycle (ticker, actual_result, initial_probability_yes, last_probability_yes) "
        "VALUES (?, ?, ?, ?)",
        ("T1", "yes", 0.7, 0.9),
    )
    storage._conn.commit()
    storage.close()

    calibration = build_calibration(db_path)

    assert calibration["initial"]["n"] == 1
    assert calibration["last"]["n"] == 1
    assert len(calibration["initial"]["buckets"]) == 1


def test_build_analytics_payload_shape_and_no_live_tile(tmp_path):
    db_path = str(tmp_path / "test.db")
    Storage(db_path).close()
    settings = make_settings(db_path=db_path)

    payload = build_analytics_payload(settings)

    assert "live_tile" not in payload
    assert "generated_at" in payload
    assert payload["recent_settled"] == []
    assert {b["name"] for b in payload["backtests"]} == {"model", "favorite", "momentum", "agreement"}
    assert payload["pattern_log"] == {"total_settled": 0, "up_count": 0, "down_count": 0}
    assert payload["calibration"]["last"]["n"] == 0
    assert payload["calibration_trend"] == []


def test_build_analytics_payload_includes_calibration_trend(tmp_path):
    db_path = str(tmp_path / "test.db")
    storage = Storage(db_path)
    storage.insert_calibration_snapshot("last", n=10, brier_score=0.2, directional_accuracy=0.6)
    storage.insert_calibration_snapshot("last", n=15, brier_score=0.15, directional_accuracy=0.65)
    storage.close()
    settings = make_settings(db_path=db_path)

    payload = build_analytics_payload(settings)

    assert len(payload["calibration_trend"]) == 2
    assert payload["calibration_trend"][0]["n"] == 10  # oldest first
    assert payload["calibration_trend"][1]["n"] == 15
