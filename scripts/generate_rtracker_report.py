"""Generate rtracker weekly regime study reports.

Usage:
  python scripts/generate_rtracker_report.py --weeks 8
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import get_settings
from intelligence.rtracker import RegimeTracker


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate rtracker regime study reports")
    parser.add_argument("--weeks", type=int, default=None, help="Number of weeks to include, default from settings")
    parser.add_argument("--root", default=None, help="Override rtracker data directory")
    args = parser.parse_args()

    settings = get_settings()
    tracker = RegimeTracker.from_settings(settings)
    if args.root:
        tracker.root_dir = Path(args.root)
        tracker.events_dir = tracker.root_dir / "events"
        tracker.reports_dir = tracker.root_dir / "reports"
    result = tracker.generate_weekly_reports(
        weeks=args.weeks or int(getattr(settings, "rtracker_report_weeks", 8) or 8)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
