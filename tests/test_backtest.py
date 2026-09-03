from __future__ import annotations

import sqlite3

import pytest

from src.backtest import (
    MarketOutcome,
    direction_for_strategy,
    entry_price_for_direction,
    fetch_market_outcomes,
    pnl_for_trade,
    run_backtest_for_strategy,
    run_backtests,
    wilson_interval,
)


def test_wilson_interval_zero_n_is_maximally_uncertain():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_narrows_with_more_data_at_same_rate():
    lo_small, hi_small = wilson_interval(7, 10)
    lo_large, hi_large = wilson_interval(70, 100)

    assert (hi_large - lo_large) < (hi_small - lo_small)


def test_wilson_interval_is_centered_around_observed_rate():
    lo, hi = wilson_interval(50, 100)
    assert lo < 0.5 < hi


def _outcome(
    ticker="T",
    actual_result="yes",
    initial_probability_yes=0.7,
    opening_yes_bid=0.55,
    opening_yes_ask=0.60,
    opening_no_bid=0.38,
    opening_no_ask=0.45,
    opening_momentum_pct=0.01,
) -> MarketOutcome:
    return MarketOutcome(
        ticker=ticker,
        actual_result=actual_result,
        initial_probability_yes=initial_probability_yes,
        opening_yes_bid=opening_yes_bid,
        opening_yes_ask=opening_yes_ask,
        opening_no_bid=opening_no_bid,
        opening_no_ask=opening_no_ask,
        opening_momentum_pct=opening_momentum_pct,
    )


def test_model_direction_from_initial_probability():
    assert direction_for_strategy("model", _outcome(initial_probability_yes=0.7)) == "yes"
    assert direction_for_strategy("model", _outcome(initial_probability_yes=0.3)) == "no"
    assert direction_for_strategy("model", _outcome(initial_probability_yes=None)) is None


def test_favorite_direction_from_opening_midpoint():
    outcome = _outcome(opening_yes_bid=0.55, opening_yes_ask=0.65)  # midpoint 0.6
    assert direction_for_strategy("favorite", outcome) == "yes"

    outcome_no = _outcome(opening_yes_bid=0.10, opening_yes_ask=0.20)  # midpoint 0.15
    assert direction_for_strategy("favorite", outcome_no) == "no"

    outcome_missing = _outcome(opening_yes_bid=None)
    assert direction_for_strategy("favorite", outcome_missing) is None


def test_momentum_direction_from_opening_momentum():
    assert direction_for_strategy("momentum", _outcome(opening_momentum_pct=0.02)) == "yes"
    assert direction_for_strategy("momentum", _outcome(opening_momentum_pct=-0.02)) == "no"
    assert direction_for_strategy("momentum", _outcome(opening_momentum_pct=None)) is None
    assert direction_for_strategy("momentum", _outcome(opening_momentum_pct=0.0)) is None


def test_agreement_only_fires_when_model_and_momentum_match():
    agree = _outcome(initial_probability_yes=0.7, opening_momentum_pct=0.01)  # both yes
    assert direction_for_strategy("agreement", agree) == "yes"

    disagree = _outcome(initial_probability_yes=0.7, opening_momentum_pct=-0.01)  # yes vs no
    assert direction_for_strategy("agreement", disagree) is None

    missing_momentum = _outcome(initial_probability_yes=0.7, opening_momentum_pct=None)
    assert direction_for_strategy("agreement", missing_momentum) is None


def test_entry_price_uses_yes_ask_for_yes_and_no_ask_for_no():
    outcome = _outcome(opening_yes_ask=0.6, opening_no_ask=0.45)
    assert entry_price_for_direction(outcome, "yes") == 0.6
    assert entry_price_for_direction(outcome, "no") == 0.45


def test_entry_price_none_when_price_missing_or_zero():
    assert entry_price_for_direction(_outcome(opening_yes_ask=None), "yes") is None
    assert entry_price_for_direction(_outcome(opening_yes_ask=0.0), "yes") is None


def test_pnl_win_and_loss():
    assert pnl_for_trade("yes", 0.6, "yes") == pytest.approx(0.4)
    assert pnl_for_trade("yes", 0.6, "no") == pytest.approx(-0.6)


def test_run_backtest_for_strategy_aggregates_wins_and_pnl():
    outcomes = [
        _outcome(ticker="A", actual_result="yes", initial_probability_yes=0.7),  # model calls yes, wins
        _outcome(ticker="B", actual_result="no", initial_probability_yes=0.7),  # model calls yes, loses
        _outcome(ticker="C", actual_result="no", initial_probability_yes=0.2),  # model calls no, wins
    ]

    result = run_backtest_for_strategy("model", outcomes)

    assert result.n == 3
    assert result.wins == 2
    assert result.win_rate == pytest.approx(2 / 3)
    assert result.low_confidence is True  # n=3 < 20
    assert result.ci_low <= result.win_rate <= result.ci_high


def test_low_confidence_flag_threshold():
    many_outcomes = [_outcome(ticker=f"T{i}", initial_probability_yes=0.9, actual_result="yes") for i in range(25)]

    result = run_backtest_for_strategy("model", many_outcomes)

    assert result.n == 25
    assert result.low_confidence is False


def test_skips_market_with_no_signal_or_no_valid_price():
    outcomes = [
        _outcome(ticker="A", initial_probability_yes=None),  # no model signal
        _outcome(ticker="B", initial_probability_yes=0.7, opening_yes_ask=None),  # no price
        _outcome(ticker="C", initial_probability_yes=0.7, opening_yes_ask=0.6, actual_result="yes"),
    ]

    result = run_backtest_for_strategy("model", outcomes)

    assert result.n == 1


def test_run_backtests_returns_all_four_strategies():
    outcomes = [_outcome()]
    results = run_backtests(outcomes=outcomes)
    assert {r.name for r in results} == {"model", "favorite", "momentum", "agreement"}


def _seed_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE market_lifecycle (ticker TEXT PRIMARY KEY, actual_result TEXT, initial_probability_yes REAL)"
    )
    conn.execute(
        "CREATE TABLE snapshots (ticker TEXT, pulled_at_utc TEXT, yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL)"
    )
    conn.execute("CREATE TABLE predictions (ticker TEXT, computed_at_utc TEXT, momentum_pct REAL)")

    conn.execute(
        "INSERT INTO market_lifecycle (ticker, actual_result, initial_probability_yes) VALUES (?, ?, ?)",
        ("T1", "yes", 0.7),
    )
    conn.execute(
        "INSERT INTO snapshots (ticker, pulled_at_utc, yes_bid, yes_ask, no_bid, no_ask) VALUES (?, ?, ?, ?, ?, ?)",
        ("T1", "2026-01-01T00:00:00+00:00", 0.55, 0.60, 0.38, 0.45),
    )
    conn.execute(
        "INSERT INTO snapshots (ticker, pulled_at_utc, yes_bid, yes_ask, no_bid, no_ask) VALUES (?, ?, ?, ?, ?, ?)",
        ("T1", "2026-01-01T00:00:20+00:00", 0.60, 0.65, 0.33, 0.40),  # later -- should NOT be picked as opening
    )
    conn.execute(
        "INSERT INTO predictions (ticker, computed_at_utc, momentum_pct) VALUES (?, ?, ?)",
        ("T1", "2026-01-01T00:00:00+00:00", 0.02),
    )
    conn.commit()
    conn.close()
    return db_path


def test_fetch_market_outcomes_uses_earliest_snapshot_and_prediction(tmp_path):
    db_path = _seed_db(tmp_path)

    outcomes = fetch_market_outcomes(db_path)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.ticker == "T1"
    assert outcome.opening_yes_bid == 0.55  # the earlier snapshot, not the later one
    assert outcome.opening_yes_ask == 0.60
    assert outcome.opening_momentum_pct == 0.02


def test_fetch_market_outcomes_excludes_unsettled(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE market_lifecycle (ticker TEXT PRIMARY KEY, actual_result TEXT, initial_probability_yes REAL)"
    )
    conn.execute(
        "CREATE TABLE snapshots (ticker TEXT, pulled_at_utc TEXT, yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL)"
    )
    conn.execute("CREATE TABLE predictions (ticker TEXT, computed_at_utc TEXT, momentum_pct REAL)")
    conn.execute(
        "INSERT INTO market_lifecycle (ticker, actual_result, initial_probability_yes) VALUES (?, NULL, ?)",
        ("UNSETTLED", 0.6),
    )
    conn.commit()
    conn.close()

    assert fetch_market_outcomes(db_path) == []
