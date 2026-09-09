"""Frozen chronological A/B gate for exit-policy and lot-size promotion.

Uses the same accepted trades and real archived option-premium bars for both
policies. Costs/slippage are charged by the shared accounting engine. Nothing
in this script mutates live settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import get_settings  # noqa: E402
from scripts.backtest_exit_lifecycle import run_day  # noqa: E402


def _metrics(rows: list[dict], key: str) -> dict:
    values = [float(row.get(key) or 0.0) for row in rows]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gross_win = sum(value for value in values if value > 0)
    gross_loss = abs(sum(value for value in values if value < 0))
    ordered = sorted(values)
    p10 = ordered[max(0, math.ceil(len(ordered) * 0.10) - 1)] if ordered else 0.0
    return {
        "samples": len(values),
        "net": round(sum(values), 2),
        "expectancy": round(sum(values) / len(values), 2) if values else 0.0,
        "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100.0, 2) if values else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "max_drawdown": round(max_dd, 2),
        "p10_trade": round(p10, 2),
    }


def _settings_fingerprint() -> tuple[str, dict]:
    settings = get_settings()
    names = (
        "dynamic_rr_runner_unlock_ratio",
        "dynamic_rr_runner_lock_pct",
        "trend_runner_lock_pct_high_vol",
        "trend_runner_min_peak_r_multiple",
        "profit_giveback_exit_ratio",
        "profit_lock_min_r_multiple",
        "option_trade_charge_per_order",
        "option_slippage_pct",
    )
    frozen = {name: getattr(settings, name, None) for name in names}
    encoded = json.dumps(frozen, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16], frozen


def _segment_readiness(train: list[dict], holdout: list[dict], minimum: int) -> dict:
    def segment(row):
        return "|".join((
            str(row.get("index") or "UNKNOWN"),
            str(row.get("timeframe") or "UNKNOWN"),
            str(row.get("regime") or "UNKNOWN"),
        ))

    train_counts = Counter(segment(row) for row in train)
    hold_counts = Counter(segment(row) for row in holdout)
    hold_min = max(10, int(math.ceil(minimum / 3)))
    details = {}
    for name in sorted(set(train_counts) | set(hold_counts)):
        train_n = train_counts[name]
        hold_n = hold_counts[name]
        details[name] = {
            "train_samples": train_n,
            "holdout_samples": hold_n,
            "calibration_ready": train_n >= minimum and hold_n >= hold_min,
        }
    return {
        "minimum_train_samples": minimum,
        "minimum_holdout_samples": hold_min,
        "ready_segments": [name for name, value in details.items() if value["calibration_ready"]],
        "segments": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Chronological frozen exit-policy A/B gate")
    parser.add_argument("--minimum-sessions", type=int, default=60)
    parser.add_argument("--maximum-sessions", type=int, default=90)
    parser.add_argument("--holdout-sessions", type=int, default=20)
    parser.add_argument("--minimum-segment-samples", type=int, default=50)
    parser.add_argument(
        "--output",
        default=str(ROOT / "reports" / "exit_policy_walk_forward_latest.json"),
    )
    args = parser.parse_args()
    if not (60 <= args.minimum_sessions <= args.maximum_sessions <= 90):
        parser.error("certification window must be 60-90 sessions")
    if args.holdout_sessions < 20 or args.holdout_sessions >= args.minimum_sessions:
        parser.error("holdout must be at least 20 sessions and smaller than minimum window")

    event_dir = ROOT / "data_store" / "rtracker" / "events"
    days = sorted(path.stem for path in event_dir.glob("20??-??-??.jsonl"))[-args.maximum_sessions :]
    sessions = []
    for day in days:
        result = run_day(day, quiet=True)
        if int(result.get("matched") or 0) > 0:
            sessions.append(result)

    split_at = max(0, len(sessions) - args.holdout_sessions)
    train_sessions = sessions[:split_at]
    hold_sessions = sessions[split_at:]
    train_trades = [trade for session in train_sessions for trade in session.get("trade_ledger", [])]
    hold_trades = [trade for session in hold_sessions for trade in session.get("trade_ledger", [])]
    all_trades = train_trades + hold_trades

    fingerprint, frozen_settings = _settings_fingerprint()
    baseline_hold = _metrics(hold_trades, "baseline_net")
    candidate_hold = _metrics(hold_trades, "candidate_net")
    baseline_pf = baseline_hold["profit_factor"] or float("inf")
    candidate_pf = candidate_hold["profit_factor"] or float("inf")
    gates = {
        "session_count_60_to_90": args.minimum_sessions <= len(sessions) <= args.maximum_sessions,
        "chronological_holdout_at_least_20": len(hold_sessions) >= args.holdout_sessions,
        "candidate_holdout_expectancy_better": candidate_hold["expectancy"] > baseline_hold["expectancy"],
        "candidate_holdout_pf_not_worse": candidate_pf >= baseline_pf,
        "candidate_holdout_drawdown_not_worse": candidate_hold["max_drawdown"] <= baseline_hold["max_drawdown"],
        "candidate_tail_not_worse": candidate_hold["p10_trade"] >= baseline_hold["p10_trade"],
    }
    segment_status = _segment_readiness(
        train_trades, hold_trades, max(1, args.minimum_segment_samples)
    )
    gates["at_least_one_calibration_segment_ready"] = bool(segment_status["ready_segments"])
    certified = all(gates.values())
    report = {
        "schema": "exit_policy_walk_forward.v1",
        "candidate": "runtime_trend_runner",
        "candidate_fingerprint": fingerprint,
        "frozen_settings": frozen_settings,
        "live_settings_changed": False,
        "costs_and_slippage_included": True,
        "requested_window": {
            "minimum_sessions": args.minimum_sessions,
            "maximum_sessions": args.maximum_sessions,
            "holdout_sessions": args.holdout_sessions,
        },
        "available": {
            "event_sessions": len(days),
            "replayable_sessions": len(sessions),
            "train_sessions": len(train_sessions),
            "holdout_sessions": len(hold_sessions),
            "matched_trades": len(all_trades),
            "first_session": sessions[0]["date"] if sessions else "",
            "last_session": sessions[-1]["date"] if sessions else "",
        },
        "train": {
            "baseline": _metrics(train_trades, "baseline_net"),
            "candidate": _metrics(train_trades, "candidate_net"),
        },
        "holdout": {"baseline": baseline_hold, "candidate": candidate_hold},
        "all": {
            "baseline": _metrics(all_trades, "baseline_net"),
            "candidate": _metrics(all_trades, "candidate_net"),
        },
        "calibration_segments": segment_status,
        "certification_gates": gates,
        "candidate_certified": certified,
        # Backtest certification is necessary but not sufficient for risking more.
        "lot_increase_allowed": False,
        "lot_increase_blockers": (
            ["candidate_not_certified"] if not certified else []
        ) + ["requires_at_least_20_shadow_live_sessions_after_certification"],
        "session_results": sessions,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "replayable_sessions": len(sessions),
        "matched_trades": len(all_trades),
        "candidate_certified": certified,
        "lot_increase_allowed": False,
        "failed_gates": [name for name, ok in gates.items() if not ok],
    }, indent=2))
    return 0 if certified else 2


if __name__ == "__main__":
    raise SystemExit(main())
