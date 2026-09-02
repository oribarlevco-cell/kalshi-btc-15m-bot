from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

PROD_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"

DEFAULT_PRICE_FEED_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"


@dataclass(frozen=True)
class Settings:
    env: str
    base_url: str
    api_key_id: str | None
    private_key_path: str | None
    poll_interval_seconds: int
    db_path: str
    series_ticker: str

    price_feed_url: str
    min_samples_for_prediction: int
    min_signal_confidence: float
    momentum_window_seconds: int

    trading_enabled: bool
    max_order_cost_dollars: float
    trade_window_min_seconds: int
    trade_window_max_seconds: int

    backup_dir: str
    backup_interval_hours: float

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)

    @property
    def can_trade(self) -> bool:
        """Trading (even paper) requires explicit opt-in, credentials, and the
        demo environment -- this build never places prod/real-money orders."""
        return self.trading_enabled and self.has_credentials and self.env == "demo"


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
        price_feed_url=os.getenv("PRICE_FEED_URL", DEFAULT_PRICE_FEED_URL),
        min_samples_for_prediction=int(os.getenv("MIN_SAMPLES_FOR_PREDICTION", "5")),
        min_signal_confidence=float(os.getenv("MIN_SIGNAL_CONFIDENCE", "0.15")),
        momentum_window_seconds=int(os.getenv("MOMENTUM_WINDOW_SECONDS", "300")),
        trading_enabled=os.getenv("TRADING_ENABLED", "false").strip().lower() == "true",
        max_order_cost_dollars=float(os.getenv("MAX_ORDER_COST_DOLLARS", "5.0")),
        trade_window_min_seconds=int(os.getenv("TRADE_WINDOW_MIN_SECONDS", "60")),
        trade_window_max_seconds=int(os.getenv("TRADE_WINDOW_MAX_SECONDS", "780")),
        backup_dir=os.getenv("BACKUP_DIR", "data/backups"),
        backup_interval_hours=float(os.getenv("BACKUP_INTERVAL_HOURS", "24")),
    )
