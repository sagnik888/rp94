"""Order flow was the only intelligence pillar that never abstained.

Volume, OI and PCR all gate BOTH their vote and their `components += 1` on an
availability flag. Order flow did neither — it voted and took a component
unconditionally.

`OrderFlowAnalyzer` returns `_empty_result` whenever the current-session frame has fewer
than five bars (its own comment: "fewer than five live bars must mean 'no reading'") and
on any exception. That result reports signal NEUTRAL with source "unavailable". So
"unknown" was scored as "balanced" AND added to the denominator, shrinking every other
pillar's contribution by roughly 1/N.

`intel_df` is the 1-minute frame, so this bit 09:15-09:19 EVERY session. If the 1-minute
frame is empty the aggregator falls back to the 5-minute frame and the dead window
stretches past the 09:20 entry cutoff. Exits and the loss guard read the same diluted
score.

The aggregator also relabelled the result's `source` to "ohlcv_close_position" in
`candle_pressure`, erasing the absence marker for anything reading that instead.

The gate abstains on POSITIVE evidence of absence rather than requiring an opt-in. A
strict `is True` — the form the other three pillars use — silently stopped every existing
stub and collaborator from contributing, which is the same "stricter contract than
collaborators honour" failure this codebase keeps hitting.
"""
import pandas as pd
import pytest

from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.order_flow import OrderFlowAnalyzer


# ── the analyzer publishes its availability ──────────────────────────────────

def test_an_empty_reading_is_marked_unavailable():
    assert OrderFlowAnalyzer()._empty_result().get("available") is False


def test_too_few_bars_is_unavailable_not_neutral():
    """Its own rule: fewer than five live bars must mean 'no reading'."""
    df = pd.DataFrame(
        {"open": [1.0] * 3, "high": [2.0] * 3, "low": [0.5] * 3,
         "close": [1.5] * 3, "volume": [10] * 3},
        index=pd.date_range("2026-08-05 09:15", periods=3, freq="1min"),
    )
    assert OrderFlowAnalyzer().analyze(df).get("available") is False


def test_a_real_reading_is_marked_available():
    df = pd.DataFrame(
        {"open": [1.0] * 30, "high": [2.0] * 30, "low": [0.5] * 30,
         "close": [1.5] * 30, "volume": [10] * 30},
        index=pd.date_range("2026-08-05 09:15", periods=30, freq="1min"),
    )
    assert OrderFlowAnalyzer().analyze(df).get("available") is True


def test_zero_volume_ohlcv_without_depth_is_not_order_flow():
    df = pd.DataFrame(
        {"open": [1.0] * 30, "high": [2.0] * 30, "low": [0.5] * 30,
         "close": [1.5] * 30, "volume": [0.0] * 30},
        index=pd.date_range("2026-08-05 09:15", periods=30, freq="1min"),
    )
    result = OrderFlowAnalyzer().analyze(df)
    assert result.get("available") is False
    assert result.get("source") == "unavailable"


# ── the aggregator abstains ──────────────────────────────────────────────────

def _aggregate(flow):
    from types import SimpleNamespace

    agg = IntelligenceAggregator()
    agg.volume = SimpleNamespace(analyze=lambda *a, **k: {
        "signal": "NEUTRAL", "buy_sell_ratio": 1.0, "current_volume": 1000})
    agg.regime = SimpleNamespace(detect=lambda *a, **k: {"regime": "TRENDING_UP"})
    agg.order_flow = SimpleNamespace(analyze=lambda *a, **k: flow)
    idx = pd.date_range("2026-08-05 09:15", periods=60, freq="5min")
    df = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0},
        index=idx,
    )
    return agg.analyze(
        instrument="NIFTY", timeframe="5min", candle_df=df, options_chain=None,
        spot_price=100.0, price_change_pct=0.1,
    )["aggregate"]


def test_an_unavailable_reading_casts_no_vote():
    got = _aggregate({"signal": "BULLISH", "ratio": 1.5, "available": False})
    baseline = _aggregate({"signal": "NEUTRAL", "ratio": 1.0, "available": False})
    assert got["score_before_regime_dampening"] == baseline["score_before_regime_dampening"]


def test_an_unavailable_source_marker_also_abstains():
    """Robust to a future early return that forgets the flag."""
    got = _aggregate({"signal": "BULLISH", "ratio": 1.5, "source": "unavailable"})
    baseline = _aggregate({"signal": "NEUTRAL", "ratio": 1.0, "source": "unavailable"})
    assert got["score_before_regime_dampening"] == baseline["score_before_regime_dampening"]


def test_an_available_reading_still_votes():
    bullish = _aggregate({"signal": "BULLISH", "ratio": 1.5, "available": True})
    neutral = _aggregate({"signal": "NEUTRAL", "ratio": 1.0, "available": True})
    assert bullish["score_before_regime_dampening"] > neutral["score_before_regime_dampening"]


def test_a_collaborator_without_the_key_is_not_silently_dropped():
    """The trap: a strict `is True` stops every existing stub from contributing."""
    bullish = _aggregate({"signal": "BULLISH", "ratio": 1.5})
    neutral = _aggregate({"signal": "NEUTRAL", "ratio": 1.0})
    assert bullish["score_before_regime_dampening"] > neutral["score_before_regime_dampening"]


# ── the absence marker is not overwritten ────────────────────────────────────

def test_candle_pressure_does_not_relabel_an_absent_reading():
    from types import SimpleNamespace

    agg = IntelligenceAggregator()
    agg.volume = SimpleNamespace(analyze=lambda *a, **k: {
        "signal": "NEUTRAL", "buy_sell_ratio": 1.0, "current_volume": 1000})
    agg.regime = SimpleNamespace(detect=lambda *a, **k: {"regime": "TRENDING_UP"})
    agg.order_flow = SimpleNamespace(analyze=lambda *a, **k: OrderFlowAnalyzer()._empty_result())
    idx = pd.date_range("2026-08-05 09:15", periods=60, freq="5min")
    df = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0},
        index=idx,
    )
    res = agg.analyze(
        instrument="NIFTY", timeframe="5min", candle_df=df, options_chain=None,
        spot_price=100.0, price_change_pct=0.1,
    )
    assert res["candle_pressure"]["source"] == "unavailable", (
        "an absent reading was relabelled as a real ohlcv_close_position measurement"
    )


# ── the sibling contract this was aligned to ─────────────────────────────────

def test_the_other_three_pillars_still_gate_their_component():
    import inspect

    src = inspect.getsource(IntelligenceAggregator.analyze)
    for marker in ('vol.get("available") is True',
                   'oi_result.get("available") is True',
                   'pcr_result.get("available") is True'):
        assert marker in src, f"{marker} disappeared — re-read this module's reasoning"
