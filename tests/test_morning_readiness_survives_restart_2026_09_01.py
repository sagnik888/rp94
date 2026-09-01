"""A restart must not un-do today's warmup.

2026-09-01 15:20. The operator switched Auto Mode on and got:

    Settings blocked: auto-execution readiness failed:
    morning readiness unavailable for 2026-09-01

The warmup had in fact completed three times that day -- 09:13:10, 13:33:32 and
15:26:45 -- but `_morning_refresh_summary` was an in-memory attribute, initialised to
{} in __init__ and never written anywhere. Every restart erased the proof, and the
auto-execution gate then refused until the scan loop rebuilt it, which took ~3 minutes:

    09:13:10  refresh COMPLETE
    13:31:33  RESTART      <- record erased
    13:33:32  refresh COMPLETE     (+2m)
    14:13:59  RESTART      <- record erased
    14:15:24  refresh COMPLETE     (+1.5m)
    15:23:39  RESTART      <- record erased
    15:26:45  refresh COMPLETE     (+3m)

The gate itself is right and stays: unattended REAL execution should not begin without
today's complete 4-index warmup. What was wrong is that it FORGOT a warmup that had
already happened, turning every restart into a silent three-minute outage on a control
the operator expects to work the moment they flip it.

The record is now written to disk and reloaded on demand. The session_day check is the
safety: a file from yesterday can never satisfy today's gate, so a stale record cannot
arm live execution on data that was never fetched.
"""
import datetime as dt
import json
from pathlib import Path

import pytest

from scanner import Scanner

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def _summary(session_day, complete=True):
    return {
        "session_day": session_day,
        "complete": complete,
        "history": True,
        "expiry": True,
        "index_quotes": 4,
        "chains": 4,
        "vix": True,
        "indices": {n: {"quote": True, "chain": True} for n in INDICES},
    }


def _scanner():
    """A scanner shell as it exists immediately after a restart: memory empty."""
    sc = Scanner.__new__(Scanner)
    sc._morning_refresh_summary = {}
    sc.active_indices = list(INDICES)
    sc._setting_enabled = lambda value, default=False: (
        bool(value) if value is not None else default
    )
    return sc


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Point the record at a temp file so the real one is never touched."""
    target = tmp_path / "morning_refresh_summary.json"
    monkeypatch.setattr(
        Scanner, "_morning_refresh_state_path", lambda self: target, raising=False
    )
    return target


def test_a_restart_keeps_todays_warmup(state_file):
    """The reported bug: Auto Mode refused at 15:20 for a warmup done at 09:13."""
    today = dt.date.today().isoformat()
    _scanner()._persist_morning_refresh_summary(_summary(today))

    after_restart = _scanner()
    assert after_restart._auto_execution_morning_readiness_blockers() == [], (
        "a restart still loses today's warmup, so Auto Mode is refused for ~3 minutes "
        "every time the process is restarted mid-session"
    )


def test_yesterdays_record_cannot_arm_today(state_file):
    """The safety the gate exists for: today's data, not any data."""
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _scanner()._persist_morning_refresh_summary(_summary(yesterday))

    blockers = _scanner()._auto_execution_morning_readiness_blockers()
    assert blockers, "a record from yesterday satisfied today's readiness gate"
    assert today in blockers[0]


def test_an_incomplete_warmup_still_blocks(state_file):
    """Restoring the record must not restore a PASS it never had."""
    today = dt.date.today().isoformat()
    partial = _summary(today, complete=False)
    partial["chains"] = 1
    partial["indices"]["SENSEX"]["chain"] = False
    _scanner()._persist_morning_refresh_summary(partial)

    blockers = _scanner()._auto_execution_morning_readiness_blockers()
    assert blockers, "an incomplete warmup was restored as if it were complete"
    assert "incomplete" in blockers[0]


def test_no_file_means_no_readiness(state_file):
    """A missing record must fail closed, not open."""
    assert not state_file.exists()
    blockers = _scanner()._auto_execution_morning_readiness_blockers()
    assert blockers and "unavailable" in blockers[0]


def test_a_corrupt_record_fails_closed(state_file):
    """Half-written JSON must block, not raise and not admit."""
    state_file.write_text("{not json", encoding="utf-8")
    blockers = _scanner()._auto_execution_morning_readiness_blockers()
    assert blockers and "unavailable" in blockers[0]


def test_the_refresh_writes_the_record():
    """The accessor can only restore what the refresh remembered to write."""
    import inspect

    source = inspect.getsource(Scanner)
    # EVERY site that sets the in-memory summary must mirror it, or disk can end up
    # staler than memory and a restart restores an older, rosier state. There are four:
    # the accessor's own restore, the attempt-cap path, the completion path and the
    # failure path. Only the accessor is exempt -- it is the reader.
    unmirrored = []
    for offset, line in enumerate(source.splitlines()):
        if "self._morning_refresh_summary = " not in line:
            continue
        window = chr(10).join(source.splitlines()[offset : offset + 6])
        if "_persist_morning_refresh_summary" in window:
            continue
        if "stored" in line:      # the accessor restoring FROM disk
            continue
        unmirrored.append(line.strip())
    assert not unmirrored, (
        "these summary writes do not reach disk, so a restart can lose or stale them: "
        + "; ".join(unmirrored)
    )


def test_persisting_never_breaks_the_refresh(state_file, monkeypatch):
    """Bookkeeping may not become a data-path failure mode."""
    def boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(Scanner, "_morning_refresh_state_path", boom, raising=False)
    _scanner()._persist_morning_refresh_summary(_summary(dt.date.today().isoformat()))
    # reaching here without raising is the assertion
