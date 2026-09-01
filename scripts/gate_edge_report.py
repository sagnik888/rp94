"""What each entry gate is actually worth, measured on what it refused.

A gate is only justified if the signals it blocks behave worse than the ones it
lets through. Nothing in this system measured that, so thresholds were argued
from counts — and the counts were inflated by duplicate rows (2026-08-12: 482
rows over 307 ids). This reads the archive, counts DISTINCT signals, and scores
each rejection family by what the tape did next.

The metric is excursion, not endpoint. A signal whose index drifts favourably but
first runs 0.3% against you is not a trade you wanted: it stops out on the way.
So each signal gets

    MAE  worst move against it,  over the window
    MFE  best move in its favour, over the window

and a family is summarised by median MFE / median MAE. Above 1 the gate is
refusing signals with more upside than heat; below 1 it is doing its job. Rank,
not level, is what matters — the same measurement error applies to every family.

Index candles are the proxy: for a bought option the index direction decides the
outcome, and 5-minute bars understate intrabar heat equally everywhere.

Read it across many sessions. One session cannot settle a threshold — a family
with n=2 clustered in one trend is one observation wearing a disguise, which is
exactly how "late-trend rejects your best signals" survived a first look at the
2026-08-12 data and did not survive the second.

Usage:
    python scripts/gate_edge_report.py                 # most recent session
    python scripts/gate_edge_report.py 2026-08-12
    python scripts/gate_edge_report.py --days 20       # pooled across sessions
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EVENTS_DIR = Path("data_store/rtracker/events")
CANDLE_DIR = Path("data_store/candles")
INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
MIN_N = 8          # below this a family is reported but never called actionable
MIN_SESSIONS = 3   # one session is one observation wearing a disguise
MAX_SESSION_SHARE = 0.5   # nor is a family half of which fired on a single day
WINDOW_MINUTES = 30


def reason_family(reason: Any) -> str:
    """Gate identity, without the live measurements its message embeds.

    Gate messages carry readings, and every distinct reading would otherwise mint
    a family of one — which is how an inflated count becomes a confident finding.
    Three shapes appear in this archive:

        "Late-trend entry: ADX 31 > 30 ..."        reading after a colon
        "Price-action ADVERSE [-0.40 vs -0.35]"    reading in brackets
        "Counter-session-bias LONG (session 7L/18S) needs >= 62%"

    Only what is STRUCTURALLY marked as a reading is stripped — text after a
    colon, or inside brackets. Bare numbers are left alone, because from one
    string there is no telling a live measurement from a configured threshold,
    and "Confidence below 50%" / "below 58%" are two gates whose floors are the
    whole point. Stripping every number would merge them and hide which floor did
    the work.

    "X below Y" is the exception, and it IS structural: whatever is below a
    threshold is the reading, and what it is below is the threshold. That is what
    the word means. Without this rule "B+ 55.983% below 58.000% confidence gate"
    minted a family of one per signal — 71 rows over 20 sessions became ~50
    families, every one of them n=1 and therefore stamped "not actionable", so the
    confidence gates disappeared from the ranking entirely. This keeps the grade and
    the floor, which are the two things that distinguish one confidence gate from
    another, and drops only the per-signal reading.
    """
    text = str(reason or "").strip()
    if not text:
        return "(unstated)"
    if ":" in text:
        text = text.split(":")[0]
    text = re.sub(r"[\[\(][^\]\)]*[\]\)]?", " ", text)      # bracketed readings
    text = re.sub(r",?\s*got\s+\S+.*$", "", text, flags=re.I)  # ", got 27%"
    # "<reading>% below <floor>%" -> "below <floor>%": drop the measurement, keep
    # the configured floor. Trailing zeros are normalised too, so one gate does not
    # split into "58%" and "58.000%" families over a formatting change.
    # The copula matters. This started as "<reading>% below <floor>%", which is how the
    # confidence gates phrase it. engine/entry_policy.py's final-entry gate says
    # "<reading>% IS below <floor>%", and the bare rule did not match across the verb --
    # so on 2026-08-28 one gate that refused ALL SEVEN of the session's final entries
    # fragmented into seven families of n=1, every one stamped "not actionable", which is
    # exactly the failure this function exists to prevent.
    text = re.sub(
        r"\b\d+(?:\.\d+)?\s*%\s+(?:is\s+|was\s+|are\s+|were\s+)?below\b",
        "below",
        text,
    )
    text = re.sub(r"\b(\d+)\.0+\s*%", lambda m: f"{m.group(1)}%", text)
    text = re.sub(r"\s+", " ", text).strip(" -,;")
    return text[:52] or "(unstated)"


def load_events(days: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for day in days:
        path = EVENTS_DIR / f"{day}.jsonl"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


def load_candles(days: List[str]):
    import pandas as pd

    out: Dict[Tuple[str, str], Any] = {}
    for index in INDICES:
        path = CANDLE_DIR / f"{index}_5min.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, parse_dates=["timestamp"])
        for day in days:
            sel = frame[frame.timestamp.dt.date == pd.Timestamp(day).date()]
            if not sel.empty:
                out[(index, day)] = sel.reset_index(drop=True)
    return out


def excursion(candles, index: str, day: str, signal_ts, direction: str) -> Optional[Tuple[float, float]]:
    """(MAE, MFE) as percentages, in the signal's own direction."""
    import pandas as pd

    frame = candles.get((index, day))
    if frame is None or not signal_ts:
        return None
    try:
        start_at = pd.Timestamp(signal_ts)
    except Exception:
        return None
    at = frame[frame.timestamp <= start_at]
    window = frame[
        (frame.timestamp > start_at)
        & (frame.timestamp <= start_at + pd.Timedelta(minutes=WINDOW_MINUTES))
    ]
    if at.empty or window.empty:
        return None
    start = float(at.iloc[-1].close)
    if start <= 0:
        return None
    short = str(direction or "").upper() in ("SHORT", "SELL")
    if short:
        mae = (float(window.high.max()) - start) / start * 100.0
        mfe = (start - float(window.low.min())) / start * 100.0
    else:
        mae = (start - float(window.low.min())) / start * 100.0
        mfe = (float(window.high.max()) - start) / start * 100.0
    return max(0.0, mae), max(0.0, mfe)


FULL_WINDOW_BARS = WINDOW_MINUTES // 5   # the candles are 5-minute


def window_bars(candles, index: str, day: str, signal_ts) -> int:
    """How many bars the excursion window actually got.

    A gate that fires near 15:30 is measured over a window the close cuts short.
    Fewer bars is less opportunity to move against the signal, so MAE collapses
    toward zero and the ratio explodes on arithmetic alone. The three families that
    topped this report at 17.21, 8.57 and 6.25 have median windows of 5, 2 and 4
    bars against 6 everywhere else, and MAEs of 0.010%, 0.009% and 0.017% against
    0.060-0.100% for full-window families. That is the clock, not gate quality.
    """
    import pandas as pd

    frame = candles.get((index, day))
    if frame is None or not signal_ts:
        return 0
    try:
        start_at = pd.Timestamp(signal_ts)
    except Exception:
        return 0
    return int(
        (
            (frame.timestamp > start_at)
            & (frame.timestamp <= start_at + pd.Timedelta(minutes=WINDOW_MINUTES))
        ).sum()
    )


def distinct_signals(rows: List[Dict[str, Any]], decision: str) -> List[Dict[str, Any]]:
    """One row per (signal, gate). Rows predating signal_id fall back to the key
    it is derived from, so historical archives are counted the same way."""
    seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        if str(row.get("decision") or "") != decision:
            continue
        ident = str(row.get("signal_id") or "") or "|".join(
            str(row.get(k) or "")
            for k in ("date", "instrument", "timeframe", "direction", "signal_timestamp")
        )
        seen.setdefault((ident, reason_family(row.get("reason"))), row)
    return list(seen.values())


def build(days: List[str]) -> Dict[str, Any]:
    rows = load_events(days)
    candles = load_candles(days)
    families: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    family_days: Dict[str, List[str]] = defaultdict(list)
    family_bars: Dict[str, List[int]] = defaultdict(list)

    for row in distinct_signals(rows, "REJECTED"):
        index = str(row.get("instrument") or "").split()[0].upper()
        day = str(row.get("date") or "")
        signal_ts = row.get("signal_timestamp") or row.get("timestamp")
        moved = excursion(
            candles, index, day, signal_ts,
            row.get("direction") or row.get("signal_type"),
        )
        if moved:
            name = reason_family(row.get("reason"))
            families[name].append(moved)
            family_days[name].append(day)
            family_bars[name].append(window_bars(candles, index, day, signal_ts))

    summary = []
    for name, points in families.items():
        maes = [p[0] for p in points]
        mfes = [p[1] for p in points]
        mae = statistics.median(maes)
        mfe = statistics.median(mfes)
        days_seen = family_days[name]
        sessions = len(set(days_seen))
        top_share = (
            max(days_seen.count(d) for d in set(days_seen)) / len(days_seen)
            if days_seen else 1.0
        )
        bars = statistics.median(family_bars[name]) if family_bars[name] else 0
        summary.append({
            "family": name,
            "n": len(points),
            "sessions": sessions,
            "top_session_share": round(top_share, 3),
            "median_window_bars": bars,
            "median_mae_pct": round(mae, 4),
            "median_mfe_pct": round(mfe, 4),
            "edge_ratio": round(mfe / mae, 2) if mae > 0 else None,
            # n alone was never enough. A family can clear MIN_N inside a single
            # session -- 8 rows on 2026-08-05 reached rank 4 of this table -- and a
            # family whose window the close truncated gets its ratio from the clock.
            "actionable": (
                len(points) >= MIN_N
                and sessions >= MIN_SESSIONS
                and top_share <= MAX_SESSION_SHARE
                and bars >= FULL_WINDOW_BARS
            ),
        })
    summary.sort(key=lambda r: (r["edge_ratio"] is None, -(r["edge_ratio"] or 0)))

    # Deduped, because one position can emit SEVERAL EXIT rows and this line is the
    # only number in the report that is not already counted distinct. On 2026-08-14 one
    # lifecycle emitted 8 EXIT rows and the raw sum printed "trades 21, realised
    # Rs.-1,125" for a session that was actually 14 trades and +Rs.353 -- the tool
    # reported a LOSS where there was a WIN, in a report used to judge gates.
    #
    # The archive has been clean since 2026-08-20 (intelligence/rtracker.py derives the
    # EXIT id from the lifecycle), but every window reaching further back still carries
    # the old duplicates, so this cannot be left to the writer alone.
    exit_rows = [r for r in rows if str(r.get("decision") or "") == "EXIT"]
    taken = list({
        (str(r.get("lifecycle_id") or ""), str(r.get("entry_timestamp") or "")): r
        for r in exit_rows
    }.values())
    raw_exit_rows = len(exit_rows)
    realised = sum(float(r.get("pnl") or 0.0) for r in taken)
    return {
        "days": days,
        "rows_read": len(rows),
        "distinct_rejected_signals": len(distinct_signals(rows, "REJECTED")),
        "trades": len(taken),
        # Kept so a future duplicate regression is visible rather than silently absorbed.
        "raw_exit_rows": raw_exit_rows,
        "realised_pnl": round(realised, 2),
        "families": summary,
    }


def render(report: Dict[str, Any]) -> str:
    lines = [
        f"gate edge report — {', '.join(report['days'])}",
        f"  rows read {report['rows_read']}, distinct rejected signals "
        f"{report['distinct_rejected_signals']}, trades {report['trades']}"
        + (
            f" (from {report['raw_exit_rows']} raw exit rows)"
            if report.get("raw_exit_rows", 0) != report["trades"]
            else ""
        )
        + f", realised Rs.{report['realised_pnl']:+.0f}",
        "",
        "  edge_ratio = median MFE / median MAE of what the gate REFUSED.",
        "  >1 it refused signals with more upside than heat; <1 it earned its place.",
        "",
        "  W = median window bars; 6.0 is a full 30 minutes. Fewer means the 15:30",
        "  close truncated it, which drives MAE to zero and inflates the ratio.",
        "  Shown to one decimal: an even-n family can median to 5.5, and rounding that",
        "  to \"6\" printed a full window next to a note saying it was truncated.",
        "",
        f"  {'n':>4} {'sess':>4} {'top%':>5} {'W':>4} {'MAE%':>7} {'MFE%':>7} {'ratio':>7}  family",
    ]
    for row in report["families"]:
        ratio = "  n/a" if row["edge_ratio"] is None else f"{row['edge_ratio']:>7.2f}"
        mark = "" if row["actionable"] else f"   ({_why_not_actionable(row)})"
        lines.append(
            f"  {row['n']:>4} {row['sessions']:>4} {row['top_session_share']:>5.0%} "
            f"{row['median_window_bars']:>4.1f} "
            f"{row['median_mae_pct']:>7.3f} {row['median_mfe_pct']:>7.3f} "
            f"{ratio}  {row['family']}{mark}"
        )
    return "\n".join(lines)


def _why_not_actionable(row: Dict[str, Any]) -> str:
    """Name the failing test, so a reader need not guess which one it was."""
    why = []
    if row["n"] < MIN_N:
        why.append(f"n<{MIN_N}")
    if row["sessions"] < MIN_SESSIONS:
        why.append(f"{row['sessions']} session{'s' if row['sessions'] != 1 else ''}")
    if row["top_session_share"] > MAX_SESSION_SHARE:
        why.append(f"{row['top_session_share']:.0%} in one session")
    if row["median_window_bars"] < FULL_WINDOW_BARS:
        why.append(f"window truncated to {row['median_window_bars']:.1f} bars")
    return ", ".join(why) + ", not actionable"


def recent_days(limit: int) -> List[str]:
    if not EVENTS_DIR.exists():
        return []
    days = sorted(p.stem for p in EVENTS_DIR.glob("*.jsonl"))
    return days[-max(1, limit):]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("day", nargs="?", help="session date, e.g. 2026-08-12")
    parser.add_argument("--days", type=int, default=1, help="pool the N most recent sessions")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    days = [args.day] if args.day else recent_days(args.days)
    if not days:
        print("no rtracker sessions found")
        return 1

    report = build(days)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
