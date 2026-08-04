"""Hold a marginal profit-taking exit while the bid is eating the edge — and nothing else.

"spread" appeared twice in the whole 24k-line scanner; the concept was absent. On an index
option the quoted spread is routinely wider than the profit a marginal lock is trying to
capture, so exiting at the bid turns a small win into a small loss.

This DELAYS an exit, which is a risk in itself, so the bypasses are the feature:

  * every protective, loss, forced, expiry, broker or manual exit leaves immediately;
  * a losing position is never held for a better price;
  * an exchange stop already armed AND crossed outranks it entirely — Kite fires that
    SL-M regardless, so delaying locally would only desynchronise the book;
  * the hold is bounded, or it becomes an open-ended refusal to leave.
"""
import time

import pytest

from scanner import Scanner


class _Trade:
    def __init__(self, entry=200.0, qty=65, inst="OPT", ghost=False):
        self.entry_price = entry
        self.broker_quantity = qty
        self.lots = 1
        self.lot_size = 65
        self.inst_type = inst
        self.is_ghost = ghost
        self.instrument = "NIFTY"
        self.trading_symbol = "NIFTY2680524500CE"
        self.symbol_token = "1"
        self.direction = "LONG"
        self.id = "TSP1"


class _Data:
    def __init__(self, quote):
        self._quote = quote

    def get_contract_touch_quote(self, exchange, symbol, token, max_age_seconds=1.5):
        return self._quote


class _S:
    spread_aware_exit_enabled = True
    spread_exit_deferral_min_spread_pct = 0.35
    spread_exit_min_net_profit_rs = 250.0
    spread_exit_max_cost_to_profit_ratio = 0.35
    spread_exit_deferral_max_seconds = 8.0
    execution_touch_quote_max_age_seconds = 1.5


def _scanner(quote, armed_crossed=False, settings=None):
    s = Scanner.__new__(Scanner)
    s.mode = "REAL"
    s.data = _Data(quote)
    s._setting_enabled = lambda v, d=True: bool(v)
    s._armed_broker_stop_crossed = lambda t, p: armed_crossed
    s._settings_obj = settings or _S()
    return s


def _decide(scanner, trade, price, reason, settings=None):
    import scanner as scanner_mod

    original = scanner_mod.get_settings
    scanner_mod.get_settings = lambda: (settings or _S())
    try:
        return Scanner._spread_aware_exit_decision(scanner, trade, price, reason)
    finally:
        scanner_mod.get_settings = original


WIDE = {"usable": True, "bid": 205.0, "ask": 215.0}     # ~4.8% spread


# ── it does defer the case it exists for ─────────────────────────────────────

def test_a_marginal_profit_exit_into_a_wide_book_is_held():
    deferred, context = _decide(_scanner(WIDE), _Trade(), 210.0, "SMART_PROFIT_LOCK")
    assert deferred is True
    assert "spread=" in context


# ── every bypass ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", [
    "TRAILING_STOP", "SL_HIT", "EARLY_LOSS_CUT", "LOSS_THREAT_EXIT", "RUPEE_LOSS",
    "FORCE_EOD_KILL", "SESSION_END", "EXPIRY_SETTLED", "REPAINT_ABORT",
    "MARKET_LTP_SHOCK", "BROKER_ERROR", "MANUAL", "RECOVERY_AUTO_FLATTEN",
    "SIGNAL_INVALIDATED",
])
def test_no_protective_exit_is_ever_delayed(reason):
    deferred, context = _decide(_scanner(WIDE), _Trade(), 210.0, reason)
    assert deferred is False, f"{reason} was delayed for spread"
    assert context == "hard_exit"


def test_an_unlisted_reason_is_not_delayed():
    """Allow-list, not deny-list: an unknown reason exits immediately."""
    deferred, _ = _decide(_scanner(WIDE), _Trade(), 210.0, "SOMETHING_NEW")
    assert deferred is False


def test_a_losing_position_is_never_held_for_a_better_price():
    deferred, context = _decide(_scanner(WIDE), _Trade(), 190.0, "SMART_PROFIT_LOCK")
    assert deferred is False and context == "non_profitable"


def test_an_armed_and_crossed_exchange_stop_outranks_the_deferral():
    """Kite fires that SL-M regardless; delaying only desynchronises the book."""
    scanner = _scanner(WIDE, armed_crossed=True)
    deferred, context = _decide(scanner, _Trade(), 210.0, "SMART_PROFIT_LOCK")
    assert deferred is False and context == "exchange_stop_armed"


def test_a_tight_book_exits_immediately():
    tight = {"usable": True, "bid": 209.9, "ask": 210.1}
    deferred, context = _decide(_scanner(tight), _Trade(), 210.0, "SMART_PROFIT_LOCK")
    assert deferred is False and context == "spread_tight"


def test_an_unusable_quote_exits_rather_than_waiting_blind():
    bad = {"usable": False, "reason": "stale quote"}
    deferred, _ = _decide(_scanner(bad), _Trade(), 210.0, "SMART_PROFIT_LOCK")
    assert deferred is False


def test_a_crossed_book_is_refused():
    crossed = {"usable": True, "bid": 215.0, "ask": 205.0}
    deferred, context = _decide(_scanner(crossed), _Trade(), 210.0, "SMART_PROFIT_LOCK")
    assert deferred is False and context == "invalid_book"


def test_a_healthy_edge_that_survives_the_spread_exits_now():
    """Wide book, but the bid still clears the profit floor comfortably."""
    rich = {"usable": True, "bid": 320.0, "ask": 340.0}
    deferred, context = _decide(_scanner(rich), _Trade(), 330.0, "SMART_PROFIT_LOCK")
    assert deferred is False and context == "edge_survives_spread"


def test_futures_and_ghosts_are_out_of_scope():
    assert _decide(_scanner(WIDE), _Trade(inst="FUT"), 210.0, "SMART_PROFIT_LOCK")[0] is False
    assert _decide(_scanner(WIDE), _Trade(ghost=True), 210.0, "SMART_PROFIT_LOCK")[0] is False


def test_simulation_mode_is_out_of_scope():
    scanner = _scanner(WIDE)
    scanner.mode = "HISTORICAL"
    assert _decide(scanner, _Trade(), 210.0, "SMART_PROFIT_LOCK")[0] is False


def test_disabled_by_default():
    class _Off(_S):
        spread_aware_exit_enabled = False

    deferred, context = _decide(_scanner(WIDE), _Trade(), 210.0, "SMART_PROFIT_LOCK",
                                settings=_Off())
    assert deferred is False and context == "not_applicable"


# ── the hold is bounded ──────────────────────────────────────────────────────

def test_the_hold_releases_after_the_ceiling():
    """Unbounded, this becomes an open-ended refusal to leave."""
    scanner = _scanner(WIDE)
    trade = _Trade()
    assert _decide(scanner, trade, 210.0, "SMART_PROFIT_LOCK")[0] is True
    # Rewind the start so the ceiling has elapsed.
    trade._spread_exit_defer_started = time.monotonic() - 99.0
    deferred, context = _decide(scanner, trade, 210.0, "SMART_PROFIT_LOCK")
    assert deferred is False
    assert "timeout_release" in context


def test_a_tightening_book_clears_the_timer():
    """Otherwise a later wide quote inherits an old deadline and releases at once."""
    scanner = _scanner(WIDE)
    trade = _Trade()
    _decide(scanner, trade, 210.0, "SMART_PROFIT_LOCK")
    assert float(getattr(trade, "_spread_exit_defer_started", 0.0)) > 0

    tight = _scanner({"usable": True, "bid": 209.9, "ask": 210.1})
    _decide(tight, trade, 210.0, "SMART_PROFIT_LOCK")
    assert float(getattr(trade, "_spread_exit_defer_started", 0.0)) == 0.0


# ── the wiring, not just the decision ────────────────────────────────────────

def _close_and_record_scanner(quote, reason_holder):
    """Minimal scanner that can run _close_and_record far enough to observe the hold."""
    s = Scanner.__new__(Scanner)
    s.mode = "REAL"
    s.data = _Data(quote)
    s._setting_enabled = lambda v, d=True: bool(v)
    s._armed_broker_stop_crossed = lambda t, p: False
    s._reset_daily_counters = lambda: None
    s.log_event = lambda *a, **k: None
    # _close_and_record has a manual-exit gate ahead of the internal close; auto_mode
    # short-circuits it so this test observes the spread decision rather than that gate.
    s.auto_mode = True

    class _Trades:
        open_trades = {}

    s.trades = _Trades()
    s._close_and_record_internal = lambda *a, **k: reason_holder.append(a)
    return s


def test_close_and_record_actually_holds_the_exit():
    """Exercises the WIRING.

    The decision function being correct proves nothing if _close_and_record never calls
    it — which is exactly what the first version of this file failed to catch.
    """
    import scanner as scanner_mod

    proceeded = []
    scanner = _close_and_record_scanner(WIDE, proceeded)
    trade = _Trade()
    scanner.trades.open_trades["TSP1"] = trade

    original = scanner_mod.get_settings
    scanner_mod.get_settings = lambda: _S()
    try:
        scanner._close_and_record("TSP1", 210.0, "SMART_PROFIT_LOCK")
    finally:
        scanner_mod.get_settings = original

    assert proceeded == [], "the exit went through despite a marginal edge and a wide book"


def test_close_and_record_does_not_hold_a_protective_exit():
    import scanner as scanner_mod

    proceeded = []
    scanner = _close_and_record_scanner(WIDE, proceeded)
    trade = _Trade()
    scanner.trades.open_trades["TSP1"] = trade

    original = scanner_mod.get_settings
    scanner_mod.get_settings = lambda: _S()
    try:
        scanner._close_and_record("TSP1", 210.0, "TRAILING_STOP")
    finally:
        scanner_mod.get_settings = original

    assert proceeded, "a protective exit was delayed by the spread check"
