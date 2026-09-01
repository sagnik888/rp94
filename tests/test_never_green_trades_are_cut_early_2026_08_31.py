"""A trade that has never printed a positive P&L must not be held for 46 minutes.

2026-08-31, investigating a session where the system took 2 small wins and 4 losses
while NIFTY swung -50 -> -180 -> -70 -> -150 -> -90 and the other three indices did the
same. The operator's hypothesis was that the expected-move floor was rejecting the real
moves. It was not: the floor rejected NOTHING that session in either sink, and taking
every rejected signal would have LOST Rs 30,187.

What the archive shows instead, over 61 sessions and 313 deduped exits:

    72 of 204 losing trades never ticked green (peak_pnl == 0)
     0 of 109 winning trades ever did

"Never green" is therefore a 72/72 loser with no counterexample, and those trades lose
2.8x harder than the rest -- mean Rs -653 against Rs -230.

They were also held far too long. _loss_guard_decision cuts purely on an R-multiple of
the loss (0.35-1.0R depending on severity) with NO time term, so an immediately-wrong
trade simply bled until it hit that multiple: a MEDIAN OF 46 MINUTES, up to 156. The
reason string said EARLY_LOSS_CUT; nothing about it was early.

That session's three losers were all never-green: -505.75, -408.25 and -764.00, together
-1,678 of the -1,106 day.

Replaying "cut at 5 minutes if never green" over the 118 exits that carry a pnl_path
(13 sessions), applied ALWAYS rather than only where it helps, and tighten-side only --
the sole direction pnl_path can measure honestly, since the path ends at the real exit:

    +Rs 3,577 against a subset that actually realised +Rs 521
    robust 3-10 minutes (+2,840 .. +3,577), so not perched on a knife edge
    10 of 13 sessions improved, 1 worsened, 2 unchanged
    leave-one-session-out minimum +Rs 2,775
    session-clustered bootstrap 95% CI +Rs 1,537 .. +Rs 5,731, 100% of resamples > 0

Caveat kept deliberately in view: that corpus is the VIRTUAL lane and its P&L is
advisory -- a flat Rs 80 cost, no broker fill. The rule is a tightening, not a loosening.
"""
import pytest

from config.settings import Settings


class _Trade:
    """Minimal stand-in. `instrument` and `timeframe` are needed because a decision that
    does NOT trip the never-green rule falls through to the rest of the loss guard."""

    def __init__(self, peak_pnl=0.0, inst_type="OPT"):
        self.peak_pnl = peak_pnl
        self.inst_type = inst_type
        self.entry_time = None
        self.instrument = "NIFTY"
        self.timeframe = "5min"
        self.direction = "LONG"


@pytest.fixture
def guard():
    """The decision function, bound to a scanner shell with the helpers it needs."""
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    return scanner


def _decide(scanner, trade, *, pnl, risk, age_seconds):
    scanner._trade_like_age_seconds = lambda t, when: age_seconds
    scanner._setting_enabled = lambda value, default=False: bool(value)
    return scanner._loss_guard_decision(
        trade,
        current_pnl_rs=pnl,
        initial_risk_rs=risk,
        latest_time=None,
    )


def test_a_never_green_trade_is_cut_at_the_grace_period(guard):
    """The regression: this trade used to be held a median of 46 minutes."""
    trade = _Trade(peak_pnl=0.0)

    should_cut, reason, _ = _decide(guard, trade, pnl=-120.0, risk=900.0, age_seconds=310.0)

    assert should_cut is True, (
        "a trade that has never been green was held past the grace period; the "
        "R-multiple guard alone would keep it for tens of minutes"
    )
    assert "never-green" in reason


def test_it_is_not_cut_before_the_grace_period(guard):
    """Five minutes is a grace period, not a hair trigger."""
    trade = _Trade(peak_pnl=0.0)

    should_cut, reason, _ = _decide(guard, trade, pnl=-120.0, risk=900.0, age_seconds=60.0)

    assert should_cut is False, f"cut after only 60s: {reason}"
    assert "never-green" not in reason


def test_a_trade_that_went_green_is_left_to_the_r_multiple_guard(guard):
    """The rule keys on peak_pnl, NOT on the current P&L.

    A trade showing a loss right now but which HAS been green is an ordinary pullback;
    those lose Rs 230 on average and recover often enough to be worth the room. Only the
    never-green cohort is a 72/72 loser.
    """
    trade = _Trade(peak_pnl=450.0)

    should_cut, reason, _ = _decide(guard, trade, pnl=-120.0, risk=900.0, age_seconds=3600.0)

    assert "never-green" not in reason, (
        "a trade that peaked at +450 was cut by the never-green rule; the rule must key "
        "on peak_pnl, not on the current unrealised loss"
    )


def test_a_winner_never_reaches_the_rule(guard):
    """The loss guard is idle while a trade is in profit."""
    trade = _Trade(peak_pnl=0.0)

    should_cut, reason, _ = _decide(guard, trade, pnl=25.0, risk=900.0, age_seconds=9999.0)

    assert should_cut is False
    assert "loss guard idle" in reason


def test_the_rule_can_be_switched_off(guard):
    """An exit-behaviour change on a money system must be reversible without a patch."""
    from config.settings import get_settings

    settings = get_settings()
    original = getattr(settings, "loss_guard_never_green_cut_enabled", True)
    try:
        settings.loss_guard_never_green_cut_enabled = False
        trade = _Trade(peak_pnl=0.0)
        should_cut, reason, _ = _decide(
            guard, trade, pnl=-120.0, risk=900.0, age_seconds=3600.0
        )
        assert "never-green" not in reason
    finally:
        # Settings is a shared singleton; a bare assignment here would leak into every
        # later test in the session.
        settings.loss_guard_never_green_cut_enabled = original


def test_the_grace_period_sits_inside_the_measured_optimum():
    """3-10 minutes all replayed positive; the default must stay in that band."""
    seconds = float(Settings().loss_guard_never_green_cut_seconds)

    assert 180.0 <= seconds <= 600.0, (
        f"grace period {seconds}s is outside the 3-10 minute range the replay measured; "
        "outside it the +Rs 3,577 result was never demonstrated"
    )


def test_the_default_is_on():
    assert Settings().loss_guard_never_green_cut_enabled is True
