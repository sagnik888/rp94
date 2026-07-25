"""ADX-strength escape for the range-day trend guard (2026-07-24 audit).

Root cause of the 2026-07-24 session: strong trends (raw regime TRENDING/BREAKOUT,
ADX 44-48) were demoted to RANGING because the day-type classifier labelled the day a
RANGE_DAY, and the demotion override (`directional_proof`) ignored ADX entirely. The
RANGING relabel then cascaded into choppy gating (mass signal rejection), scalp-lock
early exits, and dormant trend re-entry — so the system missed the big moves.

`_range_day_guarded_regime` now skips demotion when ADX is above
`regime_range_day_trend_guard_adx_override` (default 35), so a genuinely strong trend is
never relabelled RANGING on a mislabelled range day.
"""
from types import SimpleNamespace

from intelligence.regime_detector import RegimeDetector


def _detector():
    return RegimeDetector.__new__(RegimeDetector)


def _settings(adx_override=35.0, enabled=True, min_conf=0.60):
    return SimpleNamespace(
        regime_range_day_trend_guard_enabled=enabled,
        regime_range_day_trend_guard_min_confidence=min_conf,
        regime_range_day_trend_guard_adx_override=adx_override,
    )


# A RANGE_DAY context with NO directional proof (VWAP chop) — the exact shape that
# demoted today's strong trends.
_RANGE_DAY_NO_PROOF = {
    "day_type": "RANGE_DAY",
    "confidence": 0.75,
    "vwap_cross_count": 5,
    "opening_range_status": "INSIDE",
    "close_above_vwap_pct": 50.0,
}


def test_strong_adx_trend_not_demoted_on_range_day():
    d = _detector()
    # ADX 44 (today's NIFTY) on a range-classified day: escape fires -> NOT demoted.
    assert d._range_day_guarded_regime(
        settings=_settings(adx_override=35.0),
        session_context=_RANGE_DAY_NO_PROOF,
        regime="TRENDING_UP",
        adx_val=44.0,
    ) is False


def test_weak_adx_trend_still_demoted_on_range_day():
    d = _detector()
    # ADX 22 (genuinely weak) on a range day with no directional proof: guard still
    # demotes, so real range-day protection is preserved.
    assert d._range_day_guarded_regime(
        settings=_settings(adx_override=35.0),
        session_context=_RANGE_DAY_NO_PROOF,
        regime="TRENDING_UP",
        adx_val=22.0,
    ) is True


def test_adx_override_zero_restores_old_behaviour():
    d = _detector()
    # override=0 disables the escape -> even ADX 44 is demoted (pre-fix behaviour).
    assert d._range_day_guarded_regime(
        settings=_settings(adx_override=0.0),
        session_context=_RANGE_DAY_NO_PROOF,
        regime="TRENDING_UP",
        adx_val=44.0,
    ) is True


def test_directional_proof_day_still_spares_low_adx_trend():
    d = _detector()
    # Existing directional-proof path is untouched: a clean opening-break day with
    # price holding one side of VWAP is not demoted even at low ADX.
    proof_ctx = {
        "day_type": "RANGE_DAY",
        "confidence": 0.75,
        "vwap_cross_count": 1,
        "opening_range_status": "BREAK_UP",
        "close_above_vwap_pct": 72.0,
    }
    assert d._range_day_guarded_regime(
        settings=_settings(adx_override=35.0),
        session_context=proof_ctx,
        regime="TRENDING_UP",
        adx_val=10.0,
    ) is False


def test_non_trend_regime_never_demoted():
    d = _detector()
    assert d._range_day_guarded_regime(
        settings=_settings(),
        session_context=_RANGE_DAY_NO_PROOF,
        regime="RANGING",
        adx_val=44.0,
    ) is False
