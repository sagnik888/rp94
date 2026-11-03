"""Integration cases found in the October 1 UTS timing audit."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from engine.execution_service import ExecutionService
from scanner import Scanner
from tests.test_ut_smart_live_path_2026_09_26 import _flip, _uts_result
from tests.test_uts_intrabar_buffer_integration_2026_09_30 import _execution_host


def _publish(host, signals):
    """Publish a successful pipeline result, including its exact provenance."""
    scanner = host.scanner
    result = _uts_result(host.candidate.signal_timestamp, host.clock[0], signals,
                         timeframe=host.candidate.timeframe)
    key = f'{host.candidate.instrument}_{host.candidate.timeframe}'
    cutoff = max((item.get('result_required_after_epoch', 0.)
                  for item in scanner._pending_live_signals.values()), default=0.)
    scanner._cached_results[key] = result
    scanner._cached_result_metadata = {key: {
        'result_object': result,
        'result': {'status': 'ready', 'computed_at_epoch': cutoff + 1.},
    }}
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize('timeframe,wait', [('1min', 5.), ('5min', 15.), ('15min', 15.)])
async def test_authoritative_empty_uts_scan_discards_buffer_and_reappearance_restarts_it(monkeypatch, timeframe, wait):
    host = _execution_host(monkeypatch, timeframe)
    host.scanner._repaint_signal_status = Scanner._repaint_signal_status.__get__(host.scanner)
    service = ExecutionService(host.scanner)
    started = host.clock[0]
    await service.coordinate_and_execute([host.candidate])
    host.clock[0] = started + timedelta(seconds=2)
    _publish(host, [])
    await service.coordinate_and_execute([])
    assert not host.scanner._pending_live_signals
    assert host.records[-1][1] == 'REJECTED'

    host.clock[0] = started + timedelta(seconds=3)
    _publish(host, [_flip(host.candidate.signal_timestamp)])
    await service.coordinate_and_execute([host.candidate])
    host.clock[0] = started + timedelta(seconds=wait)
    _publish(host, [_flip(host.candidate.signal_timestamp)])
    await service.coordinate_and_execute([])
    assert not any(decision == 'STABILIZED' for _, decision in host.records)
    host.clock[0] = started + timedelta(seconds=wait + 3)
    _publish(host, [_flip(host.candidate.signal_timestamp)])
    await service.coordinate_and_execute([])
    assert host.records[-1][1] == 'STABILIZED'


def test_normal_exit_monitor_latches_observed_uts_repaint_before_gateway_retry(monkeypatch):
    from tests.test_s1_lane_2026_09_23 import _monitor, _open_trade

    # The gateway may defer an exit while obtaining broker authority. An observed
    # repaint must survive the arrow returning before the independent worker runs.
    stamp = datetime(2026, 9, 24, 10, 30)
    trade = _open_trade(source_engine='ut_smart', source_lane='UTS', signal_time=stamp,
                        entered_intracandle=True)
    result = _uts_result(stamp, stamp, timeframe='5min')
    closed, _, _ = _monitor(monkeypatch, trade, 23405., 100., engine_result=result)
    assert closed == ['UTS_REPAINT']
    assert getattr(trade, 'uts_repaint_exit_required', False)


@pytest.mark.parametrize('invalid', ['stale', 'error', 'coverage', 'instrument', 'engine'])
def test_empty_buffer_result_requires_current_native_evidence(monkeypatch, invalid):
    host = _execution_host(monkeypatch, '5min')
    result = _publish(host, [])
    if invalid == 'stale':
        result['uts_evaluation']['observed_at'] = host.clock[0] - timedelta(seconds=31)
    elif invalid == 'error':
        result['error'] = 'fetch failed'
    elif invalid == 'coverage':
        result['uts_evaluation']['forming_bar'] = host.candidate.signal_timestamp - timedelta(minutes=5)
    elif invalid == 'instrument':
        result['instrument'] = 'BANKNIFTY'
    else:
        result['source_engine'] = 'utbot'
    assert Scanner._repaint_signal_status(host.scanner, host.candidate,
                                         host.candidate.signal_timestamp, 0.) == 'UNKNOWN'
    assert not getattr(host.candidate, 'uts_repaint_exit_required', False)


def test_observed_repaint_obligation_survives_returning_signal_and_missing_cache():
    from engine import uts_lane
    stamp = datetime(2026, 9, 30, 10)
    trade = SimpleNamespace(instrument='NIFTY', timeframe='5min', direction='LONG',
        signal_time=stamp, entered_intracandle=True, uts_confirmed=False)
    now = stamp + timedelta(seconds=30)
    assert uts_lane.repaint_exit(trade, _uts_result(stamp, now, [], timeframe='5min'), now) == 'REPAINT'
    returning = _uts_result(stamp, now, [_flip(stamp)], timeframe='5min')
    assert uts_lane.repaint_exit(trade, returning, now) == 'REPAINT'
    assert uts_lane.repaint_exit(trade, None, now) == 'REPAINT'
