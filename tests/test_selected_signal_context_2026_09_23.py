"""Exercise the actual grading -> routing/veto seam with opposing TF contexts."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import engine.signal_processor as processor_module
from engine.signal_processor import SignalProcessor
from trading.trade_manager import IST


class VetoReached(Exception):
    pass


def _run_to_veto(monkeypatch, *, own_regime, own_score=0.65, own_context=True,
                 signal_type="BUY", reject_before_selection=False):
    now = datetime(2026, 9, 23, 10, 15, 10)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(now) if tz else now

    settings = SimpleNamespace(
        scanner_engine="quad_prime", ut_timeframe_entry_policy="INCLUDE_5MIN",
        volume_spike_entry_enabled=False, one_min_scalping_enabled=False,
        get_signal_fire_mode=lambda: "CANDLE_CLOSE", regime_veto_enabled=True,
        regime_veto_adx_floor=18.0,
    )
    monkeypatch.setattr(processor_module, "datetime", Clock)
    monkeypatch.setattr(processor_module, "get_settings", lambda: settings)
    monkeypatch.setattr(processor_module, "_scanner_is_expiry_day", lambda *a: False)
    monkeypatch.setattr(processor_module, "build_identity", lambda *a: {})
    signal = SimpleNamespace(
        instrument="NIFTY", timeframe="15min", timestamp=now.replace(minute=0, second=0),
        signal_type=signal_type, price=25000.0, trailing_stop=24950.0,
        adx_value=30.0, atr_value=25.0,
    )
    caller_regime = "TRENDING_DOWN" if own_regime == "TRENDING_UP" else "TRENDING_UP"
    caller_score = -0.8
    caller_bundle = {"greeks": {"iv_percentile": 90.0}, "session_context": {"day_type": "RANGE_DAY"}}
    own_bundle = {"greeks": {"iv_percentile": 35.0}, "session_context": {"day_type": "TREND_DAY"}}
    grade_signal = Mock(return_value=("A", 0.8, ["graded evidence"]))
    route = Mock(return_value="FUT")
    power = Mock(return_value=None)
    relief = Mock(return_value=None)
    scanner = SimpleNamespace(
        mode="REAL", inst_pref="FUT", risk_manager=SimpleNamespace(can_trade=lambda *a: True),
        _last_signal_time={"NIFTY_15min": signal.timestamp - timedelta(seconds=1)},
        _vix_data={signal.timestamp.strftime("%Y-%m-%d %H:%M:00"): 15.0},
        signals=SimpleNamespace(_grade_signal=grade_signal),
        trades=SimpleNamespace(open_trades={}, can_open_trade=lambda: (True, "")),
        _handle_opposite_signal_exit=lambda *a: reject_before_selection,
        _detect_power_move=power, _entry_reversal_relief_context=relief,
        _resolve_instrument_type=route, log_event=lambda *a: None,
        log_signal_decision_once=lambda *a: None,
    )
    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = scanner
    processor._build_timeframe_contexts = lambda *a: (
        {"15min": {"intel_result": own_bundle, "intel_score": own_score, "regime": own_regime}}
        if own_context else None
    )
    mtf = SimpleNamespace(results_5min=None,
        results_15min={"signals": [signal], "state": {"position": 1}}, confluence_score=0.7)
    captured = {}
    real_veto = processor_module.directional_fut_veto

    def stop_at_veto(settings, **kwargs):
        captured.update(kwargs)
        captured["reason"] = real_veto(settings, **kwargs)
        raise VetoReached

    monkeypatch.setattr(processor_module, "directional_fut_veto", stop_at_veto)
    args = ("NIFTY", mtf, caller_bundle, caller_score, caller_regime, 1, 65, {}, 25000, 25000)
    if reject_before_selection:
        captured["rejected"] = processor.process_best_signal(*args)[0]
    else:
        with pytest.raises(VetoReached):
            processor.process_best_signal(*args)
    return SimpleNamespace(
        captured=captured, grade=grade_signal, route=route, power=power, relief=relief,
        caller_regime=caller_regime, caller_score=caller_score, own_bundle=own_bundle,
    )


@pytest.mark.parametrize("signal_type,own_regime,should_veto", [
    ("BUY", "TRENDING_UP", False), ("BUY", "TRENDING_DOWN", True),
    ("SELL", "TRENDING_DOWN", False), ("SELL", "TRENDING_UP", True),
])
def test_selected_15m_context_reaches_routing_and_real_futures_veto(
    monkeypatch, signal_type, own_regime, should_veto,
):
    run = _run_to_veto(monkeypatch, own_regime=own_regime, signal_type=signal_type)
    assert run.grade.call_args.args[2:4] == (0.65, own_regime)
    assert run.route.call_args.kwargs["regime"] == own_regime
    assert run.route.call_args.kwargs["intel_score"] == pytest.approx(0.65)
    assert run.route.call_args.kwargs["iv_percentile"] == 35.0
    assert run.route.call_args.kwargs["adx_value"] == 30.0
    assert run.power.call_args.kwargs["intel_score"] == pytest.approx(0.65)
    assert run.relief.call_args.kwargs["regime"] == own_regime
    assert bool(run.captured["reason"]) is should_veto


def test_neutral_own_timeframe_score_does_not_reuse_caller_score(monkeypatch):
    run = _run_to_veto(monkeypatch, own_regime="TRENDING_UP", own_score=0.0)
    assert run.route.call_args.kwargs["intel_score"] == 0.0
    assert run.power.call_args.kwargs["intel_score"] == 0.0


def test_legacy_grading_context_remains_the_routing_context(monkeypatch):
    run = _run_to_veto(monkeypatch, own_regime="TRENDING_UP", own_context=False)
    assert run.grade.call_args.args[2:4] == (run.caller_score, run.caller_regime)
    assert run.route.call_args.kwargs["regime"] == run.caller_regime
    assert run.route.call_args.kwargs["intel_score"] == run.caller_score
    assert run.route.call_args.kwargs["iv_percentile"] == 90.0


def test_preselection_rejection_carries_its_own_grade_context(monkeypatch):
    run = _run_to_veto(monkeypatch, own_regime="TRENDING_UP", reject_before_selection=True)
    rejected = run.captured["rejected"]
    assert rejected.market_regime == "TRENDING_UP"
    assert rejected.intelligence_score == pytest.approx(0.65)
    assert rejected.market_intelligence_context["session_context"] == run.own_bundle["session_context"]


@pytest.mark.parametrize("sibling_inputs", [{"signal": {"timeframe": "5min"}}, None])
def test_cloned_sibling_never_inherits_the_winners_regrade_snapshot(sibling_inputs):
    winner = SimpleNamespace(
        direction="LONG", timeframe="15min", grade="A", inst_type="OPT",
        signal_timestamp=datetime(2026, 9, 23, 10),
        grading_inputs={"signal": {"timeframe": "15min"}},
    )
    signal = SimpleNamespace(signal_type="BUY", timestamp=winner.signal_timestamp,
                             grading_inputs=sibling_inputs)
    processor = SignalProcessor.__new__(SignalProcessor)
    sibling = processor._clone_cascade_tf_sibling(
        winner, sig=signal, tf="5min", grade="A", confidence=0.8,
        reasons=[], scanner_engine_mode="quad_prime",
    )
    assert sibling.grading_inputs == sibling_inputs
    if sibling_inputs is not None:
        assert sibling.grading_inputs is not sibling_inputs
