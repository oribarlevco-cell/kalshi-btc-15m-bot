"""
Tail-Confidence Calibration Experiment
=======================================

Follow-up to backtest_engine.py's calibration report, which found that the
live model's most extreme calls are overconfident: at 90-100% predicted
probability, only ~82% actually resolve that way (and that bucket has the
worst per-trade P&L of any confidence band). This script tests whether a
simple post-hoc recalibration -- Platt scaling, i.e. squashing the model's
log-odds toward 0 before turning them back into a probability -- fixes that,
using a genuine out-of-sample split so we don't just fool ourselves by
fitting and grading on the same data.

This is a backtest-only experiment. It imports backtest_engine.py (read-only)
and does not touch src/predictor.py or any live-bot file -- if the fix looks
real here, promoting it into the live model would be a separate, explicit
change to src/predictor.py, made deliberately, not automatically from this
script.

Method: walk-forward, not a single train/test split
-----------------------------------------------------
A first pass at this (fit Platt scaling once on the first 70% of history,
grade on the last 30%) found a real but UNSTABLE effect: P&L improved
out-of-sample but the calibration (Brier score, tail bucket shape) barely
moved, and the tails were still off by a similar margin in the held-out
period. That's the signature of a fix that doesn't transfer across time --
one static snapshot of "how overconfident is the model" doesn't necessarily
hold a few weeks later (BTC's volatility regime shifts).

So this version walks forward through the whole history in sequential
folds: for each fold, fit Platt scaling using only trades STRICTLY BEFORE
that fold (never using future data), evaluate on that fold only, then slide
forward. This is the standard way to test whether a recalibration is a real,
durable fix rather than an artifact of one lucky/unlucky split -- it answers
"would refitting this periodically, as more history came in, have actually
helped, fold after fold" instead of "did one split work once."

`--rolling-window-trades N` switches training from expanding (use all prior
history) to a trailing window of the last N trades only -- worth comparing,
since a recency-focused fit may track regime drift better than one that
also weighs stale, out-of-regime history equally.

Run:
    python3 backtesting/calibration_fix.py                          # expanding window
    python3 backtesting/calibration_fix.py --rolling-window-trades 2000
    python3 backtesting/calibration_fix.py --fold-trades 500 --min-train-trades 2000
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_engine import (  # noqa: E402
    CONTRACT_PAYOUT,
    RESULTS_DIR,
    BacktestTrade,
    calibration_report,
    fetch_binance_1m_klines,
    kalshi_fee,
    load_settings,
    resample_to_15min,
    run_backtest,
)


def _logit(p: np.ndarray, eps: float = 1e-4) -> np.ndarray:
    p = np.clip(p, eps, 1 - eps)
    return np.log(p / (1 - p))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x))


def fit_platt_scaling(raw_p: np.ndarray, outcome: np.ndarray, iterations: int = 2000, lr: float = 0.1) -> tuple[float, float]:
    """Fit calibrated_p = sigmoid(a * logit(raw_p) + b) by gradient descent
    on log-loss. a < 1 shrinks confident calls toward 0.5 (fixes
    overconfidence); a > 1 would sharpen underconfident calls."""
    x = _logit(raw_p)
    y = outcome.astype(float)
    a, b = 1.0, 0.0
    n = len(x)

    for _ in range(iterations):
        z = a * x + b
        pred = _sigmoid(z)
        error = pred - y  # d(log-loss)/dz
        grad_a = float(np.mean(error * x))
        grad_b = float(np.mean(error))
        a -= lr * grad_a
        b -= lr * grad_b

    return a, b


def _repriced(trade: BacktestTrade, calibrated_p: float) -> BacktestTrade:
    """Recompute entry price/fee/P&L using a recalibrated probability for the
    SAME trade (same direction, same actual outcome) -- isolates the effect
    of a different assumed fill price from everything else."""
    entry_price = calibrated_p if trade.direction == "yes" else 1 - calibrated_p
    entry_price = min(max(round(entry_price, 2), 0.01), 0.99)
    fee = kalshi_fee(entry_price)
    pnl = (CONTRACT_PAYOUT - entry_price - fee) if trade.won else -(entry_price + fee)

    return BacktestTrade(
        window_open=trade.window_open,
        window_close=trade.window_close,
        decision_time=trade.decision_time,
        floor_strike=trade.floor_strike,
        btc_price_at_decision=trade.btc_price_at_decision,
        direction=trade.direction,
        probability_yes=calibrated_p,
        confidence=abs(calibrated_p - 0.5) * 2,
        entry_price=entry_price,
        fee=fee,
        actual_close_price=trade.actual_close_price,
        actual_result=trade.actual_result,
        won=trade.won,
        pnl=pnl,
    )


def apply_platt(trade: BacktestTrade, a: float, b: float) -> BacktestTrade:
    calibrated_p = float(_sigmoid(a * _logit(np.array([trade.probability_yes]))[0] + b))
    return _repriced(trade, calibrated_p)


def apply_cap(trade: BacktestTrade, lo: float, hi: float) -> BacktestTrade:
    """Hard-clip the model's probability into [lo, hi] -- a crude but
    targeted alternative to Platt scaling: instead of smoothly compressing
    every probability's log-odds (which also disturbs the already-decent
    20-80% range, as the walk-forward Platt run showed), this only touches
    the extreme tail where the miscalibration actually lives."""
    calibrated_p = min(max(trade.probability_yes, lo), hi)
    return _repriced(trade, calibrated_p)


@dataclass
class FoldStats:
    n: int
    win_rate: float
    total_pnl: float
    total_fees: float
    brier: float


def fold_stats(trades: list[BacktestTrade]) -> FoldStats:
    if not trades:
        return FoldStats(0, 0.0, 0.0, 0.0, 0.0)
    n = len(trades)
    win_rate = sum(1 for t in trades if t.won) / n
    total_pnl = sum(t.pnl for t in trades)
    total_fees = sum(t.fee for t in trades)
    brier = sum((t.probability_yes - (1 if t.actual_result == "yes" else 0)) ** 2 for t in trades) / n
    return FoldStats(n, win_rate, total_pnl, total_fees, brier)


@dataclass
class Fold:
    index: int
    train_n: int
    test_trades: list[BacktestTrade]
    calibrated_test_trades: list[BacktestTrade]
    label: str  # e.g. "a=0.68 b=-0.01" (platt) or "cap=[0.10,0.90]" (cap) -- fold-specific fit description


def platt_fitter(train: list[BacktestTrade]):
    train_p = np.array([t.probability_yes for t in train])
    train_y = np.array([1 if t.actual_result == "yes" else 0 for t in train])
    a, b = fit_platt_scaling(train_p, train_y)
    return (lambda t: apply_platt(t, a, b)), f"a={a:.3f} b={b:.3f}"


def cap_fitter(lo: float, hi: float):
    """A cap's bounds are fixed hyperparameters, not fit from train data --
    returns a fitter with the same shape as platt_fitter for a uniform
    walk_forward harness, but ignores `train` entirely."""

    def _fit(train: list[BacktestTrade]):
        return (lambda t: apply_cap(t, lo, hi)), f"cap=[{lo:.2f},{hi:.2f}]"

    return _fit


def walk_forward(
    trades: list[BacktestTrade],
    min_train: int,
    fold_size: int,
    rolling_window: Optional[int],
    fitter,
) -> list[Fold]:
    """Slide through `trades` in chronological order, refitting via `fitter`
    before each fold using only earlier trades, then grading that fold
    out-of-sample. `rolling_window=None` uses all prior history (expanding);
    an int uses only the trailing N trades (recency-focused)."""
    folds = []
    i = min_train
    fold_idx = 0
    while i + fold_size <= len(trades) or (len(trades) - i) >= fold_size // 2:
        test = trades[i : i + fold_size]
        if not test:
            break
        train_start = max(0, i - rolling_window) if rolling_window else 0
        train = trades[train_start:i]

        apply_fn, label = fitter(train)
        calibrated_test = [apply_fn(t) for t in test]
        folds.append(Fold(fold_idx, len(train), test, calibrated_test, label))
        fold_idx += 1
        i += fold_size

    return folds


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward test of a Platt-scaling fix for tail overconfidence.")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--fold-trades", type=int, default=750, help="Trades per out-of-sample evaluation fold (~1 week)")
    parser.add_argument("--min-train-trades", type=int, default=1500, help="Minimum trades before the first fold is evaluated")
    parser.add_argument(
        "--rolling-window-trades", type=int, default=None,
        help="Train on only the trailing N trades instead of all prior history (tests recency vs. expanding-window fits)",
    )
    parser.add_argument(
        "--method", choices=["platt", "cap"], default="platt",
        help="platt: smooth log-odds compression (fit on train). cap: hard-clip probability into [--cap-lo, --cap-hi] (fixed, no fit).",
    )
    parser.add_argument("--cap-lo", type=float, default=0.10, help="Lower probability bound for --method cap")
    parser.add_argument("--cap-hi", type=float, default=0.90, help="Upper probability bound for --method cap")
    args = parser.parse_args()

    settings = load_settings()

    df_1m = fetch_binance_1m_klines(days=args.days, symbol=args.symbol, use_cache=not args.no_cache)
    print(f"Loaded {len(df_1m)} 1-min bars: {df_1m.index.min()} -> {df_1m.index.max()}")
    df_15m = resample_to_15min(df_1m)

    result = run_backtest(df_1m, df_15m, settings)
    trades = sorted(result.trades, key=lambda t: t.decision_time)
    print(f"Simulated {len(trades)} trades\n")

    fitter = platt_fitter if args.method == "platt" else cap_fitter(args.cap_lo, args.cap_hi)
    folds = walk_forward(trades, args.min_train_trades, args.fold_trades, args.rolling_window_trades, fitter)
    if not folds:
        print(f"Not enough trades ({len(trades)}) for even one walk-forward fold with these settings.")
        return

    window_desc = f"trailing {args.rolling_window_trades} trades" if args.rolling_window_trades else "expanding (all prior history)"
    print(f"Walk-forward [{args.method}]: {len(folds)} folds, ~{args.fold_trades} trades each, training window = {window_desc}\n")

    print(f"{'fold':>4}  {'period (test)':<37}{'train n':>8}  {'fit':<18}  {'raw pnl':>9}  {'cal pnl':>9}  {'raw brier':>10}  {'cal brier':>10}")
    for f in folds:
        period = f"{f.test_trades[0].decision_time.strftime('%m-%d')} -> {f.test_trades[-1].decision_time.strftime('%m-%d')}"
        raw_s, cal_s = fold_stats(f.test_trades), fold_stats(f.calibrated_test_trades)
        print(
            f"{f.index:>4}  {period:<37}{f.train_n:>8}  {f.label:<18}  "
            f"{'$' + format(raw_s.total_pnl, '.0f'):>9}  {'$' + format(cal_s.total_pnl, '.0f'):>9}  "
            f"{raw_s.brier:>10.4f}  {cal_s.brier:>10.4f}"
        )

    all_raw = [t for f in folds for t in f.test_trades]
    all_cal = [t for f in folds for t in f.calibrated_test_trades]
    raw_stats, cal_stats = fold_stats(all_raw), fold_stats(all_cal)
    folds_pnl_improved = sum(1 for f in folds if fold_stats(f.calibrated_test_trades).total_pnl > fold_stats(f.test_trades).total_pnl)
    folds_brier_improved = sum(1 for f in folds if fold_stats(f.calibrated_test_trades).brier < fold_stats(f.test_trades).brier)

    print()
    print("=" * 65)
    print(f"AGGREGATE ACROSS ALL {len(folds)} OUT-OF-SAMPLE FOLDS")
    print("=" * 65)
    print(f"{'metric':<16}{'raw':>15}{'calibrated':>18}")
    print(f"{'n trades':<16}{raw_stats.n:>15}{cal_stats.n:>18}")
    print(f"{'win rate':<16}{raw_stats.win_rate:>14.1%}{cal_stats.win_rate:>17.1%}")
    print(f"{'total P&L':<16}{'$' + format(raw_stats.total_pnl, '.2f'):>15}{'$' + format(cal_stats.total_pnl, '.2f'):>18}")
    print(f"{'brier score':<16}{raw_stats.brier:>15.4f}{cal_stats.brier:>18.4f}")
    print(f"\nFolds where calibration improved P&L:    {folds_pnl_improved}/{len(folds)}")
    print(f"Folds where calibration improved Brier:  {folds_brier_improved}/{len(folds)}")
    print()

    print("Raw model calibration (all OOS folds combined):")
    print(calibration_report(all_raw))
    print()
    print(f"[{args.method}]-calibrated (all OOS folds combined):")
    print(calibration_report(all_cal))
    print()

    pnl_delta = cal_stats.total_pnl - raw_stats.total_pnl
    brier_delta = cal_stats.brier - raw_stats.brier
    consistent = folds_brier_improved >= len(folds) * 0.6
    verdict = "DURABLE FIX" if (brier_delta < 0 and consistent) else ("MIXED / REGIME-DEPENDENT" if pnl_delta > 0 or brier_delta < 0 else "NO IMPROVEMENT")
    print(
        f"Verdict: {verdict}  (aggregate P&L {pnl_delta:+.2f}, aggregate Brier {brier_delta:+.4f}, "
        f"Brier improved in {folds_brier_improved}/{len(folds)} individual folds)"
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"calibration_fix_{stamp}.csv"
    with out_path.open("w", newline="") as f_out:
        writer = csv.writer(f_out)
        writer.writerow(["fold", "test_period_start", "test_period_end", "train_n", "fit", "raw_pnl", "cal_pnl", "raw_brier", "cal_brier"])
        for f in folds:
            raw_s, cal_s = fold_stats(f.test_trades), fold_stats(f.calibrated_test_trades)
            writer.writerow([
                f.index, f.test_trades[0].decision_time.isoformat(), f.test_trades[-1].decision_time.isoformat(),
                f.train_n, f.label, round(raw_s.total_pnl, 2), round(cal_s.total_pnl, 2),
                round(raw_s.brier, 4), round(cal_s.brier, 4),
            ])
        writer.writerow(["aggregate", "", "", "", "", round(raw_stats.total_pnl, 2), round(cal_stats.total_pnl, 2), round(raw_stats.brier, 4), round(cal_stats.brier, 4)])
    print(f"\nSaved: {out_path.relative_to(Path(__file__).resolve().parent.parent)}")


if __name__ == "__main__":
    main()
