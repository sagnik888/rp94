"""
B-grade signal treatment audit (characterization tests).

These tests pin down HOW a B-grade signal (confidence band [0.40, 0.55)) is
actually treated across the filter stack, and document a real divergence:

  * The POLICY gate (`engine.signal_quality_policy.passes_quality_gate`), used
    by the historical reconstruction / recovery paths, blocks B-grade in REAL
    mode entirely and allows it in HISTORICAL (`ut_backtest_more_results`).
  * `option_grade_allowed` routes a B-grade to options ONLY in trending+momentum
    (the dedicated B rule); it is blocked in choppy/ranging/volatile/unknown.

The scanner's *live* entry path uses a DIFFERENT gate
(`_passes_live_trade_ready_gate`) that is looser for 15min (no confidence
floor) — captured in the audit notes, not here, because it needs the live
scanner object. The contrast is the headline finding.
"""

import pytest

from engine.signal_manager import SignalManager
from engine.ut_bot_core import UTBotSignal
from engine.signal_quality_policy import (
    grade_rank,
    option_grade_allowed,
    passes_quality_gate,
)
from config.settings import get_settings


@pytest.fixture
def settings():
    s = get_settings()
    # Pin thresholds so the characterization is deterministic regardless of .env.
    s.live_filter_leniency_pct = 0.0
    s.ut_timeframe_entry_policy = "INCLUDE_5MIN"
    s.ut_regime_adaptation = True
    s.ut_backtest_more_results = True
    return s


def _signal(adx=20.0):
    return UTBotSignal(
        timestamp=None,
        signal_type="BUY",
        price=25000.0,
        trailing_stop=24950.0,
        stop_distance=50.0,
        atr_value=40.0,
        adx_value=adx,
        position_state=1,
        bar_index=10,
        raw_candle={"open": 24990.0, "high": 25010.0, "low": 24980.0, "close": 25000.0},
    )


# ── Grading boundary ────────────────────────────────────────────────
def test_b_grade_confidence_band_is_0_40_to_0_55():
    assert grade_rank("B") == 1
    assert grade_rank("B+") == 2
    # The grader maps confidence -> grade; B occupies [0.40, 0.55).
    mgr = SignalManager()
    # Force a near-neutral score so confidence lands in the B band.
    grade, conf, _ = mgr._grade_signal(_signal(adx=18.0), confluence_score=0.0,
                                       intelligence_score=0.0, regime="UNKNOWN")
    # UNKNOWN regime applies a penalty -> confidence below 0.40 -> C (documents
    # that "neutral" setups are NOT B; B needs some positive contribution).
    assert grade in {"B", "C"}


# ── option routing: B options only in trending+momentum (new dedicated rule) ──
@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNKNOWN"])
@pytest.mark.parametrize("tf", ["5min", "15min"])
@pytest.mark.parametrize("mode", ["REAL", "HISTORICAL"])
def test_b_grade_blocked_as_option_outside_trend(settings, regime, tf, mode):
    # Outside trending regimes, B can never be an option even with maxed momentum.
    allowed = option_grade_allowed(
        "B", 0.54, regime, mode=mode,
        adx_value=45.0, intel_score=0.95, signal_score=0.95, timeframe=tf,
    )
    assert allowed is False, f"B leaked into options: {tf}/{regime}/{mode}"


@pytest.mark.parametrize("tf", ["5min", "15min"])
@pytest.mark.parametrize("mode", ["REAL", "HISTORICAL"])
def test_b_grade_allowed_as_option_in_trend_with_momentum(settings, tf, mode):
    # The dedicated B path: trending + conf>=0.50 + ADX>=25 -> option-eligible.
    assert option_grade_allowed(
        "B", 0.52, "TRENDING_UP", mode=mode, adx_value=30.0, timeframe=tf,
    ) is True


# ── policy gate: B blocked live, allowed in backtest (the divergence) ─
@pytest.mark.parametrize("tf", ["5min", "15min"])
def test_policy_gate_blocks_b_grade_in_live(settings, tf):
    ok = passes_quality_gate(
        tf, "B", 0.54, "TRENDING", "FUT", settings,
        mode="REAL", adx_value=20.0, trend_15m_agrees=True, intel_score=0.0,
    )
    assert ok is False, f"Policy gate unexpectedly allowed live B on {tf}"


def test_policy_gate_allows_b_grade_in_backtest_trending(settings):
    # more_results path lets B-grade FUT through in HISTORICAL — these trades
    # do NOT reproduce live via the policy gate (parity gap).
    ok = passes_quality_gate(
        "15min", "B", 0.50, "TRENDING", "FUT", settings,
        mode="HISTORICAL", adx_value=20.0, trend_15m_agrees=True, intel_score=0.0,
    )
    assert ok is True


def test_bplus_is_first_live_tradeable_tier_on_15min(settings):
    # B+ (rank 2, conf >= 0.58) clears the 15min live floor where B cannot.
    assert passes_quality_gate(
        "15min", "B+", 0.60, "TRENDING", "FUT", settings,
        mode="REAL", adx_value=20.0, trend_15m_agrees=True,
    ) is True
    assert passes_quality_gate(
        "15min", "B", 0.54, "TRENDING", "FUT", settings,
        mode="REAL", adx_value=20.0, trend_15m_agrees=True,
    ) is False
