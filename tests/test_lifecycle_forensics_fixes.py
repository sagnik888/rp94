"""2026-07-03 lifecycle-forensics fixes: non-trend give-back scope,
exhaustion confidence cap, pre-candle flip veto (telemetry-first)."""

import inspect
from types import SimpleNamespace

from config.settings import get_settings
from engine.signal_manager import SignalManager
from engine.signal_processor import SignalProcessor
from scanner import Scanner


# ── Fix A: non-trend give-back scope + lower arm ──

def _guard_scanner(day_type):
    scanner = Scanner.__new__(Scanner)
    scanner._eod_protect_active = lambda now: False
    scanner._cached_timeframe_intel = lambda *a, **k: {
        "session_context": {"day_type": day_type, "confidence": 0.8}
    }
    return scanner


def test_nontrend_arm_uses_lower_floor_and_multiple():
    scanner = _guard_scanner("UNDECIDED")
    # July-3 NIFTY trade: risk Rs.990, peak Rs.390. Old arm max(500, 396)=500
    # never armed; non-trend arm max(300, 0.30*990)=300 arms at the 390 peak.
    assert scanner._profit_giveback_arm(990.0) == 500.0
    assert scanner._profit_giveback_arm(990.0, nontrend_scope=True) == 300.0
    # BANKNIFTY trade: risk Rs.1773 -> non-trend arm 0.30R = 531.9 < 574 peak.
    assert scanner._profit_giveback_arm(1773.0, nontrend_scope=True) == 1773.0 * 0.30


def test_july3_roundtrips_would_now_arm():
    scanner = _guard_scanner("UNDECIDED")
    # (initial risk Rs, actual peak Rs) for the July-3 round-trip losers.
    # Three of four now arm; the 14:53 SENSEX PE (peak Rs.270) stays under the
    # Rs.300 noise floor by design — protecting sub-Rs.300 blips would churn on
    # spread/exit costs, and bending the floor to one sample is overfitting.
    protected = [(990.0, 390.0), (948.0, 465.0), (1773.0, 574.0)]
    for risk, peak in protected:
        arm = scanner._profit_giveback_arm(risk, nontrend_scope=True)
        assert peak >= arm, f"risk={risk} peak={peak} arm={arm}"
    below_floor_arm = scanner._profit_giveback_arm(820.0, nontrend_scope=True)
    assert below_floor_arm == 300.0
    assert 270.0 < below_floor_arm


def test_giveback_scope_extends_to_nontrend_days_only():
    source = inspect.getsource(Scanner._peak_giveback_or_reversal_exit)
    assert "profit_giveback_nontrend_all_trades" in source
    assert "_current_day_type" in source
    assert 'not in {"TREND_DAY_UP", "TREND_DAY_DOWN"}' in source
    # Trend day -> day-type helper reports trend -> scope must not widen.
    assert _guard_scanner("TREND_DAY_UP")._current_day_type("NIFTY") == "TREND_DAY_UP"
    assert _guard_scanner("RANGE_DAY")._current_day_type("NIFTY") == "RANGE_DAY"


# ── Fix B: exhaustion confidence cap ──

def _grade(adx):
    manager = SignalManager()
    signal = SimpleNamespace(
        instrument="SENSEX",
        timeframe="5min",
        signal_type="BUY",
        price=80000.0,
        adx_value=adx,
        atr_value=100.0,
        stop_distance=100.0,
        timestamp=None,
    )
    # Strong technical + aligned regime + strong intel: would land A+ uncapped.
    return manager._grade_signal(signal, confluence_score=0.9, intelligence_score=80.0, regime="TRENDING_UP")


def test_exhaustion_cap_blocks_top_label_above_adx_40():
    grade, confidence, reasons = _grade(adx=50.0)
    cap = get_settings().ut_exhaustion_confidence_cap
    assert confidence <= cap
    assert any("Exhaustion cap" in r for r in reasons)
    assert grade != "A+"


def test_healthy_adx_is_not_capped():
    _, confidence, reasons = _grade(adx=30.0)
    assert not any("Exhaustion cap" in r for r in reasons)
    assert confidence > get_settings().ut_exhaustion_confidence_cap


# ── Fix C: pre-candle flip veto wiring ──

def test_pre_candle_flip_veto_wired_default_off_with_telemetry():
    source = inspect.getsource(SignalProcessor.process_best_signal)
    assert "pre_candle_flip_veto_enabled" in source
    assert "REJECTED - Pre-Candle Flip Veto" in source
    assert "pre-candle would-veto" in source
    settings = get_settings()
    assert settings.pre_candle_flip_veto_enabled is False  # opt-in gate
    assert settings.pre_candle_flip_veto_min_confidence == 0.70
