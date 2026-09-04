from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings  # noqa: E402


def make_settings(**overrides) -> Settings:
    defaults = dict(
        env="demo",
        base_url="https://external-api.demo.kalshi.co/trade-api/v2",
        api_key_id=None,
        private_key_path=None,
        poll_interval_seconds=1,
        db_path=":memory:",
        series_ticker="KXBTC15M",
        price_feed_url="https://example.invalid/ticker",
        min_samples_for_prediction=5,
        min_signal_confidence=0.15,
        momentum_window_seconds=300,
        trading_enabled=False,
        max_order_cost_dollars=5.0,
        trade_window_min_seconds=60,
        trade_window_max_seconds=780,
        backup_dir=":memory-backups:",
        backup_interval_hours=24,
        live_server_port=8899,
        live_server_token="test-token",
        live_server_allowed_origin="https://example.invalid",
        live_server_refresh_seconds=15,
        calibration_snapshot_interval_minutes=15,
        multi_strategy_trading_enabled=False,
        strategy_tier1_min_n=20,
        strategy_tier1_min_ci_lower=0.50,
        strategy_tier2_min_n=50,
        strategy_tier2_min_ci_lower=0.55,
        strategy_tier1_multiplier=2,
        strategy_tier2_multiplier=4,
        multi_strategy_max_concurrent_positions=5,
        analytics_publish_enabled=False,
        analytics_publish_interval_minutes=10,
        divergence_confident_threshold=0.65,
        divergence_min_volume=0.0,
        ema_rsi_candles_url="https://example.invalid/candles",
    )
    defaults.update(overrides)
    return Settings(**defaults)
