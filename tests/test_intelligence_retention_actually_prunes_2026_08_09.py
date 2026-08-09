"""A documented 65-day retention policy that could never delete anything.

`DataManager._prune_intelligence` filtered the store's KEYS against an ISO date:

    data[instrument] = {k: v for k, v in records.items() if k >= cutoff_iso}

with the comment "Keyed by timestamp string". It is not. IntelligenceMemory keys each
instrument by FIELD NAME -- timestamps / vol_ratio / oi_signal / ca_oi / pcr / regime
/ order_ratio / greeks / score -- index-aligned parallel lists. Every one of those
names starts with a lowercase letter (>= 0x63), which sorts above any "2026-..."
cutoff (0x32), so `k >= cutoff_iso` held for all nine. new_size always equalled
orig_size, the write guard never fired, and nothing was ever pruned or logged.

Not an over-delete -- no field name can sort BELOW an ISO cutoff, so it was a pure
silent no-op. The file sits at 6.7 MB only because of an unrelated 5000-point-per-list
trim in the writer.

Two changes:

  1. Prune by POSITION against the epoch seconds in "timestamps", slicing every
     aligned sibling list to the same index.
  2. Route it through the live IntelligenceMemory. That object holds the
     authoritative copy and rewrites the file wholesale on save(), so a file-level
     prune would have been silently undone by the next periodic save even once the
     filter was correct.
"""
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from data.data_manager import DataManager
from intelligence.memory import IntelligenceMemory

FIELDS = ["vol_ratio", "oi_signal", "ca_oi", "pcr", "regime", "order_ratio",
          "greeks", "score"]
INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def _memory(tmp_path, old=100, fresh=50, retention_days=65):
    mem = IntelligenceMemory(storage_path=str(tmp_path / "intelligence_history.json"))
    now = time.time()
    old_base = now - (retention_days + 30) * 86400
    for inst in INDICES:
        stamps = ([int(old_base + i * 60) for i in range(old)]
                  + [int(now - (fresh - i) * 60) for i in range(fresh)])
        mem.memory[inst] = {"timestamps": stamps}
        for f in FIELDS:
            mem.memory[inst][f] = [float(i) for i in range(len(stamps))]
    return mem, now


# ── the old filter was a guaranteed no-op ────────────────────────────────────

def test_a_field_name_always_sorts_above_an_iso_cutoff():
    """The premise of the bug, pinned so nobody 'restores' the old filter."""
    cutoff_iso = (datetime.now() - timedelta(days=65)).isoformat()
    for name in ["timestamps"] + FIELDS:
        assert name >= cutoff_iso, f"{name} would have been filtered — premise broken"


def test_the_writer_still_keys_by_field_name():
    """If the schema ever becomes timestamp-keyed, the positional prune is wrong."""
    mem = IntelligenceMemory(storage_path="x")
    mem.memory.pop("NIFTY", None)
    mem.record("NIFTY", time.time(), {"aggregate": {"score": 1.0}})
    assert "timestamps" in mem.memory["NIFTY"]
    assert all(isinstance(v, list) for v in mem.memory["NIFTY"].values())


# ── it now actually prunes ───────────────────────────────────────────────────

def test_old_points_are_dropped(tmp_path):
    mem, now = _memory(tmp_path)
    removed = mem.prune_older_than(now - 65 * 86400)
    assert set(removed) == set(INDICES)
    for inst in INDICES:
        assert removed[inst] == 100
        assert len(mem.memory[inst]["timestamps"]) == 50


@pytest.mark.parametrize("inst", INDICES)
def test_every_parallel_list_is_cut_to_the_same_length(tmp_path, inst):
    """Shearing one list against another would corrupt every historical row."""
    mem, now = _memory(tmp_path)
    mem.prune_older_than(now - 65 * 86400)
    lengths = {k: len(v) for k, v in mem.memory[inst].items()}
    assert len(set(lengths.values())) == 1, lengths


def test_fresh_points_survive(tmp_path):
    mem, now = _memory(tmp_path)
    mem.prune_older_than(now - 65 * 86400)
    for inst in INDICES:
        assert all(ts >= now - 65 * 86400 for ts in mem.memory[inst]["timestamps"])


def test_nothing_is_dropped_when_everything_is_fresh(tmp_path):
    mem, now = _memory(tmp_path, old=0, fresh=40)
    assert mem.prune_older_than(now - 65 * 86400) == {}
    assert len(mem.memory["NIFTY"]["timestamps"]) == 40


def test_the_prune_is_persisted(tmp_path):
    mem, now = _memory(tmp_path)
    mem.prune_older_than(now - 65 * 86400)
    on_disk = json.loads((tmp_path / "intelligence_history.json").read_text())
    assert len(on_disk["NIFTY"]["timestamps"]) == 50


def test_a_ragged_list_is_left_alone(tmp_path):
    """A list out of step with timestamps must not be sliced at the same index."""
    mem, now = _memory(tmp_path)
    mem.memory["NIFTY"]["pcr"] = [1.0, 2.0]
    mem.prune_older_than(now - 65 * 86400)
    assert mem.memory["NIFTY"]["pcr"] == [1.0, 2.0]
    assert len(mem.memory["NIFTY"]["timestamps"]) == 50


@pytest.mark.parametrize("cutoff", [0, -1, None])
def test_a_missing_cutoff_prunes_nothing(tmp_path, cutoff):
    mem, _ = _memory(tmp_path)
    assert mem.prune_older_than(cutoff) == {}
    assert len(mem.memory["NIFTY"]["timestamps"]) == 150


def test_junk_rows_do_not_raise(tmp_path):
    mem, now = _memory(tmp_path)
    mem.memory["JUNK"] = {"timestamps": "not a list"}
    mem.memory["EMPTY"] = {}
    mem.memory["NONE"] = None
    mem.prune_older_than(now - 65 * 86400)
    assert len(mem.memory["NIFTY"]["timestamps"]) == 50


# ── DataManager routes through the owner, not the file ───────────────────────

def test_the_manager_prunes_through_the_live_memory(tmp_path):
    mem, now = _memory(tmp_path)
    dm = DataManager(SimpleNamespace(), intel_memory=mem)
    dm.intel_file = tmp_path / "intelligence_history.json"
    dm._prune_intelligence()
    assert len(mem.memory["NIFTY"]["timestamps"]) == 50, (
        "the in-memory copy still holds the old points, so the next save undoes this"
    )


def test_the_manager_does_not_write_the_file_behind_the_writers_back(tmp_path):
    """The prune must reach the object that owns the file, or it is lost."""
    mem, now = _memory(tmp_path)
    calls = []
    mem.save = lambda: calls.append(1)
    dm = DataManager(SimpleNamespace(), intel_memory=mem)
    dm.intel_file = tmp_path / "intelligence_history.json"
    dm._prune_intelligence()
    assert calls, "pruned in memory but never persisted"


def test_the_file_fallback_still_works_offline(tmp_path):
    """Maintenance with no live scanner still has to prune something."""
    now = time.time()
    payload = {"NIFTY": {
        "timestamps": [int(now - 200 * 86400), int(now - 190 * 86400), int(now - 60)],
        "pcr": [1.0, 2.0, 3.0], "score": [0.1, 0.2, 0.3]}}
    path = tmp_path / "intelligence_history.json"
    path.write_text(json.dumps(payload))
    dm = DataManager(SimpleNamespace(), intel_memory=None)
    dm.intel_file = path
    dm._prune_intelligence()
    out = json.loads(path.read_text())
    assert out["NIFTY"]["timestamps"] == [payload["NIFTY"]["timestamps"][-1]]
    assert out["NIFTY"]["pcr"] == [3.0]
    assert out["NIFTY"]["score"] == [0.3]


def test_the_scanner_hands_the_manager_its_memory():
    """The wiring. Without it the manager silently takes the file fallback."""
    import ast
    import inspect

    import scanner as scanner_mod

    src = inspect.getsource(scanner_mod)
    assert "DataManager(self.data, intel_memory=self.intel_memory)" in src


def test_a_broken_memory_object_falls_back_rather_than_raising(tmp_path):
    class Boom:
        def prune_older_than(self, _):
            raise RuntimeError("nope")

    dm = DataManager(SimpleNamespace(), intel_memory=Boom())
    dm.intel_file = tmp_path / "missing.json"
    dm._prune_intelligence()  # must not raise
