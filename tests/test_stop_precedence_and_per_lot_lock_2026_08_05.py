"""Two exit-side gaps found by comparing against the ut-index-2 lineage.

1. AN ARMED EXCHANGE STOP OUTRANKS EVERY LOCAL POLICY.
   _should_defer_trend_option_profit_lock lets a strong trend-aligned option winner
   breathe through a pullback rather than exiting on the profit-lock level. But
   TradeManager.update_trade raises trade.current_stop and then immediately calls
   ensure_broker_protection, which pushes that same level to Kite as an SL-M and TIGHTENS
   it as the stop trails. So the exchange can already hold a trigger at the very level
   being deferred.

   Deferring it does not keep the position open — Kite fires the SL-M regardless. It only
   means the broker closes the position while this process goes on managing a trade that
   no longer exists: re-arming stops, holding a slot, and counting exposure against it.

2. THE RETENTION LADDER IS READ IN ABSOLUTE RUPEES.
   Its tiers were tuned on single-lot trades, so a 3-lot Rs.3000 peak — Rs.1000 per lot —
   lands on the MAJOR_WIN_GUARD rung and locks 90%. Per-lot is opt-in because it is
   LOOSER: the trade lands on a lower rung and may give back more.
"""
import pytest

from scanner import Scanner


class _Trade:
    def __init__(self, trigger=0.0, symbol="NIFTY2680524500CE", instrument="NIFTY",
                 timeframe="5min"):
        self.protective_trigger = trigger
        self.trading_symbol = symbol
        self.instrument = instrument
        self.timeframe = timeframe


class _Candidate:
    def __init__(self, direction="LONG", symbol="NIFTY2680524500CE", instrument="NIFTY",
                 timeframe="5min", lots=1, lot_size=65):
        self.direction = direction
        self.trading_symbol = symbol
        self.instrument = instrument
        self.timeframe = timeframe
        self.inst_type = "OPT"
        self.lots = lots
        self.lot_size = lot_size


class _Trades:
    def __init__(self, trades):
        self.open_trades = {str(i): t for i, t in enumerate(trades)}


def _scanner(trades):
    s = Scanner.__new__(Scanner)
    s.trades = _Trades(trades)
    s._is_premium_long_candidate = lambda c: str(getattr(c, "inst_type", "")) == "OPT"
    return s


# ── 1. armed-stop precedence ─────────────────────────────────────────────────

def test_a_crossed_armed_stop_is_detected():
    scanner = _scanner([_Trade(trigger=180.0)])
    assert scanner._armed_broker_stop_crossed(_Candidate(), current_price=179.0) is True


def test_an_uncrossed_armed_stop_is_not_flagged():
    scanner = _scanner([_Trade(trigger=180.0)])
    assert scanner._armed_broker_stop_crossed(_Candidate(), current_price=185.0) is False


def test_no_armed_stop_means_nothing_to_respect():
    """With no exchange-side trigger the local policy is free to defer."""
    scanner = _scanner([_Trade(trigger=0.0)])
    assert scanner._armed_broker_stop_crossed(_Candidate(), current_price=1.0) is False


def test_the_exact_contract_wins_over_the_index():
    """Two trades on one index at different strikes must not read each other's stop."""
    other = _Trade(trigger=999.0, symbol="NIFTY2680524600CE")
    mine = _Trade(trigger=180.0, symbol="NIFTY2680524500CE")
    scanner = _scanner([other, mine])
    assert scanner._armed_broker_stop_for(_Candidate()) == 180.0


def test_a_short_position_crosses_upward():
    scanner = _scanner([_Trade(trigger=180.0)])
    candidate = _Candidate(direction="SHORT")
    candidate.inst_type = "FUT"
    scanner._is_premium_long_candidate = lambda c: False
    assert scanner._armed_broker_stop_crossed(candidate, current_price=181.0) is True
    assert scanner._armed_broker_stop_crossed(candidate, current_price=179.0) is False


def test_the_deferral_refuses_once_the_exchange_stop_is_crossed():
    """The whole point: a level Kite will fire anyway must never be deferred."""
    import scanner as scanner_mod

    class _S:
        trend_option_profit_lock_defer = True
        trend_option_profit_lock_severe_retention = 0.50

    scanner = _scanner([_Trade(trigger=180.0)])
    scanner._setting_enabled = lambda v, d=True: bool(v)
    candidate = _Candidate()
    candidate.grade = "A+"

    original = scanner_mod.get_settings
    scanner_mod.get_settings = lambda: _S()
    try:
        deferred, reason = Scanner._should_defer_trend_option_profit_lock(
            scanner, candidate, 179.0, 180.0, 900.0, 1000.0, None,
        )
    finally:
        scanner_mod.get_settings = original

    assert deferred is False, (
        "deferred a stop already armed at the exchange — Kite fires it anyway and the "
        "position closes while this process keeps managing it"
    )
    assert "exchange stop" in reason


# ── 2. per-lot retention ─────────────────────────────────────────────────────

def _floor(peak, lots, per_lot):
    import scanner as scanner_mod

    class _S:
        profit_lock_per_lot_basis = per_lot
        option_major_win_trend_lock_ratio_cap = 0.95

    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda v, d=True: bool(v)
    s._profit_lock_min_peak = lambda *a, **k: 0.0
    trade = _Candidate(lots=lots)
    original = scanner_mod.get_settings
    scanner_mod.get_settings = lambda: _S()
    try:
        return Scanner._profit_lock_floor(
            s, entry_price=200.0, direction="LONG", qty=65 * lots, peak_pnl=peak,
            inst_mult=1.0, timeframe="5min", trade_like=trade,
        )
    finally:
        scanner_mod.get_settings = original


def test_absolute_basis_is_unchanged_by_default():
    """Default must not move live exit behaviour."""
    _floor_px, reason, ratio = _floor(peak=3000.0, lots=3, per_lot=False)
    assert reason == "MAJOR_WIN_GUARD"
    assert ratio == pytest.approx(0.80)   # raw 0.90 -> 20-25% breathing band ceiling


def test_per_lot_basis_moves_a_multi_lot_peak_down_a_rung():
    _floor_px, reason, ratio = _floor(peak=3000.0, lots=3, per_lot=True)
    assert reason == "SMART_PROFIT_LOCK", "Rs.1000/lot is not a major win"
    # The RUNG is what per-lot changes; the band then clamps both rungs to 0.75-0.80, so
    # the reason is the meaningful assertion here rather than the ratio.
    assert ratio == pytest.approx(0.75)


def test_a_single_lot_trade_is_identical_either_way():
    assert _floor(peak=3000.0, lots=1, per_lot=False) == _floor(peak=3000.0, lots=1, per_lot=True)


def test_the_price_floor_still_uses_the_real_peak_not_the_per_lot_value():
    """Dividing the FLOOR by the lot count would understate every stop.

    ladder_pnl selects the tier; peak_pnl must still drive locked_points.
    """
    floor_px, _reason, ratio = _floor(peak=3000.0, lots=3, per_lot=True)
    expected = 200.0 + (3000.0 * ratio) / (65 * 3)
    assert floor_px == pytest.approx(expected), (
        "the protective floor was computed from the per-lot figure instead of the "
        "actual peak, placing the stop far below where it belongs"
    )
