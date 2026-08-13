"""The LTP shock floor sat above almost every move the market actually makes.

market_ltp_shock_min_move_pct was 0.20% over a 3-minute lookback. Measured on
19,984 three-minute windows across four indices and 13 sessions:

    median 0.022%   p90 0.065%   p99 0.161%   max 0.978%

so 0.20% sits ABOVE the 99th percentile. It fired zero times on 2026-08-13, a
session in which NIFTY moved +0.32% and SENSEX +0.35%. A detector that never
fires is not protection, it is an absence.

0.12% is the p97-98 of that distribution — genuinely unusual, not noise. Paired
with the multi-index requirement already in place (>=2 core indices moving the
same way in the same window) the firing rate is bounded:

    >=0.20%  30 bars over 13 sessions  ~2.3/day
    >=0.12%  82 bars over 13 sessions  ~6.3/day

Six openings a day, each still facing the quality floor, the volume ratio and
the grade/confidence gates.

DELIBERATELY NOT ADDED: a companion 20-minute "sustained drift" detector. Once
this floor is at 0.12%, drift has no unique coverage — of 103 twenty-minute
windows moving >=0.30%, ZERO lacked a qualifying 3-minute spike inside them
(>=0.25%: 6.3% unique; >=0.20%: 22.4% unique but fires 19.6/day). A trailing
window also cannot lead a back-loaded move: on 2026-08-13 drift at 0.20% first
fired at 12:01 against an actual entry at 11:57:28, and at 0.30% never fired.
Two detectors on one tape, the second arriving after the first.
"""
import pytest

from config.settings import Settings, get_settings


def test_the_floor_sits_where_the_market_actually_moves():
    """p97-98 of the measured 3-minute distribution, not above its p99."""
    assert Settings.model_fields["market_ltp_shock_min_move_pct"].default == pytest.approx(0.12)


def test_the_strong_tier_still_sits_above_the_normal_tier():
    normal = Settings.model_fields["market_ltp_shock_min_move_pct"].default
    strong = Settings.model_fields["market_ltp_shock_strong_move_pct"].default

    assert strong > normal, "the strong tier must remain a higher bar than the normal one"


def test_the_multi_index_requirement_is_what_bounds_the_rate():
    """The floor is only safe because two core indices must agree; pin that."""
    settings = get_settings()

    assert int(getattr(settings, "market_ltp_shock_min_core_indices")) >= 2
    assert int(getattr(settings, "market_ltp_shock_min_indices")) >= 2


def test_the_volume_confirmation_survives():
    """A price move with no volume behind it is not a shock."""
    assert float(getattr(get_settings(), "market_ltp_shock_min_volume_ratio")) >= 1.0


def test_the_lookback_stays_short():
    """This is the SPIKE lane. Widening the window is how it would start
    duplicating a drift measure — the redundancy this change deliberately avoids."""
    settings = get_settings()

    assert int(getattr(settings, "market_ltp_shock_min_lookback_minutes")) <= 5
    assert int(getattr(settings, "market_ltp_shock_lookback_minutes")) <= 10


def test_no_sustained_drift_detector_was_added():
    """Pinned so the redundancy finding is not quietly undone later: at 0.30%/20m
    every drift window already contained a >=0.12%/3m spike."""
    settings = get_settings()

    for name in dir(settings):
        assert "sustained_drift" not in name, (
            f"{name}: a 20-minute drift lane is redundant with the 0.12% spike lane "
            "(0 of 103 windows unique) and lags a back-loaded move"
        )
