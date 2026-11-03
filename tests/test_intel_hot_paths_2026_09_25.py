"""Market-intelligence CPU (2026-09-25 live py-spy): greeks via scipy scalar norm.cdf and
the VIX percentile re-parsing ~31k rows on each new VIX value were ~30% of intelligence
CPU. Both are now fast; these tests pin that the answers did not change."""
import itertools
import time
from datetime import datetime, timedelta

import pytest

from intelligence.greeks_engine import GreeksEngine
from scanner import IST, Scanner


def _scipy_reference(spot, strike, days, iv, opt_type, r=0.065):
    import numpy as np
    from scipy.stats import norm

    effective_days = max(float(days or 0.0), 0.5)
    T = effective_days / 365.0
    d1 = (np.log(spot / strike) + (r + 0.5 * iv**2) * T) / (iv * np.sqrt(T))
    d2 = d1 - iv * np.sqrt(T)
    if opt_type == "call":
        delta, price = norm.cdf(d1), spot * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2)
    else:
        delta, price = norm.cdf(d1) - 1, strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)
    gamma = norm.pdf(d1) / (spot * iv * np.sqrt(T))
    return round(float(delta), 4), round(float(gamma), 6), round(float(max(0, price)), 2)


def test_greeks_match_the_scipy_formulas():
    engine = GreeksEngine()
    for spot, k, days, iv, kind in itertools.product(
        [14050.0, 23100.5, 55400.0], [0.98, 1.0, 1.02], [0.3, 2.5, 30], [0.08, 0.2, 0.6], ["call", "put"]
    ):
        strike = round(spot * k / 50) * 50
        got = engine.calculate(spot, strike, days, iv, kind)
        assert (got["delta"], got["gamma"], got["price"]) == _scipy_reference(spot, strike, days, iv, kind)


def test_greeks_are_fast():
    engine = GreeksEngine()
    start = time.perf_counter()
    for _ in range(2000):
        engine.calculate(23100.0, 23100.0, 2.5, 0.15, "call")
    assert (time.perf_counter() - start) / 2000 < 100e-6      # was ~500us with scipy


def _vix_scanner(days=60):
    sc = Scanner.__new__(Scanner)
    base = datetime(2026, 7, 1)
    data = {}
    for d in range(days):
        day = base + timedelta(days=d)
        for minute in range(0, 375, 25):                   # intraday rows too
            ts = day.replace(hour=9, minute=15) + timedelta(minutes=minute)
            data[ts.strftime("%Y-%m-%d %H:%M:%S")] = 10.0 + (d % 17) * 0.5 + minute / 1000.0
    sc._vix_data = data
    return sc


def _reference_percentile(data, value, as_of):
    daily = {}
    for key, raw in data.items():
        ts = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
        if ts.date() >= as_of.date():
            continue
        day = ts.date().isoformat()
        if day not in daily or key > daily[day][0]:
            daily[day] = (key, float(raw))
    values = [v for _, v in daily.values()]
    return round(sum(1 for v in values if v <= value) / len(values) * 100.0, 1)


@pytest.mark.parametrize("value", [9.0, 11.2, 11.35, 13.37, 15.0, 30.0])
def test_vix_percentile_is_unchanged_and_reuses_the_daily_closes(value):
    sc = _vix_scanner()
    as_of = IST.localize(datetime(2026, 8, 25, 11, 0))
    assert sc._vix_percentile(value, as_of) == _reference_percentile(sc._vix_data, value, as_of)
    # A new live value on the same day reuses the cached closes (no re-parse) and agrees.
    other = value + 0.013
    assert sc._vix_percentile(other, as_of) == _reference_percentile(sc._vix_data, other, as_of)
    assert "2026-08-25" in sc._vix_daily_values_cache
    # Cached path, fresh scanner: warm the day with another value, then ask for this one
    # (ties with a daily close must count as <=, like the original loop).
    warm = _vix_scanner()
    warm._vix_percentile(99.0, as_of)
    assert warm._vix_percentile(value, as_of) == _reference_percentile(warm._vix_data, value, as_of)
