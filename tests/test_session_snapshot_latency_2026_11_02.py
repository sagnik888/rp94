"""Ledger snapshots stay isolated without deepcopy dispatch per path scalar."""

import asyncio
import copy
import threading
from collections import defaultdict
from datetime import datetime

import pytest

from scanner import Scanner
from tests.test_scanner_modules import _session_persist_scanner


def test_snapshot_preserves_every_path_sample_and_nested_mutable_isolation():
    paths = {
        "pnl_path": [[float(i), float(i - 500)] for i in range(1000)],
        "intel_path": [[float(i), {"score": i, "votes": [i, i + 1]}]
                       for i in range(150)],
    }
    rows = [{"id": "entry", **paths, "nested": {"checks": [{"allowed": True}]}}]
    expected = copy.deepcopy(rows)
    frozen = Scanner._snapshot_session_signal_rows(rows)

    rows[0]["pnl_path"][0][1] = 12345.0
    rows[0]["intel_path"][0][1]["votes"].append(999)
    rows[0]["nested"]["checks"][0]["allowed"] = False
    assert frozen == expected
    frozen[0]["intel_path"][-1][1]["votes"].clear()
    assert rows[0]["intel_path"][-1][1]["votes"] == [149, 150]


def test_snapshot_preserves_shared_references_and_container_cycles():
    shared = {"stop": [12.0]}
    rows = [{"first": shared, "second": shared}]
    rows[0]["self"] = rows
    shared["parent"] = rows[0]
    frozen = Scanner._snapshot_session_signal_rows(rows)

    assert frozen is not rows
    assert frozen[0]["first"] is frozen[0]["second"]
    assert frozen[0]["first"] is not shared
    assert frozen[0]["self"] is frozen
    assert frozen[0]["first"]["parent"] is frozen[0]


def test_snapshot_retains_exact_non_json_types_and_fallback_aliases():
    class TaggedList(list):
        pass

    shared = TaggedList([{"event": datetime(2026, 11, 2, 14, 44)}])
    defaults = defaultdict(list, events=shared)
    rows = [{("source", 1): defaults, "same_events": shared,
             "tuple": (shared,), "set": {"ENTRY", "EXIT"}}]
    frozen = Scanner._snapshot_session_signal_rows(rows)

    assert frozen == rows
    assert type(frozen[0][("source", 1)]) is defaultdict
    assert frozen[0][("source", 1)].default_factory is list
    assert type(frozen[0]["same_events"]) is TaggedList
    assert type(frozen[0]["tuple"]) is tuple
    assert type(frozen[0]["set"]) is set
    assert type(frozen[0]["same_events"][0]["event"]) is datetime
    assert frozen[0]["same_events"] is frozen[0][("source", 1)]["events"]
    assert frozen[0]["same_events"] is frozen[0]["tuple"][0]
    assert frozen[0]["same_events"] is not shared


def test_queued_snapshot_is_detached_before_candidate_mutation():
    payload = {"id": "entry", "action": "ENTRY", "nested": {"stop": [12.0]}}
    scanner = _session_persist_scanner(payload)
    writes = []
    scanner._save_session_signal_rows = lambda day, rows: writes.append(rows)

    async def exercise():
        scanner._persist_session_trade_candidates()
        payload["nested"]["stop"][0] = 15.0
        await scanner._session_persist_task
        assert writes[0][0]["nested"]["stop"] == [12.0]
        await Scanner.flush_session_trade_candidates(scanner)

    asyncio.run(exercise())
    assert writes[-1][0]["nested"]["stop"] == [15.0]


def four_index_scanner():
    scanner = _session_persist_scanner({})
    scanner._session_trade_candidates = {
        name: {"entry": {"id": name, "action": "ENTRY", "source_engine": "ut_ema"}}
        for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
    }
    scanner.config = {"indices": {name: {} for name in scanner._session_trade_candidates}}
    scanner._loaded_session_is_current_day = lambda: True
    scanner._refresh_trade_candidates = lambda *args, **kwargs: None
    scanner._get_session_trade_candidates = lambda name: list(scanner._session_trade_candidates[name].values())
    scanner._session_entries_for_protection = scanner._get_session_trade_candidates
    scanner._prune_session_trade_candidates_for_live_gate = lambda: None
    scanner._record_session_protection_fault = lambda *args: None
    return scanner


@pytest.mark.parametrize("caller", ["manual", "scan"])
def test_four_changed_indices_freeze_one_complete_native_exit_snapshot(caller):
    scanner = four_index_scanner()
    serializations, writes = [], []

    def serialize(candidate):
        serializations.append(candidate["id"])
        return dict(candidate)

    def check(name):
        book = scanner._session_trade_candidates[name]
        book["exit"] = {"id": name + "|EXIT", "action": "EXIT", "source_engine": "ut_ema"}
        scanner._persist_session_trade_candidates()
        assert writes == []

    scanner._serialize_trade_candidate = serialize
    scanner._check_session_candidates_breaches_locked = check
    scanner._save_session_signal_rows = lambda day, rows: writes.append(rows)
    if caller == "manual":
        scanner._poll_manual_signal_protection()
        assert len(serializations) == 8
    else:
        results = {"instruments": {name: {} for name in scanner._session_trade_candidates}}
        scanner._refresh_session_signal_payloads(results)
        # Eight display serializations plus one complete eight-row ledger freeze.
        assert len(serializations) == 16
        assert all(len(payload["trade_candidates"]) == 2 for payload in results["instruments"].values())

    assert len(writes) == 1
    assert len(writes[0]) == 8
    assert sum(row["action"] == "EXIT" for row in writes[0]) == 4
    assert scanner._session_snapshot_revision == 1
    assert scanner._session_persist_batch_depth == 0
    assert scanner._session_persist_batch_dirty is False


def test_nested_batch_flushes_before_unlock_even_when_scan_raises():
    scanner = four_index_scanner()
    writes = []

    def check(name):
        with scanner._batch_session_signal_persistence():
            scanner._session_trade_candidates[name]["entry"]["action"] = "EXIT"
            scanner._persist_session_trade_candidates()
        assert writes == []
        if name == "BANKNIFTY":
            raise RuntimeError("broken display row")

    def save(day, rows):
        assert scanner._session_breach_lock._is_owned()
        writes.append(rows)

    scanner._check_session_candidates_breaches_locked = check
    scanner._save_session_signal_rows = save
    results = {"instruments": {name: {} for name in scanner._session_trade_candidates}}
    with pytest.raises(RuntimeError, match="broken display row"):
        scanner._refresh_session_signal_payloads(results)

    assert len(writes) == 1
    assert sum(row["action"] == "EXIT" for row in writes[0]) == 2
    assert scanner._session_persist_batch_depth == 0
    scanner._persist_session_trade_candidates()
    assert len(writes) == 2


def test_batch_does_not_defer_another_threads_standalone_persistence():
    scanner = four_index_scanner()
    entered, finished = threading.Event(), threading.Event()
    writes = []
    scanner._save_session_signal_rows = lambda day, rows: writes.append(rows)

    def persist():
        entered.set()
        scanner._persist_session_trade_candidates()
        finished.set()

    with scanner._batch_session_signal_persistence():
        worker = threading.Thread(target=persist)
        worker.start()
        assert entered.wait(1.0)
        assert not finished.is_set()
        scanner._session_trade_candidates["NIFTY"]["entry"]["action"] = "EXIT"
    worker.join(2.0)
    assert finished.is_set()
    assert len(writes) == 1
    assert next(row for row in writes[0] if row["id"] == "NIFTY")["action"] == "EXIT"
