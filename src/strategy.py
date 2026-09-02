from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.markets import MarketSnapshot


@dataclass
class Signal:
    ticker: str
    side: str  # "yes" or "no"
    reason: str


def evaluate_signal(snapshot: MarketSnapshot) -> Optional[Signal]:
    """Placeholder hook for a future trading strategy.

    This intentionally always returns None. This phase of the bot is
    data/monitoring only — nothing in this codebase places orders. When a
    strategy is ready, implement its logic here and have the caller (see
    src/poller.py) log the resulting Signal; do not wire order placement in
    without explicit, separate sign-off.
    """
    return None
