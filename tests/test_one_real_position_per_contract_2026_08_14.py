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

def _settings_with(monkeypatch, **over):
    import config.settings as cs
    base = cs.get_settings()
    monkeypatch.setattr(
        cs, "get_settings",
        lambda: SimpleNamespace(**{**vars(base), **over}),
    )


def test_lifting_the_master_switch_does_not_mean_unrestricted(monkeypatch):
    """The second lock. Off does not mean open.

    Stacking is only safe while the ledger can say who owns what on THIS contract, so
    it must prove that per contract rather than once, globally, by operator judgement.
    """
    mgr = _mgr(trades=[_t("t-5min")])
    _settings_with(monkeypatch, forbid_concurrent_same_contract_real=False)
    monkeypatch.setattr(TradeManager, "_ledger_owned_quantity", staticmethod(lambda _t: None))

    blocked, reason = mgr._same_contract_entry_blocked(SYMBOL)

    assert blocked is True
    assert "ledger cannot account" in reason


def test_a_ledger_accounted_contract_may_be_stacked(monkeypatch):
    """What step 5 unlocks: the ledger knows the existing trade, so adding is safe."""
    mgr = _mgr(trades=[_t("t-5min")])
    _settings_with(monkeypatch, forbid_concurrent_same_contract_real=False)
    monkeypatch.setattr(TradeManager, "_ledger_owned_quantity", staticmethod(lambda _t: 75))

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is False


def test_a_ledger_known_zero_still_counts_as_accounted(monkeypatch):
    """0 is a real answer -- the trade's own orders net to nothing. None is not."""
    mgr = _mgr(trades=[_t("t-5min")])
    _settings_with(monkeypatch, forbid_concurrent_same_contract_real=False)
    monkeypatch.setattr(TradeManager, "_ledger_owned_quantity", staticmethod(lambda _t: 0))

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is False


def test_one_unaccounted_sibling_blocks_the_whole_contract(monkeypatch):
    """Ownership is only knowable if EVERY trade on the contract is attributable."""
    mgr = _mgr(trades=[_t("known"), _t("unknown")])
    _settings_with(monkeypatch, forbid_concurrent_same_contract_real=False)
    monkeypatch.setattr(
        TradeManager, "_ledger_owned_quantity",
        staticmethod(lambda tid: 75 if tid == "known" else None),
    )

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is True


def test_both_locks_off_is_fully_unrestricted(monkeypatch):
    """A deliberate two-step escape hatch for a backtest or an emergency."""
    mgr = _mgr(trades=[_t("t-5min")])
    _settings_with(
        monkeypatch,
        forbid_concurrent_same_contract_real=False,
        same_contract_requires_ledger_evidence=False,
    )
    monkeypatch.setattr(TradeManager, "_ledger_owned_quantity", staticmethod(lambda _t: None))

    assert mgr._same_contract_entry_blocked(SYMBOL)[0] is False


def test_the_ledger_gate_defaults_on():
    from config.settings import Settings

    assert Settings.model_fields["same_contract_requires_ledger_evidence"].default is True


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
