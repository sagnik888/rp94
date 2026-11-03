"""Check paired evidence; exit non-zero unless strategy promotion is safe."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.promotion_gate import assess_promotion


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.report.read_text(encoding="utf-8"))
    decision = assess_promotion(payload)
    print(json.dumps({
        "allowed": decision.allowed,
        "mode": decision.mode,
        "blockers": decision.blockers,
    }, indent=2))
    return 0 if decision.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
