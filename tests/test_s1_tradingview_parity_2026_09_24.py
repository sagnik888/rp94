"""S1 must signal on the bars the user's TradingView chart signals.

2026-09-24, NIFTY 5m, Heikin Ashi. The user's chart printed:
    SELL 11:10  CONF 68  ENTRY 23219.75  SL1 23266.20 (0.20%)  SL2 23312.60 (0.40%)
        EXIT SHORT TSL (Profit Locked) on the 13:25 bar
    SELL 13:40  CONF 98  ENTRY 23123.40  SL1 23169.60           SL2 23215.85
        EXIT SHORT TSL (Profit Locked) on the 13:55 bar
Our engine printed SELL 11:35 / 13:40 / 13:45 / 13:55 / 14:20 instead, for two reasons:
  1. The feed keeps 15:15-15:29 bars every session (frozen, then the post-close price
     with bulk volume). TradingView's chart ends at 15:15. The junk bars moved EMA/RSI/
     volume averages and the Heikin Ashi chain, and even opened a fake 15:25 trade whose
     loss raised the next day's score floor from 60 to 68 - just above the 11:10 bar.
  2. The chart state machine ran on the 0.10% live SL instead of the chart's 0.20%, so
     the 13:40 trade stopped out inside its own bar and extra SELLs followed.
The fixture is the live store's frame for that day, junk bars included.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.alt_scanners import AltSignalEngine
from engine.s1_engine import S1Params, _pine_levels, drop_off_session_bars

FIXTURE = Path(__file__).parent / "fixtures" / "s1_nifty_5min_2026_09_24.csv"


def _fixture():
    return pd.read_csv(FIXTURE, parse_dates=["timestamp"]).set_index("timestamp")


def _day_signals(df):
    res = AltSignalEngine(signal_source="s1").process(df, instrument="NIFTY", timeframe="5min")
    day = pd.Timestamp("2026-09-24").date()
    return res, [s for s in res["signals"] if s.timestamp.date() == day]


def test_nifty_5m_matches_the_users_tradingview_chart(monkeypatch):
    from config import settings as settings_module

    fresh = settings_module.Settings()
    for k, v in dict(s1_candle_type="heikin_ashi", s1_chart_sl1_pct=0.20, s1_chart_sl2_pct=0.40,
                     s1_session_end="15:15", s1_min_score=60.0, s1_signal_cooldown=0,
                     s1_max_consec_loss_nifty=3, s1_circuit_pause=5).items():
        object.__setattr__(fresh, k, v)
    monkeypatch.setattr(settings_module, "get_settings", lambda: fresh)
    _, sigs = _day_signals(_fixture())
    got = [(f"{s.timestamp:%H:%M}", s.signal_type, round(s.s1_score),
            f"{s.s1_exit_time:%H:%M}" if s.s1_exit_time else None, s.s1_exit_reason) for s in sigs]
    assert got[0] == ("11:10", "SELL", 68, "13:25", "TSL (Profit Locked)")
    assert got[1] == ("13:40", "SELL", 98, "13:55", "TSL (Profit Locked)")
    # The chart label ENTRY is the Heikin Ashi close of the signal bar.
    assert sigs[0].s1_chart_signal_close == pytest.approx(23219.75, abs=0.05)
    assert sigs[1].s1_chart_signal_close == pytest.approx(23123.40, abs=0.05)
    # The chart SL1 on the label is 0.20% of that close (the trade's own SL uses its open).
    assert sigs[1].s1_chart_signal_close * 1.002 == pytest.approx(23169.60, abs=0.1)


def test_the_junk_session_end_bars_alone_hid_the_1110_signal(monkeypatch):
    from config import settings as settings_module

    fresh = settings_module.Settings()
    for k, v in dict(s1_candle_type="heikin_ashi", s1_chart_sl1_pct=0.20, s1_chart_sl2_pct=0.40,
                     s1_session_end="15:30", s1_max_consec_loss_nifty=3, s1_circuit_pause=5).items():
        object.__setattr__(fresh, k, v)
    monkeypatch.setattr(settings_module, "get_settings", lambda: fresh)
    _, sigs = _day_signals(_fixture())
    assert "11:10" not in [f"{s.timestamp:%H:%M}" for s in sigs]


def test_outputs_stay_aligned_to_the_callers_candles():
    df = _fixture()
    res, sigs = _day_signals(df)
    assert len(res["positions"]) == len(res["trailing_stop"]) == len(df)
    for ov in res["indicator_overlays"]:
        assert len(ov["values"]) == len(df)
    for s in res["signals"]:
        assert df.index[s.bar_index] == pd.Timestamp(s.timestamp)
    after = df.index.indexer_between_time("15:15", "15:29")
    assert not any(s.bar_index in set(after) for s in res["signals"])


def test_drop_off_session_bars():
    df = _fixture()
    kept = drop_off_session_bars(df, "15:15")
    minutes = kept.index.hour * 60 + kept.index.minute
    assert minutes.max() == 15 * 60 + 10 and minutes.min() == 9 * 60 + 15
    raw = df.index.hour * 60 + df.index.minute
    assert len(df) - len(kept) == int(((raw < 555) | (raw >= 915)).sum()) > 0


def test_the_chart_state_machine_uses_the_chart_stops_not_the_live_stop():
    p = S1Params(sl1_pct=0.10, chart_sl1_pct=0.20, chart_sl2_pct=0.40)
    sl1, sl2, targets, rk = _pine_levels(p, 23000.0, -1)
    assert sl1 == pytest.approx(23000.0 * 1.002) and sl2 == pytest.approx(23000.0 * 1.004)
    assert targets[0] == pytest.approx(23000.0 - 2 * rk)
