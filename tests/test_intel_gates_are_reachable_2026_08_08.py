"""The intelligence score is WEIGHT-NORMALISED, so its thresholds read as written.

Both candidate fixes were measured over the same pillar votes before choosing
(2026-08-08):

    criterion                                  A: mean of votes    B: weight-normalised
    a CONFIRMING pillar LOWERED the score      4 of 7 times        0 of 7
    fraction of the 0-1 range reachable        0.24                1.00
    one strong pillar + four confirming        0.350 -> 0.160      1.000 -> 0.727
    discrimination, strong-bull vs neutral     3.66 sd             3.74 sd

B was chosen on COHERENCE, not sharpness -- discrimination is a wash. The mean was
non-monotone: more agreement could mean less conviction, because a confirming pillar
voting below the running mean dragged it down. It also scored a better-informed read
LOWER, since the option chain grew the divisor faster than the sum, which is why the
ceiling fell from 0.29 to 0.178 whenever the chain was present.

On the new scale the score divides by the sum of the MAXIMUM vote each counted pillar
could have cast, so it spans a true -1..+1. Every threshold the original authors wrote --
the five external gates at 0.60-0.75 AND this module's own 0.3/0.6 BUY/STRONG_BUY -- is
reachable again without anyone re-guessing a number. The Option A rebase to 0.107-0.134
was reverted; the gate-parity golden returning to its pre-Option-A hash proves the gates
are byte-identical to before.

MEASURED ceiling: 1.00 on thin evidence, 0.823 with a full option chain.

Bidirectional and per-index symmetry is asserted here too: this system trades four
indices separately and buys BOTH CE and PE, so a maximally bearish read must mirror a
maximally bullish one exactly, on every index.
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

ORIGINAL = {
    "live_15min_trending_momentum_intel_threshold": 0.60,
    "live_15min_momentum_intel_threshold": 0.65,
    "rare_burst_min_intel_score": 0.70,
    "volume_spike_entry_strong_1min_intel_threshold": 0.70,
    "live_5min_special_intel_threshold": 0.75,
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


def test_more_evidence_does_not_collapse_the_score():
    """Under the MEAN this fell 0.290 -> 0.178 when the chain arrived, because the
    divisor grew faster than the sum. Weight-normalised, extra pillars are a weighted
    average of conviction: still below a unanimous maximum, but nowhere near halved."""
    with_chain, a_with = _max_score(_chain())
    without, a_without = _max_score(None)
    assert a_with["components_analyzed"] > a_without["components_analyzed"]
    assert with_chain > 0.75, f"a fully-evidenced maximal read scored only {with_chain:.3f}"
    assert with_chain > 0.8 * without


def test_a_confirming_pillar_never_lowers_conviction():
    """The property the mean violated 4 times in 7. This is why Option B was chosen."""
    from engine.safe_mode import INTEL_SCORE_CEILING as _c  # noqa: F401

    weights = {"volume": 0.35, "oi": 0.10, "pcr": 0.20, "oi_wall": 0.20,
               "regime": 0.25, "order_flow": 0.20, "divergence": 0.30, "top_down": 0.35}
    names = list(weights)
    previous = None
    for n in range(1, len(names) + 1):
        votes = {k: weights[k] for k in names[:n]}
        score = sum(votes.values()) / sum(weights[k] for k in votes)
        if previous is not None:
            assert score >= previous - 1e-9, (
                f"adding pillar {n} LOWERED conviction {previous:.3f} -> {score:.3f}"
            )
        previous = score
    assert previous == pytest.approx(1.0)


# ── four indices, both directions ────────────────────────────────────────────

def _maximally_bearish():
    agg = _maximally_bullish()
    agg.volume = SimpleNamespace(analyze=lambda *a, **k: {
        "available": True, "signal": "BEARISH_VOLUME", "buy_sell_ratio": 0.3,
        "current_volume": 900000, "surge_level": "EXTREME", "volume_ratio": 3.0,
        "market_structure_profile": {"signal": "ACCEPTING_LOWER", "state": "", "poc": 24500},
        "aroon": {"trend": "DOWN", "late_trend_caution": False}, "wavetrend": {},
        "buy_pct": 20.0, "is_proxy": False, "volume_confidence": 90})
    OI = {"available": True, "signal": "STRONG_BEARISH", "call_put_pressure": 0.33,
          "max_oi_support": 24000, "max_oi_resistance": 26000, "pressure": "BEARISH"}
    PCR = {"available": True, "signal": "STRONG_BEARISH", "pcr": 1.8}
    agg.oi = SimpleNamespace(analyze=lambda *a, **k: dict(OI),
                             _empty_result=lambda *a, **k: dict(OI))
    agg.pcr = SimpleNamespace(analyze=lambda *a, **k: dict(PCR),
                              _empty_result=lambda *a, **k: dict(PCR))
    agg.order_flow = SimpleNamespace(analyze=lambda *a, **k: {
        "available": True, "signal": "BEARISH", "ratio": 0.33, "buy_sell_ratio": 0.33,
        "buy_pct": 15.0, "multi_source_flow": {}, "source": "ohlcv_close_position",
        "options_flow": {"ce_pe_activity_ratio": 0.5, "top_ce": [], "top_pe": []}})
    agg.regime = SimpleNamespace(detect=lambda *a, **k: {
        "regime": "TRENDING_DOWN", "confidence": 95, "adx": 42.0})
    return agg


SPOTS = {"NIFTY": 24700.0, "BANKNIFTY": 57900.0, "SENSEX": 78500.0, "MIDCPNIFTY": 14900.0}


@pytest.mark.parametrize("index,spot", sorted(SPOTS.items()))
def test_every_index_reaches_the_same_conviction(index, spot):
    """Four indices trade SEPARATELY; none may carry a scale advantage or handicap."""
    a = _maximally_bullish().analyze(
        instrument=index, timeframe="5min", candle_df=_frame(),
        candle_1min_df=_frame(bars=200, slope=1.0), options_chain=_chain(spot),
        spot_price=spot, strike_interval=50, price_change_pct=2.5)["aggregate"]
    assert float(a["score"]) / 100.0 == pytest.approx(0.823, abs=0.02)
    assert a["signal"] == "STRONG_BUY"


@pytest.mark.parametrize("index,spot", sorted(SPOTS.items()))
def test_bearish_mirrors_bullish_exactly(index, spot):
    """This system BUYS both CE and PE, so a bearish read must be a clean mirror."""
    kw = dict(instrument=index, timeframe="5min", options_chain=_chain(spot),
              spot_price=spot, strike_interval=50)
    bull = _maximally_bullish().analyze(
        candle_df=_frame(slope=8.0), candle_1min_df=_frame(bars=200, slope=1.0),
        price_change_pct=2.5, **kw)["aggregate"]
    bear = _maximally_bearish().analyze(
        candle_df=_frame(slope=-8.0), candle_1min_df=_frame(bars=200, slope=-1.0),
        price_change_pct=-2.5, **kw)["aggregate"]
    assert float(bear["score"]) == pytest.approx(-float(bull["score"]), abs=2.0)
    assert bull["signal"] == "STRONG_BUY" and bear["signal"] == "STRONG_SELL"


# ── the gates are now reachable ──────────────────────────────────────────────

def test_no_gate_is_unreachable_in_the_live_config():
    assert unreachable_intel_gates(get_settings()) == []


@pytest.mark.parametrize("name,expected", sorted(ORIGINAL.items()))
def test_each_gate_resolves_to_its_original_value(name, expected):
    """Resolved LIVE, not the dataclass default — .env overrides and had to change too."""
    assert float(getattr(get_settings(), name)) == pytest.approx(expected)


def test_every_known_gate_is_covered():
    assert set(ORIGINAL) == set(INTEL_SCORE_GATES)


def test_the_relative_strictness_is_preserved():
    """0.60 < 0.65 < 0.70 = 0.70 < 0.75."""
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
    for name, value in ORIGINAL.items():
        assert value < INTEL_SCORE_CEILING, f"{name} is still unreachable"
        assert value > 0.5 * INTEL_SCORE_CEILING, f"{name} is now trivially easy"
