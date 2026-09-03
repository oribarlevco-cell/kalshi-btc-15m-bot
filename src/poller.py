from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from config.settings import Settings
from src.backup import backup_database
from src.kalshi_client import KalshiClient
from src.markets import MarketSnapshot, discover_active_market, get_settled_history, get_snapshot
from src.publish_analytics import publish_once
from src.storage import Storage
from src.summary import log_summary

logger = logging.getLogger("kalshi_bot")

BACKFILL_INTERVAL_SECONDS = 3600


def run_once(
    client: KalshiClient,
    storage: Storage,
    settings: Settings,
    on_snapshot: Callable[[MarketSnapshot], None] | None = None,
) -> bool:
    """Discover the active market, snapshot it, store it, and log a summary.

    Returns True if a snapshot was taken, False if no market was open
    (e.g. a brief rollover gap between 15-min windows). If `on_snapshot` is
    given, it's called with the fresh snapshot (used by --predict/--trade to
    layer prediction/trading logic on top of the base polling loop).
    """
    market = discover_active_market(client, settings.series_ticker)
    if market is None:
        logger.warning("No open %s market found (rollover gap?)", settings.series_ticker)
        return False

    snapshot = get_snapshot(client, market["ticker"])
    storage.insert_snapshot(snapshot)
    log_summary(snapshot)

    if on_snapshot is not None:
        on_snapshot(snapshot)

    return True


def backfill_settled(client: KalshiClient, storage: Storage, settings: Settings, since: timedelta) -> int:
    min_close_ts = int((datetime.now(timezone.utc) - since).timestamp())
    count = 0
    for payload in get_settled_history(client, settings.series_ticker, min_close_ts=min_close_ts):
        storage.upsert_settled_outcome(payload)
        storage.finalize_market_lifecycle(payload, settings.series_ticker)
        count += 1
    if count:
        logger.info("Backfilled %d settled market(s)", count)
    return count


def run_forever(
    client: KalshiClient,
    storage: Storage,
    settings: Settings,
    on_snapshot: Callable[[MarketSnapshot], None] | None = None,
) -> None:
    last_backfill = 0.0
    last_backup = 0.0
    last_analytics_publish = 0.0
    backup_interval_seconds = settings.backup_interval_hours * 3600
    analytics_publish_interval_seconds = settings.analytics_publish_interval_minutes * 60
    while True:
        try:
            run_once(client, storage, settings, on_snapshot=on_snapshot)
        except Exception:
            logger.exception("Poll iteration failed; continuing")

        now = time.monotonic()
        if now - last_backfill > BACKFILL_INTERVAL_SECONDS:
            try:
                backfill_settled(client, storage, settings, since=timedelta(hours=2))
            except Exception:
                logger.exception("Settled-history backfill failed; continuing")
            last_backfill = now

        if now - last_backup > backup_interval_seconds:
            try:
                backup_database(settings.db_path, settings.backup_dir)
            except Exception:
                logger.exception("Database backup failed; continuing")
            last_backup = now

        if settings.analytics_publish_enabled and now - last_analytics_publish > analytics_publish_interval_seconds:
            try:
                publish_once(settings)
            except Exception:
                logger.exception("Publishing dashboard analytics failed; continuing")
            last_analytics_publish = now

        time.sleep(settings.poll_interval_seconds)
