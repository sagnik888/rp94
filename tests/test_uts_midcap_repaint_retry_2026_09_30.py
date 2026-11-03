"""Index-specific UTS premium caps and fresh admission after repaint exits."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from config.settings import Settings
from engine import uts_lane
from engine.market_regime import option_stop_tolerance_pct
from engine.signal_processor import SignalProcessor
from scanner import Scanner
from tests.test_ut_smart_live_path_2026_09_26 import live


@pytest.mark.parametrize('instrument,tiers', [
    ('NIFTY', (5., 10., 15.)), ('BANKNIFTY', (5., 10., 15.)),
    ('SENSEX', (5., 10., 15.)), ('MIDCPNIFTY', (3.5, 7., 10.)),
    ('MIDCAPNIFTY', (3.5, 7., 10.)),
])
@pytest.mark.parametrize('timeframe', ['1min', '5min', '15min'])
@pytest.mark.parametrize('regime', ['UNKNOWN', 'RANGING', 'VOLATILE', 'TRENDING_UP'])
def test_uts_index_timeframe_stop_matrix(instrument, tiers, timeframe, regime):
    setup = NS(instrument=instrument, timeframe=timeframe, direction='LONG',
               source_lane='UTS', adx_value=20., rsi_value=56.)
    expected = tiers[0] if timeframe == '1min' else tiers[2] if timeframe == '15min' and regime in {'RANGING','VOLATILE'} else tiers[1]
    assert uts_lane.premium_cap_pct(setup, Settings(_env_file=None), regime=regime) == expected
    assert option_stop_tolerance_pct(50., regime, setup=setup) == expected
    assert uts_lane.premium_stop_distance(100., 100., .5, setup=setup, regime=regime) == expected


@pytest.mark.parametrize('flag', ['one_way_cascade_entry', 'uts_multi_index_alignment'])
@pytest.mark.parametrize('timeframe', ['1min', '5min', '15min'])
def test_midcap_special_setups_never_inherit_frozen_15_percent_cap(flag, timeframe):
    setup = NS(instrument='MIDCPNIFTY', timeframe=timeframe, direction='LONG',
        source_lane='UTS', **{flag: True}, quality_verdict={'policy_version': 'uts_native.v1',
            'checks': {'uts_premium_cap_pct': 15., 'uts_stop_extended': True}})
    assert uts_lane.premium_cap_pct(setup, regime='VOLATILE') == 3.5


def test_midcap_controlled_extension_is_7_to_10_percent():
    from tests.test_uts_user_cap_policy_2026_09_30 import extension_case
    trade, result, context, now = extension_case()
    trade.instrument = result['instrument'] = 'MIDCPNIFTY'
    trade.initial_stop = trade.current_stop = trade.trailing_stop = 93.
    trade.quality_verdict['checks']['uts_premium_cap_pct'] = 7.
    assert uts_lane.extension_stop(trade, 94., result=result, context=context, now=now) == 90.


def _retry_host(monkeypatch, timeframe='5min'):
    stamp = datetime(2026, 9, 30, 10)
    now = stamp + timedelta(seconds=40)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            from trading.trade_manager import IST
            return IST.localize(now) if tz else now
    monkeypatch.setattr('scanner.datetime', Clock)
    settings = Settings(_env_file=None, scanner_engine='ut_smart')
    monkeypatch.setattr('scanner.get_settings', lambda: settings)
    monkeypatch.setattr('config.settings.get_settings', lambda: settings)
    closed = NS(id='closed-1', instrument='NIFTY', timeframe=timeframe, direction='LONG',
        source_engine='ut_smart', source_lane='UTS', status='CLOSED', exit_reason='UTS_REPAINT',
        entered_intracandle=True, signal_time=stamp, exit_time=now, is_ghost=False, is_recovery=False)
    candidate = NS(instrument='NIFTY', timeframe=timeframe, direction='LONG', action='ENTRY',
        source_lane='UTS', source_engine='ut_smart', signal_timestamp=stamp, trading_symbol='NIFTY-CE')
    sc = Scanner.__new__(Scanner)
    sc.mode, sc.trades = 'REAL', NS(open_trades={}, closed_trades=[closed])
    identity = f'{stamp.isoformat()}|BUY|ut_smart'
    peer = f'{stamp.isoformat()}|SELL|ut_smart'
    key = f'NIFTY_{timeframe}'
    sc._last_signal_time = {key: stamp}
    sc._last_signal_identity = {key: {identity, peer}}
    sc._last_live_signal_candle_time = {key: stamp}
    sc._live_signal_recheck_at, sc._live_signal_recheck_eligible = {}, {}
    sc._pending_live_signals, sc._candidate_process_cache = {}, {'NIFTY': 'old decision'}
    sc.mtf = NS(require_fresh_result=lambda *a, **kw: None)
    sc._diag_inc = lambda *a: None
    return sc, closed, candidate, stamp, key, identity, peer


@pytest.mark.parametrize('timeframe', ['1min','5min','15min'])
def test_confirmed_repaint_exit_releases_only_its_signal_and_gives_retry_new_lifecycle(monkeypatch, timeframe):
    sc, closed, candidate, stamp, key, identity, peer = _retry_host(monkeypatch, timeframe)
    assert sc._release_uts_repaint_signal(closed)
    assert identity not in sc._last_signal_identity[key] and peer in sc._last_signal_identity[key]
    assert SignalProcessor._live_signal_is_new(stamp, 'BUY', sc._last_signal_time[key], sc._last_signal_identity[key])
    assert sc._live_signal_recheck_eligible[key] == identity
    retry_key = sc._candidate_lifecycle_key(candidate)
    assert 'UTS_RETRY:closed-1' in retry_key
    closed2 = NS(**{**vars(closed), 'id': 'closed-2', '_uts_repaint_rearmed': False})
    closed.exit_time -= timedelta(microseconds=1)
    sc.trades.closed_trades.append(closed2)
    assert sc._candidate_lifecycle_key(candidate) != retry_key
    assert not sc._release_uts_repaint_signal(closed)  # Duplicate callback cannot reset a new buffer.


@pytest.mark.parametrize('change', ['pending', 'different_reason', 'confirmed_entry', 'ghost'])
def test_repaint_retry_is_not_armed_for_unconfirmed_closure_or_other_exits(monkeypatch, change):
    sc, closed, _, _, key, identity, _ = _retry_host(monkeypatch)
    if change == 'pending': closed.status = 'OPEN'
    if change == 'different_reason': closed.exit_reason = 'TRAILING_STOP'
    if change == 'confirmed_entry': closed.entered_intracandle = False
    if change == 'ghost': closed.is_ghost = True
    assert not sc._release_uts_repaint_signal(closed)
    assert identity in sc._last_signal_identity[key]


def test_midcap_verdict_freezes_new_profile_and_translates_legacy_caps():
    setup = NS(instrument='MIDCPNIFTY', timeframe='15min', direction='LONG', source_lane='UTS')
    setup.quality_verdict = uts_lane.entry_verdict(setup, regime='VOLATILE').to_dict()
    assert setup.quality_verdict['checks']['uts_premium_cap_pct'] == 10.
    assert uts_lane.premium_cap_pct(setup, regime='UNKNOWN') == 10.
    setup.quality_verdict['checks'].pop('uts_premium_cap_profile')
    assert uts_lane.premium_cap_pct(setup, regime='VOLATILE') == 7.
    setup.quality_verdict['checks']['uts_premium_cap_pct'] = 15.
    assert uts_lane.premium_cap_pct(setup, regime='UNKNOWN') == 10.
    assert uts_lane.premium_cap_pct(setup, operator_cap=3.) == 3.


def test_midcap_broker_extension_confirms_93_to_90_and_never_allows_85():
    from tests.test_uts_extension_broker_integration_2026_09_30 import case
    manager, trade, order, writes = case()
    trade._uts_extension_request = None
    trade.instrument, trade.trading_symbol = 'MIDCPNIFTY', 'MIDCPNIFTY-CE'
    trade.initial_stop = trade.current_stop = trade.trailing_stop = trade.protective_trigger = 93.
    trade.current_price, trade.protective_quantity = 94., 120
    trade.quality_verdict['checks']['uts_premium_cap_pct'] = 7.
    manager.options_sl_pct = 10.
    manager._transactable_quantity = lambda *a: 120
    order.update(tradingsymbol=trade.trading_symbol, triggerprice=93., quantity=120)
    assert not manager.request_uts_stop_extension(trade,85.,quote_verified=True,now=datetime.now())
    assert trade._uts_extension_request is None
    assert not manager.request_uts_stop_extension(trade,90.,quote_verified=True,now=datetime.now())
    assert trade._uts_extension_request
    assert manager._apply_uts_stop_extension(trade,120)
    assert trade.current_stop == trade.protective_trigger == 90.
    assert trade.initial_stop == 93. and trade._entry_option_stop_cap_pct == 10.
    assert len(writes) == 1 and order['triggerprice'] == 90.


def test_actual_option_replay_preserves_midcap_identity_without_a_setup_instrument():
    import pandas as pd
    start = datetime(2026,9,30,10)
    frame = pd.DataFrame({'open': [100.,100.,94.], 'high': [101.,101.,95.],
        'low': [99.,92.,93.], 'close': [100.,94.,94.]},
        index=pd.date_range(start, periods=3, freq='min'))
    result = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        frame,start,start+timedelta(minutes=3),50.,3.,replay_source_lane='UTS',
        tf15_instrument='MIDCPNIFTY',tf15_timeframe='5min',live_exits=False)
    assert result['reason'] == 'SL HIT' and result['exit'] == 93.


@pytest.mark.parametrize('timeframe', ['5min','15min'])
def test_real_signal_processor_rebuffers_the_same_arrow_after_terminal_repaint(live, monkeypatch, timeframe):
    from trading.trade_manager import IST
    from engine.execution_service import ExecutionService
    from tests.test_uts_intrabar_buffer_integration_2026_09_30 import _execution_host
    h = live(tf=timeframe, settings_overrides={
        'signal_fire_mode': 'INTRACANDLE', 'live_intrabar_signal_entries': True,
        'ut_signal_mode': 'realtime', 'uts_lane_allow_intrabar_entries': True})
    clock = [h.sig.timestamp.replace(tzinfo=None) + timedelta(minutes=2)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(clock[0]) if tz else clock[0]
    monkeypatch.setattr('scanner.datetime',Clock)
    monkeypatch.setattr('engine.signal_processor.datetime',Clock)
    h.sig.uts_forming = True
    h.sc._live_signal_recheck_eligible, h.sc._live_signal_recheck_at = {}, {}
    first = h.processor.process_best_signal(*h.args)[0]
    first.lifecycle_id = h.sc._candidate_lifecycle_key(first)
    closed = NS(id='filled-repaint-1', instrument='NIFTY', direction=first.direction,
        timeframe=timeframe, source_lane='UTS', source_engine='ut_smart', status='CLOSED',
        entered_intracandle=True, signal_time=h.sig.timestamp, exit_time=clock[0],
        exit_reason='UTS_REPAINT', lifecycle_id=first.lifecycle_id)
    h.sc.trades.closed_trades.append(closed)
    assert h.sc._release_uts_repaint_signal(closed)
    retried = h.processor.process_best_signal(*h.args)
    assert len(retried) == 1 and retried[0].status == 'TRADE SIGNAL'
    assert retried[0].signal_timestamp == first.signal_timestamp
    assert h.sc._candidate_lifecycle_key(retried[0]) != first.lifecycle_id
    assert h.sc._candidate_history_key(retried[0]) != h.sc._candidate_history_key(first)
    # The fresh candidate must pass the normal buffer; it is not executed by re-arming.
    execution = _execution_host(monkeypatch,timeframe)
    execution.clock[0] = clock[0]
    import asyncio
    service = ExecutionService(execution.scanner)
    asyncio.run(service.coordinate_and_execute(retried))
    assert execution.scanner._pending_live_signals and not execution.records
    from config.settings import get_settings
    wait = float(get_settings().uts_lane_intrabar_wait_seconds)
    execution.clock[0] += timedelta(seconds=wait - .1)
    asyncio.run(service.coordinate_and_execute([]))
    assert not execution.records
    execution.clock[0] += timedelta(seconds=.1)
    asyncio.run(service.coordinate_and_execute([]))
    assert execution.records[-1][1] == 'STABILIZED'


@pytest.mark.asyncio
async def test_repaint_exit_intent_survives_arrow_return_before_contract_quote_recovers(monkeypatch):
    from tests.test_uts_intrabar_buffer_integration_2026_09_30 import _worker_host
    h = _worker_host(monkeypatch)
    h.present[0] = False
    h.host._active_option_decision_mark = lambda *a: (90.,'stale',False)
    await h.worker.run_once(h.stamp+timedelta(seconds=30))
    assert h.trade.uts_repaint_exit_required and not h.exits
    h.present[0] = True
    h.host._active_option_decision_mark = lambda *a: (101.,'fresh-contract',True)
    await h.worker.run_once(h.stamp+timedelta(seconds=40))
    assert h.exits[0][0] == ('UTS-1',101.,'UTS_REPAINT')


def test_buffer_discard_with_source_identity_reopens_only_the_repainted_side(monkeypatch):
    from engine.execution_service import ExecutionService
    from tests.test_uts_intrabar_buffer_integration_2026_09_30 import _execution_host
    import asyncio
    h = _execution_host(monkeypatch,'5min')
    stamp = h.candidate.signal_timestamp
    key = 'NIFTY_5min'
    identity, peer = f'{stamp.isoformat()}|BUY|ut_smart', f'{stamp.isoformat()}|SELL|ut_smart'
    h.scanner._last_signal_time[key] = stamp
    h.scanner._last_signal_identity[key] = {identity,peer}
    service = ExecutionService(h.scanner)
    asyncio.run(service.coordinate_and_execute([h.candidate]))
    h.state[0] = 'ABSENT'
    h.clock[0] += timedelta(seconds=2)
    asyncio.run(service.coordinate_and_execute([]))
    assert h.scanner._last_signal_identity[key] == {peer}
    assert not h.scanner._pending_live_signals


def test_later_regular_stop_blocks_restart_rearming_of_an_old_repaint(monkeypatch):
    sc, closed, candidate, _, _, _, _ = _retry_host(monkeypatch)
    newer = NS(**{**vars(closed), 'id':'regular-stop-2', 'exit_reason':'TRAILING_STOP'})
    sc.trades.closed_trades.append(newer)
    assert sc._uts_repaint_retry_parent(candidate) is None
    assert not sc._release_uts_repaint_signal(closed)


def test_retry_lifecycle_stays_distinct_when_the_returning_arrow_closes(monkeypatch):
    sc, closed, candidate, stamp, _, _, _ = _retry_host(monkeypatch)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            from trading.trade_manager import IST
            now = stamp + timedelta(minutes=5)
            return IST.localize(now) if tz else now
    monkeypatch.setattr('scanner.datetime',Clock)
    assert not sc._release_uts_repaint_signal(closed)
    assert 'UTS_RETRY:closed-1' in sc._candidate_lifecycle_key(candidate)


def test_repaint_exit_intent_is_persisted_with_the_trade(tmp_path):
    from data.sqlite_db import DatabaseManager
    from trading.trade_manager import Trade
    trade = Trade(
        id='midcap-repaint', instrument='MIDCPNIFTY', timeframe='5min',
        direction='LONG', entry_price=100., entry_time=datetime(2026,9,30,10),
        trailing_stop=93., current_stop=93., lots=1, lot_size=120, grade='C',
        inst_type='OPT', source_lane='UTS', initial_stop=93.,
        uts_repaint_exit_required=True,
    )
    database = object.__new__(DatabaseManager)
    database._init_db(str(tmp_path/'repaint-intent.db'))
    try:
        assert database.save_trade(trade.to_dict())
        restored = Trade.from_dict(database.load_all_trades()[0])
        assert restored.uts_repaint_exit_required is True
    finally:
        database.conn.close()


@pytest.mark.parametrize('timeframe,regime,cap,stop', [
    ('1min','UNKNOWN',3.5,96.5), ('5min','UNKNOWN',7.,93.),
    ('15min','VOLATILE',10.,90.),
])
def test_midcap_actual_fill_tightens_legacy_fifty_percent_stop(timeframe, regime, cap, stop):
    from tests.test_uts_user_cap_policy_2026_09_30 import manager, setup
    value, _ = manager()
    trade = setup(instrument='MIDCPNIFTY',timeframe=timeframe,inst_type='OPT',
        is_recovery=False,entry_price=100.,current_stop=50.,market_regime=regime)
    value._cap_option_entry_stop(trade)
    assert trade.current_stop == trade.initial_stop == stop
    assert trade._entry_option_stop_cap_pct == cap


@pytest.mark.asyncio
async def test_worker_manual_repaint_notification_keeps_the_running_loop(monkeypatch):
    import asyncio
    from tests.test_uts_intrabar_buffer_integration_2026_09_30 import _worker_host
    h = _worker_host(monkeypatch,auto_mode=False)
    notifications, loops = [], []
    h.trade.quote_verified = True
    h.trade.target, h.trade.rr_ratio = 120., 2.
    h.trade.entry_price, h.trade.trailing_stop, h.trade.quantity = 100., 90., 65
    h.host._reset_daily_counters = lambda: None
    h.host._spread_aware_exit_decision = lambda *a: (False,'')
    h.host._close_and_record = lambda *a, **kw: Scanner._close_and_record(h.host,*a,**kw)
    def manager(loop):
        loops.append(loop)
        return NS(send_trade_notification=lambda **kw: notifications.append(kw))
    monkeypatch.setattr('scanner.get_notification_manager',manager)
    h.present[0] = False
    await h.worker.run_once(h.stamp+timedelta(seconds=30))
    assert loops == [asyncio.get_running_loop()]
    assert notifications[0]['exit_reason'] == 'UTS_REPAINT'
    assert callable(notifications[0]['on_execute'])
    assert h.trade.pending_exit and not h.exits


@pytest.mark.parametrize('operator_cap,absolute_cap,allowed', [
    (10.,2000.,True), (9.9,2000.,False), (10.,1000.,False),
])
def test_midcap_extension_budget_uses_ten_percent_and_preserves_rupee_limits(operator_cap, absolute_cap, allowed):
    from engine.risk_manager import RiskManager
    from tests.test_uts_user_cap_policy_2026_09_30 import extension_case
    scanner = Scanner.__new__(Scanner)
    scanner.auto_mode = True
    risk = RiskManager.__new__(RiskManager)
    risk.options_sl_pct, risk.futures_sl_pct = operator_cap, .5
    risk.max_trade_loss_abs = absolute_cap
    risk.capital_total = risk.capital_opt = 100000.
    risk.max_daily_loss_pct = risk.risk_opt_pct = 2.
    risk.circuit_breaker_slippage_bps = 10.
    risk.can_trade = lambda _: True
    scanner.risk_manager = risk
    trade, _, _, _ = extension_case()
    trade.instrument, trade.id = 'MIDCPNIFTY', 'midcap-budget'
    trade.lots, trade.lot_size, trade.charges = 1, 120, 0.
    scanner.trades = NS(open_trades={trade.id:trade},closed_trades=[])
    assert scanner._uts_stop_extension_budget_ok(trade,90.) is allowed
