"""Regressions reproduced while auditing the October 1 live session."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from engine.execution_service import ExecutionService
from engine.trade_row_adapter import reconcile_legacy_trade_row
from scanner import Scanner
from tests.test_ut_smart_live_path_2026_09_26 import _flip
from tests.test_uts_intrabar_audit_2026_10_01 import _publish
from tests.test_uts_intrabar_buffer_integration_2026_09_30 import _execution_host
from tests.test_zero_qty_row_does_not_close_a_live_entry_2026_08_03 import (
    _Broker, _manager, _trade, SQUARED_OFF_ROW,
)
from trading.trade_dashboard import _serialize_trade_row


@pytest.mark.asyncio
@pytest.mark.parametrize('timeframe,wait', [('1min', 6), ('5min', 20), ('15min', 20)])
async def test_buffer_cannot_mature_from_an_observation_near_its_start(monkeypatch, timeframe, wait):
    host = _execution_host(monkeypatch, timeframe)
    host.scanner._repaint_signal_status = Scanner._repaint_signal_status.__get__(host.scanner)
    service = ExecutionService(host.scanner)
    start = host.clock[0]
    await service.coordinate_and_execute([host.candidate])
    host.clock[0] = start + timedelta(seconds=1)
    _publish(host, [_flip(host.candidate.signal_timestamp)])
    await service.coordinate_and_execute([])
    host.clock[0] = start + timedelta(seconds=wait)
    await service.coordinate_and_execute([])
    assert host.scanner._pending_live_signals
    assert not any(decision == 'STABILIZED' for _, decision in host.records)
    # A current calculation now proves the arrow disappeared during that gap.
    _publish(host, [])
    await service.coordinate_and_execute([])
    assert host.records[-1][1] == 'REJECTED'
    assert not host.scanner._pending_live_signals


@pytest.mark.parametrize('entry,actual,aggregate,quantity', [
    (183.7, 165.5, 177.15, 65), (204.9, 194.2, 186.25, 20),
    (201.45, 193.95, 205.9, 120),
])
@pytest.mark.parametrize('postback', [False, True])
def test_flat_protective_fill_keeps_its_order_price_through_dashboard(
    monkeypatch, entry, actual, aggregate, quantity, postback,
):
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade('OPEN', broker_quantity=quantity, pending=False)
    trade.entry_price, trade.lot_size, trade.data_provenance = entry, quantity, 'LIVE'
    trade.protective_order_id, trade.protective_quantity = 'stop-current', quantity
    trade.current_stop = entry * .9
    manager.open_trades[trade.id] = trade
    manager._mark_dirty = lambda: None
    manager._record_fill = lambda *a, **kw: None
    manager._record_leg_fill_from_order = lambda *a, **kw: True
    manager._ledger_owned_quantity = lambda *a: 0
    manager._stamp_fill_quality = lambda *a, **kw: None
    manager._record_exit_slippage = lambda *a, **kw: None
    broker.get_fill = lambda order: (dict(state='FILLED', quantity=quantity,
        average_price=actual, last_fill_at='2026-10-01 10:51:41')
        if order == 'stop-current' else dict(state='UNKNOWN'))
    if postback:
        assert manager.on_broker_postback(dict(order_id='stop-current', status='COMPLETE',
            filled_quantity=quantity, average_price=actual,
            exchange_update_timestamp='2026-10-01 10:51:41'))
    manager.reconcile_broker_positions({'data': [{**SQUARED_OFF_ROW,
        'sellavgprice': aggregate, 'realised': 0}]})
    assert trade.exit_price == actual
    assert trade.exit_order_id == 'stop-current'
    assert trade.exit_reason == 'STOP_HIT'
    assert not broker.cancelled, 'A filled stop must not be cancelled or reported unowned'
    assert trade.pnl == pytest.approx((actual-entry)*quantity - trade.charges)
    row = reconcile_legacy_trade_row(_serialize_trade_row(manager, trade))
    assert row['exit_price'] == actual
    assert row['pnl'] == pytest.approx(round(trade.pnl, 2))
    assert trade.to_dict()['pnl'] == pytest.approx(round(trade.pnl, 2))


def test_tracker_does_not_count_an_unfilled_margin_rejection_as_a_thirteenth_fill():
    from scripts.live_session_tracker import Tracker
    tracker = Tracker.__new__(Tracker)
    payload = dict(open=[], closed=[dict(id='T1', status='CLOSED', execution_status='CLOSED'),
        dict(id='T2', status='CLOSED', execution_status='REJECTED')], closed_total=2,
        session_rows_total=2, session_closed_total=2, meta={'source': 'mixed', 'mode': 'REAL'})
    result = tracker._summarize_trade_contract(payload, {'session_stats': {'entries': 1, 'exits': 1}})
    assert result['status'] == 'OK'
    assert result['raw_open_closed_rows_total'] == 2
    assert result['comparison_rows_total'] == result['comparison_closed_total'] == 1


@pytest.mark.parametrize('fill', [
    {'state': 'UNKNOWN'},
    {'state': 'FILLED', 'quantity': 20, 'average_price': 90.},
    {'state': 'FILLED', 'quantity': 75, 'average_price': float('nan')},
    {'state': 'FILLED', 'quantity': 75, 'average_price': 0.},
])
def test_unproven_or_partial_protective_fill_cannot_take_exit_ownership(fill):
    broker = _Broker()
    manager = _manager(broker)
    trade = _trade('OPEN', broker_quantity=75, pending=False)
    trade.protective_order_id, trade.protective_quantity = 'stop-current', 75
    trade.exit_order_id = 'separate-exit'
    broker.get_fill = lambda _: fill
    assert not manager._adopt_completed_protective_fill(trade)
    assert trade.exit_order_id == 'separate-exit'
    assert trade.protective_order_id == 'stop-current'


def test_live_reentry_checks_positions_now_not_at_the_source_candle_open(monkeypatch):
    from datetime import datetime
    from config.settings import Settings
    monkeypatch.setattr('scanner.get_settings', lambda: Settings(_env_file=None))
    scanner = Scanner.__new__(Scanner)
    scanner.mode = 'REAL'
    scanner.trades = SimpleNamespace(open_trades={})
    now = datetime(2026, 10, 1, 9, 46, 40)
    scanner._session_squareoff_clock = lambda: now
    stamp = datetime(2026, 10, 1, 9, 45)
    entry = SimpleNamespace(instrument='MIDCPNIFTY', timeframe='5min', direction='SHORT',
        action='ENTRY', lifecycle_id='already-exited', signal_timestamp=stamp,
        accepted_by_gate=True, confidence=.3, inst_type='OPT',
        price=100.0, entry_price=100.0, stop=95.0, lots=1, lot_size=120,
        trading_symbol='MIDCPNIFTY26OCT2624000PE', option_type='PE')
    exit_row = SimpleNamespace(**{**vars(entry), 'action': 'EXIT',
        'exit_timestamp': datetime(2026, 10, 1, 9, 46, 17)})
    scanner._get_session_trade_candidates = lambda *a: [entry, exit_row]
    scanner.log_event = scanner._record_live_reject = lambda *a, **kw: None
    scanner._concurrency_cascade_override = lambda *a, **kw: (False, '')
    candidate = SimpleNamespace(**{**vars(entry), 'lifecycle_id': 'fresh-retry'})
    assert scanner._prepare_candidate_for_concurrency(candidate)


@pytest.mark.asyncio
async def test_timed_out_analysis_releases_undelivered_signal_for_a_fresh_scan(monkeypatch):
    """Execute the production nested coordinator with an actual executor timeout."""
    import ast
    import asyncio
    import inspect
    import textwrap
    import time
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime
    import scanner as module
    from engine.signal_processor import SignalProcessor

    # This fixture consumes standalone UTS identities; never inherit a combined
    # scanner mode from the operator's .env.
    monkeypatch.setattr(module, 'get_settings', lambda: SimpleNamespace(
        scanner_engine='ut_smart', uts_lane_enabled=True))
    host = Scanner.__new__(Scanner)
    host.mode, host.simulation_id = 'REAL', 1
    host._live_processing_futures, host._live_processing_started = {}, {}
    host._diagnostics = {}
    host._last_signal_identity, host._last_signal_time = {}, {}
    host._candidate_process_cache = {}
    host.candles = SimpleNamespace(get_latest_price=lambda _: 100.)
    host._diag_inc = host._record_live_processing_timeout = lambda *a, **kw: None
    host._set_live_entry_data_block = host._clear_live_entry_data_block = lambda *a, **kw: None
    host._clear_live_inflight_skip = lambda *a: None
    host._build_live_fallback_result = lambda *a: ({'fallback': True}, [])
    host.mtf = SimpleNamespace(require_fresh_result=lambda *a, **kw: None)
    stamp = datetime(2026, 10, 1, 10, 20)
    candidate = SimpleNamespace(instrument='NIFTY', timeframe='5min', direction='SHORT',
        source_engine='ut_smart', signal_timestamp=stamp, source_signal_type='SELL', action='ENTRY')
    identity = f'{stamp.isoformat()}|SELL|ut_smart'
    def calculate(*args):
        time.sleep(.04)
        SignalProcessor._remember_live_signal_identity(host, 'NIFTY_5min', stamp, identity)
        return {}, [candidate]
    host._process_instrument = calculate
    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._scan_cycle)))
    body = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                and node.name == 'process_task')
    namespace = dict(vars(module), self=host, cycle_is_warmup=False,
        ScannerRuntimeSettings=SimpleNamespace(from_flat=lambda _: SimpleNamespace(
            live_scan=SimpleNamespace(index_deadline_seconds=.005))))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[body], type_ignores=[])),
                 '<production process_task>', 'exec'), namespace)
    with ThreadPoolExecutor(max_workers=1) as executor:
        host._scanner_live_executor = executor
        _, result = await namespace['process_task']('NIFTY', {})
        assert result[0]['fallback']
        await asyncio.sleep(.08)
    assert identity not in SignalProcessor._live_signal_identities(host._last_signal_identity.get('NIFTY_5min'))
    assert getattr(host, '_analysis_event_generation', 0) > 0


@pytest.mark.parametrize('protected', ['accepted', 'changed_session', 'historical'])
def test_timeout_cleanup_does_not_reopen_owned_or_other_session_signals(protected):
    from concurrent.futures import Future
    from datetime import datetime
    stamp = datetime(2026, 10, 1, 10, 20)
    candidate = SimpleNamespace(instrument='NIFTY', timeframe='5min', direction='SHORT',
        source_engine='ut_smart', signal_timestamp=stamp, source_signal_type='SELL',
        action='ENTRY', accepted_by_gate=protected == 'accepted')
    host = Scanner.__new__(Scanner)
    host.mode = 'HISTORICAL' if protected == 'historical' else 'REAL'
    host.simulation_id = 2 if protected == 'changed_session' else 1
    identity = f'{stamp.isoformat()}|SELL|ut_smart'
    host._last_signal_identity = {'NIFTY_5min': {identity}}
    future = Future()
    future.set_result(({}, [candidate]))
    host._release_undelivered_live_analysis(future, 1)
    assert host._last_signal_identity['NIFTY_5min'] == {identity}
    assert not getattr(host, '_analysis_event_generation', 0)
