"""Legacy repair indexes are unnecessary when the current book has no repair exits."""

from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from scanner import Scanner


def row(identity, stamp, *, action="ENTRY", reason="", exit_at=None):
    return NS(lifecycle_id=identity, instrument="NIFTY", timeframe="5min", direction="LONG",
              inst_type="OPT", option_type="CE", signal_timestamp=stamp, source_lane="S1",
              status="TRADE SIGNAL", action=action, exit_reason=reason, exit_timestamp=exit_at)


def repair_host():
    host = Scanner.__new__(Scanner)
    host._session_trade_candidates = {}
    return host


def legacy_book():
    old = datetime(2026, 11, 2, 10, 0)
    refresh = datetime(2026, 11, 2, 10, 5)
    return {
        "old": row("old", old),
        "refresh": row("refresh", refresh),
        "exit": row("old", old, action="EXIT", reason="SUPERSEDED_SIGNAL", exit_at=refresh),
    }


def test_healthy_books_skip_entry_identity_and_minute_indexes():
    host = repair_host()
    stamp = datetime(2026, 11, 2, 10, 0)
    for instrument in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"):
        host._session_trade_candidates[instrument] = {
            str(index): row(str(index), stamp) for index in range(150)
        }
    calls = []
    host._candidate_lifecycle_key = lambda candidate: calls.append(candidate) or candidate.lifecycle_id

    assert host._repair_same_side_superseded_session_rows() is False
    assert calls == []


@pytest.mark.parametrize("change", ["insert", "reason", "action", "entry_action", "source", "hydrate"])
def test_repair_always_observes_in_place_changes_and_replaced_books(change):
    host = repair_host()
    book = legacy_book()
    exit_row = book["exit"]
    if change == "insert":
        book.pop("exit")
    elif change == "reason":
        exit_row.exit_reason = "STOP_HIT"
    elif change == "action":
        exit_row.action = "NO_ENTRY"
    elif change == "entry_action":
        book["refresh"].action = "NO_ENTRY"
    elif change == "source":
        book["refresh"].direction = "SHORT"
        book["refresh"].source_lane = "S2"
    elif change == "hydrate":
        book = {}
    host._session_trade_candidates = {"NIFTY": book}

    assert host._repair_same_side_superseded_session_rows() is False

    if change == "insert":
        book["exit"] = exit_row
    elif change == "reason":
        exit_row.exit_reason = "SUPERSEDED_SIGNAL"
    elif change == "action":
        exit_row.action = "EXIT"
    elif change == "entry_action":
        book["refresh"].action = "ENTRY"
        book["refresh"].status = "TRADE SIGNAL"
    elif change == "source":
        book["refresh"].direction = "LONG"
        book["refresh"].source_lane = "S1"
    elif change == "hydrate":
        host.mode = "HISTORICAL"
        host._session_candidate_day = "2026-11-03"
        book = legacy_book()
        host._session_trade_candidates = {"NIFTY": book}

    assert host._repair_same_side_superseded_session_rows() is True
    assert set(book) == {"refresh"}
    assert host._repair_same_side_superseded_session_rows() is False
