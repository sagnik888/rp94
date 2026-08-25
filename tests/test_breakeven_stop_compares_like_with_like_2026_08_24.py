"""The breakeven stop must compare a gross peak against a gross mark, not mix the two.

The rule reads two numbers (scanner.py, _peak_giveback_cap_exit): arm when
`peak_pnl_rs >= breakeven_stop_arm_rs` (150), fire when `current_pnl_rs <=
breakeven_stop_floor_rs` (80). The two lanes fed it different units.

    session lane (scanner.py:22318)
        current_pnl_rs = _candidate_gross_pnl(...)                     GROSS
        peak_pnl_rs    = max(peak_pnl, current_pnl_rs, candle_extreme) GROSS   consistent

    live lane (scanner.py:28886)
        current_pnl_rs = (current_price - entry) * qty                 GROSS
        peak_pnl_rs    = trade.peak_pnl                                NET     mismatched

`trade.peak_pnl` tracks `trade.pnl`, and `trade.pnl = trade.unrealized_pnl`
(trade_manager.py:6112-6115), whose own docstring is "Net P&L including estimated
charges" — it subtracts estimate_trade_charges, Rs.117-145 on these contracts.

Measured on a real Trade (BANKNIFTY 1x35 @ 200 -> 210): gross peak 350.0,
trade.peak_pnl 226.9, difference 123.1. So a rule configured to arm at Rs.150 does
not arm in the live lane until roughly Rs.273 GROSS — it under-protects by ~82% —
while still firing on the gross floor of 80.

GROSS is the correct unit for both, not net: the floor is 80 and opt_cost is 80, so
"gross has fallen back to the round trip" IS the breakeven statement, and every
calibration of this rule was replayed over pnl_path, which is gross.

NOT changed here, deliberately: the floor value itself. Replaying 87 recorded paths
at floors 0/80/100/130/145/160/200/250 gives totals 5,282 / 5,800 / 3,594 / 4,090 /
4,333 / 4,985 / 4,061 / 3,883 — Rs.80 is the best of them, and raising it to "clear
the round trip" costs Rs.1,200-2,200 and takes the worst single clip from -Rs.306 to
-Rs.1,886. It fires late and books small losses because it is a polled threshold, not
a resting order; raising the floor to cure that clips winners instead, which is worse.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytz

from config.settings import get_settings
from engine.trade_accounting import estimate_trade_charges
from scanner import Scanner
from trading.trade_manager import Trade

IST = pytz.timezone("Asia/Kolkata")


def _trade(entry=200.0, qty=35):
    t = Trade(
        id="T-UNITS", instrument="BANKNIFTY", timeframe="5min", direction="LONG",
        entry_price=entry, entry_time=datetime.now(IST), trailing_stop=entry * 0.9,
        current_stop=entry * 0.9, lots=1, lot_size=qty, grade="A",
    )
    t.inst_type = "OPT"; t.option_type = "CE"; t.status = "OPEN"
    t.broker_quantity = qty; t.instrument_multiplier = 1.0
    t.trading_symbol = "BANKNIFTY26AUG57000CE"
    return t


def test_peak_pnl_is_net_and_therefore_not_comparable_to_a_gross_mark():
    """Pins the premise. If this ever flips, the fix below must be revisited."""
    t = _trade()
    t.current_price = 210.0
    gross = (210.0 - 200.0) * t.pnl_quantity
    # Against the SINGLETON, because that is what unrealized_pnl itself reads. The
    # magnitude is asserted separately below; here the claim is only that the property
    # is gross MINUS charges, whatever those charges currently are.
    charges = estimate_trade_charges(
        200.0, 210.0, t.pnl_quantity, "OPT", get_settings(), 1.0, t.instrument
    )

    assert t.unrealized_pnl == pytest.approx(gross - charges, abs=0.01)
    assert charges > 0.0


def test_the_gap_between_the_units_is_three_figures_in_this_build():
    """Why the mismatch mattered rather than being a rounding detail.

    On a FRESH Settings, never the singleton: other tests legitimately zero the
    slippage component while exercising the charge model, and a suite-order-dependent
    magnitude assertion reads whatever they left behind (observed: 80.0, the flat
    opt_cost alone, with slippage stripped).
    """
    from config.settings import Settings

    t = _trade()
    charges = estimate_trade_charges(
        200.0, 210.0, t.pnl_quantity, "OPT", Settings(), 1.0, t.instrument
    )

    assert charges > 100.0, "these contracts carry a three-figure round trip"


def test_the_live_lane_tracks_a_gross_peak():
    """The scanner must keep its own gross high-water mark, as the session lane does."""
    sc = Scanner.__new__(Scanner)
    t = _trade()

    assert sc._breakeven_peak_gross(t, 210.0) == 210.0     # first mark
    assert sc._breakeven_peak_gross(t, 350.0) == 350.0     # climbs
    assert sc._breakeven_peak_gross(t, 70.0) == 350.0      # never falls
    assert getattr(t, "peak_gross_pnl") == 350.0           # written back onto the trade


def test_a_negative_mark_does_not_seed_the_peak():
    sc = Scanner.__new__(Scanner)
    t = _trade()

    assert sc._breakeven_peak_gross(t, -400.0) == 0.0


def test_the_arm_now_triggers_where_it_is_configured_to():
    """Rs.150 of gross profit must arm a rule whose arm is Rs.150."""
    s = get_settings()
    sc = Scanner.__new__(Scanner)
    tr = SimpleNamespace(instrument="BANKNIFTY", inst_type="OPT", timeframe="5min")
    arm = float(s.breakeven_stop_arm_rs)
    floor = float(s.breakeven_stop_floor_rs)

    # Peak just over the arm, mark pulled back under the floor -> must fire.
    fired, why = Scanner._peak_giveback_cap_exit(
        sc, tr, current_pnl_rs=floor - 10.0, peak_pnl_rs=arm + 1.0
    )
    assert fired is True and "may not close negative" in why

    # The same trade as the LIVE LANE used to see it: the peak arrives already netted
    # of a ~Rs.123 round trip, so it sits under the arm and nothing fires.
    fired_old, _ = Scanner._peak_giveback_cap_exit(
        sc, tr, current_pnl_rs=floor - 10.0, peak_pnl_rs=arm + 1.0 - 123.0
    )
    assert fired_old is False, "this is the bug: the netted peak never reached the arm"


def test_the_live_call_site_passes_the_gross_peak():
    """The helper is worthless if the live lane still hands over trade.peak_pnl."""
    import inspect

    src = inspect.getsource(Scanner._update_active_trades)
    call = src.find("_peak_giveback_cap_exit(")
    assert call != -1, "the live call site moved; re-point this test"
    window = src[call:call + 300]
    assert "_breakeven_peak_gross" in window, "live lane still passes a netted peak"
    assert "trade.peak_pnl" not in window


def test_the_floor_is_left_where_the_replay_puts_it():
    """Rs.80 beat every alternative over 87 recorded paths; do not 'fix' it upward."""
    s = get_settings()

    assert float(s.breakeven_stop_floor_rs) == 80.0
    assert float(s.breakeven_stop_floor_rs) == float(s.opt_cost)
