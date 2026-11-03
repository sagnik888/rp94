"""Behavior regressions for the raw grading/admission audit."""
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config.settings import get_settings
from engine.candidate_selection import rank_entry_candidates
from engine.quality_verdict import QualityInputs, evaluate_quality
from engine.signal_manager import SignalManager
from engine.signal_processor import SignalProcessor, carry_grading_telemetry
from engine.signal_quality_policy import power_move_confirmed, volume_spike_entry_confirmed
from intelligence.confidence_calibration import ConfidenceCalibrator, _isotonic_pava, calibrate, load_calibrator
from scanner import Scanner


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    for name, value in {
        "scanner_engine": "quad_prime", "ut_confidence_logistic": True,
        "ut_confidence_logistic_k": 5., "ut_dynamic_confidence": True,
        "ut_exhaustion_confidence_cap": .82, "utbot_prime_confidence_per_agreement": .03,
        "utbot_prime_max_confidence_bonus": .09, "ut_confidence_calibration_live_apply_enabled": False,
        "power_move_override_enabled": True, "power_move_adx_threshold": 20.,
        "power_move_volume_mult": 2.5, "power_move_range_atr_mult": 2.,
        "power_move_momentum_volume_mult": 3.5, "volume_spike_entry_enabled": True,
        "one_min_scalping_high_burst_only": True, "volume_spike_entry_min_volume_ratio": 2.5,
        "option_chain_entry_max_age_seconds": 120., "stabilization_regrade_enabled": True,
        "stale_regime_reversal_enabled": False,
    }.items():
        monkeypatch.setattr(s, name, value)
    return s


def _signal(side="BUY", **values):
    defaults = dict(instrument="NIFTY", timeframe="5min", signal_type=side,
        timestamp=datetime(2026, 10, 1, 10), price=100., trailing_stop=95.,
        stop_distance=10., atr_value=10., adx_value=30., trend_agreement=0,
        raw_candle={}, source_engine="multi", source_lane="A",
        source_engines=["utbot", "ema"])
    defaults.update(values)
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("agreement", [0, 1, 3])
def test_exhaustion_cap_survives_all_agreement_bonuses(settings, side, agreement):
    signal = _signal(side, adx_value=50., trend_agreement=agreement)
    sign = 1 if side == "BUY" else -1
    grade, confidence, reasons = SignalManager()._grade_signal(
        signal, sign, sign, "TRENDING_UP" if sign == 1 else "TRENDING_DOWN")
    assert confidence == .82 and grade == "A"
    assert any("caps confidence at 82%" in r for r in reasons)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("body", [0., .1, .3, .4, .5])
def test_small_body_with_favorable_wick_has_no_trap_penalty(settings, side, body):
    candle = dict(open=100-body, high=101., low=81., close=100.)
    if side == "SELL":
        candle = dict(open=100+body, high=119., low=99., close=100.)
    _, confidence, reasons = SignalManager()._grade_signal(
        _signal(side, raw_candle=candle), 0., 0., "TRENDING_UP" if side == "BUY" else "TRENDING_DOWN")
    assert confidence == pytest.approx(.8354835371)
    assert not any("Wick Detected" in r for r in reasons)


@pytest.mark.parametrize("side,candle", [
    ("BUY", dict(open=100., close=100., high=119., low=99.)),
    ("SELL", dict(open=100., close=100., high=101., low=81.)),
])
def test_dominant_adverse_doji_wick_still_penalized(settings, side, candle):
    _, _, reasons = SignalManager()._grade_signal(_signal(side, raw_candle=candle), 0., 0., "RANGING")
    assert any("Wick Detected" in r for r in reasons)


@pytest.mark.parametrize("side,sign", [("BUY", 1.), ("SELL", -1.)])
def test_momentum_intelligence_must_support_signal_direction(settings, side, sign):
    power = dict(volume_ratio=2.8, range_atr_ratio=2.1, close_location=.9,
        adx_value=18., trend_15m_agrees=True, settings=settings, signal_type=side)
    spike = dict(timeframe="1min", volume_ratio=3., range_atr_ratio=2.,
        close_location=.9, adx_value=18., settings=settings, signal_type=side)
    assert power_move_confirmed(**power, intel_score=.9*sign)
    assert not power_move_confirmed(**power, intel_score=-.9*sign)
    assert volume_spike_entry_confirmed(**spike, intel_score=.9*sign)
    assert not volume_spike_entry_confirmed(**spike, intel_score=-.9*sign)


def test_withdrawn_source_and_structural_flags_no_longer_admit_setup(settings):
    raw = _signal(impulse_confirmed=True, is_explosive_bypass=True, power_move=True)
    grade, confidence, reasons = SignalManager()._grade_signal(raw, 0., .5, "RANGING")
    candidate = SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG",
        inst_type="OPT", signal_timestamp=raw.timestamp, grading_inputs=raw.grading_inputs,
        source_engine="multi", source_lane="A", source_engines=["utbot", "ema"],
        quad_union_entry=True, grade=grade, confidence=confidence, reasons=reasons,
        impulse_confirmed=True, is_explosive_bypass=True, power_move=True, data_provenance="LIVE")
    current = _signal(source_engine="utbot", source_engines=["utbot"])
    intel = dict(aggregate={"score_norm": .5}, regime={"regime": "RANGING"}, volume={"available": False})
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._cached_results = {"NIFTY_5min": {"signals": [current]}}
    scanner._diag_inc = lambda *a, **k: None
    scanner._fresh_exact_trade_intel = lambda *a, **k: intel
    scanner._candidate_market_intelligence_context = lambda c: c.market_intelligence_context
    scanner._candidate_15m_trend_agrees = lambda c: True
    telemetry = scanner._regrade_matured_candidate(candidate)
    verdict = scanner._quality_verdict_for_candidate(candidate, regime="RANGING")
    assert telemetry["setup_refreshed"]
    assert candidate.source_engines == ["utbot"]
    assert not any(getattr(candidate, name) for name in ("impulse_confirmed", "is_explosive_bypass", "power_move"))
    assert verdict.checks["source_count"] == 1 and not verdict.allowed


def test_chain_age_advances_while_buffered_and_new_observation_replaces_it(settings, monkeypatch):
    import scanner as module
    clock = [1_000_000.]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    scanner = Scanner.__new__(Scanner)
    candidate = SimpleNamespace(instrument="NIFTY", inst_type="OPT", iv_percentile=10.,
        chain_quality={"age_seconds": 110., "captured_at_epoch": clock[0], "stale": False})
    assert not scanner._option_chain_entry_freshness_blocker(candidate, "TRENDING_UP")
    clock[0] += 30
    assert "140.0s" in scanner._option_chain_entry_freshness_blocker(candidate, "TRENDING_UP")
    scanner._chain_quality_cache = {"NIFTY": {"age_seconds": 0., "fetched_at_epoch": clock[0]-1, "stale": False}}
    assert not scanner._option_chain_entry_freshness_blocker(candidate, "TRENDING_UP")
    assert candidate.chain_quality["fetched_at_epoch"] == clock[0]-1


@pytest.mark.parametrize("field", ["fetched_at_epoch", "captured_at_epoch"])
def test_chain_clock_rejects_future_or_nonfinite_evidence(settings, monkeypatch, field):
    import scanner as module
    monkeypatch.setattr(module.time, "time", lambda: 1_000_000.)
    scanner = Scanner.__new__(Scanner)
    for timestamp in (float('nan'), float('inf'), 1_000_001.):
        candidate = SimpleNamespace(instrument="NIFTY", inst_type="OPT", chain_quality={"age_seconds": 0., field: timestamp})
        assert "invalid" in scanner._option_chain_entry_freshness_blocker(candidate, "TRENDING_UP")


def test_isotonic_equal_confidences_are_order_invariant():
    for outcomes in set(permutations([0., 0., 1., 1.])):
        thresholds, values = _isotonic_pava([.5]*4, outcomes)
        assert ConfidenceCalibrator(thresholds, values, 4).transform(.5) == .5


def test_isotonic_pooled_training_points_use_their_block():
    thresholds, values = _isotonic_pava([.1, .2, .3], [0., 1., 0.])
    model = ConfidenceCalibrator(thresholds, values, 3)
    assert [model.transform(x) for x in [.1, .2, .3]] == [0., .5, .5]
    assert [model.transform(x/100) for x in range(101)] == sorted(model.transform(x/100) for x in range(101))


def test_calibration_reports_actual_application_and_ignores_invalid_rows(tmp_path, settings, monkeypatch):
    path = tmp_path/'outcomes.csv'
    path.write_text('instrument,timeframe,regime,confidence,pnl\nNIFTY,5min,TRENDING_UP,0.5,-1\nNIFTY,5min,TRENDING_UP,0.5,1\nNIFTY,5min,TRENDING_UP,nan,1\nNIFTY,5min,TRENDING_UP,0.8,inf\n')
    for k,v in {"ut_confidence_calibration_enabled": True, "ut_confidence_calibration_live_apply_enabled": True,
                "ut_confidence_calibration_min_samples": 2, "ut_confidence_calibration_min_segment_samples": 2}.items():
        monkeypatch.setattr(settings,k,v)
    kwargs = dict(instrument="NIFTY", timeframe="5min", regime="TRENDING_UP")
    fitted = calibrate(.5, settings, str(path), **kwargs)
    assert fitted == .5 and fitted.calibration_applied
    assert load_calibrator(str(path), min_samples=2).n == 2
    raw = calibrate(.8, settings, str(tmp_path/'missing.csv'), **kwargs)
    assert raw == .8 and not getattr(raw, "calibration_applied", False)
    inputs = QualityInputs(timeframe="5min", inst_type="FUT", confidence=.8, regime="TRENDING_UP")
    assert evaluate_quality(inputs, settings).checks["confidence_semantics"] == "heuristic_score"
    assert evaluate_quality(QualityInputs(**{**inputs.__dict__, "confidence_calibrated": True}),settings).checks["confidence_semantics"] == "calibrated_probability"


def test_grading_provenance_reaches_candidate_and_replay(settings, monkeypatch):
    from intelligence.confidence_calibration import CalibratedConfidence
    monkeypatch.setattr("intelligence.confidence_calibration.calibrate", lambda *a, **k: CalibratedConfidence(.75))
    signal = _signal()
    grade, confidence, _ = SignalManager()._grade_signal(signal, 0., 0., "TRENDING_UP")
    candidate = SimpleNamespace()
    carry_grading_telemetry(candidate, signal)
    assert not candidate.confidence_calibrated
    assert candidate.setup_score == confidence
    assert candidate.calibrated_win_probability is None  # old curve lacks versioned full-funnel evidence
    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = SimpleNamespace(mode="HISTORICAL")
    verdict = processor._quality_verdict_for_signal(signal, tf="5min", grade=grade,
        confidence=confidence, regime="TRENDING_UP", inst_type="FUT", settings=settings,
        trend_15m_agrees=True)
    assert not verdict.checks["confidence_calibrated"]
    signal.trend_agreement = 1
    SignalManager()._grade_signal(signal, 0., 0., "TRENDING_UP")
    assert not signal.confidence_calibrated
    assert signal.calibrated_win_probability is None


def test_native_uts_selection_keeps_recency(settings):
    old = _signal(timestamp=datetime(2026,10,1,10), source_lane="UTS")
    new = _signal(timestamp=datetime(2026,10,1,10,5), source_lane="UTS")
    rows=[dict(sig=old,tf="5min",grade="A+",confidence=.99,reasons=[]),
          dict(sig=new,tf="5min",grade="C",confidence=.1,reasons=[])]
    assert rank_entry_candidates(rows, lambda s: s.source_lane)[0][1] is new


@pytest.mark.parametrize("capacity", [True, False])
def test_candidate_veto_retries_alternative_but_capacity_halt_stops(settings, monkeypatch, capacity):
    import engine.signal_processor as module
    old, new = _signal(timeframe="15min"), _signal(timestamp=datetime(2026,10,1,10,10))
    # The stronger candidate fails its own veto; the lower-ranked setup remains valid.
    old.graded_regime, new.graded_regime = "TRENDING_DOWN", "TRENDING_UP"
    old.graded_intel_score, new.graded_intel_score = -.5, .5
    rows=[dict(sig=old,tf="15min",grade="A+",confidence=.95,reasons=[]),
          dict(sig=new,tf="5min",grade="A",confidence=.8,reasons=[])]
    prepared=dict(timeframe_contexts=None,scanner_engine_mode="quad_prime",timeframe_policy="INCLUDE_5MIN",
        policy_timeframes={"5min","15min"},candidate_timeframes=["5min","15min"], live=True,
        volume_spike=False,one_min_scalp=False,reversion_enabled=False,reversion_window=None)
    route=Mock(return_value="FUT"); opposite=Mock(return_value=False)
    scanner=SimpleNamespace(mode="REAL",inst_pref="FUT",signals=SimpleNamespace(),
        trades=SimpleNamespace(open_trades={},can_open_trade=lambda:(capacity,"")),
        risk_manager=SimpleNamespace(can_trade=lambda *a:True),_handle_opposite_signal_exit=opposite,
        _detect_power_move=lambda *a,**k:None,_entry_reversal_relief_context=lambda *a,**k:None,
        _resolve_instrument_type=route,log_event=lambda *a:None,log_signal_decision_once=lambda *a:None)
    processor=SignalProcessor.__new__(SignalProcessor);processor.scanner=scanner
    processor._prepare_signal_pipeline_inputs=Mock(return_value=prepared)
    processor._collect_signal_candidates=Mock(return_value=rows)
    processor._append_adaptive_dual_apex_candidates=lambda **k:None
    monkeypatch.setattr(module,"build_identity",lambda *a:{})
    monkeypatch.setattr(module,"_scanner_is_expiry_day",lambda *a:False)
    monkeypatch.setattr(settings,"regime_veto_enabled",True)
    class AlternativeReached(Exception): pass
    actual=module.directional_fut_veto
    def veto(settings,**kwargs):
        reason=actual(settings,**kwargs)
        if not reason: raise AlternativeReached
        return reason
    monkeypatch.setattr(module,"directional_fut_veto",veto)
    args=("NIFTY",SimpleNamespace(results_15min={"state":{"position":1}},confluence_score=.7),
          {},0.,"RANGING",1,65,{},25000.,25000.)
    if capacity:
        with pytest.raises(AlternativeReached): processor.process_best_signal(*args)
        assert [call.kwargs["timeframe"] for call in route.call_args_list]==["15min","5min"]
    else:
        result=processor.process_best_signal(*args)
        assert result[0].status=="REJECTED - Max Trades" and not route.called
    assert processor._collect_signal_candidates.call_count==1
    assert opposite.call_count==2  # once per raw signal, never repeated by fallback
