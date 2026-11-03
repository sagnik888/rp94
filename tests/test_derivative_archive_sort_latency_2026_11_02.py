"""Preserve archive ordering without per-event Python sort callbacks."""
import builtins
import random

from data.derivative_participation import DerivativeParticipation


def indexed_store(tmp_path, count=20000):
    store = DerivativeParticipation(tmp_path)
    start = 1793687100.
    day = store._session(start)
    seqs = list(range(count))
    random.Random(2).shuffle(seqs)
    events = [dict(at=start+i*.01, sequence=sequence) for i, sequence in enumerate(seqs)]
    store._archive_obs[day] = {"NIFTY": dict(ats=[event["at"] for event in events],
        seqs=seqs, events=events, next_seq=count)}
    store._archive_floor[day] = None
    return store, start, events


def test_archive_window_matches_original_order_without_python_sort_keys(tmp_path, monkeypatch):
    import data.derivative_participation as module
    store, start, events = indexed_store(tmp_path)
    expected = builtins.sorted(events[100:15001], key=lambda event: event["sequence"])
    calls = []
    def observed_sorted(values, *, key=None, **kwargs):
        def counted(value):
            calls.append(1)
            return key(value)
        return builtins.sorted(values, key=counted if key else None, **kwargs)
    monkeypatch.setattr(module, "sorted", observed_sorted, raising=False)
    actual = store._archived_observations("NIFTY", start+150., start+1.)
    assert actual == expected
    assert not calls, "Archive window executes one Python callback per retained event"


def test_archive_order_handles_late_insertions_and_rewinds(tmp_path):
    store = DerivativeParticipation(tmp_path)
    start = 1793687100.
    day = store._session(start)
    store._archive_floor[day] = None
    events = []
    for offsets in ((20, 10, 30), (15, 5, 30)):
        batch = [dict(kind="observation", at=start+offset, key=["NIFTY"], row=[start+offset],
                      metadata={"batch": len(events)}) for offset in offsets]
        events.extend(batch)
        store._index_observations(day, batch)
        for lower, upper in ((0, 40), (10, 20), (30, 30), (40, 50), (0, 10)):
            expected = [event for event in events if start+lower <= event["at"] <= start+upper]
            assert store._archived_observations("NIFTY", start+upper, start+lower) == expected
