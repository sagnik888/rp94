"""Two feeds disagreeing about one index is a fault, and the bar was too high.

Kite and Fyers both quote the SAME computed index. There is no spread to explain a
gap away, so any real disagreement is staleness or a bad tick. Past the threshold
the system keeps the venue that fills the orders and quarantines the other for that
symbol; inside it, the fresher quote wins.

At the old 0.05% the tolerance was wide enough to hide a genuinely lagging feed on
the very series that gates entries:

                       0.05%          0.015%
    NIFTY    24,000    12.00 pts      3.60 pts
    BANKNIFTY 52,000   26.00 pts      7.80 pts
    SENSEX   78,000    39.00 pts     11.70 pts
    MIDCPNIFTY 14,975   7.49 pts      2.25 pts

Thirty-nine points of undetected disagreement on SENSEX is a large move on an index
whose signals open positions.

The threshold is a setting now rather than a bare class constant, so it can be
tuned without a code change. The class attribute remains as the fallback for a
provider constructed without settings.
"""
import pytest

from data.market_data import MarketDataProvider


def _provider():
    return MarketDataProvider.__new__(MarketDataProvider)


# ── the value itself ─────────────────────────────────────────────────────────

def test_the_default_is_the_tightened_value():
    assert MarketDataProvider.QUOTE_DIVERGENCE_ALERT_PCT == pytest.approx(0.015)


def test_it_is_read_from_settings():
    from config.settings import get_settings

    assert _provider()._quote_divergence_alert_pct() == pytest.approx(
        get_settings().quote_divergence_alert_pct
    )


def test_the_setting_is_what_actually_applies(monkeypatch):
    """Tunable without a code change — the point of moving it out of the constant."""
    import config.settings as settings_module

    settings = settings_module.get_settings()
    monkeypatch.setattr(settings, "quote_divergence_alert_pct", 0.01, raising=False)

    assert _provider()._quote_divergence_alert_pct() == pytest.approx(0.01)


@pytest.mark.parametrize("bad", [0.0, -1.0, None])
def test_an_unusable_setting_falls_back_to_the_class_default(bad, monkeypatch):
    """A zero tolerance would quarantine a feed on every tick-timing difference."""
    import config.settings as settings_module

    settings = settings_module.get_settings()
    monkeypatch.setattr(settings, "quote_divergence_alert_pct", bad, raising=False)

    assert _provider()._quote_divergence_alert_pct() == pytest.approx(
        MarketDataProvider.QUOTE_DIVERGENCE_ALERT_PCT
    )


# ── what it means on the real indices ────────────────────────────────────────

@pytest.mark.parametrize("name,ltp,max_points", [
    ("NIFTY", 24000, 4.0),
    ("BANKNIFTY", 52000, 8.0),
    ("SENSEX", 78000, 12.0),
    ("MIDCPNIFTY", 14975, 2.5),
])
def test_the_tolerated_gap_is_small_on_every_index(name, ltp, max_points):
    """Pinned in points, because percent hides what the number actually permits."""
    tolerated = ltp * _provider()._quote_divergence_alert_pct() / 100.0

    assert tolerated < max_points, f"{name} tolerates {tolerated:.2f} points"


def test_it_is_tighter_than_it_was():
    assert MarketDataProvider.QUOTE_DIVERGENCE_ALERT_PCT < 0.05


def test_it_is_not_so_tight_that_normal_tick_timing_trips_it():
    """Both feeds tick at different instants; a fraction of a point must pass."""
    tolerated = 24000 * _provider()._quote_divergence_alert_pct() / 100.0

    assert tolerated > 1.0, "sub-point tolerance would quarantine a healthy feed"
