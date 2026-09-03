from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Direction = Literal["yes", "no"]


@dataclass(frozen=True)
class DivergenceCheck:
    is_diverging: bool
    spot_direction: Direction | None
    market_direction: Direction | None


def check_divergence(
    btc_price: float | None,
    floor_strike: float | None,
    yes_bid: float | None,
    confident_threshold: float = 0.65,
) -> DivergenceCheck:
    """Flags disagreement between BTC's raw spot price (vs. the window's
    strike) and the market's own confident lean (yes_bid far from a
    coinflip). Note: Kalshi settles on a 60s BRTI average at close, not raw
    spot -- a divergence isn't proof the market is wrong, just a naturally
    interesting disagreement to track and evaluate over time."""
    if btc_price is None or floor_strike is None or yes_bid is None:
        return DivergenceCheck(False, None, None)

    spot_direction: Direction = "yes" if btc_price > floor_strike else "no"
    market_direction: Direction = "yes" if yes_bid > 0.5 else "no"
    market_confident = yes_bid > confident_threshold or yes_bid < (1 - confident_threshold)

    is_diverging = spot_direction != market_direction and market_confident
    if not is_diverging:
        return DivergenceCheck(False, None, None)
    return DivergenceCheck(True, spot_direction, market_direction)
