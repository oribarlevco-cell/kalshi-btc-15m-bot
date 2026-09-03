from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from config.settings import Settings
from src.backtest import StrategyResult, pattern_log_stats, run_backtests
from src.report_calibration import brier_score, bucketize, directional_accuracy, fetch_rows

RECENT_SETTLED_LIMIT = 10
CALIBRATION_TREND_LIMIT = 20


def _row_to_calibration_point(row: tuple) -> dict[str, Any]:
    computed_at_utc, n, brier, accuracy = row
    return {"computed_at_utc": computed_at_utc, "n": n, "brier_score": brier, "directional_accuracy": accuracy}


def strategy_result_to_dict(r: StrategyResult) -> dict[str, Any]:
    return {
        "name": r.name,
        "n": r.n,
        "wins": r.wins,
        "win_rate": r.win_rate,
        "ci_low": r.ci_low,
        "ci_high": r.ci_high,
        "avg_pnl": r.avg_pnl,
        "low_confidence": r.low_confidence,
    }


def build_recent_settled(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ticker, actual_result, closed_logged_at_utc, opened_at_utc FROM market_lifecycle "
        "WHERE actual_result IS NOT NULL ORDER BY closed_logged_at_utc DESC LIMIT ?",
        (RECENT_SETTLED_LIMIT,),
    ).fetchall()
    return [
        {
            "ticker": r[0],
            "result": r[1],
            "closed_at_utc": r[2],
            "data_quality": "observed" if r[3] is not None else "backfill",
        }
        for r in rows
    ]


def build_calibration(db_path: str) -> dict[str, Any]:
    rows_last = fetch_rows(db_path, "last")
    rows_initial = fetch_rows(db_path, "initial")

    def summarize(rows):
        if not rows:
            return {"n": 0, "buckets": [], "brier_score": None, "directional_accuracy": None}

        buckets = bucketize(rows, n_bins=5)
        return {
            "n": len(rows),
            "buckets": [
                {
                    "lo": b.lo,
                    "hi": b.hi,
                    "count": b.count,
                    "mean_predicted": b.mean_predicted,
                    "actual_yes_rate": b.actual_yes_rate,
                }
                for b in buckets
            ],
            "brier_score": brier_score(rows),
            "directional_accuracy": directional_accuracy(rows),
        }

    return {"initial": summarize(rows_initial), "last": summarize(rows_last)}


def build_analytics_payload(settings: Settings) -> dict[str, Any]:
    """Everything the dashboard shows that's historical/aggregate rather than
    "right now" -- recent settled windows, strategy backtests, the pattern
    log, and calibration (current + trend). Deliberately excludes the live
    tile (see live_server.build_live_tile), since this payload is meant to
    be cheap enough to compute every ~10 min from a plain script and commit
    to docs/analytics.json, not just served from a running process."""
    conn = sqlite3.connect(settings.db_path)
    try:
        recent_settled = build_recent_settled(conn)
        backtests = [strategy_result_to_dict(r) for r in run_backtests(settings.db_path)]
        pattern_log = pattern_log_stats(settings.db_path)
        calibration = build_calibration(settings.db_path)
        trend_last = [
            _row_to_calibration_point(r)
            for r in conn.execute(
                "SELECT computed_at_utc, n, brier_score, directional_accuracy FROM calibration_snapshots "
                "WHERE which = 'last' ORDER BY computed_at_utc DESC LIMIT ?",
                (CALIBRATION_TREND_LIMIT,),
            ).fetchall()
        ]
    finally:
        conn.close()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recent_settled": recent_settled,
        "backtests": backtests,
        "pattern_log": {
            "total_settled": pattern_log.total_settled,
            "up_count": pattern_log.up_count,
            "down_count": pattern_log.down_count,
            "observed_count": pattern_log.observed_count,
            "backfill_count": pattern_log.backfill_count,
            "trend": strategy_result_to_dict(pattern_log.trend_stats),
            "divergence": strategy_result_to_dict(pattern_log.divergence_stats),
        },
        "calibration": calibration,
        "calibration_trend": list(reversed(trend_last)),
    }
