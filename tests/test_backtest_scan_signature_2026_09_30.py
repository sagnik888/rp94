"""Backtest rebuilds must reach analysis after candle preparation finishes."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import Settings
from scanner import Scanner


def host(monkeypatch, mode='HISTORICAL', fire_mode='CANDLE_CLOSE'):
    settings = Settings(_env_file=None, scanner_engine='ut_smart')
    settings.set_signal_fire_mode(fire_mode)
    settings.event_driven_scan_enabled = True
    monkeypatch.setattr('scanner.get_settings',lambda: settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = scanner.runtime_mode = mode
    scanner.simulation_id = 101
    scanner.active_indices = ['NIFTY','MIDCPNIFTY']
    scanner._analysis_event_generation = 3
    scanner._analysis_recompute_reason = ''
    scanner._initial_setup_completed = True
    scanner._pending_live_signals = {}
    scanner._intel_cache_time = {'NIFTY_5min':1.}
    scanner._last_chain_fetch = {'MIDCPNIFTY':2.}
    scanner._intel_bar_start = lambda *a: datetime(2026,9,30,10)
    receipt = [datetime(2026,9,30,10)]
    scanner.candles = SimpleNamespace(latest_quote_receipt=lambda *a: receipt[0])
    return scanner, receipt


@pytest.mark.parametrize('mode',['HISTORICAL','REAL','PAPER'])
def test_real_scanner_signature_includes_generation_bars_and_features(monkeypatch, mode):
    scanner, _ = host(monkeypatch,mode)
    signature = scanner._analysis_input_signature()
    assert isinstance(signature,tuple) and len(signature)==6
    assert signature[:3] == (mode,101,3)
    assert len(signature[3])==6 and signature[4]==2.
    assert signature[5]==()


def test_recalculated_backtest_wakes_analysis_without_new_market_ticks(monkeypatch):
    scanner, _ = host(monkeypatch)
    scanner._last_analysis_signature = scanner._analysis_input_signature()
    scanner._historical_rebuild_pending = True
    scanner._recalculation_status = {'status':'awaiting_scan'}
    assert scanner._analysis_recompute_due(scanner._last_analysis_signature,False)[0] is False
    scanner.simulation_id += 1  # Applied history advances the simulation generation.
    assert not scanner._historical_recalculation_blocks_scan()
    assert scanner._analysis_recompute_due(scanner._analysis_input_signature(),False)[0] is True


def test_explicit_recompute_event_changes_real_signature(monkeypatch):
    scanner, _ = host(monkeypatch,'REAL')
    scanner._last_analysis_signature = scanner._analysis_input_signature()
    scanner.request_analysis_recompute('settings-refresh')
    assert scanner._analysis_recompute_due(scanner._analysis_input_signature(),False)==(True,'settings-refresh')


def test_intracandle_quote_generation_remains_in_real_signature(monkeypatch):
    scanner, receipt = host(monkeypatch,'REAL','INTRACANDLE')
    first = scanner._analysis_input_signature()
    assert first and len(first[5])==2
    receipt[0] += timedelta(seconds=10)
    updated = scanner._analysis_input_signature()
    assert first[:-1]==updated[:-1] and first[-1]!=updated[-1]
