"""Replay certification: a whole session of legs, with the invariant checked after each.

Step 5 asks for "replay and paper certification" before same-contract stacking returns.
Paper certification needs a funded account and real fills. Replay does not — and it is
the half that can be done now.

The invariant, checked after EVERY leg rather than at the end:

    for each contract:  sum(ledger holdings) == broker net position

Checked continuously because a ledger that is right at the start and right at the end
can still be wrong in the middle, and the middle is where a protective stop gets sized.

The session below is deliberately the hard one — two trades on ONE contract, moving
through every leg that changes a holding, including the two that shipped wrong:

    entry, entry           two trades on one strike
    scale-in               the leg that recorded local intent
    partial exit           the leg that recorded an aggregate difference
    protective stop fires  the leg that recorded nothing at all
    full exit              the survivor closes out

Plus a restart in the middle, because attribution that does not survive a restart is
not attribution.
"""
import os
import tempfile
import uuid

import pytest


SYM = "NIFTY2681424300CE"


@pytest.fixture
def led():
    from data.sqlite_db import DatabaseManager

    path = os.path.join(tempfile.gettempdir(), f"replay_{uuid.uuid4().hex}.db")
    handle = object.__new__(DatabaseManager)
    handle._init_db(path)
    yield handle
    try:
        handle.conn.close()
    except Exception:
        pass


def _holdings(led, symbol=SYM):
    return led.owned_quantities_for_symbol(symbol)


def _assert_matches(led, broker_net, note):
    """The certification invariant, stated once and reused after every leg."""
    total = sum(_holdings(led).values())
    assert total == broker_net, (
        f"after {note}: ledger attributes {total}, broker holds {broker_net}"
    )


# ── the full session ─────────────────────────────────────────────────────────

def test_a_whole_session_on_one_contract_keeps_the_invariant(led):
    broker = 0

    # T1 enters, 1 lot.
    led.record_order_fill("o-t1-entry", "T1", 75, tradingsymbol=SYM, leg="ENTRY")
    broker += 75
    _assert_matches(led, broker, "T1 entry")

    # T2 enters on the SAME strike from another timeframe. The position row is now 150
    # and cannot say whose is whose; the ledger can.
    led.record_order_fill("o-t2-entry", "T2", 75, tradingsymbol=SYM, leg="ENTRY")
    broker += 75
    _assert_matches(led, broker, "T2 entry onto the same contract")
    assert _holdings(led) == {"T1": 75, "T2": 75}

    # T1 scales in. Previously recorded from local intent.
    led.record_order_fill("o-t1-add", "T1", 75, tradingsymbol=SYM, leg="ADD")
    broker += 75
    _assert_matches(led, broker, "T1 scale-in")
    assert _holdings(led)["T1"] == 150

    # T1 partially reduces. Previously recorded as broker_quantity - aggregate, which
    # computed as zero here because the aggregate also holds T2's lot.
    led.record_order_fill("o-t1-partial", "T1", -75, tradingsymbol=SYM, leg="PARTIAL_EXIT")
    broker -= 75
    _assert_matches(led, broker, "T1 partial exit")
    assert _holdings(led) == {"T1": 75, "T2": 75}

    # T2's protective stop fires. Previously recorded nowhere at all.
    led.record_order_fill("o-t2-stop", "T2", -75, tradingsymbol=SYM, leg="PROTECTIVE")
    broker -= 75
    _assert_matches(led, broker, "T2 stop firing")
    assert _holdings(led) == {"T1": 75, "T2": 0}, "the stop closed the wrong trade"

    # T1 closes out.
    led.record_order_fill("o-t1-exit", "T1", -75, tradingsymbol=SYM, leg="FULL_EXIT")
    broker -= 75
    _assert_matches(led, broker, "T1 full exit")
    assert sum(_holdings(led).values()) == 0
    assert broker == 0


def test_the_invariant_survives_a_restart_mid_session(led):
    """Attribution that does not survive a restart is not attribution."""
    from data.sqlite_db import DatabaseManager

    led.record_order_fill("o-t1-entry", "T1", 75, tradingsymbol=SYM, leg="ENTRY")
    led.record_order_fill("o-t2-entry", "T2", 75, tradingsymbol=SYM, leg="ENTRY")
    path = str(led.db_path)
    led.conn.close()

    reopened = object.__new__(DatabaseManager)
    reopened._init_db(path)
    try:
        _assert_matches(reopened, 150, "restart")

        # and the session continues correctly on the far side of it
        reopened.record_order_fill("o-t2-stop", "T2", -75, tradingsymbol=SYM, leg="PROTECTIVE")
        _assert_matches(reopened, 75, "a stop firing after restart")
        assert reopened.owned_quantity("T1") == 75
        assert reopened.owned_quantity("T2") == 0
    finally:
        reopened.conn.close()


def test_a_replayed_leg_does_not_break_the_invariant(led):
    """Reconcile re-reads and postback retries replay legs; the sum must not drift."""
    led.record_order_fill("o-t1-entry", "T1", 75, tradingsymbol=SYM, leg="ENTRY")
    led.record_order_fill("o-t2-entry", "T2", 75, tradingsymbol=SYM, leg="ENTRY")

    for _ in range(5):
        led.record_order_fill("o-t1-entry", "T1", 75, tradingsymbol=SYM, leg="ENTRY")
        led.record_order_fill("o-t2-entry", "T2", 75, tradingsymbol=SYM, leg="ENTRY")

    _assert_matches(led, 150, "five replays of both entries")


def test_the_certification_report_agrees_with_the_replay(led):
    """The replay and the gate that reads it must reach the same verdict."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "ledger_cert_replay",
        Path(__file__).resolve().parents[1] / "scripts" / "ledger_certification.py",
    )
    cert = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cert)

    led.record_order_fill("o-t1-entry", "T1", 75, tradingsymbol=SYM, leg="ENTRY")
    led.record_order_fill("o-t2-entry", "T2", 75, tradingsymbol=SYM, leg="ENTRY")
    led.record_order_fill("o-t2-stop", "T2", -75, tradingsymbol=SYM, leg="PROTECTIVE")

    ledger = {SYM: _holdings(led)}

    assert cert._report(ledger, {SYM: 75}, offline=False) == 0
    assert cert._report(ledger, {SYM: 150}, offline=False) == 1, (
        "the gate should fail when the broker holds quantity the ledger cannot explain"
    )


# ── the replay must be able to fail ──────────────────────────────────────────

def test_a_missing_leg_is_caught_by_the_invariant(led):
    """Proof the replay discriminates: skip the stop recording and it must fail.

    This is the defect that actually shipped — a protective stop firing with nothing
    written — so the replay has to detect exactly that.
    """
    led.record_order_fill("o-t1-entry", "T1", 75, tradingsymbol=SYM, leg="ENTRY")
    led.record_order_fill("o-t2-entry", "T2", 75, tradingsymbol=SYM, leg="ENTRY")

    # T2's stop fires at the broker; the ledger is never told.
    with pytest.raises(AssertionError, match="ledger attributes 150, broker holds 75"):
        _assert_matches(led, 75, "an unrecorded stop")
