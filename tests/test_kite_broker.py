"""KiteBroker must preserve the live-money invariants carried over from AngelOne.

Kite reports failures as typed exceptions instead of error dicts, so the classification
moves from "which envelope is this?" to "does this exception prove the order never
reached the exchange?". Getting that wrong is how you duplicate a live position:

  NetworkException/DataException/GeneralException -> the call did not complete, the
      order MAY be resting at the exchange -> AMBIGUOUS, never retried.
  TokenException/PermissionException/InputException -> refused outright, retrying
      cannot help -> terminal rejection.
  OrderException -> exchange/RMS rejected it, definitively not live, and the cause
      (margin) can clear -> retryable rejection.

And the two polarity rules proved on 2026-07-31: a refused cancel and a refused
protective-stop modify must both report False, never True.
"""
import threading

import pytest

from trading.kite_broker import (
    DataException,
    GeneralException,
    InputException,
    KiteBroker,
    NetworkException,
    OrderException,
    PermissionException,
    TokenException,
    classify_kite_exception,
)
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected


class _Kite:
    """Minimal stand-in for KiteConnect."""

    def __init__(self, *, place=None, orders=None, positions=None, cancel=None, modify=None):
        self._place = place
        self._orders = orders if orders is not None else []
        self._positions = positions or {"net": []}
        self._cancel = cancel
        self._modify = modify
        self.placed = []

    def place_order(self, **params):
        self.placed.append(params)
        if isinstance(self._place, Exception):
            raise self._place
        return self._place

    def orders(self):
        if isinstance(self._orders, Exception):
            raise self._orders
        return self._orders

    def positions(self):
        return self._positions

    def cancel_order(self, **_kw):
        if isinstance(self._cancel, Exception):
            raise self._cancel
        return "CANCELLED"

    def modify_order(self, **_kw):
        if isinstance(self._modify, Exception):
            raise self._modify
        return "MODIFIED"


def _broker(kite):
    b = KiteBroker(kite=kite)
    b._execution_lease = None
    b._min_call_interval = 0.0
    return b


def _order_row(**over):
    row = {
        "order_id": "250731000123", "status": "OPEN", "tradingsymbol": "NIFTY2680424350CE",
        "transaction_type": "BUY", "quantity": 65, "order_type": "LIMIT",
        "product": "NRML", "exchange": "NFO",
    }
    row.update(over)
    return row


# ── classification ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("exc", [NetworkException("t"), DataException("d"), GeneralException("g")])
def test_incomplete_calls_are_ambiguous(exc):
    assert classify_kite_exception(exc) == "AMBIGUOUS"


@pytest.mark.parametrize("exc", [TokenException("t"), PermissionException("p"), InputException("i")])
def test_outright_refusals_are_terminal(exc):
    assert classify_kite_exception(exc) == "TERMINAL"


def test_rms_rejection_is_retryable():
    assert classify_kite_exception(OrderException("insufficient margin")) == "RETRYABLE"


def test_unknown_failures_default_to_ambiguous():
    """Assuming 'not placed' is the assumption that duplicates a live order."""
    assert classify_kite_exception(RuntimeError("who knows")) == "AMBIGUOUS"


# ── entry ────────────────────────────────────────────────────────────────────
def test_successful_entry_returns_the_order_id():
    kite = _Kite(place="250731000123")
    assert _broker(kite).place_order("NIFTY2680424350CE", "12345", 65, "BUY", price=124.45) == "250731000123"
    sent = kite.placed[0]
    assert sent["exchange"] == "NFO" and sent["product"] == "NRML"
    assert sent["variety"] == "regular"


def test_entries_take_the_market_by_default():
    """A limit that does not fill is a missed signal.

    Every 2026-07-31 entry went out as a blind LTP mark-up limit against an empty
    book, and the cancel/replace path converts a stale limit to MARKET after ~12s
    anyway — so a limit mostly bought a delay. Slippage is bounded by
    market_protection instead. Those signals graded 27-93% confidence; all of them
    would take the market under this policy.
    """
    kite = _Kite(place="OID")
    _broker(kite).place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45, confidence=0.93)   # 0-1: the scale the live caller uses
    sent = kite.placed[0]
    assert sent["order_type"] == "MARKET"
    assert sent["market_protection"] == -1
    assert "price" not in sent


def test_only_a_high_conviction_signal_waits_for_a_price():
    kite = _Kite(place="OID")
    _broker(kite).place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45, confidence=0.97)
    sent = kite.placed[0]
    assert sent["order_type"] == "LIMIT" and sent["price"] == 124.45
    # A protection band is meaningless on a limit — the limit IS the bound.
    assert "market_protection" not in sent


def test_an_entry_without_a_reference_price_is_always_market():
    kite = _Kite(place="OID")
    _broker(kite).place_order("NIFTY2680424350CE", "1", 65, "BUY", price=0.0, confidence=0.99)
    assert kite.placed[0]["order_type"] == "MARKET"


def test_sensex_routes_to_bfo():
    kite = _Kite(place="X")
    _broker(kite).place_order("SENSEX2680678100CE", "1", 20, "BUY", price=511.85)
    assert kite.placed[0]["exchange"] == "BFO"


def test_network_failure_on_entry_is_ambiguous_never_a_rejection():
    """The order may be live; a rejection here would let the retry duplicate it."""
    with pytest.raises(BrokerOrderAmbiguous):
        _broker(_Kite(place=NetworkException("read timed out"))).place_order(
            "NIFTY2680424350CE", "1", 65, "BUY", price=124.45
        )


def test_dead_session_on_entry_is_a_terminal_rejection():
    with pytest.raises(BrokerOrderRejected) as err:
        _broker(_Kite(place=TokenException("Invalid access token"))).place_order(
            "NIFTY2680424350CE", "1", 65, "BUY", price=124.45
        )
    assert err.value.terminal is True


def test_margin_rejection_is_retryable_not_terminal():
    with pytest.raises(BrokerOrderRejected) as err:
        _broker(_Kite(place=OrderException("Insufficient funds"))).place_order(
            "NIFTY2680424350CE", "1", 65, "BUY", price=124.45
        )
    assert err.value.terminal is False


def test_missing_order_id_with_unreadable_book_stays_ambiguous():
    """Never conclude 'not placed' from a scan that did not complete."""
    with pytest.raises(BrokerOrderAmbiguous):
        _broker(_Kite(place="", orders=NetworkException("book down"))).place_order(
            "NIFTY2680424350CE", "1", 65, "BUY", price=124.45
        )


def test_missing_order_id_with_empty_book_is_a_clean_rejection():
    with pytest.raises(BrokerOrderRejected):
        _broker(_Kite(place="", orders=[])).place_order(
            "NIFTY2680424350CE", "1", 65, "BUY", price=124.45
        )


def test_missing_order_id_adopts_an_order_found_in_the_book():
    """A lost acknowledgement must never cause a second live order."""
    kite = _Kite(place="", orders=[_order_row()])
    assert _broker(kite).place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45) == "250731000123"


def test_no_session_refuses_instead_of_crashing():
    with pytest.raises(BrokerOrderRejected) as err:
        _broker(None).place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45)
    assert err.value.terminal is True
    assert "kite_login" in err.value.reason


# ── polarity rules ───────────────────────────────────────────────────────────
def test_refused_cancellation_reports_failure():
    """True here makes emergency_flatten_trade market-exit over a live SL-M."""
    assert _broker(_Kite(cancel=InputException("no such order"))).cancel_order("1", "regular") is False
    assert _broker(_Kite()).cancel_order("1", "regular") is True


def test_refused_stop_modify_reports_failure():
    b = _broker(_Kite(modify=OrderException("rejected")))
    assert b.modify_protective_stop("1", "NIFTY2680424350CE", "1", 65, "SELL", 105.78) is False
    assert _broker(_Kite()).modify_protective_stop("1", "NIFTY2680424350CE", "1", 65, "SELL", 105.78) is True


def test_ambiguous_protective_stop_placement_raises_not_returns_none():
    """A duplicate SL-M fires against a flat book and opens a naked reversed leg."""
    with pytest.raises(BrokerOrderAmbiguous):
        _broker(_Kite(place=NetworkException("timeout"))).place_protective_stop(
            "NIFTY2680424350CE", "1", 65, "SELL", 105.78
        )


# ── reads ────────────────────────────────────────────────────────────────────
def test_positions_are_normalised_to_netqty():
    kite = _Kite(positions={"net": [{"tradingsymbol": "NIFTY2680424350CE", "quantity": 65}]})
    rows = _broker(kite).get_positions()["data"]
    assert rows[0]["netqty"] == 65 and rows[0]["tradingsymbol"] == "NIFTY2680424350CE"


def test_working_protective_stop_is_found_and_plain_orders_ignored():
    kite = _Kite(orders=[
        _order_row(order_id="ENTRY-1", order_type="LIMIT"),
        _order_row(order_id="SL-1", order_type="SL-M", transaction_type="SELL", status="TRIGGER PENDING"),
    ])
    result = _broker(kite).inspect_working_protective_stop("NIFTY2680424350CE", "1", "SELL")
    assert result["state"] == "FOUND" and result["order_id"] == "SL-1"


def test_unreadable_book_is_unknown_never_none():
    """NONE would let a second stop be placed on top of an invisible live one."""
    result = _broker(_Kite(orders=NetworkException("down"))).inspect_working_protective_stop(
        "NIFTY2680424350CE", "1", "SELL"
    )
    assert result["state"] == "UNKNOWN"


def test_order_status_is_normalised_for_the_cancel_confirmation_loop():
    kite = _Kite(orders=[_order_row(status="COMPLETE")])
    row = _broker(kite).get_order_status("250731000123")
    assert row["orderstatus"] == "complete"


class _GttKite(_Kite):
    def __init__(self, gtts=None, place=None, delete=None):
        super().__init__()
        self._gtts = gtts if gtts is not None else []
        self._gtt_place = place
        self._gtt_delete = delete
        self.placed_gtt = []
        self.GTT_TYPE_SINGLE = "single"

    def get_gtts(self):
        if isinstance(self._gtts, Exception):
            raise self._gtts
        return self._gtts

    def place_gtt(self, **kw):
        self.placed_gtt.append(kw)
        if isinstance(self._gtt_place, Exception):
            raise self._gtt_place
        return self._gtt_place or {"trigger_id": 55501}

    def delete_gtt(self, trigger_id):
        if isinstance(self._gtt_delete, Exception):
            raise self._gtt_delete
        return {"trigger_id": trigger_id}


def test_gtt_stop_is_armed_on_the_server():
    """A GTT survives this process dying; an SL-M in the order book does not."""
    kite = _GttKite()
    assert _broker(kite).place_gtt_stop(
        "NIFTY2680424350CE", 65, "SELL", 105.78, last_price=124.45
    ) == "55501"
    sent = kite.placed_gtt[0]
    assert sent["exchange"] == "NFO" and sent["trigger_values"] == [105.8]
    assert sent["orders"][0]["transaction_type"] == "SELL"


def test_refused_gtt_returns_none_not_a_fake_id():
    assert _broker(_GttKite(place=InputException("bad trigger"))).place_gtt_stop(
        "NIFTY2680424350CE", 65, "SELL", 105.78, last_price=124.45
    ) is None


def test_refused_gtt_deletion_reports_failure():
    """Same polarity rule as cancel_order: True would flatten over a live trigger."""
    assert _broker(_GttKite(delete=InputException("nope"))).cancel_gtt_stop("55501") is False
    assert _broker(_GttKite()).cancel_gtt_stop("55501") is True


def test_gtt_scan_failure_is_unknown_never_none():
    """NONE from a failed scan would authorise a duplicate protective trigger."""
    result = _broker(_GttKite(gtts=NetworkException("down"))).find_gtt_stop("NIFTY2680424350CE")
    assert result["state"] == "UNKNOWN"


def test_existing_active_gtt_is_found():
    kite = _GttKite(gtts=[{
        "id": 55501, "status": "active",
        "condition": {"tradingsymbol": "NIFTY2680424350CE", "trigger_values": [105.78]},
    }])
    assert _broker(kite).find_gtt_stop("NIFTY2680424350CE")["trigger_id"] == "55501"


class _FillKite(_Kite):
    def __init__(self, trades=None, history=None):
        super().__init__()
        self._trades = trades
        self._history = history

    def order_trades(self, order_id):
        if isinstance(self._trades, Exception):
            raise self._trades
        return self._trades or []

    def order_history(self, order_id):
        if isinstance(self._history, Exception):
            raise self._history
        return self._history or []


def test_fill_reports_real_quantity_and_average_price():
    """Replaces inferring a fill; 2026-07-31 rows carried phantom P&L on no fill."""
    kite = _FillKite(trades=[
        {"quantity": 40, "average_price": 124.50, "fill_timestamp": "2026-07-31 13:56:20"},
        {"quantity": 25, "average_price": 124.90, "fill_timestamp": "2026-07-31 13:56:21"},
    ])
    fill = _broker(kite).get_fill("250731000123")
    assert fill["state"] == "FILLED" and fill["quantity"] == 65
    assert fill["average_price"] == pytest.approx(124.65, abs=0.01)


def test_no_executions_is_distinct_from_an_unreadable_lookup():
    assert _broker(_FillKite(trades=[])).get_fill("X")["state"] == "NO_FILL"
    assert _broker(_FillKite(trades=NetworkException("down"))).get_fill("X")["state"] == "UNKNOWN"


def test_order_lifecycle_surfaces_the_rejection_reason():
    """The order book says 'is it there now'; history says whether it was ever taken."""
    kite = _FillKite(history=[
        {"status": "PUT ORDER REQ RECEIVED", "order_timestamp": "13:56:19"},
        {"status": "REJECTED", "status_message": "RMS:Margin exceeds", "order_timestamp": "13:56:20"},
    ])
    life = _broker(kite).get_order_lifecycle("250731000123")
    assert life["state"] == "REJECTED"
    assert "Margin exceeds" in life["reason"]
    assert len(life["transitions"]) == 2


def test_dead_token_invalidates_the_session_for_the_watchdog():
    class _Auth:
        def __init__(self): self.reason = ""
        def invalidate(self, reason): self.reason = reason

    auth = _Auth()
    b = KiteBroker(kite=_Kite(place=TokenException("Invalid access token")), auth=auth)
    b._execution_lease = None
    b._min_call_interval = 0.0
    with pytest.raises(BrokerOrderRejected):
        b.place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45)
    assert "Invalid access token" in auth.reason


# ── every account write needs the lease ──────────────────────────────────────
class _DeniedLease:
    """Stands in for a lease this process could not take."""
    held = False
    owner_metadata = {"instance_id": "UT1:other", "pid": 4242, "host": "box"}

    def acquire(self):
        return False


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.place_protective_stop("NIFTYFUT", "1", 10, "SELL", 95.0),
        lambda b: b.modify_protective_stop("OID-1", "NIFTYFUT", "1", 10, "SELL", 96.0),
        lambda b: b.place_gtt_stop("NIFTYFUT", 10, "SELL", 95.0, 100.0),
        lambda b: b.cancel_order("OID-1"),
    ],
    ids=["place-stop", "modify-stop", "place-gtt", "cancel"],
)
def test_account_writes_refuse_without_the_execution_lease(call):
    """A GTT and a stop-modify are real orders on the account, not local state.

    modify_protective_stop and place_gtt_stop each shipped without this guard, so a
    second instance that did NOT own the account could rewrite or stack on the stop the
    true holder was managing.
    """
    b = _broker(_Kite(place="OID-9"))
    b._execution_lease = _DeniedLease()
    with pytest.raises((PermissionError, BrokerOrderRejected)):
        call(b)


def test_a_lost_gtt_acknowledgement_adopts_the_live_trigger():
    """A GTT rests on Zerodha's servers, so an incomplete call may still have armed one.

    Reporting that as a clean refusal leaves no local id and the next cycle arms a
    SECOND trigger; one fires and exits, the other fires against a flat book.
    """
    class _Kite2:
        def place_gtt(self, **_kw):
            raise NetworkException("gateway timeout")

        def get_gtts(self):
            return [{
                "id": 55501, "status": "active",
                "condition": {"tradingsymbol": "NIFTYFUT", "trigger_values": [95.0]},
            }]

    b = _broker(_Kite2())
    assert b.place_gtt_stop("NIFTYFUT", 10, "SELL", 95.0, 100.0) == "55501"


def test_an_unreadable_trigger_list_after_a_lost_gtt_ack_stays_ambiguous():
    class _Kite3:
        def place_gtt(self, **_kw):
            raise NetworkException("gateway timeout")

        def get_gtts(self):
            raise NetworkException("trigger list unreadable")

    with pytest.raises(BrokerOrderAmbiguous):
        _broker(_Kite3()).place_gtt_stop("NIFTYFUT", 10, "SELL", 95.0, 100.0)


# ── the LIMIT gate must be reachable with the confidence the LIVE caller passes ──
def test_limit_entry_is_reachable_with_a_real_zero_to_one_confidence():
    """trade.confidence is a FRACTION; the threshold shipped as 95.0.

    `confidence >= 95.0` can never be true for a value <= 1.0, so the LIMIT path was
    dead and every entry went out MARKET however strong the signal — the exact opposite
    of the documented policy.
    """
    kite = _Kite(place="OID")
    _broker(kite).place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45,
                              confidence=0.97)
    assert kite.placed[0]["order_type"] == "LIMIT"


def test_an_ordinary_conviction_fraction_still_takes_the_market():
    kite = _Kite(place="OID")
    _broker(kite).place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45,
                              confidence=0.93)
    assert kite.placed[0]["order_type"] == "MARKET"


def test_a_percentage_scale_threshold_is_still_honoured(monkeypatch):
    """An existing 95.0 in .env must keep meaning 95%, not disable the gate."""
    from config.settings import get_settings

    settings = get_settings()
    old = settings.entry_limit_confidence_threshold
    try:
        settings.entry_limit_confidence_threshold = 95.0
        assert KiteBroker._entry_limit_confidence_threshold() == pytest.approx(0.95)
        kite = _Kite(place="OID")
        _broker(kite).place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45,
                                  confidence=0.97)
        assert kite.placed[0]["order_type"] == "LIMIT"
    finally:
        settings.entry_limit_confidence_threshold = old
