"""UTS admission, filled stops and ordered exit events across market regimes."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from engine import uts_lane
from engine.signal_processor import SignalProcessor
from trading.trade_manager import Trade, TradeManager
from tests.test_s1_lane_2026_09_23 import _monitor


@pytest.fixture
def native_settings(monkeypatch):
    settings = NS(uts_lane_enabled=True, options_sl_pct=15.,
                  uts_lane_lock_arm_r=1., uts_lane_lock_giveback_pct=30.)
    monkeypatch.setattr('config.settings.get_settings', lambda: settings)
    return settings


REGIMES = [
    ('ordinary', 'UNKNOWN', 20., None),
    ('normal_trend', 'TRENDING', 20., None),
    ('strong_trend', 'TRENDING', 40., None),
    ('breakout', 'BREAKOUT', 40., None),
    ('choppy', 'CHOPPY', 20., None),
    ('ranging', 'RANGING', 20., None),
    ('volatile', 'VOLATILE', 20., None),
    ('cascade', 'VOLATILE', 20., 'one_way_cascade_entry'),
    ('alignment', 'RANGING', 20., 'uts_multi_index_alignment'),
    ('spike', 'VOLATILE', 20., 'volume_spike_entry'),
]


@pytest.mark.parametrize('instrument,lot', [('NIFTY',65), ('BANKNIFTY',30),
                                          ('SENSEX',20), ('MIDCPNIFTY',120)])
@pytest.mark.parametrize('timeframe', ['1min','5min','15min'])
@pytest.mark.parametrize('direction', ['LONG','SHORT'])
@pytest.mark.parametrize('name,regime,adx,special', REGIMES)
def test_native_entry_to_filled_stop_and_ordered_profit_exit(
        monkeypatch, native_settings, instrument, lot, timeframe, direction,
        name, regime, adx, special):
    if regime in {'TRENDING','BREAKOUT'}:
        regime += '_UP' if direction == 'LONG' else '_DOWN'
    signal = NS(instrument=instrument, timeframe=timeframe, direction=direction,
        source_lane='UTS', source_engine='ut_smart', grade='C', confidence=.01,
        action='ENTRY', adx_value=adx, rsi_value=60. if direction=='LONG' else 40.,
        volume_ratio=.8, volume_available=True, volume_basis='FUTURES_VOLUME',
        rare_burst_entry=timeframe=='1min')
    if special:
        setattr(signal, special, True)
    verdict = uts_lane.entry_verdict(signal, native_settings, regime=regime)
    assert verdict.allowed  # Display grade never vetoes valid native setups.
    tight, normal, wide = (3.5,7.,10.) if instrument=='MIDCPNIFTY' else (5.,10.,15.)
    expected = (tight if timeframe=='1min' or name in {'strong_trend','breakout','cascade','alignment'}
                else wide if timeframe=='15min' and name in {'choppy','ranging','volatile','spike'}
                else normal)
    manager = TradeManager.__new__(TradeManager)
    manager.options_sl_pct = 15.
    trade = Trade(id='T1', instrument=instrument, timeframe=timeframe, direction=direction,
        inst_type='OPT', source_lane='UTS', entry_price=100., current_stop=50.,
        initial_stop=50., trailing_stop=50., lots=1, lot_size=lot,
        entry_time=datetime(2026,9,24,10), grade='C', market_regime=regime,
        quality_verdict=verdict.to_dict())
    manager._cap_option_entry_stop(trade)
    assert trade.current_stop == pytest.approx(100.-expected)
    assert trade.initial_stop == trade.premium_stop == trade.current_stop
    # A small favourable move cannot erase the initial premium cap. A later
    # bounce cannot erase an earlier observed stop touch.
    clock = datetime(2026,9,24,10,29)
    ticks = [(102.,clock), (100.-expected-.5,clock+timedelta(seconds=1)),
             (130.,clock+timedelta(seconds=2))]
    closed, updated, other = _monitor(monkeypatch, trade, 23000., 130., observed_ticks=ticks)
    assert closed == ['TRAILING_STOP'] and not updated and not other
    assert trade.s1_peak_premium == 102.
    # Independent run: a peak, pullback through the 70% retained-gain floor,
    # and bounce occur between monitor passes. Both CE and PE must exit.
    trade.s1_peak_premium = 100.
    peak = 100. + 2.*expected
    floor = 100. + 1.4*expected
    ticks = [(peak,clock), (floor-.5,clock+timedelta(seconds=1)),
             (peak+1.,clock+timedelta(seconds=2))]
    closed, updated, other = _monitor(monkeypatch, trade, 23000., peak+1., observed_ticks=ticks)
    assert closed == ['UTS_PROFIT_LOCK'] and not updated and not other
    assert trade.s1_peak_premium == peak


@pytest.mark.parametrize('ratio', [float('nan'),float('inf'),-1.,'missing',None])
def test_unmeasured_volume_cannot_prove_extreme_weakness_in_replay(native_settings, ratio):
    signal = NS(instrument='NIFTY',timeframe='5min',direction='LONG',source_lane='UTS',
        source_engine='ut_smart',adx_value=10.,rsi_value=49.,volume_ratio=ratio,
        volume_available=True,volume_basis='FUTURES_VOLUME')
    processor = SignalProcessor.__new__(SignalProcessor)
    processor.scanner = NS(mode='HISTORICAL')
    verdict = processor._quality_verdict_for_signal(signal,tf='5min',grade='C',
        confidence=.1,regime='RANGING',inst_type='OPT',settings=native_settings,
        trend_15m_agrees=False)
    assert verdict.allowed
    assert not verdict.checks['measured_volume']


def test_measured_zero_volume_still_proves_extreme_weakness(native_settings):
    signal = NS(instrument='NIFTY',timeframe='5min',direction='LONG',source_lane='UTS',
        adx_value=10.,rsi_value=49.,volume_ratio=0.,volume_available=True,
        volume_basis='FUTURES_VOLUME')
    verdict = uts_lane.entry_verdict(signal, native_settings)
    assert not verdict.allowed and verdict.checks['measured_volume']


def test_live_missing_ratio_cannot_be_invented_as_measured_zero(monkeypatch, native_settings):
    from scanner import Scanner
    monkeypatch.setattr('scanner.get_settings',lambda:native_settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = 'REAL'
    scanner._count_uts_aligned_indices = lambda *args,**kwargs: 1
    scanner._candidate_market_intelligence_context = lambda candidate: {
        'volume': {'available': True, 'measurement_basis': 'FUTURES_VOLUME'}}
    candidate = NS(instrument='NIFTY',timeframe='5min',direction='LONG',source_lane='UTS',
        source_engine='ut_smart',adx_value=10.,rsi_value=49.,volume_ratio=.2,
        data_provenance='LIVE',action='ENTRY')
    verdict = scanner._quality_verdict_for_candidate(candidate,regime='RANGING')
    assert verdict.allowed
    assert not verdict.checks['measured_volume']


@pytest.fixture(autouse=True)
def _without_uts_5m_intel_gate(monkeypatch):
    """These 5m fixtures predate the 6 Oct 5m intel gate and carry no measured volume.

    The gate has its own tests in test_uts_5m_intel_gate_2026_10_06.py.
    """
    from engine import uts_lane
    monkeypatch.setattr(uts_lane, "five_minute_intel_failures", lambda *a, **k: [])
