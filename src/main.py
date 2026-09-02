from __future__ import annotations

import argparse
import logging

from config.settings import load_settings
from src.kalshi_client import KalshiClient
from src.poller import run_forever, run_once
from src.storage import Storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kalshi 15-min BTC market monitoring bot")
    parser.add_argument("--once", action="store_true", help="Run a single poll iteration and exit")
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
    client = KalshiClient(settings)
    storage = Storage(settings.db_path)

    try:
        if args.once:
            run_once(client, storage, settings)
        else:
            run_forever(client, storage, settings)
    finally:
        storage.close()


if __name__ == "__main__":
    main()
