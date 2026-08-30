"""Generate an advisory bid/ask entry-policy expectancy report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import get_settings
from intelligence.entry_policy_evaluation import evaluate_entry_shadow_walk_forward
from intelligence.rtracker import RegimeTracker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--min-train-sessions", type=int, default=5)
    parser.add_argument("--test-sessions", type=int, default=1)
    parser.add_argument(
        "--output",
        default="data_store/reports/entry_policy_expectancy.json",
    )
    args = parser.parse_args()

    tracker = RegimeTracker.from_settings(get_settings())
    events = tracker.load_events(weeks=max(1, args.weeks), as_of=datetime.now().astimezone())
    report = evaluate_entry_shadow_walk_forward(
        events,
        horizon_minutes=max(1, args.horizon),
        min_train_sessions=max(1, args.min_train_sessions),
        test_sessions=max(1, args.test_sessions),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
