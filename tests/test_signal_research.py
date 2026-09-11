from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.backtest import Direction, MarketOutcome
from src.signal_research import (
    CANDIDATE_SIGNALS,
    _classify_verdict,
    _fold_brier_errors,
    _liquidity_check,
    _win_rate_train_by_direction,
    build_folds,
    fetch_findings,
    fetch_run,
    print_report,
    run_signal_research,
)

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)


def _outcome(
    ticker="T",
    actual_result="yes",
    days_ago=0,
    hour=0,
    signal_direction: Direction | None = "yes",
    initial_probability_yes=0.5,
    opening_volume=50.0,
) -> MarketOutcome:
    close_time = NOW - timedelta(days=days_ago) + timedelta(hours=hour)
    return MarketOutcome(
        ticker=ticker,
        actual_result=actual_result,
        initial_probability_yes=initial_probability_yes,
        opening_yes_bid=0.5,
        opening_yes_ask=0.55,
        opening_no_bid=0.45,
        opening_no_ask=0.5,
        opening_momentum_pct=0.001,
        trend_state="bull" if signal_direction == "yes" else ("bear" if signal_direction == "no" else None),
        divergence_direction=signal_direction,
        observed=True,
        close_time_utc=close_time.isoformat(),
        opening_volume=opening_volume,
        opening_yes_depth_total=80.0,
        opening_no_depth_total=20.0,
    )


def _trend_direction(outcome: MarketOutcome) -> Direction | None:
    if outcome.trend_state == "bull":
        return "yes"
    if outcome.trend_state == "bear":
        return "no"
    return None


# ---------- build_folds ----------


def test_build_folds_empty_when_fewer_than_min_train_plus_one_days():
    outcomes = [_outcome(ticker=f"T{i}", days_ago=d) for d in range(3) for i in range(5)]  # only 3 days
    assert build_folds(outcomes, min_train_days=4, fold_size_days=1) == []


def test_build_folds_train_never_includes_current_or_future_days():
    outcomes = [_outcome(ticker=f"T{d}-{i}", days_ago=d) for d in range(7) for i in range(5)]
    folds = build_folds(outcomes, min_train_days=4, fold_size_days=1)
    assert len(folds) >= 1

    for f in folds:
        test_days = {NOW.date() - timedelta(days=int(t.ticker.split("-")[0][1:])) for t in f.test}
        train_days = {NOW.date() - timedelta(days=int(t.ticker.split("-")[0][1:])) for t in f.train}
        # every train day must be strictly earlier than every test day in this fold
        assert max(train_days, default=None) is None or max(train_days) < min(test_days)


def test_build_folds_one_fold_per_calendar_day_after_min_train():
    # 4 train days + 3 more days of data -> 3 folds of 1 day each
    outcomes = [_outcome(ticker=f"T{d}-{i}", days_ago=d) for d in range(7) for i in range(5)]
    folds = build_folds(outcomes, min_train_days=4, fold_size_days=1)
    assert len(folds) == 3
    for f in folds:
        assert len(f.test) == 5  # each day has exactly 5 outcomes


def test_build_folds_parses_both_z_and_offset_suffixed_close_times():
    offset_style = MarketOutcome(
        ticker="A",
        actual_result="yes",
        initial_probability_yes=0.5,
        opening_yes_bid=0.5,
        opening_yes_ask=0.55,
        opening_no_bid=0.45,
        opening_no_ask=0.5,
        opening_momentum_pct=0.0,
        close_time_utc="2026-09-01T00:00:00+00:00",
    )
    z_style = MarketOutcome(
        ticker="B",
        actual_result="yes",
        initial_probability_yes=0.5,
        opening_yes_bid=0.5,
        opening_yes_ask=0.55,
        opening_no_bid=0.45,
        opening_no_ask=0.5,
        opening_momentum_pct=0.0,
        close_time_utc="2026-09-02T00:00:00Z",
    )
    # Both should parse without error and be usable in fold-building (no crash, no silent drop).
    folds = build_folds([offset_style, z_style], min_train_days=0, fold_size_days=10)
    all_seen = {o.ticker for f in folds for o in f.train} | {o.ticker for f in folds for o in f.test}
    assert all_seen == {"A", "B"}


def test_build_folds_sorts_chronologically_regardless_of_input_order():
    # 7 contiguous days of data (days_ago 6..0), each day non-empty -- real
    # settled-market history has no gaps, unlike an arbitrary synthetic one.
    chronological = [_outcome(ticker=f"T{d}", days_ago=d) for d in range(6, -1, -1)]
    shuffled = [chronological[3], chronological[0], chronological[6], chronological[1], chronological[5]]
    shuffled += [chronological[2], chronological[4]]

    folds_sorted_input = build_folds(chronological, min_train_days=4, fold_size_days=1)
    folds_shuffled_input = build_folds(shuffled, min_train_days=4, fold_size_days=1)

    assert [f.test[0].ticker for f in folds_shuffled_input] == [f.test[0].ticker for f in folds_sorted_input]
    assert [{o.ticker for o in f.train} for f in folds_shuffled_input] == [
        {o.ticker for o in f.train} for f in folds_sorted_input
    ]
    assert [f.test[0].ticker for f in folds_sorted_input] == ["T2", "T1", "T0"]


def test_build_folds_skips_gap_days_instead_of_stopping_at_first_one():
    # days_ago 6..4 (train) then a gap at 3/2 (e.g. an outage) then 1, 0.
    outcomes = [_outcome(ticker=f"T{d}", days_ago=d) for d in (6, 5, 4, 1, 0)]
    folds = build_folds(outcomes, min_train_days=4, fold_size_days=1)
    # Without gap-tolerance this would find 0 folds (stopping at the empty
    # day-3 fold); it should instead skip the gap and still find day 1 and day 0.
    assert [f.test[0].ticker for f in folds] == ["T1", "T0"]


# ---------- _win_rate_train_by_direction ----------


def test_win_rate_train_by_direction_splits_yes_and_no_calls():
    train = (
        [_outcome(ticker=f"Y{i}", signal_direction="yes", actual_result="yes") for i in range(8)]
        + [_outcome(ticker=f"Y{i}-lose", signal_direction="yes", actual_result="no") for i in range(2)]
        + [_outcome(ticker=f"N{i}", signal_direction="no", actual_result="no") for i in range(5)]
        + [_outcome(ticker=f"N{i}-lose", signal_direction="no", actual_result="yes") for i in range(5)]
    )
    win_rate_yes, win_rate_no = _win_rate_train_by_direction(lambda o: o.divergence_direction, train)
    assert win_rate_yes == pytest.approx(0.8)
    assert win_rate_no == pytest.approx(0.5)


def test_win_rate_train_by_direction_none_below_min_firings():
    train = [_outcome(ticker=f"Y{i}", signal_direction="yes", actual_result="yes") for i in range(3)]  # < 5
    win_rate_yes, win_rate_no = _win_rate_train_by_direction(lambda o: o.divergence_direction, train)
    assert win_rate_yes is None
    assert win_rate_no is None


# ---------- _fold_brier_errors ----------


def test_score_fold_brier_naive_rule_matches_hand_computed_example():
    train = [_outcome(ticker=f"Y{i}", signal_direction="yes", actual_result="yes") for i in range(8)] + [
        _outcome(ticker=f"Y{i}-lose", signal_direction="yes", actual_result="no") for i in range(2)
    ]  # win_rate_yes = 0.8
    test = [_outcome(ticker="TEST1", signal_direction="yes", actual_result="yes", initial_probability_yes=0.6)]

    signal_errors, baseline_errors = _fold_brier_errors(lambda o: o.divergence_direction, train, test)

    assert signal_errors == pytest.approx([(0.8 - 1.0) ** 2])  # p=0.8, actual=1 -> 0.04
    assert baseline_errors == pytest.approx([(0.6 - 1.0) ** 2])  # baseline p=0.6, actual=1 -> 0.16


def test_score_fold_brier_excludes_rows_missing_baseline_probability():
    train = [_outcome(ticker=f"Y{i}", signal_direction="yes", actual_result="yes") for i in range(8)]
    test = [_outcome(ticker="TEST1", signal_direction="yes", initial_probability_yes=None)]

    signal_errors, baseline_errors = _fold_brier_errors(lambda o: o.divergence_direction, train, test)
    assert signal_errors == []
    assert baseline_errors == []


def test_score_fold_brier_none_when_zero_test_firings():
    train = [_outcome(ticker=f"Y{i}", signal_direction="yes", actual_result="yes") for i in range(8)]
    test = [_outcome(ticker="TEST1", signal_direction=None)]

    signal_errors, baseline_errors = _fold_brier_errors(lambda o: o.divergence_direction, train, test)
    assert signal_errors == []
    assert baseline_errors == []


# ---------- _liquidity_check ----------


def test_liquidity_check_fails_when_edge_only_in_low_liquidity_bucket():
    low = [_outcome(ticker=f"L{i}", opening_volume=2.0, actual_result="yes") for i in range(27)] + [
        _outcome(ticker=f"L{i}-lose", opening_volume=2.0, actual_result="no") for i in range(3)
    ]  # thin bucket ~90% win rate
    high = [_outcome(ticker=f"H{i}", opening_volume=50.0, actual_result="yes") for i in range(13)] + [
        _outcome(ticker=f"H{i}-lose", opening_volume=50.0, actual_result="no") for i in range(12)
    ]  # liquid bucket ~52% win rate
    passed, low_n, low_wr, high_n, high_wr = _liquidity_check(lambda o: o.divergence_direction, low + high, 10.0)
    assert passed is False
    assert low_n == 30
    assert high_n == 25


def test_liquidity_check_passes_when_edge_holds_in_high_liquidity_bucket_too():
    low = [_outcome(ticker=f"L{i}", opening_volume=2.0, actual_result="yes") for i in range(20)] + [
        _outcome(ticker=f"L{i}-lose", opening_volume=2.0, actual_result="no") for i in range(5)
    ]
    high = [_outcome(ticker=f"H{i}", opening_volume=50.0, actual_result="yes") for i in range(20)] + [
        _outcome(ticker=f"H{i}-lose", opening_volume=50.0, actual_result="no") for i in range(5)
    ]  # edge holds here too
    passed, *_ = _liquidity_check(lambda o: o.divergence_direction, low + high, 10.0)
    assert passed is True


def test_liquidity_check_none_when_high_liquidity_bucket_too_small():
    low = [_outcome(ticker=f"L{i}", opening_volume=2.0, actual_result="yes") for i in range(25)]
    high = [_outcome(ticker=f"H{i}", opening_volume=50.0, actual_result="yes") for i in range(5)]  # n=5 < 20
    passed, *_ = _liquidity_check(lambda o: o.divergence_direction, low + high, 10.0)
    assert passed is None


# ---------- _classify_verdict ----------


def test_verdict_pass_requires_every_gate_simultaneously():
    verdict, _ = _classify_verdict(
        n=60,
        ci_low=0.60,
        ci_high=0.80,
        eligible_fold_count=5,
        fold_consistency_rate=0.8,
        liquidity_check_passed=True,
        brier_delta=0.03,
    )
    assert verdict == "PASS"

    # Drop just the liquidity check -- should no longer PASS.
    verdict, reason = _classify_verdict(
        n=60,
        ci_low=0.60,
        ci_high=0.80,
        eligible_fold_count=5,
        fold_consistency_rate=0.8,
        liquidity_check_passed=False,
        brier_delta=0.03,
    )
    assert verdict == "FAIL"
    assert "liquidity" in reason


def test_verdict_fail_when_ci_upper_at_or_below_coinflip():
    verdict, reason = _classify_verdict(
        n=40,
        ci_low=0.30,
        ci_high=0.50,
        eligible_fold_count=5,
        fold_consistency_rate=0.5,
        liquidity_check_passed=True,
        brier_delta=None,
    )
    assert verdict == "FAIL"
    assert "coinflip" in reason


def test_verdict_fail_when_liquidity_check_fails_even_if_ci_looks_good():
    verdict, reason = _classify_verdict(
        n=60,
        ci_low=0.65,
        ci_high=0.85,
        eligible_fold_count=5,
        fold_consistency_rate=0.8,
        liquidity_check_passed=False,
        brier_delta=0.05,
    )
    assert verdict == "FAIL"
    assert "thin-liquidity" in reason


def test_verdict_fail_when_fold_consistency_rate_below_0_4():
    verdict, reason = _classify_verdict(
        n=60,
        ci_low=0.55,
        ci_high=0.75,
        eligible_fold_count=5,
        fold_consistency_rate=0.2,
        liquidity_check_passed=True,
        brier_delta=0.01,
    )
    assert verdict == "FAIL"
    assert "regime-dependent" in reason


def test_verdict_inconclusive_when_fewer_than_min_folds_for_verdict():
    verdict, reason = _classify_verdict(
        n=60,
        ci_low=0.65,
        ci_high=0.85,
        eligible_fold_count=2,
        fold_consistency_rate=1.0,
        liquidity_check_passed=True,
        brier_delta=0.05,
    )
    assert verdict == "INCONCLUSIVE"
    assert "fold" in reason


def test_verdict_inconclusive_when_n_below_20():
    verdict, reason = _classify_verdict(
        n=10,
        ci_low=0.65,
        ci_high=0.85,
        eligible_fold_count=5,
        fold_consistency_rate=1.0,
        liquidity_check_passed=True,
        brier_delta=0.05,
    )
    assert verdict == "INCONCLUSIVE"
    assert "n=10" in reason


# ---------- run_signal_research (end-to-end) ----------


def _many_outcomes_across_days(num_days: int, per_day: int) -> list[MarketOutcome]:
    outcomes = []
    for d in range(num_days):
        for i in range(per_day):
            outcomes.append(
                _outcome(
                    ticker=f"T{d}-{i}",
                    days_ago=num_days - 1 - d,
                    hour=i,
                    actual_result="yes" if (d + i) % 2 == 0 else "no",
                )
            )
    return outcomes


def test_run_signal_research_covers_all_seven_registered_signals(tmp_path):
    from src.storage import Storage

    db_path = str(tmp_path / "test.db")
    Storage(db_path).close()
    outcomes = _many_outcomes_across_days(num_days=10, per_day=10)
    settings = _settings(db_path)

    _run_id, findings = run_signal_research(db_path, settings, outcomes=outcomes)

    assert {f.signal_name for f in findings} == {name for name, _ in CANDIDATE_SIGNALS}


def test_run_signal_research_persists_one_run_and_one_finding_per_signal(tmp_path):
    from src.storage import Storage

    db_path = str(tmp_path / "test.db")
    Storage(db_path).close()
    outcomes = _many_outcomes_across_days(num_days=10, per_day=10)
    settings = _settings(db_path)

    run_id, findings = run_signal_research(db_path, settings, outcomes=outcomes)

    run_row = fetch_run(db_path, run_id)
    assert run_row is not None
    assert run_row[0] == run_id
    assert run_row[2] == len(outcomes)  # total_outcomes_n

    finding_rows = fetch_findings(db_path, run_id)
    assert len(finding_rows) == len(findings) == len(CANDIDATE_SIGNALS)


def test_run_signal_research_handles_insufficient_history_gracefully(tmp_path):
    from src.storage import Storage

    db_path = str(tmp_path / "test.db")
    Storage(db_path).close()
    outcomes = _many_outcomes_across_days(num_days=2, per_day=5)  # far below MIN_TRAIN_DAYS
    settings = _settings(db_path)

    run_id, findings = run_signal_research(db_path, settings, outcomes=outcomes)

    assert all(f.verdict == "INCONCLUSIVE" for f in findings)
    run_row = fetch_run(db_path, run_id)
    assert run_row is not None
    assert "insufficient history" in (run_row[6] or "")


def test_print_report_smoke(tmp_path, capsys):
    from src.storage import Storage

    db_path = str(tmp_path / "test.db")
    Storage(db_path).close()
    outcomes = _many_outcomes_across_days(num_days=10, per_day=10)
    settings = _settings(db_path)
    run_id, _findings = run_signal_research(db_path, settings, outcomes=outcomes)

    run_row = fetch_run(db_path, run_id)
    finding_rows = fetch_findings(db_path, run_id)
    print_report(run_row, finding_rows)

    out = capsys.readouterr().out
    assert f"run #{run_id}" in out
    assert "divergence" in out


def _settings(db_path: str):
    from tests.conftest import make_settings

    return make_settings(db_path=db_path, divergence_min_volume=10.0)
