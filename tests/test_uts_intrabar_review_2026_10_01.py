"""Independent verification of UTS buffer freshness and restart persistence."""
from datetime import datetime, timedelta

import pytest

from scanner import Scanner
from tests.test_uts_intrabar_audit_2026_10_01 import _publish
from tests.test_uts_intrabar_buffer_integration_2026_09_30 import _execution_host, _worker_host
from tests.test_ut_smart_live_path_2026_09_26 import _flip, _uts_result


@pytest.mark.parametrize('present', [False, True])
def test_populated_uts_result_requires_fresh_evaluation_before_buffer_decision(monkeypatch, present):
    host = _execution_host(monkeypatch, '15min')
    stamp = host.candidate.signal_timestamp
    signal_stamp = stamp if present else stamp - timedelta(minutes=15)
    result = _publish(host, [_flip(signal_stamp)])
    result['uts_evaluation']['observed_at'] = host.clock[0] - timedelta(seconds=31)
    # Object provenance can remain ready on a cached result while another index
    # or quote check delays the next execution pass beyond the freshness bound.
    assert Scanner._repaint_signal_status(host.scanner, host.candidate, stamp, 0.) == 'UNKNOWN'


@pytest.mark.asyncio
@pytest.mark.parametrize('auto_mode', [False, True])
async def test_normal_monitor_persists_latch_and_restored_worker_keeps_exit_authority(monkeypatch, tmp_path, auto_mode):
    from data.sqlite_db import DatabaseManager
    from trading.trade_manager import Trade
    from tests.test_s1_lane_2026_09_23 import _monitor

    stamp = datetime(2026, 9, 24, 10, 30)
    trade = Trade(id='T1', instrument='NIFTY', timeframe='5min', direction='LONG',
        entry_price=100., entry_time=stamp-timedelta(seconds=30), trailing_stop=90.,
        current_stop=90., initial_stop=90., lots=1, lot_size=65, grade='C',
        inst_type='OPT', source_engine='ut_smart', source_lane='UTS', signal_time=stamp,
        entered_intracandle=True, status='OPEN', execution_status='OPEN')
    database = object.__new__(DatabaseManager)
    database._init_db(str(tmp_path/'normal-monitor-repaint.db'))
    persisted = []
    original = Scanner._update_active_trades

    def monitor_with_persistence(scanner, **kwargs):
        def dirty():
            persisted.append(bool(trade.uts_repaint_exit_required))
            assert database.save_trade(trade.to_dict())
        scanner.trades._mark_dirty = dirty
        return original(scanner, **kwargs)

    monkeypatch.setattr(Scanner, '_update_active_trades', monitor_with_persistence)
    try:
        closed, _, _ = _monitor(monkeypatch, trade, 23405., 100., auto_mode=auto_mode,
            engine_result=_uts_result(stamp, stamp, timeframe='5min'))
        assert closed == ['UTS_REPAINT'] and persisted == [True]
        restored = Trade.from_dict(database.load_all_trades()[0])
        assert restored.uts_repaint_exit_required

        host = _worker_host(monkeypatch, auto_mode=auto_mode)
        host.trade = restored
        host.host.trades.open_trades = {restored.id: restored}
        host.worker._calculate = lambda *args: pytest.fail('Latched exit must not depend on returning signal')
        await host.worker.run_once(stamp + timedelta(seconds=40))
        assert host.exits[0][0] == ('T1', 100., 'UTS_REPAINT')
        assert host.host.auto_mode is auto_mode
    finally:
        database.conn.close()
