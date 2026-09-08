from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from engine.ut_bot_core import UTBotEngine
from intelligence.regime_detector import RegimeDetector
from intelligence.rtracker import RegimeTracker, VECTOR_KEYS


def _trend_df(rows=70):
    values = [100.0 + i * 0.5 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": [v - 0.1 for v in values],
            "high": [v + 0.4 for v in values],
            "low": [v - 0.4 for v in values],
            "close": values,
            "volume": [10000 + i * 20 for i in range(rows)],
        },
        index=pd.date_range("2026-07-08 09:15", periods=rows, freq="5min"),
    )


def test_regime_detector_returns_conviction_vector(monkeypatch):
    def fake_adx(df, period=14):
        series = pd.Series(24.0, index=df.index)
        return series, series, series

    def fake_atr(df, period=14):
        return pd.Series(0.5, index=df.index)

    monkeypatch.setattr(UTBotEngine, "calculate_adx", staticmethod(fake_adx))
    monkeypatch.setattr(UTBotEngine, "calculate_atr", staticmethod(fake_atr))

    result = RegimeDetector().detect(
        _trend_df(),
        "NIFTY",
        "5min",
        session_context={
            "day_type": "TREND_DAY_UP",
            "confidence": 0.76,
            "vwap_cross_count": 1,
        },
        breadth_context={"bias": "BULLISH", "aligned_count": 3},
    )

    vector = result["conviction_vector"]
    assert set(VECTOR_KEYS).issubset(vector)
    assert result["raw_regime"]
    assert result["guarded_regime"]
    assert result["smoothed_regime"] == result["regime"]
    assert result["conviction_label"]
    assert "leads" in result["conviction_summary"]
    assert "label" in result["description"]


def test_rtracker_records_candidate_and_generates_weekly_report(tmp_path):
    vector = {
        "trend_up_prob": 0.72,
        "trend_down_prob": 0.05,
        "range_prob": 0.22,
        "breakout_prob": 0.30,
        "reversal_prob": 0.10,
        "volatility_expansion_prob": 0.40,
        "breadth_stress_prob": 0.20,
    }
    ctx = {
        "regime": {
            "regime": "TRENDING_UP",
            "raw_regime": "TRENDING_UP",
            "guarded_regime": "TRENDING_UP",
            "smoothed_regime": "TRENDING_UP",
            "confidence": 72,
            "conviction_label": "Trend Up",
            "conviction_summary": "Trend Up leads (72%)",
            "conviction_vector": vector,
            "adx": 24.0,
            "kaufman_er": 0.64,
            "atr_pct": 0.2,
            "bb_width": 1.4,
        },
        "session_context": {
            "day_type": "TREND_DAY_UP",
            "bias": "BULLISH",
            "confidence": 0.76,
            "vwap_cross_count": 1,
            "close_above_vwap_pct": 82.0,
        },
        "breadth_context": {"bias": "BULLISH", "aligned_count": 3},
        "volume": {"signal": "BULLISH_VOLUME", "current_volume": 50000, "buy_sell_ratio": 1.4},
        "aggregate": {"score": 62.0, "signal": "BUY", "regime_score_multiplier": 1.0},
    }
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        source_signal_type="BUY",
        signal_timestamp=datetime(2026, 7, 8, 14, 50),
        entry_timestamp=datetime(2026, 7, 8, 14, 51),
        action="ENTRY",
        status="TRADE SIGNAL",
        accepted_by_gate=True,
        grade="A",
        confidence=0.82,
        price=24450.0,
        stop=24430.0,
        target=24480.0,
        current_price=24455.0,
        pnl=0.0,
        peak_pnl=0.0,
        max_drawdown=0.0,
        market_regime="TRENDING_UP",
        market_intelligence_context=ctx,
        reasons=["test accepted"],
    )

    tracker = RegimeTracker(tmp_path, enabled=True, max_weeks=8)
    assert tracker.record_candidate(candidate, decision="ACCEPTED", source="unit")

    candidate.action = "EXIT"
    candidate.status = "EXIT SIGNAL"
    candidate.exit_timestamp = datetime(2026, 7, 8, 15, 5)
    candidate.exit_price = 24478.0
    candidate.current_price = 24478.0
    candidate.pnl = 1250.0
    candidate.exit_reason = "TARGET_HIT"
    assert tracker.record_candidate(candidate, decision="EXIT", source="unit")

    result = tracker.generate_weekly_reports(weeks=1, as_of=datetime(2026, 7, 8, 16, 0))
    assert result["events"] == 2
    assert result["weekly_reports"]
    assert (tmp_path / "reports" / "2026-W28.json").exists()
    assert (tmp_path / "reports" / "rtracker_1_week_summary.md").exists()


def test_rtracker_promotes_run_mode_and_evidence_domain_to_top_level(tmp_path):
    tracker = RegimeTracker(tmp_path, enabled=True, max_weeks=1)
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        signal_timestamp=datetime(2026, 7, 8, 10, 0),
    )

    event = tracker._event_from_candidate(
        candidate,
        decision="REJECTED",
        reason="test",
        source="unit",
        extra={"run_mode": "REAL", "evidence_domain": "LIVE_DECISION"},
    )

    assert event["run_mode"] == "REAL"
    assert event["evidence_domain"] == "LIVE_DECISION"
