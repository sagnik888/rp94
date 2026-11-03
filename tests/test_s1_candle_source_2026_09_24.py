"""S1 must read the same chart type as the reference Pine indicator."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from engine.s1_engine import (
    S1Params, _ema, _rsi, compute_features, get_s1_signals,
    s1_candle_frame, simulate,
)
from tests.test_s1_engine_2026_09_23 import _frame


def _aggregate(frame, minutes):
    return frame.resample(f"{minutes}min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna()


def test_heikin_ashi_matches_manual_recurrence_and_preserves_market_frame():
    raw = pd.DataFrame({"open": [100., 103., 101.], "high": [105., 107., 104.],
                        "low": [98., 99., 96.], "close": [104., 100., 97.],
                        "volume": [10., 20., 30.]})
    before = raw.copy(deep=True)
    ha = s1_candle_frame(raw)
    np.testing.assert_allclose(ha["close"], [101.75, 102.25, 99.5])
    np.testing.assert_allclose(ha["open"], [102., 101.875, 102.0625])
    np.testing.assert_allclose(ha["high"], [105., 107., 104.])
    np.testing.assert_allclose(ha["low"], [98., 99., 96.])
    pd.testing.assert_frame_equal(raw, before)
    pd.testing.assert_series_equal(ha["volume"], raw["volume"])
    pd.testing.assert_frame_equal(s1_candle_frame(raw, "standard"), raw)
    pd.testing.assert_frame_equal(s1_candle_frame(raw.iloc[:0]), raw.iloc[:0])
    with pytest.raises(ValueError, match="candle type"):
        s1_candle_frame(raw, "unknown")


@pytest.mark.parametrize("kind", ["standard", "heikin_ashi"])
def test_current_features_use_selected_ohlc(kind):
    raw = _frame()
    p = S1Params(candle_type=kind)
    f = compute_features(raw, "5min", p)
    expected = s1_candle_frame(raw, kind)
    for key in ("open", "high", "low", "close"):
        np.testing.assert_allclose(f[key], expected[key])
    np.testing.assert_allclose(f["rsi"], _rsi(expected["close"]), equal_nan=True)


def test_five_minute_chart_requests_independently_constructed_ha_fifteen():
    raw5 = _frame()
    raw15 = _aggregate(raw5, 15)
    f = compute_features(raw5, "5min", df_15min=raw15)
    stamp = pd.Timestamp("2026-08-06 10:00")
    row = raw5.index.get_loc(stamp)
    expected = _ema(s1_candle_frame(raw15)["close"], 9).loc[stamp - pd.Timedelta(minutes=15)]
    wrong = _ema(s1_candle_frame(raw5)["close"].resample("15min").last().dropna(), 9).loc[stamp - pd.Timedelta(minutes=15)]
    assert f["ema9_15"][row] == pytest.approx(expected)
    assert expected != pytest.approx(wrong)


def test_raw_aggregation_fallback_matches_independent_frame_and_seals_incomplete_buckets():
    raw5 = _frame()
    raw15 = _aggregate(raw5, 15)
    a = compute_features(raw5, "5min")
    b = compute_features(raw5, "5min", df_15min=raw15)
    np.testing.assert_allclose(a["ema9_15"], b["ema9_15"], equal_nan=True)
    # The frame ends halfway through a target bar; neither full nor prefix computation
    # may use that incomplete bar before its closing time.
    cut = raw5.index.get_loc(pd.Timestamp("2026-08-06 10:05")) + 1
    short = compute_features(raw5.iloc[:cut], "5min")
    np.testing.assert_allclose(short["ema9_15"], a["ema9_15"][:cut], equal_nan=True)
    partial = raw5.drop(pd.Timestamp("2026-08-06 09:35"))
    short = compute_features(partial, "5min")
    row = partial.index.get_loc(pd.Timestamp("2026-08-06 09:45"))
    prev = partial.index.get_loc(pd.Timestamp("2026-08-06 09:30"))
    assert short["ema9_15"][row] == short["ema9_15"][prev]


def test_fifteen_minute_chart_requests_ha_five_minute_rsi_at_first_intrabar():
    raw5 = _frame()
    raw15 = _aggregate(raw5, 15)
    f = compute_features(raw15, "15min", df_5min=raw5)
    stamp = pd.Timestamp("2026-08-06 10:00")
    row = raw15.index.get_loc(stamp)
    expected = _rsi(s1_candle_frame(raw5)["close"]).loc[stamp - pd.Timedelta(minutes=5)]
    assert f["rsi5m"][row] == pytest.approx(expected)
    # A provider returning newest-first must not run the HA recurrence backward.
    reverse = compute_features(raw15, "15min", df_5min=raw5.iloc[::-1])
    np.testing.assert_allclose(f["rsi5m"], reverse["rsi5m"], equal_nan=True)


def test_one_minute_chart_builds_both_requested_timeframes_from_raw_ohlc():
    raw1 = _frame(sessions=2, tf=1)
    raw5, raw15 = _aggregate(raw1, 5), _aggregate(raw1, 15)
    f = compute_features(raw1, "1min", df_5min=raw5, df_15min=raw15)
    stamp = pd.Timestamp("2026-08-04 10:03")
    row = raw1.index.get_loc(stamp)
    assert f["rsi5m"][row] == pytest.approx(_rsi(s1_candle_frame(raw5)["close"]).loc["2026-08-04 09:55"])
    assert f["ema9_15"][row] == pytest.approx(_ema(s1_candle_frame(raw15)["close"], 9).loc["2026-08-04 09:45"])


def test_executable_prices_and_fixed_risk_stay_raw_while_chart_state_is_ha():
    raw = _frame(11)
    ha = s1_candle_frame(raw)
    signals, _, _ = get_s1_signals(raw, "NIFTY", "5min", S1Params(sl_mode="fixed", sl1_pct=0.2,
                                                               sl1_pct_5min=0.0, sl1_pct_15min=0.0))
    assert signals
    assert any(s.price != s.s1_chart_signal_close for s in signals)
    for s in signals:
        i, d = s.bar_index, s.position_state
        assert s.price == raw["close"].iloc[i]
        assert s.s1_chart_entry == ha["open"].iloc[i]
        assert s.s1_chart_signal_close == ha["close"].iloc[i]
        assert s.s1_sl1 == pytest.approx(s.price * (1 - d * .002))
        assert s.s1_targets == pytest.approx([s.price * (1 + d * .002 * r) for r in (2, 3, 4)])


def test_close_execution_mode_has_market_entries_and_market_candle_stop_breaches():
    raw = _frame(11)
    p = S1Params(entry_mode="close", sl1_pct=.05, use_trail=False)
    result = simulate(raw, "5min", p)
    assert result.trades
    for t in result.trades:
        assert t.entry_price == raw["close"].iloc[t.entry_bar]
        assert t.exit_bar < 0 or t.exit_bar > t.entry_bar
        if t.exit_reason == "Hard SL1":
            b = raw.iloc[t.exit_bar]
            if t.direction == 1:
                assert b["low"] <= t.sl1
                assert t.exit_price == min(b["open"], t.sl1)
            else:
                assert b["high"] >= t.sl1
                assert t.exit_price == max(b["open"], t.sl1)
