"""The non-scalp profit lock armed so late that almost nothing reached it.

Measured across 12 sessions / 106 live trades (rtracker, 28 Jul - 11 Aug 2026):

    median peak P&L                    Rs.  274
    median NON-SCALP arm computed      Rs.  868   <- only 4 of 59 rows ever reached it
    median SCALP/CHOPPY arm computed   Rs.  250   <- 25 of 48 rows reached it

    book reached Rs.42,444 of peak profit and banked -Rs.992
    45 trades went green then closed red; 42 of them peaked below every arming floor

The Rs.500 base was not the binding term -- it beat risk*1.0 on just 5 of the 59
non-scalp rows. profit_lock_min_r_multiple bound the other 54, which is why lowering
the base alone would have armed 2 more of the 45 give-backs while the base+R pair arms
20. Both are asserted here so a future edit cannot quietly restore the no-op half.

The counter-worry is the whole point of let_winners_run: does arming early truncate the
fat tail? The production control group says no. scalp_lock trades already ran the low
arm and carried the SAME eight peak>=Rs.750 winners as the loose cohort, with a HIGHER
best peak (Rs.3,582 vs Rs.2,603), while capturing +42.6% of peak against -4.5%.
Arming is not exiting -- the ladder still trails from there.
"""
import pytest

from scanner import Scanner

CAP = 3000.0  # _profit_lock_arm_cap_rs, per-lot basis off


@pytest.fixture(autouse=True)
def _pinned_settings():
    from config.settings import get_settings

    s = get_settings()
    keys = ("profit_lock_min_peak_rs", "profit_lock_nonscalp_min_peak_rs",
            "profit_lock_min_r_multiple", "scalp_profit_lock_min_r_multiple",
            "let_winners_run", "fifteen_min_choppy_scalp_min_peak_rs",
            "profit_lock_per_lot_basis")
    saved = {k: getattr(s, k, None) for k in keys}
    from config.settings import Settings
    for k in keys:
        setattr(s, k, Settings.model_fields[k].default)
    yield
    for k, v in saved.items():
        setattr(s, k, v)


class _Trade:
    market_regime = "TRENDING_UP"
    fifteen_min_choppy_scalp_entry = False


def _arm(risk, scalp=False, timeframe="5min", trade=None):
    sc = Scanner.__new__(Scanner)
    return Scanner._profit_lock_min_peak(
        sc, scalp, risk, timeframe, None, trade_like=trade if trade is not None else _Trade()
    )


# ── the literal is gone ──────────────────────────────────────────────────────

def test_no_hardcoded_arm_remains():
    import inspect

    src = inspect.getsource(Scanner._profit_lock_min_peak)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "= 500.0" not in body, "the arm floor is hardcoded again"


@pytest.mark.parametrize("name", ["profit_lock_min_peak_rs",
                                  "profit_lock_nonscalp_min_peak_rs"])
def test_the_floors_are_settings(name):
    from config.settings import Settings

    assert name in Settings.model_fields


# ── the non-scalp arm now sits where trades actually reach ───────────────────

@pytest.mark.parametrize("risk,expected", [
    (773.0, 250.0),      # p25 risk  -> base backstops risk*0.20 = 154.6
    (937.0, 250.0),      # median    -> base backstops 187.4
    (1440.0, 288.0),     # p75       -> R-multiple takes over
    (2568.0, 513.6),     # max       -> R-multiple takes over
])
def test_the_measured_risk_range_arms_where_trades_reach(risk, expected):
    """p25/median/p75/max of the initial risk actually observed in the window. Every one
    of these used to arm at risk x 1.0, i.e. Rs.773-2,568."""
    assert _arm(risk) == pytest.approx(expected)


def test_the_median_trade_now_reaches_its_arm():
    """Median peak Rs.274 against the median risk of Rs.937."""
    assert _arm(937.0) <= 274.0


def test_the_widest_risk_rows_still_arm_above_the_median_peak():
    """Honest bound on the fix: a p75-risk trade arms at Rs.288, still above the Rs.274
    median peak. This narrows the unprotected band, it does not close it — the rest is
    what the P&L-path logging is being added to answer."""
    assert _arm(1440.0) > 274.0


def test_the_old_behaviour_is_what_changed():
    """Guards the finding itself: at r=1.0 the median trade could never arm."""
    from config.settings import get_settings

    setattr(get_settings(), "profit_lock_min_r_multiple", 1.0)
    assert _arm(937.0) == pytest.approx(937.0)
    assert _arm(937.0) > 274.0


def test_lowering_the_base_alone_would_have_been_a_no_op():
    """The half-fix: base 250 with the R-multiple left at 1.0 changes nothing, because
    risk*1.0 dominates on 54 of 59 rows."""
    from config.settings import get_settings

    setattr(get_settings(), "profit_lock_min_r_multiple", 1.0)
    setattr(get_settings(), "profit_lock_nonscalp_min_peak_rs", 250.0)
    assert _arm(937.0) == pytest.approx(937.0)


# ── the base still backstops the R-multiple ──────────────────────────────────

def test_a_tiny_risk_still_cannot_arm_below_the_base():
    """risk*0.20 on a Rs.100 stop is Rs.20; the base keeps a blip from capping a trade."""
    assert _arm(100.0) == pytest.approx(250.0)


def test_a_huge_risk_is_capped_at_the_major_win_rung():
    assert _arm(100000.0) == pytest.approx(CAP)


def test_zero_risk_falls_back_to_the_base():
    assert _arm(0.0) == pytest.approx(250.0)


# ── let_winners_run keeps its meaning ────────────────────────────────────────

def test_let_winners_run_off_never_arms_later_than_on():
    """The flag adds the R-multiple DELAY. If the base were gated on it too, switching
    it OFF would arm later than leaving it ON — the inversion this test pins shut."""
    from config.settings import get_settings

    for risk in (0.0, 250.0, 937.0, 2568.0, 100000.0):
        setattr(get_settings(), "let_winners_run", True)
        on = _arm(risk)
        setattr(get_settings(), "let_winners_run", False)
        off = _arm(risk)
        assert off <= on, f"risk={risk}: let_winners_run OFF armed later ({off} > {on})"


# ── the scalp path is untouched ──────────────────────────────────────────────

def test_the_scalp_base_is_unchanged():
    """Trending scalp keeps the historical Rs.500 base."""
    assert _arm(100.0, scalp=True) == pytest.approx(500.0)


def test_the_choppy_scalp_path_is_unchanged():
    t = _Trade()
    t.market_regime = "RANGING"
    assert _arm(100.0, scalp=True, trade=t) == pytest.approx(250.0)


def test_the_15min_choppy_scalp_arm_is_unchanged():
    t = _Trade()
    t.market_regime = "RANGING"
    t.fifteen_min_choppy_scalp_entry = True
    assert _arm(937.0, scalp=True, timeframe="15min", trade=t) == pytest.approx(250.0)


def test_scalp_still_uses_its_own_r_multiple():
    """risk*0.50 above the Rs.500 base."""
    assert _arm(4000.0, scalp=True) == pytest.approx(2000.0)


# ── the arm is a floor for the ladder, not an exit ───────────────────────────

def test_arming_does_not_by_itself_close_a_trade():
    """A peak below the arm returns no lock at all; above it, the ladder decides the
    retention. Nothing here takes a position off."""
    import inspect

    src = inspect.getsource(Scanner._profit_lock_floor)
    assert "peak_pnl < min_peak" in src
    assert 'return 0.0, "", 0.0' in src
