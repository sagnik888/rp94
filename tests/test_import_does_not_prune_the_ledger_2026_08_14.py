"""Building a db handle must not delete anything.

_init_db called cleanup_old_records() on every instantiation, suppressed only when
PYTEST_CURRENT_TEST was set. So anything that constructed a handle — a helper script,
an ad-hoc `python -c "import engine.signal_processor"`, any tooling that touches a
module which builds one — DELETED 30-day-old session_signals, signal_decisions and
closed trades from the production ledger and then VACUUMed it.

This was found the hard way. Roughly twenty diagnostic imports during the 2026-08-14
session each logged "Database cleanup completed and vacuumed" against the live
database. No rows were actually lost — the oldest signal_decisions row was
2026-07-16 against a 2026-07-15 cutoff, one day inside the window — but that is luck,
not safety. The next day's run would have taken real data.

The shape of the bug is the point: a read-shaped action with a destructive side
effect, where nothing at the call site looks like a delete.

Retention is unaffected. main.py:149 runs the prune deliberately once a month, gated
on a marker it writes only when the prune succeeds — a marker that was meaningless
while startup pruned on construction anyway.
"""
import inspect
import sqlite3
from datetime import datetime, timedelta

import pytest

from data import sqlite_db as db_module


def _seed(path, days_old):
    """Rows dated `days_old` days back, written into the handle's OWN database.

    _resolve_db_path redirects to an isolated file whenever PYTEST_CURRENT_TEST is
    set, so seeding a tmp_path file and then constructing a handle tests two different
    databases — which is how the first draft of this file passed vacuously.
    """
    stamp = (datetime.now() - timedelta(days=days_old)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO session_signals (date) VALUES (?)", (stamp,))
    conn.execute("INSERT INTO signal_decisions (date) VALUES (?)", (stamp,))
    conn.commit()
    conn.close()
    return stamp


def _counts(path):
    conn = sqlite3.connect(path)
    try:
        return (
            conn.execute("SELECT COUNT(*) FROM session_signals").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0],
        )
    finally:
        conn.close()


# ── the invariant ────────────────────────────────────────────────────────────

def _fresh_handle():
    """A handle plus its OWN database path, with any prior rows cleared."""
    handle = db_module.DatabaseManager()
    conn = sqlite3.connect(handle.db_path)
    conn.execute("DELETE FROM session_signals")
    conn.execute("DELETE FROM signal_decisions")
    conn.commit()
    conn.close()
    return handle, handle.db_path


def test_constructing_a_handle_deletes_nothing(monkeypatch):
    """Even with the pytest suppression lifted, construction must not prune."""
    handle, path = _fresh_handle()
    _seed(path, days_old=400)
    before = _counts(path)
    assert before == (1, 1), "sanity: the old rows must exist to be deletable"

    # Lift the very suppression that was hiding this, so the test proves the
    # behaviour rather than the escape hatch.
    monkeypatch.setattr(db_module, "_running_under_pytest", lambda: False)
    db_module.DatabaseManager(path)

    assert _counts(path) == before, "constructing a db handle pruned the ledger"


def test_init_db_no_longer_calls_cleanup():
    """Pinned at source: construction must not reach the delete path at all."""
    src = inspect.getsource(db_module.DatabaseManager._init_db)
    # Comments explain the removed call by name; only executable lines matter here.
    code = " ".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )

    assert "cleanup_old_records" not in code, (
        "construction prunes again; retention belongs to the deliberate monthly call"
    )


# ── retention still works when it is actually asked for ──────────────────────

def test_an_explicit_prune_still_deletes():
    """The cleanup contract is unchanged — only the automatic trigger is gone."""
    handle, path = _fresh_handle()
    _seed(path, days_old=400)

    assert handle.cleanup_old_records(days=30) is True

    assert _counts(path) == (0, 0), "an explicit prune should still remove old rows"


def test_an_explicit_prune_keeps_rows_inside_the_window():
    handle, path = _fresh_handle()
    _seed(path, days_old=2)

    handle.cleanup_old_records(days=30)

    assert _counts(path) == (1, 1), "a prune removed rows inside the retention window"


# ── the deliberate caller is still wired ─────────────────────────────────────

def test_the_monthly_prune_remains_the_owner_of_retention():
    """Removing the automatic call must not silently end retention."""
    import main

    src = inspect.getsource(main)

    assert "cleanup_old_records(days=31)" in src, (
        "nothing prunes any more: retention was dropped rather than relocated"
    )
