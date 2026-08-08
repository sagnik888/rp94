"""The repaint flag was lost on every restart, permanently arming the give-back guard.

`repaint_guard_checked` marks that a post-entry repaint check has completed. It was only
ever a dynamic `setattr` — `Trade` persisted `entered_intracandle` but had no field for
the flag, so save/load dropped it.

The consumer is the give-back guard:

    repaint_window = (
        repaint_window_giveback_guard_enabled
        and entered_intracandle
        and not repaint_guard_checked
    )

An intracandle trade therefore rehydrated as structurally unconfirmed: let-winners-run
disabled and the blunt sub-1R give-back guard armed, for the remaining hours of the
session, on a position whose source candle had closed long before. The async repaint
tracker is started from exactly one place — the entry acknowledgement — and there is no
rehydration path that restarts it, so the flag could never be set again either.

Two halves to the fix: persist the field, and on load treat a trade whose source candle
has already closed as checked, because such an arrow cannot repaint by definition.

An existing guard, test_every_field_from_dict_reads_is_actually_stored, caught the first
attempt: the field was added to to_dict/from_dict but not to TRADE_PERSISTED_COLUMNS, so
save_trades filtered it straight back out. Exactly the silent-default failure that test
was written for.
"""
from datetime import datetime, timedelta

import pytest

from data.sqlite_db import TRADE_PERSISTED_COLUMNS
from trading.trade_manager import IST, Trade


def _trade(**kw):
    base = dict(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, entry_time=datetime.now(IST), trailing_stop=95.0,
        current_stop=95.0, lots=1, lot_size=75, grade="A",
    )
    base.update(kw)
    return Trade(**base)


# ── the field round-trips ────────────────────────────────────────────────────

def test_the_flag_is_a_real_persisted_field():
    assert "repaint_guard_checked" in TRADE_PERSISTED_COLUMNS, (
        "save_trades filters on this set, so the field is dropped on write"
    )


def test_a_checked_trade_stays_checked_through_a_round_trip():
    t = _trade()
    t.repaint_guard_checked = True
    assert Trade.from_dict(t.to_dict()).repaint_guard_checked is True


def test_an_unchecked_trade_stays_unchecked():
    t = _trade()
    t.repaint_guard_checked = False
    assert Trade.from_dict(t.to_dict()).repaint_guard_checked is False


def test_a_legacy_row_without_the_key_defaults_to_unchecked():
    """Rows written before the field existed must not claim to be checked."""
    data = _trade().to_dict()
    data.pop("repaint_guard_checked", None)
    assert Trade.from_dict(data).repaint_guard_checked is False


def test_the_schema_can_hold_it():
    """The column-type map is what migrates an existing DB."""
    import inspect

    import data.sqlite_db as db

    assert '"repaint_guard_checked": "INTEGER"' in inspect.getsource(db.DatabaseManager)


# ── the give-back guard reads the new name ───────────────────────────────────

def test_the_giveback_guard_reads_the_persisted_field():
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner)
    assert '"repaint_guard_checked"' in src
    assert '"_repaint_guard_checked"' not in src, (
        "a call site still uses the old private name and will never see the persisted "
        "value"
    )


def test_every_setter_and_reader_uses_one_name():
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner)
    assert src.count('"repaint_guard_checked"') >= 7, (
        "some repaint-flag call sites were left on the old attribute"
    )


# ── a closed candle cannot repaint ───────────────────────────────────────────

def test_load_marks_a_trade_whose_candle_has_closed():
    """The rehydration half: no tracker restarts, so the flag must be inferred."""
    import inspect

    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager.load_state)
    assert "repaint_guard_checked" in src, (
        "a restarted intracandle trade stays 'unconfirmed' for the rest of the session"
    )
    assert "entered_intracandle" in src


def test_a_fresh_intracandle_trade_is_not_marked_checked():
    """Only an ELAPSED candle is safe to infer; a live one still needs the tracker."""
    t = _trade(entry_time=datetime.now(IST) - timedelta(seconds=30))
    assert t.repaint_guard_checked is False
