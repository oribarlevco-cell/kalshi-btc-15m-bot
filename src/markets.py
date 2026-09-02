from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from src.kalshi_client import KalshiClient


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _as_float(value: Any) -> float | None:
    """Kalshi's *_dollars/*_fp fields come back as decimal strings (e.g. "0.4400")."""
    if value is None or value == "":
        return None
    return float(value)


@dataclass
class MarketSnapshot:
    ticker: str
    event_ticker: str
    status: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume: float | None
    volume_24h: float | None
    open_interest: float | None
    floor_strike: float | None
    strike_type: str | None
    close_time: datetime
    pulled_at: datetime

    @property
    def time_remaining_seconds(self) -> float:
        return max(0.0, (self.close_time - self.pulled_at).total_seconds())

    @property
    def implied_probability(self) -> float | None:
        """Implied probability of YES, derived from the yes bid/ask midpoint (cents -> %)."""
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2 * 100

    @classmethod
    def from_market_payload(cls, payload: dict[str, Any]) -> "MarketSnapshot":
        return cls(
            ticker=payload["ticker"],
            event_ticker=payload.get("event_ticker", ""),
            status=payload.get("status", "unknown"),
            yes_bid=_as_float(payload.get("yes_bid_dollars")),
            yes_ask=_as_float(payload.get("yes_ask_dollars")),
            no_bid=_as_float(payload.get("no_bid_dollars")),
            no_ask=_as_float(payload.get("no_ask_dollars")),
            last_price=_as_float(payload.get("last_price_dollars")),
            volume=_as_float(payload.get("volume_fp")),
            volume_24h=_as_float(payload.get("volume_24h_fp")),
            open_interest=_as_float(payload.get("open_interest_fp")),
            floor_strike=payload.get("floor_strike"),
            strike_type=payload.get("strike_type"),
            close_time=_parse_ts(payload["close_time"]),
            pulled_at=_utcnow(),
        )


def discover_active_market(client: KalshiClient, series_ticker: str) -> dict[str, Any] | None:
    """Return the raw market payload for the currently-open 15-min BTC market,
    or None if none is open right now (e.g. a brief rollover gap)."""
    markets = list(
        client.get_paginated("/markets", params={"series_ticker": series_ticker, "status": "open"}, item_key="markets")
    )
    if not markets:
        return None

    now = _utcnow()
    open_now = [m for m in markets if _parse_ts(m["close_time"]) > now]
    if not open_now:
        return None

    # If more than one is open (rare boundary overlap), take the one closing soonest.
    return min(open_now, key=lambda m: _parse_ts(m["close_time"]))


def get_snapshot(client: KalshiClient, ticker: str) -> MarketSnapshot:
    payload = client.get(f"/markets/{ticker}")
    return MarketSnapshot.from_market_payload(payload["market"])


def get_orderbook(client: KalshiClient, ticker: str) -> dict[str, Any]:
    return client.get(f"/markets/{ticker}/orderbook")


@dataclass
class OrderbookSummary:
    ticker: str
    yes_levels: list[tuple[float, float]]
    no_levels: list[tuple[float, float]]
    yes_depth_total: float
    no_depth_total: float


def get_orderbook_summary(client: KalshiClient, ticker: str) -> OrderbookSummary:
    """Resting bid levels beyond the top-of-book yes_bid/no_bid already on
    MarketSnapshot -- Kalshi's orderbook response shape is
    {"orderbook_fp": {"yes_dollars": [[price, size], ...], "no_dollars": [...]}}."""
    payload = get_orderbook(client, ticker)
    book = payload.get("orderbook_fp") or {}
    yes_levels = [(float(price), float(size)) for price, size in (book.get("yes_dollars") or [])]
    no_levels = [(float(price), float(size)) for price, size in (book.get("no_dollars") or [])]
    return OrderbookSummary(
        ticker=ticker,
        yes_levels=yes_levels,
        no_levels=no_levels,
        yes_depth_total=sum(size for _, size in yes_levels),
        no_depth_total=sum(size for _, size in no_levels),
    )


def get_settled_history(
    client: KalshiClient,
    series_ticker: str,
    min_close_ts: int | None = None,
    max_close_ts: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield raw market payloads for settled 15-min BTC markets, for backfill."""
    params: dict[str, Any] = {"series_ticker": series_ticker, "status": "settled"}
    if min_close_ts is not None:
        params["min_close_ts"] = min_close_ts
    if max_close_ts is not None:
        params["max_close_ts"] = max_close_ts
    yield from client.get_paginated("/markets", params=params, item_key="markets")
