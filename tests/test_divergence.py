from __future__ import annotations

from src.divergence import check_divergence


def test_no_divergence_when_market_agrees_with_spot():
    # spot above strike (implies yes), market also confidently favors yes
    result = check_divergence(btc_price=51000, floor_strike=50000, yes_bid=0.70)
    assert result.is_diverging is False
    assert result.spot_direction is None
    assert result.market_direction is None


def test_diverges_when_spot_up_but_market_confidently_down():
    result = check_divergence(btc_price=51000, floor_strike=50000, yes_bid=0.20)
    assert result.is_diverging is True
    assert result.spot_direction == "yes"
    assert result.market_direction == "no"


def test_diverges_when_spot_down_but_market_confidently_up():
    result = check_divergence(btc_price=49000, floor_strike=50000, yes_bid=0.80)
    assert result.is_diverging is True
    assert result.spot_direction == "no"
    assert result.market_direction == "yes"


def test_no_divergence_when_market_not_confident():
    # spot disagrees with market's lean, but market isn't confident (yes_bid in [0.35, 0.65])
    result = check_divergence(btc_price=51000, floor_strike=50000, yes_bid=0.45)
    assert result.is_diverging is False


def test_boundary_exactly_at_threshold_is_not_confident():
    result = check_divergence(btc_price=51000, floor_strike=50000, yes_bid=0.35, confident_threshold=0.65)
    assert result.is_diverging is False  # 0.35 is not < 0.35


def test_custom_confident_threshold():
    # yes_bid=0.55 isn't confident at the default 0.65 threshold...
    assert check_divergence(51000, 50000, yes_bid=0.20, confident_threshold=0.65).is_diverging is True
    # ...but at a looser threshold, a milder lean still counts as confident
    result = check_divergence(btc_price=51000, floor_strike=50000, yes_bid=0.40, confident_threshold=0.55)
    assert result.is_diverging is True


def test_no_signal_when_inputs_missing():
    assert check_divergence(None, 50000, 0.7).is_diverging is False
    assert check_divergence(51000, None, 0.7).is_diverging is False
    assert check_divergence(51000, 50000, None).is_diverging is False


def test_min_volume_defaults_to_no_filtering():
    # min_volume=0.0 (the default) skips the liquidity check entirely, even
    # with volume=None -- existing callers that never pass volume are unaffected.
    result = check_divergence(51000, 50000, 0.20)
    assert result.is_diverging is True


def test_min_volume_blocks_zero_liquidity_divergence():
    # A fresh market's yes_bid=0.01 with zero volume is an unpriced
    # placeholder, not a confident (and wrong) market -- min_volume filters it.
    result = check_divergence(51000, 50000, 0.01, volume=0.0, min_volume=10.0)
    assert result.is_diverging is False


def test_min_volume_allows_genuinely_traded_divergence():
    result = check_divergence(51000, 50000, 0.20, volume=25.0, min_volume=10.0)
    assert result.is_diverging is True
    assert result.spot_direction == "yes"
    assert result.market_direction == "no"


def test_min_volume_boundary_is_inclusive():
    result = check_divergence(51000, 50000, 0.20, volume=10.0, min_volume=10.0)
    assert result.is_diverging is True  # 10.0 is not < 10.0


def test_min_volume_treats_missing_volume_as_zero():
    result = check_divergence(51000, 50000, 0.20, volume=None, min_volume=10.0)
    assert result.is_diverging is False
