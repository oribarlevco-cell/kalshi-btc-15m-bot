from __future__ import annotations

import logging
import math
import sys

from config.settings import Settings, load_settings
from src.backtest import MarketOutcome, StrategyResult, direction_for_strategy, run_backtests
from src.kalshi_client import KalshiClient
from src.markets import MarketSnapshot
from src.orders import get_balance, place_order
from src.poller import run_forever
from src.storage import Storage

logger = logging.getLogger("kalshi_bot")

AUTOMATED_STRATEGIES = ("momentum", "favorite", "agreement")


def stake_for_result(result: StrategyResult | None, settings: Settings) -> float:
    """Evidence-based position sizing: a strategy's stake only increases once
    its OWN backtested win rate has a 95%-Wilson-CI lower bound above a
    coinflip, at a large enough sample size. See README for the full table."""
    if result is None:
        return settings.max_order_cost_dollars

    if result.n >= settings.strategy_tier2_min_n and result.ci_low >= settings.strategy_tier2_min_ci_lower:
        return settings.max_order_cost_dollars * settings.strategy_tier2_multiplier
    if result.n >= settings.strategy_tier1_min_n and result.ci_low >= settings.strategy_tier1_min_ci_lower:
        return settings.max_order_cost_dollars * settings.strategy_tier1_multiplier
    return settings.max_order_cost_dollars


class MultiTrader:
    """Automated, unattended paper trading for momentum/favorite/agreement --
    NOT gated by a confirmation prompt (unlike src.trader.Trader). Paper/demo
    money only: settings.can_multi_trade requires KALSHI_ENV=demo, API
    credentials, and the separate MULTI_STRATEGY_TRADING_ENABLED flag."""

    def __init__(self, client: KalshiClient, storage: Storage, settings: Settings, enabled: bool = False):
        self._client = client
        self._storage = storage
        self._settings = settings
        self._enabled = enabled and settings.can_multi_trade

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        if not self._enabled:
            return

        outcome = self._outcome_for_open_market(snapshot)
        if outcome is None:
            return

        backtest_results = {r.name: r for r in run_backtests(self._settings.db_path)}
        for strategy in AUTOMATED_STRATEGIES:
            self._maybe_trade(strategy, snapshot, outcome, backtest_results.get(strategy))

    def _outcome_for_open_market(self, snapshot: MarketSnapshot) -> MarketOutcome | None:
        """Build a MarketOutcome from the market's OPENING quote/signals for
        direction decisions, matching exactly what the backtest scores
        against -- but note we trade at the CURRENT snapshot's price, not
        the recorded opening price (that's only used for direction/backtest
        consistency, not execution)."""
        initial_probability_yes = self._storage.initial_probability_yes(snapshot.ticker)
        if initial_probability_yes is None:
            return None

        opening = self._storage.opening_quote(snapshot.ticker)
        if opening is None:
            return None
        opening_yes_bid, opening_yes_ask, opening_no_bid, opening_no_ask = opening

        return MarketOutcome(
            ticker=snapshot.ticker,
            actual_result="",  # unknown yet -- not used by direction_for_strategy
            initial_probability_yes=initial_probability_yes,
            opening_yes_bid=opening_yes_bid,
            opening_yes_ask=opening_yes_ask,
            opening_no_bid=opening_no_bid,
            opening_no_ask=opening_no_ask,
            opening_momentum_pct=self._storage.opening_momentum_pct(snapshot.ticker),
        )

    def _maybe_trade(
        self, strategy: str, snapshot: MarketSnapshot, outcome: MarketOutcome, backtest_result: StrategyResult | None
    ) -> None:
        if self._storage.has_order_for_strategy(snapshot.ticker, strategy):
            return
        open_positions = self._storage.count_open_positions_for_strategy(strategy)
        if open_positions >= self._settings.multi_strategy_max_concurrent_positions:
            logger.debug("%s: at max concurrent positions; skipping %s", strategy, snapshot.ticker)
            return

        direction = direction_for_strategy(strategy, outcome)
        if direction is None:
            return

        price = snapshot.yes_ask if direction == "yes" else snapshot.no_ask
        if not price or price <= 0:
            return

        stake = stake_for_result(backtest_result, self._settings)
        count = max(1, math.floor(stake / price))
        cost = round(price * count, 2)

        try:
            balance = get_balance(self._client)
        except Exception:
            logger.exception("%s: failed to fetch balance; skipping %s", strategy, snapshot.ticker)
            return
        balance_dollars = float(balance.get("balance_dollars") or 0)
        if balance_dollars < cost:
            logger.warning(
                "%s: paper balance $%.2f can't cover $%.2f; skipping %s",
                strategy, balance_dollars, cost, snapshot.ticker,
            )
            return

        try:
            response = place_order(self._client, snapshot.ticker, direction, price, count)
        except Exception:
            logger.exception("%s: order placement failed for %s", strategy, snapshot.ticker)
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
                "rationale": f"auto:{strategy} stake=${stake:.2f}",
                "fill_count": response.get("fill_count"),
                "average_fill_price": response.get("average_fill_price"),
                "strategy": strategy,
            }
        )
        logger.info(
            "[AUTO %s] %s %s x%d @ %.2f ($%.2f) order_id=%s",
            strategy,
            snapshot.ticker,
            direction.upper(),
            count,
            price,
            cost,
            response.get("order_id"),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    if not settings.can_multi_trade:
        reasons = []
        if not settings.multi_strategy_trading_enabled:
            reasons.append("MULTI_STRATEGY_TRADING_ENABLED is not 'true' in your .env")
        if not settings.has_credentials:
            reasons.append("KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH are not set")
        if settings.env != "demo":
            reasons.append("KALSHI_ENV must be 'demo' -- this build only places paper orders, never prod/real-money")
        print("multi_trader requires all of the following:\n  - " + "\n  - ".join(reasons), file=sys.stderr)
        sys.exit(1)

    client = KalshiClient(settings)
    storage = Storage(settings.db_path)
    trader = MultiTrader(client, storage, settings, enabled=True)

    try:
        run_forever(client, storage, settings, on_snapshot=trader.on_snapshot)
    finally:
        storage.close()


if __name__ == "__main__":
    main()
