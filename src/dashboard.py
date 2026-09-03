from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import Settings, load_settings
from src.divergence import check_divergence
from src.kalshi_client import KalshiClient
from src.markets import discover_active_market, get_snapshot
from src.predictor import predict
from src.price_feed import PriceFeed
from src.trend import fetch_trend_state

logger = logging.getLogger("kalshi_bot")

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "data.json"

# This script runs fresh in a GitHub Actions job every couple of minutes, so
# unlike the long-running --predict loop it has no rolling price history to
# draw on. It bootstraps a small one in-process by sampling the price feed a
# few times a couple seconds apart before predicting.
SAMPLE_SPACING_SECONDS = 3


def build_dashboard_data(settings: Settings) -> dict[str, Any]:
    client = KalshiClient(settings)
    generated_at = datetime.now(timezone.utc).isoformat()

    market = discover_active_market(client, settings.series_ticker)
    if market is None:
        return {
            "generated_at": generated_at,
            "series_ticker": settings.series_ticker,
            "error": "no_active_market",
        }

    snapshot = get_snapshot(client, market["ticker"])

    price_feed = PriceFeed(url=settings.price_feed_url)
    for i in range(settings.min_samples_for_prediction):
        try:
            price_feed.fetch_and_record()
        except Exception:
            logger.exception("Price feed fetch failed")
        if i < settings.min_samples_for_prediction - 1:
            time.sleep(SAMPLE_SPACING_SECONDS)

    prediction = predict(snapshot, price_feed, settings)
    btc_price = prediction.btc_price if prediction else price_feed.latest_price()

    divergence = check_divergence(
        btc_price, snapshot.floor_strike, snapshot.yes_bid, settings.divergence_confident_threshold
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

    try:
        trend = fetch_trend_state(settings.ema_rsi_candles_url)
    except Exception:
        logger.exception("Failed to fetch EMA/RSI trend state")
        trend = None

    return {
        "generated_at": generated_at,
        "series_ticker": settings.series_ticker,
        "ticker": snapshot.ticker,
        "event_ticker": snapshot.event_ticker,
        "status": snapshot.status,
        "close_time": snapshot.close_time.isoformat(),
        "time_remaining_seconds": snapshot.time_remaining_seconds,
        "floor_strike": snapshot.floor_strike,
        "yes_bid": snapshot.yes_bid,
        "yes_ask": snapshot.yes_ask,
        "no_bid": snapshot.no_bid,
        "no_ask": snapshot.no_ask,
        "last_price": snapshot.last_price,
        "volume": snapshot.volume,
        "open_interest": snapshot.open_interest,
        "btc_price": btc_price,
        "probability_yes": prediction.probability_yes if prediction else None,
        "confidence": prediction.confidence if prediction else None,
        "sample_count": price_feed.sample_count,
        "rationale": prediction.rationale if prediction else None,
        "divergence": divergence_dict,
        "trend_state": trend.state if trend else None,
        "trend_rsi14": trend.rsi14 if trend else None,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    data = build_dashboard_data(settings)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(data, indent=2))
    logger.info("Wrote %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
