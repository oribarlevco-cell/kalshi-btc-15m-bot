from __future__ import annotations

import logging

from src.markets import MarketSnapshot

logger = logging.getLogger("kalshi_bot")


def format_summary(snapshot: MarketSnapshot) -> str:
    minutes, seconds = divmod(int(snapshot.time_remaining_seconds), 60)
    prob = snapshot.implied_probability
    prob_str = f"{prob:.1f}%" if prob is not None else "n/a"
    volume = snapshot.volume if snapshot.volume is not None else "n/a"

    return (
        f"[{snapshot.ticker}] time_left={minutes:02d}:{seconds:02d} "
        f"implied_yes_prob={prob_str} "
        f"yes_bid={snapshot.yes_bid} yes_ask={snapshot.yes_ask} "
        f"volume={volume} open_interest={snapshot.open_interest}"
    )


def log_summary(snapshot: MarketSnapshot) -> None:
    logger.info(format_summary(snapshot))
