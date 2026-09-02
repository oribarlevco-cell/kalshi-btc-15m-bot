from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from config.settings import Settings
from src.markets import MarketSnapshot
from src.price_feed import PriceFeed

logger = logging.getLogger("kalshi_bot")

SUPPORTED_STRIKE_TYPE = "greater_or_equal"


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass(frozen=True)
class Prediction:
    ticker: str
    btc_price: float
    floor_strike: float
    probability_yes: float
    confidence: float
    sample_count: int
    rationale: str
    sigma_per_sqrt_second: float = 0.0
    momentum_pct: float | None = None


def predict(snapshot: MarketSnapshot, price_feed: PriceFeed, settings: Settings) -> Prediction | None:
    """Estimate P(yes) for a KXBTC15M market: probability BTC's price at
    close is >= the market's floor_strike, modeled as a driftless random
    walk (the standard "probability finishes above a barrier" formula,
    i.e. a binary option's N(d2)) using realized volatility from our own
    recent BTC price samples as a stand-in for Kalshi's real settlement
    feed (CF Benchmarks' BRTI, which requires a paid license)."""
    if snapshot.floor_strike is None:
        logger.debug("%s has no floor_strike; skipping prediction", snapshot.ticker)
        return None
    if snapshot.strike_type != SUPPORTED_STRIKE_TYPE:
        logger.warning("%s has unsupported strike_type=%r; skipping prediction", snapshot.ticker, snapshot.strike_type)
        return None

    tau = snapshot.time_remaining_seconds
    if tau <= 0:
        return None

    if price_feed.sample_count < settings.min_samples_for_prediction:
        logger.debug(
            "Only %d/%d BTC price samples so far; skipping prediction",
            price_feed.sample_count,
            settings.min_samples_for_prediction,
        )
        return None

    current_price = price_feed.latest_price()
    sigma = price_feed.volatility_per_second()
    if current_price is None or sigma is None or sigma <= 1e-12 or current_price <= 0:
        logger.debug("%s: insufficient/degenerate volatility signal; skipping prediction", snapshot.ticker)
        return None

    d2 = math.log(current_price / snapshot.floor_strike) / (sigma * math.sqrt(tau))
    probability_yes = _norm_cdf(d2)
    confidence = abs(probability_yes - 0.5) * 2

    # Momentum is logged for later analysis only -- it does not feed into d2.
    momentum_pct = price_feed.momentum(settings.momentum_window_seconds)

    rationale = (
        f"BTC ${current_price:,.2f} vs strike ${snapshot.floor_strike:,.2f} "
        f"(delta {current_price - snapshot.floor_strike:+,.2f}), "
        f"sigma/sqrt(s)={sigma:.6f}, time_left={tau:.0f}s -> P(yes)={probability_yes:.1%}"
    )

    return Prediction(
        ticker=snapshot.ticker,
        btc_price=current_price,
        floor_strike=snapshot.floor_strike,
        probability_yes=probability_yes,
        confidence=confidence,
        sample_count=price_feed.sample_count,
        rationale=rationale,
        sigma_per_sqrt_second=sigma,
        momentum_pct=momentum_pct,
    )
