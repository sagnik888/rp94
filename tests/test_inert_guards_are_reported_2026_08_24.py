"""A guard whose configured values make it unable to fire must say so at startup.

engine/safe_mode.py already reports two neighbouring classes: a setting nothing reads
(config/inert_settings.py) and a threshold above what its score can physically reach
(unreachable_intel_gates). This covers the third: the consumer EXISTS and runs, but the
shipped values make the operation a no-op, so the protection an operator believes is
active does nothing.

Two found on 2026-08-24, all verified against the live config:

  1. Scanner._dynamic_runner_lock_pct, whose own docstring promises runners "MORE room
     in fast/volatile tape ... so noise does not shake the runner out". It returns
     min(base, loose) with base = dynamic_rr_runner_lock_pct = 0.85 (settings.py:1649)
     and loose = trend_runner_lock_pct_high_vol = 0.85 (settings.py:1252), i.e. the base
     unchanged in every regime. settings.py:1250 still describes the base as 0.90.

  2. dynamic_rr_max = 12.0 (settings.py:1647). The RR ladder is
     min_rr + confidence_runway + grade_bonus + intel_adjustment, bounded above by
     1.6 + 1.44 + 1.15 + 0.833 = 5.02, because normalize_intelligence_score clamps to
     [-1, 1] and the code divides by 1.20. 12.0 can never bind; what binds on every
     1min/5min entry is dynamic_rr_5min_max = 2.5.

Nothing here changes behaviour, matching config/inert_settings.py's own rule that wiring
a guard up or retuning it is the operator's decision, not a cleanup pass's.
"""
import pytest

from config.settings import Settings
from engine.safe_mode import inert_guard_warnings


def _s(**kw):
    """A FRESH Settings, never the singleton."""
    return Settings(**kw)


def _names(settings):
    return {name for name, _why in inert_guard_warnings(settings)}


def test_shipped_runner_and_rr_settings_are_not_inert():
    found = _names(_s())

    assert "trend_runner_lock_pct_high_vol" not in found
    assert "dynamic_rr_max" not in found


def test_equal_runner_locks_and_unreachable_rr_still_warn():
    found = _names(_s(
        trend_runner_lock_pct_high_vol=0.85,
        dynamic_rr_runner_lock_pct=0.85,
        dynamic_rr_max=12.0,
    ))

    assert "trend_runner_lock_pct_high_vol" in found
    assert "dynamic_rr_max" in found


def test_each_warning_explains_what_governs_instead():
    for _name, why in inert_guard_warnings(_s()):
        assert len(why) > 40, why



def test_a_genuinely_looser_high_vol_lock_is_not_reported():
    """A lower lock_pct leaves MORE of the gain unlocked, i.e. more room."""
    assert "trend_runner_lock_pct_high_vol" not in _names(
        _s(trend_runner_lock_pct_high_vol=0.70, dynamic_rr_runner_lock_pct=0.85)
    )


def test_a_reachable_rr_ceiling_is_not_reported():
    assert "dynamic_rr_max" not in _names(_s(dynamic_rr_max=4.0))


def test_the_rr_ceiling_boundary_is_the_arithmetic_maximum():
    """1.6 + 1.44 + 1.15 + 0.833 = 5.02 is the most the ladder can produce."""
    assert "dynamic_rr_max" not in _names(_s(dynamic_rr_max=5.0))
    assert "dynamic_rr_max" in _names(_s(dynamic_rr_max=5.5))


def test_the_rr_ceiling_claim_matches_the_real_ladder():
    """Pin the arithmetic against the actual function, not a copied constant.

    dynamic_rr(grade, confidence, intel_score, max_rr, timeframe). max_rr is passed
    explicitly so the ladder, not the setting, is what bounds the answer; timeframe is
    15min because dynamic_rr_5min_max=2.5 would otherwise cap 1min/5min at 2.5.
    """
    from engine.intelligence_score import normalize_intelligence_score
    from engine.signal_quality_policy import dynamic_rr

    assert normalize_intelligence_score(100.0) == 1.0, "intel normalises into [-1, 1]"

    best = max(
        dynamic_rr("A+", conf, intel, 99.0, "15min")
        for conf in (0.95, 0.99, 1.0)
        for intel in (1.0, 5.0, 100.0)
    )

    # 1.6 + 1.44 + 1.15 + 0.833 = 5.02
    assert best <= 5.03, best
    assert best > 4.5, best


def test_it_surfaces_through_real_mode_readiness():
    """The helper is worthless if validate_real_mode_readiness never asks."""
    import inspect

    from engine import safe_mode

    src = inspect.getsource(safe_mode.validate_real_mode_readiness)
    assert "inert_guard_warnings" in src
