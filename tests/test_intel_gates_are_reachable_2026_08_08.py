"""Option A: the five intelligence gates rebased onto the score's real scale.

MEASURED, not inferred. Driving the real aggregator with maximally bullish inputs on
every pillar (2026-08-08):

    thin evidence, no option chain : vote sum 0.87 / 3 components = 0.290
    full evidence, WITH the chain  : vote sum 1.07 / 6 components = 0.178

Because the published score is a MEAN, extra evidence DILUTES it: the chain raises the
vote sum by 23% but doubles the divisor, so a better-informed read scores lower. Live
always has a chain, so 0.178 is the ceiling that actually applies.

The five gates were written on a 0..1 scale and tested against 0.60-0.75, so none of them
could ever fire. They are rebased to `old x ceiling`, which preserves the relative
strictness their author chose:

    live_15min_trending_momentum   0.60 -> 0.107
    live_15min_momentum            0.65 -> 0.116
    rare_burst_min_intel_score     0.70 -> 0.125
    volume_spike_entry_strong_1min 0.70 -> 0.125
    live_5min_special              0.75 -> 0.134

Option B (rescaling the score itself) was NOT taken: it would move the intelligence number
everywhere at once, including the figure on the dashboard.

Both the dataclass defaults AND .env had to change. Rebasing only the defaults left all
four .env-backed gates on their old values — the .env-overrides-defaults trap, caught here
by the readiness check itself.
"""
import numpy as np
import pandas as pd
import pytest
from types import SimpleNamespace

from config.settings import get_settings
from engine.safe_mode import (
    INTEL_SCORE_CEILING,
    INTEL_SCORE_GATES,
    unreachable_intel_gates,
)
from intelligence.intelligence_aggregator import IntelligenceAggregator

REBASED = {
    "live_15min_trending_momentum_intel_threshold": 0.107,
    "live_15min_momentum_intel_threshold": 0.116,
    "rare_burst_min_intel_score": 0.125,
    "volume_spike_entry_strong_1min_intel_threshold": 0.125,
    "live_5min_special_intel_threshold": 0.134,
}


# ── the measurement the constant rests on ────────────────────────────────────

def _frame(bars=90, slope=8.0):
    idx = pd.date_range("2026-08-05 09:15", periods=bars, freq="5min")
    c = 24000.0 + np.arange(bars) * slope
    return pd.DataFrame(
        {"open": c - 2, "high": c + 6, "low": c - 6, "close": c,
         "volume": np.linspace(3e5, 9e5, bars)}, index=idx,
    )


def _chain(spot=24700.0, n=11, step=50):
    rows = []
    for i in range(-(n // 2), n // 2 + 1):
        k = spot + i * step
        common = dict(strike=k, strikePrice=k, call_oi=40000, put_oi=160000,
                      call_volume=9000, put_volume=3000, iv=14.0)
        rows.append({**common, "type": "CE", "oi": 40000, "volume": 9000,
                     "ltp": 120.0, "change_oi": -5000, "delta": 0.55})
        rows.append({**common, "type": "PE", "oi": 160000, "volume": 3000,
                     "ltp": 80.0, "change_oi": 40000, "delta": -0.45})
    return pd.DataFrame(rows)


def _maximally_bullish():
    agg = IntelligenceAggregator()
    OI = {"available": True, "signal": "STRONG_BULLISH", "call_put_pressure": 3.0,
          "max_oi_support": 24000, "max_oi_resistance": 26000, "pressure": "BULLISH"}
    PCR = {"available": True, "signal": "STRONG_BULLISH", "pcr": 0.4}
    agg.volume = SimpleNamespace(analyze=lambda *a, **k: {
        "available": True, "signal": "BULLISH_VOLUME", "buy_sell_ratio": 3.0,
        "current_volume": 900000, "surge_level": "EXTREME", "volume_ratio": 3.0,
        "market_structure_profile": {"signal": "ACCEPTING_HIGHER",
                                     "state": "ACCEPTING_HIGHER", "poc": 24500},
        "aroon": {"trend": "UP", "late_trend_caution": False}, "wavetrend": {},
        "buy_pct": 80.0, "is_proxy": False, "volume_confidence": 90})
    agg.oi = SimpleNamespace(analyze=lambda *a, **k: dict(OI),
                             _empty_result=lambda *a, **k: dict(OI))
    agg.pcr = SimpleNamespace(analyze=lambda *a, **k: dict(PCR),
                              _empty_result=lambda *a, **k: dict(PCR))
    agg.order_flow = SimpleNamespace(analyze=lambda *a, **k: {
        "available": True, "signal": "BULLISH", "ratio": 3.0, "buy_sell_ratio": 3.0,
        "buy_pct": 85.0, "multi_source_flow": {}, "source": "ohlcv_close_position",
        "options_flow": {"ce_pe_activity_ratio": 2.0, "top_ce": [], "top_pe": []}})
    agg.regime = SimpleNamespace(detect=lambda *a, **k: {
        "regime": "TRENDING_UP", "confidence": 95, "adx": 42.0})
    return agg


def _max_score(chain):
    a = _maximally_bullish().analyze(
        instrument="NIFTY", timeframe="5min", candle_df=_frame(),
        candle_1min_df=_frame(bars=200, slope=1.0), options_chain=chain,
        spot_price=24720.0, strike_interval=50, price_change_pct=2.5)["aggregate"]
    raw = abs(float(a.get("score") or 0.0))
    return (raw / 100.0 if raw > 1.0 else raw), a


def test_the_ceiling_still_matches_the_live_aggregator():
    """Re-measures rather than trusting the constant. If the votes change, this fails."""
    measured, _ = _max_score(_chain())
    assert measured == pytest.approx(INTEL_SCORE_CEILING, abs=0.03), (
        f"maximal bullish now scores {measured:.3f} but INTEL_SCORE_CEILING is "
        f"{INTEL_SCORE_CEILING} — the gates need rebasing again"
    )


def test_more_evidence_lowers_the_score():
    """The structural reason the ceiling is so low: it is a MEAN, so pillars dilute."""
    with_chain, a_with = _max_score(_chain())
    without, a_without = _max_score(None)
    assert with_chain < without
    assert a_with["components_analyzed"] > a_without["components_analyzed"]
    assert float(a_with["score_before_regime_dampening"]) > float(
        a_without["score_before_regime_dampening"]
    ), "the vote SUM should rise with more evidence even as the mean falls"


# ── the gates are now reachable ──────────────────────────────────────────────

def test_no_gate_is_unreachable_in_the_live_config():
    assert unreachable_intel_gates(get_settings()) == []


@pytest.mark.parametrize("name,expected", sorted(REBASED.items()))
def test_each_gate_resolves_to_its_rebased_value(name, expected):
    """Resolved LIVE, not the dataclass default — .env overrides and had to change too."""
    assert float(getattr(get_settings(), name)) == pytest.approx(expected)


def test_every_known_gate_was_rebased():
    assert set(REBASED) == set(INTEL_SCORE_GATES)


def test_the_relative_strictness_is_preserved():
    """0.60 < 0.65 < 0.70 = 0.70 < 0.75 must still hold after rebasing."""
    s = get_settings()
    order = [
        "live_15min_trending_momentum_intel_threshold",
        "live_15min_momentum_intel_threshold",
        "rare_burst_min_intel_score",
        "live_5min_special_intel_threshold",
    ]
    values = [float(getattr(s, n)) for n in order]
    assert values == sorted(values), f"rebasing reordered the gates: {values}"


def test_the_gates_sit_below_the_ceiling_but_are_not_trivial():
    """Reachable, yet still demanding 60-75% of the maximum possible conviction."""
    for name, value in REBASED.items():
        assert value < INTEL_SCORE_CEILING, f"{name} is still unreachable"
        assert value > 0.5 * INTEL_SCORE_CEILING, f"{name} is now trivially easy"
