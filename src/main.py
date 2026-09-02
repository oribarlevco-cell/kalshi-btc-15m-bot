from __future__ import annotations

import argparse
import logging
import sys

from config.settings import load_settings
from src.kalshi_client import KalshiClient
from src.poller import run_forever, run_once
from src.price_feed import PriceFeed
from src.storage import Storage
from src.trader import Trader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kalshi 15-min BTC market monitoring bot")
    parser.add_argument("--once", action="store_true", help="Run a single poll iteration and exit")
    parser.add_argument(
        "--predict", action="store_true", help="Also compute and log real-time BTC-vs-strike predictions"
    )
    parser.add_argument(
        "--trade",
        action="store_true",
        help="Also propose paper trades on Kalshi demo, confirmed interactively (implies --predict)",
    )
    parser.add_argument("--env-file", default=None, help="Path to a .env file (default: .env in cwd)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings(args.env_file)

    if args.trade and not settings.can_trade:
        reasons = []
        if not settings.trading_enabled:
            reasons.append("TRADING_ENABLED is not 'true' in your .env")
        if not settings.has_credentials:
            reasons.append("KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH are not set")
        if settings.env != "demo":
            reasons.append("KALSHI_ENV must be 'demo' -- this build only places paper orders, never prod/real-money")
        print("--trade requires all of the following:\n  - " + "\n  - ".join(reasons), file=sys.stderr)
        sys.exit(1)

    client = KalshiClient(settings)
    storage = Storage(settings.db_path)

    on_snapshot = None
    if args.predict or args.trade:
        price_feed = PriceFeed(url=settings.price_feed_url)
        trader = Trader(client, storage, settings, price_feed=price_feed, enable_trading=args.trade)
        on_snapshot = trader.on_snapshot

    try:
        if args.once:
            run_once(client, storage, settings, on_snapshot=on_snapshot)
        else:
            run_forever(client, storage, settings, on_snapshot=on_snapshot)
    finally:
        storage.close()


if __name__ == "__main__":
    main()
