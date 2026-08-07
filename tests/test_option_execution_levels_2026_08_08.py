"""Two structural money bugs on the only instrument this system trades.

`_execution_levels` re-derives entry/stop/target in contract space just before the order
goes out. Its OPT branch had two defects that its own FUT sibling does not.

1. THE TARGET WAS MULTIPLIED BY DELTA A SECOND TIME.

   For an OPT candidate both operands are ALREADY premium — SignalProcessor sets
   live_trade_price = live_premium, then candidate_target = live_trade_price +
   max_sl_dist * final_rr. So `candidate.target - candidate.price` is a premium
   distance. The branch then did `target_move *= abs(candidate.multiplier)`, and
   multiplier is the Black-Scholes signed delta.

   Every normal option entry was therefore closed at roughly delta x the graded reward:
   a 2.0 RR realised as 1.0 at an ATM delta of 0.5, and 0.6 on a 0.3-delta strike.

2. THE ATR-AWARE PER-TIMEFRAME STOP WAS THROWN AWAY.

   SignalProcessor bounds the premium stop to 4-10% (5min) / 6-15% (15min) with
   OPTION_STOP_TF_ATR_AWARE on. `_execution_levels` ignored candidate.stop entirely and
   recomputed from `_option_premium_stop_distance_for_candidate`, which has no ATR term
   and no timeframe band — it returns the flat OPTIONS_SL_PCT cap (15%). So a 5min
   option graded on a 4% stop was sent with a 15% stop, up to 3.75x the chosen risk.

Together: risk 15% of premium to chase ~5-12% of it. Structurally negative expectancy.

The FUT branch never had either bug — it preserves candidate risk_distance verbatim and
does not scale the reward. Neither defect had a single test.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner

ENTRY_PREMIUM = 100.0


def _scanner(premium=ENTRY_PREMIUM, opt_sl_pct=15.0):
    s = Scanner.__new__(Scanner)
    s._get_live_premium_cached = lambda c: premium
    s._get_live_fut_ltp_cached = lambda c: 0.0
    s.log_signal_decision_once = lambda *a, **k: None
    s._configured_option_sl_pct = lambda: opt_sl_pct
    s.expiry = SimpleNamespace(in_gamma_window=lambda *a, **k: False)
    s._session_squareoff_clock = lambda: None
    return s


def _opt_candidate(price=100.0, stop=96.0, target=112.0, delta=0.5, **kw):
    """A 5min ATM call: 4% ATR stop, RR 3 -> 12% target. Premium space throughout."""
    base = dict(
        inst_type="OPT", instrument="NIFTY", timeframe="5min", direction="LONG",
        trading_symbol="NIFTY25000CE", price=price, stop=stop, target=target,
        multiplier=delta, rr=3.0, option_type="CE",
        volume_spike_entry=False, late_session_reclaim_entry=False,
        one_way_cascade_entry=False, missed_signal_reentry=False,
        is_reversion_scalp=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── 1. the target must not be re-scaled by delta ─────────────────────────────

def test_the_option_target_keeps_the_graded_reward():
    """Candidate reward is 12 premium points. Executed target must be entry + 12."""
    _, _, target = _scanner()._execution_levels(_opt_candidate())
    assert target == pytest.approx(112.0)


def test_the_delta_does_not_shrink_the_target():
    """The bug: 12 * 0.5 = 6, i.e. entry + 6. Guard the specific wrong number."""
    _, _, target = _scanner()._execution_levels(_opt_candidate(delta=0.5))
    assert target != pytest.approx(106.0), "delta applied a second time"


@pytest.mark.parametrize("delta", [0.25, 0.3, 0.5, 0.7, 0.95])
def test_the_target_is_independent_of_delta(delta):
    """A 0.3-delta strike and a 0.7-delta strike graded to the same reward get it."""
    _, _, target = _scanner()._execution_levels(_opt_candidate(delta=delta))
    assert target == pytest.approx(112.0)


def test_a_negative_delta_put_is_not_inverted():
    """multiplier is a SIGNED delta; a bought PE carries a negative one."""
    _, _, target = _scanner()._execution_levels(_opt_candidate(delta=-0.5))
    assert target == pytest.approx(112.0)


def test_no_target_stays_no_target():
    _, _, target = _scanner()._execution_levels(_opt_candidate(target=0.0))
    assert target == 0.0


# ── 2. the ATR-aware stop must survive execution ─────────────────────────────

def test_the_tight_atr_stop_is_preserved():
    """Candidate stop is 4% of premium. The flat cap is 15%. 4% must win."""
    _, stop, _ = _scanner()._execution_levels(_opt_candidate(stop=96.0))
    assert stop == pytest.approx(96.0)


def test_the_flat_cap_no_longer_widens_a_graded_stop():
    _, stop, _ = _scanner()._execution_levels(_opt_candidate(stop=96.0))
    assert stop != pytest.approx(85.0), "sent on the 15% cap instead of the 4% ATR stop"


def test_a_candidate_stop_wider_than_the_cap_is_still_capped():
    """The cap is a ceiling on risk; the candidate cannot loosen past it."""
    _, stop, _ = _scanner()._execution_levels(_opt_candidate(stop=70.0))
    assert stop == pytest.approx(85.0)


def test_the_stop_scales_to_the_live_premium_not_the_candidate_premium():
    """Premium moved 100 -> 120 between grading and execution; 4% of 120 is 4.8."""
    _, stop, _ = _scanner(premium=120.0)._execution_levels(_opt_candidate())
    assert stop == pytest.approx(115.2)


@pytest.mark.parametrize("bad_stop", [0.0, -5.0, 100.0, 140.0])
def test_an_unusable_candidate_stop_falls_back_to_the_cap(bad_stop):
    _, stop, _ = _scanner()._execution_levels(_opt_candidate(stop=bad_stop))
    assert stop == pytest.approx(85.0)


# ── what must NOT change ─────────────────────────────────────────────────────

def test_the_fast_path_override_still_wins():
    """Volume-spike entries build the target off the tight risk, not the candidate move."""
    _, stop, target = _scanner()._execution_levels(
        _opt_candidate(volume_spike_entry=True, rr=2.0)
    )
    assert target == pytest.approx(stop and ENTRY_PREMIUM + (ENTRY_PREMIUM - stop) * 2.0)


def test_a_missing_live_premium_still_blocks_execution():
    assert _scanner(premium=0.0)._execution_levels(_opt_candidate()) == (0.0, 0.0, 0.0)


def test_the_futures_branch_is_untouched():
    """FUT preserves both distances verbatim and never scales by multiplier."""
    s = Scanner.__new__(Scanner)
    s._get_live_fut_ltp_cached = lambda c: 24800.0
    fut = SimpleNamespace(
        inst_type="FUT", instrument="NIFTY", timeframe="5min", direction="LONG",
        price=24750.0, stop=24700.0, target=24850.0, multiplier=1.0, rr=2.0,
    )
    entry, stop, target = s._execution_levels(fut)
    assert (entry, stop, target) == pytest.approx((24800.0, 24750.0, 24900.0))


# ── the risk/reward the pair actually produces ───────────────────────────────

def test_the_executed_trade_has_the_graded_risk_reward():
    """The headline: 4% risk chasing 12% reward, RR 3 — not 15% chasing 6%."""
    entry, stop, target = _scanner()._execution_levels(_opt_candidate())
    risk, reward = entry - stop, target - entry
    assert risk == pytest.approx(4.0)
    assert reward == pytest.approx(12.0)
    assert reward / risk == pytest.approx(3.0)
