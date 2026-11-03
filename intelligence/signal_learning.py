"""Frozen decision evidence and chronological, shadow-only signal learning.

The target is a marketable entry/exit at a fixed horizon, net of the stored fee
and a symmetric slippage scenario. It is not the strategy's stop/target outcome.
The existing quality/execution policy remains the sole live eligibility owner.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np

SCHEMA = "signal_evidence.v1"
MODEL_SCHEMA = "signal_learning.v1"
SCORING_VERSION = "bounded_evidence.v2"
NUMERIC = ("trend_group", "nontrend", "intel", "vix_penalty", "source_count",
           "adx_available", "vix_available", "candle_available", "trend_conflict")
CATEGORICAL = ("instrument", "timeframe", "regime", "source_engine", "source_set", "source_lane", "inst_type", "direction")
DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[1] / "data_store/signal_learning_model.json"
_MODEL_CACHE = {}


def clock(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def target_policy(horizon_minutes=30, slippage_bps=2.0):
    if int(horizon_minutes) <= 0 or not math.isfinite(float(slippage_bps)) or slippage_bps < 0:
        raise ValueError("Positive horizon and finite nonnegative slippage required")
    return {"version": "fixed_touch_net_r.v1", "horizon_minutes": int(horizon_minutes),
            "entry_grace_seconds": 30, "exit_grace_seconds": 180,
            "quote_max_age_seconds": 10,
            "round_trip_slippage_bps": float(slippage_bps),
            "entry": "marketable_touch", "exit": "marketable_touch_at_horizon",
            "fees": "recorded_round_trip_cost_rs", "positive": "net_r>0"}


def admission_evidence(candidate, decision_group):
    """Detach the authoritative admission, separate from generic quality evidence.

    A native lane may intentionally treat generic quality as nonbinding. Its
    canonical admission and execution safety verdict must remain visible without
    rewriting the legacy ``eligibility`` field or inventing missing approvals.
    """
    get = lambda name, default=None: (candidate.get(name, default) if isinstance(candidate, dict)
                                      else getattr(candidate, name, default))
    current = get("decision_record", {}) or {}
    current = deepcopy(current) if isinstance(current, dict) else {}
    accepted = get("accepted_decision_record", {}) or {}
    accepted = deepcopy(accepted) if isinstance(accepted, dict) else {}
    group = str(decision_group or "").upper()
    record = accepted if accepted and group != "REJECTED" else current
    from engine.decision_record import policy_owner
    owner, quality_status = policy_owner(candidate if not isinstance(candidate, dict)
                                          else SimpleNamespace(**candidate))
    status = str(record.get("status") or "NOT_APPLICABLE").upper()
    execution = get("entry_policy_verdict", {}) or {}
    return {
        "schema": "signal_admission.v1", "decision_group": group,
        "policy_owner": str(record.get("policy_owner") or owner),
        "status": status, "allowed": True if status == "ALLOW" else False if status == "BLOCK" else None,
        "generic_quality_status": str(record.get("quality_policy_status") or quality_status),
        "decision_record": record,
        "execution_policy": deepcopy(execution) if isinstance(execution, dict) else {},
    }


def freeze_decision(candidate, decision_group, observed_at):
    """Detach all model inputs before an outcome exists; hash their exact values."""
    get = lambda name, default=None: getattr(candidate, name, default)
    pillars = deepcopy(get("grade_pillars", {}) or {})
    raw = deepcopy((get("grading_inputs", {}) or {}).get("signal", {}) or {})
    parts = str(get("instrument", "") or "").split()
    sources = sorted(set(str(x).lower() for x in (get("source_engines", []) or []) if str(x).strip()))
    setup = number(get("setup_score", None))
    if setup is None:
        setup = number(get("confidence", None))
    features = {key: number(pillars.get(key)) for key in ("trend_group", "nontrend", "intel", "vix_penalty")}
    adx = number(get("adx_value", raw.get("adx_value")))
    features.update(source_count=len(sources) or int(bool(get("source_engine", ""))),
                    adx_available=int(adx is not None and adx > 0),
                    vix_available=int(bool(pillars.get("vix_available", False))),
                    candle_available=int(bool(raw.get("raw_candle"))))
    trend_parts = [number(pillars.get(k)) or 0 for k in ("trend_adx", "trend_mtf", "regime", "trend_agreement")]
    features["trend_conflict"] = min(sum(max(v, 0) for v in trend_parts), sum(max(-v, 0) for v in trend_parts))
    stamp = clock(observed_at)
    evidence = {
        "schema": SCHEMA, "scoring_version": str(pillars.get("scoring_version") or "legacy_unversioned"),
        "decision_at": stamp.isoformat() if stamp else "", "decision_group": str(decision_group).upper(),
        "lifecycle_id": str(get("lifecycle_id", "") or ""),
        "signal_timestamp": str(get("signal_timestamp", raw.get("timestamp", "")) or ""),
        "instrument": parts[0].upper() if parts else "", "timeframe": str(get("timeframe", "") or ""),
        "regime": str(get("market_regime", get("graded_regime", "UNKNOWN")) or "UNKNOWN"),
        "source_engine": str(get("source_engine", "") or "UNKNOWN"), "source_engines": sources,
        "source_set": "+".join(sources) or str(get("source_engine", "") or "UNKNOWN"),
        "source_lane": str(get("source_lane", "") or "UNKNOWN"), "inst_type": str(get("inst_type", "") or ""),
        "direction": str(get("direction", "") or ""), "setup_score": setup,
        "features": features, "pillars": pillars,
        "eligibility": deepcopy(get("quality_verdict", {}) or {}),
        "admission": admission_evidence(candidate, decision_group),
        "strategy_revision": str(get("strategy_revision", "") or ""),
        "strategy_logic_fingerprint": str(get("strategy_logic_fingerprint", "") or ""),
    }
    # The fingerprint excludes later predictions so those can be verified/replayed.
    evidence["fingerprint"] = digest(evidence)
    return evidence


def valid_evidence(evidence):
    if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA:
        return False
    frozen = {k: v for k, v in evidence.items() if k not in {"fingerprint", "assessment"}}
    try:
        return evidence.get("fingerprint") == digest(frozen)
    except (ValueError, TypeError):
        return False


def training_rows(events, policy=None):
    """Use only complete, comparable immutable evidence; account for every skip."""
    policy = policy or target_policy()
    rows, exclusions, seen = [], Counter(), set()
    for event in events:
        if not isinstance(event, dict):
            exclusions["malformed_event"] += 1
            continue
        if event.get("decision") != "SHADOW_OUTCOME":
            continue
        outcome = event.get("shadow_outcome") or {}
        evidence = event.get("signal_learning") or {}
        if not isinstance(outcome, dict):
            exclusions["malformed_shadow_outcome"] += 1
            continue
        if not isinstance(evidence, dict):
            exclusions["missing_or_mutated_evidence"] += 1
            continue
        reason = ""
        registered, completed = clock(outcome.get("registered_at")), clock(outcome.get("completed_at"))
        decision = clock(evidence.get("decision_at"))
        if outcome.get("horizon_minutes") != policy["horizon_minutes"]:
            reason = "different_target_horizon"
        elif not valid_evidence(evidence):
            reason = "missing_or_mutated_evidence"
        elif evidence.get("scoring_version") != SCORING_VERSION:
            reason = "different_scoring_version"
        elif not outcome.get("outcome_available"):
            reason = "outcome_unavailable:" + str(outcome.get("unavailable_reason") or "unknown")
        elif evidence.get("decision_group") not in {"ACCEPTED", "REJECTED"} or evidence.get("decision_group") != outcome.get("decision_group"):
            reason = "decision_group_mismatch"
        elif not decision or not registered or not completed or decision > registered or completed < registered:
            reason = "invalid_decision_clock"
        elif evidence.get("lifecycle_id") != event.get("lifecycle_id") or not evidence.get("lifecycle_id"):
            reason = "lifecycle_mismatch"
        elif any(number(evidence.get("features", {}).get(k)) is None for k in NUMERIC):
            reason = "missing_features"
        elif number(evidence.get("setup_score")) is None or not 0 <= evidence["setup_score"] <= 1:
            reason = "invalid_setup_score"
        elif any(not evidence.get(k) or evidence.get(k) == "UNKNOWN" for k in CATEGORICAL):
            reason = "missing_segment_identity"
        if reason:
            exclusions[reason] += 1
            continue
        entry, exit_price = number(outcome.get("entry_executable_price")), number(outcome.get("exit_executable_price"))
        quantity = number((event.get("contract") or {}).get("quantity"))
        fees = number(outcome.get("round_trip_cost_rs"))
        gross = number(outcome.get("gross_pnl_rs"))
        geometry = event.get("geometry") or {}
        stop = number(geometry.get("premium_stop") if evidence["inst_type"] == "OPT" else geometry.get("underlying_stop"))
        entry_at = clock(outcome.get("entry_quote_captured_at"))
        exit_at = clock(outcome.get("exit_quote_captured_at"))
        entry_age = number(outcome.get("entry_quote_age_seconds"))
        exit_age = number(outcome.get("exit_quote_age_seconds"))
        if None in (entry, exit_price, quantity, fees, gross, stop) or min(entry, exit_price, quantity, stop) <= 0 or fees < 0 or entry == stop:
            exclusions["missing_execution_or_risk"] += 1
            continue
        if entry_age is None or exit_age is None or min(entry_age, exit_age) < 0 or max(entry_age, exit_age) > policy["quote_max_age_seconds"]:
            exclusions["missing_or_stale_touch_clock"] += 1
            continue
        deadline = registered + timedelta(minutes=policy["horizon_minutes"])
        quote_age_limit = timedelta(seconds=policy["quote_max_age_seconds"])
        if not entry_at or not exit_at or exit_at > completed or not (registered - quote_age_limit <= entry_at <= registered + timedelta(seconds=policy["entry_grace_seconds"])) or not (deadline - quote_age_limit <= exit_at <= deadline + timedelta(seconds=policy["exit_grace_seconds"])):
            exclusions["outside_execution_window"] += 1
            continue
        expected_gross = (exit_price - entry) * quantity * (1 if evidence["inst_type"] == "OPT" or evidence["direction"] == "LONG" else -1)
        if not math.isclose(gross, expected_gross, abs_tol=.03):
            exclusions["inconsistent_execution_pnl"] += 1
            continue
        # One outcome per lifecycle and decision group. Multiple horizons, repeat
        # writes or restarts must not turn the same signal into extra observations.
        key = (evidence["lifecycle_id"], evidence["decision_group"])
        if key in seen:
            exclusions["duplicate_outcome"] += 1
            continue
        seen.add(key)
        risk = abs(entry - stop) * quantity
        slippage = (entry + exit_price) * quantity * policy["round_trip_slippage_bps"] / 20000
        net = gross - fees - slippage
        if not math.isfinite(net / risk):
            exclusions["invalid_risk_normalized_outcome"] += 1
            continue
        rows.append({**{k: evidence[k] for k in CATEGORICAL}, "features": deepcopy(evidence["features"]),
                     "setup_score": evidence["setup_score"], "decision_at": decision.isoformat(),
                     "outcome_at": completed.isoformat(), "session": decision.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat(),
                     "lifecycle_id": key[0], "decision_group": key[1], "evidence_fingerprint": evidence["fingerprint"],
                     "policy_hash": digest(policy), "net_r": net / risk, "net_pnl_rs": net,
                     "risk_rs": risk, "slippage_rs": slippage, "fees_rs": fees})
    rows.sort(key=lambda r: (r["decision_at"], r["lifecycle_id"], r["decision_group"]))
    return rows, dict(exclusions)


def family(row):
    return row["source_engine"] + ":" + row["source_set"] + "|" + row["source_lane"]


def segment(row):
    return "|".join(str(row[k]) for k in CATEGORICAL)


def _matrix(rows, encoder=None):
    raw = np.array([[float(r["features"][k]) for k in NUMERIC] for r in rows], dtype=float)
    if encoder is None:
        observation_weights = _sample_weights(rows)
        mean = np.average(raw, axis=0, weights=observation_weights)
        scale = np.sqrt(np.average((raw-mean)**2, axis=0, weights=observation_weights))
        encoder = {"mean": mean.tolist(), "scale": np.maximum(scale, 1e-6).tolist(),
                   "minimum": raw.min(axis=0).tolist(), "maximum": raw.max(axis=0).tolist(),
                   "categories": {k: sorted(set(r[k] for r in rows)) for k in CATEGORICAL}}
    continuous = (raw - np.array(encoder["mean"])) / np.array(encoder["scale"])
    categoric = np.array([[float(r[k] == v) for k in CATEGORICAL for v in encoder["categories"][k]] for r in rows])
    return np.column_stack((np.ones(len(rows)), continuous, categoric)), encoder


def _sigmoid(logits):
    return 1 / (1 + np.exp(-np.clip(logits, -35, 35)))


def _sample_weights(rows):
    counts = Counter(r["lifecycle_id"] for r in rows)
    return np.array([1 / counts[r["lifecycle_id"]] for r in rows])


def _logistic(x, y, penalty, sample_weights):
    weights = np.zeros(x.shape[1])
    effective = float(sample_weights.sum())
    # A fixed deterministic optimizer, with a Lipschitz-based step size.
    rate = 1 / (.25 * np.linalg.norm(x * np.sqrt(sample_weights[:, None]), ord=2) ** 2 / effective + penalty / effective + 1e-6)
    for _ in range(600):
        regularized = weights.copy()
        regularized[0] = 0
        gradient = x.T @ (sample_weights * (_sigmoid(x @ weights) - y)) / effective + penalty * regularized / effective
        weights -= rate * gradient
        if np.max(np.abs(gradient)) < 1e-7:
            break
    return weights


def fit_model(rows, policy, *, min_samples=200, min_segment_samples=50, penalty=10.0):
    """Fit one engine/lane using past train/calibration sessions only."""
    sessions = sorted(set(r["session"] for r in rows))
    if len(set(r["lifecycle_id"] for r in rows)) < min_samples or len(sessions) < 4 or len(set(family(r) for r in rows)) != 1:
        return None
    split = max(2, int(len(sessions) * .75))
    boundary = clock(sessions[split] + "T00:00:00+05:30")
    calibration = [r for r in rows if r["session"] >= sessions[split]]
    calibration_ids = {r["lifecycle_id"] for r in calibration}
    train = [r for r in rows if r["session"] < sessions[split] and clock(r["outcome_at"]) < boundary and r["lifecycle_id"] not in calibration_ids]
    if min(len(train), len(calibration)) < max(8, min_samples // 5) or len(set(r["net_r"] > 0 for r in train)) < 2 or len(set(r["net_r"] > 0 for r in calibration)) < 2:
        return None
    x, encoder = _matrix(train)
    y = np.array([r["net_r"] for r in train])
    sample_weights = _sample_weights(train)
    regularizer = np.eye(x.shape[1]) * penalty
    regularizer[0, 0] = 0
    net_weights = np.linalg.solve(x.T @ (sample_weights[:, None] * x) + regularizer, x.T @ (sample_weights * y))
    win_weights = _logistic(x, (y > 0).astype(float), penalty, sample_weights)
    cx, _ = _matrix(calibration, encoder)
    # Separate chronological calibration data; fit a monotonic sigmoid to the
    # classifier logits. The slope constraint prevents artificial rank reversal.
    logits = cx @ win_weights
    cy = np.array([r["net_r"] > 0 for r in calibration], dtype=float)
    cal_weights = _sample_weights(calibration)
    cal_count = float(cal_weights.sum())
    calibration_weights = np.array([0., 1.])
    calx = np.column_stack((np.ones(len(logits)), logits))
    rate = 1 / (.25 * np.linalg.norm(calx * np.sqrt(cal_weights[:, None]), ord=2) ** 2 / cal_count + 1 / cal_count + 1e-6)
    for _ in range(600):
        gradient = calx.T @ (cal_weights * (_sigmoid(calx @ calibration_weights) - cy)) / cal_count
        gradient[1] += (calibration_weights[1] - 1) / cal_count
        calibration_weights -= rate * gradient
        calibration_weights[1] = max(0., calibration_weights[1])
    net_predictions = cx @ net_weights
    residual = np.array([r["net_r"] for r in calibration]) - net_predictions
    counts = Counter()
    for key in set(segment(r) for r in rows):
        counts[key] = len(set(r["lifecycle_id"] for r in rows if segment(r) == key))
    eligible = {k: n for k, n in counts.items() if n >= min_segment_samples}
    return {"schema": MODEL_SCHEMA, "scoring_version": SCORING_VERSION,
            "target_policy": policy, "policy_hash": digest(policy), "family": family(rows[0]),
            "trained_until": max(r["outcome_at"] for r in rows), "samples": len(set(r["lifecycle_id"] for r in rows)),
            "fit_samples": len(train), "calibration_samples": len(calibration), "encoder": encoder,
            "net_weights": net_weights.tolist(), "win_weights": win_weights.tolist(),
            "calibration_weights": calibration_weights.tolist(), "eligible_segments": eligible,
            "net_error_se": float(np.sqrt(np.average(residual**2, weights=cal_weights) / cal_count)),
            "grade_cutoffs": np.quantile(net_predictions, [.4, .6, .8]).tolist(),
            "validation_passed": False, "mode": "SHADOW_ONLY", "live_eligible": False,
            "model_id": digest({"evidence": [r["evidence_fingerprint"] for r in rows], "policy": policy, "penalty": penalty})}


def predict(model, row, *, require_validated=True):
    """Abstain for sparse/unseen/future model evidence; never grant eligibility."""
    result = {"status": "UNVALIDATED", "setup_score": row.get("setup_score"),
              "calibrated_win_probability": None, "expected_net_r": None,
              "research_grade": None, "mode": "SHADOW_ONLY", "live_eligible": False}
    if not isinstance(model, dict) or model.get("schema") != MODEL_SCHEMA or model.get("scoring_version") != SCORING_VERSION:
        return result
    if require_validated and not model.get("validation_passed"):
        return result
    observed = clock(row.get("decision_at"))
    fitted = clock(model.get("trained_until"))
    if not observed or not fitted or fitted >= observed or family(row) != model.get("family"):
        return result
    if segment(row) not in model["eligible_segments"] or any(row[k] not in model["encoder"]["categories"][k] for k in CATEGORICAL):
        return result
    if any(number(row.get("features", {}).get(k)) is None for k in NUMERIC):
        return result
    values = [float(row["features"][k]) for k in NUMERIC]
    if any(value < low - 1e-6 or value > high + 1e-6 for value, low, high in zip(values, model["encoder"]["minimum"], model["encoder"]["maximum"])):
        return result
    x, _ = _matrix([row], model["encoder"])
    logits = float((x @ np.array(model["win_weights"]))[0])
    intercept, slope = model["calibration_weights"]
    probability = float(_sigmoid(intercept + slope * logits))
    expected = float((x @ np.array(model["net_weights"]))[0])
    # This margin is an empirical calibration error margin, not a guaranteed
    # confidence bound for individual trades or a proof of positive expectancy.
    margin = 1.96 * model["net_error_se"]
    cutoffs = model["grade_cutoffs"]
    research_grade = "C" if expected <= 0 else "B" if expected - margin <= 0 else "A+" if expected >= cutoffs[2] else "A" if expected >= cutoffs[1] else "B+"
    result.update(status="SHADOW_VALIDATED" if model.get("validation_passed") else "SHADOW_ESTIMATE",
                  calibrated_win_probability=probability, expected_net_r=expected,
                  empirical_error_margin_r=margin, research_grade=research_grade,
                  model_id=model["model_id"], model_samples=model["samples"], target_policy=model["target_policy"])
    return result


def _metrics(rows, probabilities=None):
    if not rows:
        return {"samples": 0, "mean_net_r": None, "max_drawdown_r": None, "brier": None, "log_loss": None}
    returns = np.array([r["net_r"] for r in rows])
    path = np.r_[0., np.cumsum(returns)]
    result = {"samples": len(rows), "mean_net_r": float(returns.mean()),
              "max_drawdown_r": float(np.max(np.maximum.accumulate(path) - path)),
              "mean_net_pnl_rs": float(np.mean([r["net_pnl_rs"] for r in rows])),
              "brier": None, "log_loss": None, "reliability": []}
    if probabilities is not None:
        p = np.clip(np.array(probabilities), 1e-6, 1 - 1e-6)
        y = (returns > 0).astype(float)
        result["brier"] = float(np.mean((p-y)**2))
        result["log_loss"] = float(np.mean(-y*np.log(p) - (1-y)*np.log(1-p)))
        for low, high in zip(np.arange(0, 1, .2), np.arange(.2, 1.2, .2)):
            mask = (p >= low) & (p < high)
            if mask.any():
                result["reliability"].append({"samples": int(mask.sum()), "predicted": float(p[mask].mean()), "observed": float(y[mask].mean())})
    return result


def evaluate(rows, policy=None, *, min_samples=200, min_segment_samples=50, penalty=10.0):
    """Expanding chronological folds; tune nothing against untouched test days."""
    policy = policy or target_policy()
    sessions = sorted(set(r["session"] for r in rows))
    folds, evaluated, predictions, baseline_p, selected, baseline_selected = [], [], [], [], [], []
    for test_day in sessions[4:]:
        boundary = clock(test_day + "T00:00:00+05:30")
        test = [r for r in rows if r["session"] == test_day]
        test_ids = {r["lifecycle_id"] for r in test}
        past = [r for r in rows if r["session"] < test_day and clock(r["outcome_at"]) < boundary and r["lifecycle_id"] not in test_ids]
        fold = {"test_session": test_day, "train_samples": len(past), "test_samples": len(test), "scored": 0}
        models = {key: fit_model([r for r in past if family(r) == key], policy, min_samples=min_samples,
                                min_segment_samples=min_segment_samples, penalty=penalty) for key in sorted(set(family(r) for r in past))}
        by_family = {}
        for row in test:
            model = models.get(family(row))
            estimate = predict(model, row, require_validated=False)
            if estimate["expected_net_r"] is None:
                continue
            fold["scored"] += 1
            evaluated.append(row)
            predictions.append(estimate["calibrated_win_probability"])
            # Compare to a past-only empirical base rate from the same model's data.
            model_past = [r for r in past if family(r) == family(row)]
            baseline_p.append(sum(r["net_r"] > 0 for r in model_past) / len(model_past))
            by_family.setdefault(family(row), []).append((row, estimate))
        for group in by_family.values():
            unique = {}
            for row, estimate in group:
                unique.setdefault(row["lifecycle_id"], (row, estimate))
            group = list(unique.values())
            # Same selection budget per engine/day isolates ranking improvement.
            count = max(1, len(group) // 3)
            selected.extend(r for r, _ in sorted(group, key=lambda pair: pair[1]["expected_net_r"], reverse=True)[:count])
            baseline_selected.extend(r for r, _ in sorted(group, key=lambda pair: pair[0]["setup_score"], reverse=True)[:count])
        folds.append(fold)
    # Drawdown depends on actual decision order, never the model's rank order.
    selected.sort(key=lambda r: (r["decision_at"], r["lifecycle_id"], r["decision_group"]))
    baseline_selected.sort(key=lambda r: (r["decision_at"], r["lifecycle_id"], r["decision_group"]))
    predicted_metrics, base_metrics = _metrics(evaluated, predictions), _metrics(evaluated, baseline_p)
    ranking, baseline_ranking = _metrics(selected), _metrics(baseline_selected)
    sessions_scored = [f for f in folds if f["scored"]]
    improved_days = sum(sum(r["net_r"] for r in selected if r["session"] == f["test_session"]) > sum(r["net_r"] for r in baseline_selected if r["session"] == f["test_session"]) for f in sessions_scored)
    coverage = len(evaluated) / sum(f["test_samples"] for f in folds) if folds else 0.
    gates = {
        "enough_unseen_sessions": len(sessions_scored) >= 5,
        "enough_unseen_outcomes": len(set(r["lifecycle_id"] for r in evaluated)) >= 100,
        "both_decision_groups": {r["decision_group"] for r in evaluated} == {"ACCEPTED", "REJECTED"},
        "coverage": coverage >= .90,
        "probability_improves": bool(evaluated and predicted_metrics["brier"] < base_metrics["brier"] and predicted_metrics["log_loss"] < base_metrics["log_loss"]),
        "ranking_improves": bool(selected and ranking["mean_net_r"] > baseline_ranking["mean_net_r"] and ranking["mean_net_r"] > 0),
        "drawdown_acceptable": bool(selected and ranking["max_drawdown_r"] <= baseline_ranking["max_drawdown_r"]),
        "multiple_unseen_days_improve": bool(sessions_scored and improved_days > len(sessions_scored) / 2),
    }
    return {"schema": "signal_learning_evaluation.v1", "mode": "SHADOW_ONLY", "live_eligible": False,
            "target_policy": policy, "target_hash": digest(policy), "samples": len(rows),
            "unique_signals": len(set(r["lifecycle_id"] for r in rows)),
            "decision_counts": dict(Counter(r["decision_group"] for r in rows)), "folds": folds,
            "coverage": coverage, "probability": predicted_metrics, "probability_baseline": base_metrics,
            "ranking": ranking, "ranking_baseline": baseline_ranking, "gates": gates,
            "validation_passed": all(gates.values()), "blockers": [k for k, v in gates.items() if not v]}


def build_artifact(rows, report, *, min_samples=200, min_segment_samples=50, penalty=10.0):
    models = {}
    for key in sorted(set(family(r) for r in rows)):
        group = [r for r in rows if family(r) == key]
        own_report = evaluate(group, report["target_policy"], min_samples=min_samples, min_segment_samples=min_segment_samples, penalty=penalty)
        model = fit_model(group, report["target_policy"], min_samples=min_samples, min_segment_samples=min_segment_samples, penalty=penalty)
        if model:
            model["validation_passed"] = own_report["validation_passed"]
            model["validation_blockers"] = own_report["blockers"]
            models[key] = model
    return {"schema": MODEL_SCHEMA, "mode": "SHADOW_ONLY", "live_eligible": False, "models": models}


def shadow_assessment(evidence, path=DEFAULT_MODEL_PATH):
    """Cached read of a frozen model; no fitting or networking in the scanner."""
    try:
        path = Path(path)
        signature = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
        key = str(path.resolve())
        cached = _MODEL_CACHE.get(key)
        if cached is None or cached[0] != signature:
            artifact = json.loads(path.read_text()) if signature else {}
            _MODEL_CACHE[key] = (signature, artifact)
        artifact = _MODEL_CACHE[key][1]
        if not isinstance(artifact, dict):
            return predict(None, evidence)
        if not valid_evidence(evidence):
            return predict(None, evidence)
        model = (artifact.get("models") or {}).get(family(evidence))
        return predict(model, evidence)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, np.linalg.LinAlgError):
        return predict(None, evidence)
