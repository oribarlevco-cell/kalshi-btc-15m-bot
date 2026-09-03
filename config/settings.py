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

    live_server_port: int
    live_server_token: str | None
    live_server_allowed_origin: str
    live_server_refresh_seconds: int
    calibration_snapshot_interval_minutes: int

    multi_strategy_trading_enabled: bool
    strategy_tier1_min_n: int
    strategy_tier1_min_ci_lower: float
    strategy_tier2_min_n: int
    strategy_tier2_min_ci_lower: float
    strategy_tier1_multiplier: float
    strategy_tier2_multiplier: float
    multi_strategy_max_concurrent_positions: int

    analytics_publish_enabled: bool
    analytics_publish_interval_minutes: float

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)

    @property
    def can_trade(self) -> bool:
        """Trading (even paper) requires explicit opt-in, credentials, and the
        demo environment -- this build never places prod/real-money orders."""
        return self.trading_enabled and self.has_credentials and self.env == "demo"

    @property
    def can_multi_trade(self) -> bool:
        """Automated multi-strategy paper trading has its own, separate
        opt-in from --trade's TRADING_ENABLED -- both still require demo +
        credentials, and prod is always hard-blocked."""
        return self.multi_strategy_trading_enabled and self.has_credentials and self.env == "demo"


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
        live_server_port=int(os.getenv("LIVE_SERVER_PORT", "8899")),
        live_server_token=os.getenv("LIVE_SERVER_TOKEN") or None,
        live_server_allowed_origin=os.getenv("LIVE_SERVER_ALLOWED_ORIGIN", "https://oribarlevco-cell.github.io"),
        live_server_refresh_seconds=int(os.getenv("LIVE_SERVER_REFRESH_SECONDS", "15")),
        calibration_snapshot_interval_minutes=int(os.getenv("CALIBRATION_SNAPSHOT_INTERVAL_MINUTES", "15")),
        multi_strategy_trading_enabled=os.getenv("MULTI_STRATEGY_TRADING_ENABLED", "false").strip().lower() == "true",
        strategy_tier1_min_n=int(os.getenv("STRATEGY_TIER1_MIN_N", "20")),
        strategy_tier1_min_ci_lower=float(os.getenv("STRATEGY_TIER1_MIN_CI_LOWER", "0.50")),
        strategy_tier2_min_n=int(os.getenv("STRATEGY_TIER2_MIN_N", "50")),
        strategy_tier2_min_ci_lower=float(os.getenv("STRATEGY_TIER2_MIN_CI_LOWER", "0.55")),
        strategy_tier1_multiplier=float(os.getenv("STRATEGY_TIER1_MULTIPLIER", "2")),
        strategy_tier2_multiplier=float(os.getenv("STRATEGY_TIER2_MULTIPLIER", "4")),
        multi_strategy_max_concurrent_positions=int(os.getenv("MULTI_STRATEGY_MAX_CONCURRENT_POSITIONS", "5")),
        analytics_publish_enabled=os.getenv("ANALYTICS_PUBLISH_ENABLED", "false").strip().lower() == "true",
        analytics_publish_interval_minutes=float(os.getenv("ANALYTICS_PUBLISH_INTERVAL_MINUTES", "10")),
    )
