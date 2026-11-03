"""The expiry-day gamma stop must not be widened straight back by the choppy-regime floor.

_option_premium_stop_distance_for_candidate clamped the option stop DOWN to
expiry_day_gamma_sl_pct (10%) inside the gamma window, and then seven lines later ran
`if is_risky_regime(regime): opt_sl_pct = max(opt_sl_pct, 15.0)`, undoing it.

RANGING / VOLATILE / UNKNOWN all count as risky, and that is the normal read on a 0DTE
afternoon — so the gamma tightening was discarded on exactly the trades it exists for,
and the stop sat 50% wider than configured. Wider is not safer on an expiring contract:
it decays to zero regardless, so a loose stop only means more of the premium is gone
before the exit fires.
"""
import pytest

from scanner import Scanner, TradeCandidate

PREMIUM = 200.0


class _Expiry:
    def __init__(self, in_window):
        self._in_window = in_window

    def in_gamma_window(self, instrument, now_dt=None):
        return self._in_window


def _scanner(in_gamma_window, regime):
    s = Scanner.__new__(Scanner)
    s.expiry = _Expiry(in_gamma_window)
    s._session_squareoff_clock = lambda: None
    s._candidate_market_regime = lambda candidate: regime
    s.options_sl_pct = 15.0
    return s


def _candidate():
    return TradeCandidate(
        instrument="NIFTY", direction="LONG", price=PREMIUM, stop=PREMIUM * 0.85,
        target=PREMIUM * 1.3, lots=1, lot_size=65, grade="A", confidence=0.7,
        timeframe="5min", inst_type="OPT", option_type="CE", atm_strike=24500.0,
        multiplier=0.5, trading_symbol="NIFTY2680424500CE", symbol_token="16858626",
    )


def _stop_pct(in_gamma_window, regime):
    scanner = _scanner(in_gamma_window, regime)
    distance = Scanner._option_premium_stop_distance_for_candidate(
        scanner, PREMIUM, _candidate()
    )
    return distance / PREMIUM * 100.0


@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNKNOWN"])
def test_the_gamma_stop_wins_in_every_risky_regime(regime):
    """These three are what is_risky_regime covers, and they are the 0DTE norm."""
    assert _stop_pct(True, regime) == pytest.approx(10.0), (
        f"the {regime} floor widened the expiry-day stop back to 15%"
    )


def test_risky_regimes_do_not_widen_the_operator_option_cap_off_expiry():
    """A 15% cap is retained; a lower configured cap is covered separately."""
    assert _stop_pct(False, "RANGING") == pytest.approx(15.0)


def test_a_trending_regime_is_capped_at_ten_percent_either_way():
    assert _stop_pct(True, "TRENDING_UP") == pytest.approx(10.0)
    assert _stop_pct(False, "TRENDING_UP") == pytest.approx(10.0)


def test_the_tightening_is_worth_real_money():
    """Quantifies the gap the floor was silently reintroducing, per lot."""
    lot = 65
    gap = (_stop_pct(False, "RANGING") - _stop_pct(True, "RANGING")) / 100.0 * PREMIUM
    assert gap == pytest.approx(10.0)
    assert gap * lot == pytest.approx(650.0)
