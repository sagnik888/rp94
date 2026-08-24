"""Archive the premium path, not just the promise of one.

start_session_archiver_scheduler exists because "the live candle cache only keeps the
last-fetched contracts, so without this snapshot a traded strike's intraday bars are
lost/overwritten". It snapshots the cache -- so it can only ever preserve what the cache
already holds.

For OPTION contracts the cache held nothing. As of 2026-08-14 there was no CE/PE file
newer than 2026-07-31, and today's log shows 349 candle-cache writes, all of them the 12
index series and not one contract. So every session after July archived index candles
and no premium, and became permanently unreplayable: the exit backtester matched 8/12
trades on 2026-07-24 and 0/17 on 2026-08-12.

The archiver cannot fix this itself -- it runs as a child process with no broker
session. The back-fill therefore happens in the parent, where auth lives, before the
child is spawned.
"""
import inspect

import pytest

import scripts.archive_session_data as arch


def _events(*symbols):
    return [{"contract": {"trading_symbol": s}} for s in symbols]


# ── what is missing ──────────────────────────────────────────────────────────

def test_a_traded_contract_with_no_cached_bars_is_reported_missing(monkeypatch):
    monkeypatch.setattr(arch, "day_slice", lambda *a, **k: None)

    missing = arch.missing_contract_symbols("2026-08-12", _events("NIFTY2681424300CE"))

    assert missing == ["NIFTY2681424300CE"]


def test_a_contract_already_cached_is_not_refetched(monkeypatch):
    monkeypatch.setattr(arch, "day_slice", lambda *a, **k: "some-frame")

    assert arch.missing_contract_symbols("2026-08-12", _events("NIFTY2681424300CE")) == []


def test_index_series_are_not_treated_as_contracts(monkeypatch):
    """Index candles are archived by a different path and are never missing premium."""
    monkeypatch.setattr(arch, "day_slice", lambda *a, **k: None)

    assert arch.missing_contract_symbols("2026-08-12", _events("NIFTY", "BANKNIFTY")) == []


def test_both_option_sides_are_covered(monkeypatch):
    monkeypatch.setattr(arch, "day_slice", lambda *a, **k: None)

    missing = arch.missing_contract_symbols(
        "2026-08-12", _events("NIFTY2681424300CE", "NIFTY2681424300PE")
    )

    assert len(missing) == 2


# ── the back-fill ────────────────────────────────────────────────────────────

def test_each_missing_contract_is_fetched(monkeypatch):
    monkeypatch.setattr(arch, "day_slice", lambda *a, **k: None)
    asked = []

    filled = arch.backfill_contract_history(
        "2026-08-12", lambda s: asked.append(s) or True,
        events=_events("NIFTY2681424300CE", "SENSEX2681480000CE"), quiet=True,
    )

    assert sorted(asked) == ["NIFTY2681424300CE", "SENSEX2681480000CE"]
    assert len(filled) == 2


def test_a_fetch_that_finds_nothing_is_not_counted(monkeypatch):
    monkeypatch.setattr(arch, "day_slice", lambda *a, **k: None)

    filled = arch.backfill_contract_history(
        "2026-08-12", lambda _s: False,
        events=_events("NIFTY2681424300CE"), quiet=True,
    )

    assert filled == []


def test_one_failing_fetch_does_not_abandon_the_rest(monkeypatch):
    """A session that cannot be fully back-filled must still archive what it has."""
    monkeypatch.setattr(arch, "day_slice", lambda *a, **k: None)

    def _fetch(symbol):
        if symbol.startswith("NIFTY"):
            raise RuntimeError("token lookup failed")
        return True

    filled = arch.backfill_contract_history(
        "2026-08-12", _fetch,
        events=_events("NIFTY2681424300CE", "SENSEX2681480000CE"), quiet=True,
    )

    assert filled == ["SENSEX2681480000CE"]


def test_nothing_missing_means_no_fetching(monkeypatch):
    monkeypatch.setattr(arch, "day_slice", lambda *a, **k: "frame")
    asked = []

    arch.backfill_contract_history(
        "2026-08-12", lambda s: asked.append(s) or True,
        events=_events("NIFTY2681424300CE"), quiet=True,
    )

    assert asked == []


# ── wired where the broker session actually is ───────────────────────────────

def test_the_backfill_runs_in_the_parent_before_the_child():
    import main

    src = inspect.getsource(main.start_session_archiver_scheduler)

    assert "backfill_contract_history" in src, "the archive still snapshots an empty cache"
    assert src.index("backfill_contract_history") < src.index("subprocess.run"), (
        "back-filling after the child has archived preserves nothing"
    )


def test_the_scheduler_receives_the_data_provider():
    import main

    assert "data_provider" in inspect.signature(main.start_session_archiver_scheduler).parameters
    src = inspect.getsource(main)
    assert "start_session_archiver_scheduler(data_provider=" in src, (
        "the scheduler is started without the authenticated provider, so it cannot fetch"
    )


def test_a_backfill_failure_never_blocks_the_archive():
    import main

    src = inspect.getsource(main.start_session_archiver_scheduler)
    i = src.index("from scripts.archive_session_data import")
    j = src.index("subprocess.run")

    assert "except Exception" in src[i:j], (
        "a back-fill error would propagate and stop the session archiving at all"
    )


def test_it_fetches_one_minute_bars():
    """The exit backtester replays 1-minute premium; anything else is unusable to it."""
    import main

    src = inspect.getsource(main._fetch_contract_history_for_session)

    assert '"1min"' in src
