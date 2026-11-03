"""Compare captured FYERS and Kite/Angel data without contacting any broker.

Each directory contains INDEX_INTERVAL.csv (four indices, 1min/5min/15min),
INDEX_chain.csv, INDIAVIX.csv, and decisions.json. CSVs use explicit timestamp
columns. This checks captured evidence only; it does not authorize live cutover.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import pandas as pd

INDICES = ("NIFTY", "BANKNIFTY", "MIDCPNIFTY", "SENSEX")


def compare_captures(baseline: Path, candidate: Path, *, price_tolerance=0.0, volume_tolerance=0.0) -> dict:
    failures = []
    checked = []
    def compare(name, keys, columns, candidate_volume=False):
        try:
            old = pd.read_csv(baseline/name)
            new = pd.read_csv(candidate/name)
            if old.empty or new.empty:
                raise ValueError("empty_capture")
            for frame in (old, new):
                if not set(keys+columns).issubset(frame.columns):
                    raise ValueError("missing_columns")
                stamps = pd.to_datetime(frame["timestamp"])
                stamps = stamps.dt.tz_localize("Asia/Kolkata") if stamps.dt.tz is None else stamps
                frame["timestamp"] = stamps.dt.tz_convert("UTC").astype(str)
                if frame.duplicated(keys).any():
                    raise ValueError("duplicate_identity")
            old, new = old.set_index(keys).sort_index(), new.set_index(keys).sort_index()
            if not old.index.equals(new.index):
                raise ValueError("coverage_or_contract_mismatch")
            if candidate_volume:
                if "volume_complete" not in new or not new["volume_complete"].eq(True).all():
                    raise ValueError("constituent_coverage_incomplete")
                if "volume_basis" not in new or not new["volume_basis"].eq("constituent_shares").all():
                    raise ValueError("wrong_volume_basis")
                if "membership_version" not in new or new["membership_version"].isna().any():
                    raise ValueError("membership_provenance_missing")
            for column in columns:
                before = pd.to_numeric(old[column], errors="coerce")
                after = pd.to_numeric(new[column], errors="coerce")
                signed_change = column in {"call_oi_change", "put_oi_change"}
                if not all(math.isfinite(float(value)) and (signed_change or float(value) >= 0)
                           for values in (before, after) for value in values):
                    raise ValueError(column+"_invalid")
                if column == "volume":
                    tolerance = before.abs()*volume_tolerance
                else:
                    tolerance = price_tolerance if column in {"open", "high", "low", "close", "ltp", "call_ltp", "put_ltp"} else 0.0
                if not (after.sub(before).abs() <= tolerance).all():
                    raise ValueError(column+"_differs")
            checked.append(name)
        except (OSError, KeyError, ValueError, TypeError) as error:
            failures.append({"capture": name, "reason": type(error).__name__ if isinstance(error, OSError) else str(error)})
    for index in INDICES:
        for interval in ("1min", "5min", "15min"):
            compare(f"{index}_{interval}.csv", ["timestamp"], ["open", "high", "low", "close", "volume"], True)
        compare(f"{index}_chain.csv", ["timestamp", "expiry", "strike"],
                ["call_ltp", "put_ltp", "call_oi", "put_oi", "call_volume", "put_volume", "call_oi_change", "put_oi_change"])
    compare("INDIAVIX.csv", ["timestamp"], ["ltp"])
    try:
        old = json.loads((baseline/"decisions.json").read_text(encoding="utf-8"))
        new = json.loads((candidate/"decisions.json").read_text(encoding="utf-8"))
        if not isinstance(old, list) or not old or old != new:
            raise ValueError("decision_replay_differs_or_empty")
        required = {"timestamp", "instrument", "timeframe", "entry_eligible", "selected_contract", "management", "exit"}
        if any(not isinstance(row, dict) or not required.issubset(row) for row in old):
            raise ValueError("decision_evidence_missing")
        checked.append("decisions.json")
    except (OSError, ValueError, TypeError) as error:
        failures.append({"capture": "decisions.json", "reason": type(error).__name__ if isinstance(error, OSError) else str(error)})
    return {"captured_field_parity": not failures, "checked": checked, "failures": failures,
            "price_absolute_tolerance": price_tolerance, "volume_relative_tolerance": volume_tolerance,
            "third_provider_redundancy_preserved": False,
            "cutover_authorized": False,
            "limitation": "Only supplied captures were compared. Multi-session coverage, fault recovery and live entitlements require separate evidence."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--price-tolerance", type=float, default=0.0)
    parser.add_argument("--volume-relative-tolerance", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if any(not math.isfinite(value) or value < 0 for value in (args.price_tolerance, args.volume_relative_tolerance)):
        parser.error("tolerances must be finite and non-negative")
    result = compare_captures(args.baseline, args.candidate, price_tolerance=args.price_tolerance,
                              volume_tolerance=args.volume_relative_tolerance)
    content = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    print(content)
    return 0 if result["captured_field_parity"] else 1


if __name__ == "__main__":
    sys.exit(main())
