"""Offline feasibility proof; imports only the removable report prototype."""
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from config.settings import get_settings
from engine.alt_scanners import AltSignalEngine, build_scanner_engine
from engine.multi_timeframe import MultiTimeframeEngine
from engine import s1_lane
from reports.s1_composite_trial_20261004.companion import S1CompanionTrial
from reports.s1_composite_trial_20261004.companion import temporary_scanner_hooks
from reports.s1_composite_trial_20261004.companion import native_entry_settings
from tests.test_s1_engine_2026_09_23 import _frame
from tests.test_ut_smart_live_path_2026_09_26 import live

MODES = ('utbot', 'ema', 'consensus', 'utbot_consensus', 'quad_union',
         'quad_prime', 'adaptive_dual', 's1', 'ut_smart')
INDICES = ('NIFTY', 'BANKNIFTY', 'SENSEX', 'MIDCPNIFTY')
TFS = ('1min', '5min', '15min')


def signature(result):
    return json.dumps({
        'signals': [vars(s) for s in result.get('signals', [])],
        'positions': result.get('positions'),
        'stops': result.get('trailing_stop'),
        'state': result.get('state'),
        'overlays': result.get('indicator_overlays'),
    }, default=lambda x: x.tolist() if isinstance(x, np.ndarray) else str(x),
        sort_keys=True, allow_nan=True)


@pytest.fixture(scope='module')
def frames():
    return {tf: _frame(sessions=4, tf=int(tf.replace('min', ''))) for tf in TFS}


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('instrument', INDICES)
@pytest.mark.parametrize('tf', TFS)
def test_all_supported_modes_execute_native_interface(monkeypatch, frames, mode, instrument, tf):
    monkeypatch.setattr(get_settings(), 'scanner_engine', mode)
    engine = build_scanner_engine({'signal_mode': 'realtime'})
    extra = dict(df_5min=frames['5min'], df_15min=frames['15min']) if mode == 's1' else {}
    result = engine.process(frames[tf], instrument=instrument, timeframe=tf, **extra)
    assert not result.get('error')
    assert isinstance(result['signals'], list)
    assert result['state'] is not None
    assert len(result['positions']) == len(frames[tf])
    assert all(s.instrument == instrument and s.timeframe == tf for s in result['signals'])


@pytest.mark.parametrize('mode', MODES)
def test_mode_switch_rebuilds_all_twelve_engines_and_discards_old_cache(monkeypatch, mode):
    monkeypatch.setattr(get_settings(), 'scanner_engine', 'utbot')
    pipeline = MultiTimeframeEngine()
    previous = dict(pipeline.engines)
    pipeline._result_cache['NIFTY_5min'] = {'signals': ['obsolete']}
    pipeline._data_hash['NIFTY_5min'] = 1
    assert pipeline.set_scanner_engine(mode) == mode
    assert get_settings().scanner_engine == mode
    assert not pipeline._result_cache and not pipeline._data_hash
    assert len(pipeline.engines) == 12
    for key, engine in pipeline.engines.items():
        assert engine is not previous[key]
        assert getattr(engine, 'signal_source', 'utbot') == mode


@pytest.mark.parametrize('base', ['quad_prime', 'ut_smart'])
@pytest.mark.parametrize('instrument', INDICES)
@pytest.mark.parametrize('fire', ['confirmed', 'realtime'])
def test_companion_matches_both_native_engines_exactly(frames, base, instrument, fire):
    trial = S1CompanionTrial(base, {'signal_mode': fire})
    for tf, frame in frames.items():
        trial.update_data(instrument, tf, frame)
    before = {tf: f.copy(deep=True) for tf, f in frames.items()}
    combined = trial.process_instrument(instrument)
    for mode, view in combined.entry_views():
        for tf in TFS:
            engine = AltSignalEngine(signal_source=mode,
                signal_mode='realtime' if tf == '1min' else fire)
            native = engine.process(frames[tf], instrument=instrument, timeframe=tf,
                df_5min=frames['5min'], df_15min=frames['15min'],
                capital=100000., risk_pct=1., lots=1, lot_size=25)
            actual = getattr(view, f'results_{tf}')
            assert signature(actual) == signature(native)
        assert view.confluence_score == MultiTimeframeEngine._compute_confluence(
            trial.lanes[mode], {tf: getattr(view, f'results_{tf}') for tf in TFS}, instrument)[0]
    for tf in TFS:
        pd.testing.assert_frame_equal(frames[tf], before[tf])
    assert trial.lanes[base]._result_cache is not trial.lanes['s1']._result_cache


@pytest.mark.parametrize('base', ['quad_prime', 'ut_smart'])
def test_companion_withdraws_missing_input_and_rebuilds(frames, base):
    trial = S1CompanionTrial(base)
    for tf, frame in frames.items():
        trial.update_data('NIFTY', tf, frame)
    first = trial.process_instrument('NIFTY')
    assert first.base.results_5min and first.s1.results_5min
    trial.update_data('NIFTY', '5min', None)
    missing = trial.process_instrument('NIFTY')
    assert missing.base.results_5min is None and missing.s1.results_5min is None
    trial.update_data('NIFTY', '5min', frames['5min'])
    rebuilt = trial.process_instrument('NIFTY')
    assert signature(rebuilt.s1.results_5min) == signature(first.s1.results_5min)


@pytest.mark.parametrize('base', ['quad_prime', 'ut_smart'])
def test_failure_in_one_native_engine_does_not_serve_stale_arrows(frames, base, monkeypatch):
    trial = S1CompanionTrial(base)
    for tf, frame in frames.items():
        trial.update_data('NIFTY', tf, frame)
    trial.process_instrument('NIFTY')
    trial.lanes['s1']._data_hash.clear()
    trial.lanes['s1']._s1_light_signatures = {}
    def fail(*args, **kwargs):
        raise RuntimeError('injected engine failure')
    monkeypatch.setattr(trial.lanes['s1'].engines['NIFTY_5min'], 'process', fail)
    result = trial.process_instrument('NIFTY')
    assert result.base.results_5min is not None
    assert result.s1.results_5min is None
    assert result.s1.timeframe_metadata['5min']['status'] == 'processing_error'


@pytest.mark.parametrize('base', ['quad_prime', 'ut_smart'])
def test_native_exit_and_trend_use_the_owning_result(frames, base):
    trial = S1CompanionTrial(base)
    for tf, frame in frames.items():
        trial.update_data('NIFTY', tf, frame)
    result = trial.process_instrument('NIFTY')
    seen = 0
    for mode, view in result.entry_views():
        if mode == 'quad_prime':
            continue
        for tf in TFS:
            native = getattr(view, f'results_{tf}')
            for signal in native['signals']:
                trade = SimpleNamespace(source_engine=mode, source_lane='S1' if mode == 's1' else 'UTS',
                    timeframe=tf, direction='LONG' if signal.signal_type == 'BUY' else 'SHORT',
                    signal_time=signal.timestamp)
                own = result.result_for(trade)
                assert own is native
                assert s1_lane.engine_exit_for(trade, own) == s1_lane.engine_exit_for(trade, native)
                assert s1_lane.closed_bar_state(trade, own) == s1_lane.closed_bar_state(trade, native)
                seen += 1
    assert seen > 0, 'Fixture must exercise native lifecycle ownership'


@pytest.mark.parametrize('base', ['quad_prime', 'ut_smart'])
def test_native_parameter_change_invalidates_only_s1_cache(frames, base, monkeypatch):
    trial = S1CompanionTrial(base)
    for tf, frame in frames.items():
        trial.update_data('NIFTY', tf, frame)
    first = trial.process_instrument('NIFTY')
    monkeypatch.setattr(get_settings(), 's1_min_score', 100.)
    second = trial.process_instrument('NIFTY')
    assert second.base.results_5min is first.base.results_5min
    assert second.s1.results_5min is not first.s1.results_5min


def test_removal_requires_no_settings_or_schema_change():
    before = get_settings().model_dump()
    S1CompanionTrial('quad_prime')
    S1CompanionTrial('ut_smart')
    assert get_settings().model_dump() == before
    with pytest.raises(ValueError):
        S1CompanionTrial('s1')


def test_temporary_patch_protects_foreign_lane_without_disabling_native_reversal(live):
    h = live()
    opposite = 'LONG' if h.sig.signal_type == 'SELL' else 'SHORT'
    def position(key, mode, lane):
        return SimpleNamespace(id=key, instrument='NIFTY', timeframe='15min',
            direction=opposite, inst_type='OPT', status='OPEN', entry_price=140.,
            current_price=150., source_engine=mode, source_lane=lane,
            signal_time=h.sig.timestamp - pd.Timedelta(minutes=30),
            trading_symbol='NIFTYPREV', lots=1, lot_size=65)
    h.sc.trades.open_trades = {'S1': position('S1', 's1', 'S1'),
                              'UTS': position('UTS', 'ut_smart', 'UTS')}
    h.sc._get_live_premium_cached = lambda *a, **kw: 155.
    closed = []
    h.sc._close_and_record = lambda key, *a, **kw: closed.append(key)
    # Reproduce the unsafe direct merge before installing the removable patch.
    h.sc._handle_opposite_signal_exit('NIFTY', h.sig, '15min')
    assert set(closed) == {'S1', 'UTS'}
    closed.clear()
    h.sc._last_opposite_exit_signal_time.clear()
    original = h.sc._quad_union_opposite_exit_allowed
    with temporary_scanner_hooks(h.sc):
        h.sc._handle_opposite_signal_exit('NIFTY', h.sig, '15min')
        assert closed == ['UTS']
    assert h.sc._quad_union_opposite_exit_allowed == original


def test_temporary_patch_separates_same_bar_history_and_restores_on_exception(live):
    h = live()
    native = h.processor.process_best_signal(*h.args)[0]
    other = copy.copy(native)
    other.source_engine, other.source_lane = 's1', 'S1'
    assert h.sc._candidate_history_key(native) == h.sc._candidate_history_key(other)
    original = h.sc._candidate_history_key
    with pytest.raises(RuntimeError, match='test cleanup'):
        with temporary_scanner_hooks(h.sc):
            assert h.sc._candidate_history_key(native) != h.sc._candidate_history_key(other)
            raise RuntimeError('test cleanup')
    assert h.sc._candidate_history_key == original


@pytest.mark.parametrize('base', ['quad_prime', 'ut_smart'])
def test_patched_exit_readers_see_exact_native_evidence(frames, base, live):
    h = live()
    trial = S1CompanionTrial(base)
    for tf, frame in frames.items():
        trial.update_data('NIFTY', tf, frame)
    combined = trial.process_instrument('NIFTY')
    cache = combined.exit_cache()
    checked = 0
    for mode, view in combined.entry_views():
        if mode == 'quad_prime':
            continue
        for tf in TFS:
            native = getattr(view, f'results_{tf}')
            for signal in native['signals']:
                trade = SimpleNamespace(source_engine=mode, source_lane='S1' if mode == 's1' else 'UTS',
                    timeframe=tf, direction='LONG' if signal.signal_type == 'BUY' else 'SHORT',
                    signal_time=signal.timestamp)
                expected_exit = s1_lane.engine_exit_for(trade, native)
                expected_state = s1_lane.closed_bar_state(trade, native)
                with temporary_scanner_hooks(h.sc):
                    assert s1_lane.engine_exit_for(trade, cache[f'NIFTY_{tf}']) == expected_exit
                    assert s1_lane.closed_bar_state(trade, cache[f'NIFTY_{tf}']) == expected_state
                checked += 1
    assert checked > 0


@pytest.mark.parametrize('instrument', INDICES)
@pytest.mark.parametrize('base', ['quad_prime', 'ut_smart'])
def test_saved_market_candles_preserve_both_native_signal_and_exit_streams(instrument, base):
    root = Path(__file__).resolve().parents[1]
    saved = {tf: pd.read_csv(root / f'data_store/candles/{instrument}_{tf}.csv',
             index_col='timestamp', parse_dates=True).tail(900) for tf in TFS}
    trial = S1CompanionTrial(base, {'signal_mode': 'confirmed'})
    for tf, frame in saved.items():
        trial.update_data(instrument, tf, frame)
    combined = trial.process_instrument(instrument)
    seen = {'s1': 0, base: 0}
    for mode, view in combined.entry_views():
        for tf in TFS:
            native = AltSignalEngine(signal_source=mode,
                signal_mode='realtime' if tf == '1min' else 'confirmed').process(
                saved[tf], instrument=instrument, timeframe=tf,
                df_5min=saved['5min'], df_15min=saved['15min'],
                capital=100000., risk_pct=1., lots=1, lot_size=25)
            assert signature(getattr(view, f'results_{tf}')) == signature(native)
            seen[mode] += len(native['signals'])
    assert all(seen.values()), 'Actual saved candles must exercise both signal streams'


@pytest.mark.parametrize('base', ['quad_prime', 'ut_smart'])
@pytest.mark.parametrize('tf', ['5min', '15min'])
def test_s1_native_live_candidate_keeps_its_own_entry_geometry(live, frames, monkeypatch, base, tf):
    import engine.signal_processor as sp
    import scanner as scanner_module
    from trading.trade_manager import IST
    h = live(tf=tf)
    settings = sp.get_settings()
    settings.scanner_engine = base
    root = Path(__file__).resolve().parents[1]
    frames = {name: pd.read_csv(root / f'data_store/candles/NIFTY_{name}.csv',
        index_col='timestamp', parse_dates=True).tail(900) for name in TFS}
    full = frames[tf]
    raw = AltSignalEngine(signal_source='s1').process(full, instrument='NIFTY', timeframe=tf,
        df_5min=frames['5min'], df_15min=frames['15min'])
    signals = [s for s in raw['signals'] if s.timestamp.hour < 14]
    assert signals
    signal = signals[-1]
    now = signal.timestamp + timedelta(minutes=int(tf.replace('min', '')), seconds=20)
    current = {name: frame.loc[frame.index <= now] for name, frame in frames.items()}
    raw = AltSignalEngine(signal_source='s1').process(current[tf], instrument='NIFTY', timeframe=tf,
        df_5min=current['5min'], df_15min=current['15min'])
    signal = next(s for s in raw['signals'] if s.timestamp == signal.timestamp)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(now) if tz else now
    monkeypatch.setattr(sp, 'datetime', Clock)
    monkeypatch.setattr(scanner_module, 'datetime', Clock)
    result = dict(raw, signals=[signal])
    mtf = SimpleNamespace(results_1min=None, results_5min=result if tf == '5min' else None,
        results_15min=result if tf == '15min' else None, confluence_score=.5)
    h.sc._last_signal_time = {f'NIFTY_{tf}': signal.timestamp - timedelta(seconds=1)}
    h.sc.candles.get_latest_price = lambda *a: signal.price
    h.sc.candles.get_candles = lambda instrument, timeframe: current.get(timeframe)
    args = list(h.args)
    args[1], args[8], args[9] = mtf, signal.price, round(signal.price / 50) * 50
    with native_entry_settings('s1', settings):
        candidates = h.processor.process_best_signal(*args)
    assert candidates and candidates[0].status == 'TRADE SIGNAL'
    candidate = candidates[0]
    assert (candidate.source_engine, candidate.source_lane) == ('s1', 'S1')
    assert candidate.s1_lane
    assert not candidate.quad_union_entry and not candidate.raw_union_entry
    assert candidate.spot_stop != candidate.entry_spot
    assert candidate.stop < candidate.price < candidate.target
    assert settings.scanner_engine == base


@pytest.mark.asyncio
async def test_direct_merge_keeps_native_lane_buffers_separate(monkeypatch):
    """The combined coordinator must retain both native candidates independently."""
    from config.settings import get_settings
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    from tests.test_four_by_three_signal_pipeline_e2e_2026_08_22 import (
        _build_mtf, _result_lanes, _scanner_harness, _candidate, INDICES)
    pipeline, engines, frames = _build_mtf()
    results = {instrument: pipeline.process_instrument(instrument) for instrument in INDICES}
    lanes = _result_lanes(results)
    scanner, diagnostics, _, _, _ = _scanner_harness(pipeline, lanes, monkeypatch)
    signal = lanes[('NIFTY', '5min')][2]
    first, second = _candidate(signal), _candidate(signal)
    first.source_engine, first.source_lane = 's1', 'S1'
    second.source_engine, second.source_lane = 'ut_smart', 'UTS'
    await scanner._coordinate_and_execute([first, second])
    assert len(scanner._pending_live_signals) == 2
    assert diagnostics['stabilization.buffered'] == 2
    assert scanner._pending_live_signals['NIFTY_5min_LONG_s1']['candidate'] is first
    assert scanner._pending_live_signals['NIFTY_5min_LONG_ut_smart']['candidate'] is second
