"""UTS historical/recovery/EOD quality uses the same native policy as live."""
from types import SimpleNamespace as NS
from datetime import datetime, timedelta

import pytest

from engine.signal_processor import SignalProcessor
from engine.quality_verdict import QualityVerdict


def signal(**changes):
    values = dict(source_engine='ut_smart', source_lane='UTS', timeframe='5min',
                  signal_type='BUY', adx_value=20., uts_setup={'rsi_value':56.},
                  volume_ratio=.8, volume_available=True, volume_basis='FUTURES_VOLUME')
    values.update(changes)
    return NS(**values)


def evaluate(obj, **changes):
    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = NS(mode='HISTORICAL')
    values = dict(tf='5min', grade='C', confidence=.1, regime='RANGING',
                  inst_type='OPT', settings=NS(uts_lane_enabled=True, options_sl_pct=15.),
                  trend_15m_agrees=False)
    values.update(changes)
    return processor._quality_verdict_for_signal(obj, **values)


def test_replay_recovery_eod_native_uts_does_not_reject_display_grade():
    obj = signal()
    verdict = evaluate(obj)
    assert verdict.allowed
    assert verdict.policy_version == 'uts_native.v1'
    assert verdict.checks['grade_filter_enabled'] is False
    assert obj.quality_verdict == verdict.to_dict()
    assert verdict.checks['uts_premium_cap_pct'] == 10.


@pytest.mark.parametrize('basis,available,rejected', [
    ('FUTURES_VOLUME',True,True), ('INDEX_TICK_ACTIVITY',True,False),
    ('FUTURES_VOLUME',False,False), ('',True,False),
])
def test_native_helper_keeps_joint_weakness_and_volume_provenance(basis, available, rejected):
    obj = signal(adx_value=10., uts_setup={'rsi_value':49.}, volume_ratio=.4,
                 volume_basis=basis, volume_available=available)
    verdict = evaluate(obj)
    assert verdict.allowed is not rejected
    assert verdict.checks['adx'] == 10.
    assert verdict.checks['rsi'] == 49.
    assert verdict.checks['volume_ratio'] == .4


@pytest.mark.parametrize('tf,special,allowed,cap', [
    ('1min',False,False,5.), ('1min',True,True,5.),
    ('15min',False,True,15.), ('5min',False,True,10.),
])
def test_native_helper_uses_call_site_timeframe(tf, special, allowed, cap):
    obj = signal(timeframe='', rare_burst_entry=special)
    verdict = evaluate(obj, tf=tf)
    assert verdict.allowed is allowed
    assert verdict.checks['uts_premium_cap_pct'] == cap


def test_native_stop_facts_remain_frozen_after_queue_or_recovery():
    obj = signal(adx_value=35.)
    first = evaluate(obj)
    second = evaluate(obj, regime='TRENDING_UP')
    assert second.checks['uts_premium_cap_pct'] == first.checks['uts_premium_cap_pct'] == 10.


def test_other_scanners_keep_existing_quality_helper(monkeypatch):
    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = NS(mode='HISTORICAL')
    expected = QualityVerdict(False,'normal','C_GRADE','existing rule')
    calls = []
    processor._passes_quality_gate = lambda *args, **kw: calls.append((args,kw)) or expected
    obj = signal(source_lane='S1', source_engine='stock_master')
    verdict = processor._quality_verdict_for_signal(obj, tf='5min', grade='C',
        confidence=.1, regime='RANGING', inst_type='OPT',
        settings=NS(uts_lane_enabled=True), trend_15m_agrees=False)
    assert verdict is expected
    assert calls[0][0][1:3] == ('C',.1)
    assert calls[0][1]['return_verdict'] is True


def alignment_case(tf='5min'):
    from scanner import Scanner
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ['NIFTY','BANKNIFTY']
    now = datetime(2026,9,30,11,15,10)
    minutes = {'1min':1,'5min':5,'15min':15}[tf]
    bar = now - timedelta(minutes=minutes,seconds=10)
    result = dict(instrument='BANKNIFTY', timeframe=tf, source_engine='ut_smart',
                  uts_evaluation={'observed_at':now,'last_closed_bar':bar},
                  state={'position':1,'uts_state_bar_time':bar})
    scanner._cached_results = {f'BANKNIFTY_{tf}':result}
    return scanner, result, now, bar


@pytest.mark.parametrize('tf',['1min','5min','15min'])
def test_alignment_uses_current_closed_state_even_when_original_flip_is_old(tf):
    scanner, result, now, bar = alignment_case(tf)
    result['signals'] = [signal(instrument='BANKNIFTY',timeframe=tf,
        timestamp=bar-timedelta(hours=1),uts_forming=False)]
    assert scanner._count_uts_aligned_indices('LONG',tf,as_of=now,include_self='NIFTY') == 2
    result['state']['position'] = -1
    assert scanner._count_uts_aligned_indices('LONG',tf,as_of=now,include_self='NIFTY') == 1


@pytest.mark.parametrize('change',['error','stale','future_observation','missing_observation',
    'old_bar','forming_bar','wrong_tf','wrong_index','wrong_engine','state_bar_mismatch',
    'flat','unknown_position','boolean_position'])
def test_alignment_refuses_unproven_peer_direction(change):
    scanner, result, now, bar = alignment_case()
    if change == 'error': result['error'] = 'data unavailable'
    if change == 'stale': result['uts_evaluation']['observed_at'] -= timedelta(seconds=31)
    if change == 'future_observation': result['uts_evaluation']['observed_at'] += timedelta(seconds=1)
    if change == 'missing_observation': del result['uts_evaluation']['observed_at']
    if change == 'old_bar': result['uts_evaluation']['last_closed_bar'] -= timedelta(minutes=1,seconds=31)
    if change == 'forming_bar': result['uts_evaluation']['last_closed_bar'] += timedelta(minutes=5)
    if change == 'wrong_tf': result['timeframe'] = '15min'
    if change == 'wrong_index': result['instrument'] = 'SENSEX'
    if change == 'wrong_engine': result['source_engine'] = 'utbot'
    if change == 'state_bar_mismatch': result['state']['uts_state_bar_time'] -= timedelta(minutes=5)
    if change == 'flat': result['state']['position'] = 0
    if change == 'unknown_position': result['state']['position'] = None
    if change == 'boolean_position': result['state']['position'] = True
    assert scanner._count_uts_aligned_indices('LONG','5min',as_of=now,include_self='NIFTY') == 1


def test_alignment_fallback_uses_newest_confirmed_flip_not_older_matching_flip():
    scanner, result, now, bar = alignment_case()
    result['state'] = {}
    result['signals'] = [signal(instrument='BANKNIFTY',timestamp=bar-timedelta(minutes=5),uts_forming=False),
        signal(instrument='BANKNIFTY',timestamp=bar,signal_type='SELL',uts_forming=False)]
    assert scanner._count_uts_aligned_indices('LONG','5min',as_of=now,include_self='NIFTY') == 1
    result['signals'][-1].signal_type = 'BUY'
    assert scanner._count_uts_aligned_indices('LONG','5min',as_of=now,include_self='NIFTY') == 2


@pytest.mark.parametrize('changes',[{'timestamp':None},{'timestamp':datetime(2026,9,30,12)},
    {'uts_forming':True},{'uts_forming':None},{'timeframe':'15min'}, {'instrument':'SENSEX'}])
def test_alignment_fallback_never_counts_missing_future_or_forming_signal(changes):
    scanner, result, now, bar = alignment_case()
    result['state'] = {}
    obj = signal(instrument='BANKNIFTY',timestamp=bar,uts_forming=False)
    for key,value in changes.items(): setattr(obj,key,value)
    result['signals'] = [obj]
    assert scanner._count_uts_aligned_indices('LONG','5min',as_of=now,include_self='NIFTY') == 1


def test_opposite_current_peer_keeps_ordinary_ten_percent_cap(monkeypatch):
    import scanner as module
    scanner, result, now, bar = alignment_case()
    # Use the real clock so admission and the helper share the same evaluation.
    now = datetime.now(module.IST).replace(tzinfo=None)
    bar = now - timedelta(minutes=5,seconds=10)
    result['uts_evaluation'] = {'observed_at':now,'last_closed_bar':bar}
    result['state'] = {'position':-1,'uts_state_bar_time':bar}
    scanner.mode = 'REAL'
    scanner._candidate_market_intelligence_context = lambda obj: {
        'volume': {'available':True,'measurement_basis':'FUTURES_VOLUME','volume_ratio':.8}}
    settings = NS(uts_lane_enabled=True,options_sl_pct=15.)
    monkeypatch.setattr(module,'get_settings',lambda:settings)
    obj = signal(direction='LONG',instrument='NIFTY',data_provenance='LIVE',action='ENTRY')
    verdict = scanner._quality_verdict_for_candidate(obj,regime='RANGING')
    assert verdict.allowed
    assert verdict.checks['uts_premium_cap_pct'] == 10.
    assert not obj.uts_multi_index_alignment
    result['state']['position'] = 1
    obj.quality_verdict = {}
    verdict = scanner._quality_verdict_for_candidate(obj,regime='RANGING')
    assert verdict.checks['uts_premium_cap_pct'] == 5.


@pytest.fixture(autouse=True)
def _without_uts_5m_intel_gate(monkeypatch):
    """These 5m fixtures predate the 6 Oct 5m intel gate and carry no measured volume.

    The gate has its own tests in test_uts_5m_intel_gate_2026_10_06.py.
    """
    from engine import uts_lane
    monkeypatch.setattr(uts_lane, "five_minute_intel_failures", lambda *a, **k: [])
