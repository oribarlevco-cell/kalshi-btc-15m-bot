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
        trading_enabled=False,
        max_order_cost_dollars=5.0,
        trade_window_min_seconds=60,
        trade_window_max_seconds=780,
    )
    defaults.update(overrides)
    return Settings(**defaults)
