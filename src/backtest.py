from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Literal

Direction = Literal["yes", "no"]
StrategyName = Literal["model", "favorite", "momentum", "agreement"]

STRATEGY_NAMES: tuple[StrategyName, ...] = ("model", "favorite", "momentum", "agreement")

LOW_CONFIDENCE_MIN_N = 20


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% (by default) confidence interval for a binomial proportion.
    Well-behaved at small n and at p near 0/1, unlike a normal approximation."""
    if n == 0:
        return (0.0, 1.0)
    phat = successes / n
    denom = 1 + z**2 / n
    center = (phat + z**2 / (2 * n)) / denom
    margin = (z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass(frozen=True)
class MarketOutcome:
    """Everything a strategy needs to decide + score one settled market."""

    ticker: str
    actual_result: str  # "yes" / "no"
    initial_probability_yes: float | None
    opening_yes_bid: float | None
    opening_yes_ask: float | None
    opening_no_bid: float | None
    opening_no_ask: float | None
    opening_momentum_pct: float | None


def direction_for_strategy(strategy: StrategyName, outcome: MarketOutcome) -> Direction | None:
    """The single source of truth for what each strategy would bet, used
    identically by the historical backtest and by the live multi_trader so
    the two never drift apart. Returns None when the strategy has no signal
    for this market (skipped, not counted)."""
    if strategy == "model":
        if outcome.initial_probability_yes is None:
            return None
        return "yes" if outcome.initial_probability_yes > 0.5 else "no"

    if strategy == "favorite":
        if outcome.opening_yes_bid is None or outcome.opening_yes_ask is None:
            return None
        market_implied = (outcome.opening_yes_bid + outcome.opening_yes_ask) / 2
        return "yes" if market_implied > 0.5 else "no"

    if strategy == "momentum":
        if not outcome.opening_momentum_pct:
            return None
        return "yes" if outcome.opening_momentum_pct > 0 else "no"

    if strategy == "agreement":
        model_dir = direction_for_strategy("model", outcome)
        momentum_dir = direction_for_strategy("momentum", outcome)
        if model_dir is None or momentum_dir is None or model_dir != momentum_dir:
            return None
        return model_dir

    raise ValueError(f"unknown strategy {strategy!r}")


def entry_price_for_direction(outcome: MarketOutcome, direction: Direction) -> float | None:
    price = outcome.opening_yes_ask if direction == "yes" else outcome.opening_no_ask
    return price if price and price > 0 else None


def pnl_for_trade(direction: Direction, entry_price: float, actual_result: str) -> float:
    won = direction == actual_result
    return (1 - entry_price) if won else -entry_price


@dataclass(frozen=True)
class StrategyResult:
    name: StrategyName
    n: int
    wins: int
    win_rate: float
    ci_low: float
    ci_high: float
    avg_pnl: float
    low_confidence: bool


def fetch_market_outcomes(db_path: str) -> list[MarketOutcome]:
    """One row per settled market, with the earliest snapshot (opening
    quote) and earliest momentum_pct joined in from the tables that already
    log them, rather than duplicating that data onto market_lifecycle."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                m.ticker,
                m.actual_result,
                m.initial_probability_yes,
                (SELECT yes_bid FROM snapshots WHERE ticker = m.ticker ORDER BY pulled_at_utc ASC LIMIT 1),
                (SELECT yes_ask FROM snapshots WHERE ticker = m.ticker ORDER BY pulled_at_utc ASC LIMIT 1),
                (SELECT no_bid FROM snapshots WHERE ticker = m.ticker ORDER BY pulled_at_utc ASC LIMIT 1),
                (SELECT no_ask FROM snapshots WHERE ticker = m.ticker ORDER BY pulled_at_utc ASC LIMIT 1),
                (SELECT momentum_pct FROM predictions WHERE ticker = m.ticker ORDER BY computed_at_utc ASC LIMIT 1)
            FROM market_lifecycle m
            WHERE m.actual_result IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()

    return [
        MarketOutcome(
            ticker=r[0],
            actual_result=r[1],
            initial_probability_yes=r[2],
            opening_yes_bid=r[3],
            opening_yes_ask=r[4],
            opening_no_bid=r[5],
            opening_no_ask=r[6],
            opening_momentum_pct=r[7],
        )
        for r in rows
    ]


def run_backtest_for_strategy(strategy: StrategyName, outcomes: list[MarketOutcome]) -> StrategyResult:
    wins = 0
    n = 0
    pnls: list[float] = []

    for outcome in outcomes:
        direction = direction_for_strategy(strategy, outcome)
        if direction is None:
            continue
        entry_price = entry_price_for_direction(outcome, direction)
        if entry_price is None:
            continue

        n += 1
        if direction == outcome.actual_result:
            wins += 1
        pnls.append(pnl_for_trade(direction, entry_price, outcome.actual_result))

    win_rate = wins / n if n else 0.0
    ci_low, ci_high = wilson_interval(wins, n)
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0

    return StrategyResult(
        name=strategy,
        n=n,
        wins=wins,
        win_rate=win_rate,
        ci_low=ci_low,
        ci_high=ci_high,
        avg_pnl=avg_pnl,
        low_confidence=n < LOW_CONFIDENCE_MIN_N,
    )


def run_backtests(db_path: str | None = None, outcomes: list[MarketOutcome] | None = None) -> list[StrategyResult]:
    if outcomes is None:
        outcomes = fetch_market_outcomes(db_path)
    return [run_backtest_for_strategy(strategy, outcomes) for strategy in STRATEGY_NAMES]


@dataclass(frozen=True)
class PatternLogStats:
    total_settled: int
    up_count: int
    down_count: int
    strategy_results: list[StrategyResult]


def pattern_log_stats(db_path: str | None = None, outcomes: list[MarketOutcome] | None = None) -> PatternLogStats:
    if outcomes is None:
        outcomes = fetch_market_outcomes(db_path)
    up_count = sum(1 for o in outcomes if o.actual_result == "yes")
    down_count = sum(1 for o in outcomes if o.actual_result == "no")
    strategy_results = run_backtests(outcomes=outcomes)
    return PatternLogStats(
        total_settled=len(outcomes),
        up_count=up_count,
        down_count=down_count,
        strategy_results=strategy_results,
    )
