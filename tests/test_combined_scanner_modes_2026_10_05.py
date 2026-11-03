"""Combined scanner regression proof across native math and real admission seams."""
import asyncio
import copy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from config.settings import get_settings, Settings
from engine.alt_scanners import AltSignalEngine, build_scanner_engine
from engine.multi_timeframe import MultiTimeframeEngine
from engine.combined_scanner import CombinedSignalEngine, native_mtf_view
from engine.scanner_modes import COMBINED_MODES, scanner_mode, native_policy, native_result, scope_suffix
from engine import s1_lane
from scanner import Scanner
from trading.trade_manager import Trade
from tests.test_s1_companion_trial_2026_10_04 import signature
from tests.test_ut_smart_live_path_2026_09_26 import live

INDICES = ('NIFTY', 'BANKNIFTY', 'SENSEX', 'MIDCPNIFTY')
TFS = ('1min', '5min', '15min')


@pytest.fixture
def combined(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'scanner_engine', 'quad_prime_s1')
    return settings


@pytest.fixture(scope='module')
def saved_frames():
    root = Path(__file__).resolve().parents[1]
    return {instrument: {tf: pd.read_csv(root / f'data_store/candles/{instrument}_{tf}.csv',
        index_col='timestamp', parse_dates=True).tail(900) for tf in TFS} for instrument in INDICES}


@pytest.mark.parametrize('mode', COMBINED_MODES)
@pytest.mark.parametrize('instrument', INDICES)
@pytest.mark.parametrize('fire', ['confirmed', 'realtime'])
def test_all_native_arrays_signals_and_exits_match_saved_candle_reference(combined, saved_frames, mode, instrument, fire):
    combined.scanner_engine = mode
    pipeline = MultiTimeframeEngine({'signal_mode': fire})
    frames = saved_frames[instrument]
    for tf, frame in frames.items():
        pipeline.update_data(instrument, tf, frame)
    result = pipeline.process_instrument(instrument)
    for native in (COMBINED_MODES[mode], 's1'):
        view = native_mtf_view(result, native)
        for tf in TFS:
            standalone = AltSignalEngine(signal_source=native,
                signal_mode='realtime' if tf == '1min' else fire).process(
                frames[tf], instrument=instrument, timeframe=tf,
                df_5min=frames['5min'], df_15min=frames['15min'],
                capital=100000., risk_pct=1., lots=1, lot_size=25)
            assert signature(getattr(view, f'results_{tf}')) == signature(standalone)
        assert view.confluence_score == pipeline._compute_confluence({
            tf: getattr(view, f'results_{tf}') for tf in TFS}, instrument)[0]


@pytest.mark.parametrize('mode', COMBINED_MODES)
def test_modes_are_validated_built_and_switched_across_all_twelve_slots(combined, mode):
    assert Settings(_env_file=None, scanner_engine=mode).scanner_engine == mode
    pipeline = MultiTimeframeEngine()
    pipeline._result_cache['NIFTY_5min'] = {'obsolete': True}
    assert pipeline.set_scanner_engine(mode) == mode
    assert not pipeline._result_cache
    assert len(pipeline.engines) == 12
    assert all(isinstance(engine, CombinedSignalEngine) and engine.signal_source == mode
               for engine in pipeline.engines.values())
    assert {id(native) for engine in pipeline.engines.values()
            for native in engine.native_engines.values()}.__len__() == 24


@pytest.mark.asyncio
async def test_native_policy_context_does_not_leak_between_concurrent_tasks(combined):
    before = combined.model_dump()
    async def read(mode):
        with native_policy(mode):
            await asyncio.sleep(0)
            return scanner_mode(combined)
    assert await asyncio.gather(read('s1'), read('quad_prime')) == ['s1', 'quad_prime']
    assert scanner_mode(combined) == 'quad_prime'
    assert combined.model_dump() == before
    with pytest.raises(RuntimeError):
        with native_policy('s1'):
            raise RuntimeError('restore')
    assert scanner_mode(combined) == 'quad_prime'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', COMBINED_MODES)
async def test_same_bar_sources_buffer_and_mature_independently(combined, monkeypatch, mode):
    from tests.test_four_by_three_signal_pipeline_e2e_2026_08_22 import (
        _build_mtf, _result_lanes, _scanner_harness, _candidate, INDICES as MATRIX_INDICES)
    combined.scanner_engine = mode
    pipeline, _, frames = _build_mtf()
    result = {instrument: pipeline.process_instrument(instrument) for instrument in MATRIX_INDICES}
    lanes = _result_lanes(result)
    scanner, diagnostics, accepted_log, _, _ = _scanner_harness(pipeline, lanes, monkeypatch)
    original = lanes[('NIFTY', '5min')][2]
    s1_signal, base_signal = copy.copy(original), copy.copy(original)
    s1_signal.source_engine, s1_signal.source_lane = 's1', 'S1'
    base_mode = COMBINED_MODES[mode]
    base_signal.source_engine = base_mode
    base_signal.source_lane = 'UTS' if base_mode == 'ut_smart' else 'QP'
    base, companion = _candidate(base_signal), _candidate(s1_signal)
    base.source_engine, base.source_lane = base_signal.source_engine, base_signal.source_lane
    companion.source_engine, companion.source_lane = 's1', 'S1'
    envelope = {'signals': [base_signal, s1_signal], 'state': {'position': 1},
        'native_results': {base_mode: {'signals': [base_signal]}, 's1': {'signals': [s1_signal]}}}
    scanner._cached_results['NIFTY_5min'] = envelope
    assert await scanner._coordinate_and_execute([base, companion]) == []
    assert set(scanner._pending_live_signals) == {f'NIFTY_5min_LONG_{base_mode}', 'NIFTY_5min_LONG_s1'}
    assert diagnostics['stabilization.buffered'] == 2
    assert scanner._candidate_history_key(base) != scanner._candidate_history_key(companion)
    # Independent freshness is earned from a post-buffer computation, not a cache read.
    pipeline.update_data('NIFTY', '5min', frames['5min'])
    fresh = pipeline.process_instrument('NIFTY')
    metadata = fresh.timeframe_metadata['5min']
    scanner._cached_result_metadata['NIFTY_5min'] = {**metadata, 'result_object': envelope}
    # UTS validation additionally requires native observation provenance. Exercise
    # the shared buffer and source-specific presence here; UTS provenance is tested
    # separately with the real engine and existing native live-path suite.
    if base_mode == 'ut_smart':
        monkeypatch.setattr(scanner, '_repaint_signal_status', lambda c, *a, **k: 'PRESENT')
    out = await scanner._coordinate_and_execute([])
    assert len(out) == 2 and not scanner._pending_live_signals
    assert {candidate.source_lane for candidate in out} == {base.source_lane, 'S1'}


def test_foreign_engine_evidence_never_confirms_an_s1_candidate(combined):
    scanner = Scanner.__new__(Scanner)
    stamp = datetime(2026, 10, 5, 10)
    candidate = NS(instrument='NIFTY', timeframe='5min', direction='LONG',
        source_engine='s1', source_lane='S1', source_signal_type='BUY')
    scanner._cached_results = {'NIFTY_5min': {'signals': [NS(timestamp=stamp, signal_type='BUY')],
        'native_results': {'quad_prime': {'signals': [NS(timestamp=stamp, signal_type='BUY')]},
                           's1': {'signals': [NS(timestamp=stamp, signal_type='SELL')]}}}}
    assert scanner._repaint_signal_status(candidate, stamp) == 'FLIPPED'


def test_foreign_strategy_cannot_close_s1_but_native_uts_reversal_still_works(combined, live, monkeypatch):
    import engine.signal_processor as sp
    import scanner as module
    h = live()
    combined.scanner_engine = 'ut_smart_s1'
    # The harness owns its Settings instance; use it consistently at all seams.
    settings = sp.get_settings()
    settings.scanner_engine = 'ut_smart_s1'
    monkeypatch.setattr('config.settings._settings', settings)
    opposite = 'LONG' if h.sig.signal_type == 'SELL' else 'SHORT'
    def position(mode, lane):
        return NS(id=lane, instrument='NIFTY', timeframe='15min', direction=opposite,
            inst_type='OPT', status='OPEN', entry_price=140., current_price=150.,
            source_engine=mode, source_lane=lane, signal_time=h.sig.timestamp-timedelta(minutes=30),
            trading_symbol='NIFTYPREV', lots=1, lot_size=65)
    h.sc.trades.open_trades = {'S1': position('s1', 'S1'), 'UTS': position('ut_smart', 'UTS')}
    h.sc._get_live_premium_cached = lambda *a, **k: 155.
    closed = []
    h.sc._close_and_record = lambda key, *a, **k: closed.append(key)
    h.sc._handle_opposite_signal_exit('NIFTY', h.sig, '15min')
    assert closed == ['UTS']


def test_persisted_owner_recovers_native_result_and_exit_after_restart(combined):
    from tests.test_s1_system_integration_2026_09_24 import trade
    original = trade(source_lane='S1')
    restored = Trade.from_dict(original.to_dict())
    own = {'signals': [NS(source_engine='s1', timestamp=restored.signal_time, signal_type='BUY',
        s1_exit_reason='Momentum Exit', s1_exit_time=restored.signal_time+timedelta(minutes=20),
        s1_exit_price=23420.)]}
    envelope = {'native_results': {'s1': own, 'quad_prime': {'signals': []}}}
    assert native_result(restored, envelope) is own
    assert s1_lane.engine_exit_for(restored, envelope, restored.signal_time+timedelta(minutes=25))['reason'] == 'Momentum Exit'
    assert scope_suffix(restored, combined) == '_s1'


def test_shared_arbitration_keeps_both_native_contracts_and_ignores_display_grade(combined):
    scanner = Scanner.__new__(Scanner)
    first = NS(action='ENTRY', instrument='NIFTY', timeframe='5min', signal_timestamp=datetime(2026,10,5,10),
        source_engine='s1', source_lane='S1', grade='A+', confidence=.99)
    second = NS(**{**vars(first), 'source_engine':'ut_smart', 'source_lane':'UTS', 'grade':'C', 'confidence':.1})
    assert scanner._apply_multi_signal_mixed_book([first, second]) == [first, second]
    assert scanner._candidate_rank_score(first) == scanner._candidate_rank_score(second)


@pytest.mark.parametrize('mode', COMBINED_MODES)
def test_dynamic_parameters_reach_both_engines_and_survive_mode_switch(combined, mode):
    combined.scanner_engine = mode
    pipeline = MultiTimeframeEngine()
    pipeline.apply_engine_params({**combined.get_ut_engine_params(), 'key_value': 3.7, 'atr_period': 12, 'signal_mode': 'confirmed', 'use_heikin_ashi': True, 'adx_period': 9})
    for key, wrapper in pipeline.engines.items():
        for engine in wrapper.native_engines.values():
            assert engine.key_value == 3.7 and engine.atr_period == 12
            assert engine.use_heikin_ashi and engine.adx_period == 9
            assert engine.signal_mode == ('realtime' if key.endswith('_1min') else 'confirmed')
    assert len(pipeline.get_all_states()) == 12
    pipeline.set_scanner_engine('utbot')
    assert all(engine.key_value == 3.7 for engine in pipeline.engines.values())
    pipeline.set_scanner_engine(mode)
    assert all(engine.key_value == 3.7 for wrapper in pipeline.engines.values()
               for engine in wrapper.native_engines.values())


@pytest.mark.parametrize('mode', COMBINED_MODES)
def test_live_warmup_keeps_s1_history_in_both_modes(combined, mode):
    from engine.s1_engine import S1_FEED_BARS
    combined.scanner_engine = mode
    assert Scanner._signal_engine_warmup_bars() == S1_FEED_BARS


def test_failed_native_lane_retries_without_hiding_healthy_lane(combined, saved_frames, monkeypatch):
    pipeline = MultiTimeframeEngine()
    for tf, frame in saved_frames['NIFTY'].items():
        pipeline.update_data('NIFTY', tf, frame)
    engine = pipeline.engines['NIFTY_5min'].native_engines['quad_prime']
    original = engine.process
    calls = []
    def broken(**kwargs):
        calls.append(1)
        raise RuntimeError('native lane unavailable')
    monkeypatch.setattr(engine, 'process', broken)
    result = pipeline.process_instrument('NIFTY')
    assert native_mtf_view(result, 'quad_prime').results_5min is None
    assert native_mtf_view(result, 's1').results_5min is not None
    pipeline.process_instrument('NIFTY')
    assert len(calls) == 2
    monkeypatch.setattr(engine, 'process', original)
    assert native_mtf_view(pipeline.process_instrument('NIFTY'), 'quad_prime').results_5min is not None


def test_s1_dependency_correction_invalidates_combined_one_minute_cache(combined, saved_frames):
    pipeline = MultiTimeframeEngine()
    for tf, frame in saved_frames['NIFTY'].items():
        pipeline.update_data('NIFTY', tf, frame)
    before = pipeline.process_instrument('NIFTY').results_1min
    assert pipeline.process_instrument('NIFTY').results_1min is before
    corrected = saved_frames['NIFTY']['15min'].copy()
    corrected.iloc[-20, corrected.columns.get_loc('close')] += 5
    pipeline.update_data('NIFTY', '15min', corrected)
    assert pipeline.process_instrument('NIFTY').results_1min is not before


def test_actual_uts_entry_selection_preserves_native_contract_and_geometry(live, monkeypatch):
    import engine.signal_processor as sp
    baseline = live()
    expected = baseline.processor.process_best_signal(*baseline.args)[0]
    harness = live()
    settings = sp.get_settings()
    settings.scanner_engine = 'ut_smart_s1'
    monkeypatch.setattr('config.settings._settings', settings)
    partition = harness.args[1]
    partition.instrument = 'NIFTY'
    for tf in TFS:
        result = getattr(partition, f'results_{tf}')
        if result is not None:
            setattr(partition, f'results_{tf}', {'native_results': {'ut_smart': result,
                 's1': {'signals': [], 'state': None}}})
    foreign = NS(id='S1-other', instrument='NIFTY', timeframe='5min', direction='LONG',
                 status='OPEN', source_lane='S1', source_engine='s1', is_ghost=False)
    harness.sc.trades.open_trades = {'S1-other': foreign}
    before = settings.model_dump()
    out = harness.processor.process_best_signal(*harness.args)
    assert foreign.timeframe == '5min'
    assert len(out) == 1
    actual = out[0]
    for field in ('direction', 'inst_type', 'trading_symbol', 'price', 'stop', 'target',
                  'spot_stop', 'entry_spot', 'source_engine', 'source_lane', 'rr'):
        assert getattr(actual, field) == getattr(expected, field)
    assert settings.model_dump() == before


def test_opposite_confirmation_timers_do_not_cancel_other_owner(combined, monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = 'REAL'
    scanner._pending_opposite_exit = {}
    scanner._combo_solo_utbot_exit_blocked = lambda signal: False
    scanner._has_opposite_exposure = lambda *args: True
    scanner._opposite_flip_quality_verdict = lambda *args: 'confirm'
    scanner.log_event = lambda *args: None
    stamp = datetime(2026, 10, 5, 10)
    s1 = NS(timestamp=stamp, signal_type='SELL', source_engine='s1', source_lane='S1')
    quad = NS(timestamp=stamp, signal_type='SELL', source_engine='ema', source_lane='QP')
    assert not scanner._opposite_exit_confirm_ready('NIFTY', s1, '5min')
    assert not scanner._opposite_exit_confirm_ready('NIFTY', quad, '5min')
    assert len(scanner._pending_opposite_exit) == 2
    assert {key.rsplit('_', 1)[-1] for key in scanner._pending_opposite_exit} == {'s1', 'prime'}


@pytest.mark.parametrize('mode', COMBINED_MODES)
def test_combined_chart_uses_private_engines_and_retains_both_sources(combined, saved_frames, mode):
    combined.scanner_engine = mode
    frames = saved_frames['NIFTY']
    scanner = Scanner.__new__(Scanner)
    scanner.candles = NS(get_candles=lambda instrument, tf: frames[tf])
    sentinel = {'untouched': True}
    scanner._cached_results = {'NIFTY_5min': sentinel}
    scanner.mtf = NS(_result_cache={'NIFTY_5min': sentinel}, engines={})
    displayed = scanner._chart_result_for_display('NIFTY', '5min', frames['5min'], sentinel)
    assert set(displayed['native_results']) == {COMBINED_MODES[mode], 's1'}
    assert all(not lane.get('error') for lane in displayed['native_results'].values())
    assert len(displayed['signals']) == sum(len(lane['signals']) for lane in displayed['native_results'].values())
    assert scanner._cached_results['NIFTY_5min'] is sentinel
    assert scanner.mtf._result_cache['NIFTY_5min'] is sentinel


@pytest.mark.parametrize('mode', COMBINED_MODES)
def test_engine_lab_shadow_identity_separates_same_bar_owners(combined, mode):
    from engine.engine_lab import EngineLab
    base = NS(timestamp=datetime(2026, 10, 5, 10), signal_type='BUY', bar_index=10,
              source_engine=COMBINED_MODES[mode], source_lane='')
    companion = NS(**{**vars(base), 'source_engine': 's1', 'source_lane': 'S1'})
    assert EngineLab._signal_key(mode, 'NIFTY', '5min', base) != EngineLab._signal_key(mode, 'NIFTY', '5min', companion)
    base_lane, companion_lane = (EngineLab._source_lane(item, mode) for item in (base, companion))
    assert EngineLab._trade_key(mode, 'NIFTY', '5min', base_lane) != EngineLab._trade_key(mode, 'NIFTY', '5min', companion_lane)
    assert EngineLab._replay_trade_key(mode, 'NIFTY', '5min', base_lane) != EngineLab._replay_trade_key(mode, 'NIFTY', '5min', companion_lane)


@pytest.mark.parametrize('mode', COMBINED_MODES)
def test_independent_same_timeframe_entries_still_share_index_capacity(combined, mode):
    combined.scanner_engine = mode
    scanner = Scanner.__new__(Scanner)
    candidate = NS(instrument='NIFTY', timeframe='5min', direction='LONG', inst_type='OPT',
                   source_engine='s1', source_lane='S1', signal_timestamp=datetime(2026,10,5,10))
    foreign = NS(instrument='NIFTY', timeframe='5min', direction='LONG', inst_type='OPT',
                 source_engine=COMBINED_MODES[mode], source_lane='UTS' if mode == 'ut_smart_s1' else 'QP')
    scanner.trades = NS(open_trades={'base': foreign})
    scanner._active_index_trade_cap = lambda *a: (2, 'shared capacity')
    scanner._opposite_direction_stack_block = lambda *a: None
    scanner._record_live_reject = lambda *a, **k: None
    assert scanner._prepare_candidate_for_concurrency(candidate)
    scanner.trades.open_trades['second'] = copy.copy(foreign)
    assert not scanner._prepare_candidate_for_concurrency(candidate)
    assert 'max 2 concurrent trades' in candidate.concurrency_block_reason


def test_cascade_cannot_add_to_another_engine_trade(combined, monkeypatch):
    from tests.test_cascade_adds_a_lot_2026_08_07 import _scanner, _open, _cand
    foreign = _open()
    foreign.source_lane, foreign.source_engine = 'S1', 's1'
    candidate = _cand()
    candidate.source_lane, candidate.source_engine = 'QP', 'ema'
    scanner = _scanner({'S1': foreign})
    scanner._fresh_entry_universe = lambda: ['NIFTY']
    monkeypatch.setattr(Scanner, '_auto_entry_resource_blockers', lambda *a: [])
    assert scanner._offer_cascade_add(candidate)[0] is False
    assert scanner.calls == []


def test_manual_lifecycle_cannot_refresh_or_supersede_another_owner(combined):
    scanner = Scanner.__new__(Scanner)
    common = dict(direction='LONG', timeframe='5min', inst_type='OPT', option_type='CE',
                  signal_timestamp=datetime(2026,10,5,10), price=100., source_lane='QP', source_engine='ema')
    candidate = NS(**common)
    previous = NS(**{**common, 'source_lane':'S1', 'source_engine':'s1', 'price':200.})
    assert not scanner._refresh_same_direction_session_lifecycle(candidate, previous)
    assert candidate.price == 100.
    candidate.direction = 'SHORT'
    assert not scanner._should_supersede_session_entry(candidate, previous)
