"""An unreadable GTT list was treated as "no trigger exists".

`find_gtt_stop` is deliberately tri-state and its own comment says why:

    # Never NONE from a failed scan — that would authorise a duplicate trigger.
    return {"state": "UNKNOWN", ...}

But `_arm_gtt_fallback` only handled FOUND. UNKNOWN — and a raising finder, which was
swallowed to `prior = {}` and so became indistinguishable from NONE — fell straight
through to `placer(...)`. Two GTTs on one contract exit the position twice, and the
second fill opens a naked reversed leg.

The sibling SL-M path already refuses when its prior-stop scan is AMBIGUOUS. This is
the same never-conclude-a-state-from-a-failed-read rule, unenforced on the GTT side.

BUT refusing outright is NOT the safe answer, and that is the interesting part. This
function runs only because the protective SL-M was already refused, so declining to
arm leaves the position with no server-side stop at all — a certain exposure traded
for a possible duplicate. So:

  * UNKNOWN gets ONE retry, which resolves a transient scan failure;
  * if it is still UNKNOWN, arm anyway and set gtt_arm_uncertain, so the possible
    duplicate is reconciled by a human instead of sitting silent until both fire;
  * an AMBIGUOUS placement — the exact chain that CREATES the orphan the scan cannot
    see — now marks recovery instead of returning a bare False.

The flag is persisted. Losing it on restart is how the orphan goes unnoticed.
"""
import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

from trading.trade_manager import Trade, TradeManager
from trading.order_retry import BrokerOrderAmbiguous


def _tm(finder, placer, product="NRML"):
    tm = TradeManager.__new__(TradeManager)
    tm.broker = SimpleNamespace(place_gtt_stop=placer, find_gtt_stop=finder)
    tm.product_type = product
    tm.trades = []
    tm._last_recovery_alert = {}
    tm._alert_recovery = lambda t: None
    tm._trade_owning_gtt = lambda tid, exclude_id="": None
    return tm


def _trade():
    return Trade(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=120.0, entry_time=datetime(2026, 8, 7, 10, 0),
        trailing_stop=100.0, current_stop=100.0, lots=1, lot_size=65, grade="A",
        trading_symbol="NIFTY26AUG24700CE", inst_type="OPT",
    )


# ── UNKNOWN is retried, not read as absent ───────────────────────────────────

def test_a_transient_scan_failure_is_retried():
    seen = []
    states = iter([{"state": "UNKNOWN"}, {"state": "NONE"}])

    def finder(sym):
        seen.append(sym)
        return next(states)

    tm = _tm(finder, lambda *a, **k: "GTT9")
    t = _trade()
    assert tm._arm_gtt_fallback(t, 65, "SELL", 100.0) is True
    assert len(seen) == 2, "an UNKNOWN scan is still taken at face value"
    assert t.gtt_arm_uncertain is False, "a resolved retry must not flag uncertainty"


def test_a_retry_that_finds_a_trigger_adopts_it_instead_of_stacking():
    states = iter([{"state": "UNKNOWN"},
                   {"state": "FOUND", "trigger_id": "G7", "trigger_values": [100.0]}])
    tm = _tm(lambda s: next(states), lambda *a, **k: pytest.fail("armed a SECOND GTT"))
    tm._gtt_matches_trade = staticmethod(lambda row, trig: True)
    t = _trade()
    assert tm._arm_gtt_fallback(t, 65, "SELL", 100.0) is True
    assert t.gtt_trigger_id == "G7"


def test_a_raising_finder_is_unknown_not_absent():
    """It was swallowed to {}, which read exactly like NONE."""
    calls = {"n": 0}

    def finder(sym):
        calls["n"] += 1
        raise RuntimeError("kite down")

    t = _trade()
    tm = _tm(finder, lambda *a, **k: "GTT9")
    assert tm._arm_gtt_fallback(t, 65, "SELL", 100.0) is True
    assert calls["n"] == 2, "a raising finder is still collapsed to 'nothing there'"
    assert t.gtt_arm_uncertain is True


# ── a persistently blind scan arms, but says so ──────────────────────────────

def test_a_persistently_unknown_scan_still_arms():
    """Refusing would leave the position with NO stop — the SL-M was already refused."""
    tm = _tm(lambda s: {"state": "UNKNOWN"}, lambda *a, **k: "GTT9")
    t = _trade()
    assert tm._arm_gtt_fallback(t, 65, "SELL", 100.0) is True
    assert t.gtt_trigger_id == "GTT9"


def test_a_persistently_unknown_scan_flags_the_trade():
    tm = _tm(lambda s: {"state": "UNKNOWN"}, lambda *a, **k: "GTT9")
    t = _trade()
    tm._arm_gtt_fallback(t, 65, "SELL", 100.0)
    assert t.gtt_arm_uncertain is True, (
        "a possible duplicate trigger is armed with nothing recording the doubt"
    )


def test_a_clean_scan_leaves_the_trade_unflagged():
    tm = _tm(lambda s: {"state": "NONE"}, lambda *a, **k: "GTT9")
    t = _trade()
    assert tm._arm_gtt_fallback(t, 65, "SELL", 100.0) is True
    assert t.gtt_arm_uncertain is False


# ── an ambiguous placement is the orphan-maker ───────────────────────────────

def test_an_ambiguous_arm_marks_recovery():
    def placer(*a, **k):
        raise BrokerOrderAmbiguous("timeout after send")

    tm = _tm(lambda s: {"state": "NONE"}, placer)
    t = _trade()
    assert tm._arm_gtt_fallback(t, 65, "SELL", 100.0) is False
    assert t.gtt_arm_uncertain is True
    assert t.execution_status == "RECOVERY_REQUIRED"


def test_a_plain_rejection_does_not_mark_recovery():
    """A refused GTT left nothing behind; only AMBIGUOUS can orphan a trigger."""
    def placer(*a, **k):
        raise ValueError("bad trigger price")

    tm = _tm(lambda s: {"state": "NONE"}, placer)
    t = _trade()
    assert tm._arm_gtt_fallback(t, 65, "SELL", 100.0) is False
    assert t.gtt_arm_uncertain is False
    assert t.execution_status != "RECOVERY_REQUIRED"


# ── unchanged behaviour ──────────────────────────────────────────────────────

def test_an_already_armed_trade_is_untouched():
    tm = _tm(lambda s: pytest.fail("scanned despite a local id"),
             lambda *a, **k: pytest.fail("armed despite a local id"))
    t = _trade()
    t.gtt_trigger_id = "EXISTING"
    assert tm._arm_gtt_fallback(t, 65, "SELL", 100.0) is True


def test_a_trigger_owned_by_another_trade_is_still_refused():
    tm = _tm(lambda s: {"state": "FOUND", "trigger_id": "G7"},
             lambda *a, **k: pytest.fail("stacked onto another trade's trigger"))
    tm._trade_owning_gtt = lambda tid, exclude_id="": SimpleNamespace(id="OTHER")
    assert tm._arm_gtt_fallback(_trade(), 65, "SELL", 100.0) is False


def test_a_mismatched_trigger_level_is_still_refused():
    tm = _tm(lambda s: {"state": "FOUND", "trigger_id": "G7", "trigger_values": [999.0]},
             lambda *a, **k: pytest.fail("adopted a trigger at the wrong level"))
    assert tm._arm_gtt_fallback(_trade(), 65, "SELL", 100.0) is False


def test_no_finder_at_all_still_arms():
    tm = _tm(None, lambda *a, **k: "GTT9")
    tm.broker = SimpleNamespace(place_gtt_stop=lambda *a, **k: "GTT9")
    assert tm._arm_gtt_fallback(_trade(), 65, "SELL", 100.0) is True


def test_the_finder_is_still_consulted_before_arming():
    src = inspect.getsource(TradeManager._arm_gtt_fallback)
    assert "find_gtt_stop" in src and "UNKNOWN" in src


# ── the flag survives a restart ──────────────────────────────────────────────

def test_the_flag_round_trips_through_to_dict():
    t = _trade()
    t.gtt_arm_uncertain = True
    assert Trade.from_dict(t.to_dict()).gtt_arm_uncertain is True


def test_the_flag_is_persisted_to_sqlite():
    from data.sqlite_db import TRADE_PERSISTED_COLUMNS

    assert "gtt_arm_uncertain" in TRADE_PERSISTED_COLUMNS, (
        "a possible orphaned trigger is forgotten on restart"
    )


def test_the_column_exists_in_the_table():
    import inspect as _i

    import data.sqlite_db as db

    src = _i.getsource(db)
    assert '"gtt_arm_uncertain": "INTEGER"' in src, (
        "persisted column list names a column the table does not have"
    )


def test_the_default_is_false():
    assert _trade().gtt_arm_uncertain is False
    assert Trade.from_dict({"id": "X", "instrument": "NIFTY"}).gtt_arm_uncertain is False
