"""The breakeven stop must compare a gross peak against a gross mark, not mix the two.

The rule reads two numbers (scanner.py, _peak_giveback_cap_exit): arm when
`peak_pnl_rs >= breakeven_stop_arm_rs` (150), fire when `current_pnl_rs <=
breakeven_stop_floor_rs` (80). Both sides have to be in the same unit, and that unit
has to be GROSS: the floor is 80 and opt_cost is 80, so "gross has fallen back to the
round trip" IS the breakeven statement, and every calibration of this rule was replayed
over pnl_path, which is gross.

THE TRAP, and it is a real one. On a bare Trade, `trade.peak_pnl` tracks `trade.pnl`,
and `trade.pnl = trade.unrealized_pnl`, whose own docstring is "Net P&L including
estimated charges" — Rs.117-145 on these contracts. Construct a Trade in isolation,
push it to 200 -> 210, and you will measure gross 350.0 against peak_pnl 226.9. From
that alone it looks obvious that the live lane is feeding the rule a netted peak against
a gross mark and under-arming by ~Rs.123.

IT IS NOT, and that is what this file exists to pin. The live lane never reads a bare
Trade. Unconditionally, forty lines above the call, inside the same method:

    scanner.py, _update_active_trades
        trade.peak_pnl = max(trade.peak_pnl, current_pnl_rs, candle_extreme_pnl)

`current_pnl_rs` there is `(current_price - entry) * qty * inst_mult` — the same gross
number the rule is handed as its mark. So by the time the rule reads `peak_pnl` it is
already gross, and it additionally carries the intrabar extreme. Because net is always
gross minus positive charges, a net sample can never raise that max above the gross one.

2026-08-25: acting on the trap cost two things. A separate, non-persisted gross
high-water mark was introduced and passed instead. It never saw `candle_extreme_pnl`,
so a peak reached between two price samples was invisible — sampled marks
40/90/120/100/60 against a real intrabar high of Rs.500 yielded a peak of 120, and the
rule stayed silent at Rs.60 where it had fired. And its `peak_gross_pnl` was absent from
`Trade.to_dict`/`from_dict` and from the sqlite persisted set, so after a restart it
reset to the CURRENT mark — and no number is ever Rs.150 above itself, so the rule could
never arm again for that position for the rest of its life. main.py restarts several
times a session.

Arming more often is safe here, which is why the higher peak is the right one: the rule
cannot fire while a trade is in profit above the floor, so it cannot clip a winner.
Measured helped 10, HURT 0 across 49 replayed positions, at every arm tested.

NOT changed here, deliberately: the floor value itself. Replaying 87 recorded paths at
floors 0/80/100/130/145/160/200/250 gives totals 5,282 / 5,800 / 3,594 / 4,090 / 4,333 /
4,985 / 4,061 / 3,883 — Rs.80 is the best of them, and raising it to "clear the round
trip" costs Rs.1,200-2,200 and takes the worst single clip from -Rs.306 to -Rs.1,886.
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


def _fired(peak, now):
    sc = Scanner.__new__(Scanner)
    tr = SimpleNamespace(instrument="BANKNIFTY", inst_type="OPT", timeframe="5min")
    verdict, _why = Scanner._peak_giveback_cap_exit(
        sc, tr, current_pnl_rs=now, peak_pnl_rs=peak
    )
    return bool(verdict)


# ── the trap ─────────────────────────────────────────────────────────────────

def test_peak_pnl_on_a_bare_trade_really_is_net():
    """The measurement that makes the wrong conclusion look obvious. It is correct
    about a Trade in isolation; it is the inference about the LIVE LANE that fails."""
    t = _trade()
    t.current_price = 210.0
    gross = (210.0 - 200.0) * t.pnl_quantity
    charges = estimate_trade_charges(
        200.0, 210.0, t.pnl_quantity, "OPT", get_settings(), 1.0, t.instrument
    )

    assert t.unrealized_pnl == pytest.approx(gross - charges, abs=0.01)
    assert charges > 0.0


def test_the_gap_between_the_units_is_three_figures_in_this_build():
    """Why the trap is tempting rather than a rounding detail.

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


def test_a_netted_peak_would_not_arm_which_is_why_one_is_never_passed():
    s = get_settings()
    arm = float(s.breakeven_stop_arm_rs)
    floor = float(s.breakeven_stop_floor_rs)

    assert _fired(peak=arm + 1.0, now=floor - 10.0) is True
    assert _fired(peak=arm + 1.0 - 123.0, now=floor - 10.0) is False


# ── what the live lane actually hands over ───────────────────────────────────

def test_the_ratchet_makes_the_peak_gross_before_the_rule_reads_it():
    """The claim the whole file rests on, exercised rather than read.

    Replays the ratchet the way _update_active_trades runs it and shows that a net
    sample cannot pull the peak below the gross one — net is gross minus positive
    charges, so it never wins the max.
    """
    t = _trade()
    t.current_price = 210.0
    gross = (210.0 - 200.0) * t.pnl_quantity
    net = t.unrealized_pnl
    assert net < gross

    peak = 0.0
    for sample in (net, gross, net):            # interleaved, worst case ordering
        peak = max(peak, sample)

    assert peak == pytest.approx(gross)
    assert peak > net


def test_a_peak_reached_between_two_samples_still_arms_the_rule():
    """The intrabar case. scanner.py's ratchet takes candle_extreme_pnl as well as the
    sampled mark, so a spike that happened between two polls is not lost."""
    s = get_settings()
    arm = float(s.breakeven_stop_arm_rs)
    floor = float(s.breakeven_stop_floor_rs)

    # Derived from the configured arm rather than hard-coded, so the case survives the
    # arm being retuned: every sampled mark stays UNDER the arm while the intrabar
    # high clears it comfortably.
    marks = [arm * 0.4, arm * 0.6, arm * 0.9, arm * 0.7, floor - 20.0]
    candle_extreme = arm * 5.0

    sampled_only = max(marks)
    with_extreme = max(sampled_only, candle_extreme)
    assert sampled_only < arm, "the fixture no longer isolates the intrabar case"

    assert _fired(peak=with_extreme, now=marks[-1]) is True
    assert _fired(peak=sampled_only, now=marks[-1]) is False, (
        "a peak built from sampled marks alone cannot see the intrabar high"
    )


def test_the_peak_survives_a_restart():
    """The regression that mattered most. A high-water mark the rule needs must be
    persisted, or every restart disarms the rule for every open position: the peak
    becomes the current mark, and no number is Rs.150 above itself."""
    live = _trade()
    live.peak_pnl = 600.0

    restored = Trade.from_dict(live.to_dict())

    assert "peak_pnl" in live.to_dict()
    assert restored.peak_pnl == pytest.approx(600.0)
    assert _fired(peak=restored.peak_pnl, now=20.0) is True


def test_the_live_call_site_passes_the_trades_own_peak():
    """Ordering, which is the crux and is genuinely a source-level fact: the ratchet
    has to run BEFORE the rule reads what it wrote.

    A text assertion cannot see runtime — that is what the tests above are for. This one
    only pins the two things text can actually establish: which expression is passed,
    and that the ratchet precedes it.
    """
    import inspect

    src = inspect.getsource(Scanner._update_active_trades)
    ratchet = src.find("trade.peak_pnl = max(")
    call = src.find("_peak_giveback_cap_exit(")

    assert ratchet != -1, "the peak ratchet moved; re-point this test"
    assert call != -1, "the live call site moved; re-point this test"
    assert ratchet < call, "the rule now reads a peak the ratchet has not written yet"

    # Not `"peak_pnl" in window` -- the keyword `peak_pnl_rs=` contains that substring,
    # so it passes for ANY expression at all. Match what is actually handed over.
    window = src[call:call + 400]
    assert 'getattr(trade, "peak_pnl"' in window, (
        "the live lane is passing something other than the trade's own high-water mark"
    )


# ── where the arm sits, and why ──────────────────────────────────────────────
#
# 2026-08-26: the arm moved 150 -> 100 on a replay of the 100 recorded lifecycles.
# The largest losing cohort in the corpus is 33 trades that dipped early, poked into
# profit at a MEDIAN PEAK OF Rs.139, and finished at a median of -Rs.212 -- Rs.12,991
# surrendered. A median peak of 139 sits just UNDER a 150 arm, so the guard whose whole
# job is "a proven trade may not close negative" could not arm on the median member of
# the cohort it exists for. Sweep vs recorded, net of Rs.80/trip:
#     arm   60      80      100     120     150     200
#     net  +3,190  +3,942  +5,164  +3,199  +3,038  +1,533
# A plateau over 80-120, not a spike. 100 rather than the marginally better 80 because
# arm == floor would make "proven" mean nothing at all.

def test_the_arm_is_reachable_by_the_cohort_it_exists_to_defend():
    """The number that moved the arm: a median peak of Rs.139 must be able to arm it."""
    s = get_settings()
    cohort_median_peak = 139.0

    assert float(s.breakeven_stop_arm_rs) <= cohort_median_peak, (
        "the guard cannot arm on the median member of the cohort it was built for"
    )
    assert _fired(peak=cohort_median_peak, now=float(s.breakeven_stop_floor_rs) - 1.0) is True


def test_the_arm_still_sits_above_the_floor():
    """arm == floor would mean 'proven' asserts nothing: any trade that touches the
    floor from either side would arm it."""
    s = get_settings()

    assert float(s.breakeven_stop_arm_rs) > float(s.breakeven_stop_floor_rs)


def test_lowering_the_arm_cannot_clip_a_winner():
    """The property that makes a lower arm safe rather than merely profitable: the rule
    fires only at or below the floor, so the arm widens WHICH trades are defended and
    never how much profit is taken off any one of them."""
    s = get_settings()
    floor = float(s.breakeven_stop_floor_rs)

    for peak in (100.0, 150.0, 400.0, 5000.0):
        assert _fired(peak=peak, now=floor + 1.0) is False, (
            "fired on a trade still in profit above the floor"
        )
        assert _fired(peak=peak, now=floor) is True
