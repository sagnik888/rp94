"""The best exit in the system went dormant as the market quietened.

Across the stored sessions the median daily range fell 0.97% (W30) to 0.64% (W33). The
profit-side thresholds are fixed rupees, so nothing moved with it, and:

    week   trades reaching a Rs.750 peak    PEAK_THREAT_EXIT fired
    W28              43%                             11
    W30              37%                             17
    W31              23%                              8
    W32              13%                              6
    W33               4%                              1

PEAK_THREAT_EXIT banks 87% of peak on big trades — the best capture in the book. As it
went dormant LOSS_THREAT_EXIT rose from 30% to 50% of all exits and the win rate went
47.8% -> 12.5%.

Scoped to the peak-threat arm ONLY. The first draft also scaled the profit-lock and
give-back floors and broke test_july3_roundtrips_would_now_arm: the Rs.300 give-back
noise floor is an incident-derived operator decision (2026-07-03) and scaling it would
have armed on a Rs.270 blip the operator deliberately excluded. The give-back guard also
captures only 15-25% on big trades, so it was never the exit worth rescuing.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner


@pytest.fixture(autouse=True)
def _pinned():
    from config.settings import Settings, get_settings

    s = get_settings()
    keys = ("profit_threshold_scaling_enabled", "profit_threshold_regime_scaling_enabled",
            "profit_threshold_reference_risk_rs", "profit_threshold_scale_min",
            "profit_threshold_scale_max", "range_scaled_budget_enabled",
            "range_scaled_budget_reference_pct", "range_scaled_budget_min_trades")
    saved = {k: getattr(s, k, None) for k in keys}
    for k in keys:
        setattr(s, k, Settings.model_fields[k].default)
    yield
    for k, v in saved.items():
        setattr(s, k, v)


def _sc(regime="RANGING"):
    sc = Scanner.__new__(Scanner)
    sc._timeframe_regimes = {}
    sc.latest_regimes = {"NIFTY": regime}
    return sc


def _t(regime="RANGING"):
    return SimpleNamespace(instrument="NIFTY", timeframe="5min", inst_type="OPT",
                           market_regime=regime)


def _scale(risk, regime="RANGING"):
    return Scanner._profit_threshold_scale(_sc(regime), _t(regime), risk)


# ── the scale ────────────────────────────────────────────────────────────────

def test_a_quiet_market_lowers_the_floor():
    """Risk is an ATR-derived stop times quantity, so a small risk IS a quiet market."""
    assert _scale(500.0) < _scale(1000.0)


def test_the_reference_risk_is_neutral_in_a_neutral_regime():
    assert _scale(1000.0, "VOLATILE") == pytest.approx(1.0)


def test_the_scale_can_never_raise_the_arm():
    """It exists to RESTORE an exit that has gone unreachable. Allowing >1.0 in trending
    regimes suppressed the very exit it was meant to rescue — measured as an exact wash,
    17 trades reaching the arm both before and after."""
    for risk in (500.0, 1000.0, 5000.0, 100000.0):
        for reg in ("TRENDING_UP", "BREAKOUT_UP", "VOLATILE", "RANGING", "TRENDING_DOWN"):
            assert _scale(risk, reg) <= 1.0


def test_the_scale_is_floored():
    """An absurdly small risk must not drive the arm to zero."""
    from config.settings import get_settings

    assert _scale(1.0) == pytest.approx(getattr(get_settings(), "profit_threshold_scale_min"))


def test_regime_still_differentiates_below_the_cap():
    """Chop banks sooner than a trend, which is the tuning that was asked for — it just
    happens entirely below 1.0 now."""
    assert _scale(800.0, "RANGING") < _scale(800.0, "TRENDING_UP")


def test_absent_risk_leaves_the_floor_alone():
    """No measurement is not evidence of a quiet market."""
    assert _scale(0.0, "VOLATILE") == pytest.approx(1.0)


@pytest.mark.parametrize("toggle,expected_neutral", [
    ("profit_threshold_scaling_enabled", True),
    ("profit_threshold_regime_scaling_enabled", False),
])
def test_the_toggles_work(toggle, expected_neutral):
    from config.settings import get_settings

    setattr(get_settings(), toggle, False)
    got = _scale(1000.0, "RANGING")
    if expected_neutral:
        assert got == pytest.approx(1.0), "scaling off must be a no-op"
    else:
        assert got == pytest.approx(1.0), "regime off leaves only the volatility term"


def test_it_is_wired_into_the_peak_threat_arm():
    import inspect

    # Every occurrence, not the first — the first is the settings-echo dict, and
    # asserting on it silently tested nothing.
    src = inspect.getsource(Scanner)
    hits, at = 0, -1
    while True:
        at = src.find("peak_threat_exit_min_peak_rs", at + 1)
        if at < 0:
            break
        if "_profit_threshold_scale" in src[at:at + 400]:
            hits += 1
    assert hits >= 1, "the peak-threat arm is no longer volatility-scaled"


def test_it_is_NOT_wired_into_the_giveback_or_lock_arms():
    """The Rs.300 give-back noise floor is an incident-derived decision (2026-07-03) and
    scaling it armed on a Rs.270 blip the operator deliberately excluded. The give-back
    guard captures 15-25% on big trades; it was never the exit worth rescuing."""
    import inspect

    for fn in (Scanner._profit_giveback_arm, Scanner._profit_lock_min_peak):
        assert "_profit_threshold_scale" not in inspect.getsource(fn)


# ── the range-scaled entry budget ────────────────────────────────────────────

def _budget(measured, base=12):
    sc = Scanner.__new__(Scanner)
    sc._session_range_pct = lambda _inst: measured
    return Scanner._range_scaled_trade_budget(sc, "NIFTY", base)


@pytest.mark.parametrize("rng,expected", [
    (0.97, 12),   # W30 — a full day, full budget
    (0.77, 10),   # W32
    (0.64, 9),    # W33
    (0.62, 8),    # W31
])
def test_the_budget_follows_the_measured_range(rng, expected):
    """Trades per 1% of range ran 15.5, 13.8, 16.9, 18.8 across these weeks — activity
    rose against opportunity while the median peak fell Rs.533 to Rs.78."""
    assert _budget(rng)[0] == expected


def test_a_wide_day_never_increases_the_budget():
    """Only ever reduces — the phase bonuses above it own any widening."""
    assert _budget(2.50)[0] == 12


def test_the_budget_has_a_floor():
    assert _budget(0.01)[0] == 3


def test_an_unmeasurable_range_leaves_the_budget_alone():
    """No measurement is not evidence of a quiet day."""
    assert _budget(None)[0] == 12
    assert _budget(0.0)[0] == 12


def test_the_budget_scaling_can_be_switched_off():
    from config.settings import get_settings

    setattr(get_settings(), "range_scaled_budget_enabled", False)
    assert _budget(0.30)[0] == 12


def test_it_is_wired_into_the_dynamic_budget():
    import inspect

    src = inspect.getsource(Scanner._dynamic_trade_budget)
    assert "_range_scaled_trade_budget" in src
    assert src.index("_range_scaled_trade_budget") < src.index("return min(20, budget)")


def test_the_reason_string_explains_the_cut():
    """The operator has to be able to see WHY the budget shrank."""
    _b, note = _budget(0.62)
    assert "range" in note and "0.62" in note
