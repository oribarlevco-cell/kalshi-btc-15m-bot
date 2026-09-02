from __future__ import annotations

import sqlite3

import pytest

from src.report_calibration import brier_score, bucketize, directional_accuracy, fetch_rows


def test_bucketize_groups_by_predicted_probability():
    rows = [(0.65, 1), (0.68, 1), (0.72, 0), (0.15, 0), (0.18, 0)]

    buckets = bucketize(rows, n_bins=10)

    bucket_60_70 = next(b for b in buckets if b.lo == pytest.approx(0.6))
    assert bucket_60_70.count == 2
    assert bucket_60_70.actual_yes_rate == 1.0

    bucket_10_20 = next(b for b in buckets if b.lo == pytest.approx(0.1))
    assert bucket_10_20.count == 2
    assert bucket_10_20.actual_yes_rate == 0.0


def test_bucketize_includes_probability_of_exactly_one():
    rows = [(1.0, 1)]

    buckets = bucketize(rows, n_bins=10)

    assert len(buckets) == 1
    assert buckets[0].count == 1


def test_bucketize_skips_empty_buckets():
    rows = [(0.05, 0)]

    buckets = bucketize(rows, n_bins=10)

    assert len(buckets) == 1


def test_brier_score_perfect_predictions_is_zero():
    rows = [(1.0, 1), (0.0, 0), (1.0, 1)]

    assert brier_score(rows) == 0.0


def test_brier_score_coinflip_is_quarter():
    rows = [(0.5, 1), (0.5, 0)]

    assert brier_score(rows) == pytest.approx(0.25)


def test_brier_score_worst_case_predictions_is_one():
    rows = [(1.0, 0), (0.0, 1)]

    assert brier_score(rows) == 1.0


def test_directional_accuracy():
    # >=0.5 counts as a YES call
    rows = [(0.7, 1), (0.6, 0), (0.3, 0), (0.4, 1)]

    # correct: (0.7,1) yes-called-yes=correct; (0.6,0) yes-called-no=wrong;
    # (0.3,0) no-called-no=correct; (0.4,1) no-called-yes=wrong -> 2/4
    assert directional_accuracy(rows) == 0.5


def test_fetch_rows_reads_market_lifecycle(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE market_lifecycle (
            ticker TEXT PRIMARY KEY,
            initial_probability_yes REAL,
            last_probability_yes REAL,
            actual_result TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO market_lifecycle (ticker, initial_probability_yes, last_probability_yes, actual_result) "
        "VALUES (?, ?, ?, ?)",
        [
            ("A", 0.6, 0.7, "yes"),
            ("B", 0.4, None, "no"),  # no last_probability_yes -- excluded from "last"
            ("C", None, 0.3, "no"),  # no initial_probability_yes -- excluded from "initial"
            ("D", 0.5, 0.5, None),  # not settled yet -- excluded from both
        ],
    )
    conn.commit()
    conn.close()

    initial_rows = fetch_rows(db_path, "initial")
    last_rows = fetch_rows(db_path, "last")

    assert initial_rows == [(0.6, 1), (0.4, 0)]
    assert last_rows == [(0.7, 1), (0.3, 0)]
