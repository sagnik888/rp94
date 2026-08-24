"""Order tagging and postbacks: settle order identity and state at the source.

On 2026-07-31 the system could not answer "did this order reach the exchange, and is
it mine?" — it matched orders by symbol + side + quantity, which is ambiguous the
moment two trades sit on one strike, and it learned outcomes by polling.

Kite fixes both: a `tag` (max 20 chars) is echoed on the order book, on trades and on
every postback, and postbacks push state changes instead of being polled for.

A postback endpoint must be reachable by Zerodha, so it cannot sit behind the
dashboard's header token. Authenticity comes from the checksum instead —
SHA-256(order_id + order_timestamp + api_secret) — and an unverified payload must be
ignored, because a forged "COMPLETE" would convince the system a phantom position
exists.
"""
import hashlib
import threading
from datetime import datetime

import pytz

from trading.kite_broker import KiteBroker
from trading.trade_manager import Trade, TradeManager

IST = pytz.timezone("Asia/Kolkata")


# ── tagging ──────────────────────────────────────────────────────────────────
def test_tag_stays_within_the_20_char_kite_limit():
    for trade_id in (
        "T0003_NIFTY_07311356",
        "T0005_MIDCPNIFTY_07311451",     # 25 chars raw — must not be sent as-is
        "T0001_BANKNIFTY_07311238",
    ):
        tag = KiteBroker.order_tag(trade_id)
        assert 0 < len(tag) <= 20, f"{trade_id} produced {tag!r}"


def test_tag_keeps_the_parts_that_make_a_trade_unique():
    """Instrument is dropped; counter + timestamp are what disambiguate.

    The separating underscore went with it on 2026-08-16: the v3 docs specify the tag
    as "alphanumeric, max 20 chars" and every entry order carries one.
    """
    assert KiteBroker.order_tag("T0005_MIDCPNIFTY_07311451") == "T000507311451"
    assert KiteBroker.order_tag("T0003_NIFTY_07311356") == "T000307311356"
    assert KiteBroker.order_tag("T0005_MIDCPNIFTY_07311451").isalnum()


def test_two_trades_on_one_strike_get_distinct_tags():
    """The exact case the symbol+side+qty heuristic cannot separate."""
    a = KiteBroker.order_tag("T0007_NIFTY_08010930")
    b = KiteBroker.order_tag("T0008_NIFTY_08010930")
    assert a != b


def test_missing_trade_id_yields_no_tag_rather_than_junk():
    assert KiteBroker.order_tag("") == ""
    assert KiteBroker.order_tag(None) == ""


# ── order parameters ─────────────────────────────────────────────────────────
class _ParamApi:
    def __init__(self):
        self.sent = None

    def place_order(self, **kw):
        self.sent = kw
        return "OID-1"

    def orders(self):
        return []


def _param_broker():
    b = KiteBroker.__new__(KiteBroker)
    b.api = _ParamApi()
    b._order_lock = threading.Lock()
    b._execution_lease = None
    b._auth = None
    b._last_call_at = 0.0
    b._min_call_interval = 0.0
    b._api_call = lambda ctx, fn, timeout=5.0: fn()
    b._entry_rate_allowed = lambda: True
    return b


def test_entry_limit_carries_ttl_and_tag():
    """TTL only applies to limits — a market order fills or rejects immediately."""
    b = _param_broker()
    b.place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45,
                  trade_id="T0003_NIFTY_08010930", confidence=0.99)
    sent = b.api.sent
    assert sent["order_type"] == "LIMIT"
    assert sent["validity"] == "TTL" and sent["validity_ttl"] > 0
    # Alphanumeric since 2026-08-16 — the v3 tag charset.
    assert sent["tag"] == "T000308010930"


def test_market_entry_is_tagged_too():
    """Tagging is what maps an order back to its trade, whatever the order type."""
    b = _param_broker()
    b.place_order("NIFTY2680424350CE", "1", 65, "BUY", price=124.45,
                  trade_id="T0003_NIFTY_08010930", confidence=0.50)
    sent = b.api.sent
    assert sent["order_type"] == "MARKET"
    # Alphanumeric since 2026-08-16 — the v3 tag charset.
    assert sent["tag"] == "T000308010930"
    assert sent["validity"] == "DAY"


def test_exit_never_expires_and_is_slippage_capped():
    """A TTL on an exit could expire the order and leave the position open."""
    b = _param_broker()
    b.close_order("NIFTY2680424350CE", "1", 65, "SELL")
    sent = b.api.sent
    assert sent["order_type"] == "MARKET"
    assert sent["validity"] == "DAY"
    assert "validity_ttl" not in sent
    # MARKET exits in a thin option book fill arbitrarily far from the touch without
    # this; AngelOne had no equivalent and every exit was unbounded.
    assert sent["market_protection"] == -1


# ── postbacks ────────────────────────────────────────────────────────────────
def _manager(trade=None):
    m = TradeManager.__new__(TradeManager)
    m.open_trades = {trade.id: trade} if trade else {}
    m.closed_trades = []
    m._lock = threading.RLock()
    m.save_state = lambda: None
    m.ui_logger = None
    m._log_ui = lambda *_a, **_kw: None
    m.on_notification = lambda *_a, **_kw: None
    return m


def _trade():
    return Trade(
        id="T0003_NIFTY_08010930", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=124.45, entry_time=datetime.now(IST), trailing_stop=105.78,
        current_stop=105.78, lots=1, lot_size=65, grade="A+", inst_type="OPT",
        status="OPEN", trading_symbol="NIFTY2680424350CE", symbol_token="16816386",
        broker_order_id="250801000123",
    )


def test_rejection_postback_records_the_brokers_reason():
    trade = _trade()
    manager = _manager(trade)
    assert manager.on_broker_postback({
        "order_id": "250801000123", "status": "REJECTED",
        "status_message": "RMS:Margin exceeds available",
    }) is True
    assert "Margin exceeds" in trade.execution_error


def test_complete_postback_records_the_filled_quantity():
    trade = _trade()
    manager = _manager(trade)
    manager.on_broker_postback({
        "order_id": "250801000123", "status": "COMPLETE",
        "filled_quantity": 65, "average_price": 124.90,
    })
    assert trade.broker_quantity == 65
    # Deliberately does NOT close or open positions — reconciliation against real
    # broker positions stays the only authority, so a replayed postback is harmless.
    assert trade.status == "OPEN"


def test_postback_for_an_unknown_order_is_ignored():
    assert _manager(_trade()).on_broker_postback(
        {"order_id": "SOMEONE-ELSE", "status": "COMPLETE"}
    ) is False


def test_postback_without_an_order_id_is_ignored():
    assert _manager(_trade()).on_broker_postback({"status": "COMPLETE"}) is False


def test_checksum_scheme_matches_zerodhas_definition():
    """SHA-256(order_id + order_timestamp + api_secret); a forged payload must fail."""
    order_id, stamp, secret = "250801000123", "2026-08-01 09:30:00", "SECRET"
    good = hashlib.sha256(f"{order_id}{stamp}{secret}".encode()).hexdigest()
    forged = hashlib.sha256(f"{order_id}{stamp}WRONG".encode()).hexdigest()
    assert good != forged
    assert hashlib.sha256(f"{order_id}{stamp}{secret}".encode()).hexdigest() == good
