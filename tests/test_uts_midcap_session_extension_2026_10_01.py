"""Session rows mirror the approved stop on their exact live option contract."""
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from config.settings import Settings
from scanner import Scanner
from tests.test_s1_lane_exits_2026_09_24 import _row


@pytest.mark.parametrize('instrument,normal_stop,extended_stop,premium', [
    ('NIFTY',90.,85.,88.), ('BANKNIFTY',90.,85.,88.),
    ('SENSEX',90.,85.,88.), ('MIDCPNIFTY',93.,90.,92.),
    ('MIDCAPNIFTY',93.,90.,92.),
])
def test_acknowledged_extension_reaches_matching_session_row(
        monkeypatch,instrument,normal_stop,extended_stop,premium):
    settings = Settings(_env_file=None)
    monkeypatch.setattr('scanner.get_settings', lambda:settings)
    monkeypatch.setattr('config.settings.get_settings', lambda:settings)
    source_time = datetime(2026,9,30,11)
    normal = 100.-normal_stop
    verdict = {'policy_version':'uts_native.v1','checks':{
        'uts_premium_cap_pct':normal,
        'uts_premium_cap_profile':'midcap.v1' if normal==7. else 'standard.v1'}}
    row = _row(instrument=instrument,direction='LONG',price=100.,stop=normal_stop,
        initial_stop=normal_stop,current_price=premium,signal_timestamp=source_time,
        source_lane='UTS',source_engine='ut_smart',quality_verdict=verdict,
        entered_intracandle=False,symbol_token='same-contract')
    live = NS(instrument=instrument,source_lane='UTS',timeframe='5min',direction='LONG',
        signal_time=source_time,symbol_token='same-contract',is_ghost=False,
        pending_exit=False,current_stop=extended_stop,quality_verdict={
            'policy_version':'uts_native.v1','checks':{
                **verdict['checks'],'uts_stop_extended':True,
                'uts_extended_stop':extended_stop}})
    scanner = Scanner.__new__(Scanner)
    scanner.mode, scanner.auto_mode = 'REAL', True
    scanner.trades = NS(open_trades={'live':live})
    scanner._loaded_session_is_current_day = lambda:True
    scanner._active_session_entry_candidates = lambda _: [row]
    scanner.candles = NS(get_latest_price=lambda _:23000.)
    scanner._ensure_trade_like_initial_stop = lambda _:None
    scanner._get_trade_regime = lambda *args,**kwargs:'RANGING'
    scanner._candidate_net_pnl = lambda candidate,price:(price-candidate.price)*65
    scanner._trade_like_initial_risk_rs = lambda _:normal*65
    scanner.mtf = NS(_result_cache={f'{instrument}_5min':{'signals':[]}})
    scanner._diag_inc = lambda *args:None
    scanner._inherit_exit_entry_metadata = lambda *args:None
    scanner.log_event = lambda *args,**kwargs:None
    scanner._notify = lambda *args,**kwargs:None
    scanner._maybe_arm_reversion_from_exit = lambda *args,**kwargs:None
    exits = []
    scanner._remember_trade_candidates = lambda items:exits.extend(items)
    scanner._latest_exit_candidates = {}
    scanner._persist_session_trade_candidates = lambda:None
    scanner._fifteen_min_one_min_support_exit_decision = lambda *args,**kwargs:pytest.fail('generic exit reached')
    assert scanner._uts_session_live_match(row) is live
    scanner._check_session_candidates_breaches_locked(instrument)
    assert not exits
    assert row.stop == extended_stop
    assert row.initial_stop == normal_stop
    assert row.quality_verdict['checks']['uts_stop_extended']
    assert live.current_stop == extended_stop
