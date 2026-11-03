"""CLI for strict paired causal replay promotion evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine.decision_evaluation import EvaluationThresholds, evaluate_paired_replays


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--starting-capital", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-drawdown-pct", type=float, default=20.0)
    parser.add_argument("--max-day-concentration", type=float, default=0.35)
    parser.add_argument("--min-positive-period-ratio", type=float, default=0.55)
    parser.add_argument("--min-index-profit-factor", type=float, default=1.0)
    parser.add_argument("--min-pair-coverage", type=float, default=0.95)
    args = parser.parse_args()
    thresholds = EvaluationThresholds(
        max_drawdown_pct=args.max_drawdown_pct,
        max_single_day_profit_concentration=args.max_day_concentration,
        min_positive_unseen_period_ratio=args.min_positive_period_ratio,
        min_index_profit_factor=args.min_index_profit_factor,
        min_pair_coverage=args.min_pair_coverage,
    )
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    result = evaluate_paired_replays(
        baseline,
        candidate,
        starting_capital=args.starting_capital,
        thresholds=thresholds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "promotable": result["promotable"], "gates": result["gates"]}, indent=2))
    return 0 if result["promotable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
