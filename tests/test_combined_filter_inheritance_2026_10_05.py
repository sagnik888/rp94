"""Real candidate-builder proof that combined ownership preserves native admission gates."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from engine.alt_scanners import AltSignalEngine
from engine.multi_timeframe import MultiTimeframeEngine
from engine.scanner_modes import COMBINED_MODES, scope_suffix
from tests.test_s1_engine_2026_09_23 import _frame
from tests.test_ut_smart_live_path_2026_09_26 import live
from trading.trade_manager import IST


OWNERS = [
    ('quad_prime_s1', 'quad_prime'), ('quad_prime_s1', 's1'),
    ('ut_smart_s1', 'ut_smart'), ('ut_smart_s1', 's1'),
]


def _run(live, monkeypatch, mode, native, combined, gate):
    import engine.signal_processor as processor_module
    import scanner as scanner_module

    h = live(tf='5min', settings_overrides={'s1_min_score': 60})
    settings = processor_module.get_settings()
    settings.scanner_engine = native
    monkeypatch.setattr('config.settings._settings', settings)
    if native == 'ut_smart':
        signal, result = h.sig, h.res
        data = h.sc.candles.get_candles('NIFTY', '5min')
    else:
        candles = _frame()
        engine = AltSignalEngine(signal_source=native)
        history = engine.process(candles, instrument='NIFTY', timeframe='5min')
        signals = [s for s in history['signals'] if s.timestamp.time() < datetime.strptime('12:30', '%H:%M').time()]
        assert signals, 'The deterministic native engine must produce a usable signal'
        signal = signals[-1]
        data = candles.iloc[:candles.index.get_loc(pd.Timestamp(signal.timestamp)) + 2]
        result = engine.process(data, instrument='NIFTY', timeframe='5min')
        assert result['signals'][-1].timestamp == signal.timestamp
        signal = result['signals'][-1]

    now = signal.timestamp.replace(tzinfo=None) + timedelta(minutes=5, seconds=20)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(now) if tz else now

    for module in (processor_module, scanner_module):
        monkeypatch.setattr(module, 'datetime', Clock)
    spot = float(data.close.iloc[-1])
    h.sc.candles = NS(get_candles=lambda i, tf: data if tf == '5min' else None,
                      get_latest_price=lambda i: spot, get_max_timestamp=lambda: now)
    settings.scanner_engine = mode if combined else native
    suffix = scope_suffix(NS(source_engine=native), settings)
    h.sc._last_signal_time = {f'NIFTY_5min{suffix}': signal.timestamp - timedelta(seconds=1)}
    weights = NS(tf_weights={'1min': .15, '5min': .4, '15min': .45})
    score, direction = MultiTimeframeEngine._compute_confluence(weights, {'5min': result}, 'NIFTY')
    view = NS(instrument='NIFTY', results_1min=None, results_5min=result, results_15min=None,
              confluence_score=score, confluence_signal=direction, active_signals=result['signals'])
    if combined:
        other = 's1' if native != 's1' else COMBINED_MODES[mode]
        view.results_5min = {'native_results': {native: result, other: {'signals': [], 'state': None}}}

    if gate == 'daily_loss':
        h.sc.risk_manager.can_trade = lambda segment: segment != 'ANY'
    elif gate == 'segment_loss':
        h.sc.risk_manager.can_trade = lambda segment: segment == 'ANY'
    elif gate == 'capacity':
        h.sc.trades.can_open_trade = lambda: (False, 'test shared capacity')
    elif gate == 'direction':
        settings.strategy_direction_mode = 'SHORT_ONLY' if signal.signal_type == 'BUY' else 'LONG_ONLY'
    elif gate == 'timeframe':
        settings.ut_timeframe_entry_policy = 'PRIMARY_15'
    elif gate == 'missing_own_intelligence':
        settings.own_timeframe_intel_fail_closed = True
        h.processor._build_timeframe_contexts = lambda *a: {}
    args = list(h.args)
    args[1], args[8], args[9] = view, spot, round(spot / 50) * 50
    before = settings.model_dump()
    out = h.processor.process_best_signal(*args)
    assert settings.model_dump() == before
    return out, h


@pytest.mark.parametrize('mode,native', OWNERS)
@pytest.mark.parametrize('gate,expected', [
    ('normal', ['TRADE SIGNAL']),
    ('daily_loss', []),
    ('segment_loss', ['REJECTED - OPT Risk Halt']),
    ('capacity', ['REJECTED - Max Trades']),
    ('direction', []),
    ('timeframe', []),
    ('missing_own_intelligence', []),
])
def test_combined_candidate_builder_preserves_native_shared_gates(live, monkeypatch, mode, native, gate, expected):
    standalone, _ = _run(live, monkeypatch, mode, native, False, gate)
    combined, scanner = _run(live, monkeypatch, mode, native, True, gate)
    assert [c.status for c in standalone] == expected
    assert [c.status for c in combined] == expected
    if gate == 'normal':
        for c, reference in zip(combined, standalone):
            for field in ('direction', 'inst_type', 'price', 'stop', 'target', 'spot_stop',
                          'entry_spot', 'rr', 'grade', 'confidence', 'source_engine'):
                assert getattr(c, field) == getattr(reference, field), field
    if gate == 'missing_own_intelligence':
        assert any('own-timeframe intelligence' in text for text in scanner.logs)


@pytest.mark.parametrize('mode', ['ut_smart', 'ut_smart_s1'])
def test_combined_uts_keeps_native_one_minute_burst_requirement(live, monkeypatch, mode):
    """The TF toggle does not weaken the separate UTS native 1m quality rule."""
    import engine.signal_processor as processor_module

    h = live(tf='1min', settings_overrides={'ut_timeframe_entry_policy': 'PRIMARY_1'})
    settings = processor_module.get_settings()
    settings.scanner_engine = mode
    monkeypatch.setattr('config.settings._settings', settings)
    if mode in COMBINED_MODES:
        h.args[1].instrument = 'NIFTY'
        h.args[1].active_signals = h.res['signals']
        h.args[1].results_1min = {'native_results': {
            'ut_smart': h.res, 's1': {'signals': [], 'state': None}}}
        h.sc._last_signal_time = {
            'NIFTY_1min_ut_smart': h.sig.timestamp - timedelta(seconds=1)}
    out = h.processor.process_best_signal(*h.args)
    assert len(out) == 1 and out[0].status == 'TRADE SIGNAL'
    candidate = out[0]
    assert candidate.one_min_raw_entry and not candidate.volume_spike_entry
    assert not h.sc._passes_live_trade_ready_gate(candidate)
    assert candidate.quality_verdict['reason_code'] == 'UTS_1MIN_SPECIAL_REQUIRED'
    assert candidate.quality_verdict['policy_version'] == 'uts_native.v1'
