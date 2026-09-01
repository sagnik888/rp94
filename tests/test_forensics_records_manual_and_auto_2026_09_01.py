"""Forensics must record signals in BOTH execution modes, and say which.

2026-09-01. The operator reported the Signal Forensics tab as dead, still showing an
old 30-Aug date after a restart. Everything in the pipeline was verified working -- the
endpoint returned status ok, _record_trade_forensics produced events when called
directly, and db.append_signal_lifecycle_events persisted a probe row. The table
genuinely held ONE row: the LFC-TEST-12345 fixture, which the server hides as synthetic.
The 30-Aug date was simply the only session that existed.

The cause was structural. Forensics received rows only from TradeManager -- that is,
only from signals that became TRADES. Auto Mode is Manual, so signals are announced and
never become trades, and an operator running manually had a permanently empty flight
recorder. Recording only auto-executed trades also makes the record useless for the
comparison that matters: what the system chose, against what it would have chosen.

So acceptance is now recorded in both modes, stamped AUTO or MANUAL:

  * scanner._record_signal_forensics() writes one row per accepted signal at
    _record_accepted_entry(), the acceptance choke point every route reaches,
  * TradeManager's later trade-level transitions carry the same stamp,
  * the stamp is a dedicated `execution_mode` column, NOT `lane` -- lane already means
    the signal's source lane (fast/slow) and overloading it would destroy that.

HISTORICAL stays excluded, exactly as the trade writer excludes it: a replay must never
fill the recorder with rows the operator never traded. That separation is the same one
that let a -Rs 1,106 session display as +Rs 6,065.
"""
import sqlite3
import time
from types import SimpleNamespace

import pytest

from scanner import Scanner


@pytest.fixture
def scanner():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.is_warmup = False
    sc.auto_mode = False
    return sc


def _candidate(lifecycle_id):
    return SimpleNamespace(
        lifecycle_id=lifecycle_id, signal_id="S1", instrument="NIFTY",
        trading_symbol="NIFTY2690824100PE", symbol_token="1", timeframe="5min",
        direction="SHORT", grade="B+", confidence=0.67, source_engine="utbot",
        source_lane="fast", price=132.5, entry_price=132.5, stop=121.9, target=149.0,
        entry_quote_source="kite_ws", entry_quote_verified=True, contract=None,
    )


def _captured(monkeypatch):
    """Capture events at the DB boundary; the real write is dispatched off-thread."""
    seen = []
    import data.sqlite_db as sq
    monkeypatch.setattr(sq.db, "append_signal_lifecycle_events",
                        lambda events: seen.extend(events) or len(events))
    return seen


@pytest.mark.parametrize("auto,expected", [(True, "AUTO"), (False, "MANUAL")])
def test_an_accepted_signal_is_recorded_in_both_modes(scanner, monkeypatch, auto, expected):
    """The whole point: a manual signal never becomes a trade, so it was never recorded."""
    seen = _captured(monkeypatch)
    scanner.auto_mode = auto

    scanner._record_signal_forensics(_candidate("LC-1"), None)
    for _ in range(40):
        if seen:
            break
        time.sleep(0.05)

    assert seen, f"no forensics event written for {expected} mode"
    event = seen[0]
    assert event["execution_mode"] == expected
    assert event["phase"] == "ACCEPTED"
    assert event["lifecycle_id"] == "LC-1"


def test_the_mode_is_not_smuggled_into_lane(scanner, monkeypatch):
    """`lane` means the signal's source lane and must keep meaning that."""
    seen = _captured(monkeypatch)
    scanner._record_signal_forensics(_candidate("LC-2"), None)
    for _ in range(40):
        if seen:
            break
        time.sleep(0.05)

    assert seen[0]["lane"] == "fast", "the source lane was overwritten by the mode"
    assert seen[0]["execution_mode"] == "MANUAL"


def test_the_decision_value_stays_filterable(scanner, monkeypatch):
    """The dashboard validates `decision` against a fixed set before filtering."""
    from dashboard.server import _FORENSIC_DECISIONS, _FORENSIC_PHASES

    seen = _captured(monkeypatch)
    scanner._record_signal_forensics(_candidate("LC-3"), None)
    for _ in range(40):
        if seen:
            break
        time.sleep(0.05)

    assert seen[0]["decision"] in _FORENSIC_DECISIONS, (
        "the decision value is not in _FORENSIC_DECISIONS, so the dashboard filter "
        "would silently drop it"
    )
    assert seen[0]["phase"] in _FORENSIC_PHASES


def test_a_replay_writes_nothing(scanner, monkeypatch):
    """HISTORICAL must never fill the recorder; that is the lane separation."""
    seen = _captured(monkeypatch)
    scanner.mode = "HISTORICAL"

    scanner._record_signal_forensics(_candidate("LC-REPLAY"), None)
    time.sleep(0.3)

    assert seen == [], "a replayed signal reached the forensics recorder"


def test_a_signal_without_a_lifecycle_is_skipped(scanner, monkeypatch):
    """The lifecycle id is the join key; a row without one cannot be followed."""
    seen = _captured(monkeypatch)
    scanner._record_signal_forensics(_candidate(""), None)
    time.sleep(0.3)
    assert seen == []


def test_recording_never_breaks_acceptance(scanner, monkeypatch):
    """Forensics is observability and may not become an entry failure mode."""
    import data.sqlite_db as sq

    def boom(_events):
        raise RuntimeError("db down")

    monkeypatch.setattr(sq.db, "append_signal_lifecycle_events", boom)
    scanner._record_signal_forensics(_candidate("LC-4"), None)
    time.sleep(0.3)
    # reaching here without raising is the assertion


def test_the_column_exists_and_round_trips():
    """The stamp is useless if the schema drops it."""
    from data.sqlite_db import db  # noqa: F401  (import applies the migration)

    columns = {
        row[1]
        for row in sqlite3.connect("data_store/utbot_data.db").execute(
            "PRAGMA table_info(signal_lifecycle_events)"
        )
    }
    assert "execution_mode" in columns, (
        "signal_lifecycle_events has no execution_mode column; the AUTO/MANUAL stamp "
        "would be silently discarded on insert"
    )


def test_the_ledger_shows_the_mode():
    """A stamp the operator cannot see does not answer the question that was asked."""
    from pathlib import Path

    script = Path("dashboard/static/js/signal-forensics.js").read_text(encoding="utf-8")
    assert "item.execution_mode" in script, (
        "the forensics ledger does not render execution_mode, so AUTO and MANUAL rows "
        "are indistinguishable on screen"
    )
    row_at = script.index("html += `<tr tabindex")
    window = script[row_at : row_at + 2600]
    assert "execution_mode" in window, "the mode is not rendered in the row template"
    assert "esc(item.lane)" in window, "the source lane stopped being displayed"
