from __future__ import annotations

import logging
import math

from config.settings import Settings
from src.kalshi_client import KalshiClient
from src.markets import MarketSnapshot, get_orderbook_summary
from src.orders import get_balance, get_positions, place_order
from src.predictor import Prediction, predict
from src.price_feed import PriceFeed
from src.storage import Storage

logger = logging.getLogger("kalshi_bot")


class Trader:
    """Computes real-time predictions for every snapshot, logs each market's
    open/close lifecycle and orderbook depth for later evaluation, and —
    only when settings.can_trade is true — proposes at most one
    paper-trading order per 15-min market and blocks on the user's explicit
    y/N confirmation before ever calling src.orders.place_order. See
    README.md for the full set of trading safety gates (env flags,
    demo-only, per-order confirmation) -- none of that is affected by the
    logging added here."""

    def __init__(
        self,
        client: KalshiClient,
        storage: Storage,
        settings: Settings,
        price_feed: PriceFeed | None = None,
        enable_trading: bool = False,
    ):
        self._client = client
        self._storage = storage
        self._settings = settings
        self._price_feed = price_feed or PriceFeed(url=settings.price_feed_url)
        self._handled_tickers: set[str] = set()
        # Trading additionally requires the CLI caller to have explicitly
        # passed --trade -- settings.can_trade (env/creds/demo-only) alone is
        # not enough, so a `--predict`-only run never places orders even if
        # TRADING_ENABLED=true happens to be set in .env.
        self._enable_trading = enable_trading and settings.can_trade

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        try:
            self._price_feed.fetch_and_record()
        except Exception:
            logger.exception("Failed to fetch BTC price; skipping this tick's prediction")
            return

        prediction = predict(snapshot, self._price_feed, self._settings)

        self._storage.record_market_open(snapshot, prediction, self._settings.series_ticker)

        try:
            orderbook = get_orderbook_summary(self._client, snapshot.ticker)
            self._storage.insert_orderbook_snapshot(orderbook)
        except Exception:
            logger.exception("Failed to fetch/store orderbook for %s", snapshot.ticker)

        if prediction is None:
            return

        self._storage.fill_initial_prediction_if_missing(snapshot.ticker, prediction)
        self._storage.insert_prediction(prediction)
        logger.info("Prediction: %s", prediction.rationale)

        if not self._should_propose_trade(snapshot, prediction):
            return

        self._handled_tickers.add(snapshot.ticker)
        self._propose_and_confirm(snapshot, prediction)

    def _should_propose_trade(self, snapshot: MarketSnapshot, prediction: Prediction) -> bool:
        if not self._enable_trading:
            return False
        if snapshot.ticker in self._handled_tickers:
            return False
        if prediction.confidence < self._settings.min_signal_confidence:
            return False
        remaining = snapshot.time_remaining_seconds
        return self._settings.trade_window_min_seconds <= remaining <= self._settings.trade_window_max_seconds

    def _propose_and_confirm(self, snapshot: MarketSnapshot, prediction: Prediction) -> None:
        direction = "yes" if prediction.probability_yes > 0.5 else "no"
        price = snapshot.yes_ask if direction == "yes" else snapshot.no_ask
        if not price or price <= 0:
            logger.warning("%s: no valid %s ask price; skipping trade proposal", snapshot.ticker, direction)
            return

        count = max(1, math.floor(self._settings.max_order_cost_dollars / price))
        cost = round(price * count, 2)

        try:
            positions = get_positions(self._client, ticker=snapshot.ticker)
        except Exception:
            logger.exception("Failed to fetch positions for %s; skipping trade proposal", snapshot.ticker)
            return
        if any(abs(float(p.get("position_fp") or 0)) > 0 for p in positions):
            logger.info("%s: already have an open position; skipping trade proposal", snapshot.ticker)
            return

        try:
            balance = get_balance(self._client)
        except Exception:
            logger.exception("Failed to fetch balance; skipping trade proposal for %s", snapshot.ticker)
            return
        balance_dollars = float(balance.get("balance_dollars") or 0)
        if balance_dollars < cost:
            logger.warning(
                "%s: paper balance $%.2f can't cover $%.2f order; skipping", snapshot.ticker, balance_dollars, cost
            )
            return

        print(
            f"\n[PAPER TRADE PROPOSAL] {snapshot.ticker}\n"
            f"  {prediction.rationale}\n"
            f"  Direction: {direction.upper()}  Price: {price:.2f}  Count: {count}  Cost: ${cost:.2f}\n"
            f"  Demo balance: ${balance_dollars:.2f}\n"
            f"  Time remaining: {snapshot.time_remaining_seconds:.0f}s\n"
        )
        answer = input("Place this DEMO/paper order? [y/N]: ").strip().lower()
        if answer != "y":
            logger.info("%s: order declined by user", snapshot.ticker)
            return

        try:
            response = place_order(self._client, snapshot.ticker, direction, price, count)
        except Exception:
            logger.exception("Order placement failed for %s", snapshot.ticker)
            return

        self._storage.insert_order(
            {
                "ticker": snapshot.ticker,
                "direction": direction,
                "side": "bid" if direction == "yes" else "ask",
                "price": price,
                "count": count,
                "cost_dollars": cost,
                "client_order_id": response.get("client_order_id"),
                "kalshi_order_id": response.get("order_id"),
                "status": "submitted",
                "rationale": prediction.rationale,
                "fill_count": response.get("fill_count"),
                "average_fill_price": response.get("average_fill_price"),
            }
        )
        print(f"Order submitted: order_id={response.get('order_id')} fill_count={response.get('fill_count')}")
