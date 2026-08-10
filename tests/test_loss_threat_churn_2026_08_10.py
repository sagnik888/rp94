"""The 2026-08-10 churn: nine losing trades, all cut inside seven minutes.

Every one exited LOSS_THREAT_EXIT. Decomposed, the day looked like this:

    contract              hold    move   move%   price P&L   reported   charges
    SENSEX 78500CE        155s   -2.20  -0.38%     -44.00    -124.00    -80.00
    NIFTY 24550CE         141s   -0.15  -0.06%      -9.75     -89.75    -80.00
    NIFTY 24550CE           4s   -0.45  -0.19%     -29.25    -109.25    -80.00
    BANKNIFTY 57800PE      33s   -2.65  -0.45%     -79.50    -159.50    -80.00
    ... (9 rows)           TOTAL                  -720.50   -1440.50   -720.00

Half the day's loss was pure transaction cost, and the adverse moves were -0.06% to
-0.68% — inside the bid-ask spread, not a real move. The index itself had moved
0.00-0.05% in the five minutes before each exit (from the reversion-window log lines),
which is below the 0.40%/5min the system's OWN reversion gate calls a move.

Four defects combined:

1. NO COST FLOOR. `loss_mode = current_pnl <= 0.0` — any negative arms it, including one
   rupee of spread. Closing costs a round trip (opt_cost Rs.80), so cutting a Rs.29 loss
   spends Rs.80 to save Rs.29. Value-destroying by construction.

2. NEUTRAL SCORED AS ADVERSE. Threshold 6. A blank tape gave 5 for free: aggregate
   NEUTRAL +1, order-flow NEUTRAL +1, weak volume 0.90x +1, soft-adverse intel -0.16 +1,
   weak trend votes +1. Four of those five are "we do not know", not "this is wrong" —
   the same absence-as-evidence defect as the repaint and single-pillar bugs.

3. NO MINIMUM HOLD. Confirmed absent on both call sites. One exit fired 4.0s after a
   fill, on a signal that had just spent 31s in stabilization.

4. THE CHURN BRAKE COULD NOT FIRE. max_consecutive_losses=3 exists, but the streak was
   measured on the GLOBAL exit tape, so any other index's exit cleared it. NIFTY lost
   four times and the streak read 1, 1, 1, 2.

Plus two reporting defects: the exit's reason string was never logged (the day was
undiagnosable from logs), and the mirror charges a flat Rs.80 where the real model
computes Rs.149-186 — so the dashboard understated the cost by about half.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from scanner import Scanner

ENTRY = datetime(2026, 8, 10, 11, 20, 53)

# The nine live rows: (index, lot, entry, exit, hold_seconds)
TODAY = [
    ("SENSEX", 20, 574.00, 571.80, 155), ("NIFTY", 65, 232.85, 232.70, 141),
    ("NIFTY", 65, 234.90, 234.45, 4),    ("BANKNIFTY", 30, 586.40, 583.75, 33),
    ("NIFTY", 65, 228.40, 227.35, 235),  ("SENSEX", 20, 553.20, 552.10, 247),
    ("BANKNIFTY", 30, 760.30, 757.25, 415), ("NIFTY", 65, 184.55, 183.30, 60),
    ("SENSEX", 20, 614.20, 599.45, 135),
]


def _scanner(neutral_tape=True, trend_votes=0, live_adverse=False):
    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda v, d=True: bool(v) if v is not None else d
    s._trade_direction_view = lambda t, **k: "LONG"
    s.candles = SimpleNamespace(get_latest_price=lambda i: 24550.0)
    s._eod_protect_active = lambda t: False
    s._profit_guard_reversal_context = lambda *a, **k: {
        "has_context": True, "fast_reversal": False, "one_min_emergency": False,
        "trend_supportive": trend_votes >= 1, "trend_votes": trend_votes,
        "reason": f"trend votes {trend_votes}/3"}
    sig = "NEUTRAL" if neutral_tape else "BEARISH"
    s._trade_like_market_intelligence_context = lambda t: {
        "aggregate": {"signal": "HOLD" if neutral_tape else "BEARISH"},
        "order_flow": {"signal": sig, "ratio": 1.0},
        "volume": {"signal": sig}, "oi": {}, "pcr": {}}
    s._regime_conviction_vector_from_context = lambda t, c: {}
    s._trade_like_signed_intelligence = lambda t, c: -0.16
    s._view_aligned_intel_score = lambda v, x: x
    s._intel_signal_opposes_view = lambda g, v: not neutral_tape
    s._intel_signal_aligns_view = lambda g, v: False
    s._live_direction_context = lambda *a, **k: {
        "adverse": live_adverse, "supportive": False, "live_regime": "X",
        "cross_opp": 2, "cross_peers": 3}
    return s


def _trade(index="NIFTY", entry=234.90, lot=65):
    return SimpleNamespace(
        instrument=index, timeframe="5min", inst_type="OPT", direction="LONG",
        option_type="CE", volume_ratio=0.9, price_action_verdict="",
        price_action_score=0.0, entry_price=entry, entry_time=ENTRY,
        lots=1, lot_size=lot)


@pytest.fixture(autouse=True)
def _pin_charge_settings():
    """Pin the charge inputs this file reasons about.

    get_settings() is a process-wide singleton and other tests in this suite mutate it
    (tests/test_audit_trading_fixes.py sets opt_cost / slippage_opt_pct /
    slippage_auto_calibration_enabled on the shared object). Without pinning, the floor
    assertions here pass alone and fail in a full run purely on test order. Restored
    afterwards so this file does not become the next leak.
    """
    from config.settings import get_settings

    s = get_settings()
    saved = {k: getattr(s, k, None) for k in
             ("opt_cost", "fut_cost", "slippage_opt_pct", "slippage_auto_calibration_enabled")}
    s.opt_cost = 80.0
    s.fut_cost = 200.0
    s.slippage_opt_pct = 0.30
    s.slippage_auto_calibration_enabled = False
    yield
    for k, v in saved.items():
        if v is not None:
            setattr(s, k, v)


def _flat_opt_cost() -> float:
    from config.settings import get_settings
    return float(getattr(get_settings(), "opt_cost", 80.0) or 80.0)


def _charged(trade) -> float:
    """What estimate_trade_charges bills this exact row, under whatever settings are live."""
    from config.settings import get_settings
    from engine.trade_accounting import estimate_trade_charges
    qty = int(trade.lots) * int(trade.lot_size)
    return float(estimate_trade_charges(
        trade.entry_price, trade.entry_price, qty, "OPT", get_settings(),
        instrument=trade.instrument))


def _confirmed(s, trade, pnl, held, gap=5):
    """Run the decision to a VERDICT.

    The loss arm now needs the same evidence signature twice, at least
    peak_threat_exit_loss_confirm_seconds apart on the market clock. A single call can
    only ever answer "confirmation 1/2", so a test that calls once and asserts False
    proves nothing about the guard.
    """
    _decide(s, trade, pnl, held)
    return _decide(s, trade, pnl, held + gap)


def _decide(s, trade, pnl, held):
    return Scanner._peak_threat_exit_decision(
        s, trade, current_price=trade.entry_price, current_pnl_rs=pnl, peak_pnl=0.0,
        initial_risk_rs=780.0, latest_time=ENTRY + timedelta(seconds=held),
        regime="RANGING", inst_type="OPT", option_type="CE", direction="LONG")


# ── every one of today's nine is now refused ─────────────────────────────────

@pytest.mark.parametrize("index,lot,entry,exit_px,held", TODAY)
def test_none_of_todays_exits_would_fire_again(index, lot, entry, exit_px, held):
    price_pnl = (exit_px - entry) * lot
    ok, reason, ctx = _decide(_scanner(), _trade(index, entry, lot), price_pnl, held)
    assert ok is False, f"{index} would still be cut: {ctx}"


def test_the_day_in_aggregate():
    """All nine, not one at a time."""
    fired = [
        (i, c) for i, lot, e, x, h in TODAY
        for ok, _r, c in [_decide(_scanner(), _trade(i, e, lot), (x - e) * lot, h)]
        if ok
    ]
    assert fired == [], f"{len(fired)} of 9 still fire: {fired[:2]}"


# ── 1. the cost floor ────────────────────────────────────────────────────────

def test_a_loss_smaller_than_the_round_trip_is_refused():
    ok, _r, ctx = _decide(_scanner(), _trade(), -29.25, 600)
    assert ok is False
    assert "round-trip cost" in ctx


def test_a_genuinely_adverse_tape_still_cannot_cut_inside_the_spread():
    """The floor's real job. Everything else about this trade says exit — adverse on
    every pillar, well past the minimum hold — but the loss is Rs.29 and closing costs
    Rs.80, so taking it turns a Rs.29 problem into a Rs.109 one. This is the case the
    other three gates do NOT cover."""
    ok, _r, ctx = _decide(_scanner(neutral_tape=False), _trade(), -29.25, 600)
    assert ok is False, "cut a Rs.29 loss at a Rs.80 round trip"
    assert "round-trip cost" in ctx


def test_a_loss_larger_than_the_round_trip_may_still_be_judged():
    """The floor must not disable the guard, only stop it eating the spread."""
    ok, _r, ctx = _decide(_scanner(neutral_tape=False), _trade(), -180.0, 600)
    assert "round-trip cost" not in ctx


def test_the_floor_prices_the_actual_position_not_a_flat_placeholder():
    """The 13:04 row is why. The flat opt_cost is Rs.80, but a MIDCPNIFTY 174.50 x 120
    round trip costs Rs.205 — and the row WAS charged 205. Judging "is this loss worth
    the cost of closing" against 80 let through a Rs.168 loss cut at a Rs.205 cost, which
    is the exact trade this floor exists to refuse."""
    # Asserted as a RELATIONSHIP, not a magic number: another test in this suite mutates
    # the shared get_settings() singleton, so any hardcoded rupee figure here passes alone
    # and fails in a full run. The contract is that the floor equals what the row is
    # actually charged.
    s = _scanner()
    midcap = _trade("MIDCPNIFTY", 174.50, 120)
    floor = Scanner._round_trip_cost_rs(s, midcap, inst_type="OPT")
    assert floor == pytest.approx(_charged(midcap), rel=0.02), (
        f"floor Rs.{floor:.0f} does not match the Rs.{_charged(midcap):.0f} the row pays"
    )
    assert floor > _flat_opt_cost(), "the floor is still the flat placeholder"


def test_the_1304_row_is_refused_by_the_corrected_floor():
    s = _scanner(neutral_tape=False)
    midcap = _trade("MIDCPNIFTY", 174.50, 120)
    floor = Scanner._round_trip_cost_rs(s, midcap, inst_type="OPT")
    ok, _r, ctx = _decide(s, midcap, -(floor * 0.8), 500)
    assert ok is False, f"still cut a loss smaller than the cost of cutting it: {ctx}"
    assert "round-trip cost" in ctx


def test_a_loss_beyond_the_real_cost_still_exits_on_adverse_evidence():
    s = _scanner(neutral_tape=False)
    midcap = _trade("MIDCPNIFTY", 174.50, 120)
    floor = Scanner._round_trip_cost_rs(s, midcap, inst_type="OPT")
    ok, _r, _c = _confirmed(s, midcap, -(floor * 1.3), 500)
    assert ok is True, "the floor must bound the guard, not disable it"


def test_the_floor_falls_back_to_the_flat_cost_when_it_cannot_price_the_row():
    from types import SimpleNamespace as NS

    s = _scanner()
    bare = NS(instrument="NIFTY", inst_type="OPT")   # no lots/lot_size/price
    assert Scanner._round_trip_cost_rs(s, bare, inst_type="OPT") == 80.0
    assert Scanner._round_trip_cost_rs(s, bare, inst_type="FUT") == 200.0


def test_the_floor_scales_with_position_size():
    """A 3-lot position costs more to close than a 1-lot, so its floor must be higher."""
    s = _scanner()
    one = _trade("NIFTY", 232.85, 65)
    three = _trade("NIFTY", 232.85, 65)
    three.lots = 3
    assert _charged(three) > _charged(one), "premise: 3 lots must cost more to close"
    assert (Scanner._round_trip_cost_rs(s, three, inst_type="OPT")
            > Scanner._round_trip_cost_rs(s, one, inst_type="OPT"))


# ── 2. neutral is not adverse ────────────────────────────────────────────────

def test_a_blank_tape_no_longer_reaches_the_threshold():
    """It scored exactly 6 of 6 before; four of those points were 'do not know'."""
    ok, _r, ctx = _confirmed(_scanner(), _trade(), -180.0, 600)
    assert ok is False
    assert "score 4/6" in ctx or "score 5/6" in ctx, ctx


def test_a_genuinely_adverse_tape_still_exits():
    """The guard must keep working on real evidence."""
    s = _scanner(neutral_tape=False)
    ok, reason, ctx = _confirmed(s, _trade(), -180.0, 600)
    assert ok is True, ctx
    assert reason == "LOSS_THREAT_EXIT"


# ── 3. minimum hold ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("held", [0, 4, 33, 59])
def test_no_exit_inside_the_minimum_hold(held):
    ok, _r, ctx = _decide(_scanner(neutral_tape=False), _trade(), -180.0, held)
    assert ok is False
    assert "minimum" in ctx


def test_past_the_minimum_hold_the_guard_is_live_again():
    ok, _r, _c = _confirmed(_scanner(neutral_tape=False), _trade(), -180.0, 61)
    assert ok is True


def test_an_unknowable_age_does_not_block_the_guard():
    """None means 'cannot tell' and must not be read as 'too young' — that would
    disarm the exit for any row without an entry stamp."""
    t = _trade()
    del t.entry_time
    ok, _r, _c = _confirmed(_scanner(neutral_tape=False), t, -180.0, 600)
    assert ok is True


def test_the_age_helper_is_direction_safe():
    t = _trade()
    assert Scanner._trade_like_age_seconds(t, ENTRY + timedelta(seconds=90)) == 90.0
    assert Scanner._trade_like_age_seconds(t, ENTRY - timedelta(seconds=90)) is None
    assert Scanner._trade_like_age_seconds(t, None) is None


# ── 4. the churn brake reaches its halt ──────────────────────────────────────

def test_todays_interleaved_losses_now_halt_nifty():
    s = Scanner.__new__(Scanner)
    s._losses_today = {}
    s._last_loss_outcome_instrument = ""
    s._setting_enabled = lambda v, d=True: bool(v) if v is not None else d
    order = ["SENSEX", "NIFTY", "BANKNIFTY", "NIFTY", "SENSEX",
             "BANKNIFTY", "NIFTY", "NIFTY", "SENSEX"]
    peak = 0
    for name in order:
        streak = s._record_consecutive_loss_outcome(name, -100.0)
        if name == "NIFTY":
            peak = max(peak, streak)
    assert peak >= 3, f"NIFTY peaked at {peak}; the halt at 3 still cannot fire"


# ── 5/6. the reporting defects ───────────────────────────────────────────────

def test_the_mirror_exit_logs_its_reason():
    import inspect

    src = inspect.getsource(Scanner._check_session_candidates_breaches_locked)
    assert "[EXIT {exit_reason}]" in src or "EXIT {exit_reason}" in src, (
        "the mirror still closes positions without recording why"
    )


def test_the_mirror_charges_what_the_real_model_charges():
    import inspect

    src = inspect.getsource(Scanner._candidate_net_pnl)
    assert "estimate_trade_charges" in src, (
        "the dashboard still shows a flat Rs.80 where REAL mode pays Rs.149-186"
    )
    assert "opt_cost" in src, "the flat figure must remain as the fallback"
