"""UTS native admission, capital caps and the one controlled stop extension."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
import threading
import time

import pytest

from engine import uts_lane
from engine.market_regime import option_stop_tolerance_pct
from trading.trade_manager import TradeManager, Trade


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    value = NS(uts_lane_enabled=True, options_sl_pct=15.0)
    monkeypatch.setattr('config.settings.get_settings', lambda: value)
    return value


def setup(**changes):
    values = dict(source_lane='UTS', instrument='NIFTY', timeframe='5min', direction='LONG',
                  action='ENTRY', adx_value=20., rsi_value=56., grade='C', confidence=.1,
                  volume_ratio=.8, volume_available=True, volume_basis='FUTURES_VOLUME')
    values.update(changes)
    return NS(**values)


@pytest.mark.parametrize('tf,regime,adx,rsi,pct', [
    ('5min','UNKNOWN',20,56,10), ('15min','UNKNOWN',20,56,10),
    ('5min','RANGING',20,56,10), ('15min','RANGING',20,56,15),
    ('15min','VOLATILE',20,56,15), ('1min','RANGING',20,56,5),
    ('5min','TRENDING_UP',35,55,5), ('15min','TRENDING_UP',35,55,5),
    ('5min','TRENDING_DOWN',40,56,10), ('5min','TRENDING_UP',34,55,10),
])
def test_native_stop_matrix_without_grade_or_atr(tf, regime, adx, rsi, pct, settings):
    obj = setup(timeframe=tf, adx_value=adx, rsi_value=rsi)
    assert uts_lane.premium_stop_distance(100, 100, .5, settings,
        setup=obj, regime=regime) == pct
    obj.grade, obj.confidence = 'A+', .99
    assert option_stop_tolerance_pct(50, regime, setup=obj, grade='A+', confidence=.99) == pct


@pytest.mark.parametrize('flag', ['one_way_cascade_entry','uts_multi_index_alignment',
                                 'market_ltp_shock_entry','market_wide_reversal_entry'])
@pytest.mark.parametrize('tf', ['1min','5min','15min'])
def test_cascade_and_alignment_are_always_five(flag, tf, settings):
    obj = setup(timeframe=tf, **{flag: True})
    assert uts_lane.premium_cap_pct(obj, settings, regime='VOLATILE') == 5.
    obj.quality_verdict = {'policy_version':'uts_native.v1', 'checks':{'uts_premium_cap_pct':15.}}
    assert uts_lane.premium_cap_pct(obj, settings, regime='VOLATILE') == 5.


@pytest.mark.parametrize('changes,rejected', [
    ({'adx_value':10,'rsi_value':49,'volume_ratio':.4},True),
    ({'adx_value':12,'rsi_value':49,'volume_ratio':.4},False),
    ({'adx_value':10,'rsi_value':55,'volume_ratio':.4},False),
    ({'adx_value':10,'rsi_value':49,'volume_ratio':.5},False),
    ({'adx_value':10,'rsi_value':49,'volume_ratio':.4,'volume_available':False},False),
    ({'adx_value':10,'rsi_value':49,'volume_ratio':.4,'volume_basis':'INDEX_TICK_ACTIVITY'},False),
    ({'direction':'SHORT','adx_value':10,'rsi_value':51,'volume_ratio':.4},True),
    ({'direction':'SHORT','adx_value':10,'rsi_value':45,'volume_ratio':.4},False),
    ({'adx_value':10,'rsi_value':None,'volume_ratio':.4},False),
    ({'adx_value':10,'rsi_value':float('nan'),'volume_ratio':.4},False),
])
def test_only_joint_extreme_weakness_rejects(changes, rejected, settings):
    obj = setup(**changes)
    verdict = uts_lane.entry_verdict(obj, settings)
    assert verdict.allowed is not rejected
    assert verdict.checks['grade_filter_enabled'] is False
    obj.action = 'EXIT'
    assert uts_lane.entry_verdict(obj, settings).allowed


def test_one_minute_needs_filtered_special_signal(settings):
    obj = setup(timeframe='1min')
    assert not uts_lane.entry_verdict(obj, settings).allowed
    for flag in ['volume_spike_entry','rare_burst_entry','is_reversion_scalp',
                 'market_ltp_shock_entry','market_wide_reversal_entry','one_way_cascade_entry']:
        obj = setup(timeframe='1min', **{flag:True})
        assert uts_lane.entry_verdict(obj, settings).allowed


def test_grade_preference_and_ranking_do_not_veto_uts():
    from scanner import Scanner
    from engine.candidate_selection import candidate_selection_score
    scanner = Scanner.__new__(Scanner)
    scanner.grade_pref = 'A+'
    obj = setup()
    assert scanner._passes_candidate_grade_preference(obj)[0]
    low = scanner._candidate_rank_score(obj)
    obj.grade, obj.confidence = 'A+', .99
    assert scanner._candidate_rank_score(obj) == low
    raw = dict(sig=obj, tf='5min', confidence=.1)
    assert candidate_selection_score(raw, lambda _: 'UTS') == 0
    raw['confidence'] = .99
    assert candidate_selection_score(raw, lambda _: 'UTS') == 0


def extension_case():
    now = datetime(2026,9,30,11,5,10)
    trade = setup(entry_price=100., initial_stop=90., current_stop=90., trailing_stop=90.,
        current_price=92., pending_exit=False, is_recovery=False, inst_type='OPT',
        id='uts-extension', protective_trigger=90.,
        protective_order_id='stop-1', trading_symbol='NIFTY-CE', symbol_token='fake',
        quality_verdict={'policy_version':'uts_native.v1','checks':{'uts_premium_cap_pct':10.}})
    result = dict(instrument='NIFTY',timeframe='5min',source_engine='ut_smart',
        uts_evaluation=dict(observed_at=now,last_closed_bar=now-timedelta(seconds=310)),
        state=dict(position=1,adx_value=28.,rsi_value=58.))
    context = dict(regime='RANGING',volume_ratio=1.3,volume_available=True,volume_basis='FUTURES_VOLUME')
    return trade, result, context, now


def test_extension_requires_fresh_strong_setup_and_can_happen_only_once(settings):
    trade, result, context, now = extension_case()
    assert uts_lane.extension_stop(trade,92.,result=result,context=context,now=now,settings=settings) == 85.
    uts_lane.commit_extension(trade,85.,now)
    assert trade.initial_stop == 90.  # Profit protection still measures original risk.
    assert uts_lane.extension_stop(trade,92.,result=result,context=context,now=now,settings=settings) == 0


@pytest.mark.parametrize('change', ['old_stop_hit','profit_stop','stale','error','wrong_tf',
    'wrong_index','wrong_side','weak_adx','weak_rsi','weak_volume','proxy','normal_regime',
    'already_attempted','operator_cap','cascade','alignment','recovery'])
def test_unsafe_extension_is_refused(change, settings):
    trade, result, context, now = extension_case()
    premium = 92.
    if change == 'old_stop_hit': premium = 90.
    if change == 'profit_stop': trade.current_stop = 108.
    if change == 'stale': result['uts_evaluation']['observed_at'] -= timedelta(seconds=31)
    if change == 'error': result['error'] = 'missing bars'
    if change == 'wrong_tf': result['timeframe'] = '15min'
    if change == 'wrong_index': result['instrument'] = 'BANKNIFTY'
    if change == 'wrong_side': result['state']['position'] = -1
    if change == 'weak_adx': result['state']['adx_value'] = 24.
    if change == 'weak_rsi': result['state']['rsi_value'] = 54.
    if change == 'weak_volume': context['volume_ratio'] = 1.19
    if change == 'proxy': context['volume_basis'] = 'INDEX_TICK_ACTIVITY'
    if change == 'normal_regime': context['regime'] = 'TRENDING_UP'
    if change == 'already_attempted': trade.quality_verdict['checks']['uts_stop_extension_attempted'] = True
    if change == 'operator_cap': settings.options_sl_pct = 10.
    if change == 'cascade': trade.one_way_cascade_entry = True
    if change == 'alignment': trade.uts_multi_index_alignment = True
    if change == 'recovery': trade.is_recovery = True
    assert uts_lane.extension_stop(trade,premium,result=result,context=context,now=now,settings=settings) == 0


def manager(*, accepted=True, status='TRIGGER PENDING', budget=True):
    value = TradeManager.__new__(TradeManager)
    value.mode, value.product_type, value.options_sl_pct = 'REAL', 'MIS', 15.
    value._protection_lock = threading.RLock()
    value._lock = threading.RLock()
    value.open_trades = {}
    calls = []
    value.broker = NS(get_order_status=lambda _: {'status':status,'orderid':'stop-1',
                          'tradingsymbol':'NIFTY-CE','transactiontype':'SELL','quantity':65,
                          'triggerprice':calls[-1][5] if calls and accepted else 90.},
                      modify_protective_stop=lambda *args: calls.append(args) or accepted)
    value.uts_stop_extension_guard = lambda *args: budget
    value._transactable_quantity = lambda *args: 65
    value.nudge_position_sync = lambda: None
    value._mark_dirty = lambda: None
    value._mark_recovery = lambda trade,reason: setattr(trade,'execution_status','RECOVERY_REQUIRED')
    return value, calls


def test_broker_stop_stays_at_ninety_until_extension_acknowledged():
    trade,_,_,now = extension_case()
    value,calls = manager()
    assert not value.request_uts_stop_extension(trade,85.,quote_verified=True,now=now)
    assert trade.current_stop == 90. and calls == []
    assert value._apply_uts_stop_extension(trade,65)
    assert trade.current_stop == trade.protective_trigger == 85.
    assert len(calls) == 1
    assert not value.request_uts_stop_extension(trade,85.,quote_verified=True,now=now)


@pytest.mark.parametrize('status',['OPEN','COMPLETE','CANCELLED','UNKNOWN'])
def test_triggered_or_unknown_broker_order_cannot_widen(status):
    trade,_,_,now = extension_case()
    value,calls = manager(status=status)
    value.request_uts_stop_extension(trade,85.,quote_verified=True,now=now)
    assert not value._apply_uts_stop_extension(trade,65)
    assert trade.current_stop == 90. and calls == []


@pytest.mark.parametrize('failure',['budget','unverified','hidden_touch','expired','broker_refusal','status_error','slow_status'])
def test_extension_failure_keeps_old_local_stop(failure):
    trade,_,_,now = extension_case()
    value,calls = manager(accepted=failure!='broker_refusal')
    value.request_uts_stop_extension(trade,85.,quote_verified=True,now=now)
    if failure == 'budget': value.uts_stop_extension_guard = lambda *args: False
    if failure == 'unverified': trade.quote_verified = False
    if failure == 'hidden_touch': trade._uts_stop_touch_pending = True
    if failure == 'expired': trade._uts_extension_request['at'] -= 3
    if failure == 'status_error':
        def unavailable(_): raise RuntimeError('offline')
        value.broker.get_order_status = unavailable
    if failure == 'slow_status':
        def delayed(_):
            trade._uts_extension_request = None
            request['at'] -= 3
            return {'status':'TRIGGER PENDING'}
        request = trade._uts_extension_request
        value.broker.get_order_status = delayed
    assert not value._apply_uts_stop_extension(trade,65)
    assert trade.current_stop == 90.
    if failure == 'broker_refusal':
        assert trade.execution_status == 'RECOVERY_REQUIRED'
        assert trade.quality_verdict['checks']['uts_stop_extension_attempted']
    else: assert calls == []


def test_native_cap_and_extension_survive_database_restore(tmp_path):
    from data.sqlite_db import DatabaseManager
    database = object.__new__(DatabaseManager)
    database._init_db(str(tmp_path/'isolated.db'))
    try:
        trade = Trade(id='uts-cap',instrument='NIFTY',timeframe='5min',direction='LONG',
            entry_price=100.,entry_time=datetime(2026,9,30,10),trailing_stop=90.,current_stop=90.,
            lots=1,lot_size=65,grade='C',inst_type='OPT',source_lane='UTS',initial_stop=90.,
            quality_verdict={'policy_version':'uts_native.v1','checks':{'uts_premium_cap_pct':10.}})
        uts_lane.commit_extension(trade,85.,datetime(2026,9,30,10,5))
        assert database.save_trade(trade.to_dict())
        restored = Trade.from_dict(database.load_all_trades()[0])
        assert restored.current_stop == 85.
        assert restored.quality_verdict['checks']['uts_stop_extended']
        assert uts_lane.premium_cap_pct(restored) == 15.
    finally: database.conn.close()


def test_broker_fill_caps_legacy_fifty_percent_at_native_selected_risk():
    value,_ = manager()
    trade = setup(inst_type='OPT',is_recovery=False,entry_price=100.,current_stop=50.,
        market_regime='VOLATILE', quality_verdict={'policy_version':'uts_native.v1',
                                               'checks':{'uts_premium_cap_pct':5.}})
    value._cap_option_entry_stop(trade)
    assert trade.current_stop == trade.initial_stop == 95.


def test_other_scanners_retain_grade_stop_policy():
    assert option_stop_tolerance_pct(15,'VOLATILE',grade='C',confidence=.28) == 5.
    assert option_stop_tolerance_pct(15,'RANGING',grade='B',confidence=.6) == 15.


def test_alignment_evidence_reaches_live_candidate_verdict(monkeypatch, settings):
    import scanner as module
    monkeypatch.setattr(module,'get_settings',lambda: settings)
    scanner = module.Scanner.__new__(module.Scanner)
    scanner.mode = 'REAL'
    scanner._count_uts_aligned_indices = lambda *args,**kw: 2
    scanner._trade_like_market_intelligence_context = lambda *args: {
        'volume':{'available':True,'measurement_basis':'FUTURES_VOLUME','volume_ratio':.8}}
    obj = setup(data_provenance='LIVE')
    verdict = scanner._quality_verdict_for_candidate(obj,regime='RANGING')
    assert verdict.allowed and verdict.policy_version == 'uts_native.v1'
    assert verdict.checks['uts_premium_cap_pct'] == 5.
    assert obj.uts_multi_index_alignment


@pytest.mark.parametrize('failure',['none','absolute_cap','daily_budget','untracked','quantity','operator_cap'])
def test_extension_reserves_full_extra_loss_plus_costs(failure):
    from scanner import Scanner
    from engine.risk_manager import RiskManager
    scanner = Scanner.__new__(Scanner)
    scanner.auto_mode = True
    risk = RiskManager.__new__(RiskManager)
    risk.options_sl_pct, risk.futures_sl_pct, risk.max_trade_loss_abs = 15., .5, 1000.
    risk.capital_total = risk.capital_opt = 100000.
    risk.max_daily_loss_pct = risk.risk_opt_pct = 2.
    risk.circuit_breaker_slippage_bps = 10.
    risk.can_trade = lambda _: True
    scanner.risk_manager = risk
    trade,_,_,_ = extension_case()
    trade.id, trade.lots, trade.lot_size, trade.charges = 'uts-budget',1,65,0.
    scanner.trades = NS(open_trades={trade.id:trade},closed_trades=[])
    if failure == 'absolute_cap': risk.max_trade_loss_abs = 900.
    if failure == 'daily_budget': risk.capital_total = risk.capital_opt = 50000.
    if failure == 'untracked': scanner.trades.open_trades = {}
    if failure == 'quantity': trade.lots = 0
    if failure == 'operator_cap': risk.options_sl_pct = 10.
    assert scanner._uts_stop_extension_budget_ok(trade,85.) is (failure == 'none')
    assert trade.current_stop == 90.


@pytest.fixture(autouse=True)
def _without_uts_5m_intel_gate(monkeypatch):
    """These 5m fixtures predate the 6 Oct 5m intel gate and carry no measured volume.

    The gate has its own tests in test_uts_5m_intel_gate_2026_10_06.py.
    """
    from engine import uts_lane
    monkeypatch.setattr(uts_lane, "five_minute_intel_failures", lambda *a, **k: [])
