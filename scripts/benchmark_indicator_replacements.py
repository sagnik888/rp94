"""Run the research-only ATR replacement benchmark.

Example:
    python scripts/benchmark_indicator_replacements.py --lookback-days 30
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.indicator_replacement_benchmark import run_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare ATR/UT1 against alternative indicator triggers.")
    parser.add_argument("--data-dir", default="data_store/candles")
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--friction-bps", type=float, default=0.5, help="Per-side friction in spot bps.")
    parser.add_argument("--output", default=".benchmarks/indicator_replacement_benchmark.json")
    args = parser.parse_args()

    result = run_benchmark(
        args.data_dir,
        lookback_days=args.lookback_days,
        friction_bps_per_side=args.friction_bps,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"baseline: {result['baseline']}")
    print(f"friction_bps_per_side: {result['friction_bps_per_side']}")
    print(f"files: {len(result['files'])}, skipped: {len(result['skipped'])}")
    print("rank strategy              trades  win%    net_pts     PF    vs_base")
    for rank, row in enumerate(result["aggregate"][:12], start=1):
        pf = row["profit_factor"]
        pf_text = "--" if pf is None else f"{pf:.2f}"
        print(
            f"{rank:>4} {row['strategy']:<22} {row['trades']:>6} "
            f"{row['win_rate']:>5.1f} {row['net_points']:>10.2f} "
            f"{pf_text:>6} {row.get('improvement_vs_baseline_points', 0):>10.2f}"
        )
    print(f"wrote: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
