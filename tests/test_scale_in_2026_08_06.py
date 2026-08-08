"""Adding lots to a position that is already working.

Nothing in this system increased an open trade's quantity before now — "cascade
lot-adding" opens SEPARATE trades across timeframes, which is a different thing. Four
design decisions, each made deliberately and each pinned here:

  1. ONE shared stop, re-armed for the full quantity. Every exit path assumes one position
     with one stop; two stops on one trade would mean teaching all of them about legs.
  2. The profit ladder reads the COMBINED position, so this is meant to run with
     profit_lock_per_lot_basis on — otherwise adding size fakes a bigger win.
  3. WEIGHTED-AVERAGE entry price with the original rupee risk FROZEN. Averaging up moves
     the stop distance, which would silently redefine 1R.
  4. An add does NOT consume a position slot — it is the same idea — but it IS new money
     at risk, so it faces the rupee budget and is refused when it does not fit.

Ordering matters and is the opposite of the reduce. partial_close_trade must cancel the
full-size stop FIRST, because exiting on top of a live SL-M double-exits. An add leaves
the stop armed: a failed add changes nothing, and a filled one is merely under-protected
on the new lots until the re-arm — never naked, never over-exited.
"""
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

import config.settings as cs
from trading.order_retry import BrokerOrderAmbiguous, BrokerOrderRejected
from trading.trade_manager import IST, Trade, TradeManager


class _Settings(SimpleNamespace):
    def __init__(self, **kw):
        base = dict(
            scale_in_enabled=True,
            scale_in_max_adds_per_trade=2,
            scale_in_lots_per_add=1,
            scale_in_min_profit_r=0.75,
            scale_in_requires_trend=True,
            scale_in_max_total_lots=6,
            entry_risk_budget_enabled=False,
        )
        base.update(kw)
        super().__init__(**base)


@pytest.fixture
def settings(monkeypatch):
    s = _Settings()
    monkeypatch.setattr(cs, "get_settings", lambda: s)
    return s


def _trade(**kw):
    t = Trade(
        id=kw.get("id", "T1"),
        instrument="NIFTY",
        timeframe="5min",
        direction=kw.get("direction", "LONG"),
        entry_price=kw.get("entry_price", 200.0),
        entry_time=datetime.now(IST),
        trailing_stop=170.0,
        current_stop=170.0,
        lots=kw.get("lots", 2),
        lot_size=75,
        grade="A",
    )
    t.inst_type = "OPT"
    t.trading_symbol = "NIFTY2680524500CE"
    t.symbol_token = "1"
    t.current_price = kw.get("current_price", 240.0)
    t.broker_quantity = t.lots * t.lot_size
    t.initial_stop = 170.0
    t.market_regime = kw.get("regime", "STRONG_UPTREND")
    t.status = "OPEN"
    t.execution_status = "OPEN"
    for k, v in kw.items():
        if hasattr(t, k) and k not in {"lots", "entry_price", "direction", "current_price", "regime", "id"}:
            setattr(t, k, v)
    return t


def _manager(trade, mode="HISTORICAL", broker=None):
    m = TradeManager.__new__(TradeManager)
    m.mode = mode
    m.broker = broker
    m.product_type = "CARRYFORWARD"
    m._lock = threading.RLock()
    m.open_trades = {trade.id: trade}
    m.closed_trades = []
    m.save_state = lambda: None
    m._log_ui = lambda *a, **k: None
    m._mark_recovery = lambda t, r: setattr(t, "execution_status", "RECOVERY_REQUIRED")
    return m


# ── 3. weighted average, frozen R ────────────────────────────────────────────

def test_the_entry_price_becomes_the_weighted_average(settings):
    """2 lots at 200 plus 2 lots at 240 is 4 lots at 220."""
    t = _trade(lots=2, entry_price=200.0)
    ok, why = _manager(t).scale_in_trade("T1", 2, 240.0)
    assert ok, why
    assert t.lots == 4
    assert t.entry_price == pytest.approx(220.0)
    assert t.broker_quantity == 4 * 75


def test_the_original_rupee_risk_is_frozen(settings):
    """Rs.30 x 150 = Rs.4,500 committed at entry, and it stays Rs.4,500."""
    t = _trade(lots=2, entry_price=200.0)
    _manager(t).scale_in_trade("T1", 2, 240.0)
    assert t.frozen_initial_risk_rs == pytest.approx(4500.0)


def test_freezing_happens_before_the_average_moves(settings):
    """Computed after the average, the same arithmetic gives a different, meaningless
    answer: 220 - 170 = 50 over 300 qty is Rs.15,000, not Rs.4,500."""
    t = _trade(lots=2, entry_price=200.0)
    _manager(t).scale_in_trade("T1", 2, 240.0)
    assert t.frozen_initial_risk_rs != pytest.approx(15000.0)


def test_a_second_add_does_not_re_freeze_the_risk(settings):
    t = _trade(lots=2, entry_price=200.0)
    m = _manager(t)
    m.scale_in_trade("T1", 1, 240.0)
    first = t.frozen_initial_risk_rs
    t.current_price = 280.0
    m.scale_in_trade("T1", 1, 280.0)
    assert t.frozen_initial_risk_rs == pytest.approx(first)
    assert t.scale_in_count == 2


def test_the_scanner_reads_the_frozen_risk(settings):
    """The whole point: every R-based exit rule must see the original number."""
    from scanner import Scanner

    t = _trade(lots=2, entry_price=200.0)
    _manager(t).scale_in_trade("T1", 2, 240.0)
    s = Scanner.__new__(Scanner)
    assert s._trade_like_initial_risk_rs(t) == pytest.approx(4500.0)


def test_an_untouched_trade_still_computes_its_risk_normally(settings):
    from scanner import Scanner

    t = _trade(lots=2, entry_price=200.0)
    s = Scanner.__new__(Scanner)
    assert s._trade_like_initial_risk_rs(t) == pytest.approx(4500.0)
    assert float(getattr(t, "frozen_initial_risk_rs", 0.0)) == 0.0


# ── the add has to be earned ─────────────────────────────────────────────────

def test_a_losing_trade_is_never_added_to(settings):
    """Adding to a loser is averaging down."""
    t = _trade(lots=2, entry_price=200.0, current_price=190.0)
    ok, why = _manager(t).scale_in_trade("T1", 1, 190.0)
    assert ok is False
    assert "below" in why


def test_a_barely_profitable_trade_is_not_enough(settings):
    """0.5R of open profit against a 0.75R bar."""
    t = _trade(lots=2, entry_price=200.0, current_price=215.0)
    ok, why = _manager(t).scale_in_trade("T1", 1, 215.0)
    assert ok is False and "0.50R" in why


def test_a_working_trade_qualifies(settings):
    t = _trade(lots=2, entry_price=200.0, current_price=240.0)
    assert _manager(t).scale_in_trade("T1", 1, 240.0)[0] is True


def test_a_counter_trend_regime_blocks_the_add(settings):
    t = _trade(lots=2, entry_price=200.0, current_price=240.0, regime="STRONG_DOWNTREND")
    ok, why = _manager(t).scale_in_trade("T1", 1, 240.0)
    assert ok is False and "opposes" in why


def test_a_rangebound_regime_blocks_the_add(settings):
    t = _trade(lots=2, entry_price=200.0, current_price=240.0, regime="RANGING")
    ok, why = _manager(t).scale_in_trade("T1", 1, 240.0)
    assert ok is False and "not trending" in why


def test_the_trend_requirement_can_be_turned_off(settings):
    settings.scale_in_requires_trend = False
    t = _trade(lots=2, entry_price=200.0, current_price=240.0, regime="RANGING")
    assert _manager(t).scale_in_trade("T1", 1, 240.0)[0] is True


def test_a_futures_short_is_measured_the_right_way_round(settings):
    """A genuine SHORT only exists in futures.

    This was inst_type="OPT" with direction="SHORT" and premium 200 -> 240, described as
    "40 points underwater". For a BOUGHT PE that is a Rs.40/unit PROFIT: the test
    encoded the sign inversion fixed on 2026-08-08.
    """
    t = _trade(lots=2, direction="SHORT", entry_price=200.0, current_price=240.0,
               regime="STRONG_DOWNTREND")
    t.inst_type = "FUT"
    t.trailing_stop = t.current_stop = t.initial_stop = 230.0
    ok, why = _manager(t).scale_in_trade("T1", 1, 240.0)
    assert ok is False, "a SHORT 40 points underwater was treated as profitable"


def test_a_losing_bought_pe_is_refused_an_add(settings):
    """The fix. A PE is stored SHORT but profits when its PREMIUM RISES.

    Premium 200 -> 160 is a loss, and adding to it is the averaging-down this
    function's own docstring forbids.
    """
    t = _trade(lots=2, direction="SHORT", entry_price=200.0, current_price=160.0,
               regime="STRONG_DOWNTREND")
    t.trailing_stop = t.current_stop = t.initial_stop = 170.0
    ok, why = _manager(t).scale_in_trade("T1", 1, 160.0)
    assert ok is False, f"added size to a losing bought PE ({why})"


def test_a_winning_bought_pe_still_qualifies(settings):
    """Premium 200 -> 240 on a PE is a real profit and must still be addable."""
    t = _trade(lots=2, direction="SHORT", entry_price=200.0, current_price=240.0,
               regime="STRONG_DOWNTREND")
    t.trailing_stop = t.current_stop = t.initial_stop = 170.0
    ok, why = _manager(t).scale_in_trade("T1", 1, 240.0)
    assert ok is True, f"refused an add on a winning bought PE ({why})"


# ── caps ─────────────────────────────────────────────────────────────────────

def test_the_add_cap_binds(settings):
    settings.scale_in_max_adds_per_trade = 1
    t = _trade(lots=2, entry_price=200.0)
    m = _manager(t)
    assert m.scale_in_trade("T1", 1, 240.0)[0] is True
    ok, why = m.scale_in_trade("T1", 1, 240.0)
    assert ok is False and "cap" in why


def test_the_total_lot_ceiling_binds(settings):
    settings.scale_in_max_total_lots = 3
    t = _trade(lots=2, entry_price=200.0)
    ok, why = _manager(t).scale_in_trade("T1", 2, 240.0)
    assert ok is False and "total lots" in why


def test_scale_in_is_off_by_default(monkeypatch):
    monkeypatch.setattr(cs, "get_settings", lambda: _Settings(scale_in_enabled=False))
    t = _trade()
    ok, why = _manager(t).scale_in_trade("T1", 1, 240.0)
    assert ok is False and "disabled" in why


# ── 4. no new slot, but real money ───────────────────────────────────────────

def test_an_add_faces_the_rupee_risk_budget(settings):
    settings.entry_risk_budget_enabled = True
    t = _trade(lots=2, entry_price=200.0)
    m = _manager(t)
    m._entry_risk_admission = lambda **kw: (False, "book risk Rs.90,000 over cap Rs.50,000")
    ok, why = m.scale_in_trade("T1", 1, 240.0)
    assert ok is False and "entry risk budget" in why


def test_the_admission_is_asked_about_the_ADDED_lots_only(settings):
    """Not the whole position: the parent's risk is already counted in the book."""
    t = _trade(lots=2, entry_price=200.0)
    m = _manager(t)
    seen = {}
    m._entry_risk_admission = lambda **kw: (seen.update(kw) or (True, ""))
    m.scale_in_trade("T1", 1, 240.0)
    assert seen["lots"] == 1


def test_an_add_does_not_consume_a_position_slot(settings):
    """It is the same idea, so open_trades must not grow."""
    t = _trade(lots=2)
    m = _manager(t)
    m.scale_in_trade("T1", 1, 240.0)
    assert list(m.open_trades) == ["T1"]


# ── refusals that protect the position ───────────────────────────────────────

@pytest.mark.parametrize("status", ["EXIT_PENDING", "PARTIAL_EXIT", "RECOVERY_REQUIRED"])
def test_no_add_while_an_exit_or_recovery_is_in_flight(settings, status):
    t = _trade()
    t.execution_status = status
    ok, why = _manager(t).scale_in_trade("T1", 1, 240.0)
    assert ok is False and "in flight" in why


def test_a_de_risked_trade_is_not_added_back_to(settings):
    """De-risking and scaling in are opposite decisions about the same trade."""
    t = _trade()
    t.partial_derisk_done = True
    ok, why = _manager(t).scale_in_trade("T1", 1, 240.0)
    assert ok is False and "de-risked" in why


def test_a_persisted_in_flight_add_blocks_a_second_one(settings):
    t = _trade()
    t.scale_in_order_id = "ADD-1"
    ok, why = _manager(t).scale_in_trade("T1", 1, 240.0)
    assert ok is False and "in flight" in why


def test_an_unknown_trade_is_refused(settings):
    assert _manager(_trade()).scale_in_trade("nope", 1, 240.0)[0] is False


# ── 1. the live path: add, confirm, THEN re-arm ──────────────────────────────

class _Broker:
    def __init__(self, order_id="ADD-1", exc=None):
        self.order_id = order_id
        self.exc = exc
        self.orders = []

    def place_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", **kw):
        self.orders.append((symbol, qty, side))
        if self.exc:
            raise self.exc
        return self.order_id


def _real_manager(trade, broker, confirmed=300):
    m = _manager(trade, mode="REAL", broker=broker)
    m._tag_kwargs = lambda func, t, leg, attempt=0: {}
    m._confirmed_scaled_quantity = lambda t, expected, timeout=4.0: confirmed
    m.protections = []
    m.ensure_broker_protection = lambda t, qty=None: (
        m.protections.append(qty) or True
    )
    return m


def test_the_live_add_buys_and_then_re_arms_the_stop_for_the_total(settings):
    t = _trade(lots=2, entry_price=200.0)
    broker = _Broker()
    m = _real_manager(t, broker, confirmed=300)
    m._run_scale_in(t, 2, 150, 240.0, "CASCADE_ADD")
    assert broker.orders == [("NIFTY2680524500CE", 150, "BUY")]
    assert m.protections == [300], "the stop was not re-armed for the enlarged position"
    assert t.lots == 4


def test_the_existing_stop_is_never_cancelled_first(settings):
    """A failed add must leave the position exactly as protected as it was."""
    t = _trade(lots=2)
    t.protective_order_id = "SLM-1"
    broker = _Broker(exc=BrokerOrderRejected("no funds", terminal=True))
    m = _real_manager(t, broker)
    m._run_scale_in(t, 2, 150, 240.0, "CASCADE_ADD")
    assert t.protective_order_id == "SLM-1"
    assert t.lots == 2, "booked lots that were never bought"
    assert m.protections == []


def test_an_ambiguous_add_books_nothing_and_re_arms_nothing(settings):
    """A stop sized for the OLD quantity on a possibly larger position is the safe
    direction; reconciliation reads the truth."""
    t = _trade(lots=2)
    m = _real_manager(t, _Broker(exc=BrokerOrderAmbiguous("read timed out")))
    m._run_scale_in(t, 2, 150, 240.0, "CASCADE_ADD")
    assert t.lots == 2
    assert m.protections == []
    assert t.execution_status == "RECOVERY_REQUIRED"


def test_an_unconfirmed_add_does_not_move_the_average_entry(settings):
    """Booking off an acknowledgement would reprice lots that may never have arrived."""
    t = _trade(lots=2, entry_price=200.0)
    m = _real_manager(t, _Broker(), confirmed=None)
    m._run_scale_in(t, 2, 150, 240.0, "CASCADE_ADD")
    assert t.entry_price == pytest.approx(200.0)
    assert t.lots == 2
    assert t.execution_status == "RECOVERY_REQUIRED"


def test_a_failed_re_arm_marks_recovery(settings):
    t = _trade(lots=2)
    m = _real_manager(t, _Broker(), confirmed=300)
    m.ensure_broker_protection = lambda t_, qty=None: False
    m._run_scale_in(t, 2, 150, 240.0, "CASCADE_ADD")
    assert t.execution_status == "RECOVERY_REQUIRED"


def test_the_add_is_tagged_as_its_own_leg(settings):
    """ENTRY and ADD must be distinguishable in the order book."""
    t = _trade(lots=2)
    broker = _Broker()
    m = _manager(t, mode="REAL", broker=broker)
    m._confirmed_scaled_quantity = lambda t_, e, timeout=4.0: 300
    m.ensure_broker_protection = lambda t_, qty=None: True
    legs = []
    m._tag_kwargs = lambda func, t_, leg, attempt=0: legs.append(leg) or {}
    m._run_scale_in(t, 2, 150, 240.0, "CASCADE_ADD")
    assert legs == ["ADD"]


# ── persistence ──────────────────────────────────────────────────────────────

def test_scale_in_state_survives_a_restart(settings):
    t = _trade(lots=2, entry_price=200.0)
    _manager(t).scale_in_trade("T1", 2, 240.0)
    restored = Trade.from_dict(t.to_dict())
    assert restored.lots == 4
    assert restored.entry_price == pytest.approx(220.0)
    assert restored.scale_in_count == 1
    assert restored.scale_in_lots == 2
    assert restored.frozen_initial_risk_rs == pytest.approx(4500.0)


def test_the_default_add_size_comes_from_the_setting(settings):
    """scale_in_lots_per_add is the size of one add; a caller may override it."""
    settings.scale_in_lots_per_add = 2
    t = _trade(lots=2, entry_price=200.0)
    ok, why = _manager(t).scale_in_trade("T1", price=240.0)
    assert ok, why
    assert t.lots == 4


def test_an_explicit_size_overrides_the_setting(settings):
    settings.scale_in_lots_per_add = 2
    t = _trade(lots=2, entry_price=200.0)
    _manager(t).scale_in_trade("T1", 1, 240.0)
    assert t.lots == 3
