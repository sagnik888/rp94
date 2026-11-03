"""Build immutable shadow training data and evaluate frozen chronological folds."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from intelligence.signal_learning import build_artifact, evaluate, target_policy, training_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=ROOT / "data_store/rtracker/events")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--horizon-minutes", type=int, default=30)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--min-samples", type=int, default=200)
    parser.add_argument("--min-segment-samples", type=int, default=50)
    parser.add_argument("--penalty", type=float, default=10.0)
    args = parser.parse_args()
    if args.min_samples < 20 or args.min_segment_samples < 2 or args.penalty <= 0:
        parser.error("Require min-samples >=20, min-segment-samples >=2, penalty >0")
    policy = target_policy(args.horizon_minutes, args.slippage_bps)
    events, bad_lines = [], 0
    paths = sorted(args.events.glob("*.jsonl")) if args.events.is_dir() else [args.events]
    for path in paths:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
                else:
                    bad_lines += 1
            except ValueError:
                bad_lines += 1
    rows, exclusions = training_rows(events, policy)
    settings = dict(min_samples=args.min_samples, min_segment_samples=args.min_segment_samples, penalty=args.penalty)
    report = evaluate(rows, policy, **settings)
    report.update(input_files=len(paths), malformed_lines=bad_lines, exclusions=exclusions,
                  frozen_training_parameters=settings,
                  target_limitations="Fixed horizon touch simulation; not broker fills or strategy stop/target replay")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "training_rows.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    (args.output / "evaluation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    artifact = build_artifact(rows, report, **settings)
    (args.output / "model.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps({"samples": len(rows), "exclusions": exclusions, "validation_passed": report["validation_passed"],
                      "mode": report["mode"], "blockers": report["blockers"], "model_families": len(artifact["models"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
