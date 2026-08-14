"""Four defects an external audit found in the previous round of fixes.

Each is the same shape as the bug it was meant to fix, reintroduced one layer down.
"""
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from trading.kite_broker import KiteBroker
from trading.order_retry import BrokerOrderRejected
from trading.trade_manager import TradeManager


# ── P0: an explicit zero became full size ────────────────────────────────────

class _T:
    def __init__(self, tid="T1", broker_quantity=0, quantity=75):
        self.id = tid
        self.broker_quantity = broker_quantity
        self.quantity = quantity


def _mgr(ledger=None):
    m = TradeManager.__new__(TradeManager)
    m._ledger_owned_quantity = staticmethod(ledger or (lambda _t: None))
    return m


def test_an_explicitly_requested_zero_stays_zero():
    """`quantity or broker_quantity or quantity` read 0 as absent and armed 75."""
    assert _mgr()._transactable_quantity(_T(), 0) == 0


def test_a_ledger_known_zero_sizes_to_nothing():
    """The ledger made 0 a real answer, which is what made the old shape dangerous."""
    mgr = _mgr(lambda _t: 0)

    assert mgr._transactable_quantity(_T(broker_quantity=0, quantity=75)) == 0


def test_an_unknown_holding_still_falls_back_to_intent():
    """The case the old fallback existed for: entry not confirmed yet."""
    assert _mgr()._transactable_quantity(_T(broker_quantity=0, quantity=75)) == 75


def test_a_confirmed_fill_beats_intent():
    assert _mgr()._transactable_quantity(_T(broker_quantity=65, quantity=75)) == 65


def test_the_ledger_beats_a_stale_confirmed_fill():
    mgr = _mgr(lambda _t: 0)

    assert mgr._transactable_quantity(_T(broker_quantity=75, quantity=75)) == 0


def test_protection_refuses_to_arm_on_zero():
    import inspect

    src = inspect.getsource(TradeManager.ensure_broker_protection)
    i = src.index("_transactable_quantity(trade, quantity)")

    assert "if qty <= 0" in src[i: i + 200], "a zero-quantity SL-M would still be sent"


def test_an_owning_nothing_trade_sends_no_exit():
    import inspect

    src = inspect.getsource(TradeManager._request_broker_exit)

    assert "exit_quantity <= 0" in src, (
        "a trade owning nothing would exit for its intended size, selling a sibling's"
    )


# ── P0: distinct exit legs existed but were not used ─────────────────────────

@pytest.mark.parametrize(
    "method,leg",
    [
        ("_run_partial_reduce", "PARTIAL_EXIT"),
        ("emergency_flatten_trade", "RECOVERY_EXIT"),
        ("_request_broker_exit", "FULL_EXIT"),
    ],
)
def test_each_exit_sends_its_own_leg(method, leg):
    """All three sent legacy "EXIT", so two orders on one trade shared a tag."""
    import inspect

    src = inspect.getsource(getattr(TradeManager, method))

    assert f'"{leg}"' in src, f"{method} still submits a shared exit identity"


def test_the_three_exit_legs_are_distinct():
    tags = {
        KiteBroker.order_tag("T1", leg)
        for leg in ("PARTIAL_EXIT", "FULL_EXIT", "RECOVERY_EXIT")
    }

    assert len(tags) == 3, f"exit legs collide: {tags}"


def test_recovery_looks_up_the_leg_it_sends():
    import inspect

    src = inspect.getsource(TradeManager.emergency_flatten_trade)

    assert '_recover_order_id_from_tag(trade, "RECOVERY_EXIT")' in src


def test_an_order_placed_under_the_legacy_tag_is_still_recoverable():
    """The upgrade hazard: exits resting from the previous build carry "EXIT"."""
    assert "EXIT" in TradeManager._LEGACY_TAG_LEGS["RECOVERY_EXIT"]
    assert "EXIT" in TradeManager._LEGACY_TAG_LEGS["FULL_EXIT"]
    assert "EXIT" in TradeManager._LEGACY_TAG_LEGS["PARTIAL_EXIT"]


# ── P1: authority skipped for a blank account id ─────────────────────────────

def _broker(leased, authority):
    b = KiteBroker.__new__(KiteBroker)
    b.api = object()
    b._execution_lease = SimpleNamespace(held=lambda: True) if leased else None
    b._authority_provider = authority
    return b


@pytest.fixture(autouse=True)
def _lease_stubs(monkeypatch):
    monkeypatch.setattr(
        KiteBroker, "has_execution_lease",
        lambda self: getattr(self, "_execution_lease", None) is not None,
    )
    monkeypatch.setattr(KiteBroker, "acquire_execution_lease", lambda self: True)


def test_a_wired_authority_is_honoured_even_without_a_lease():
    """The fail-open path: a blank account id skipped the mode check entirely."""
    with pytest.raises(BrokerOrderRejected, match="not in REAL mode"):
        _broker(leased=False, authority=lambda: False)._authorize_mutation("entry order")


def test_a_wired_authority_still_permits_when_it_says_yes():
    _broker(leased=False, authority=lambda: True)._authorize_mutation("entry order")


def test_a_lease_less_broker_with_no_authority_is_still_inert():
    """Fixtures build these deliberately; they cannot reach a real account."""
    b = KiteBroker.__new__(KiteBroker)
    b.api = object()
    b._execution_lease = None

    b._authorize_mutation("entry order")


# ── P0 data-safety: a failed pre-count skipped the snapshot ──────────────────

def _db(tmp_path):
    from data.sqlite_db import DatabaseManager

    handle = object.__new__(DatabaseManager)
    handle._init_db(str(tmp_path / "ledger.db"))
    return handle


def test_an_uncountable_prune_still_snapshots(tmp_path, monkeypatch):
    """If every COUNT fails, doomed stayed 0, the snapshot was skipped and DELETE ran.

    The tables are dropped so the counts fail the way they really would -- an
    OperationalError from SQLite, not a synthetic patch.
    """
    handle = _db(tmp_path)
    calls = []
    monkeypatch.setattr(
        type(handle), "_snapshot_before_prune",
        lambda self, doomed, cutoff: calls.append(doomed) or "snap",
    )
    for table in ("session_signals", "signal_decisions", "trades"):
        handle.conn.execute(f"DROP TABLE IF EXISTS {table}")
    handle.conn.commit()

    handle.cleanup_old_records(days=30)

    assert calls, "counts all failed and the prune proceeded with no snapshot"
    handle.conn.close()


def test_an_uncountable_prune_aborts_if_the_snapshot_fails(tmp_path, monkeypatch):
    """Unknown + unbackupable is the one combination that must never delete."""
    handle = _db(tmp_path)
    monkeypatch.setattr(
        type(handle), "_snapshot_before_prune",
        lambda self, doomed, cutoff: (_ for _ in ()).throw(OSError("disk full")),
    )
    for table in ("session_signals", "signal_decisions", "trades"):
        handle.conn.execute(f"DROP TABLE IF EXISTS {table}")
    handle.conn.commit()

    assert handle.cleanup_old_records(days=30) is False
    handle.conn.close()


def test_a_no_op_prune_does_not_vacuum():
    """Nothing to delete should cost no snapshot and no VACUUM."""
    import inspect
    from data.sqlite_db import DatabaseManager

    src = inspect.getsource(DatabaseManager.cleanup_old_records)
    code = " ".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    i = code.index("if doomed == 0:")
    window = code[i: i + 400]

    assert "return True" in window
    assert "VACUUM" not in window


def test_a_failed_cleanup_rolls_back():
    import inspect
    from data.sqlite_db import DatabaseManager

    src = inspect.getsource(DatabaseManager.cleanup_old_records)
    i = src.index("except Exception as e:")

    assert "rollback()" in src[i:], "a part-way failure left tables inconsistently pruned"
