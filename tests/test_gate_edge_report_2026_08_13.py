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
    # "X below Y": the reading is what is below, the floor is what it is below.
    # Left unstripped these minted a family per signal -- 71 rows over 20 sessions
    # became ~50 families, all n=1, all "not actionable", so the confidence gates
    # vanished from the ranking entirely.
    ("B+ 55.983% below 58.000% confidence gate", "B+ below 58% confidence gate"),
    ("B+ 57.534% below 58.000% confidence gate", "B+ below 58% confidence gate"),
    ("B 48.024% below 58.000% confidence gate", "B below 58% confidence gate"),
])
def test_a_gate_gets_one_name_whatever_it_measured(reason, expected):
    assert reason_family(reason) == expected


def test_the_grade_and_the_floor_both_still_identify_the_gate():
    """Collapsing the reading must not collapse the two things that are NOT readings."""
    b_plus_58 = reason_family("B+ 55.983% below 58.000% confidence gate")
    b_plus_62 = reason_family("B+ 61.326% below 62.000% confidence gate")
    b_58 = reason_family("B 48.024% below 58.000% confidence gate")

    assert b_plus_58 != b_plus_62, "different floors are different gates"
    assert b_plus_58 != b_58, "different grades are different gates"


def test_a_floor_is_the_same_floor_however_it_was_formatted():
    assert reason_family("B+ 55.9% below 58.000% confidence gate") == reason_family(
        "B+ 55.9% below 58% confidence gate"
    )


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


# ── the actionable test ──────────────────────────────────────────────────────
#
# n alone was never enough, and the report's own top rows proved it. Ranks 1-3
# (17.21, 8.57, 6.25) were gates that fire near 15:30: the close truncates their
# 30-minute window to a median 5, 2 and 4 bars against 6 everywhere else, so
# there is less room to move against the signal, MAE collapses to 0.009-0.017%
# against 0.060-0.100% elsewhere, and the ratio explodes on arithmetic alone.
# Rank 4 was 8 rows that all fired on 2026-08-05 -- one session wearing a
# disguise, which MIN_N passed because the tool never counted sessions.

def _points(n, day="2026-08-12", bars=6):
    import pandas as pd

    rows = []
    for i in range(n):
        rows.append({
            "signal_id": f"s{i}", "decision": "REJECTED", "reason": "Choppy market blocked",
            "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
            "date": day, "signal_timestamp": f"{day} 09:15:00",
        })
    return rows


def test_a_truncated_window_is_never_actionable(monkeypatch):
    """MAE has nowhere to go in a 2-bar window, so the ratio is the clock."""
    import scripts.gate_edge_report as ger

    monkeypatch.setattr(ger, "load_events", lambda days: _points(20))
    monkeypatch.setattr(ger, "load_candles", lambda days: {})
    monkeypatch.setattr(ger, "excursion", lambda *a, **k: (0.01, 0.17))
    monkeypatch.setattr(ger, "window_bars", lambda *a, **k: 2)

    family = ger.build(["2026-08-12"])["families"][0]

    assert family["n"] == 20
    assert family["median_window_bars"] == 2
    assert family["actionable"] is False


def test_one_session_is_never_actionable_however_many_rows(monkeypatch):
    """The regression: 8 rows on 2026-08-05 reached rank 4 of the table."""
    import scripts.gate_edge_report as ger

    monkeypatch.setattr(ger, "load_events", lambda days: _points(40))
    monkeypatch.setattr(ger, "load_candles", lambda days: {})
    monkeypatch.setattr(ger, "excursion", lambda *a, **k: (0.05, 0.19))
    monkeypatch.setattr(ger, "window_bars", lambda *a, **k: 6)

    family = ger.build(["2026-08-05"])["families"][0]

    assert family["n"] == 40 and family["sessions"] == 1
    assert family["actionable"] is False


def test_a_family_that_clears_every_test_is_actionable(monkeypatch):
    import scripts.gate_edge_report as ger

    rows = []
    for day in ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"):
        rows.extend(
            dict(r, signal_id=f"{day}-{r['signal_id']}") for r in _points(5, day=day)
        )
    monkeypatch.setattr(ger, "load_events", lambda days: rows)
    monkeypatch.setattr(ger, "load_candles", lambda days: {})
    monkeypatch.setattr(ger, "excursion", lambda *a, **k: (0.06, 0.09))
    monkeypatch.setattr(ger, "window_bars", lambda *a, **k: 6)

    family = ger.build(["x"])["families"][0]

    assert family["n"] == 20 and family["sessions"] == 4
    assert family["top_session_share"] == 0.25
    assert family["actionable"] is True


def test_the_render_names_which_test_a_family_failed():
    from scripts.gate_edge_report import _why_not_actionable

    why = _why_not_actionable({
        "n": 8, "sessions": 1, "top_session_share": 1.0, "median_window_bars": 2,
    })

    assert "1 session" in why
    assert "100% in one session" in why
    assert "window truncated to 2.0 bars" in why


def test_a_half_bar_median_is_not_rounded_into_a_contradiction():
    """An even-n family medians to x.5. Rounded to "6" the row printed a FULL window
    in the W column and "window truncated to 6 bars" in the note beside it -- the
    report denying, in the same line, the reason it excluded the family. Python's
    round-half-even makes 5.5 the only value that collides with the full-window
    number (4.5 -> 4, 2.5 -> 2), which is exactly the boundary that matters."""
    from scripts.gate_edge_report import _why_not_actionable

    why = _why_not_actionable({
        "n": 12, "sessions": 4, "top_session_share": 0.3, "median_window_bars": 5.5,
    })

    assert "window truncated to 5.5 bars" in why
    assert "6 bars" not in why


def test_window_bars_counts_only_what_the_close_left():
    import pandas as pd
    from scripts.gate_edge_report import window_bars

    # Session ends 15:30; a signal at 15:15 gets three bars, not six.
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-12 15:00", "2026-08-12 15:30", freq="5min"),
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1,
    })
    candles = {("NIFTY", "2026-08-12"): frame}

    assert window_bars(candles, "NIFTY", "2026-08-12", "2026-08-12 15:15") == 3
    assert window_bars(candles, "NIFTY", "2026-08-12", "2026-08-12 15:00") == 6
    assert window_bars(candles, "NIFTY", "2026-08-12", None) == 0
    assert window_bars({}, "NIFTY", "2026-08-12", "2026-08-12 15:00") == 0


# ── the copula ───────────────────────────────────────────────────────────────
#
# The "<reading>% below <floor>%" rule landed 2026-08-25 for the confidence gates.
# engine/entry_policy.py's final-entry gate phrases the same shape with a verb --
# "<reading>% IS below <floor>%" -- and the bare rule did not match across it. On
# 2026-08-28 that gate refused ALL SEVEN of the session's final entries and fragmented
# into seven families of n=1, every one stamped "not actionable": the exact failure
# reason_family exists to prevent, reintroduced by a phrasing nobody had seen yet.

@pytest.mark.parametrize("reason,expected", [
    ("directional expected favorable move 0.0447% is below 0.1500% floor",
     "directional expected favorable move below 0.1500% fl"),
    ("directional expected favorable move 0.0291% is below 0.1500% floor",
     "directional expected favorable move below 0.1500% fl"),
    ("B+ 55.9% was below 58.000% confidence gate", "B+ below 58% confidence gate"),
    ("readings 12.5% are below 20.000% floor", "readings below 20% floor"),
])
def test_a_copula_between_the_reading_and_below_does_not_split_the_family(reason, expected):
    assert reason_family(reason) == expected


def test_the_copula_rule_still_separates_distinct_floors():
    """Collapsing the reading must not collapse the thresholds either side of it."""
    a = reason_family("directional expected favorable move 0.0447% is below 0.1500% floor")
    b = reason_family("directional expected favorable move 0.0451% is below 0.2500% floor")

    assert a != b, "two different floors were merged into one family"


# ── trades and realised P&L must be deduped like everything else ─────────────
#
# Every rejection family in this report is counted DISTINCT -- that is the tool's whole
# premise. `trades` and `realised` were summed from RAW EXIT rows, and one position can
# emit several. On 2026-08-14 a single lifecycle emitted 8 EXIT rows and the report
# printed "trades 21, realised Rs.-1,125" for a session that was 14 trades and +Rs.353.
# It reported a LOSS where there was a WIN, in the report used to judge gate thresholds.

def _exit_row(lifecycle, entry_ts, pnl):
    return {
        "decision": "EXIT", "lifecycle_id": lifecycle, "entry_timestamp": entry_ts,
        "pnl": pnl, "instrument": "NIFTY", "timeframe": "5min", "direction": "LONG",
        "date": "2026-08-14", "signal_timestamp": "2026-08-14 12:37:00", "reason": "",
    }


def test_one_position_emitting_many_exit_rows_counts_once(monkeypatch):
    import scripts.gate_edge_report as ger

    rows = [_exit_row("LC-1", "2026-08-14T12:37:25.534426", 353.0) for _ in range(8)]
    rows.append(_exit_row("LC-2", "2026-08-14T13:01:00.000000", -100.0))
    monkeypatch.setattr(ger, "load_events", lambda days: rows)
    monkeypatch.setattr(ger, "load_candles", lambda days: {})

    report = ger.build(["2026-08-14"])

    assert report["trades"] == 2, "duplicate EXIT rows were counted as separate trades"
    assert report["realised_pnl"] == pytest.approx(253.0)
    assert report["raw_exit_rows"] == 9


def test_the_raw_row_count_is_kept_so_a_regression_stays_visible(monkeypatch):
    """Silently absorbing duplicates would hide the writer regressing again."""
    import scripts.gate_edge_report as ger

    rows = [_exit_row("LC-1", "2026-08-14T12:37:25", 10.0) for _ in range(3)]
    monkeypatch.setattr(ger, "load_events", lambda days: rows)
    monkeypatch.setattr(ger, "load_candles", lambda days: {})

    rendered = ger.render(ger.build(["2026-08-14"]))

    assert "trades 1 (from 3 raw exit rows)" in rendered


def test_no_duplicates_means_no_noise_in_the_header(monkeypatch):
    import scripts.gate_edge_report as ger

    rows = [_exit_row("LC-1", "t1", 10.0), _exit_row("LC-2", "t2", 20.0)]
    monkeypatch.setattr(ger, "load_events", lambda days: rows)
    monkeypatch.setattr(ger, "load_candles", lambda days: {})

    rendered = ger.render(ger.build(["2026-08-14"]))

    assert "trades 2," in rendered
    assert "raw exit rows" not in rendered
