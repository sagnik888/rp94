"""Interim containment: one REAL position per tradingsymbol.

Kite reports POSITIONS per tradingsymbol, so when several REAL trades share a contract
the broker cannot say who owns what. _allocate_broker_quantity splits the row by a rule
-- confirmed fills first, then oldest-first by intended size -- and that rule is a GUESS
the moment one sibling exits and the remainder has to be re-attributed. broker_quantity
SIZES the protective stop, so a wrong guess arms the wrong quantity.

The real answer is a persisted per-order fill ledger. Until that exists, one REAL trade
per contract removes the need to guess at all.

Scope, deliberately narrow:
  * REAL mode only. Backtests and replays keep exercising the multi-trade paths,
    including the allocator's own tests.
  * One CONTRACT, not one instrument. Stacking across strikes or instruments is
    untouched, so cascade multi-timeframe entries still work wherever the strikes differ.
  * Recovery is exempt: it re-establishes a position that already exists, and refusing
    it would strand a live position rather than prevent a second one.

Lifts with forbid_concurrent_same_contract_real=False, which is how the ledger work
gets validated before this is retired.
"""
import threading
from types import SimpleNamespace

import pytest

from trading.trade_manager import TradeManager


SYMBOL = "NIFTY2681424300CE"


def _mgr(mode="REAL", trades=(), enabled=True, monkeypatch=None):
    mgr = TradeManager.__new__(TradeManager)
    mgr.mode = mode
    mgr._lock = threading.RLock()
    mgr.open_trades = {t.id: t for t in trades}
    return mgr


def _t(tid, symbol=SYMBOL, timeframe="5min", ghost=False):
    return SimpleNamespace(id=tid, trading_symbol=symbol, timeframe=timeframe, is_ghost=ghost)


# ── the containment ──────────────────────────────────────────────────────────

def test_a_second_real_trade_on_the_same_contract_is_refused():
    mgr = _mgr(trades=[_t("t-5min")])

    blocked, reason = mgr._same_contract_entry_blocked(SYMBOL)

    assert blocked is True
    assert "t-5min" in reason


def test_a_different_strike_is_allowed():
    """Cascade stacking across strikes is untouched."""
    mgr = _mgr(trades=[_t("t-5min", symbol="NIFTY2681424350CE")])

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is False


def test_a_different_instrument_is_allowed():
    mgr = _mgr(trades=[_t("t-bn", symbol="BANKNIFTY26AUG52000CE")])

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is False


def test_the_first_trade_on_a_contract_is_allowed():
    assert _mgr(trades=[])._same_contract_entry_blocked(SYMBOL)[0] is False


# ── scope limits ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["HISTORICAL", "", None, "PAPER"])
def test_non_real_modes_are_untouched(mode):
    """Backtests must keep exercising the multi-trade allocator paths."""
    mgr = _mgr(mode=mode, trades=[_t("t-5min")])

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is False


def test_recovery_is_exempt():
    """Recovery re-establishes an EXISTING position; refusing strands it."""
    mgr = _mgr(trades=[_t("t-5min")])

    assert mgr._same_contract_entry_blocked(SYMBOL, is_recovery=True)[0] is False


def test_a_ghost_entry_is_exempt():
    mgr = _mgr(trades=[_t("t-5min")])

    assert mgr._same_contract_entry_blocked(SYMBOL, is_ghost=True)[0] is False


def test_a_ghost_holder_does_not_block_a_real_entry():
    """A paper mirror is not a broker position."""
    mgr = _mgr(trades=[_t("t-ghost", ghost=True)])

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is False


def test_a_blank_symbol_is_not_matched():
    """FUT rows carry no tradingsymbol; they must not all collide on ''."""
    mgr = _mgr(trades=[_t("t-fut", symbol="")])

    assert mgr._same_contract_entry_blocked("")[0] is False


# ── the switch ───────────────────────────────────────────────────────────────

def test_it_can_be_lifted_for_ledger_validation(monkeypatch):
    """The guard resolves get_settings at call time from config.settings."""
    import config.settings as cs
    mgr = _mgr(trades=[_t("t-5min")])
    base = cs.get_settings()
    monkeypatch.setattr(
        cs, "get_settings",
        lambda: SimpleNamespace(**{**vars(base), "forbid_concurrent_same_contract_real": False}),
    )

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is False


def test_the_setting_exists_and_defaults_on():
    from config.settings import Settings

    field = Settings.model_fields["forbid_concurrent_same_contract_real"]
    assert field.default is True


# ── wired into the only entry funnel ─────────────────────────────────────────

def test_open_trade_consults_it_before_anything_reaches_the_broker():
    import inspect

    src = inspect.getsource(TradeManager.open_trade)
    i = src.index("_same_contract_entry_blocked(")

    assert i < src.index("_entry_mark_admission"), (
        "the contract check must run before sizing, logging or any broker call"
    )
