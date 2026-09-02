from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

PROD_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"


@dataclass(frozen=True)
class Settings:
    env: str
    base_url: str
    api_key_id: str | None
    private_key_path: str | None
    poll_interval_seconds: int
    db_path: str
    series_ticker: str

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file)

    env = os.getenv("KALSHI_ENV", "demo").strip().lower()
    if env not in ("demo", "prod"):
        raise ValueError(f"KALSHI_ENV must be 'demo' or 'prod', got {env!r}")

    return Settings(
        env=env,
        base_url=PROD_BASE_URL if env == "prod" else DEMO_BASE_URL,
        api_key_id=os.getenv("KALSHI_API_KEY_ID") or None,
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH") or None,
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "20")),
        db_path=os.getenv("DB_PATH", "data/kalshi_btc15m.db"),
        series_ticker=os.getenv("SERIES_TICKER", "KXBTC15M"),
    )
