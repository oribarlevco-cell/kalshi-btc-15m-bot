from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from config.settings import Settings, load_settings
from src.analytics import build_analytics_payload
from src.divergence import check_divergence
from src.report_calibration import brier_score, directional_accuracy, fetch_rows
from src.storage import Storage

logger = logging.getLogger("kalshi_bot")


def build_live_tile(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Live tile from the most recently-logged snapshot/prediction -- reuses
    whatever the long-running --predict/--trade process already computed,
    no extra Kalshi/Coinbase calls from the server itself. Divergence is
    recomputed fresh each time (cheap, no I/O); trend_state is read from
    market_lifecycle, since Trader already fetched+stored it once per
    ticker -- no point re-fetching from Coinbase here too."""
    conn.row_factory = sqlite3.Row
    snap = conn.execute(
        "SELECT ticker, event_ticker, status, yes_bid, yes_ask, no_bid, no_ask, last_price, volume, "
        "open_interest, close_time_utc, pulled_at_utc FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if snap is None:
        return {"error": "no_data_yet"}

    ticker = snap["ticker"]
    # floor_strike lives on predictions/market_lifecycle, not snapshots.
    pred = conn.execute(
        "SELECT btc_price, floor_strike, probability_yes, confidence, sample_count, computed_at_utc "
        "FROM predictions WHERE ticker = ? ORDER BY computed_at_utc DESC LIMIT 1",
        (ticker,),
    ).fetchone()

    momentum_1m = None
    momentum_15m = None
    if pred:
        # BTC price is a continuous series across every ticker's predictions
        # rows (the same underlying Coinbase feed) -- deliberately NOT
        # scoped to `ticker` here, or momentum would go blank for the first
        # 1/15 minutes after every market rollover.
        cutoff_1m = _iso_minus(pred["computed_at_utc"], 60)
        cutoff_15m = _iso_minus(pred["computed_at_utc"], 900)
        row_1m = conn.execute(
            "SELECT btc_price FROM predictions WHERE computed_at_utc <= ? ORDER BY computed_at_utc DESC LIMIT 1",
            (cutoff_1m,),
        ).fetchone()
        row_15m = conn.execute(
            "SELECT btc_price FROM predictions WHERE computed_at_utc <= ? ORDER BY computed_at_utc DESC LIMIT 1",
            (cutoff_15m,),
        ).fetchone()
        if row_1m and row_1m["btc_price"]:
            momentum_1m = (pred["btc_price"] - row_1m["btc_price"]) / row_1m["btc_price"]
        if row_15m and row_15m["btc_price"]:
            momentum_15m = (pred["btc_price"] - row_15m["btc_price"]) / row_15m["btc_price"]

    divergence = check_divergence(
        pred["btc_price"] if pred else None,
        pred["floor_strike"] if pred else None,
        snap["yes_bid"],
        settings.divergence_confident_threshold,
    )
    divergence_dict = (
        {
            "is_diverging": True,
            "spot_direction": divergence.spot_direction,
            "market_direction": divergence.market_direction,
        }
        if divergence.is_diverging
        else None
    )

    trend_row = conn.execute(
        "SELECT trend_state, trend_rsi14 FROM market_lifecycle WHERE ticker = ?", (ticker,)
    ).fetchone()

    return {
        "ticker": ticker,
        "event_ticker": snap["event_ticker"],
        "status": snap["status"],
        "yes_bid": snap["yes_bid"],
        "yes_ask": snap["yes_ask"],
        "no_bid": snap["no_bid"],
        "no_ask": snap["no_ask"],
        "last_price": snap["last_price"],
        "volume": snap["volume"],
        "open_interest": snap["open_interest"],
        "floor_strike": pred["floor_strike"] if pred else None,
        "close_time": snap["close_time_utc"],
        "snapshot_pulled_at": snap["pulled_at_utc"],
        "btc_price": pred["btc_price"] if pred else None,
        "probability_yes": pred["probability_yes"] if pred else None,
        "confidence": pred["confidence"] if pred else None,
        "sample_count": pred["sample_count"] if pred else None,
        "momentum_1m_pct": momentum_1m,
        "momentum_15m_pct": momentum_15m,
        "divergence": divergence_dict,
        "trend_state": trend_row["trend_state"] if trend_row else None,
        "trend_rsi14": trend_row["trend_rsi14"] if trend_row else None,
    }


def _iso_minus(iso_ts: str, seconds: float) -> str:
    dt = datetime.fromisoformat(iso_ts)
    return (dt - timedelta(seconds=seconds)).isoformat()


def build_payload(settings: Settings) -> dict[str, Any]:
    conn = sqlite3.connect(settings.db_path)
    try:
        live_tile = build_live_tile(conn, settings)
    finally:
        conn.close()

    payload = build_analytics_payload(settings)
    payload["live_tile"] = live_tile
    return payload


class _Cache:
    def __init__(self):
        self._lock = threading.Lock()
        self._payload: dict[str, Any] = {"error": "not_ready_yet"}

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._payload

    def set(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._payload = payload


def _refresh_loop(settings: Settings, cache: _Cache, stop_event: threading.Event) -> None:
    storage = Storage(settings.db_path)
    last_calibration_snapshot = 0.0
    calibration_interval = settings.calibration_snapshot_interval_minutes * 60

    while not stop_event.is_set():
        try:
            payload = build_payload(settings)
            cache.set(payload)
        except Exception:
            logger.exception("Failed to refresh live dashboard payload")

        now = time.monotonic()
        if now - last_calibration_snapshot > calibration_interval:
            try:
                for which in ("initial", "last"):
                    rows = fetch_rows(settings.db_path, which)
                    if rows:
                        storage.insert_calibration_snapshot(
                            which, len(rows), brier_score(rows), directional_accuracy(rows)
                        )
            except Exception:
                logger.exception("Failed to record calibration snapshot")
            last_calibration_snapshot = now

        stop_event.wait(settings.live_server_refresh_seconds)

    storage.close()


def make_handler(settings: Settings, cache: _Cache) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", settings.live_server_allowed_origin)
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.send_header("Access-Control-Allow-Headers", "*")

        def do_OPTIONS(self) -> None:  # noqa: N802 (stdlib naming convention)
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/dashboard":
                self.send_response(404)
                self._cors_headers()
                self.end_headers()
                return

            token = parse_qs(parsed.query).get("token", [None])[0]
            if not settings.live_server_token or token != settings.live_server_token:
                body = json.dumps({"error": "unauthorized"}).encode("utf-8")
                self.send_response(403)
                self._cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            body = json.dumps(cache.get()).encode("utf-8")
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            logger.debug("live_server: " + format, *args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    if not settings.live_server_token:
        print(
            "LIVE_SERVER_TOKEN is not set in .env. Generate one and add it, e.g.:\n"
            '  python3 -c "import secrets; print(secrets.token_urlsafe(32))"',
            file=sys.stderr,
        )
        sys.exit(1)

    cache = _Cache()
    stop_event = threading.Event()
    refresh_thread = threading.Thread(target=_refresh_loop, args=(settings, cache, stop_event), daemon=True)
    refresh_thread.start()

    handler = make_handler(settings, cache)
    server = ThreadingHTTPServer(("127.0.0.1", settings.live_server_port), handler)

    url = f"http://127.0.0.1:{settings.live_server_port}/api/dashboard?token={settings.live_server_token}"
    logger.info("Live dashboard server listening on 127.0.0.1:%d (local only)", settings.live_server_port)
    logger.info("Test it locally: curl '%s'", url)
    logger.info(
        "Point your tunnel (e.g. `ngrok http %d`) at this port, then paste the tunnel URL into the page.",
        settings.live_server_port,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
