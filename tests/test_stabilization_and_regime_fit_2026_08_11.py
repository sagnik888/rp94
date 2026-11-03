"""Four retained changes from the 2026-08-11 end-to-end audit.

The session that prompted them: NIFTY, BANKNIFTY and SENSEX opened at their high and
92-94% of the daily move was over by 10:00 (post-10:00 Kaufman ER 0.016-0.019, i.e.
noise). The engine's RANGING label was CORRECT. Within 22 seconds of the open it had
resolved the right SELL/PE candidate on all four indices — and the stabilization buffer
killed them, including a MIDCPNIFTY 15min SHORT carrying a one-way downside cascade
(core 3/3, total 4, volume 1.40x) that was re-admitted twice and still died at 13.7s.

    1  cascade latch          — confirmed cascade holds for the window
    2  thrust scaling         — a thrusting bar does not wait a flat 30s
    3  stabilization logging  — the surviving side is recorded, so a rate exists
    4  first-rung retention   — fast regimes get room matching measured heat

The former late-trend ADX ceiling was retired. It duplicated the canonical quality
verdict, used mixed observational evidence, and could reject otherwise valid setups
after the single admission decision had passed.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner
from tests.source_views import scanner_runtime_source


@pytest.fixture(autouse=True)
def _pinned():
    from config.settings import Settings, get_settings

    s = get_settings()
    keys = ("stabilization_cascade_latch_enabled", "stabilization_thrust_scaling_enabled",
            "stabilization_thrust_min_seconds", "stabilization_thrust_range_atr",
            "stabilization_thrust_volume_ratio", "first_rung_regime_retention_enabled",
            "first_rung_fast_regime_retention", "profit_lock_min_ratio")
    saved = {k: getattr(s, k, None) for k in keys}
    for k in keys:
        if k in Settings.model_fields:
            setattr(s, k, Settings.model_fields[k].default)
    yield
    for k, v in saved.items():
        setattr(s, k, v)


# ── 1. the cascade latch ─────────────────────────────────────────────────────

def _sc():
    sc = Scanner.__new__(Scanner)
    sc._diag_inc = lambda *a, **k: None
    return sc


CASCADE = {"type": "one_way_cascade", "reason": "one-way downside cascade: core 3/3, total 4"}


def test_a_confirmed_cascade_is_held_after_it_lapses():
    """The 09:15 MIDCPNIFTY case: re-admitted twice, then killed when the cascade
    momentarily stopped re-proving itself while the arrow was still quiet."""
    sc = _sc()
    c = SimpleNamespace()
    assert sc._latched_cascade_override(c, CASCADE) is CASCADE      # confirmed
    held = sc._latched_cascade_override(c, None)                    # lapsed
    assert held, "the confirmed cascade was not held"
    assert "held" in held.get("reason", "")


def test_nothing_is_held_without_a_prior_confirmation():
    assert _sc()._latched_cascade_override(SimpleNamespace(), None) is None


def test_the_latch_is_per_signal():
    """It lives on the candidate, so a different buffered signal cannot inherit it."""
    sc = _sc()
    a, b = SimpleNamespace(), SimpleNamespace()
    sc._latched_cascade_override(a, CASCADE)
    assert sc._latched_cascade_override(b, None) is None


def test_the_latch_can_be_switched_off():
    from config.settings import get_settings

    sc = _sc()
    c = SimpleNamespace()
    sc._latched_cascade_override(c, CASCADE)
    setattr(get_settings(), "stabilization_cascade_latch_enabled", False)
    assert sc._latched_cascade_override(c, None) is None


def test_a_flipped_arrow_still_vetoes_before_the_latch_is_consulted():
    """The latch rescues an arrow that went QUIET, never one the source CONTRADICTED.
    The FLIPPED branch returns before the latch is reachable."""
    import inspect

    src = inspect.getsource(Scanner._stabilization_repaint_override_context)
    flip = src.index('"FLIPPED"')
    latch = src.index("_latched_cascade_override")
    assert flip < latch, "the FLIP veto no longer precedes the latch"
    assert "return None" in src[flip:latch]


# ── 2. thrust scaling ────────────────────────────────────────────────────────

def _cand(range_atr=0.0, vol=0.0, **kw):
    return SimpleNamespace(range_atr_ratio=range_atr, volume_ratio=vol, **kw)


def test_a_thrusting_bar_waits_the_floor_not_the_full_window():
    sc = _sc()
    assert sc._thrust_scaled_stabilization_seconds(_cand(range_atr=2.0), 30.0) == 10.0
    assert sc._thrust_scaled_stabilization_seconds(_cand(vol=3.0), 30.0) == 10.0


def test_an_ordinary_bar_waits_the_full_window():
    sc = _sc()
    assert sc._thrust_scaled_stabilization_seconds(_cand(range_atr=1.0, vol=1.1), 30.0) == 30.0


def test_absent_metrics_are_not_read_as_a_thrust():
    """Absence of evidence is not evidence — the full wait stands."""
    assert _sc()._thrust_scaled_stabilization_seconds(_cand(), 30.0) == 30.0


def test_scaling_can_only_shorten():
    sc = _sc()
    assert sc._thrust_scaled_stabilization_seconds(_cand(range_atr=5.0), 8.0) == 8.0


def test_thrust_scaling_can_be_switched_off():
    from config.settings import get_settings

    setattr(get_settings(), "stabilization_thrust_scaling_enabled", False)
    assert _sc()._thrust_scaled_stabilization_seconds(_cand(range_atr=5.0), 30.0) == 30.0


def test_a_one_way_cascade_gets_the_fast_lane():
    """It is the same class of evidence as a market-wide reversal, which already drew
    8s while the cascade drew 30 — the difference that lost the 2026-08-11 open."""
    import inspect

    src = inspect.getsource(Scanner._live_signal_stabilization_requirements)
    idx = src.index("one_way_cascade_entry")
    assert "market_ltp_shock_stabilization_seconds" in src[idx:idx + 400]


# ── 3. the surviving side is recorded ────────────────────────────────────────

def test_maturation_is_recorded_to_rtracker():
    """Only deaths were recorded, so no survival rate was computable from rtracker."""
    import inspect

    src = scanner_runtime_source()
    assert 'decision="STABILIZED"' in src
    idx = src.index('decision="STABILIZED"')
    window = src[max(0, idx - 900):idx + 700]
    assert "stabilization_gate" in window
    assert "elapsed_seconds" in window and "window_seconds" in window


def test_stabilized_is_its_own_decision():
    """It must not be counted as an entry in the funnel arithmetic."""
    import inspect

    src = scanner_runtime_source()
    idx = src.index('decision="STABILIZED"')
    assert 'decision="ACCEPTED"' not in src[idx - 200:idx + 200]


# ── 4. first-rung retention by regime ────────────────────────────────────────

def _ratio(peak, regime):
    sc = Scanner.__new__(Scanner)
    sc._timeframe_regimes = {}
    sc.latest_regimes = {"NIFTY": regime}
    t = SimpleNamespace(instrument="NIFTY", timeframe="5min", inst_type="OPT",
                        grade="A", market_regime=regime)
    return sc._profit_lock_floor(100.0, "LONG", 10, peak, scalp_mode=False,
                                 initial_risk_rs=1000.0, timeframe="5min", trade_like=t)[2]


@pytest.mark.parametrize("regime,heat", [
    ("VOLATILE", 0.49), ("BREAKOUT_UP", 0.47), ("BREAKOUT_DOWN", 0.43),
])
def test_fast_regimes_breathe_past_their_measured_heat(regime, heat):
    """A 0.50 floor stops a trade that gives back half its peak, and these winners give
    back 43-49% as a matter of course. The floor must sit BELOW the measured median."""
    ratio = _ratio(600.0, regime)
    assert ratio == pytest.approx(0.38)
    assert 1.0 - ratio > heat, f"{regime}: {1-ratio:.0%} breathing does not clear {heat:.0%} heat"


@pytest.mark.parametrize("regime", ["RANGING", "TRENDING_UP", "TRENDING_DOWN", "UNKNOWN"])
def test_every_other_regime_keeps_the_operator_schedule(regime):
    """TRENDING_UP especially: 8% heat against a Rs.3,070 median peak means the existing
    leash is nowhere near binding, and loosening it would give back real money."""
    assert _ratio(600.0, regime) == pytest.approx(0.50)


@pytest.mark.parametrize("peak,expected", [(1500.0, 0.675), (2500.0, 0.775), (4000.0, 0.875)])
def test_the_higher_rungs_are_untouched(peak, expected):
    assert _ratio(peak, "VOLATILE") == pytest.approx(expected)
    assert _ratio(peak, "RANGING") == pytest.approx(expected)


def test_the_rung_can_be_switched_off():
    from config.settings import get_settings

    setattr(get_settings(), "first_rung_regime_retention_enabled", False)
    assert _ratio(600.0, "VOLATILE") == pytest.approx(0.50)


def test_the_global_ratio_floor_no_longer_silently_undoes_it():
    """profit_lock_min_ratio (0.50) is a guard against misconfiguration; as a hard clamp
    it cancelled this rule entirely, which is how the first draft shipped as a no-op."""
    assert _ratio(600.0, "VOLATILE") < 0.50


def test_it_can_only_loosen_never_tighten():
    sc = Scanner.__new__(Scanner)
    sc._timeframe_regimes = {}
    sc.latest_regimes = {"NIFTY": "VOLATILE"}
    t = SimpleNamespace(instrument="NIFTY", timeframe="5min")
    assert Scanner._first_rung_regime_retention(sc, t, "5min") <= 0.50
    sc.latest_regimes = {"NIFTY": "RANGING"}
    assert Scanner._first_rung_regime_retention(sc, t, "5min") == 1.0


def test_an_unknown_position_is_left_alone():
    sc = Scanner.__new__(Scanner)
    assert Scanner._first_rung_regime_retention(sc, None, "5min") == 1.0


# ── 5. the late-trend ceiling ────────────────────────────────────────────────

def test_the_duplicate_late_trend_gate_stays_retired():
    """Admission has one owner; a second post-verdict ADX ceiling must not return."""
    import inspect

    from config.settings import Settings
    from engine.signal_processor import SignalProcessor

    src = inspect.getsource(SignalProcessor)
    assert "late_trend_entry_adx_ceiling" not in src
    assert "late_trend_entry_guard_enabled" not in src
    assert "Late-trend entry" not in src
    assert "late_trend_entry_adx_ceiling" not in Settings.model_fields
    assert "late_trend_entry_guard_enabled" not in Settings.model_fields
