"""UTS entry timing and the independent post-entry repaint worker."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import Settings
from engine import s1_lane, uts_lane
from scanner import Scanner


@pytest.mark.parametrize('timeframe,wait', [('1min', 5.), ('5min', 15.), ('15min', 15.)])
def test_uts_buffer_uses_its_timeframe_and_keeps_the_elapsed_floor(monkeypatch, timeframe, wait):
    settings = Settings(_env_file=None, scanner_engine='ut_smart')
    monkeypatch.setattr('scanner.get_settings', lambda: settings)
    candidate = SimpleNamespace(source_engine='ut_smart', source_lane='UTS', timeframe=timeframe)
    seconds, scans, adaptive = Scanner.__new__(Scanner)._live_signal_stabilization_requirements(candidate)
    assert (seconds, scans, adaptive) == (wait, 1, False)
    assert not Scanner._stabilization_ready(wait - .001, seconds, scans, scans, adaptive)
    assert Scanner._stabilization_ready(wait, seconds, scans, scans, adaptive)


@pytest.mark.parametrize('wait', [5., 6., 7.])
def test_one_minute_wait_is_configurable_and_dashboard_validated(wait):
    from dashboard.server import _valid_command_payload
    settings = Settings(_env_file=None, uts_lane_intrabar_wait_seconds_1min=wait)
    candidate = SimpleNamespace(source_engine='ut_smart', timeframe='1min')
    assert s1_lane.lane_intrabar_wait_seconds(candidate, settings) == wait
    assert uts_lane.dashboard_values(settings)['uts_lane_intrabar_wait_seconds_1min'] == wait
    assert _valid_command_payload({'cmd': 'configure', 'uts_lane_intrabar_wait_seconds_1min': wait}) is None


@pytest.mark.parametrize('wait', [0., 4.9, 7.1, 20.])
def test_one_minute_wait_rejects_values_outside_requested_range(wait):
    from dashboard.server import _valid_command_payload
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(_env_file=None, uts_lane_intrabar_wait_seconds_1min=wait)
    assert _valid_command_payload({'cmd': 'configure', 'uts_lane_intrabar_wait_seconds_1min': wait}) == 'out_of_range:uts_lane_intrabar_wait_seconds_1min'


def test_main_timing_toggle_controls_uts_without_a_hidden_second_opt_in():
    settings = Settings(_env_file=None, scanner_engine='ut_smart')
    settings.set_signal_fire_mode('INTRACANDLE')
    assert uts_lane.intrabar_enabled(settings)
    settings.set_signal_fire_mode('CANDLE_CLOSE')
    assert not uts_lane.intrabar_enabled(settings)


def test_uts_does_not_readmit_a_vanished_flip_using_generic_momentum(monkeypatch):
    settings = Settings(_env_file=None)
    monkeypatch.setattr('scanner.get_settings', lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner._repaint_signal_status = lambda *a: 'ABSENT'
    scanner._late_session_reclaim_context = lambda *a: pytest.fail('UTS entered generic override')
    candidate = SimpleNamespace(source_engine='ut_smart', source_lane='UTS')
    assert scanner._stabilization_repaint_override_context(candidate) is None


def _worker_host(monkeypatch, *, timeframe='5min', auto_mode=True):
    import pandas as pd
    from engine.uts_repaint import UTSRepaintWorker
    from tests.test_ut_smart_live_path_2026_09_26 import _trade_at, _flip, _uts_result
    stamp = datetime(2026, 9, 30, 10)
    trade = _trade_at(stamp, timeframe=timeframe)
    trade.id, trade.status, trade.entered_intracandle = 'UTS-1', 'OPEN', True
    trade.entry_time, trade.inst_type = stamp + timedelta(seconds=20), 'OPT'
    settings = Settings(_env_file=None, scanner_engine='ut_smart')
    settings.set_signal_fire_mode('CANDLE_CLOSE')  # Active positions still get monitored.
    monkeypatch.setattr('config.settings.get_settings', lambda: settings)
    monkeypatch.setattr('engine.uts_repaint.get_settings', lambda: settings)
    samples, exits, dirty = [], [], []
    shared = {'untouched': True}
    host = SimpleNamespace(mode='REAL', auto_mode=auto_mode,
        trades=SimpleNamespace(open_trades={trade.id: trade}, _mark_dirty=lambda: dirty.append(True)),
        candles=SimpleNamespace(get_candles=lambda *a: pd.DataFrame({'close': [100.]}, index=[stamp]),
                                get_latest_price=lambda *a: 23000.),
        _cached_results=shared, mtf=SimpleNamespace(_result_cache=shared),
        _get_live_premium_cached=lambda *a, **kw: 100.,
        _active_option_decision_mark=lambda *a: (100., 'test-contract', True),
        _close_and_record=lambda *a, **kw: exits.append((a, kw)))
    worker = UTSRepaintWorker(host)
    present = [True]
    error = [False]
    def calculate(frame, instrument, tf, now):
        samples.append(now)
        if error[0]:
            return {'error': 'temporary feed gap'}
        closed = now >= stamp + timedelta(minutes=int(tf.replace('min', '')))
        return _uts_result(stamp, now,
            [_flip(stamp, forming=not closed)] if present[0] else [],
            closed=closed, timeframe=tf)
    worker._calculate = calculate
    return SimpleNamespace(worker=worker, trade=trade, stamp=stamp, samples=samples,
        exits=exits, present=present, error=error, host=host, dirty=dirty, shared=shared)


@pytest.mark.asyncio
@pytest.mark.parametrize('timeframe', ['1min', '5min', '15min'])
async def test_independent_repaint_worker_samples_every_10s_and_confirms_at_close(monkeypatch, timeframe):
    h = _worker_host(monkeypatch, timeframe=timeframe)
    for seconds in [20, 29, 30, 39, 40]:
        await h.worker.run_once(h.stamp + timedelta(seconds=seconds))
    assert h.samples == [h.stamp + timedelta(seconds=n) for n in [30, 40]]
    assert not h.exits
    close = h.stamp + timedelta(minutes=int(timeframe.replace('min', '')))
    await h.worker.run_once(close)
    assert h.trade.uts_confirmed and h.trade.repaint_guard_checked and h.dirty
    count = len(h.samples)
    h.present[0] = False
    await h.worker.run_once(close + timedelta(seconds=10))
    assert len(h.samples) == count and not h.exits
    assert h.host._cached_results is h.shared and h.shared == {'untouched': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('auto_mode', [True, False])
async def test_worker_repaint_uses_existing_exit_authority(monkeypatch, auto_mode):
    h = _worker_host(monkeypatch, auto_mode=auto_mode)
    await h.worker.run_once(h.stamp + timedelta(seconds=30))
    h.present[0] = False
    await h.worker.run_once(h.stamp + timedelta(seconds=40))
    assert len(h.exits) == 1
    assert h.exits[0][0] == ('UTS-1', 100., 'UTS_REPAINT')
    assert h.host.auto_mode is auto_mode  # Gateway retains alert-only manual semantics.


@pytest.mark.asyncio
async def test_worker_error_and_unverified_contract_cannot_exit(monkeypatch):
    h = _worker_host(monkeypatch)
    h.error[0] = True
    await h.worker.run_once(h.stamp + timedelta(seconds=30))
    assert not h.exits and not getattr(h.trade, 'uts_confirmed', False)
    h.error[0], h.present[0] = False, False
    h.host._active_option_decision_mark = lambda *a: (90., 'stale', False)
    await h.worker.run_once(h.stamp + timedelta(seconds=40))
    assert not h.exits
    h.host._active_option_decision_mark = lambda *a: (98., 'test-contract', True)
    await h.worker.run_once(h.stamp + timedelta(seconds=50))
    assert h.exits[0][0] == ('UTS-1', 98., 'UTS_REPAINT')


@pytest.mark.asyncio
async def test_worker_skips_closed_entries_and_historical_authority(monkeypatch):
    h = _worker_host(monkeypatch)
    h.trade.entered_intracandle = False
    await h.worker.run_once(h.stamp + timedelta(seconds=30))
    h.trade.entered_intracandle, h.host.mode = True, 'HISTORICAL'
    await h.worker.run_once(h.stamp + timedelta(seconds=40))
    assert not h.samples and not h.exits


def test_private_engine_still_tracks_open_intrabar_trade_after_timing_switch_off(monkeypatch):
    from pathlib import Path
    import pandas as pd
    from engine.alt_scanners import AltSignalEngine
    from engine.uts_repaint import UTSRepaintWorker
    settings = Settings(_env_file=None)
    settings.set_signal_fire_mode('CANDLE_CLOSE')
    monkeypatch.setattr('config.settings.get_settings', lambda: settings)
    frame = pd.read_csv(Path(__file__).with_name('fixtures') / 'uts_nifty_15min_2026_09_30.csv',
                        parse_dates=['timestamp']).set_index('timestamp')
    stamp = pd.Timestamp('2026-09-30 09:15')
    frame = frame.loc[frame.index <= stamp]
    regular = AltSignalEngine(signal_source='ut_smart').process(frame, instrument='NIFTY',
                timeframe='15min', as_of=stamp + pd.Timedelta(seconds=30))
    result = UTSRepaintWorker._calculate(frame, 'NIFTY', '15min', stamp + pd.Timedelta(seconds=30))
    assert not any(s.timestamp == stamp for s in regular['signals'])
    assert any(s.timestamp == stamp and s.uts_forming for s in result['signals'])


@pytest.mark.asyncio
async def test_worker_starts_after_fill_and_does_not_duplicate_pending_exit(monkeypatch):
    h = _worker_host(monkeypatch)
    h.trade.execution_status = 'ENTRY_PENDING'
    await h.worker.run_once(h.stamp + timedelta(seconds=30))
    assert not h.samples
    h.trade.execution_status, h.trade.entry_filled_at = 'OPEN', h.stamp + timedelta(seconds=32)
    await h.worker.run_once(h.stamp + timedelta(seconds=41))
    assert not h.samples
    await h.worker.run_once(h.stamp + timedelta(seconds=42))
    assert len(h.samples) == 1
    h.trade.execution_status, h.present[0] = 'EXIT_PENDING', False
    await h.worker.run_once(h.stamp + timedelta(seconds=52))
    assert len(h.samples) == 1 and not h.exits


def _execution_host(monkeypatch, timeframe):
    import engine.execution_service as module
    from trading.trade_manager import IST
    clock = [datetime(2026, 9, 30, 10, 0, 20)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(clock[0]) if tz else clock[0]
    monkeypatch.setattr(module, 'datetime', Clock)
    settings = Settings(_env_file=None, scanner_engine='ut_smart')
    settings.set_signal_fire_mode('INTRACANDLE')
    monkeypatch.setattr('scanner.get_settings', lambda: settings)
    monkeypatch.setattr(module, 'get_settings', lambda: settings)
    sc = Scanner.__new__(Scanner)
    sc.mode, sc.scan_count = 'REAL', 1
    sc._pending_live_signals, sc._cached_results = {}, {}
    sc._last_signal_time, sc._last_signal_identity, sc._last_live_signal_candle_time = {}, {}, {}
    sc._candidate_process_cache, sc._signal_preflight_cache = {}, {}
    sc._live_clock_is_sane = lambda: True
    sc._session_squareoff_clock = lambda: clock[0]
    sc._entry_cutoff_allows_candidate = lambda *a: True
    sc._diag_inc = sc.log_event = sc.log_signal_decision_once = lambda *a, **kw: None
    sc._evict_superseded_opposite_buffer = lambda *a: None
    sc._save_warm_memory = sc._maybe_prepare_stabilization_preflight = lambda *a, **kw: None
    sc._apply_stabilization_preflight = sc._forget_signal_display_rows = lambda *a, **kw: None
    sc._drop_missed_reentry_seed = sc._mark_post_repaint_recheck = lambda *a, **kw: None
    records, fresh = [], []
    sc._rtracker_record_candidate = lambda c, **kw: records.append((c, kw['decision']))
    sc._regrade_matured_candidate = lambda *a: None
    state = ['PRESENT']
    sc._repaint_signal_status = lambda *a, **kw: state[0]
    sc.mtf = SimpleNamespace(require_fresh_result=lambda *a, **kw: fresh.append((a, kw)))
    # End the test at the real execution boundary: record maturity without broker orders.
    sc.data = SimpleNamespace(is_market_open=lambda: False)
    candidate = SimpleNamespace(instrument='NIFTY', timeframe=timeframe, action='ENTRY',
        direction='LONG', source_engine='ut_smart', source_lane='UTS',
        source_signal_type='BUY', signal_timestamp=datetime(2026, 9, 30, 10),
        confidence=.2, grade='C', reasons=[])
    return SimpleNamespace(scanner=sc, candidate=candidate, clock=clock, records=records,
                            state=state, fresh=fresh)


@pytest.mark.asyncio
@pytest.mark.parametrize('timeframe,wait', [('1min', 5.), ('5min', 15.), ('15min', 15.)])
async def test_actual_execution_buffer_obeys_timeframe_wait_and_freshness(monkeypatch, timeframe, wait):
    from engine.execution_service import ExecutionService
    h = _execution_host(monkeypatch, timeframe)
    service = ExecutionService(h.scanner)
    started = h.clock[0]
    await service.coordinate_and_execute([h.candidate])
    assert h.scanner._pending_live_signals and h.fresh
    h.clock[0] = started + timedelta(seconds=wait - .001)
    await service.coordinate_and_execute([])
    assert not h.records
    h.clock[0], h.state[0] = started + timedelta(seconds=wait), 'UNKNOWN'
    await service.coordinate_and_execute([])
    assert not h.records and h.scanner._pending_live_signals
    h.state[0] = 'PRESENT'
    await service.coordinate_and_execute([])
    assert [decision for _, decision in h.records] == ['STABILIZED']
    assert not h.scanner._pending_live_signals


@pytest.mark.asyncio
async def test_vanished_intrabar_flip_must_earn_a_new_full_wait(monkeypatch):
    from engine.execution_service import ExecutionService
    h = _execution_host(monkeypatch, '5min')
    service = ExecutionService(h.scanner)
    started = h.clock[0]
    await service.coordinate_and_execute([h.candidate])
    h.clock[0], h.state[0] = started + timedelta(seconds=10), 'ABSENT'
    await service.coordinate_and_execute([])
    assert not h.scanner._pending_live_signals and h.records[-1][1] == 'REJECTED'
    h.clock[0], h.state[0] = started + timedelta(seconds=11), 'PRESENT'
    await service.coordinate_and_execute([h.candidate])
    from config.settings import get_settings
    wait = float(get_settings().uts_lane_intrabar_wait_seconds)
    h.clock[0] = started + timedelta(seconds=11 + wait - 1)
    await service.coordinate_and_execute([])
    assert not any(decision == 'STABILIZED' for _, decision in h.records)
    h.clock[0] = started + timedelta(seconds=11 + wait)
    await service.coordinate_and_execute([])
    assert h.records[-1][1] == 'STABILIZED'


@pytest.mark.asyncio
async def test_confirmed_signal_removes_its_pending_buffer_before_execution(monkeypatch):
    from engine.execution_service import ExecutionService
    h = _execution_host(monkeypatch, '1min')
    service = ExecutionService(h.scanner)
    h.clock[0] = datetime(2026, 9, 30, 10, 0, 58)
    await service.coordinate_and_execute([h.candidate])
    assert h.scanner._pending_live_signals
    h.clock[0] = datetime(2026, 9, 30, 10, 1)
    await service.coordinate_and_execute([h.candidate])
    assert h.candidate.entered_intracandle is False
    assert not h.scanner._pending_live_signals and not h.records


def _configure_host(monkeypatch):
    settings = Settings(_env_file=None, scanner_engine='ut_smart')
    settings.set_signal_fire_mode('INTRACANDLE')
    settings.uts_lane_allow_intrabar_entries = True
    monkeypatch.setattr('config.settings.get_settings', lambda: settings)
    monkeypatch.setattr('scanner.get_settings', lambda: settings)
    sc = Scanner.__new__(Scanner)
    for name, value in dict(mode='PAPER', auto_mode=False, inst_pref='AUTO',
            capital_total=100000., capital_fut=50000., capital_opt=50000.,
            risk_fut_pct=1., risk_opt_pct=1., futures_sl_pct=.15, options_sl_pct=15.,
            max_daily_loss_pct=5., user_lots={}, user_lots_fut={}, active_indices=['NIFTY'],
            backtest_days=1, data=None, on_notification=None, _hist_candidates_loaded=True,
            _pending_opposite_exit={}, _live_signal_recheck_eligible={}, _live_signal_recheck_at={},
            _cached_results={}, _last_signal_time={}, _last_signal_identity={},
            _last_live_signal_candle_time={}).items():
        setattr(sc, name, value)
    sc.trades = SimpleNamespace(has_live_exposure=lambda: False,
            update_risk_settings=lambda *a, **kw: None, mode='PAPER')
    sc.risk_manager = SimpleNamespace(update_settings=lambda **kw: None)
    sc.mtf = SimpleNamespace(apply_engine_params=lambda *a: None,
                             _result_cache={}, _data_hash={})
    sc.perf = SimpleNamespace(reset=lambda: None)
    for name in ['log_event', '_stamp_simulation_diagnostics', '_purge_session_contamination',
                 '_load_session_trade_candidates', '_set_recalculation_status',
                 'queue_full_recalculation', '_publish_config_snapshot', '_record_live_session_state']:
        setattr(sc, name, lambda *a, **kw: None)
    sc._next_simulation_id = lambda: 1
    candidate = SimpleNamespace(source_engine='ut_smart', source_lane='UTS')
    sc._pending_live_signals = {'UTS-1': {'candidate': candidate}}
    return sc, settings


@pytest.mark.parametrize('mode', ['CANDLE_CLOSE', 'INTRACANDLE'])
def test_scanner_configure_synchronizes_both_switches_and_removes_old_timers(monkeypatch, mode):
    sc, settings = _configure_host(monkeypatch)
    # Test the previously broken case: global mode remains ON, UTS changes OFF to ON.
    settings.uts_lane_allow_intrabar_entries = False
    assert sc.configure(signal_fire_mode=mode, persist_settings=False)
    assert uts_lane.intrabar_enabled(settings) is (mode == 'INTRACANDLE')
    assert settings.uts_lane_allow_intrabar_entries is (mode == 'INTRACANDLE')
    assert not sc._pending_live_signals


def test_advanced_switch_and_wait_setting_also_invalidate_buffers(monkeypatch):
    sc, settings = _configure_host(monkeypatch)
    assert sc.configure(uts_lane_allow_intrabar_entries=False, persist_settings=False)
    assert settings.get_signal_fire_mode() == 'CANDLE_CLOSE'
    assert not sc._pending_live_signals
    settings.set_signal_fire_mode('INTRACANDLE')
    sc._pending_live_signals = {'UTS-1': {'candidate': SimpleNamespace(source_engine='ut_smart')}}
    assert sc.configure(uts_lane_intrabar_wait_seconds_1min=7., persist_settings=False)
    assert settings.uts_lane_intrabar_wait_seconds_1min == 7. and not sc._pending_live_signals


def test_dashboard_switch_aliases_execute_and_other_engines_keep_their_switch(monkeypatch):
    from pathlib import Path
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    source = (Path(__file__).parents[1] / 'dashboard/static/js/dashboard-settings.js').read_text(encoding='utf-8')
    function = source[source.index('function syncUtsSignalTiming('):source.index('function syncSettingsScannerEngine(')]
    script = """const assert = require('node:assert/strict');
const elements = {
  settScannerEngine: {value: 'ut_smart'}, settIntrabarSignals: {checked: true},
  settUtsAllowIntrabar: {checked: false}, settSignalModeHelp: {textContent: ''}
};
global.document = {getElementById: id => elements[id]};
""" + function + """
syncUtsSignalTiming('settIntrabarSignals');
assert.equal(elements.settUtsAllowIntrabar.checked, true);
assert.match(elements.settSignalModeHelp.textContent, /20s.*5-7s.*10s/);
elements.settUtsAllowIntrabar.checked = false;
syncUtsSignalTiming('settUtsAllowIntrabar');
assert.equal(elements.settIntrabarSignals.checked, false);
elements.settScannerEngine.value = 's1';
elements.settIntrabarSignals.checked = true;
syncUtsSignalTiming('settIntrabarSignals');
assert.equal(elements.settUtsAllowIntrabar.checked, false);
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_final_confirmation_does_not_wait_for_next_10_second_slot(monkeypatch):
    h = _worker_host(monkeypatch, timeframe='1min')
    h.trade.entry_time = h.stamp + timedelta(seconds=23)
    for seconds in [33, 43, 53]:
        await h.worker.run_once(h.stamp + timedelta(seconds=seconds))
    await h.worker.run_once(h.stamp + timedelta(minutes=1))
    assert h.samples[-1] == h.stamp + timedelta(minutes=1)
    assert h.trade.uts_confirmed and not h.exits


@pytest.mark.asyncio
async def test_new_candle_identity_restarts_the_uts_timer(monkeypatch):
    from engine.execution_service import ExecutionService
    h = _execution_host(monkeypatch, '1min')
    service = ExecutionService(h.scanner)
    await service.coordinate_and_execute([h.candidate])
    fresh = SimpleNamespace(**vars(h.candidate))
    fresh.signal_timestamp += timedelta(minutes=1)
    h.clock[0] = fresh.signal_timestamp + timedelta(seconds=1)
    await service.coordinate_and_execute([fresh])
    assert not h.records and h.scanner._pending_live_signals
    pending = next(iter(h.scanner._pending_live_signals.values()))
    assert pending['timestamp'] == h.clock[0] and pending['stable_scans'] == 1
    h.clock[0] += timedelta(seconds=6)
    await service.coordinate_and_execute([])
    assert h.records[-1][1] == 'STABILIZED'


def test_uts_timing_fields_restore_from_saved_environment(tmp_path, monkeypatch):
    for key in ('SCANNER_ENGINE', 'SIGNAL_FIRE_MODE', 'LIVE_INTRABAR_SIGNAL_ENTRIES',
                'UT_SIGNAL_MODE', 'UTS_LANE_ALLOW_INTRABAR_ENTRIES',
                'UTS_LANE_INTRABAR_WAIT_SECONDS', 'UTS_LANE_INTRABAR_WAIT_SECONDS_1MIN',
                'UTS_LANE_REPAINT_CHECK_SECONDS'):
        monkeypatch.delenv(key, raising=False)
    saved = tmp_path / 'timing.env'
    saved.write_text('SCANNER_ENGINE=ut_smart\nLIVE_INTRABAR_SIGNAL_ENTRIES=true\n'
        'UT_SIGNAL_MODE=realtime\nUTS_LANE_ALLOW_INTRABAR_ENTRIES=true\n'
        'UTS_LANE_INTRABAR_WAIT_SECONDS=20\nUTS_LANE_INTRABAR_WAIT_SECONDS_1MIN=7\n'
        'UTS_LANE_REPAINT_CHECK_SECONDS=10\n', encoding='utf-8')
    settings = Settings(_env_file=saved)
    assert uts_lane.intrabar_enabled(settings)
    assert settings.uts_lane_intrabar_wait_seconds_1min == 7.
    assert settings.uts_lane_intrabar_wait_seconds == 20.
    assert settings.uts_lane_repaint_check_seconds == 10.
