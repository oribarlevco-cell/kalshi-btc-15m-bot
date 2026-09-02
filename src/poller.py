from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from config.settings import Settings
from src.kalshi_client import KalshiClient
from src.markets import discover_active_market, get_settled_history, get_snapshot
from src.storage import Storage
from src.strategy import evaluate_signal
from src.summary import log_summary

logger = logging.getLogger("kalshi_bot")

BACKFILL_INTERVAL_SECONDS = 3600


def run_once(client: KalshiClient, storage: Storage, settings: Settings) -> bool:
    """Discover the active market, snapshot it, store it, and log a summary.

    Returns True if a snapshot was taken, False if no market was open
    (e.g. a brief rollover gap between 15-min windows).
    """
    market = discover_active_market(client, settings.series_ticker)
    if market is None:
        logger.warning("No open %s market found (rollover gap?)", settings.series_ticker)
        return False

    snapshot = get_snapshot(client, market["ticker"])
    storage.insert_snapshot(snapshot)
    log_summary(snapshot)

    signal = evaluate_signal(snapshot)
    if signal is not None:
        logger.info("Strategy signal (not acted on): %s", signal)

    return True


def backfill_settled(client: KalshiClient, storage: Storage, settings: Settings, since: timedelta) -> int:
    min_close_ts = int((datetime.now(timezone.utc) - since).timestamp())
    count = 0
    for payload in get_settled_history(client, settings.series_ticker, min_close_ts=min_close_ts):
        storage.upsert_settled_outcome(payload)
        count += 1
    if count:
        logger.info("Backfilled %d settled market(s)", count)
    return count


def run_forever(client: KalshiClient, storage: Storage, settings: Settings) -> None:
    last_backfill = 0.0
    while True:
        try:
            run_once(client, storage, settings)
        except Exception:
            logger.exception("Poll iteration failed; continuing")

        now = time.monotonic()
        if now - last_backfill > BACKFILL_INTERVAL_SECONDS:
            try:
                backfill_settled(client, storage, settings, since=timedelta(hours=2))
            except Exception:
                logger.exception("Settled-history backfill failed; continuing")
            last_backfill = now

        time.sleep(settings.poll_interval_seconds)
