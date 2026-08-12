"""The funnel must count distinct SIGNALS, not scan cycles.

Measured on the 2026-08-12 archive: 482 rows, 307 distinct ids, and 59 ids
written MORE THAN ONCE — 175 duplicate rows. One id was written seven times:
same NIFTY 5min LONG, same signal timestamp, same reason.

So the dedupe KEY was right and the suppression was not. `_seen` is an in-memory
set on the tracker instance, and a fresh instance starts empty — there were six
process starts that day. Any id written before a restart was writable again
after it.

That inflation lands directly under every tuning decision: a gate that reads
n=11 when the truth is n=2 turns a two-observation artifact into a
confident-looking conclusion. So the dedupe is made durable — a day's ids are
recovered from that day's file before the first write to it.

A genuine change of outcome (rejected, then later accepted) or of reason is new
information and still records.
"""
from types import SimpleNamespace

import pytest

from intelligence.rtracker import RegimeTracker


def _tracker(tmp_path):
    return RegimeTracker(root_dir=tmp_path, enabled=True)


def _cand(signal_ts="2026-08-12T09:30:00", instrument="SENSEX", direction="SHORT"):
    return SimpleNamespace(
        instrument=instrument, timeframe="5min", direction=direction,
        signal_timestamp=signal_ts, signal_type="SELL",
        confidence=0.8, grade="A+", price=100.0,
    )


def _rows(tmp_path):
    out = []
    for path in (tmp_path / "events").glob("*.jsonl"):
        out.extend(l for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
    return out


def test_the_same_signal_re_evaluated_is_recorded_once(tmp_path):
    """Eleven scan cycles over one signal is one observation, not eleven."""
    rt = _tracker(tmp_path)

    for _ in range(11):
        rt.record_candidate(_cand(), decision="REJECTED", reason="Late-trend entry: ADX 31")

    assert len(_rows(tmp_path)) == 1


def test_a_later_signal_on_the_same_instrument_is_its_own_observation(tmp_path):
    rt = _tracker(tmp_path)

    rt.record_candidate(_cand("2026-08-12T09:30:00"), decision="REJECTED", reason="Late-trend entry")
    rt.record_candidate(_cand("2026-08-12T10:45:00"), decision="REJECTED", reason="Late-trend entry")

    assert len(_rows(tmp_path)) == 2


def test_a_changed_outcome_still_records(tmp_path):
    """Rejected then accepted is the funnel working, not a duplicate."""
    rt = _tracker(tmp_path)

    rt.record_candidate(_cand(), decision="REJECTED", reason="Choppy market blocked")
    rt.record_candidate(_cand(), decision="STABILIZED", reason="Survived the window")
    rt.record_candidate(_cand(), decision="ACCEPTED", reason="admitted")

    assert len(_rows(tmp_path)) == 3


def test_a_different_reason_still_records(tmp_path):
    """A signal refused by a different gate is different information."""
    rt = _tracker(tmp_path)

    rt.record_candidate(_cand(), decision="REJECTED", reason="Choppy market blocked")
    rt.record_candidate(_cand(), decision="REJECTED", reason="C grade blocked")

    assert len(_rows(tmp_path)) == 2


@pytest.mark.parametrize("field,other", [
    ("instrument", "NIFTY"), ("direction", "LONG"),
])
def test_a_different_signal_is_not_collapsed(tmp_path, field, other):
    rt = _tracker(tmp_path)

    rt.record_candidate(_cand(), decision="REJECTED", reason="Choppy market blocked")
    rt.record_candidate(_cand(**{field: other}), decision="REJECTED", reason="Choppy market blocked")

    assert len(_rows(tmp_path)) == 2


def test_every_row_carries_a_stable_signal_id(tmp_path):
    """Historical files can be deduplicated without re-deriving the key."""
    import json

    rt = _tracker(tmp_path)
    rt.record_candidate(_cand(), decision="REJECTED", reason="a")
    rt.record_candidate(_cand(), decision="REJECTED", reason="b")

    rows = [json.loads(r) for r in _rows(tmp_path)]
    ids = {r["signal_id"] for r in rows}
    assert len(rows) == 2
    assert len(ids) == 1, "one signal, one signal_id, whatever the gate said"


def test_a_restart_does_not_reopen_the_day_for_rewriting(tmp_path):
    """The actual 2026-08-12 defect: six process starts, each with an empty
    in-memory set, so already-recorded signals were recorded again."""
    first = _tracker(tmp_path)
    for _ in range(5):
        first.record_candidate(_cand(), decision="REJECTED", reason="Choppy market blocked")
    assert len(_rows(tmp_path)) == 1

    restarted = _tracker(tmp_path)          # fresh instance == fresh process
    for _ in range(5):
        restarted.record_candidate(_cand(), decision="REJECTED", reason="Choppy market blocked")

    assert len(_rows(tmp_path)) == 1


def test_recovery_does_not_block_a_genuinely_new_signal(tmp_path):
    first = _tracker(tmp_path)
    first.record_candidate(_cand("2026-08-12T09:30:00"), decision="REJECTED", reason="x")

    restarted = _tracker(tmp_path)
    restarted.record_candidate(_cand("2026-08-12T10:45:00"), decision="REJECTED", reason="x")

    assert len(_rows(tmp_path)) == 2


def test_a_corrupt_day_file_never_blocks_recording(tmp_path):
    """Recovery is best-effort: a damaged archive must not silence the tracker."""
    (tmp_path / "events").mkdir(parents=True, exist_ok=True)
    (tmp_path / "events" / "2026-08-12.jsonl").write_text("{not json\n", encoding="utf-8")

    rt = _tracker(tmp_path)
    rt.record_candidate(_cand(), decision="REJECTED", reason="x")

    assert len(_rows(tmp_path)) == 2  # the junk line plus the new row


def test_a_signal_without_its_own_timestamp_is_never_silently_merged(tmp_path):
    """No signal clock means identity is unknowable; recording twice is the safe
    error, collapsing two real signals into one is not."""
    rt = _tracker(tmp_path)

    rt.record_candidate(_cand(signal_ts=""), decision="REJECTED", reason="x")
    rt.record_candidate(_cand(signal_ts=""), decision="REJECTED", reason="x")

    assert len(_rows(tmp_path)) == 2
