"""Read-only S1 admission cohort comparison on sessions preceding the loss-day audit.

Recorded option outcomes stay fixed. This is a selection diagnostic, not a
counterfactual portfolio or native exit replay. No live settings are modified.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import get_settings
from engine.quality_verdict import QualityInputs, evaluate_quality, policy_configuration

OUTPUT = ROOT / "reports/full_session_performance_audit_20261008/s1_other_sessions_validation.json"
GROUPS = {"discovery": ("2026-09-24", "2026-09-25"),
          "chronological_holdout": ("2026-10-05", "2026-10-06")}


def inputs_for(event):
    checks = (event.get("quality") or {}).get("checks") or {}
    # Do not invent missing measured-volume or ADX evidence in older snapshots.
    return QualityInputs(
        timeframe=event["timeframe"], inst_type=(event.get("contract") or {}).get("inst_type", "OPT"),
        confidence=float(checks.get("confidence", event.get("confidence", 0.0))),
        regime=checks.get("regime", "UNKNOWN"), adx_value=checks.get("adx", 0.0),
        adx_available=checks.get("adx_measured") is True,
        trend_15m_agrees=checks.get("trend_15m_agrees") is True,
        impulse_confirmed=checks.get("impulse_confirmed") is True,
        source_count=checks.get("source_count", 1),
        volume_confirmed=checks.get("volume_confirmed") is True,
        volume_available=checks.get("volume_available") is True,
        volume_basis=checks.get("volume_basis", "UNAVAILABLE"),
        volume_ratio=checks.get("volume_ratio", 0.0),
        grade=checks.get("grade", event.get("grade", "")),
        power_move=checks.get("power_move") is True,
        market_shift_confirmed=checks.get("market_shift_confirmed") is True,
        market_shift_core_votes=checks.get("market_shift_core_votes", 0),
        market_shift_total_votes=checks.get("market_shift_total_votes", 0),
        stale_regime_reversal=checks.get("stale_regime_reversal") is True,
        lane="S1_OFFLINE_CONTEXT_CANDIDATE", direction=event.get("direction", ""),
        confidence_calibrated=checks.get("confidence_calibrated") is True,
    )


def metrics(rows):
    pnl = [float(row["recorded_net"]) for row in rows]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    return {"count": len(rows), "wins": len(wins), "losses": len(losses),
            "recorded_net_sum": round(sum(pnl), 2),
            "mean_recorded_net": round(sum(pnl) / len(pnl), 2) if pnl else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else None}


def main():
    settings = get_settings()
    sources, details, coverage = [], [], Counter()
    for group, days in GROUPS.items():
        for day in days:
            path = ROOT / f"data_store/rtracker/events/{day}.jsonl"
            raw = path.read_bytes()
            sources.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
            rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            accepted, exited = {}, {}
            for row in rows:
                if row.get("source_lane") != "S1" or row.get("run_mode") != "REAL":
                    continue
                if (row.get("contract") or {}).get("inst_type") != "OPT":
                    coverage["non_option_events_excluded"] += 1
                    continue
                key = row.get("lifecycle_id")
                if not key:
                    coverage["missing_identity"] += 1
                    continue
                if row.get("decision") == "ACCEPTED":
                    accepted.setdefault(key, row)
                if row.get("decision") == "EXIT":
                    exited.setdefault(key, row)
            for key, entry in accepted.items():
                coverage["accepted"] += 1
                out = exited.get(key)
                if out is None:
                    coverage["accepted_without_exit"] += 1
                    continue
                verdict = evaluate_quality(inputs_for(entry), settings)
                path_samples = out.get("pnl_path") or []
                coverage["matched"] += 1
                coverage["has_ordered_premium_pnl_samples"] += bool(path_samples)
                coverage["has_exit_decision_record"] += bool(out.get("exit_decision_record"))
                details.append({"group": group, "session": day, "lifecycle_id": key,
                    "recorded_net": float(out.get("pnl") or 0.0), "recorded_exit_reason": out.get("exit_reason"),
                    "candidate_allowed": verdict.allowed, "candidate_reason_code": verdict.reason_code,
                    "candidate_checks": verdict.checks, "saved_original_quality": entry.get("quality"),
                    "saved_build": entry.get("build") or entry.get("strategy"),
                    "entry_at": entry.get("entry_timestamp") or entry.get("timestamp"),
                    "ordered_sample_count": len(path_samples),
                    "exit_decision_available": bool(out.get("exit_decision_record"))})
    summaries = {}
    for group in GROUPS:
        cohort = [r for r in details if r["group"] == group]
        summaries[group] = {"baseline_recorded": metrics(cohort),
                            "retained_by_proposed_shared_quality": metrics([r for r in cohort if r["candidate_allowed"]]),
                            "vetoed_by_proposed_shared_quality": metrics([r for r in cohort if not r["candidate_allowed"]])}
    result = {"schema": "s1_context_holdout.v1", "evaluation": "fixed_recorded_outcome_cohort",
        "source_sessions": GROUPS, "candidate_policy": policy_configuration(settings),
        "candidate": "Make current shared quality a binding veto on native S1 admission",
        "coverage": dict(coverage), "summaries": summaries, "rows": details, "sources": sources,
        "promotion": {"allowed": False, "reasons": [
            "Only four source sessions; insufficient independent native-policy evidence",
            "Recorded outcomes use different historical builds and exit ownership",
            "Suppressed entries would change native state/capacity and subsequent opportunities",
            "Premium P&L samples omit per-sample native closed-bar state and synchronized underlying quotes",
            "No causal bid/ask/native-state exit replay is certified; no exit threshold changes promoted"]}}
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"coverage": result["coverage"], "summaries": summaries, "promotion": result["promotion"]}, indent=2))


if __name__ == "__main__":
    main()
