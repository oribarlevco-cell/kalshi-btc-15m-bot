from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from src.live_server import _Cache, build_live_tile, build_payload, make_handler
from src.storage import Storage
from tests.conftest import make_settings


@pytest.fixture
def running_server():
    settings = make_settings(live_server_token="secret-token", live_server_allowed_origin="https://example.invalid")
    cache = _Cache()
    cache.set({"hello": "world"})

    handler = make_handler(settings, cache)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}", "secret-token"

    server.shutdown()
    thread.join(timeout=5)


def test_server_only_binds_localhost(running_server):
    base_url, _ = running_server
    assert base_url.startswith("http://127.0.0.1:")


def test_missing_token_is_unauthorized(running_server):
    base_url, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base_url}/api/dashboard")
    assert exc_info.value.code == 403


def test_wrong_token_is_unauthorized(running_server):
    base_url, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base_url}/api/dashboard?token=wrong")
    assert exc_info.value.code == 403


def test_correct_token_returns_cached_payload(running_server):
    base_url, token = running_server
    with urllib.request.urlopen(f"{base_url}/api/dashboard?token={token}") as response:
        assert response.status == 200
        assert response.headers["Access-Control-Allow-Origin"] == "https://example.invalid"
        body = json.loads(response.read())
    assert body == {"hello": "world"}


def test_unknown_path_is_404(running_server):
    base_url, token = running_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base_url}/nope?token={token}")
    assert exc_info.value.code == 404


def test_options_preflight(running_server):
    base_url, _ = running_server
    request = urllib.request.Request(f"{base_url}/api/dashboard", method="OPTIONS")
    with urllib.request.urlopen(request) as response:
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == "https://example.invalid"
        assert response.headers["Access-Control-Allow-Methods"] == "GET"


def _seed_market_data(db_path: str) -> None:
    storage = Storage(db_path)
    now = datetime.now(timezone.utc)
    storage._conn.execute(
        "INSERT INTO snapshots (ticker, event_ticker, status, yes_bid, yes_ask, no_bid, no_ask, last_price, "
        "volume, volume_24h, open_interest, close_time_utc, pulled_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("T1", "EVT", "active", 0.55, 0.60, 0.38, 0.45, 0.55, 100, 500, 90, now.isoformat(), now.isoformat()),
    )
    storage._conn.execute(
        "INSERT INTO predictions (ticker, computed_at_utc, btc_price, floor_strike, probability_yes, confidence, "
        "sample_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("T1", now.isoformat(), 50100.0, 50000.0, 0.6, 0.2, 5),
    )
    older = now - timedelta(seconds=120)
    storage._conn.execute(
        "INSERT INTO predictions (ticker, computed_at_utc, btc_price, floor_strike, probability_yes, confidence, "
        "sample_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("T1", older.isoformat(), 49900.0, 50000.0, 0.5, 0.0, 5),
    )
    storage._conn.commit()
    storage.close()


def test_build_live_tile_reads_latest_snapshot_and_prediction(tmp_path):
    db_path = str(tmp_path / "test.db")
    _seed_market_data(db_path)

    conn = sqlite3.connect(db_path)
    tile = build_live_tile(conn)
    conn.close()

    assert tile["ticker"] == "T1"
    assert tile["btc_price"] == 50100.0
    assert tile["floor_strike"] == 50000.0
    assert tile["probability_yes"] == 0.6
    assert tile["momentum_1m_pct"] is not None  # a prediction ~2 min ago exists


def test_build_live_tile_no_data_yet(tmp_path):
    db_path = str(tmp_path / "test.db")
    Storage(db_path).close()

    conn = sqlite3.connect(db_path)
    tile = build_live_tile(conn)
    conn.close()

    assert tile == {"error": "no_data_yet"}


def test_build_payload_shape(tmp_path):
    db_path = str(tmp_path / "test.db")
    _seed_market_data(db_path)
    settings = make_settings(db_path=db_path)

    payload = build_payload(settings)

    assert "live_tile" in payload
    assert "recent_settled" in payload
    assert "backtests" in payload
    assert {b["name"] for b in payload["backtests"]} == {"model", "favorite", "momentum", "agreement"}
    assert "pattern_log" in payload
    assert "calibration" in payload
    assert "calibration_trend" in payload
