from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from config.settings import Settings, load_settings
from src.backtest import (
    LOW_CONFIDENCE_MIN_N,
    Direction,
    MarketOutcome,
    StrategyName,
    book_imbalance_direction,
    direction_for_strategy,
    divergence_direction,
    fetch_market_outcomes,
    run_signal_accuracy,
    trend_direction,
)
from src.storage import Storage

logger = logging.getLogger("kalshi_bot")

# This module only ever READS snapshots/market_lifecycle/predictions/
# divergence_events/orderbook_snapshots and WRITES to its own two tables
# (signal_research_runs, signal_research_findings). It never writes to
# src/predictor.py, src/trader.py's trading gates, or src/multi_trader.py's
# tiers/position sizing -- a PASS verdict is a proposal row in a table,
# nothing more. Promoting a signal into the live model is always a
# separate, later, explicitly human-approved change.

Verdict = Literal["PASS", "FAIL", "INCONCLUSIVE"]

# Fold sizing -- calendar-day-based (not count-based) since our live history
# is currently ~9-10 days old, a very different regime from the 90-day
# Binance-replay dataset backtesting/calibration_fix.py's walk_forward() was
# sized for. See the plan doc / README for the full rationale.
MIN_TRAIN_DAYS = 4
FOLD_SIZE_DAYS = 1
MIN_FOLDS_FOR_VERDICT = 3

# Below this many train-set firings in a given direction, that direction's
# naive calibrated probability is too noisy to trust for Brier scoring.
MIN_TRAIN_FIRINGS_PER_DIRECTION = 5

# Verdict thresholds -- PASS reuses the exact bar multi_trader.py's tier-2
# (highest-conviction) stake tier already demands (STRATEGY_TIER2_MIN_N/
# STRATEGY_TIER2_MIN_CI_LOWER in config/settings.py); a signal that's never
# been live-traded should clear at least that, not less.
PASS_MIN_N = 50
PASS_MIN_CI_LOWER = 0.55
FAIL_MAX_CI_UPPER = 0.50
# Reused verbatim from backtesting/calibration_fix.py::walk_forward()'s
# `consistent = folds_improved >= len(folds) * 0.6` precedent.
FOLD_CONSISTENCY_MIN_RATE = 0.6
FAIL_FOLD_CONSISTENCY_MAX_RATE = 0.4
HIGH_LIQUIDITY_MIN_N_FOR_CHECK = LOW_CONFIDENCE_MIN_N


def _strategy_direction_fn(strategy: StrategyName) -> Callable[[MarketOutcome], Direction | None]:
    return lambda outcome: direction_for_strategy(strategy, outcome)


CANDIDATE_SIGNALS: list[tuple[str, Callable[[MarketOutcome], Direction | None]]] = [
    ("model", _strategy_direction_fn("model")),
    ("favorite", _strategy_direction_fn("favorite")),
    ("momentum", _strategy_direction_fn("momentum")),
    ("agreement", _strategy_direction_fn("agreement")),
    ("trend", trend_direction),
    ("divergence", divergence_direction),
    ("book_imbalance", book_imbalance_direction),
]


def _parse_close(outcome: MarketOutcome) -> datetime | None:
    ts = outcome.close_time_utc
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


@dataclass(frozen=True)
class Fold:
    index: int
    train: list[MarketOutcome]
    test: list[MarketOutcome]


def build_folds(
    outcomes: list[MarketOutcome],
    min_train_days: int = MIN_TRAIN_DAYS,
    fold_size_days: int = FOLD_SIZE_DAYS,
) -> list[Fold]:
    """Sequential, chronological, expanding-window folds by close_time_utc:
    each fold's train set is every outcome strictly before the fold's start
    day, test is the fold's own day(s) -- no future leakage by construction.
    Adapted from backtesting/calibration_fix.py::walk_forward()'s proven
    pattern, re-sized in calendar days for our much smaller, younger dataset
    (that file's folds are trade-count-based, sized for thousands of trades
    over 90 days -- a different regime). Returns [] gracefully when there
    isn't yet min_train_days + fold_size_days of history."""
    dated = [(o, dt) for o in outcomes if (dt := _parse_close(o)) is not None]
    dated.sort(key=lambda pair: pair[1])
    if not dated:
        return []

    first_day = dated[0][1].date()
    last_day = dated[-1][1].date()
    fold_start_day = first_day + timedelta(days=min_train_days)

    folds: list[Fold] = []
    fold_index = 0
    while fold_start_day <= last_day:
        fold_end_day = fold_start_day + timedelta(days=fold_size_days)
        test = [o for o, dt in dated if fold_start_day <= dt.date() < fold_end_day]
        if test:
            train = [o for o, dt in dated if dt.date() < fold_start_day]
            folds.append(Fold(index=fold_index, train=train, test=test))
            fold_index += 1
        # A day (or fold_size_days window) with zero settled markets is
        # skipped, not treated as the end of history -- an outage shouldn't
        # silently truncate every later fold.
        fold_start_day = fold_end_day

    return folds


def _win_rate_train_by_direction(
    direction_fn: Callable[[MarketOutcome], Direction | None], train: list[MarketOutcome]
) -> tuple[float | None, float | None]:
    """Train-only frequency calibration, split by direction rather than one
    symmetric scalar -- a signal isn't guaranteed equally skilled calling
    "yes" vs "no". Returns None for a direction with too few train firings
    to trust (MIN_TRAIN_FIRINGS_PER_DIRECTION)."""
    yes_total = yes_wins = no_total = no_wins = 0
    for outcome in train:
        direction = direction_fn(outcome)
        if direction is None:
            continue
        correct = direction == outcome.actual_result
        if direction == "yes":
            yes_total += 1
            yes_wins += correct
        else:
            no_total += 1
            no_wins += correct

    win_rate_yes = yes_wins / yes_total if yes_total >= MIN_TRAIN_FIRINGS_PER_DIRECTION else None
    win_rate_no = no_wins / no_total if no_total >= MIN_TRAIN_FIRINGS_PER_DIRECTION else None
    return win_rate_yes, win_rate_no


def _fold_brier_errors(
    direction_fn: Callable[[MarketOutcome], Direction | None],
    train: list[MarketOutcome],
    test: list[MarketOutcome],
) -> tuple[list[float], list[float]]:
    """Fit a naive per-direction calibrated probability on train only, score
    it (and the live model's own initial_probability_yes, on the identical
    rows) out-of-sample on test. Parallel (signal_errors, baseline_errors)
    lists, one entry per scoreable test row -- leak-free by construction."""
    win_rate_yes, win_rate_no = _win_rate_train_by_direction(direction_fn, train)
    signal_errors: list[float] = []
    baseline_errors: list[float] = []

    for outcome in test:
        direction = direction_fn(outcome)
        if direction is None:
            continue
        if direction == "yes" and win_rate_yes is None:
            continue
        if direction == "no" and win_rate_no is None:
            continue
        if outcome.initial_probability_yes is None:
            continue

        p_signal = win_rate_yes if direction == "yes" else (1 - win_rate_no)
        actual = 1.0 if outcome.actual_result == "yes" else 0.0
        signal_errors.append((p_signal - actual) ** 2)
        baseline_errors.append((outcome.initial_probability_yes - actual) ** 2)

    return signal_errors, baseline_errors


def _liquidity_check(
    direction_fn: Callable[[MarketOutcome], Direction | None],
    outcomes: list[MarketOutcome],
    min_volume: float,
) -> tuple[bool | None, int, float | None, int, float | None]:
    """Generalizes the real bug found with the divergence signal: does the
    edge only show up in thin-liquidity conditions? Splits by opening_volume
    vs. min_volume (settings.divergence_min_volume -- the exact threshold
    that bug was fixed with, not a new arbitrary number). None (can't
    assess) if the high-liquidity bucket is too small; that blocks PASS the
    same as an explicit failure."""
    low = [o for o in outcomes if o.opening_volume is not None and o.opening_volume < min_volume]
    high = [o for o in outcomes if o.opening_volume is not None and o.opening_volume >= min_volume]

    low_result = run_signal_accuracy("low_liquidity", direction_fn, low)
    high_result = run_signal_accuracy("high_liquidity", direction_fn, high)

    low_win_rate = low_result.win_rate if low_result.n else None
    high_win_rate = high_result.win_rate if high_result.n else None

    if high_result.n < HIGH_LIQUIDITY_MIN_N_FOR_CHECK:
        passed = None
    elif low_result.ci_low > 0.5 and high_result.ci_low <= 0.5:
        passed = False
    else:
        passed = True

    return passed, low_result.n, low_win_rate, high_result.n, high_win_rate


def _classify_verdict(
    n: int,
    ci_low: float,
    ci_high: float,
    eligible_fold_count: int,
    fold_consistency_rate: float | None,
    liquidity_check_passed: bool | None,
    brier_delta: float | None,
) -> tuple[Verdict, str]:
    if n < LOW_CONFIDENCE_MIN_N:
        return "INCONCLUSIVE", f"n={n} below the minimum of {LOW_CONFIDENCE_MIN_N} for any verdict"
    if eligible_fold_count < MIN_FOLDS_FOR_VERDICT:
        return (
            "INCONCLUSIVE",
            f"only {eligible_fold_count} eligible out-of-sample fold(s), need >= {MIN_FOLDS_FOR_VERDICT}",
        )

    if ci_high <= FAIL_MAX_CI_UPPER:
        return "FAIL", f"95% CI upper bound ({ci_high:.0%}) doesn't clear a coinflip"
    if liquidity_check_passed is False:
        return (
            "FAIL",
            "edge only shows up in the thin-liquidity bucket -- same class of artifact as the "
            "Sep 2026 divergence min-volume fix",
        )
    if fold_consistency_rate is not None and fold_consistency_rate < FAIL_FOLD_CONSISTENCY_MAX_RATE:
        return (
            "FAIL",
            f"edge flips sign across most time periods (Brier improved in only "
            f"{fold_consistency_rate:.0%} of folds) -- regime-dependent, not durable",
        )

    if (
        n >= PASS_MIN_N
        and ci_low >= PASS_MIN_CI_LOWER
        and fold_consistency_rate is not None
        and fold_consistency_rate >= FOLD_CONSISTENCY_MIN_RATE
        and liquidity_check_passed is True
        and brier_delta is not None
        and brier_delta > 0
    ):
        return (
            "PASS",
            f"clears every gate: n={n}, ci_low={ci_low:.0%}, {fold_consistency_rate:.0%} fold "
            f"consistency, liquidity check passed, Brier improved by {brier_delta:.4f} -- "
            "candidate for promotion, still requires an explicit human-approved change to wire in",
        )

    return "INCONCLUSIVE", "does not yet clear the PASS bar on every gate simultaneously, and hasn't failed outright"


@dataclass(frozen=True)
class SignalFinding:
    signal_name: str
    verdict: Verdict
    n: int
    wins: int
    win_rate: float
    ci_low: float
    ci_high: float
    brier_signal: float | None
    brier_baseline: float | None
    brier_delta: float | None
    brier_n: int
    fold_count: int
    eligible_fold_count: int
    folds_consistent: int
    fold_consistency_rate: float | None
    liquidity_check_passed: bool | None
    low_liquidity_n: int
    low_liquidity_win_rate: float | None
    high_liquidity_n: int
    high_liquidity_win_rate: float | None
    notes: str


def score_signal(
    name: str,
    direction_fn: Callable[[MarketOutcome], Direction | None],
    folds: list[Fold],
    liquidity_min_volume: float,
) -> SignalFinding:
    pooled_test = [o for f in folds for o in f.test]
    accuracy = run_signal_accuracy(name, direction_fn, pooled_test)

    all_signal_errors: list[float] = []
    all_baseline_errors: list[float] = []
    eligible_fold_count = 0
    folds_consistent = 0
    for f in folds:
        signal_errors, baseline_errors = _fold_brier_errors(direction_fn, f.train, f.test)
        if not signal_errors:
            continue
        eligible_fold_count += 1
        fold_signal_brier = sum(signal_errors) / len(signal_errors)
        fold_baseline_brier = sum(baseline_errors) / len(baseline_errors)
        if fold_signal_brier < fold_baseline_brier:
            folds_consistent += 1
        all_signal_errors.extend(signal_errors)
        all_baseline_errors.extend(baseline_errors)

    brier_signal = sum(all_signal_errors) / len(all_signal_errors) if all_signal_errors else None
    brier_baseline = sum(all_baseline_errors) / len(all_baseline_errors) if all_baseline_errors else None
    brier_delta = brier_baseline - brier_signal if brier_signal is not None and brier_baseline is not None else None
    fold_consistency_rate = folds_consistent / eligible_fold_count if eligible_fold_count else None

    liquidity_passed, low_n, low_wr, high_n, high_wr = _liquidity_check(
        direction_fn, pooled_test, liquidity_min_volume
    )

    verdict, notes = _classify_verdict(
        n=accuracy.n,
        ci_low=accuracy.ci_low,
        ci_high=accuracy.ci_high,
        eligible_fold_count=eligible_fold_count,
        fold_consistency_rate=fold_consistency_rate,
        liquidity_check_passed=liquidity_passed,
        brier_delta=brier_delta,
    )

    return SignalFinding(
        signal_name=name,
        verdict=verdict,
        n=accuracy.n,
        wins=accuracy.wins,
        win_rate=accuracy.win_rate,
        ci_low=accuracy.ci_low,
        ci_high=accuracy.ci_high,
        brier_signal=brier_signal,
        brier_baseline=brier_baseline,
        brier_delta=brier_delta,
        brier_n=len(all_signal_errors),
        fold_count=len(folds),
        eligible_fold_count=eligible_fold_count,
        folds_consistent=folds_consistent,
        fold_consistency_rate=fold_consistency_rate,
        liquidity_check_passed=liquidity_passed,
        low_liquidity_n=low_n,
        low_liquidity_win_rate=low_wr,
        high_liquidity_n=high_n,
        high_liquidity_win_rate=high_wr,
        notes=notes,
    )


def run_signal_research(
    db_path: str, settings: Settings, outcomes: list[MarketOutcome] | None = None
) -> tuple[int, list[SignalFinding]]:
    if outcomes is None:
        outcomes = fetch_market_outcomes(db_path)

    folds = build_folds(outcomes, MIN_TRAIN_DAYS, FOLD_SIZE_DAYS)

    dated_days = sorted({dt.date() for o in outcomes if (dt := _parse_close(o)) is not None})
    history_span_days = (dated_days[-1] - dated_days[0]).days + 1 if dated_days else 0
    notes = None
    if not folds:
        notes = f"insufficient history: {history_span_days} day(s) available, need >= {MIN_TRAIN_DAYS + FOLD_SIZE_DAYS}"

    storage = Storage(db_path)
    try:
        run_id = storage.insert_signal_research_run(
            {
                "run_at_utc": datetime.now(timezone.utc).isoformat(),
                "total_outcomes_n": len(outcomes),
                "history_span_days": history_span_days,
                "min_train_days": MIN_TRAIN_DAYS,
                "fold_size_days": FOLD_SIZE_DAYS,
                "notes": notes,
            }
        )

        findings: list[SignalFinding] = []
        for name, direction_fn in CANDIDATE_SIGNALS:
            finding = score_signal(name, direction_fn, folds, settings.divergence_min_volume)
            findings.append(finding)
            storage.insert_signal_research_finding({**asdict(finding), "run_id": run_id})
    finally:
        storage.close()

    return run_id, findings


def run_signal_research_once(settings: Settings) -> None:
    """poller.run_forever()'s scheduled entry point -- mirrors
    publish_analytics.publish_once(settings)'s shape, but never commits/
    pushes anything; findings stay in the local, gitignored DB."""
    run_id, findings = run_signal_research(settings.db_path, settings)
    pass_count = sum(1 for f in findings if f.verdict == "PASS")
    fail_count = sum(1 for f in findings if f.verdict == "FAIL")
    inconclusive_count = len(findings) - pass_count - fail_count
    logger.info(
        "Signal research run %d: %d signals scored (%d PASS, %d FAIL, %d INCONCLUSIVE)",
        run_id,
        len(findings),
        pass_count,
        fail_count,
        inconclusive_count,
    )


_RUN_COLUMNS = "id, run_at_utc, total_outcomes_n, history_span_days, min_train_days, fold_size_days, notes"
_FINDING_COLUMNS = (
    "signal_name, verdict, n, win_rate, ci_low, ci_high, brier_delta, brier_n, "
    "eligible_fold_count, fold_consistency_rate, liquidity_check_passed, notes"
)


def fetch_run(db_path: str, run_id: int | None) -> tuple | None:
    conn = sqlite3.connect(db_path)
    try:
        if run_id is None:
            return conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM signal_research_runs ORDER BY id DESC LIMIT 1"  # noqa: S608
            ).fetchone()
        return conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM signal_research_runs WHERE id = ?", (run_id,)  # noqa: S608
        ).fetchone()
    finally:
        conn.close()


def fetch_findings(db_path: str, run_id: int) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT {_FINDING_COLUMNS} FROM signal_research_findings WHERE run_id = ? ORDER BY id",  # noqa: S608
            (run_id,),
        ).fetchall()
    finally:
        conn.close()


def print_report(run_row: tuple | None, findings: list[tuple]) -> None:
    if run_row is None:
        print("No signal research runs yet -- run `python -m src.signal_research` first.")
        return

    run_id, run_at, total_n, span_days, min_train_days, fold_size_days, run_notes = run_row
    print(f"Signal research run #{run_id} at {run_at}")
    print(
        f"{total_n} settled markets, {span_days or 0:.1f} day(s) of history "
        f"(min_train_days={min_train_days}, fold_size_days={fold_size_days})"
    )
    if run_notes:
        print(run_notes)
    print()

    if not findings:
        print("No signals scored.")
        return

    header = (
        f"{'signal':<16}{'verdict':<13}{'n':>5}  {'win rate':>9}  {'95% CI':>13}  "
        f"{'brier Δ':>9}  {'folds':>6}  {'liquidity':>10}"
    )
    print(header)
    for row in findings:
        name, verdict, n, win_rate, ci_low, ci_high, brier_delta, _brier_n, eligible_folds, _fold_rate, liq, notes = row
        ci_label = f"[{ci_low * 100:.0f}-{ci_high * 100:.0f}%]"
        brier_label = f"{brier_delta:+.4f}" if brier_delta is not None else "n/a"
        liq_label = "pass" if liq == 1 else ("fail" if liq == 0 else "n/a")
        print(
            f"{name:<16}{verdict:<13}{n:>5}  {win_rate * 100:>8.1f}%  {ci_label:>13}  "
            f"{brier_label:>9}  {eligible_folds:>6}  {liq_label:>10}"
        )
        print(f"    {notes}")
    print()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Weekly signal research: re-test logged signals against settled history, "
        "with train/test-split rigor. Proposal-only -- never changes live trading behavior."
    )
    parser.add_argument("--report", action="store_true", help="Print the latest (or --run-id) run instead of running")
    parser.add_argument("--run-id", type=int, default=None, help="With --report, show this run instead of the latest")
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args()

    settings = load_settings()
    db_path = args.db_path or settings.db_path

    if args.report:
        run_row = fetch_run(db_path, args.run_id)
        findings = fetch_findings(db_path, run_row[0]) if run_row else []
        print_report(run_row, findings)
    else:
        run_id, findings = run_signal_research(db_path, settings)
        print(f"Signal research run #{run_id}: {len(findings)} signals scored")


if __name__ == "__main__":
    main()
