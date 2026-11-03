"""Independent numeric and integration checks for the S1 parity repair."""
import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from engine.alt_scanners import AltSignalEngine
from engine.s1_engine import S1Params, _dmi, _rma, _rsi, compute_features, get_s1_signals
from tests.test_s1_engine_2026_09_23 import _frame


def test_wilder_seed_uses_the_first_full_average_and_skips_missing_values():
    result = _rma(pd.Series([np.nan, 1., 2., 3., 6., np.nan, 9.]), 3)
    np.testing.assert_allclose(result, [np.nan, np.nan, np.nan, 2., 10/3, 10/3, 47/9], equal_nan=True)
    assert _rsi(pd.Series([1., 2., 3., 2.]), 3).iloc[-1] == pytest.approx(200/3)
    assert _rsi(pd.Series([1., 2., 3., 2.]), 3).iloc[:3].isna().all()


def test_dmi_waits_for_valid_changes_then_adx_smoothing():
    h = pd.Series(np.arange(10., 30., 2.))
    plus, minus, adx = _dmi(h, h - 2, h - 1, 3)
    assert plus.first_valid_index() == 3
    assert adx.first_valid_index() == 5
    assert plus.iloc[3] == pytest.approx(200/3)
    assert minus.iloc[3] == 0
    assert adx.iloc[5] == pytest.approx(100.)


def _fifteen(five):
    return five.resample('15min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()


def test_historical_five_minute_rsi_does_not_read_later_intrabars():
    five = _frame()
    fifteen = _fifteen(five)
    ts = pd.Timestamp('2026-08-06 10:00')
    row = fifteen.index.get_loc(ts)
    before = compute_features(fifteen, '15min', df_5min=five)['rsi5m'][row]
    five.loc[ts:ts + pd.Timedelta(minutes=10), 'close'] += 500
    after = compute_features(fifteen, '15min', df_5min=five)['rsi5m'][row]
    assert after == before


def test_utc_and_ist_five_minute_data_map_to_identical_candles():
    five = _frame()
    fifteen = _fifteen(five)
    utc = five.tz_localize('Asia/Kolkata').tz_convert('UTC')
    np.testing.assert_allclose(
        compute_features(fifteen, '15min', df_5min=utc)['rsi5m'],
        compute_features(fifteen, '15min', df_5min=five)['rsi5m'], equal_nan=True)


def test_missing_lower_timeframe_data_fails_instead_of_using_fifteen_minute_rsi():
    with pytest.raises(ValueError, match='actual 5min'):
        AltSignalEngine(signal_source='s1').process(_fifteen(_frame()), 'NIFTY', '15min')


def test_s1_failure_cannot_publish_utbot_arrows(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('S1 failed')
    monkeypatch.setattr('engine.alt_scanners.compute_features', fail)
    with pytest.raises(RuntimeError, match='S1 failed'):
        AltSignalEngine(signal_source='s1').process(_frame(), 'NIFTY', '5min')


def test_chart_arrays_are_s1_and_do_not_change_with_ut_parameters():
    from engine.s1_engine import drop_off_session_bars

    df = drop_off_session_bars(_frame(11))   # the live path computes on session bars only
    a = AltSignalEngine(signal_source='s1', key_value=1, atr_period=10).process(df, 'NIFTY', '5min')
    b = AltSignalEngine(signal_source='s1', key_value=7, atr_period=30).process(df, 'NIFTY', '5min')
    _, _, stops = get_s1_signals(df, 'NIFTY', '5min')
    f = compute_features(df, '5min')
    np.testing.assert_allclose(np.array(a['trailing_stop'], dtype=float), stops, equal_nan=True)
    for key in ('atr', 'adx', 'di_plus', 'di_minus'):
        np.testing.assert_allclose(np.array(a[key], dtype=float), f[key], equal_nan=True)
    assert a['state'] == b['state']
    assert a['indicator_overlays'] == b['indicator_overlays']
    assert {v['name'] for v in a['indicator_overlays']} == {'S1 EMA 9', 'S1 EMA 21', 'S1 EMA 50', 'S1 EMA 200', 'S1 VWAP'}


def test_confirmed_mode_cannot_publish_a_forming_bar_entry_or_exit():
    df = _frame(11)
    sigs, _, _ = get_s1_signals(df, 'NIFTY', '5min')
    assert sigs
    cut = sigs[0].bar_index + 1
    result = AltSignalEngine(signal_source='s1', signal_mode='confirmed').process(df.iloc[:cut], 'NIFTY', '5min')
    assert all(s.timestamp < df.index[cut - 1] for s in result['signals'])
    assert all(s.s1_exit_time is None or s.s1_exit_time < df.index[cut - 1] for s in result['signals'])


def test_core_settings_and_signals_have_no_forecast_dependency():
    assert 's1_use_forecast_filter' not in Settings.model_fields
    assert 'use_forecast_filter' not in S1Params.__dataclass_fields__
    p = S1Params()
    # 2026-09-24 user: live SL1 is dynamic 0.10-0.25% (anchor); sl1_pct is the chart's fixed SL.
    assert (p.max_consec_loss, p.circuit_pause, p.sl_mode) == (3, 5, 'anchor')
    df = _frame()
    assert not any('forecast' in k for k in compute_features(df, '5min'))
    for sig in get_s1_signals(df, 'NIFTY', '5min')[0]:
        assert not hasattr(sig, 's1_forecast_dir')
        assert p.sl_min_pct - 1e-9 <= sig.s1_sl_pct <= p.sl_max_pct + 1e-9
        assert sig.s1_chart_sl1 == pytest.approx(sig.s1_chart_entry * (1 - sig.position_state * p.chart_sl1_pct / 100))
