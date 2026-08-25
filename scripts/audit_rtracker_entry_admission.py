#!/usr/bin/env python3
"""Reproduce the rtracker entry-admission evidence without mutating its source.

The rtracker can emit more than one EXIT row for the same candidate lifecycle.  This
audit therefore reports both earliest- and latest-exit retention instead of silently
choosing one.  It also keeps legacy identity-less rows separate from rows that carry an
explicit option contract, because their accounting basis cannot be normalized safely.

Examples:
  python scripts/audit_rtracker_entry_admission.py
  python scripts/audit_rtracker_entry_admission.py --through 2026-08-21
  python scripts/audit_rtracker_entry_admission.py --json-out reports/rtracker_audit.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENTS_DIR = ROOT / "data_store" / "rtracker" / "events"
DEFAULT_ARCHIVE_DIR = ROOT / "data_store" / "session_archive"
LIFECYCLE_KEY_FIELDS = (
    "identity_kind",
    "date",
    "entry_timestamp",
    "instrument",
    "timeframe",
    "direction",
    "source_lane",
    "trading_symbol",
)
IST_OFFSET = timezone(timedelta(hours=5, minutes=30))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return float(default)


def _round(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Stored naive clocks in this repository are IST wall time. Normalize any explicit
    # offset to the same naive IST representation so a mixed naive/+05:30/Z collision
    # remains sortable and archive comparisons stay in the exchange clock.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(IST_OFFSET).replace(tzinfo=None)
    return parsed


def iter_events(events_dir: Path, through: str = "") -> Iterator[dict[str, Any]]:
    """Yield valid JSON objects from date-named files, never opening them for write."""
    for path in sorted(events_dir.glob("*.jsonl")):
        if through and path.stem > through:
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Malformed JSON at {path}:{line_number}: {exc}") from exc
                if isinstance(payload, dict):
                    yield payload


def lifecycle_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Prefer producer lifecycle identity; retain a lane/contract-aware legacy key."""
    lifecycle_id = str(event.get("lifecycle_id") or "").strip()
    if lifecycle_id:
        return (
            "lifecycle_id",
            str(event.get("date") or ""),
            lifecycle_id,
            "",
            "",
            "",
            "",
            "",
        )
    contract = event.get("contract") if isinstance(event.get("contract"), Mapping) else {}
    return (
        "legacy_fields",
        str(event.get("date") or ""),
        str(event.get("entry_timestamp") or ""),
        str(event.get("instrument") or ""),
        str(event.get("timeframe") or ""),
        str(event.get("direction") or ""),
        str(event.get("source_lane") or ""),
        str((contract or {}).get("trading_symbol") or ""),
    )


def _exit_sort_key(event: Mapping[str, Any]) -> tuple[datetime, str]:
    parsed = _iso_datetime(event.get("exit_timestamp") or event.get("timestamp"))
    return parsed or datetime.min, str(event.get("id") or "")


def dedupe_exits(
    exits: Iterable[Mapping[str, Any]], *, keep: str = "latest"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return lifecycle rows plus collision diagnostics.

    ``latest`` reproduces the external report. ``earliest`` better matches the
    scanner's first-terminal-signature behavior. Neither policy is assumed to be ground
    truth; collision groups are returned so the ambiguity remains visible.
    """
    if keep not in {"earliest", "latest"}:
        raise ValueError("keep must be 'earliest' or 'latest'")

    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for event in exits:
        grouped[lifecycle_key(event)].append(dict(event))

    selected: list[dict[str, Any]] = []
    collisions: list[dict[str, Any]] = []
    index = -1 if keep == "latest" else 0
    for key, rows in grouped.items():
        ordered = sorted(rows, key=_exit_sort_key)
        selected.append(ordered[index])
        if len(ordered) > 1:
            collisions.append(
                {
                    "lifecycle": dict(zip(LIFECYCLE_KEY_FIELDS, key)),
                    "row_count": len(ordered),
                    "exit_timestamps": [str(row.get("exit_timestamp") or "") for row in ordered],
                    "pnls": [_round(_number(row.get("pnl")), 2) for row in ordered],
                }
            )

    selected.sort(key=lambda row: (str(row.get("date") or ""), _exit_sort_key(row)))
    collisions.sort(key=lambda row: tuple(row["lifecycle"].values()))
    return selected, collisions


def _hold_seconds(event: Mapping[str, Any]) -> float | None:
    entered = _iso_datetime(event.get("entry_timestamp"))
    exited = _iso_datetime(event.get("exit_timestamp") or event.get("timestamp"))
    if entered is None or exited is None:
        return None
    try:
        held = (exited - entered).total_seconds()
    except TypeError:
        return None
    return held if held >= 0 else None


def summarize_outcomes(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnls = [_number(row.get("pnl")) for row in rows]
    peaks = [_number(row.get("peak_pnl")) for row in rows]
    losers = [pnl for pnl in pnls if pnl < 0]
    never_green_rows = [
        row for row in rows if _number(row.get("peak_pnl")) <= 0
    ]
    never_green_pnl = sum(_number(row.get("pnl")) for row in never_green_rows)
    loser_pot = sum(losers)
    holds = [held for row in rows if (held := _hold_seconds(row)) is not None]
    peak_sum = sum(peaks)

    return {
        "outcomes": len(rows),
        "winners": sum(pnl > 0 for pnl in pnls),
        "losers": len(losers),
        "flat": sum(pnl == 0 for pnl in pnls),
        "total_pnl": _round(sum(pnls), 2),
        "loser_pot": _round(loser_pot, 2),
        "never_green": len(never_green_rows),
        "never_green_pnl": _round(never_green_pnl, 2),
        "never_green_share_of_loser_pot_pct": _round(
            (abs(never_green_pnl) / abs(loser_pot) * 100.0) if loser_pot else 0.0,
            3,
        ),
        "never_green_median_hold_seconds": _round(
            statistics.median(
                held
                for row in never_green_rows
                if (held := _hold_seconds(row)) is not None
            )
            if any(_hold_seconds(row) is not None for row in never_green_rows)
            else 0.0,
            3,
        ),
        # Historical pnl is generally net while peak_pnl is generally gross.  Keep the
        # published ratio reproducible but label it honestly rather than calling it a
        # basis-consistent capture statistic.
        "published_mixed_basis_capture_pct": _round(
            (sum(pnls) / peak_sum * 100.0) if peak_sum else 0.0,
            3,
        ),
        "median_hold_seconds_all": _round(statistics.median(holds), 3) if holds else 0.0,
    }


def _is_explicit_option(row: Mapping[str, Any]) -> bool:
    contract = row.get("contract")
    return isinstance(contract, Mapping) and str(contract.get("inst_type") or "").upper() == "OPT"


def summarize_grades(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("grade") or "UNKNOWN")].append(_number(row.get("pnl")))

    grade_order = ("A+", "A", "B+", "B", "C", "UNKNOWN")
    ordered = sorted(grouped, key=lambda grade: (grade_order.index(grade) if grade in grade_order else 99, grade))
    result: dict[str, Any] = {}
    for grade in ordered:
        values = grouped[grade]
        result[grade] = {
            "n": len(values),
            "wins": sum(value > 0 for value in values),
            "win_rate_pct": _round(sum(value > 0 for value in values) / len(values) * 100.0, 3),
            "average_pnl": _round(sum(values) / len(values), 2),
            "total_pnl": _round(sum(values), 2),
        }
    return result


def summarize_high_confidence(rows: Sequence[Mapping[str, Any]], floor: float = 0.95) -> dict[str, Any]:
    selected = [row for row in rows if _number(row.get("confidence"), -1.0) >= floor]
    by_date: dict[str, float] = defaultdict(float)
    for row in selected:
        by_date[str(row.get("date") or "UNKNOWN")] += _number(row.get("pnl"))
    return {
        "floor": floor,
        "n": len(selected),
        "wins": sum(_number(row.get("pnl")) > 0 for row in selected),
        "win_rate_pct": _round(
            sum(_number(row.get("pnl")) > 0 for row in selected) / len(selected) * 100.0
            if selected
            else 0.0,
            3,
        ),
        "total_pnl": _round(sum(_number(row.get("pnl")) for row in selected), 2),
        "session_dates": len(by_date),
        "losing_session_dates": sum(value < 0 for value in by_date.values()),
        "date_pnl": {date: _round(value, 2) for date, value in sorted(by_date.items())},
    }


def summarize_paths(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pathed = [row for row in rows if isinstance(row.get("pnl_path"), list) and row.get("pnl_path")]
    basis_pairs = Counter(
        (
            str(row.get("pnl_basis") or "UNDECLARED"),
            str(row.get("pnl_path_basis") or "UNDECLARED"),
        )
        for row in rows
    )
    terminal_cost_matches = 0
    for row in pathed:
        path = row.get("pnl_path") or []
        try:
            terminal = _number(path[-1][1])
        except (IndexError, TypeError):
            continue
        cost = _number(row.get("round_trip_cost"), 80.0)
        if abs((terminal - _number(row.get("pnl"))) - cost) <= 0.01:
            terminal_cost_matches += 1
    return {
        "rows_with_path": len(pathed),
        "rows_with_path_pct": _round(len(pathed) / len(rows) * 100.0 if rows else 0.0, 3),
        "rows_with_three_or_more_samples": sum(len(row.get("pnl_path") or []) >= 3 for row in pathed),
        "terminal_path_minus_final_equals_round_trip_cost": terminal_cost_matches,
        "declared_basis_pairs": {
            f"{pnl_basis} | {path_basis}": count
            for (pnl_basis, path_basis), count in sorted(basis_pairs.items())
        },
        "path_dates": dict(sorted(Counter(str(row.get("date") or "UNKNOWN") for row in pathed).items())),
    }


def summarize_archived_post_exit_coverage(
    rows: Sequence[Mapping[str, Any]], archive_dir: Path
) -> dict[str, Any]:
    """Measure exact-contract 1-minute coverage after each pathed exit.

    This does not recreate tick ordering within a candle. It establishes where a later
    exit can at least be bounded with explicit OHLC assumptions, which is materially
    better than treating every wider-stop question as wholly unanswerable.
    """
    pathed = [row for row in rows if isinstance(row.get("pnl_path"), list) and row.get("pnl_path")]
    path_matches = 0
    target_date_rows = 0
    post_exit_rows = 0
    horizons = (15, 30, 60, 120)
    endpoint_counts = Counter()
    continuous_counts = Counter()
    timestamp_cache: dict[Path, list[datetime]] = {}

    for row in pathed:
        contract = row.get("contract")
        symbol = (
            str(contract.get("trading_symbol") or "")
            if isinstance(contract, Mapping)
            else ""
        )
        session_date = str(row.get("date") or "")
        exit_time = _iso_datetime(row.get("exit_timestamp") or row.get("timestamp"))
        if not symbol or not session_date or exit_time is None:
            continue
        if exit_time.tzinfo is not None:
            exit_time = exit_time.replace(tzinfo=None)
        path = archive_dir / session_date / "candles" / f"{symbol}_1min.csv"
        if not path.is_file():
            continue
        path_matches += 1
        if path not in timestamp_cache:
            timestamps: list[datetime] = []
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for candle in csv.DictReader(handle):
                    stamp = _iso_datetime(candle.get("timestamp"))
                    try:
                        valid_ohlc = all(
                            math.isfinite(float(candle.get(field) or "nan"))
                            for field in ("open", "high", "low", "close")
                        )
                    except (TypeError, ValueError):
                        valid_ohlc = False
                    if stamp is not None and valid_ohlc:
                        timestamps.append(stamp)
            timestamp_cache[path] = timestamps
        same_date = sorted(
            {
                stamp
                for stamp in timestamp_cache[path]
                if stamp.date().isoformat() == session_date
            }
        )
        if not same_date:
            continue
        target_date_rows += 1
        later = [stamp for stamp in same_date if stamp > exit_time]
        if not later:
            continue
        post_exit_rows += 1
        for horizon in horizons:
            target = exit_time + timedelta(minutes=horizon)
            if max(later) < target:
                continue
            endpoint_counts[horizon] += 1
            window = [stamp for stamp in later if stamp <= target]
            if not window or (window[0] - exit_time).total_seconds() > 90.0:
                continue
            gaps = [
                (current - previous).total_seconds()
                for previous, current in zip(window, window[1:])
            ]
            # Strict one-minute continuity: a missing candle produces a 120s gap.
            if gaps and max(gaps) > 90.0:
                continue
            continuous_counts[horizon] += 1

    return {
        "pathed_lifecycles": len(pathed),
        "exact_contract_path_matches": path_matches,
        "files_with_target_date_rows": target_date_rows,
        "files_with_any_post_exit_bar": post_exit_rows,
        "latest_post_exit_timestamp_at_least_minutes": {
            str(horizon): endpoint_counts[horizon] for horizon in horizons
        },
        "continuous_valid_one_minute_ohlc_through_minutes": {
            str(horizon): continuous_counts[horizon] for horizon in horizons
        },
        "warning": (
            "Continuity means valid one-minute OHLC with no gap above 90 seconds from "
            "the first post-exit minute through the horizon. It can bound some price "
            "paths only under declared intrabar ordering/fill assumptions; it is not "
            "an executable quote path or tick-accurate replay."
        ),
    }


def infer_option_gross_capture(
    rows: Sequence[Mapping[str, Any]], assumed_round_trip_cost: float = 80.0
) -> dict[str, Any]:
    """Show a clearly labeled same-basis sensitivity for explicit-option rows."""
    peak_sum = sum(_number(row.get("peak_pnl")) for row in rows)
    net_pnl = sum(_number(row.get("pnl")) for row in rows)
    inferred_gross_pnl = net_pnl + assumed_round_trip_cost * len(rows)
    return {
        "n": len(rows),
        "assumed_round_trip_cost_per_row": assumed_round_trip_cost,
        "stored_net_pnl": _round(net_pnl, 2),
        "inferred_gross_pnl": _round(inferred_gross_pnl, 2),
        "inferred_gross_capture_pct": _round(
            inferred_gross_pnl / peak_sum * 100.0 if peak_sum else 0.0, 3
        ),
        "warning": (
            "Sensitivity only: most historical rows do not declare their basis/cost. "
            "Use exact fills and charges for authoritative accounting."
        ),
    }


def summarize_rejections(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rejected = [event for event in events if str(event.get("decision") or "").upper() == "REJECTED"]
    sources = Counter(str(event.get("source") or "UNKNOWN") for event in rejected)
    option_economic_labels = sum(
        bool(event.get("exit_timestamp"))
        or _number(event.get("exit_price")) != 0
        or _number(event.get("pnl")) != 0
        or bool(event.get("pnl_path"))
        for event in rejected
    )
    return {
        "event_rows": len(rejected),
        "source_counts": dict(sorted(sources.items())),
        "rows_with_any_exit_or_pnl_label": option_economic_labels,
        "warning": (
            "REJECTED rows are lifecycle-stage telemetry, not unique opportunities. "
            "A small number contain incidental non-zero fields, but there is no "
            "systematic executable-option counterfactual outcome ledger."
        ),
    }


def build_audit(
    events: Sequence[Mapping[str, Any]],
    through: str = "2026-08-21",
    archive_dir: Path | None = None,
) -> dict[str, Any]:
    exits = [event for event in events if str(event.get("decision") or "").upper() == "EXIT"]
    through_exits = [event for event in exits if not through or str(event.get("date") or "") <= through]

    latest_all, collisions = dedupe_exits(exits, keep="latest")
    earliest_all, _ = dedupe_exits(exits, keep="earliest")
    latest_through, through_collisions = dedupe_exits(through_exits, keep="latest")
    earliest_through, _ = dedupe_exits(through_exits, keep="earliest")

    explicit_options = [row for row in latest_all if _is_explicit_option(row)]
    identity_less = [row for row in latest_all if not _is_explicit_option(row)]
    snapshot_explicit_options = [row for row in latest_through if _is_explicit_option(row)]
    snapshot_identity_less = [row for row in latest_through if not _is_explicit_option(row)]

    return {
        "method": {
            "lifecycle_key": list(LIFECYCLE_KEY_FIELDS),
            "report_snapshot_through": through,
            "primary_retention": "latest (reproduces cited report; earliest also reported)",
            "accounting_warning": (
                "Historical pnl is generally net of round-trip cost while peak_pnl/pnl_path "
                "is generally gross. The published capture ratio is therefore mixed-basis."
            ),
            "scope_warning": (
                "rtracker session_candidate outcomes are advisory marks unless joined to a "
                "broker order/fill ledger; this file alone does not prove realized fills."
            ),
        },
        "raw": {
            "event_rows": len(events),
            "exit_rows": len(exits),
            "rejected": summarize_rejections(events),
        },
        "collisions": {
            "groups": len(collisions),
            "excess_exit_rows": len(exits) - len(latest_all),
            "details": collisions,
            "groups_through_snapshot": len(through_collisions),
        },
        "snapshot": {
            "through": through,
            "latest_exit_retention": summarize_outcomes(latest_through),
            "earliest_exit_retention": summarize_outcomes(earliest_through),
            "schema_era_cohorts_latest": {
                "explicit_option_contract": {
                    **summarize_outcomes(snapshot_explicit_options),
                    "basis_sensitivity": infer_option_gross_capture(snapshot_explicit_options),
                },
                "legacy_identity_less": summarize_outcomes(snapshot_identity_less),
            },
        },
        "all_dates": {
            "latest_exit_retention": summarize_outcomes(latest_all),
            "earliest_exit_retention": summarize_outcomes(earliest_all),
            "grades_latest": summarize_grades(latest_all),
            "confidence_at_or_above_0_95_latest": summarize_high_confidence(latest_all),
            "paths_latest": summarize_paths(latest_all),
        },
        "schema_era_cohorts_latest": {
            "explicit_option_contract": {
                "date_range": [
                    min((str(row.get("date") or "") for row in explicit_options), default=""),
                    max((str(row.get("date") or "") for row in explicit_options), default=""),
                ],
                "outcomes": summarize_outcomes(explicit_options),
                "grades": summarize_grades(explicit_options),
                "confidence_at_or_above_0_95": summarize_high_confidence(explicit_options),
                "basis_sensitivity": infer_option_gross_capture(explicit_options),
            },
            "legacy_identity_less": {
                "date_range": [
                    min((str(row.get("date") or "") for row in identity_less), default=""),
                    max((str(row.get("date") or "") for row in identity_less), default=""),
                ],
                "outcomes": summarize_outcomes(identity_less),
                "grades": summarize_grades(identity_less),
            },
        },
        "archived_post_exit_coverage_latest": (
            summarize_archived_post_exit_coverage(latest_all, archive_dir)
            if archive_dir is not None
            else {"not_measured": True}
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-dir", type=Path, default=DEFAULT_EVENTS_DIR)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument(
        "--through",
        default="2026-08-21",
        help="Inclusive snapshot date used to reproduce the cited 74/327 headline",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    if not args.events_dir.is_dir():
        raise SystemExit(f"Events directory not found: {args.events_dir}")
    events = list(iter_events(args.events_dir))
    result = build_audit(events, through=args.through, archive_dir=args.archive_dir)
    rendered = json.dumps(result, indent=2, sort_keys=False)
    print(rendered)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
