from engine.market_regime import (
    MarketRegime,
    futures_stop_tolerance_pct,
    is_risky_regime,
    is_trending_regime,
    normalize_regime,
    regime_aligns,
    uses_tight_trailing,
)


def test_legacy_regime_aliases_normalize_to_canonical_states():
    assert normalize_regime("CHOPPY") == MarketRegime.RANGING
    assert normalize_regime("SIDEWAYS") == MarketRegime.RANGING
    assert normalize_regime("BREAKOUT", direction="DOWN") == MarketRegime.BREAKOUT_DOWN
    assert normalize_regime("STRONG_TREND", direction="DOWN") == MarketRegime.TRENDING_DOWN


def test_regime_traits_cover_detector_outputs():
    assert is_trending_regime("TRENDING_UP")
    assert is_trending_regime("TRENDING_DOWN")
    assert is_trending_regime("BREAKOUT_UP")
    assert is_risky_regime("RANGING")
    assert is_risky_regime("VOLATILE")
    assert uses_tight_trailing("RANGING")
    assert not uses_tight_trailing("TRENDING_DOWN")


def test_regime_direction_alignment_is_explicit():
    assert regime_aligns("TRENDING_UP", "BUY")
    assert regime_aligns("BREAKOUT_DOWN", "SELL")
    assert not regime_aligns("BREAKOUT_DOWN", "BUY")


def test_futures_stop_tolerance_is_regime_based_under_configured_cap():
    assert futures_stop_tolerance_pct(0.15, "RANGING") == 0.10
    assert futures_stop_tolerance_pct(0.15, "CHOPPY") == 0.10
    assert futures_stop_tolerance_pct(0.15, "UNKNOWN") == 0.10
    assert futures_stop_tolerance_pct(0.15, "VOLATILE") == 0.15
    assert futures_stop_tolerance_pct(0.15, "TRENDING_UP") == 0.15
    assert futures_stop_tolerance_pct(0.15, "BREAKOUT_DOWN") == 0.15


def test_lower_configured_futures_stop_cap_wins_in_every_regime():
    for regime in (
        "RANGING",
        "UNKNOWN",
        "VOLATILE",
        "TRENDING_UP",
        "BREAKOUT_DOWN",
    ):
        assert futures_stop_tolerance_pct(0.02, regime) == 0.02
