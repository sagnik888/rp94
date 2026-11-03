from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from engine.signal_manager import SignalManager
from intelligence.signal_learning import (
    SCORING_VERSION, build_artifact, clock, digest, evaluate, fit_model,
    freeze_decision, predict, shadow_assessment, target_policy, training_rows, valid_evidence,
)

IST = ZoneInfo("Asia/Kolkata")


def candidate(trend=.2, setup=.8, **values):
    fields = dict(instrument="NIFTY", timeframe="5min", inst_type="FUT", direction="LONG",
                  lifecycle_id="signal", signal_timestamp=datetime(2026, 9, 1, 10, tzinfo=IST),
                  confidence=setup, setup_score=setup, market_regime="TRENDING_UP",
                  source_engine="utbot", source_engines=["utbot"], source_lane="A", adx_value=25.,
                  grade_pillars=dict(scoring_version=SCORING_VERSION, trend_group=trend, nontrend=.025,
                                    intel=0., vix_penalty=0., vix_available=True, trend_adx=.025,
                                    trend_mtf=trend, regime=.30, trend_agreement=0.),
                  grading_inputs={"signal": {"raw_candle": {"open": 100., "close": 101.}}},
                  quality_verdict={"allowed": False, "reason": "independent eligibility gate"})
    fields.update(values)
    return SimpleNamespace(**fields)


def event(day=0, index=0, trend=.2, decision="REJECTED", source="utbot"):
    stamp = datetime(2026, 9, 1, 10, tzinfo=IST) + timedelta(days=day, minutes=index)
    setup = .95 - (trend + .3) * 1.3  # Deliberately wrong heuristic ranking.
    holder = candidate(trend, setup, lifecycle_id=f"s{source}-{day}-{index}", source_engine=source, source_engines=[source])
    frozen = freeze_decision(holder, decision, stamp)
    gross = (4*trend - .1) * 100
    return {"decision": "SHADOW_OUTCOME", "lifecycle_id": holder.lifecycle_id, "signal_learning": frozen,
            "contract": {"quantity": 10}, "geometry": {"underlying_stop": 90.},
            "shadow_outcome": {"horizon_minutes": 30, "decision_group": decision, "outcome_available": True,
                               "registered_at": stamp.isoformat(), "completed_at": (stamp + timedelta(minutes=30)).isoformat(),
                               "entry_quote_captured_at": stamp.isoformat(),
                               "exit_quote_captured_at": (stamp + timedelta(minutes=30)).isoformat(),
                               "entry_quote_age_seconds": 0., "exit_quote_age_seconds": 0.,
                               "entry_executable_price": 100., "exit_executable_price": 100. + gross/10,
                               "gross_pnl_rs": gross, "round_trip_cost_rs": 2.}}


def sample_events(days=12, per_day=20):
    return [event(d, j, -.3 + .6*j/(per_day-1), "ACCEPTED" if j%2 else "REJECTED")
            for d in range(days) for j in range(per_day)]


def test_group_budget_stops_duplicate_trend_votes_and_preserves_other_evidence(monkeypatch):
    from config.settings import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "scanner_engine", "quad_prime")
    monkeypatch.setattr(settings, "ut_confidence_calibration_live_apply_enabled", False)
    raw = dict(instrument="NIFTY", timeframe="5min", signal_type="BUY", timestamp=datetime(2026,9,1,10),
               price=100., trailing_stop=90., stop_distance=10., atr_value=10., adx_value=25., raw_candle={},
               trend_agreement=0, source_engine="utbot", source_engines=["utbot"], source_lane="A")
    weak = SimpleNamespace(**raw)
    _, first, _ = SignalManager()._grade_signal(weak, 0., 0., "TRENDING_UP", vix_value=15.)
    redundant = SimpleNamespace(**{**raw, "trend_agreement": 3, "adx_value": 35.})
    _, second, _ = SignalManager()._grade_signal(redundant, 1., 0., "TRENDING_UP", vix_value=15.)
    assert first == second and first < .85
    assert redundant.grade_pillars["trend_raw"] > .30
    assert redundant.grade_pillars["trend_group"] == .30
    supported = SimpleNamespace(**raw)
    _, better, _ = SignalManager()._grade_signal(supported, 0., .8, "TRENDING_UP", vix_value=15.)
    assert better > first


def test_legacy_calibration_cannot_relabel_new_setup_score_or_override_caps(monkeypatch):
    from config.settings import get_settings
    calls = []
    from intelligence.confidence_calibration import CalibratedConfidence
    monkeypatch.setattr("intelligence.confidence_calibration.calibrate", lambda c,*a,**k: calls.append(c) or CalibratedConfidence(.97))
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime")
    signal = SimpleNamespace(instrument="NIFTY", timeframe="5min", signal_type="BUY", timestamp=datetime(2026,9,1,10),
                             price=100., trailing_stop=90., stop_distance=10., atr_value=10., adx_value=50., raw_candle={},
                             trend_agreement=3, source_engine="utbot", source_lane="A")
    grade, score, _ = SignalManager()._grade_signal(signal, 1., 1., "TRENDING_UP")
    assert calls == [] and score == .82 and grade == "A"
    assert signal.calibrated_win_probability is None
    assert not signal.confidence_calibrated and signal.setup_score == score


def test_frozen_snapshot_is_detached_and_hash_detects_mutation():
    holder = candidate()
    frozen = freeze_decision(holder, "REJECTED", datetime(2026,9,1,10,tzinfo=IST))
    holder.grade_pillars["trend_group"] = -.3
    holder.quality_verdict["allowed"] = True
    assert frozen["features"]["trend_group"] == .2 and not frozen["eligibility"]["allowed"]
    assert valid_evidence(frozen)
    frozen["features"]["trend_group"] = .9
    assert not valid_evidence(frozen)


def test_target_net_r_uses_identical_fee_slippage_rules_and_deduplicates():
    taken, refused = event(decision="ACCEPTED"), event(decision="REJECTED")
    rows, skips = training_rows([taken, refused, deepcopy(refused)])
    assert len(rows) == 2 and skips == {"duplicate_outcome": 1}
    assert rows[0]["net_r"] == rows[1]["net_r"]
    assert rows[0]["net_pnl_rs"] == pytest.approx(70. - 2. - .207)
    assert rows[0]["risk_rs"] == 100
    assert rows[0]["policy_hash"] == digest(target_policy())


@pytest.mark.parametrize("damage,reason", [
    ("missing_snapshot", "missing_or_mutated_evidence"),
    ("future_decision", "invalid_decision_clock"),
    ("missing_risk", "missing_execution_or_risk"),
    ("late_entry", "outside_execution_window"),
    ("late_exit", "outside_execution_window"),
    ("wrong_pnl", "inconsistent_execution_pnl"),
    ("unavailable", "outcome_unavailable:quote_missing"),
    ("legacy", "different_scoring_version"),
])
def test_invalid_training_evidence_is_excluded_with_reason(damage, reason):
    row = event()
    if damage == "missing_snapshot": row.pop("signal_learning")
    elif damage == "future_decision":
        row["signal_learning"]["decision_at"] = "2026-09-02T10:00:00+05:30"
        row["signal_learning"].pop("fingerprint")
        row["signal_learning"]["fingerprint"] = digest(row["signal_learning"])
    elif damage == "missing_risk": row["geometry"] = {}
    elif damage == "late_entry": row["shadow_outcome"]["entry_quote_captured_at"] = "2026-09-01T10:02:00+05:30"
    elif damage == "late_exit": row["shadow_outcome"]["exit_quote_captured_at"] = "2026-09-01T10:35:00+05:30"
    elif damage == "wrong_pnl": row["shadow_outcome"]["gross_pnl_rs"] = 1000.
    elif damage == "unavailable": row["shadow_outcome"].update(outcome_available=False, unavailable_reason="quote_missing")
    elif damage == "legacy":
        row["signal_learning"]["scoring_version"] = "legacy"
        row["signal_learning"].pop("fingerprint")
        row["signal_learning"]["fingerprint"] = digest(row["signal_learning"])
    rows, skips = training_rows([row])
    assert rows == [] and skips == {reason: 1}


def test_fitted_model_repairs_inverted_ranking_without_claiming_live_eligibility():
    rows, _ = training_rows(sample_events(days=6))
    model = fit_model(rows, target_policy(), min_samples=40, min_segment_samples=5)
    assert model and model["fit_samples"] and model["calibration_samples"]
    low, high = training_rows([event(7,0,-.3), event(7,19,.3)])[0]
    assert low["setup_score"] > high["setup_score"]
    assert predict(model, high)["expected_net_r"] is None  # unvalidated model abstains
    low_pred, high_pred = [predict(model, row, require_validated=False) for row in (low, high)]
    assert high_pred["expected_net_r"] > low_pred["expected_net_r"]
    assert high_pred["calibrated_win_probability"] > low_pred["calibrated_win_probability"]
    assert not high_pred["live_eligible"] and high_pred["mode"] == "SHADOW_ONLY"
    for altered in ({"source_lane": "UTS"}, {"inst_type": "OPT"}, {"regime": "RANGING"},
                    {"decision_at": rows[0]["decision_at"]}, {"source_engine": "ema"}):
        assert predict(model, {**high, **altered}, require_validated=False)["expected_net_r"] is None


def test_chronological_evaluation_has_untouched_days_and_compares_equal_budgets():
    rows, _ = training_rows(sample_events())
    report = evaluate(rows, min_samples=40, min_segment_samples=5)
    assert report["mode"] == "SHADOW_ONLY" and not report["live_eligible"]
    assert report["probability"]["brier"] < report["probability_baseline"]["brier"]
    assert report["ranking"]["samples"] == report["ranking_baseline"]["samples"]
    assert report["ranking"]["mean_net_r"] > report["ranking_baseline"]["mean_net_r"]
    assert report["ranking"]["max_drawdown_r"] <= report["ranking_baseline"]["max_drawdown_r"]
    assert len(report["folds"]) == 8
    assert report["validation_passed"]
    artifact = build_artifact(rows, report, min_samples=40, min_segment_samples=5)
    assert artifact["models"] and not artifact["live_eligible"]


def test_future_outcome_and_repeated_lifecycle_are_purged_from_training():
    rows, _ = training_rows(sample_events())
    ordinary = evaluate(rows, min_samples=40, min_segment_samples=5)
    contaminated = deepcopy(rows)
    for row in contaminated:
        if row["session"] == "2026-09-01": row["outcome_at"] = "2026-10-01T12:00:00+05:30"
    checked = evaluate(contaminated, min_samples=40, min_segment_samples=5)
    assert checked["folds"][0]["train_samples"] == ordinary["folds"][0]["train_samples"] - 20
    for row in contaminated:
        if row["session"] == "2026-09-01": row["lifecycle_id"] = contaminated[-1]["lifecycle_id"]
    checked = evaluate(contaminated, min_samples=40, min_segment_samples=5)
    assert checked["mode"] == "SHADOW_ONLY"


def test_sparse_model_absent_or_invalid_file_never_invents_probability(tmp_path):
    rows, _ = training_rows(sample_events(days=2))
    assert fit_model(rows, target_policy()) is None
    frozen = event()["signal_learning"]
    path = tmp_path / "model.json"
    assert shadow_assessment(frozen, path)["status"] == "UNVALIDATED"
    path.write_text('broken json')
    assert shadow_assessment(frozen, path)["calibrated_win_probability"] is None
    path.write_text('[]')
    assert shadow_assessment(frozen, path)["calibrated_win_probability"] is None


def test_repeated_groups_do_not_create_extra_independent_samples():
    events = sample_events(days=6)
    paired = []
    for value in events:
        copied = deepcopy(value)
        group = "ACCEPTED" if copied["signal_learning"]["decision_group"] == "REJECTED" else "REJECTED"
        copied["shadow_outcome"]["decision_group"] = group
        copied["signal_learning"]["decision_group"] = group
        copied["signal_learning"].pop("fingerprint")
        copied["signal_learning"]["fingerprint"] = digest(copied["signal_learning"])
        paired.extend((value, copied))
    rows, _ = training_rows(paired)
    assert len(rows) == 240 and len({row["lifecycle_id"] for row in rows}) == 120
    assert fit_model(rows, target_policy(), min_samples=200, min_segment_samples=5) is None


def test_touch_freshness_and_out_of_range_features_cannot_receive_validated_estimate():
    stale = event()
    stale["shadow_outcome"]["exit_quote_age_seconds"] = 15.
    assert training_rows([stale])[1] == {"missing_or_stale_touch_clock": 1}
    rows, _ = training_rows(sample_events(days=6))
    model = fit_model(rows, target_policy(), min_samples=40, min_segment_samples=5)
    later = training_rows([event(7,0)])[0][0]
    later["features"]["vix_penalty"] = .4
    assert predict(model, later, require_validated=False)["status"] == "UNVALIDATED"


def test_shadow_model_loads_only_validated_past_model_and_cache_invalidates(tmp_path):
    rows, _ = training_rows(sample_events())
    report = evaluate(rows, min_samples=40, min_segment_samples=5)
    artifact = build_artifact(rows, report, min_samples=40, min_segment_samples=5)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(artifact))
    evidence = event(13,19,.3)["signal_learning"]
    result = shadow_assessment(evidence, path)
    assert result["status"] == "SHADOW_VALIDATED" and result["expected_net_r"] > 0
    assert not result["live_eligible"]
    path.write_text(json.dumps({"models": {}}))
    assert shadow_assessment(evidence, path)["status"] == "UNVALIDATED"


def test_scanner_embeds_frozen_evidence_in_real_shadow_outcome(tmp_path, monkeypatch):
    from scanner import Scanner
    # Reuse the existing executable-touch harness to exercise production hooks.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("shadow_harness", Path(__file__).with_name("test_entry_policy_shadow_expectancy_2026_08_27.py"))
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    import scanner as module
    settings = harness._shadow_settings(tmp_path)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    scanner = harness._make_shadow_scanner(monkeypatch, tmp_path, "L-learning")
    holder = harness._shadow_candidate()
    holder.grade_pillars = candidate().grade_pillars
    holder.grading_inputs = candidate().grading_inputs
    holder.setup_score = .8
    holder.source_engine = "utbot"
    holder.source_engines = ["utbot"]
    holder.adx_value = 25.
    scanner._register_entry_shadow_outcome(holder, "REJECTED")
    watch = next(iter(scanner._entry_shadow_book.values()))
    frozen = deepcopy(watch["signal_learning"])
    holder.grade_pillars["trend_group"] = -.3
    scanner._register_entry_shadow_outcome(holder, "REJECTED")
    assert next(iter(scanner._entry_shadow_book.values()))["signal_learning"] == frozen
    now = clock(watch["registered_at"]) + timedelta(minutes=15)
    assert Scanner._record_entry_shadow_outcome(scanner, watch, horizon_minutes=15, now=now,
        quote={"bid": 110., "ask":111., "age_seconds":0.1}, outcome_available=True)
    recorded = scanner.rtracker.events[-1]
    assert recorded["signal_learning"] == frozen
    assert recorded["shadow_outcome"]["exit_quote_captured_at"]
