"""Projected risk must use the TRAILED stop, not the one set at entry.

trade.trailing_stop is documented as "Initial trailing stop at entry" and is never
rewritten after open_trade — the ratchet writes current_stop. RiskManager read
trailing_stop, so every open trade was projected at its full ORIGINAL risk even after its
stop had trailed into profit.

That figure feeds projected_open_pnl = min(open_pnl, -(projected_stop_risk + buffer)),
the worst case charged against the daily loss budget. Over-stating it makes the budget
look exhausted and blocks new entries against risk that no longer exists — a system that
silently stops trading while its open positions are in fact safe.

These use OPT (options_sl_pct 15%) so the configured per-trade ceiling is generous enough
that the STOP DISTANCE is the binding term. On FUT the 0.3% ceiling dominates every
realistic stop distance, which would hide the very difference under test.
"""
from types import SimpleNamespace

import pytest

from engine.risk_manager import RiskManager

QTY = 10          # lots=1 * lot_size=10
ENTRY = 100.0     # OPT ceiling = 100 * 10 * 15% = 150.0


def _rm():
    rm = RiskManager.__new__(RiskManager)
    rm.futures_sl_pct = 0.3
    rm.options_sl_pct = 15.0
    rm.max_trade_loss_abs = 0.0
    return rm


def _trade(current_stop, trailing_stop, direction="LONG", entry=ENTRY, inst_type="OPT"):
    # quantity is derived as lots * lot_size inside the method, not read directly.
    return SimpleNamespace(
        entry_price=entry, lots=1, lot_size=QTY, instrument_multiplier=1.0,
        direction=direction, inst_type=inst_type,
        current_stop=current_stop, trailing_stop=trailing_stop,
    )


def test_a_stop_trailed_into_profit_projects_no_risk():
    """Entered at 100, stop now 105: the position cannot lose. Risk must be 0."""
    risk = _rm()._projected_stop_risk(_trade(current_stop=105.0, trailing_stop=95.0))
    assert risk == 0.0, (
        f"projected {risk} of risk on a position whose stop sits above entry — the "
        "entry stop was used instead of the trailed one"
    )


def test_a_stop_trailed_partway_projects_only_the_remaining_risk():
    """Entry 100, entry-stop 95 (=50 risk), trailed to 98 -> 20 remains."""
    risk = _rm()._projected_stop_risk(_trade(current_stop=98.0, trailing_stop=95.0))
    assert risk == pytest.approx(2.0 * QTY), f"expected the trailed distance, got {risk}"


def test_an_untrailed_trade_still_projects_its_entry_risk():
    """Nothing has moved, so the entry stop IS the current stop — behaviour unchanged."""
    risk = _rm()._projected_stop_risk(_trade(current_stop=95.0, trailing_stop=95.0))
    assert risk == pytest.approx(5.0 * QTY)


def test_a_short_stop_trailed_into_profit_projects_no_risk():
    """A genuine SHORT only exists in futures, so this case is FUT.

    It was inst_type="OPT" with direction="SHORT", which asserted the bug fixed on
    2026-08-08: for an option, direction="SHORT" means a bought PE, still LONG PREMIUM,
    and a stop below entry is a real loss rather than a locked profit. Half the option
    book projected zero risk because of exactly this reading.
    """
    risk = _rm()._projected_stop_risk(
        _trade(current_stop=95.0, trailing_stop=105.0, direction="SHORT", inst_type="FUT")
    )
    assert risk == 0.0


def test_a_bought_pe_projects_real_risk_despite_direction_short():
    """The fix. A PE is stored SHORT but its stop sits BELOW entry."""
    risk = _rm()._projected_stop_risk(
        _trade(current_stop=95.0, trailing_stop=95.0, direction="SHORT", inst_type="OPT")
    )
    assert risk == pytest.approx(5.0 * QTY), (
        "a bought PE projected no stop risk — the entire PE side of the book was "
        "invisible to the forward-looking half of the circuit breaker"
    )


def test_an_option_delta_is_not_a_contract_multiplier():
    """instrument_multiplier on an OPT row is the signed BS delta, not a lot multiplier."""
    t = _trade(current_stop=95.0, trailing_stop=95.0, direction="SHORT", inst_type="OPT")
    t.instrument_multiplier = -0.4687
    assert _rm()._projected_stop_risk(t) == pytest.approx(5.0 * QTY), (
        "|delta| scaled the rupee risk, understating it by about half"
    )


def test_a_missing_current_stop_falls_back_to_the_entry_stop():
    """Trades persisted before current_stop existed must still be projected."""
    risk = _rm()._projected_stop_risk(_trade(current_stop=0.0, trailing_stop=95.0))
    assert risk == pytest.approx(5.0 * QTY)


def test_risk_never_exceeds_the_configured_per_trade_ceiling():
    """A far-away stop is still capped by options_sl_pct."""
    risk = _rm()._projected_stop_risk(_trade(current_stop=1.0, trailing_stop=1.0))
    assert risk == pytest.approx(ENTRY * QTY * 0.15)


def test_the_absolute_loss_cap_still_binds():
    rm = _rm()
    rm.max_trade_loss_abs = 12.0
    risk = rm._projected_stop_risk(_trade(current_stop=95.0, trailing_stop=95.0))
    assert risk == pytest.approx(12.0)
