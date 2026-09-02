from __future__ import annotations

import uuid
from typing import Any, Literal

from src.kalshi_client import KalshiClient

Direction = Literal["yes", "no"]


def get_balance(client: KalshiClient) -> dict[str, Any]:
    return client.get("/portfolio/balance")


def get_positions(client: KalshiClient, ticker: str | None = None) -> list[dict[str, Any]]:
    params = {"ticker": ticker} if ticker else None
    payload = client.get("/portfolio/positions", params=params)
    return payload.get("market_positions", [])


def place_order(
    client: KalshiClient,
    ticker: str,
    direction: Direction,
    limit_price: float,
    count: int,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Place an immediate-or-cancel limit order.

    Kalshi's order API is quoted entirely from the YES side: side="bid" buys
    YES at `price`; side="ask" sells YES at `price`, which is economically
    equivalent to buying NO at (1 - price). This function lets callers think
    in terms of "buy yes" / "buy no" and handles that translation.
    """
    if direction == "yes":
        side = "bid"
        price = round(limit_price, 4)
    else:
        side = "ask"
        price = round(1 - limit_price, 4)

    body = {
        "ticker": ticker,
        "side": side,
        "count": str(count),
        "price": f"{price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": client_order_id or str(uuid.uuid4()),
    }
    return client.post("/portfolio/events/orders", json_body=body)


def cancel_order(client: KalshiClient, order_id: str) -> dict[str, Any]:
    return client.delete(f"/portfolio/events/orders/{order_id}")
