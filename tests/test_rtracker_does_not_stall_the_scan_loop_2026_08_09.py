"""The 30-minute report job held the lock the scanner event loop takes.

`_RTRACKER_FILE_LOCK` is a module-global RLock. The event loop takes it
synchronously -- `_rtracker_record_candidate` is a plain method called from inside
`_scan_cycle` -- and the append it guards is microseconds of work.

But `load_events` held that same lock across the ENTIRE archive read, and
`generate_weekly_reports` re-took it across every summary build and every file write.
Measured on the live store: 45 files, 83.1 MB, 47,357 events, ~1s just to read and
json.loads. The report runs on an asyncio.to_thread worker, so the CPU cost was
correctly off-loop -- but an RLock is only reentrant per-thread, so the scan loop
blocked on the lock anyway, every 30 minutes during live hours. The 100ms exit monitor
is downstream of that.

Two changes:

  1. load_events snapshots the file list under the lock and parses outside it. Closed
     day-files are immutable; only today's is appended to, and a torn trailing line
     was already tolerated.
  2. Reports build outside any lock and write under a SEPARATE lock, so the scan loop
     never queues behind report work at all.

Separately, the archive was never pruned -- `max_weeks` bounds the read window only,
so the store grew ~1.4 MB/day forever. Pruning now exists but is OPT-IN
(rtracker_retention_days=0 by default), because it destroys recorded evidence, and it
refuses to delete inside the report's own read window.
"""
import ast
import inspect
import json
import textwrap
import threading
import time
from datetime import datetime, timedelta

import pytest

from intelligence import rtracker as rt
from intelligence.rtracker import RegimeTracker


@pytest.fixture
def store(tmp_path):
    return RegimeTracker(root_dir=tmp_path / "rtracker", enabled=True, max_weeks=2)


def _seed(store, days=14, per_day=200, as_of=None):
    as_of = as_of or datetime(2026, 8, 9, 10, 0)
    store.events_dir.mkdir(parents=True, exist_ok=True)
    for d in range(days):
        day = (as_of - timedelta(days=d)).date()
        path = store.events_dir / f"{day.isoformat()}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for i in range(per_day):
                fh.write(json.dumps({
                    "id": f"{day}-{i}", "date": day.isoformat(),
                    "timestamp": f"{day}T10:{i % 60:02d}:00", "schema": "rtracker.v1",
                    "instrument": ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"][i % 4],
                    "decision": "ENTER", "direction": "LONG" if i % 2 else "SHORT",
                }) + "\n")
    return as_of


# ── the lock is not held across the read ─────────────────────────────────────

def test_the_append_lock_is_not_held_across_the_archive_parse(store, monkeypatch):
    """The headline, measured rather than asserted structurally.

    The read is slowed deliberately so the window is unambiguous: if the lock is held
    across it, a recorder cannot get in DURING the read, only after. A generous
    timeout here would pass against the buggy code too, so this asserts entry while
    the read is provably still running.
    """
    as_of = _seed(store, days=14, per_day=60)

    reading = threading.Event()
    real_loads = json.loads
    calls = {"n": 0}

    def _slow_loads(s, *a, **k):
        calls["n"] += 1
        if calls["n"] == 5:
            reading.set()
        time.sleep(0.001)
        return real_loads(s, *a, **k)

    monkeypatch.setattr(rt.json, "loads", _slow_loads)

    done = threading.Event()
    reader = threading.Thread(
        target=lambda: (store.load_events(weeks=2, as_of=as_of), done.set()), daemon=True
    )
    reader.start()
    assert reading.wait(5.0), "the read never started"

    got = rt._RTRACKER_FILE_LOCK.acquire(timeout=0.5)
    mid_read = not done.is_set()
    if got:
        rt._RTRACKER_FILE_LOCK.release()
    reader.join(30.0)

    assert mid_read, "the read finished too fast to prove anything — raise per_day"
    assert got, "a recorder could not take the append lock while the archive was read"


def test_load_events_parses_outside_the_lock():
    src = textwrap.dedent(inspect.getsource(RegimeTracker.load_events))
    tree = ast.parse(src)
    withs = [n for n in ast.walk(tree)
             if isinstance(n, ast.With)
             and "_RTRACKER_FILE_LOCK" in ast.dump(n)]
    assert len(withs) == 1, "expected exactly one locked region in load_events"
    # Node-level, not substring: glob("*.jsonl") legitimately contains "json".
    inner = list(ast.walk(withs[0]))
    assert not [n for n in inner if isinstance(n, (ast.For, ast.While))], (
        "the archive is still iterated inside the append lock"
    )
    parses = [n for n in inner if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr in {"loads", "open", "read"}]
    assert not parses, "the archive is still parsed inside the append lock"


def test_the_report_does_not_take_the_append_lock():
    src = inspect.getsource(RegimeTracker.generate_weekly_reports)
    assert "_RTRACKER_FILE_LOCK" not in src, (
        "the report still serialises against the lock the scan loop takes"
    )
    assert "_RTRACKER_REPORT_LOCK" in src


def test_the_two_locks_are_distinct_objects():
    assert rt._RTRACKER_FILE_LOCK is not rt._RTRACKER_REPORT_LOCK


def test_the_append_still_holds_the_lock():
    """The append is the thing that genuinely needs serialising. Don't lose it."""
    src = inspect.getsource(RegimeTracker.record_event)
    assert "_RTRACKER_FILE_LOCK" in src


# ── behaviour is unchanged ───────────────────────────────────────────────────

def test_the_same_events_come_back(store):
    as_of = _seed(store, days=14, per_day=50)
    events = store.load_events(weeks=2, as_of=as_of)
    assert len(events) == 14 * 50
    assert events == sorted(events, key=lambda e: str(e.get("timestamp") or ""))


def test_a_file_vanishing_mid_read_does_not_raise(store, monkeypatch):
    """Now that the snapshot and the read are separate, the file can go away."""
    as_of = _seed(store, days=3, per_day=10)
    real_glob = store.events_dir.glob

    def _glob_then_delete(pattern):
        paths = sorted(real_glob(pattern))
        paths[0].unlink()
        return iter(paths)

    monkeypatch.setattr(type(store.events_dir), "glob",
                        lambda self, p: _glob_then_delete(p), raising=False)
    assert len(store.load_events(weeks=2, as_of=as_of)) == 20


def test_a_torn_trailing_line_is_still_skipped(store):
    as_of = _seed(store, days=1, per_day=5)
    path = next(store.events_dir.glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "half-writ')
    assert len(store.load_events(weeks=2, as_of=as_of)) == 5


def test_reports_are_still_written(store):
    as_of = _seed(store, days=7, per_day=20)
    out = store.generate_weekly_reports(weeks=2, as_of=as_of)
    assert out["events"] == 140
    assert out["weekly_reports"]
    assert store.reports_dir.exists()


# ── retention is opt-in and cannot eat the read window ───────────────────────

def test_nothing_is_pruned_by_default(store):
    as_of = _seed(store, days=40, per_day=5)
    before = len(list(store.events_dir.glob("*.jsonl")))
    assert store.prune_events(as_of=as_of)["pruned"] == 0
    assert len(list(store.events_dir.glob("*.jsonl"))) == before


def test_the_default_is_keep_everything():
    assert RegimeTracker(root_dir="x").retention_days == 0
    from config.settings import get_settings
    assert int(getattr(get_settings(), "rtracker_retention_days", 0) or 0) >= 0


def test_pruning_removes_only_files_past_the_window(tmp_path):
    store = RegimeTracker(root_dir=tmp_path / "r", max_weeks=2, retention_days=14)
    as_of = _seed(store, days=40, per_day=5)
    out = store.prune_events(as_of=as_of)
    # Seeded d=0..39 back from as_of; cutoff is as_of-14d and files ON the cutoff are
    # kept, so d=0..14 survive.
    assert out["pruned"] == 25, out
    left = sorted(p.stem for p in store.events_dir.glob("*.jsonl"))
    assert len(left) == 15
    assert min(left) == (as_of.date() - timedelta(days=14)).isoformat()
    assert out["bytes"] > 0


def test_retention_can_never_cut_into_the_report_window(tmp_path):
    """retention_days=1 with an 8-week report would delete what the summary reads."""
    store = RegimeTracker(root_dir=tmp_path / "r", max_weeks=8, retention_days=1)
    as_of = _seed(store, days=40, per_day=2)
    assert store.prune_events(as_of=as_of)["pruned"] == 0
    assert len(list(store.events_dir.glob("*.jsonl"))) == 40


def test_a_pruned_store_still_reports(tmp_path):
    store = RegimeTracker(root_dir=tmp_path / "r", max_weeks=2, retention_days=14)
    as_of = _seed(store, days=40, per_day=5)
    out = store.generate_weekly_reports(weeks=2, as_of=as_of)
    assert out["pruned_day_files"] == 25
    # The 2-week read window (as_of-13d .. as_of) survives the prune intact.
    assert out["events"] == 14 * 5


def test_junk_filenames_are_left_alone(tmp_path):
    store = RegimeTracker(root_dir=tmp_path / "r", max_weeks=1, retention_days=7)
    _seed(store, days=1, per_day=1)
    (store.events_dir / "notadate.jsonl").write_text("{}\n", encoding="utf-8")
    store.prune_events(as_of=datetime(2026, 8, 9))
    assert (store.events_dir / "notadate.jsonl").exists()


def test_pruning_a_missing_directory_is_a_no_op(tmp_path):
    store = RegimeTracker(root_dir=tmp_path / "never", retention_days=30)
    assert store.prune_events()["pruned"] == 0


# ── the archive records one scale, not two ───────────────────────────────────

def test_the_recorded_score_norm_is_canonical_for_both_shapes():
    from types import SimpleNamespace

    tracker = RegimeTracker(root_dir="x")
    cand = SimpleNamespace(instrument="NIFTY", intelligence_score=0.42, market_intelligence_context={
        "aggregate": {"score": 82.3, "score_norm": 0.823}})
    ev = tracker._event_from_candidate(cand, decision="ENTER", reason="", source="t", extra=None)
    assert ev["aggregate"]["score_norm"] == pytest.approx(0.823)


def test_a_legacy_aggregate_without_score_norm_is_still_divided():
    from types import SimpleNamespace

    tracker = RegimeTracker(root_dir="x")
    cand = SimpleNamespace(instrument="NIFTY", intelligence_score=0.42, market_intelligence_context={
        "aggregate": {"score": 82.3}})
    ev = tracker._event_from_candidate(cand, decision="ENTER", reason="", source="t", extra=None)
    assert ev["aggregate"]["score_norm"] == pytest.approx(0.823)
