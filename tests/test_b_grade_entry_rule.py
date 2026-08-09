"""
B-grade entry rule (the dedicated trending-only B path).

Spec (operator-approved):
  B-grade (confidence 0.40-0.55) may trade ONLY when:
    confidence >= 0.50  AND  15m trend aligns  AND  ADX >= 25
    AND regime is trending/breakout (NOT choppy/ranging/volatile/unknown).
  Applies to 5m and 15m, futures and options.
Plus: 15m UNKNOWN floor lowered 0.85 -> 0.70 (admits A); 5m choppy OPT floor 0.65 -> 0.70.
"""

from types import SimpleNamespace

import pytest

from scanner import Scanner
from config.settings import get_settings
from engine.signal_quality_policy import (
    b_grade_entry_allowed,
    option_grade_allowed,
    passes_quality_gate,
)


@pytest.fixture
def settings():
    s = get_settings()
    s.live_filter_leniency_pct = 0.0
    s.ut_regime_adaptation = True
    s.ut_timeframe_entry_policy = "INCLUDE_5MIN"
    s.live_b_grade_min_confidence = 0.50
    return s


# ── canonical helper ────────────────────────────────────────────────
def test_b_helper_core_rule(settings):
    base = dict(trend_15m_agrees=True, adx_value=30.0, settings=settings)
    assert b_grade_entry_allowed(0.52, "TRENDING_UP", **base) is True
    assert b_grade_entry_allowed(0.52, "BREAKOUT_UP", **base) is True
    assert b_grade_entry_allowed(0.48, "TRENDING_UP", **base) is False   # conf < 0.50
    assert b_grade_entry_allowed(0.62, "TRENDING_UP", **base) is False   # not B band (B+)
    assert b_grade_entry_allowed(0.52, "RANGING", **base) is False       # not trending
    assert b_grade_entry_allowed(0.52, "VOLATILE", **base) is False
    assert b_grade_entry_allowed(0.52, "UNKNOWN", **base) is False
    assert b_grade_entry_allowed(0.52, "TRENDING_UP", trend_15m_agrees=False,
                                 adx_value=30.0, settings=settings) is False
    assert b_grade_entry_allowed(0.52, "TRENDING_UP", trend_15m_agrees=True,
                                 adx_value=20.0, settings=settings) is False  # ADX < 25


# ── policy gate parity (backtest/recovery) ──────────────────────────
@pytest.mark.parametrize("tf", ["5min", "15min"])
def test_policy_gate_allows_qualifying_b_in_trend(settings, tf):
    assert passes_quality_gate(
        tf, "B", 0.52, "TRENDING_UP", "FUT", settings,
        mode="REAL", adx_value=30.0, trend_15m_agrees=True,
    ) is True


@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNKNOWN"])
def test_policy_gate_blocks_b_outside_trend(settings, regime):
    assert passes_quality_gate(
        "15min", "B", 0.52, regime, "FUT", settings,
        mode="REAL", adx_value=30.0, trend_15m_agrees=True,
    ) is False


def test_option_grade_allows_b_in_trend_only(settings):
    assert option_grade_allowed("B", 0.52, "TRENDING_UP", mode="REAL", adx_value=30.0) is True
    assert option_grade_allowed("B", 0.52, "RANGING", mode="REAL", adx_value=30.0) is False
    assert option_grade_allowed("B", 0.52, "TRENDING_UP", mode="REAL", adx_value=20.0) is False
    assert option_grade_allowed("B", 0.48, "TRENDING_UP", mode="REAL", adx_value=30.0) is False


# ── scanner live gate (the path that actually trades live) ──────────
_INST = ["_passes_live_trade_ready_gate", "_passes_candidate_grade_preference",
         "_passes_timeframe_entry_policy", "_market_location_entry_gate",
         "_candidate_15m_trend_agrees", "_gate_block",
         "_late_entry_scalp_active", "_set_scalp_lock", "_minutes_to_force_exit",
         # The gates resolve the CANDIDATE's own-timeframe regime rather than the
         # instrument-keyed 5-minute label, so the stub needs the resolver pair.
         "_get_trade_regime", "_trade_regime_is_own_timeframe"]
_STAT = ["_normalize_signal_grade_preference", "_setting_enabled",
         "_is_quad_prime_raw_candidate", "_timeframe_intel_key"]


def _stub(regime, tf_regimes=None):
    # `latest_regimes` is instrument-keyed and IS the 5-minute label; `_timeframe_regimes`
    # is where a candidate's own-timeframe label lives. Empty by default, so
    # _get_trade_regime falls back to latest_regimes exactly as these tests expect.
    st = SimpleNamespace(mode="REAL", latest_regimes={"NIFTY": regime},
                         _timeframe_regimes=dict(tf_regimes or {}))
    for m in _INST:
        setattr(st, m, getattr(Scanner, m).__get__(st, Scanner))
    for m in _STAT:
        setattr(st, m, getattr(Scanner, m))
    return st


def _cand(g, c, tf, inst, a, adx):
    return SimpleNamespace(grade=g, confidence=c, timeframe=tf, inst_type=inst,
                           instrument="NIFTY", direction="LONG", adx_value=adx,
                           impulse_confirmed=False, is_explosive_bypass=False,
                           trend_15m_agrees=a, reasons=[])


def _live_trades(s, g, c, tf, inst, reg, a, adx, own_tf_regime=True):
    # `reg` is the market state for THIS candidate, so publish it on the candidate's own
    # timeframe. Previously it was only ever written to latest_regimes -- the 5-minute,
    # instrument-keyed label -- so a 15-minute candidate was being admitted on the
    # 5-minute pocket. That is the privilege _trade_regime_is_own_timeframe forbids.
    # own_tf_regime=False reproduces the old shape: 5-minute label only.
    tf_regimes = {f"NIFTY_{tf}": reg} if own_tf_regime else {}
    gp = _stub(reg, tf_regimes)._passes_candidate_grade_preference(_cand(g, c, tf, inst, a, adx))[0]
    lr = _stub(reg, tf_regimes)._passes_live_trade_ready_gate(_cand(g, c, tf, inst, a, adx))
    return gp and lr


def test_a_15min_b_grade_is_not_admitted_on_the_5min_regime(settings):
    """The privilege must rest on the candidate's OWN timeframe.

    Same inputs as test_live_gate_b_trades_in_trend[15min], except the trending label
    exists only on the 5-minute pocket. 5m/15m regimes disagree in 83% of same-timestamp
    pairs, so this is not a corner case.
    """
    settings.signal_grade_preference = "A"
    assert _live_trades(settings, "B", 0.52, "15min", "FUT", "TRENDING_UP", True, 30.0,
                        own_tf_regime=False) is False
    # ...and a 5-minute candidate legitimately OWNS that same label.
    assert _live_trades(settings, "B", 0.52, "5min", "FUT", "TRENDING_UP", True, 30.0,
                        own_tf_regime=False) is True


@pytest.mark.parametrize("tf", ["5min", "15min"])
@pytest.mark.parametrize("inst", ["FUT", "OPT"])
def test_live_gate_b_trades_in_trend(settings, tf, inst):
    # Even with grade preference 'A', the qualifying-B path must allow the trade.
    settings.signal_grade_preference = "A"
    assert _live_trades(settings, "B", 0.52, tf, inst, "TRENDING_UP", True, 30.0) is True


@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNKNOWN"])
def test_live_gate_b_blocked_outside_trend(settings, regime):
    settings.signal_grade_preference = "A"
    assert _live_trades(settings, "B", 0.52, "15min", "FUT", regime, True, 30.0) is False


def test_live_gate_b_requires_all_conditions(settings):
    settings.signal_grade_preference = "A"
    assert _live_trades(settings, "B", 0.52, "15min", "FUT", "TRENDING_UP", False, 30.0) is False  # no align
    assert _live_trades(settings, "B", 0.48, "15min", "FUT", "TRENDING_UP", True, 30.0) is False   # conf<0.50
    assert _live_trades(settings, "B", 0.52, "15min", "FUT", "TRENDING_UP", True, 20.0) is False   # adx<25


def test_live_gate_unknown_admits_A_not_Bplus(settings):
    settings.signal_grade_preference = "auto"
    assert _live_trades(settings, "A", 0.72, "15min", "FUT", "UNKNOWN", False, 20.0) is True
    assert _live_trades(settings, "B+", 0.66, "15min", "FUT", "UNKNOWN", False, 20.0) is False


# ── AUTO router: qualifying B routes to OPT in strong trends, FUT otherwise ──
from datetime import datetime


class _SP:
    def _option_grade_allowed(self, g, c, r, adx_value=0, intel_score=0, signal_score=0,
                              five_min_true_impulse=False, timeframe=""):
        return option_grade_allowed(g, c, r, mode="REAL", adx_value=adx_value,
                                    intel_score=intel_score, signal_score=signal_score,
                                    five_min_true_impulse=five_min_true_impulse, timeframe=timeframe)


class _EXP:
    def is_expiry_day(self, i, d):
        return False


def _route(settings, grade, conf, regime, adx, atr=20.0, price=25000.0):
    # Use the REAL _setting_enabled so flags like lean_routing_to_options (read
    # from the live settings singleton) are honoured instead of always-default.
    st = SimpleNamespace(inst_pref="AUTO", mode="REAL", signal_processor=_SP(),
                         expiry=_EXP(), _setting_enabled=Scanner._setting_enabled)
    return Scanner._resolve_instrument_type(
        st, instrument="NIFTY", grade=grade, regime=regime, confidence=conf,
        signal_score=0.3, intel_score=0.3, adx_value=adx, atr_value=atr, price=price,
        signal_time=datetime(2026, 6, 19, 10, 0), iv_percentile=50.0,
        timeframe="15min", five_min_true_impulse=False,
    )


def test_auto_routes_qualifying_b_to_options_in_trend(settings):
    assert _route(settings, "B", 0.52, "TRENDING_UP", 30) == "OPT"
    assert _route(settings, "B", 0.52, "BREAKOUT_UP", 30) == "OPT"


def test_auto_keeps_b_in_futures_otherwise(settings):
    # Base routing rule (lean-to-options OFF): non-option-qualifying B-grades -> FUT.
    settings.lean_routing_to_options = False
    try:
        assert _route(settings, "B", 0.52, "RANGING", 30) == "FUT"
        assert _route(settings, "B", 0.52, "UNKNOWN", 30) == "FUT"
        assert _route(settings, "B", 0.48, "TRENDING_UP", 30) == "FUT"   # conf < 0.50
        assert _route(settings, "B", 0.52, "TRENDING_UP", 20) == "FUT"   # ADX < 25
        assert _route(settings, "B", 0.52, "TRENDING_UP", 30, atr=200.0) == "FUT"  # high spot vol
    finally:
        settings.lean_routing_to_options = True


def test_lean_routing_sends_loss_prone_futures_to_options(settings):
    # New default (lean ON): choppy + ordinary catch-all -> defined-risk options;
    # genuine FUT cases (high spot vol) are preserved. A weak signal in a REAL
    # trend with compressed ATR is no longer "ordinary" — slow-grind routing
    # sends it to futures (options would theta-bleed on the slow move).
    settings.lean_routing_to_options = True
    assert _route(settings, "B", 0.52, "RANGING", 30) == "OPT"        # choppy -> options
    assert _route(settings, "B", 0.48, "TRENDING_UP", 20) == "FUT"    # slow-grind trend -> futures
    assert _route(settings, "B", 0.48, "TRENDING_UP", 17) == "OPT"    # sub-floor ADX: catch-all -> options
    assert _route(settings, "B", 0.52, "TRENDING_UP", 30, atr=200.0) == "FUT"  # high spot vol stays FUT
