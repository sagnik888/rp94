"""Refuse the entry that would make the book unsafe, not just halt after it does.

RiskManager.check_circuit_breaker measures a book that already exists. This answers the
earlier question: if this candidate were accepted, would total risk AT ITS CONFIGURED
STOPS exceed the budget? At one or two positions the two nearly coincide, which is why
this is off by default — it matters once several lots run across several indices with
cascade scaling on, where the breaker's after-the-fact halt arrives too late.

Fail-CLOSED throughout, deliberately opposite to the margin preflight. Margin asks "can
the broker fund this", where a quiet endpoint must not halt trading. This asks "would
this overrun the loss budget", where an unanswerable question means the budget cannot be
proven safe.
"""
import threading
from datetime import datetime, timedelta

import pytest

from engine.entry_risk_policy import evaluate_entry_risk, projected_stop_risk_rs
from trading.trade_manager import IST, Trade, TradeManager


def _opt(entry=200.0, stop=170.0, lots=1, lot_size=65):
    return {"inst_type": "OPT", "price": entry, "current_stop": stop,
            "lots": lots, "lot_size": lot_size}


# ── the pure policy ──────────────────────────────────────────────────────────

def test_option_risk_is_premium_at_risk():
    """Rs.30 of premium across 65 units is Rs.1950, whichever way the signal points."""
    assert projected_stop_risk_rs(_opt()) == pytest.approx(1950.0)
    bearish = dict(_opt(), direction="SHORT")
    assert projected_stop_risk_rs(bearish) == pytest.approx(1950.0), (
        "a bearish view here is a BOUGHT put; treating it as a short inverts the risk"
    )


def test_a_stop_beyond_breakeven_carries_no_price_risk():
    assert projected_stop_risk_rs(_opt(entry=200.0, stop=260.0)) == pytest.approx(0.0)


def test_missing_entry_or_stop_is_unmeasurable_not_zero():
    assert projected_stop_risk_rs(_opt(entry=0.0)) is None
    assert projected_stop_risk_rs(_opt(stop=0.0)) is None


def test_futures_risk_respects_direction_and_multiplier():
    long_fut = {"inst_type": "FUT", "direction": "LONG", "price": 24000.0,
                "current_stop": 23900.0, "lots": 1, "lot_size": 65, "multiplier": 1.0}
    assert projected_stop_risk_rs(long_fut) == pytest.approx(6500.0)
    short_fut = dict(long_fut, direction="SHORT", current_stop=24100.0)
    assert projected_stop_risk_rs(short_fut) == pytest.approx(6500.0)


def _decide(candidate, positions, opt_limit=10_000.0, global_limit=25_000.0, **kw):
    return evaluate_entry_risk(
        candidate, positions, fut_limit_rs=20_000.0, opt_limit_rs=opt_limit,
        global_limit_rs=global_limit, **kw,
    )


def test_an_entry_within_budget_is_admitted():
    assert _decide(_opt(), []).allowed is True


def test_the_entry_that_would_breach_the_segment_budget_is_refused():
    """Five 1950 positions is 9750; a sixth crosses 10000 and must not open."""
    book = [_opt() for _ in range(5)]
    decision = _decide(_opt(), book)
    assert decision.allowed is False
    assert decision.projected_segment_risk_rs == pytest.approx(11_700.0)
    assert "OPT projected stop risk" in decision.reason


def test_realized_losses_shrink_the_remaining_budget():
    """Otherwise the last entry of a losing day gets the same room as the first."""
    assert _decide(_opt(), []).allowed is True
    tightened = _decide(_opt(), [], realized_loss_by_segment={"OPT": 9_000.0})
    assert tightened.allowed is False


def test_the_global_budget_binds_across_segments():
    fut = {"inst_type": "FUT", "direction": "LONG", "price": 24000.0,
           "current_stop": 23700.0, "lots": 1, "lot_size": 65, "multiplier": 1.0}
    decision = _decide(_opt(), [fut], global_limit=15_000.0)
    assert decision.allowed is False
    assert "global projected stop risk" in decision.reason


def test_an_unmeasurable_open_position_refuses_the_next_entry():
    """One unknown position makes the whole book's risk unknown."""
    decision = _decide(_opt(), [_opt(stop=0.0)])
    assert decision.allowed is False
    assert "unavailable" in decision.reason


def test_an_unmeasurable_candidate_is_refused():
    assert _decide(_opt(entry=0.0), []).allowed is False


def test_a_missing_limit_refuses_rather_than_admitting_everything():
    """A zero budget must not read as 'unlimited'."""
    assert _decide(_opt(), [], opt_limit=0.0).allowed is False
    assert _decide(_opt(), [], global_limit=0.0).allowed is False


def test_the_per_trade_absolute_cap_is_honoured():
    assert _decide(_opt(), [], max_trade_loss_abs=1_000.0).allowed is False
    assert _decide(_opt(), [], max_trade_loss_abs=5_000.0).allowed is True


# ── wiring into the entry path ───────────────────────────────────────────────

def _manager():
    m = TradeManager.__new__(TradeManager)
    m.broker = None
    m.open_trades = {}
    m.closed_trades = []
    m._lock = threading.RLock()
    m._log_ui = lambda *a, **k: None
    return m


def _admit(enabled, positions=(), is_recovery=False, closed=()):
    import config.settings as cs

    class _S:
        entry_risk_budget_enabled = enabled
        capital_fut = 900_000.0
        capital_opt = 100_000.0
        capital_total = 1_000_000.0
        risk_fut_pct = 2.0
        risk_opt_pct = 1.0          # -> Rs.1000 OPT budget, so one lot breaches
        max_daily_loss_pct = 10.0
        max_trade_loss_abs = 0.0
        slippage_fut_bps = 0.0

    manager = _manager()
    for i, p in enumerate(positions):
        manager.open_trades[str(i)] = p
    manager.closed_trades = list(closed)
    original = cs.get_settings
    cs.get_settings = lambda: _S()
    try:
        return manager._entry_risk_admission(
            inst_type="OPT", direction="LONG", price=200.0, trailing_stop=170.0,
            initial_stop=170.0, lots=1, lot_size=65, instrument_multiplier=1.0,
            is_recovery=is_recovery,
        )
    finally:
        cs.get_settings = original


def test_disabled_by_default_admits_everything():
    allowed, _reason = _admit(enabled=False)
    assert allowed is True


def test_enabled_it_refuses_an_entry_over_budget():
    allowed, reason = _admit(enabled=True)
    assert allowed is False
    assert "OPT projected stop risk" in reason


def test_recovery_entries_bypass_the_budget():
    """Refusing a recovery strands the very exposure this is meant to bound."""
    allowed, _reason = _admit(enabled=True, is_recovery=True)
    assert allowed is True


def test_todays_realised_losses_are_counted_but_not_yesterdays():
    def _closed(pnl, days_ago):
        t = Trade(
            id=f"C{days_ago}", instrument="NIFTY", timeframe="5min", direction="LONG",
            entry_price=200.0, entry_time=datetime.now(IST) - timedelta(days=days_ago),
            trailing_stop=170.0, current_stop=170.0, lots=1, lot_size=65, grade="A",
            inst_type="OPT",
        )
        t.pnl = pnl
        t.exit_time = datetime.now(IST) - timedelta(days=days_ago)
        return t

    manager = _manager()
    manager.closed_trades = [_closed(-500.0, 0), _closed(-9_000.0, 3), _closed(400.0, 0)]
    losses = manager._realized_loss_by_segment()
    assert losses["OPT"] == pytest.approx(500.0), (
        "a stale or winning trade leaked into today's realised-loss figure"
    )


def test_open_trade_actually_refuses_when_the_budget_is_breached():
    """Exercises the WIRING, not just the helper.

    The first version of this file only called _entry_risk_admission directly, so
    deleting the check from open_trade left every test green — the policy was correct
    and connected to nothing, which is the exact failure mode this codebase keeps
    producing.
    """
    import config.settings as cs

    class _S:
        entry_risk_budget_enabled = True
        capital_fut = 900_000.0
        capital_opt = 100_000.0
        capital_total = 1_000_000.0
        risk_fut_pct = 2.0
        risk_opt_pct = 1.0          # Rs.1000 OPT budget; one 65-lot at Rs.30 risk breaches
        max_daily_loss_pct = 10.0
        max_trade_loss_abs = 0.0
        slippage_fut_bps = 0.0

    manager = _manager()
    manager.mode = "HISTORICAL"
    manager.can_open_trade = lambda is_recovery=False: (True, "")

    original = cs.get_settings
    cs.get_settings = lambda: _S()
    try:
        result = manager.open_trade(
            instrument="NIFTY", timeframe="5min", direction="LONG", price=200.0,
            trailing_stop=170.0, lots=1, lot_size=65, grade="A", inst_type="OPT",
        )
    finally:
        cs.get_settings = original

    assert result is None, "open_trade admitted an entry that breaches the risk budget"
    assert manager.open_trades == {}, "a refused entry still created a row"
