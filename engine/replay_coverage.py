"""How much option history a replay actually has to work with.

Written 2026-08-17 after a backtest comparison sent an investigation down the wrong road
for hours. Two installs were compared:

    current   87 trades   Rs. 64,358
    old      496 trades   Rs.303,767

which read as "the system got 5x worse". It had not. Three separate distortions, none of
them visible anywhere in the output:

  * the two runs covered NON-OVERLAPPING calendar windows — the old one 20-28 Jul, the
    current one 29 Jul-12 Aug. Replaying the CURRENT code over both windows gives
    +Rs.48,750 for July and -Rs.64 for August, on near-identical trade counts. The market
    changed, not the code.
  * the current install had 21 trading days of contract history, the old one 35.
  * 639 of the old install's 812 contract files are HEADER-ONLY (38 bytes). Counting
    files rather than files-with-data made its coverage look 8x better than it was, and
    that wrong number then anchored the analysis.

A backtest that silently runs on a thin or different window is worse than one that
refuses, because its number looks authoritative. This module exists so the coverage
travels with the result.

Read-only and defensive: a replay must never fail because a coverage probe could not
parse something.
"""
from __future__ import annotations

import glob
import os
from typing import Any, Dict, List

CANDLE_GLOB = os.path.join("data_store", "candles", "*_1min.csv")

# Below this many distinct trading days a result is a sample, not a verdict.
#
# 25, and the number matters: the first draft used 20, which sat just BELOW the 21 days
# the misleading run actually had — so the guard would have stayed silent on the exact
# case it was written for. A floor has to be above the incident it is meant to catch.
# 21 trading days is roughly one calendar month, which is thin for judging a strategy:
# the July and August windows here differ by Rs.356/trade on the same code.
THIN_COVERAGE_DAYS = 25


def _data_row_count(path: str) -> int:
    """Data rows in a CSV, excluding the header. 0 for a header-only file."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            lines = 0
            for lines, _ in enumerate(handle, start=0):
                pass
        return max(0, lines)
    except OSError:
        return 0


def measure_option_history(root: str = ".") -> Dict[str, Any]:
    """Contract-level coverage of the 1-minute option history a replay prices from.

    Counts files WITH DATA separately from the raw file count: a header-only file is a
    contract the replay cannot trade, and conflating the two is the exact error this
    module was written after.
    """
    pattern = os.path.join(root, CANDLE_GLOB)
    files: List[str] = sorted(glob.glob(pattern))
    with_data = 0
    header_only = 0
    rows = 0
    days = set()
    for path in files:
        count = _data_row_count(path)
        if count <= 0:
            header_only += 1
            continue
        with_data += 1
        rows += count
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                next(handle, None)              # header
                for line in handle:
                    stamp = line.split(",", 1)[0].strip()
                    if len(stamp) >= 10 and stamp[4] == "-" and stamp[7] == "-":
                        days.add(stamp[:10])
        except (OSError, StopIteration, IndexError):
            continue
    ordered = sorted(days)
    return {
        "files": len(files),
        "contracts_with_data": with_data,
        "header_only_files": header_only,
        "rows": rows,
        "trading_days": len(ordered),
        "first_day": ordered[0] if ordered else "",
        "last_day": ordered[-1] if ordered else "",
    }


def coverage_warnings(root: str = ".", thin_days: int = THIN_COVERAGE_DAYS) -> List[str]:
    """Operator-facing warnings about replay coverage. Empty when nothing is wrong."""
    out: List[str] = []
    try:
        stats = measure_option_history(root)
    except Exception:
        return out                              # a probe must never break a session

    if stats["files"] == 0:
        return out                              # no history staged at all is a different problem

    if 0 < stats["trading_days"] < thin_days:
        out.append(
            f"option history covers only {stats['trading_days']} trading days "
            f"({stats['first_day']} to {stats['last_day']}) across "
            f"{stats['contracts_with_data']} contracts — a backtest over this is a SAMPLE, "
            "not a verdict, and must not be compared against a run on a different window"
        )
    if stats["header_only_files"]:
        out.append(
            f"{stats['header_only_files']} of {stats['files']} option-history files hold a "
            "header and no candles — they inflate any file COUNT while contributing "
            f"nothing to a replay ({stats['contracts_with_data']} actually carry data)"
        )
    return out
