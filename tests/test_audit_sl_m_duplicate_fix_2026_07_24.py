"""
Audit fix verification (2026-07-24): exchange-side SL-M duplicate / naked-leg fixes.

Reference: reports/AUTO_MODE_READINESS_AUDIT_2026-07-24.md

- BLOCKER-1: place_protective_stop signals ambiguity (raises BrokerOrderAmbiguous)
  on a lost/empty acknowledgement instead of returning None. A None return is
  indistinguishable from a clean reject, so the caller re-places and TWO SL-M orders
  rest at the exchange; when both trigger, the second opens a naked reversed leg.
- BLOCKER-2: TradeManager.ensure_broker_protection serialises the read-decide-place
  window (dedicated _protection_lock) and is idempotent (adopts an existing working
  stop via broker.find_working_protective_stop), so two concurrent callers, or a
  lost acknowledgement, can never create a duplicate SL-M for one fill.
- HIGH-2: enabling Auto Mode while already in REAL now re-runs
  validate_real_mode_readiness and blocks when the broker/feed is not ready.

BLOCKER-1 was originally proved against the AngelOne broker, which reported failures
as error dicts. Kite reports them as typed exceptions, so the fakes below changed
shape — the invariant did not. A broker migration is precisely when an invariant like
this gets dropped silently, so it is re-proved here against the live broker.
"""
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from trading.broker import Broker
# Imported via kite_broker, not kiteconnect: that module falls back to stub exception
# classes when the SDK is absent, so these tests run on a machine without it. A direct
# `from kiteconnect.exceptions import ...` is a collection error, and pytest aborts the
# WHOLE run on one of those — one optional dependency would silently zero the suite.
from trading.kite_broker import KiteBroker, NetworkException, OrderException
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected
from trading.trade_manager import IST, Trade, TradeManager
from scanner import Scanner


# ───────────────────────── BLOCKER-1: broker ambiguity signalling ─────────────────────────

class FakeKite:
    def __init__(self, response=None, raises=None, order_book=None):
        self.response = response
        self.raises = raises
        self.order_book = order_book if order_book is not None else []
        self.place_calls = 0

    def place_order(self, **params):
        self.place_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.response

    def orders(self):
        if isinstance(self.order_book, Exception):
            raise self.order_book
        return self.order_book


def _kite_broker(**kw):
    broker = KiteBroker(kite=FakeKite(**kw))
    broker._min_call_interval = 0.0      # no real pacing needed against a fake
    return broker


def test_place_protective_stop_raises_ambiguous_on_api_exception():
    # An SL-M can reach/accept at the exchange while the HTTP ack is lost. A
    # NetworkException is exactly that: the call did not complete.
    broker = _kite_broker(raises=NetworkException("read timed out after submit"))
    with pytest.raises(BrokerOrderAmbiguous):
        broker.place_protective_stop("NIFTYTESTFUT", "123", 10, "SELL", 95.0, "CARRYFORWARD")


def test_place_protective_stop_raises_ambiguous_on_missing_order_id():
    """Kite returns an id on success, so an empty one is unexplained — not a reject.

    Returning None here is the BLOCKER-1 bug verbatim: the caller reads it as a clean
    refusal and places a second SL-M on a position that may already have one.
    """
    broker = _kite_broker(response=None)
    with pytest.raises(BrokerOrderAmbiguous):
        broker.place_protective_stop("NIFTYTESTFUT", "123", 10, "SELL", 95.0, "CARRYFORWARD")


def test_place_protective_stop_returns_none_on_clean_reject():
    # OrderException = the exchange/RMS refused it outright; nothing is resting.
    broker = _kite_broker(raises=OrderException("RMS: margin exceeds available"))
    assert broker.place_protective_stop("NIFTYTESTFUT", "123", 10, "SELL", 95.0, "CARRYFORWARD") is None


def test_place_protective_stop_returns_id_on_success():
    broker = _kite_broker(response="SLM-1")
    assert broker.place_protective_stop("NIFTYTESTFUT", "123", 10, "SELL", 95.0, "CARRYFORWARD") == "SLM-1"


# ───────────────── find_working_protective_stop (idempotency order-book scan) ─────────────────

def test_find_working_protective_stop_matches_working_sl():
    rows = [{
        "tradingsymbol": "NIFTYTESTFUT", "order_id": "SLM-99",
        "order_type": "SL-M", "transaction_type": "SELL",
        "status": "TRIGGER PENDING",
    }]
    broker = _kite_broker(order_book=rows)
    assert broker.find_working_protective_stop("NIFTYTESTFUT", "123", "SELL") == "SLM-99"


def test_find_working_protective_stop_ignores_terminal_wrong_symbol_side_and_type():
    rows = [
        {"tradingsymbol": "NIFTYTESTFUT", "order_type": "SL-M", "transaction_type": "SELL",
         "status": "CANCELLED", "order_id": "DEAD"},
        {"tradingsymbol": "OTHERFUT", "order_type": "SL-M", "transaction_type": "SELL",
         "status": "TRIGGER PENDING", "order_id": "WRONGSYM"},
        {"tradingsymbol": "NIFTYTESTFUT", "order_type": "SL-M", "transaction_type": "BUY",
         "status": "TRIGGER PENDING", "order_id": "WRONGSIDE"},
        {"tradingsymbol": "NIFTYTESTFUT", "order_type": "MARKET", "transaction_type": "SELL",
         "status": "OPEN", "order_id": "NOTSTOP"},
    ]
    broker = _kite_broker(order_book=rows)
    assert broker.find_working_protective_stop("NIFTYTESTFUT", "", "SELL") == ""


def test_find_working_protective_stop_scan_failure_returns_empty_not_raise():
    broker = _kite_broker(order_book=NetworkException("orders endpoint down"))
    assert broker.find_working_protective_stop("NIFTYTESTFUT", "123", "SELL") == ""


def test_base_broker_find_working_protective_stop_defaults_empty():
    assert Broker().find_working_protective_stop("NIFTYTESTFUT", "123", "SELL") == ""


# ───────────────── BLOCKER-2: ensure_broker_protection is serialised + idempotent ─────────────────

class StopManagerBroker:
    """Scriptable protective-stop broker double."""

    def __init__(self, *, ambiguous=False, reaches_exchange=True, preexisting="", place_delay=0.0):
        self.ambiguous = ambiguous
        self.reaches_exchange = reaches_exchange
        self.preexisting = preexisting
        self.place_delay = place_delay
        self.place_calls = 0
        self.modify_calls = 0
        self._placed_id = ""
        self._lock = threading.Lock()

    def find_working_protective_stop(self, symbol, token, side):
        with self._lock:
            return self.preexisting or self._placed_id

    def place_protective_stop(self, symbol, token, qty, side, trigger_price, product_type):
        if self.place_delay:
            time.sleep(self.place_delay)
        with self._lock:
            self.place_calls += 1
            if self.reaches_exchange:
                self._placed_id = f"SLM-{symbol}"
            placed = self._placed_id
        if self.ambiguous:
            raise BrokerOrderAmbiguous("lost ack after protective stop submit")
        return placed

    def modify_protective_stop(self, *a, **k):
        with self._lock:
            self.modify_calls += 1
        return True

    def get_order_status(self, order_id):
        return None


def _protection_manager(broker):
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.ui_logger = None
    manager.mode = "REAL"
    manager.product_type = "CARRYFORWARD"
    manager.futures_sl_pct = 0.3
    manager.options_sl_pct = 15.0
    manager._lock = threading.RLock()
    manager._protection_lock = threading.RLock()
    manager.save_state = lambda: None
    manager._last_protective_sync = {}
    manager._last_protective_status_poll = {}
    manager._last_recovery_alert = {}
    return manager


def _fut_trade(**over):
    fields = dict(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, entry_time=IST.localize(datetime(2026, 6, 15, 10, 0)),
        trailing_stop=95.0, current_stop=95.0, lots=1, lot_size=10, grade="A",
        inst_type="FUT", trading_symbol="NIFTYTESTFUT", symbol_token="TOK",
        broker_quantity=10,
    )
    fields.update(over)
    return Trade(**fields)


def test_ensure_broker_protection_adopts_preexisting_stop_without_placing():
    broker = StopManagerBroker(preexisting="SLM-PRE")
    manager = _protection_manager(broker)
    trade = _fut_trade()

    assert manager.ensure_broker_protection(trade, 10) is True
    assert broker.place_calls == 0
    assert trade.protective_order_id == "SLM-PRE"


def test_ensure_broker_protection_places_single_slm_under_concurrency():
    # The core BLOCKER-2 case: PositionSync reconcile + scanner tick both drive
    # protection for the same fresh fill; without serialisation both would place.
    broker = StopManagerBroker(place_delay=0.05)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    results = []
    barrier = threading.Barrier(2)

    def _arm():
        barrier.wait()
        results.append(manager.ensure_broker_protection(trade, 10))

    threads = [threading.Thread(target=_arm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [True, True]
    assert broker.place_calls == 1  # never a duplicate exchange-side SL-M
    assert trade.protective_order_id == "SLM-NIFTYTESTFUT"


def test_ensure_broker_protection_recovers_ambiguous_via_rescan():
    # Ack lost but the SL-M reached the exchange → adopt it, do not re-place.
    broker = StopManagerBroker(ambiguous=True, reaches_exchange=True)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    assert manager.ensure_broker_protection(trade, 10) is True
    assert broker.place_calls == 1
    assert trade.protective_order_id == "SLM-NIFTYTESTFUT"
    assert trade.execution_status != "RECOVERY_REQUIRED"


def test_ensure_broker_protection_marks_recovery_when_ambiguous_and_stop_absent():
    # Ack lost AND no stop at the exchange → fail closed to RECOVERY_REQUIRED,
    # never a blind re-place.
    broker = StopManagerBroker(ambiguous=True, reaches_exchange=False)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    assert manager.ensure_broker_protection(trade, 10) is False
    assert broker.place_calls == 1
    assert trade.protective_order_id == ""
    assert trade.execution_status == "RECOVERY_REQUIRED"


def test_ensure_broker_protection_lazily_initialises_protection_lock():
    # Partially-constructed managers (tests / recovery paths) must not crash.
    broker = StopManagerBroker(preexisting="SLM-PRE")
    manager = _protection_manager(broker)
    del manager._protection_lock
    assert manager.ensure_broker_protection(_fut_trade(), 10) is True
    assert isinstance(manager._protection_lock, type(threading.RLock()))


# ───────── A refused stop must be FLAGGED, never allowed to escape the caller ─────────
# place_protective_stop has three failure exits, not one: BrokerOrderAmbiguous, plus
# BrokerOrderRejected (dead Kite session) and PermissionError (the account lease is held
# elsewhere). Only the first was caught. The other two unwound out of
# ensure_broker_protection AND out of reconcile_broker_positions, so a filled position was
# booked OPEN with no stop, no recovery flag — a healthy-looking row on the dashboard —
# and every trade after it in the reconcile loop went unreconciled.

class _RaisingStopBroker:
    """Protective-stop placement fails the way a dead session or lost lease fails.

    `gtt_error` matters: the real KiteBroker raises BrokerOrderRejected out of
    place_protective_stop from exactly one place — _require_api, i.e. no session — and a
    broker with no session cannot arm a GTT either. A fake that refuses the stop but
    happily returns a trigger id describes a state the real broker cannot reach, and
    would let the dead-session branch look protected when it is not.
    """

    def __init__(self, error, gtt_error=None):
        self.error = error
        self.gtt_error = gtt_error
        self.gtt_calls = []
        self.exits = []

    def find_working_protective_stop(self, symbol, token, side):
        return ""

    def inspect_working_protective_stop(self, symbol, token, side, **kw):
        return {"state": "NONE", "order_id": "", "reason": "no working stop", "matches": []}

    def place_protective_stop(self, *a, **k):
        raise self.error

    def find_gtt_stop(self, symbol):
        return {"state": "NONE", "trigger_id": "", "reason": "no active GTT"}

    def place_gtt_stop(self, symbol, qty, side, trigger, last_price=0.0, product_type=""):
        self.gtt_calls.append((symbol, qty, side, trigger))
        if self.gtt_error is not None:
            raise self.gtt_error
        return f"GTT-{symbol}"

    def cancel_gtt_stop(self, trigger_id):
        return True

    def get_order_status(self, order_id):
        return None

    def get_positions(self):
        return {"data": []}

    def close_order(self, symbol, token, qty, side, product_type, price=0.0):
        self.exits.append((symbol, qty, side))
        return "EXIT-1"


@pytest.mark.parametrize(
    "error",
    [
        BrokerOrderRejected("protective stop blocked: no authenticated Kite session", terminal=True),
        PermissionError("protective stop blocked: REAL broker account is owned by another UT1 process"),
    ],
    ids=["dead-session", "lease-denied"],
)
def test_a_refused_protective_stop_is_flagged_not_raised(error):
    broker = _RaisingStopBroker(error)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    # Must not raise: an escaping exception leaves the trade looking healthy.
    assert manager.ensure_broker_protection(trade, 10) is False
    assert trade.execution_status == "RECOVERY_REQUIRED"
    assert trade.protective_order_id == ""


def test_a_lease_denied_stop_does_not_arm_a_gtt_on_someone_elses_account():
    """The other holder is managing that account; a trigger from us stacks on theirs.

    The fake's GTT WOULD succeed, so this asserts the discrimination, not an accident.
    """
    broker = _RaisingStopBroker(PermissionError("owned by another UT1 process"))
    manager = _protection_manager(broker)
    trade = _fut_trade()

    manager.ensure_broker_protection(trade, 10)
    assert broker.gtt_calls == [], "armed a server-side trigger without holding the lease"


def test_a_dead_session_cannot_arm_a_gtt_either_and_says_so():
    """Honest about the real shape: no session means no SL-M AND no GTT.

    The recovery path attempts the fallback, but the operator must not be told the
    position is protected server-side when nothing could be armed.
    """
    dead = BrokerOrderRejected("no authenticated Kite session", terminal=True)
    broker = _RaisingStopBroker(dead, gtt_error=dead)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    assert manager.ensure_broker_protection(trade, 10) is False
    assert broker.gtt_calls, "the GTT fallback was never even attempted"
    assert trade.execution_status == "RECOVERY_REQUIRED"
    assert "GTT fallback armed" not in (trade.execution_error or ""), (
        "claimed server-side protection that was never armed"
    )


def test_a_dead_session_still_reconciles_the_trades_after_it():
    """The raise used to abort the whole reconcile loop, not just this trade."""
    broker = _RaisingStopBroker(
        BrokerOrderRejected("no authenticated Kite session", terminal=True)
    )
    manager = _protection_manager(broker)
    manager.open_trades = {}
    manager.closed_trades = []
    manager.orphan_positions = {}
    manager.max_positions = 4
    manager._last_mark_to_market_save = 0.0

    first = _fut_trade(id="T_FIRST", trading_symbol="NIFTYTESTFUT")
    second = _fut_trade(id="T_SECOND", trading_symbol="BANKNIFTYTESTFUT")
    for t in (first, second):
        t.execution_status = "ENTRY_SUBMITTED"
        t.pending_execution = True
        t.entry_submitted_at = datetime.now(IST)
        manager.open_trades[t.id] = t

    rows = [
        {"tradingsymbol": "NIFTYTESTFUT", "netqty": "10", "buyavgprice": "100", "sellavgprice": "0"},
        {"tradingsymbol": "BANKNIFTYTESTFUT", "netqty": "10", "buyavgprice": "100", "sellavgprice": "0"},
    ]
    result = manager.reconcile_broker_positions({"data": rows})

    assert result["checked"] is True
    assert second.execution_status != "ENTRY_SUBMITTED", (
        "the second trade never got reconciled; the first one's failure aborted the loop"
    )


# ───────── Flattening must not market out over a stop we cannot see ─────────

class _FlattenBroker:
    """Order book holds an SL-M whose id the trade never learned (lost ack)."""

    def __init__(self, stop_state, cancel_ok=True):
        self.stop_state = stop_state
        self.cancel_ok = cancel_ok
        self.cancelled = []
        self.exits = []

    def inspect_working_protective_stop(self, symbol, token, side, **kw):
        return self.stop_state

    def find_working_protective_stop(self, symbol, token, side):
        return str(self.stop_state.get("order_id") or "") if self.stop_state.get("state") == "FOUND" else ""

    def cancel_order(self, order_id, variety="NORMAL"):
        self.cancelled.append((order_id, variety))
        return self.cancel_ok

    def close_order(self, symbol, token, qty, side, product_type, price=0.0):
        self.exits.append((symbol, qty, side))
        return "EXIT-1"


def _flatten_manager(broker):
    manager = _protection_manager(broker)
    manager.open_trades = {}
    manager.closed_trades = []
    return manager


def test_flatten_cancels_an_unowned_stop_found_in_the_book_before_market_exiting():
    """protective_order_id is empty after a lost ack — the stop is still out there.

    Market-exiting on top of it leaves the SL-M resting on a flat book; when it later
    triggers it opens a naked reversed leg. That is the BLOCKER-1 hazard by another road.
    """
    broker = _FlattenBroker({
        "state": "FOUND", "order_id": "SLM-ORPHAN", "reason": "working", "matches": [{}],
    })
    manager = _flatten_manager(broker)
    trade = _fut_trade()
    trade.protective_order_id = ""          # the id was never learned

    manager.emergency_flatten_trade(trade, "unprotected position")

    assert ("SLM-ORPHAN", "STOPLOSS") in broker.cancelled, (
        "market exit sent without cancelling the SL-M resting at the exchange"
    )


def test_flatten_is_withheld_when_the_stop_scan_is_inconclusive():
    """UNKNOWN is not NONE. Exiting on a book we could not read can double-exit."""
    broker = _FlattenBroker({
        "state": "UNKNOWN", "order_id": "", "reason": "order book unreadable", "matches": [],
    })
    manager = _flatten_manager(broker)
    trade = _fut_trade()
    trade.protective_order_id = ""

    assert manager.emergency_flatten_trade(trade, "unprotected position") is False
    assert broker.exits == [], "flattened against an order book we could not read"


def test_flatten_is_withheld_when_two_stops_rest_on_the_contract():
    """Cancelling matches[0] and exiting leaves the SECOND stop resting on a flat book.

    That is the duplicate-SL-M naked-leg outcome verbatim — the state this guard claims
    to prevent — so AMBIGUOUS must withhold rather than cancel one and proceed.
    """
    broker = _FlattenBroker({
        "state": "AMBIGUOUS", "order_id": "", "reason": "2 working stops",
        "matches": [{"orderid": "SLM-A"}, {"orderid": "SLM-B"}],
    })
    manager = _flatten_manager(broker)
    trade = _fut_trade()
    trade.protective_order_id = ""

    assert manager.emergency_flatten_trade(trade, "unprotected position") is False
    assert broker.cancelled == [], "cancelled one of two resting stops"
    assert broker.exits == [], "market-exited with a second stop still resting"


def test_flatten_does_not_strip_another_open_trades_protection():
    """Two trades on one strike is supported; the scan matches contract, not trade."""
    broker = _FlattenBroker({
        "state": "FOUND", "order_id": "A_SLM_111", "reason": "working", "matches": [{}],
    })
    manager = _flatten_manager(broker)
    healthy = _fut_trade(id="T_A")
    healthy.protective_order_id = "A_SLM_111"          # A owns this stop and is fine
    stranded = _fut_trade(id="T_B")
    stranded.protective_order_id = ""                  # B lost its ack
    manager.open_trades = {healthy.id: healthy, stranded.id: stranded}

    assert manager.emergency_flatten_trade(stranded, "unprotected position") is False
    assert broker.cancelled == [], "cancelled a stop another open trade is managing"
    assert healthy.protective_order_id == "A_SLM_111"


def test_repeated_inconclusive_scans_escalate_to_the_operator():
    """Withholding is right, but silently withholding to the close is not.

    check_session_end retries the flatten every pass; an unreadable order book would
    otherwise block it invisibly and the NRML position rides overnight.
    """
    broker = _FlattenBroker({
        "state": "UNKNOWN", "order_id": "", "reason": "order book unreadable", "matches": [],
    })
    manager = _flatten_manager(broker)
    trade = _fut_trade()
    trade.protective_order_id = ""

    for _ in range(3):
        assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is False
    assert "ACT MANUALLY" in (trade.execution_error or ""), (
        "three blocked flattens produced no operator instruction"
    )


def test_flatten_proceeds_when_the_book_definitively_shows_no_stop():
    broker = _FlattenBroker({
        "state": "NONE", "order_id": "", "reason": "no working stop", "matches": [],
    })
    manager = _flatten_manager(broker)
    trade = _fut_trade()
    trade.protective_order_id = ""

    assert manager.emergency_flatten_trade(trade, "unprotected position") is True
    assert broker.exits == [("NIFTYTESTFUT", 10, "SELL")]


# ───────────────── HIGH-2: enabling Auto Mode in REAL re-validates readiness ─────────────────

def _real_scanner():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False
    scanner.on_notification = None
    scanner.capital_total = 500000.0
    scanner.data = SimpleNamespace()
    scanner.trades = SimpleNamespace(has_live_exposure=lambda: False)
    return scanner


def test_enable_auto_mode_while_already_real_is_blocked_when_not_ready(monkeypatch):
    scanner = _real_scanner()
    monkeypatch.setattr(
        "scanner.validate_real_mode_readiness",
        lambda settings, provider, **kwargs: {
            "ok": False,
            "blockers": ["all broker sources unavailable"],
            "warnings": [],
        },
    )

    configured = scanner.configure(
        auto_mode=True, confirm_real_mode=True, real_mode_verification="YES",
    )

    assert configured is False
    assert scanner.auto_mode is False
    assert "readiness" in scanner._last_configure_block_reason.lower()


def test_enable_auto_mode_in_real_succeeds_when_ready(monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False
    scanner.on_notification = None
    scanner.data = SimpleNamespace()
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    today = datetime.now(IST).date().isoformat()
    scanner._morning_refresh_summary = {
        "session_day": today,
        "history": True,
        "expiry": True,
        "index_quotes": 4,
        "chains": 4,
        "indices": {
            name: {"quote": True, "chain": True}
            for name in scanner.active_indices
        },
        "complete": True,
    }
    scanner.futures_sl_pct = 0.3
    scanner.options_sl_pct = 15.0
    scanner.max_daily_loss_pct = 3.0
    scanner.risk_fut_pct = 2.0
    scanner.risk_opt_pct = 8.0
    scanner.capital_fut = 250000.0
    scanner.capital_opt = 100000.0
    scanner.capital_total = 1100000.0
    scanner.trades = SimpleNamespace(
        update_risk_settings=lambda *a, **k: None,
        has_live_exposure=lambda: False,
    )
    scanner.risk_manager = SimpleNamespace(update_settings=lambda *a, **k: None)
    scanner._publish_config_snapshot = lambda *a, **k: None
    scanner.mtf = SimpleNamespace(apply_engine_params=lambda *a, **k: None)
    # This fixture verifies the readiness/configuration path.  Resource pressure is
    # host state and is covered by dedicated resource-guard tests; leaving it live
    # makes this semantic test depend on the machine's RAM/commit usage.
    scanner._auto_execution_resource_blockers = lambda: []
    monkeypatch.setattr(
        "scanner.validate_real_mode_readiness",
        lambda settings, provider, **kwargs: {"ok": True, "blockers": [], "warnings": []},
    )

    configured = scanner.configure(
        auto_mode=True, confirm_real_mode=True, real_mode_verification="YES",
        persist_settings=False,
    )

    assert configured is True
    assert scanner.auto_mode is True


# ───────── Adoption must read the tri-state, not the collapsed order id ─────────
# find_working_protective_stop returns "" for AMBIGUOUS and CONFLICT exactly as it does
# for NONE, and "" on the adopt path means "place a new stop". So the moment the broker
# gained those states, two resting SL-Ms started producing a THIRD.

class _StateBroker:
    """Reports a scripted inspection state; records what it was asked to place."""

    def __init__(self, state, order_id="", quantity=None):
        self.state = state
        self.order_id = order_id
        self.quantity = quantity
        self.placed = []
        self.gtts = []

    def inspect_working_protective_stop(
        self, symbol, token, side, quantity=0, product_type="", trigger_price=0.0,
    ):
        self.quantity = quantity
        self.trigger_price = trigger_price
        return {"state": self.state, "order_id": self.order_id, "reason": "", "matches": []}

    def find_working_protective_stop(self, symbol, token, side, quantity=0):
        return self.order_id if self.state == "FOUND" else ""

    def place_protective_stop(self, symbol, token, qty, side, trigger, product_type):
        self.placed.append((symbol, qty, trigger))
        return f"SLM-NEW-{len(self.placed)}"

    def place_gtt_stop(self, *a, **k):
        self.gtts.append(a)
        return "GTT-1"

    def find_gtt_stop(self, symbol):
        return {"state": "NONE", "trigger_id": ""}

    def get_order_status(self, order_id):
        return None


@pytest.mark.parametrize("state", ["AMBIGUOUS", "CONFLICT"])
def test_an_unmatchable_resting_stop_never_gets_a_duplicate(state):
    """Two stops must not become three. Fail closed and let a human reconcile."""
    broker = _StateBroker(state)
    manager = _protection_manager(broker)
    trade = _fut_trade()

    assert manager.ensure_broker_protection(trade, 10) is False
    assert broker.placed == [], f"{state} scan still placed another stop"
    assert trade.execution_status == "RECOVERY_REQUIRED"


def test_adoption_passes_this_positions_own_size_to_the_scan():
    """Without a quantity the broker's size discrimination is dead code."""
    broker = _StateBroker("NONE")
    manager = _protection_manager(broker)
    manager.ensure_broker_protection(_fut_trade(), 10)
    assert broker.quantity == 10, "adopt path scanned with no quantity"


def test_adoption_does_not_point_two_trades_at_one_stop():
    broker = _StateBroker("FOUND", order_id="SLM-A")
    manager = _protection_manager(broker)
    healthy = _fut_trade(id="T_A")
    healthy.protective_order_id = "SLM-A"
    stranded = _fut_trade(id="T_B")
    manager.open_trades = {healthy.id: healthy, stranded.id: stranded}

    assert manager.ensure_broker_protection(stranded, 10) is False
    assert stranded.protective_order_id == "", "adopted another trade's SL-M"
    assert broker.placed == [], "or duplicated it"


# ───────── GTT adoption has the same ownership and level requirements ─────────

class _GttBroker:
    def __init__(self, existing):
        self.existing = existing
        self.armed = []

    def place_gtt_stop(self, symbol, qty, side, trigger, last_price=0.0, product_type=""):
        self.armed.append((symbol, trigger))
        return "GTT-NEW"

    def find_gtt_stop(self, symbol):
        return self.existing

    def cancel_gtt_stop(self, trigger_id):
        return True


def test_gtt_adoption_refuses_another_trades_trigger():
    """find_gtt_stop matches the contract only — no side, size or level."""
    broker = _GttBroker({"state": "FOUND", "trigger_id": "701", "trigger_values": [112.0]})
    manager = _protection_manager(broker)
    owner = _fut_trade(id="T_A")
    owner.gtt_trigger_id = "701"
    other = _fut_trade(id="T_B")
    manager.open_trades = {owner.id: owner, other.id: other}

    assert manager._arm_gtt_fallback(other, 10, "SELL", 90.0) is False
    assert other.gtt_trigger_id == "", "two trades now point at one GTT"


def test_gtt_adoption_refuses_a_trigger_at_the_wrong_level():
    """A trigger at someone else's stop fires early or late by the difference."""
    broker = _GttBroker({"state": "FOUND", "trigger_id": "701", "trigger_values": [112.0]})
    manager = _protection_manager(broker)
    manager.open_trades = {}
    trade = _fut_trade()

    assert manager._arm_gtt_fallback(trade, 10, "SELL", 90.0) is False


def test_gtt_adoption_accepts_our_own_orphaned_trigger():
    """The case it exists for: we armed it, the ack was lost, no id was recorded."""
    broker = _GttBroker({"state": "FOUND", "trigger_id": "701", "trigger_values": [95.0]})
    manager = _protection_manager(broker)
    manager.open_trades = {}
    trade = _fut_trade()

    assert manager._arm_gtt_fallback(trade, 10, "SELL", 95.0) is True
    assert trade.gtt_trigger_id == "701"
    assert broker.armed == [], "armed a second trigger instead of adopting"


# ───────── The blocked-flatten deadline is an opt-in risk lever ─────────

def _blocked_manager():
    broker = _FlattenBroker({
        "state": "UNKNOWN", "order_id": "", "reason": "order book unreadable", "matches": [],
    })
    manager = _flatten_manager(broker)
    return broker, manager


def test_blocked_flatten_withholds_forever_by_default():
    """Default 0 = never accept the double-exit risk automatically."""
    settings = get_settings()
    old = settings.recovery_flatten_block_deadline_seconds
    try:
        settings.recovery_flatten_block_deadline_seconds = 0.0
        broker, manager = _blocked_manager()
        trade = _fut_trade()
        trade.protective_order_id = ""
        for _ in range(6):
            assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is False
        assert broker.exits == []
        assert trade.reversal_watch_required is False
    finally:
        settings.recovery_flatten_block_deadline_seconds = old


def test_blocked_flatten_exits_after_the_deadline_and_flags_a_reversal_watch():
    """Opted in: exit anyway, but record that a resting stop may reverse the position."""
    settings = get_settings()
    old = settings.recovery_flatten_block_deadline_seconds
    try:
        settings.recovery_flatten_block_deadline_seconds = 0.01
        broker, manager = _blocked_manager()
        trade = _fut_trade()
        trade.protective_order_id = ""

        assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is False
        time.sleep(0.05)
        assert manager.emergency_flatten_trade(trade, "FORCE_EOD_KILL") is True
        assert broker.exits == [("NIFTYTESTFUT", 10, "SELL")]
        assert trade.reversal_watch_required is True, (
            "exited over a possibly-resting stop without flagging the reversal risk"
        )
    finally:
        settings.recovery_flatten_block_deadline_seconds = old
