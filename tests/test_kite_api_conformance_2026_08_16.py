"""Kite Connect v3 conformance fixes, audited 2026-08-16 against the live docs.

Context that shapes every one of these: no live order round trip has ever completed on
this system — exit_order_id is empty on every stored row and trading_mode has been
HISTORICAL throughout — so none of these defects has bitten yet. They are precisely the
faults that surface on day one of AUTO, which is why they are worth fixing while nothing
is at stake.

Grouped by the audit's own lettering so each test maps to one finding.
"""
import re
import threading
from datetime import datetime

import pytest
import pytz

from kiteconnect.exceptions import (
    DataException,
    GeneralException,
    InputException,
    NetworkException,
    OrderException,
    PermissionException,
    TokenException,
)

from config.settings import Settings
from trading.kite_broker import (
    KiteBroker,
    _status_is_working,
    _warn_if_above_freeze_limit,
    classify_kite_exception,
)
from trading.trade_manager import Trade, TradeManager

IST = pytz.timezone("Asia/Kolkata")


class _Api:
    def __init__(self, **canned):
        self.sent = None
        self.modified = None
        self._canned = canned

    def place_order(self, **kw):
        self.sent = kw
        return "OID-1"

    def modify_order(self, **kw):
        self.modified = kw
        return "OID-1"

    def orders(self):
        return self._canned.get("orders", [])

    def margins(self):
        return self._canned.get("margins", {})

    def order_margins(self, payload):
        self.sent = payload
        return self._canned.get("order_margins", [])

    def get_gtts(self):
        return self._canned.get("gtts", [])


def _broker(**canned):
    b = KiteBroker.__new__(KiteBroker)
    b.api = _Api(**canned)
    b._order_lock = threading.Lock()
    b._execution_lease = None
    b._auth = None
    b._last_call_at = 0.0
    b._min_call_interval = 0.0
    b._api_call = lambda ctx, fn, timeout=5.0, **_kw: fn()
    b._entry_rate_allowed = lambda: True
    b._ip_block_alerted = ""
    return b


def _trade(**kw):
    base = dict(
        id="T0003_NIFTY_08010930", instrument="NIFTY", timeframe="5min",
        direction="LONG", entry_price=124.45, entry_time=datetime.now(IST),
        trailing_stop=105.78, current_stop=105.78, lots=1, lot_size=65, grade="A",
        inst_type="OPT", status="OPEN", trading_symbol="NIFTY2680424350CE",
        symbol_token="16816386", broker_order_id="ENTRY-1",
    )
    base.update(kw)
    return Trade(**base)


def _manager(trade=None):
    m = TradeManager.__new__(TradeManager)
    m.open_trades = {trade.id: trade} if trade else {}
    m.closed_trades = []
    m._lock = threading.RLock()
    m.save_state = lambda: None
    m.ui_logger = None
    m._log_ui = lambda *a, **kw: None
    m.on_notification = lambda *a, **kw: None
    # CAPTURE, do not swallow. The first cut of this file stubbed _record_fill to a
    # no-op, which is exactly why it failed to catch the ledger SIGN bug below: the
    # postback rewrite wrote exit-side fills into the signed ledger as POSITIVE.
    m.recorded_fills = []
    m._record_fill = lambda tr, oid, qty, leg: m.recorded_fills.append((oid, qty, leg))
    m._alert_blocking_broker_condition = lambda *a, **kw: None
    return m


# ── A6. order-status enum: missing interim states, and the unsafe default ─────

@pytest.mark.parametrize("status", [
    "modify validation pending", "cancel pending", "amo req received", "modified",
])
def test_the_missing_interim_statuses_count_as_working(status):
    """All four were absent, so an order sitting in one read as finished. A cancel that
    has not been confirmed has NOT removed the order."""
    assert _status_is_working(status) is True


@pytest.mark.parametrize("status", ["complete", "cancelled", "rejected"])
def test_only_the_three_terminal_states_are_finished(status):
    assert _status_is_working(status) is False


def test_an_unrecognised_status_is_treated_as_live():
    """The spec calls the status list open-ended. Both callers use this to decide
    whether to place ANOTHER order, so concluding 'absent' from an unfamiliar state is
    what puts a second entry — or a second SL-M — on a position that already has one."""
    assert _status_is_working("SOME FUTURE STATE") is True


def test_a_blank_status_is_not_treated_as_live():
    """_normalise_order always sets the key, so blank means the row carried no status
    at all rather than an unfamiliar one."""
    assert _status_is_working("") is False
    assert _status_is_working(None) is False


# ── B3. available_funds conflated ABSENT with ZERO ───────────────────────────

def test_a_zero_live_balance_is_reported_not_skipped():
    """The fully-deployed account — exactly when the funds gate must bite. `if value:`
    fell through to opening_balance, which is the day-start figure and never decreases
    as margin is consumed, so the gate read back the morning's cash and passed."""
    b = _broker(margins={"equity": {"available": {
        "live_balance": 0.0, "cash": 50000.0, "opening_balance": 90000.0,
    }}})
    assert b.available_funds() == pytest.approx(0.0)


def test_a_missing_live_balance_still_falls_through():
    """Only an ABSENT key may fall through — that part was right and must not regress."""
    b = _broker(margins={"equity": {"available": {
        "cash": 50000.0, "opening_balance": 90000.0,
    }}})
    assert b.available_funds() == pytest.approx(50000.0)


# ── B4. a zero margin total silently disabled the funds gate ─────────────────

def test_an_unpriced_order_margin_is_unknown_not_free():
    """Buying an option always costs at least the premium, so total:0 means Kite did not
    price it. Returning 0.0 made preflight compute committed=0.0 and pass
    unconditionally instead of degrading to MARGIN_UNVERIFIED."""
    b = _broker(order_margins=[{"total": 0.0}])
    assert b.required_entry_margin("NIFTY2680424350CE", 65, "BUY") is None


def test_a_real_margin_total_is_returned():
    b = _broker(order_margins=[{"total": 8123.5}])
    assert b.required_entry_margin("NIFTY2680424350CE", 65, "BUY") == pytest.approx(8123.5)


# ── B5. the margin request ignored the LIMIT price ───────────────────────────

def test_a_limit_entry_prices_its_margin_as_a_limit():
    b = _broker(order_margins=[{"total": 8000.0}])
    b.required_entry_margin("NIFTY2680424350CE", 65, "BUY", price=124.45)
    row = b.api.sent[0]
    assert row["order_type"] == "LIMIT"
    assert row["price"] == pytest.approx(124.45)


def test_a_market_entry_still_prices_as_market():
    b = _broker(order_margins=[{"total": 8000.0}])
    b.required_entry_margin("NIFTY2680424350CE", 65, "BUY")
    row = b.api.sent[0]
    assert row["order_type"] == "MARKET"
    assert "price" not in row


# ── the funds gate must announce when it is inert ────────────────────────────

class _NoMarginsApi(_Api):
    def margins(self):
        # The exact live symptom measured on 2026-08-16.
        raise RuntimeError("Message build error")


def _preflight_at(monkeypatch, when):
    """Run preflight with the wall clock pinned to `when` (naive IST)."""
    import datetime as _dt

    b = KiteBroker.__new__(KiteBroker)
    b.api = _NoMarginsApi(order_margins=[{"total": 8000.0}])
    b._order_lock = threading.Lock()
    b._auth = None
    b._last_call_at = 0.0
    b._min_call_interval = 0.0
    b._api_call = lambda c, fn, timeout=5.0, **_kw: fn()
    b._margin_lock = threading.Lock()
    b._margin_reservations = {}
    b._margin_reservation_ttl_seconds = 90.0

    real = _dt.datetime

    class _Pinned(real):
        @classmethod
        def now(cls, tz=None):
            return tz.localize(when) if tz else when

    monkeypatch.setattr(_dt, "datetime", _Pinned)
    return b.preflight_entry_margin(
        symbol="NIFTY2680424350CE", qty=65, side="BUY", price=124.45
    )


def test_an_inert_funds_gate_is_announced_during_market_hours(monkeypatch):
    """/user/margins failing outside hours is a documented Zerodha quirk and failing open
    is right. During market hours the same silence means the gate is passing every entry
    unchecked at precisely the moment it should bite, and the first symptom would be an
    RMS rejection on a live order. It must not be on a human to check at some particular
    minute — 09:20 was an arbitrary number."""
    import datetime as _dt

    verdict = _preflight_at(monkeypatch, _dt.datetime(2026, 8, 17, 10, 30))
    assert verdict["ok"] is True          # still fails OPEN — it does not block entries
    assert verdict["basis"] == "FUNDS_UNCHECKED_IN_SESSION"
    assert "DURING market hours" in verdict["limitation"]


def test_outside_market_hours_it_stays_quiet(monkeypatch):
    """The documented quirk. Blocking every entry on it would be worse."""
    import datetime as _dt

    verdict = _preflight_at(monkeypatch, _dt.datetime(2026, 8, 17, 2, 30))
    assert verdict["ok"] is True
    assert verdict["basis"] == "REQUIREMENT_ONLY"


def test_the_weekend_is_not_market_hours(monkeypatch):
    import datetime as _dt

    verdict = _preflight_at(monkeypatch, _dt.datetime(2026, 8, 15, 10, 30))  # Saturday
    assert verdict["basis"] == "REQUIREMENT_ONLY"


# ── C1. insufficient funds is not "the order may be live" ────────────────────

def test_a_4xx_general_exception_is_definitively_not_placed():
    """pykiteconnect resolves the error class by NAME and defines no MarginException, so
    Kite's commonest real rejection arrives as GeneralException. It sat in the AMBIGUOUS
    tuple, leaking the entry slot, the margin reservation and a concurrent-position
    slot for an order that never reached the exchange."""
    assert classify_kite_exception(GeneralException("Insufficient funds", code=400)) == "RETRYABLE"


def test_a_5xx_general_exception_stays_ambiguous():
    """The safe direction is unchanged for anything that genuinely did not complete."""
    assert classify_kite_exception(GeneralException("upstream died", code=502)) == "AMBIGUOUS"


def test_the_sdk_default_code_stays_ambiguous():
    """KiteException defaults to code=500, so an exception built without one is unknown."""
    assert classify_kite_exception(GeneralException("no code given")) == "AMBIGUOUS"


def test_a_network_failure_is_still_ambiguous():
    assert classify_kite_exception(NetworkException("connection reset")) == "AMBIGUOUS"
    assert classify_kite_exception(DataException("garbled")) == "AMBIGUOUS"


@pytest.mark.parametrize("exc", [
    TokenException("dead session"), PermissionException("no"), InputException("bad"),
])
def test_terminal_exceptions_are_unchanged(exc):
    assert classify_kite_exception(exc) == "TERMINAL"


def test_order_exception_is_unchanged():
    assert classify_kite_exception(OrderException("rms")) == "RETRYABLE"


# ── D1. MIS could not be configured at all ───────────────────────────────────

def test_the_product_setting_admits_both_products():
    """A single-member Literal meant MIS was unreachable even though _PRODUCT_MAP
    supported it. The default is deliberately unchanged."""
    field = Settings.model_fields["trade_product_type"]
    assert field.default == "CARRYFORWARD"
    assert Settings(trade_product_type="INTRADAY").trade_product_type == "INTRADAY"


def test_the_product_map_still_resolves_both():
    assert KiteBroker._product("CARRYFORWARD") == "NRML"
    assert KiteBroker._product("INTRADAY") == "MIS"


# ── D2. market_protection was missing on the SL-M stop ───────────────────────

def test_the_protective_stop_carries_market_protection():
    """SL-M is the other order type the spec's rule names. When this triggers it becomes
    a market order into whatever book exists while the trade is going against us."""
    b = _broker()
    b._authorize_mutation = lambda *_a, **_kw: None
    b.place_protective_stop("NIFTY2680424350CE", "1", 65, "SELL", 105.75)
    assert b.api.sent["order_type"] == "SL-M"
    assert b.api.sent["market_protection"] == -1


def test_tightening_the_stop_keeps_market_protection():
    """Kite replaces an order's parameters rather than merging them, so omitting it on
    the modify would strip the band off the first time the stop is tightened."""
    b = _broker()
    b._authorize_mutation = lambda *_a, **_kw: None
    assert b.modify_protective_stop("OID-1", "NIFTY2680424350CE", "1", 65, "SELL", 110.0) is True
    assert b.api.modified["market_protection"] == -1


# ── D3. the pacing floor came from the wrong rate-limit row ──────────────────

def test_the_call_floor_matches_the_order_limit_not_the_candle_limit():
    """0.34s (~2.9/s) was the HISTORICAL-CANDLE row. Order placement allows 10/s, and
    every broker call is serialised behind this floor, so the wrong row throttled exits
    too. 0.12s is ~8.3/s, with headroom."""
    b = KiteBroker(kite=_Api())
    assert b._min_call_interval == pytest.approx(0.12)
    assert 1.0 / b._min_call_interval < 10.0, "must stay under the documented 10 req/s"


# ── D4. freeze quantity: warn, never block ───────────────────────────────────

def test_an_oversized_order_warns():
    """Captured through a loguru sink. The first version of this ended in `or True`,
    which made it unconditionally green — it would have passed with the whole function
    replaced by `pass`, so it asserted nothing at all."""
    from loguru import logger as _logger

    seen = []
    sink = _logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        _warn_if_above_freeze_limit("NIFTY2680424350CE", 5000)   # over NIFTY's 1,800
        _warn_if_above_freeze_limit("NIFTY2680424350CE", 100)    # comfortably under
    finally:
        _logger.remove(sink)
    assert len(seen) == 1, f"expected exactly one warning, got {seen}"
    assert "freeze limit" in seen[0].lower()
    assert "1800" in seen[0].replace(",", ""), "the breached limit must be named"


def test_the_freeze_guard_never_raises():
    """It must never block: the exchange is the real authority and these limits are
    revised periodically, so a stale entry here would refuse valid orders."""
    for qty in (0, -1, 65, 5000, 10 ** 9):
        assert _warn_if_above_freeze_limit("BANKNIFTY26AUG57500PE", qty) is None
    assert _warn_if_above_freeze_limit("", 100) is None
    assert _warn_if_above_freeze_limit("UNKNOWNIDX123CE", 10 ** 6) is None


def test_banknifty_is_not_matched_as_nifty():
    """Longest-prefix first, or BANKNIFTY would be judged against NIFTY's 1,800 limit."""
    from trading.kite_broker import _FREEZE_QUANTITY_UNITS

    assert _FREEZE_QUANTITY_UNITS["BANKNIFTY"] != _FREEZE_QUANTITY_UNITS["NIFTY"]
    ordered = sorted(_FREEZE_QUANTITY_UNITS, key=len, reverse=True)
    assert ordered.index("BANKNIFTY") < ordered.index("NIFTY")


# ── D5. a triggered GTT read as absent ───────────────────────────────────────

def test_a_triggered_gtt_is_found_not_absent():
    """A GTT firing is not a fill: it places a LIMIT that can rest unfilled or be
    rejected. Reading it as NONE let a second trigger be armed on the same contract."""
    b = _broker(gtts=[{
        "id": 77, "status": "triggered",
        "condition": {"tradingsymbol": "NIFTY2680424350CE", "trigger_values": [105.0]},
    }])
    found = b.find_gtt_stop("NIFTY2680424350CE")
    assert found["state"] == "FOUND"
    assert found["trigger_id"] == "77"


def test_an_active_gtt_is_still_found():
    b = _broker(gtts=[{
        "id": 78, "status": "active",
        "condition": {"tradingsymbol": "NIFTY2680424350CE", "trigger_values": [105.0]},
    }])
    assert b.find_gtt_stop("NIFTY2680424350CE")["state"] == "FOUND"


def test_a_cancelled_gtt_is_absent():
    b = _broker(gtts=[{
        "id": 79, "status": "cancelled",
        "condition": {"tradingsymbol": "NIFTY2680424350CE", "trigger_values": [105.0]},
    }])
    assert b.find_gtt_stop("NIFTY2680424350CE")["state"] == "NONE"


# ── E1 / F3. close_order discarded its own classification ────────────────────

def test_a_terminal_exit_refusal_raises_instead_of_returning_none():
    """Returning None made a dead session indistinguishable from a transient refusal, so
    the caller retried the full schedule with real risk open. All four call sites
    already catch BrokerOrderRejected."""
    from trading.order_retry import BrokerOrderRejected

    b = _broker()
    b._authorize_mutation = lambda *_a, **_kw: None
    b._api_call = lambda ctx, fn, timeout=5.0, **_kw: (_ for _ in ()).throw(
        PermissionException("IP (2405:201:8000:d071::1) is not allowed to place orders")
    )
    with pytest.raises(BrokerOrderRejected) as caught:
        b.close_order("NIFTY2680424350CE", "1", 65, "SELL")
    assert caught.value.terminal is True


def test_an_ip_block_on_an_exit_is_escalated():
    """close_order was the ONLY order path that never escalated an IP block, so a block
    first seen on an exit stayed a per-order error line."""
    from trading.order_retry import BrokerOrderRejected

    b = _broker()
    b._authorize_mutation = lambda *_a, **_kw: None
    seen = []
    b._escalate_if_ip_blocked = lambda detail: seen.append(detail)
    b._api_call = lambda ctx, fn, timeout=5.0, **_kw: (_ for _ in ()).throw(
        PermissionException("IP (2405:201:8000:d071::1) is not allowed to place orders")
    )
    with pytest.raises(BrokerOrderRejected):
        b.close_order("NIFTY2680424350CE", "1", 65, "SELL")
    assert seen and "not allowed to place orders" in seen[0]


def test_an_ambiguous_exit_is_still_ambiguous():
    """Nothing may double-exit a position — this direction must not regress."""
    from trading.order_retry import BrokerOrderAmbiguous

    b = _broker()
    b._authorize_mutation = lambda *_a, **_kw: None
    b._api_call = lambda ctx, fn, timeout=5.0, **_kw: (_ for _ in ()).throw(
        NetworkException("connection reset")
    )
    with pytest.raises(BrokerOrderAmbiguous):
        b.close_order("NIFTY2680424350CE", "1", 65, "SELL")


# ── E4. the IP-block regex required parentheses ──────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("IP (2405:201:8000:d071:18b9:1657:1e0f:ff57) is not allowed to place orders",
     "2405:201:8000:d071:18b9:1657:1e0f:ff57"),
    ("IP 49.37.32.15 is not allowed to place orders for this app", "49.37.32.15"),
])
def test_both_published_shapes_of_the_ip_refusal_are_parsed(message, expected):
    """This project captured the parenthesised form; the Kite docs print it bare. On the
    bare form the address came back empty and the alert took the 'no IPs configured'
    branch — telling the operator to ADD an IP when the real cause was a mismatched one.

    Calls the PRODUCTION function. The first version re-implemented the regex inside the
    test body and asserted against its own copy, so reverting the fix entirely left it
    green — it tested the test.
    """
    from loguru import logger as _logger

    b = _broker()
    b._ip_block_alerted = ""
    seen = []
    sink = _logger.add(lambda m: seen.append(str(m)), level="CRITICAL")
    try:
        b._escalate_if_ip_blocked(f"PermissionException: {message}")
    finally:
        _logger.remove(sink)
    assert seen, "an IP block raised no CRITICAL at all"
    assert expected in seen[0], f"address not quoted back; got {seen[0]!r}"
    # Must NOT take the empty-allow-list branch, which tells the operator to add an
    # address when the real cause is that theirs is not on the list.
    assert "NO allowed IPs" not in seen[0]


# ── F4. the broker kept using a client the auth layer had killed ─────────────

def test_a_dead_token_drops_the_brokers_own_client():
    """auth.invalidate() nulls the client inside KiteAuth, but the broker holds a
    SEPARATE reference — so _require_api kept passing and every later order made a full
    round trip for a guaranteed 403."""
    class _Auth:
        def __init__(self):
            self.told = None

        def invalidate(self, why):
            self.told = why

    b = KiteBroker(kite=_Api(), auth=_Auth())
    b._min_call_interval = 0.0
    with pytest.raises(TokenException):
        b._api_call("place order", lambda: (_ for _ in ()).throw(TokenException("expired")))
    assert b.api is None
    assert b._auth.told and "expired" in b._auth.told


# ── A2-A5. postbacks: matching, coverage, ordering, locking ──────────────────

def test_a_protective_stop_fill_is_matched():
    """Matching was on broker_order_id ALONE, so every postback for an exit, a stop, a
    partial reduce or a scale-in was discarded — including 'your protective stop just
    filled', which is the commonest real exit there is."""
    trade = _trade(protective_order_id="STOP-9")
    manager = _manager(trade)
    assert manager.on_broker_postback({
        "order_id": "STOP-9", "status": "COMPLETE",
        "filled_quantity": 65, "average_price": 101.20,
    }) is True
    assert trade.exit_reason == "STOP_HIT"


def test_an_exit_fill_does_not_overwrite_the_entry_quantity():
    """broker_quantity describes the ENTRY holding. Matching more legs would be worse
    than the bug it replaces if an exit's quantity landed there."""
    trade = _trade(exit_order_id="EXIT-2", broker_quantity=65)
    manager = _manager(trade)
    manager.on_broker_postback({
        "order_id": "EXIT-2", "status": "COMPLETE",
        "filled_quantity": 65, "average_price": 130.0,
    })
    assert trade.broker_quantity == 65


def test_a_partial_fill_update_is_handled():
    """Partial fills arrive as UPDATE. There was no branch and no else, so the push
    channel was structurally blind to every one of them."""
    trade = _trade()
    manager = _manager(trade)
    assert manager.on_broker_postback({
        "order_id": "ENTRY-1", "status": "UPDATE",
        "filled_quantity": 30, "average_price": 124.50,
    }) is True
    # Recorded only: reconciliation stays the sole authority on opening/closing.
    assert trade.status == "OPEN"


def test_the_cancel_spelling_is_accepted():
    """The order API says CANCELLED; the postbacks prose says CANCEL."""
    trade = _trade()
    manager = _manager(trade)
    manager.on_broker_postback({
        "order_id": "ENTRY-1", "status": "CANCEL", "status_message": "user cancelled",
        "order_timestamp": "2026-08-16 10:00:00",
    })
    assert "cancelled" in trade.execution_error.lower()


def test_a_verbatim_redelivery_is_dropped():
    """Zerodha redelivers, and nothing checked."""
    trade = _trade()
    manager = _manager(trade)
    payload = {
        "order_id": "ENTRY-1", "status": "COMPLETE", "filled_quantity": 65,
        "average_price": 124.90, "order_timestamp": "2026-08-16 09:30:05",
    }
    assert manager.on_broker_postback(dict(payload)) is True
    assert manager.on_broker_postback(dict(payload)) is False


def test_a_late_update_cannot_reopen_a_completed_order():
    """The concrete hazard: a delayed entry-COMPLETE arriving after a partial reduce and
    rewriting broker_quantity back to the pre-reduce fill, moments before an SL-M is
    re-armed from exactly that field for a quantity no longer held."""
    trade = _trade()
    manager = _manager(trade)
    manager.on_broker_postback({
        "order_id": "ENTRY-1", "status": "COMPLETE", "filled_quantity": 65,
        "average_price": 124.90, "order_timestamp": "2026-08-16 09:30:05",
    })
    trade.broker_quantity = 30          # a partial reduce happened after the fill
    assert manager.on_broker_postback({
        "order_id": "ENTRY-1", "status": "UPDATE", "filled_quantity": 65,
        "average_price": 124.90, "order_timestamp": "2026-08-16 09:30:04",
    }) is False
    assert trade.broker_quantity == 30


def test_a_strictly_older_stamp_is_dropped():
    trade = _trade()
    manager = _manager(trade)
    manager.on_broker_postback({
        "order_id": "ENTRY-1", "status": "UPDATE", "filled_quantity": 40,
        "order_timestamp": "2026-08-16 09:30:06",
    })
    assert manager.on_broker_postback({
        "order_id": "ENTRY-1", "status": "UPDATE", "filled_quantity": 10,
        "order_timestamp": "2026-08-16 09:30:01",
    }) is False


def test_an_unknown_order_is_still_ignored():
    assert _manager(_trade()).on_broker_postback(
        {"order_id": "NOT-OURS", "status": "COMPLETE"}
    ) is False


# ── the ledger SIGN. A2 matched more legs; it must carry their direction too ──

@pytest.mark.parametrize("field,leg", [
    ("exit_order_id", "EXIT"),
    ("protective_order_id", "PROTECTIVE"),
    ("partial_reduce_order_id", "PARTIAL_EXIT"),
])
def test_an_exit_side_fill_is_recorded_negative(field, leg):
    """_record_fill's contract: "an entry or add is positive, an exit negative, so a
    trade's holding is the sum". Matching the exit legs WITHOUT their sign was worse
    than not matching them at all — a protective stop filling 65 wrote +65, so a FLAT
    position reported 130 owned, and that total feeds exit sizing, the frozen 1R and the
    scale-in gate. Order id is the ledger's primary key under INSERT OR REPLACE, and for
    an exchange-side SL-M there is no competing correctly-signed writer."""
    trade = _trade(**{field: "LEG-1"})
    manager = _manager(trade)
    manager.on_broker_postback({
        "order_id": "LEG-1", "status": "COMPLETE",
        "filled_quantity": 65, "average_price": 101.2,
    })
    assert manager.recorded_fills == [("LEG-1", -65, leg)]


@pytest.mark.parametrize("field,leg", [
    ("broker_order_id", "ENTRY"),
    ("scale_in_order_id", "ADD"),
])
def test_a_position_opening_fill_is_recorded_positive(field, leg):
    trade = _trade(**{field: "LEG-2"})
    manager = _manager(trade)
    manager.on_broker_postback({
        "order_id": "LEG-2", "status": "COMPLETE",
        "filled_quantity": 65, "average_price": 124.9,
    })
    assert manager.recorded_fills == [("LEG-2", 65, leg)]


def test_every_matched_leg_has_a_declared_sign():
    """A leg added to _POSTBACK_LEG_FIELDS without a sign would silently default to
    positive and corrupt a ledger that is summed."""
    from trading.trade_manager import _POSTBACK_LEG_SIGNS

    for _field, leg in TradeManager._POSTBACK_LEG_FIELDS:
        assert leg in _POSTBACK_LEG_SIGNS, f"{leg} has no declared ledger sign"


# ── the realised-delta baseline must not survive a session rollover ──────────

def test_the_realised_baseline_is_stamped_only_after_a_close_succeeds():
    """Ordering invariant, found on re-audit. The first cut advanced the baseline BEFORE
    calling _finalize_closed_trade, which returns False when the trade has already left
    open_trades — so the baseline moved for a trade that never closed and the NEXT close
    on that contract booked a zero delta, losing its P&L entirely. Worse than the
    cumulative double-count the delta exists to prevent.

    Asserted on source ORDER because the defect IS an ordering one: the write must sit
    inside the success branch, after the call, not before it.
    """
    import inspect

    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    call = src.index("self._finalize_closed_trade(")
    stamp = src.index("booked[key] = realised_total")
    assert stamp > call, "the baseline is still stamped before the close is confirmed"
    # and it must be guarded, not merely later in the text
    assert "if booked is not None and key:" in src


@pytest.mark.parametrize("method,marker", [
    ("_run_partial_reduce", "closed_quantity=actually_closed,"),
    ("_run_scale_in", "filled_quantity=actually_added,"),
])
def test_a_zero_fill_does_not_book_a_full_size_child(method, marker):
    """`actually_closed or None` re-introduced the exact fallback the comment beside it
    warns about: _book_partial_child reads None as "size off the REQUESTED lots", so a
    stated ZERO fill booked a full-size child carrying P&L on lots that never left, then
    set broker_quantity to 0. Reachable — _order_filled_quantity returns 0 on NO_FILL.
    """
    import inspect

    fn = getattr(TradeManager, method, None)
    assert fn is not None, f"{method} no longer exists"
    src = inspect.getsource(fn)
    assert marker in src, "the `or None` fallback is back"
    assert " or None,\n" not in src, "an `or None` remains on a fill quantity"


def test_the_realised_baseline_is_cleared_on_session_reset():
    """Kite reports `realised` cumulatively PER DAY. Left standing across a rollover,
    day 2's first exit on a strike also traded on day 1 books (today - yesterday) and is
    wrong by the whole of yesterday's P&L on that contract."""
    import inspect

    src = inspect.getsource(TradeManager.reset_session)
    assert "_realised_booked_today = {}" in src


# ── A1. the WebSocket order-update feed ──────────────────────────────────────

def test_the_ticker_subscribes_to_order_updates():
    """It rides the socket already open for ticks: no public URL, no tunnel, no
    checksum. kite_postback_url is empty by default, so without this the system had NO
    push order-state channel and learned every fill from a 5s poll."""
    import inspect

    from data.market_data import MarketDataProvider

    src = inspect.getsource(MarketDataProvider.start_kite_ticker)
    assert "ticker.on_order_update = on_order_update" in src


def test_order_updates_are_dispatched_off_the_socket_thread():
    """kiteconnect dispatches callbacks synchronously on the socket thread, and the sink
    takes the TradeManager lock and writes state — blocking there stalls the tick feed
    for all four indices."""
    import inspect

    from data.market_data import MarketDataProvider

    src = inspect.getsource(MarketDataProvider.start_kite_ticker)
    idx = src.index("def on_order_update")
    assert "threading.Thread" in src[idx:idx + 1400]


def test_the_watchdog_alerts_when_real_is_armed_but_auto_never_was():
    """The live-readiness blocker. `auto_mode_seen_on` is per-process, so a process that
    restarted mid-session has never seen AUTO on and never trips the revert branch —
    while the 09:00 premarket worker DOES re-arm REAL after a restart. Result: dashboard
    shows REAL, feeds green, signals printing, zero entries all day, and not one
    CRITICAL. Indistinguishable from a quiet market until the close.

    Read from main.py's source: the watchdog body is a closure inside _loop, and main.py
    is an entry point no test imports.
    """
    import pathlib

    src = pathlib.Path("main.py").read_text(encoding="utf-8")
    assert "auto_never_armed" in src, "no alert for the restart case"
    idx = src.index("auto_never_armed")
    window = src[max(0, idx - 900):idx + 900]
    # It must key off REAL + in-session, NOT off the per-process latch.
    assert '"REAL"' in window and "in_session" in window
    # And it must not be gated behind the latch that cannot see a restart.
    guard = src[src.index("elif in_session and str("):idx]
    assert "auto_mode_seen_on" not in guard


def test_the_public_postback_listener_is_hardened_like_the_dashboard_route():
    """This is the process actually exposed to the internet — the dashboard route sits
    behind it — so it had the weaker of the two checks, which is backwards."""
    import pathlib

    src = pathlib.Path("scripts/kite_postback_listener.py").read_text(encoding="utf-8")
    assert "secrets.compare_digest" in src, "still a timing-leaky plain comparison"
    assert "status_code=503" in src, "no refusal when api_secret is absent"


def test_the_scanner_attaches_the_sink():
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner.__init__)
    assert "order_update_sink" in src
    assert "on_broker_postback" in src
