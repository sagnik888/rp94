"""Warmup stance seed: UTBot's escape hatch for the no-crossover window.

A UT1 flip needs price to cross the ATR trail. When the ATR warm-up parks the
trail on the wrong side of a market that only ever ran one way from the first
bars, no crossover can happen and the engine sits FLAT through a full trend.
The seed emits ONE synthetic, trend-confirmed entry per session+direction.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from config.settings import get_settings
from engine.ut_bot_core import UTBotEngine


def _one_way_down(rows: int = 70, start_price: float = 25000.0) -> pd.DataFrame:
    """Strictly falling session: monotonic decline, no crossover possible."""
    start = datetime(2026, 7, 1, 9, 15)
    idx = [start + timedelta(minutes=5 * i) for i in range(rows)]
    close = start_price - 8.0 * np.arange(rows)
    open_ = np.roll(close, 1)
    open_[0] = close[0] + 8.0
    high = np.maximum(open_, close) + 2.0
    low = np.minimum(open_, close) - 2.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 50000.0},
        index=idx,
    )


def _engine(**overrides) -> UTBotEngine:
    params = dict(
        key_value=1.0,
        atr_period=10,
        use_heikin_ashi=False,
        signal_mode="realtime",
        adx_filter=True,
        adx_period=14,
        adx_threshold=25.0,
        strict_adx=False,
        session_filter=True,
        session_start="09:18",
        session_end="15:25",
    )
    params.update(overrides)
    return UTBotEngine(**params)


def test_seed_fires_sell_on_no_crossover_downtrend():
    df = _one_way_down()
    engine = _engine()
    result = engine.process(df=df, instrument="NIFTY", timeframe="5min",
                            capital=500000, risk_pct=1.0, lots=1, lot_size=65)
    signals = result["signals"]
    assert len(signals) == 1
    seed = signals[0]
    assert seed.signal_type == "SELL"
    assert getattr(seed, "warmup_seed", False) is True
    assert seed.bar_index == len(df) - 1
    # Protective stop is the trail, already on the correct (upper) side.
    assert seed.trailing_stop > seed.price
    assert seed.suggested_qty >= 65
    assert result["state"]["position"] == -1


def test_seed_is_one_shot_per_session_and_direction():
    df = _one_way_down()
    engine = _engine()
    first = engine.process(df=df, instrument="NIFTY", timeframe="5min",
                           capital=500000, risk_pct=1.0, lots=1, lot_size=65)
    assert len(first["signals"]) == 1

    # Next scan: one more falling bar. No second seed, but stance state holds.
    nxt = df.index[-1] + timedelta(minutes=5)
    df2 = pd.concat([df, pd.DataFrame(
        {"open": [df["close"].iloc[-1]], "high": [df["close"].iloc[-1] + 2],
         "low": [df["close"].iloc[-1] - 10], "close": [df["close"].iloc[-1] - 8],
         "volume": [50000.0]}, index=[nxt])])
    second = engine.process(df=df2, instrument="NIFTY", timeframe="5min",
                            capital=500000, risk_pct=1.0, lots=1, lot_size=65)
    assert len(second["signals"]) == 0
    assert second["state"]["position"] == -1


def test_seed_respects_disable_toggle():
    settings = get_settings()
    previous = settings.utbot_warmup_seed_enabled
    settings.utbot_warmup_seed_enabled = False
    try:
        result = _engine().process(df=_one_way_down(), instrument="NIFTY",
                                   timeframe="5min", capital=500000,
                                   risk_pct=1.0, lots=1, lot_size=65)
    finally:
        settings.utbot_warmup_seed_enabled = previous
    assert result["signals"] == []
    assert result["state"]["position"] == 0


def test_seed_requires_trend_strength():
    settings = get_settings()
    previous = settings.utbot_warmup_seed_min_adx
    # ADX caps at 100 (and approaches it on a monotonic series) — a floor above
    # the cap must always veto the seed.
    settings.utbot_warmup_seed_min_adx = 150.0
    try:
        result = _engine().process(df=_one_way_down(), instrument="NIFTY",
                                   timeframe="5min", capital=500000,
                                   risk_pct=1.0, lots=1, lot_size=65)
    finally:
        settings.utbot_warmup_seed_min_adx = previous
    assert result["signals"] == []


def test_seed_never_fires_when_flips_exist():
    """Normal two-way data flips on its own; the seed must stay out of the way."""
    rng = np.random.default_rng(3)
    rows = 120
    start = datetime(2026, 7, 1, 9, 15)
    idx = [start + timedelta(minutes=5 * i) for i in range(rows)]
    close = 25000 + 80 * np.sin(np.linspace(0, 12, rows)) + rng.normal(0, 10, rows)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    df = pd.DataFrame(
        {"open": open_, "high": np.maximum(open_, close) + 5,
         "low": np.minimum(open_, close) - 5, "close": close, "volume": 50000.0},
        index=idx,
    )
    result = _engine().process(df=df, instrument="NIFTY", timeframe="5min",
                               capital=500000, risk_pct=1.0, lots=1, lot_size=65)
    assert all(not getattr(sig, "warmup_seed", False) for sig in result["signals"])
    assert len(result["signals"]) > 0
