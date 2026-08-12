"""The gate-edge report: family naming, distinct-signal counting, excursion.

A gate is only justified if what it refuses behaves worse than what it passes,
and nothing measured that. The report scores each rejection family by the
excursion of the signals it blocked — but it is only as good as two things:

- family naming, because gate messages embed live measurements. "Late-trend
  entry: ADX 31 > 30" and "ADX 37 > 30" are one gate, and "Price-action ADVERSE
  [-0.40" and "[-0.58" are one gate. Split them and every family reads n=1.
- distinct-signal counting, because the archive holds repeated rows and an
  inflated n is what turns a two-observation artifact into a confident finding.
"""
import json

import pytest

from scripts.gate_edge_report import (
    build, distinct_signals, excursion, reason_family,
)


# ── family naming ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason,expected", [
    ("Late-trend entry: ADX 31 > 30 with TRENDING_DOWN", "Late-trend entry"),
    ("Late-trend entry: ADX 37 > 30 with TRENDING_DOWN", "Late-trend entry"),
    ("Price-action ADVERSE [-0.40 vs -0.35]", "Price-action ADVERSE"),
    ("Price-action ADVERSE [-0.58 vs -0.35]", "Price-action ADVERSE"),
    # The session tally varies and is bracketed; the 62% floor identifies the gate.
    ("Counter-session-bias LONG (session 7L/18S) needs >= 62%",
     "Counter-session-bias LONG needs >= 62%"),
    ("Counter-session-bias LONG (session 12L/23S) needs >= 62%",
     "Counter-session-bias LONG needs >= 62%"),
    # "got N" is the observation; the floor before it identifies the gate.
    ("Counter-session-bias LONG needs >= 60%, got 27%",
     "Counter-session-bias LONG needs >= 60%"),
    ("Counter-session-bias LONG needs >= 60%, got 49%",
     "Counter-session-bias LONG needs >= 60%"),
    ("Choppy market blocked", "Choppy market blocked"),
    ("C grade blocked", "C grade blocked"),
])
def test_a_gate_gets_one_name_whatever_it_measured(reason, expected):
    assert reason_family(reason) == expected


def test_distinct_thresholds_stay_distinct():
    """Different configured floors are different gates, not one gate's readings."""
    assert reason_family("Confidence below 50%") != reason_family("Confidence below 58%")


@pytest.mark.parametrize("reason", [None, "", "   ", 42])
def test_an_unusable_reason_never_crashes_the_report(reason):
    assert isinstance(reason_family(reason), str)


# ── distinct-signal counting ─────────────────────────────────────────────────

def _row(sid, reason, decision="REJECTED"):
    return {
        "signal_id": sid, "decision": decision, "reason": reason,
        "instrument": "SENSEX", "timeframe": "5min", "direction": "SHORT",
        "date": "2026-08-12", "signal_timestamp": "2026-08-12T09:30:00",
    }


def test_one_signal_refused_repeatedly_counts_once():
    rows = [_row("s1", f"Late-trend entry: ADX {n} > 30") for n in (30, 31, 37)]

    assert len(distinct_signals(rows, "REJECTED")) == 1


def test_one_signal_refused_by_two_gates_counts_for_each():
    rows = [_row("s1", "Choppy market blocked"), _row("s1", "C grade blocked")]

    assert len(distinct_signals(rows, "REJECTED")) == 2


def test_rows_predating_signal_id_still_count_correctly():
    """The archive holds sessions written before the field existed."""
    rows = []
    for n in (30, 31, 37):
        r = _row("", f"Late-trend entry: ADX {n} > 30")
        r.pop("signal_id")
        rows.append(r)

    assert len(distinct_signals(rows, "REJECTED")) == 1


def test_it_only_counts_the_decision_asked_for():
    rows = [_row("s1", "x"), _row("s2", "y", decision="ACCEPTED")]

    assert len(distinct_signals(rows, "REJECTED")) == 1


# ── excursion ────────────────────────────────────────────────────────────────

def _frame(closes, highs, lows):
    import pandas as pd

    return pd.DataFrame({
        "timestamp": pd.date_range("2026-08-12 09:15", periods=len(closes), freq="5min"),
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": [1] * len(closes),
    })


def test_excursion_is_measured_in_the_signals_own_direction():
    frame = _frame([100, 101, 99], [100, 102, 101], [100, 100, 98])
    candles = {("NIFTY", "2026-08-12"): frame}

    long_mae, long_mfe = excursion(candles, "NIFTY", "2026-08-12",
                                   "2026-08-12 09:15", "LONG")
    short_mae, short_mfe = excursion(candles, "NIFTY", "2026-08-12",
                                     "2026-08-12 09:15", "SHORT")

    assert long_mfe == pytest.approx(2.0) and long_mae == pytest.approx(2.0)
    # The same tape, mirrored: what helped the long hurt the short.
    assert short_mae == pytest.approx(2.0) and short_mfe == pytest.approx(2.0)


def test_a_signal_with_no_forward_tape_is_skipped_not_scored():
    frame = _frame([100], [100], [100])
    candles = {("NIFTY", "2026-08-12"): frame}

    assert excursion(candles, "NIFTY", "2026-08-12", "2026-08-12 09:15", "LONG") is None


def test_a_missing_index_is_skipped():
    assert excursion({}, "NIFTY", "2026-08-12", "2026-08-12 09:15", "LONG") is None


# ── the report holds the line on small samples ───────────────────────────────

def test_a_small_family_is_reported_but_never_called_actionable(tmp_path, monkeypatch):
    import scripts.gate_edge_report as mod

    events = tmp_path / "events"
    events.mkdir(parents=True)
    with (events / "2026-08-12.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(2):
            fh.write(json.dumps({
                "signal_id": f"s{i}", "decision": "REJECTED",
                "reason": f"Late-trend entry: ADX {30 + i} > 30",
                "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
                "date": "2026-08-12",
                "signal_timestamp": f"2026-08-12T09:{15 + i * 5}:00",
            }) + "\n")
    monkeypatch.setattr(mod, "EVENTS_DIR", events)
    monkeypatch.setattr(mod, "load_candles", lambda days: {})

    report = build(["2026-08-12"])

    assert report["distinct_rejected_signals"] == 2
    assert all(f["actionable"] is False for f in report["families"])
