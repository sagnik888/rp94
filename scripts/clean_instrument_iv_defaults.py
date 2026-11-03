"""Strip the fabricated 15.0 IV observations from the instrument IV-rank history.

`intelligence_aggregator._chain_iv_context` used to substitute `0.15` for a missing leg
under a non-default source label, which became a published `instrument_iv` of 15.0 and was
archived by `scanner._record_instrument_iv`. The guard that refuses to record a
`default`-sourced observation closed the leak: the newest fabricated entry in every
instrument is 2026-07-24, and every value since is real.

The archived entries remain, and they are counted as real observations by
`_instrument_iv_observations` (which only filters `value > 0`). Real IV for these indices
runs roughly 8-13, so a 15.0 sits at the top of the distribution and pushes every genuine
reading's percentile DOWN -- measured at up to 15 points for NIFTY.

MUST BE RUN WITH THE SCANNER STOPPED. The live process loads this file into
`_instrument_iv_data` at startup and rewrites the whole file from memory roughly every 60
seconds, so a clean written underneath a running scanner is reverted within the minute.
The script refuses to run if it can see main.py alive.

Usage:
    python scripts/clean_instrument_iv_defaults.py --dry-run
    python scripts/clean_instrument_iv_defaults.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "data_store" / "instrument_iv_history.json"

# The exact fabricated value, and the date the recording guard closed the leak. Both
# bounds matter: a genuine 15.0 after the guard landed is a real reading and is kept.
FABRICATED_VALUE = 15.0
LEAK_CLOSED_AFTER = "2026-07-24"


def scanner_is_running() -> bool:
    """True if a main.py for this repo is alive. Windows-oriented, fails open to safe."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
             "Where-Object { $_.CommandLine -like '*main.py*' } | "
             "Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=30,
        )
        return int((out.stdout or "0").strip() or 0) > 0
    except Exception:
        # Cannot tell -> assume it is running. Reverting a clean is harmless; racing a
        # live writer and reporting success is not.
        return True


def plan(history: dict) -> dict:
    """{instrument: [dates to drop]} -- exact value, on or before the leak-close date."""
    drops = {}
    for instrument, series in history.items():
        hits = [
            day for day, value in series.items()
            if float(value) == FABRICATED_VALUE and str(day) <= LEAK_CLOSED_AFTER
        ]
        if hits:
            drops[instrument] = sorted(hits)
    return drops


def percentile(series: list, value: float) -> float:
    return 100.0 * sum(1 for s in series if s <= value) / len(series) if series else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--path", default=str(HISTORY))
    parser.add_argument("--allow-running", action="store_true",
                        help="override the live-scanner refusal (the clean will be reverted)")
    args = parser.parse_args()

    path = Path(args.path)
    history = json.loads(path.read_text(encoding="utf-8"))
    drops = plan(history)

    if not drops:
        print("nothing to clean: no fabricated 15.0 observations on or before %s"
              % LEAK_CLOSED_AFTER)
        return 0

    total = sum(len(v) for v in drops.values())
    print("fabricated observations to drop: %d" % total)
    for instrument, days in drops.items():
        series = history[instrument]
        kept = [float(v) for d, v in series.items() if d not in set(days)]
        latest_day = max(series)
        latest = float(series[latest_day])
        before = percentile([float(v) for v in series.values()], latest)
        after = percentile(kept, latest)
        print("  %-11s drop %2d of %3d  (%s .. %s)  latest %s=%.2f percentile %.0f -> %.0f"
              % (instrument, len(days), len(series), days[0], days[-1],
                 latest_day, latest, before, after))

    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    if scanner_is_running() and not args.allow_running:
        print("\nREFUSED: a main.py is running. It holds this history in memory and "
              "rewrites the file about every 60s, so the clean would be reverted. "
              "Stop the scanner and re-run.")
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(".json.bak-%s" % stamp)
    shutil.copy2(path, backup)

    for instrument, days in drops.items():
        for day in days:
            history[instrument].pop(day, None)

    path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    print("\nbackup  %s" % backup.name)
    print("cleaned %s (%d observations removed)" % (path.name, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
