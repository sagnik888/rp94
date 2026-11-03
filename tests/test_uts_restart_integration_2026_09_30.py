"""Failures observed while auditing the restarted UTS integration."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from scanner import Scanner
from engine import uts_lane
import pytest


def test_vix_refresh_during_percentile_read_uses_stable_prior_sessions(monkeypatch):
    import scanner as module

    scanner = Scanner.__new__(Scanner)
    start = datetime(2026, 1, 1, 15, 30)
    scanner._vix_data = {
        (start + timedelta(days=i)).strftime('%Y-%m-%d %H:%M:%S'): 10.0 + i
        for i in range(30)
    }

    class Clock(datetime):
        refreshed = False

        @classmethod
        def strptime(cls, value, fmt):
            if not cls.refreshed:
                cls.refreshed = True
                # Same mutation performed by live VIX refresh in another worker.
                scanner._vix_data['2026-02-15 10:00:00'] = 99.0
            return datetime.strptime(value, fmt)

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'get_settings', lambda: SimpleNamespace(vix_percentile_min_sessions=20))
    assert scanner._vix_percentile(25., datetime(2026, 2, 15, 10)) == 53.3
    assert scanner._vix_data['2026-02-15 10:00:00'] == 99.0


def test_uts_profit_lock_remembers_peak_and_touch_between_monitor_passes(monkeypatch):
    from tests.test_s1_lane_2026_09_23 import _monitor, _open_trade

    trade = _open_trade(source_lane='UTS', source_engine='ut_smart')
    start = datetime(2026, 9, 24, 10, 29)
    closed,updated,_ = _monitor(monkeypatch,trade,23000.,118.,observed_ticks=[
        (120.,start), (113.,start+timedelta(seconds=1)), (118.,start+timedelta(seconds=2))])
    assert closed == ['UTS_PROFIT_LOCK']
    assert not updated
    assert trade.s1_peak_premium == 120.


def test_uts_hidden_peak_raises_stop_even_when_latest_mark_is_lower():
    trade = SimpleNamespace(inst_type='OPT',entry_price=100.,initial_stop=90.,current_stop=90.)
    start = datetime(2026,9,30,10)
    decision = uts_lane.exit_decision(trade,23000.,118.,now=start+timedelta(seconds=2),
        observed_contract_ticks=[(120.,start),(118.,start+timedelta(seconds=1))])
    assert not decision.exit and decision.premium_stop == pytest.approx(114.)


def test_uts_tick_order_cannot_use_peak_that_happens_after_original_stop_touch():
    trade = SimpleNamespace(inst_type='OPT',entry_price=100.,initial_stop=90.,current_stop=90.)
    start = datetime(2026,9,30,10)
    decision = uts_lane.exit_decision(trade,23000.,120.,now=start+timedelta(seconds=2),
        observed_contract_ticks=[(89.,start),(120.,start+timedelta(seconds=1))])
    assert decision.exit and decision.reason == 'TRAILING_STOP'
    assert getattr(trade,'s1_peak_premium',100.) == 100.


@pytest.mark.parametrize('mismatch',['none','source_time','contract','ghost','pending_exit','unknown_time','missing_instrument',
                                      'missing_candidate_token','missing_trade_token'])
def test_session_widening_receipt_matches_exact_live_signal_and_contract(mismatch):
    scanner = Scanner.__new__(Scanner)
    clock = datetime(2026,9,30,10)
    candidate = SimpleNamespace(instrument='NIFTY',timeframe='5min',direction='LONG',
        signal_timestamp=clock,symbol_token='contract-1')
    trade = SimpleNamespace(instrument='NIFTY 23000 CE',timeframe='5min',direction='LONG',
        source_lane='UTS',signal_time=clock,symbol_token='contract-1',is_ghost=False,pending_exit=False)
    if mismatch == 'source_time': trade.signal_time += timedelta(minutes=5)
    if mismatch == 'contract': trade.symbol_token = 'contract-2'
    if mismatch == 'ghost': trade.is_ghost = True
    if mismatch == 'pending_exit': trade.pending_exit = True
    if mismatch == 'unknown_time': candidate.signal_timestamp = None
    if mismatch == 'missing_instrument': candidate.instrument = ''
    if mismatch == 'missing_candidate_token': candidate.symbol_token = ''
    if mismatch == 'missing_trade_token': trade.symbol_token = ''
    scanner.trades = SimpleNamespace(open_trades={'trade':trade})
    assert scanner._uts_session_live_match(candidate) is (trade if mismatch == 'none' else None)


def test_native_setup_carries_to_candidate_without_becoming_grading_contract():
    from engine.signal_processor import GRADING_TELEMETRY_ATTRS, carry_grading_telemetry
    signal = SimpleNamespace(uts_setup={'adx_value':28.,'rsi_value':56.},uts_multi_index_alignment=True)
    candidate = SimpleNamespace()
    carry_grading_telemetry(candidate,signal)
    assert candidate.uts_setup == signal.uts_setup
    assert candidate.uts_multi_index_alignment is True
    assert 'uts_setup' not in GRADING_TELEMETRY_ATTRS
    assert 'uts_multi_index_alignment' not in GRADING_TELEMETRY_ATTRS
