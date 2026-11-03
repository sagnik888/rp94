"""The final confirmed UTS arrow receives the same replay admission and pricing."""
from datetime import datetime, timedelta
import copy
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import config.settings as settings_module
import engine.signal_processor as module
from config.settings import Settings
from engine.signal_processor import SignalProcessor


@pytest.fixture
def final_signal(monkeypatch):
    def build(*, instrument='NIFTY', tf='5min', regime='UNKNOWN', grade='C',
              special=False, at=datetime(2026, 9, 29, 10), signal_type='BUY',
              weak=False, missing=False, hole=False, engine='ut_smart',
              preference='OPT', direction_mode='LONG_SHORT', chart_exit=False):
        settings = Settings()
        for key, value in dict(scanner_engine=engine, ut_timeframe_entry_policy={
            '1min':'PRIMARY_1', '5min':'PRIMARY_5', '15min':'PRIMARY_15'}[tf],
            one_min_scalping_enabled=True, own_timeframe_intel_fail_closed=True,
            options_sl_pct=15., ut_force_exit_time='15:25', ut_session_end='15:30',
            strategy_direction_mode=direction_mode, telegram_enabled=False,
            # Pin fixed lots and leave the 2026-10-08 MIDCPNIFTY peer rule out of these
            # historical-signal tests (both have their own tests).
            risk_based_lots_enabled=False, ctx_peer_alignment_mode='off').items():
            object.__setattr__(settings, key, value)
        monkeypatch.setattr(settings_module, 'get_settings', lambda: settings)
        monkeypatch.setattr(module, 'get_settings', lambda: settings)
        signal = NS(instrument=instrument, timeframe=tf, timestamp=at,
            signal_type=signal_type, source_engine=engine,
            source_lane='UTS' if engine == 'ut_smart' else '', price=23000.,
            trailing_stop=22850. if signal_type == 'BUY' else 23150.,
            stop_distance=150., atr_value=40., adx_value=10. if weak else 20.,
            uts_setup={'rsi_value':49. if weak else 56.},
            # This fixture represents a measured ratio that passes the 5m UTS
            # minimum. The weak-setup cases deliberately remain below it.
            volume_ratio=.4 if weak else 1.1, volume_available=True,
            volume_basis='FUTURES_VOLUME', rare_burst_entry=special,
            uts_forming=False, uts_exit_time=at+timedelta(minutes=20) if chart_exit else None,
            uts_exit_reason='SMART_EXIT' if chart_exit else '')
        index = pd.date_range('2026-09-29 09:15', '2026-09-29 15:24', freq='1min')
        candles = pd.DataFrame(dict(open=23000., high=23100., low=22900.,
                                    close=23005., volume=1000.), index=index)
        candle_frames = {"1min": candles}
        for source_tf in ("5min", "15min"):
            source_frame = candles.resample(
                source_tf, origin="start_day", offset="15min", label="left", closed="left"
            ).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
            # The compact fixture has no archived source-timeframe volume. The
            # signal's measured FUTURES_VOLUME ratio is the fallback; returning the
            # 1m frame for every requested timeframe manufactured a false 1.0x ratio.
            candle_frames[source_tf] = source_frame
        options = pd.DataFrame(dict(open=100., high=104., low=99., close=101.), index=index)
        if missing:
            options = pd.DataFrame()
        elif hole:
            options = options.drop(at+timedelta(minutes=20))
        loads = []
        product_calls = []
        sc = NS(mode='HISTORICAL', inst_pref=preference, backtest_days=1,
            options_sl_pct=15., futures_sl_pct=.15, _last_signal_time={}, _vix_data={},
            _session_trade_candidates={},
            risk_manager=NS(can_trade=lambda *args: True),
            candles=NS(get_candles=lambda _instrument, timeframe: candle_frames.get(timeframe, candles)),
            trades=NS(closed_trades=[], open_trades={}),
            signals=NS(_grade_signal=lambda *args, **kwargs: (grade, .1, [])),
            data=NS(get_ltp=lambda *args: 15., is_market_open=lambda: False),
            intel=NS(regime=NS(detect=lambda *args: {'regime':regime})),
            expiry=NS(get_dte=lambda *args: 3),
            user_lots={instrument:1}, user_lots_fut={instrument:1},
            _get_current_session_day=lambda: '2026-09-29',
            _get_historical_vix=lambda *args: 15.,
            _compute_historical_intel_at=lambda *args: (.3, regime),
            _compute_historical_confluence_at=lambda *args: 0.,
            _allow_inferred_signal_recovery=lambda: False,
            log_event=lambda *args: None, log_signal_decision_once=lambda *args: None,
            _resolve_instrument_type=lambda **kwargs: product_calls.append(kwargs) or 'OPT')
        processor = SignalProcessor.__new__(SignalProcessor)
        processor.scanner = sc
        processor._build_timeframe_contexts = lambda *args: {}
        processor._load_option_candles = lambda *args, **kwargs: (
            loads.append((args,kwargs)) or options, f'{instrument}TESTCE', 'test-token')
        mtf = NS(results_1min=None, results_5min=None, results_15min=None)
        setattr(mtf, 'results_'+tf, {'signals':[signal]})
        lot = {'NIFTY':65, 'BANKNIFTY':30, 'SENSEX':20, 'MIDCPNIFTY':120}[instrument]
        args = (instrument, mtf, {}, .3, regime, 1, lot,
                {'strike_interval':50,'lot_size':lot}, 23000., 23000.)
        return NS(scanner=sc, processor=processor, settings=settings, signal=signal,
                  args=args, loads=loads, product_calls=product_calls)
    return build


@pytest.mark.parametrize('tf', ['5min', '15min'])
def test_final_c_grade_uts_arrow_is_priced_after_confirmation(final_signal, tf):
    host = final_signal(tf=tf)
    assert host.processor.process_best_signal(*host.args) == []
    assert len(host.scanner.trades.closed_trades) == 1
    trade = host.scanner.trades.closed_trades[0]
    assert trade.source_lane == 'UTS' and trade.inst_type == 'OPT'
    assert trade.quality_verdict['allowed'] and trade.quality_verdict['policy_version'] == 'uts_native.v1'
    assert trade.entry_time.replace(tzinfo=None) == host.signal.timestamp+timedelta(minutes=int(tf[:-3]))
    assert trade.signal_time == host.signal.timestamp
    assert trade.exit_reason == 'SESSION_END' and trade.exit_price == 101.
    assert trade.rr_ratio == 3.
    assert trade.target == pytest.approx(100.+(100.-trade.initial_stop)*3.)
    assert trade.replay_price_path and trade.replay_exit_trace[0]['state'] == 'VERIFIED_PRICE_PATH'


@pytest.mark.parametrize('special,allowed', [(False,False), (True,True)])
def test_final_one_minute_uses_native_special_only_admission(final_signal, special, allowed):
    host = final_signal(tf='1min', special=special)
    host.processor.process_best_signal(*host.args)
    assert bool(host.scanner.trades.closed_trades) is allowed
    assert bool(host.loads) is allowed


@pytest.mark.parametrize('tf,at', [('1min',datetime(2026,9,29,15,14)),
    ('5min',datetime(2026,9,29,15,10)), ('15min',datetime(2026,9,29,14,45))])
def test_final_late_arrow_cannot_create_new_entry(final_signal, tf, at):
    host = final_signal(tf=tf, at=at, special=True)
    host.processor.process_best_signal(*host.args)
    assert not host.scanner.trades.closed_trades and not host.loads


@pytest.mark.parametrize('tf', ['5min', '15min'])
def test_final_joint_extremely_weak_setup_stays_blocked(final_signal, tf):
    host = final_signal(tf=tf, weak=True)
    host.processor.process_best_signal(*host.args)
    assert not host.scanner.trades.closed_trades and not host.loads


@pytest.mark.parametrize('missing,hole', [(True,False), (False,True)])
def test_final_requires_complete_observed_option_path(final_signal, missing, hole):
    host = final_signal(missing=missing, hole=hole)
    host.processor.process_best_signal(*host.args)
    assert len(host.loads) == 1 and not host.scanner.trades.closed_trades


@pytest.mark.parametrize('instrument', ['NIFTY','BANKNIFTY','SENSEX','MIDCPNIFTY'])
@pytest.mark.parametrize('tf,regime,tier', [('5min','UNKNOWN','normal'), ('15min','RANGING','wide')])
def test_final_replay_keeps_all_index_stop_profiles(final_signal, instrument, tf, regime, tier):
    host = final_signal(instrument=instrument, tf=tf, regime=regime)
    host.processor.process_best_signal(*host.args)
    assert len(host.scanner.trades.closed_trades) == 1
    trade = host.scanner.trades.closed_trades[0]
    cap = (7. if tier == 'normal' else 10.) if instrument == 'MIDCPNIFTY' else (10. if tier == 'normal' else 15.)
    assert trade.entry_price == 100. and trade.initial_stop == pytest.approx(100.-cap)
    assert trade.quality_verdict['checks']['uts_premium_cap_pct'] == cap


def test_final_auto_uses_paired_product_resolver_without_grade_veto(final_signal):
    host = final_signal(preference='AUTO')
    host.processor.process_best_signal(*host.args)
    assert len(host.scanner.trades.closed_trades) == 1
    assert len(host.product_calls) == 1 and host.scanner.trades.closed_trades[0].inst_type == 'OPT'


def test_final_direction_preference_remains_enforced(final_signal):
    host = final_signal(signal_type='SELL', direction_mode='LONG_ONLY')
    host.processor.process_best_signal(*host.args)
    assert not host.scanner.trades.closed_trades and not host.loads


def test_final_earlier_smart_exit_keeps_actual_exit_reason(final_signal):
    host = final_signal(chart_exit=True)
    host.processor.process_best_signal(*host.args)
    assert len(host.scanner.trades.closed_trades) == 1
    trade = host.scanner.trades.closed_trades[0]
    assert trade.exit_reason == 'UTS_SMART_EXIT'
    assert trade.exit_time.replace(tzinfo=None) == datetime(2026,9,29,10,25)


def test_other_engine_final_branch_stays_unchanged(final_signal):
    host = final_signal(engine='utbot', grade='A')
    host.processor.process_best_signal(*host.args)
    assert not host.scanner.trades.closed_trades and not host.loads


def test_final_option_entry_respects_enabled_risk_sizing(final_signal):
    host = final_signal()
    for key, value in dict(auto_risk_sizing_enabled=True, capital_opt=100.,
                           risk_opt_pct=1., risk_per_trade_opt_pct=1.).items():
        object.__setattr__(host.settings, key, value)
    host.processor.process_best_signal(*host.args)
    assert len(host.loads) == 1 and not host.scanner.trades.closed_trades


def test_final_resolved_quantity_matches_replay_path_accounting(final_signal):
    host = final_signal()
    host.scanner.user_lots['NIFTY'] = 2
    for key, value in dict(auto_risk_sizing_enabled=True, capital_opt=100000.,
                           risk_opt_pct=2., risk_per_trade_opt_pct=1.).items():
        object.__setattr__(host.settings, key, value)
    host.processor.process_best_signal(*host.args)
    assert len(host.scanner.trades.closed_trades) == 1
    trade = host.scanner.trades.closed_trades[0]
    assert trade.lots == 1
    assert trade.replay_price_path[0]['pnl'] == pytest.approx(65.)
    assert trade.replay_price_path[0]['peak_pnl'] == pytest.approx(260.)


def test_final_explicit_futures_preserves_product_and_sizing_cap(final_signal):
    host = final_signal(preference='FUT')
    host.scanner.user_lots_fut['NIFTY'] = 2
    object.__setattr__(host.settings, 'fut_lots_hard_cap', 1)
    host.processor.process_best_signal(*host.args)
    assert len(host.scanner.trades.closed_trades) == 1
    trade = host.scanner.trades.closed_trades[0]
    assert trade.inst_type == 'FUT' and trade.lots == 1
    assert (trade.target-trade.entry_price)/(trade.entry_price-trade.initial_stop) == pytest.approx(trade.rr_ratio)
    assert trade.rr_ratio == host.processor._dynamic_rr('C', .1, timeframe='5min')
    assert not host.loads and not host.product_calls
    assert not trade.replay_price_path  # Existing FUT route remains an index proxy.


def test_final_repeated_historical_scan_does_not_duplicate_trade(final_signal):
    host = final_signal()
    host.processor.process_best_signal(*host.args)
    host.processor.process_best_signal(*host.args)
    assert len(host.scanner.trades.closed_trades) == 1 and len(host.loads) == 1


@pytest.mark.parametrize('tf', ['5min', '15min'])
def test_paired_uts_trade_reports_observed_fill_and_original_signal_time(final_signal, tf):
    host = final_signal(tf=tf)
    opposite = copy.copy(host.signal)
    opposite.timestamp = host.signal.timestamp + timedelta(minutes=30)
    opposite.signal_type = 'SELL'
    opposite.uts_setup = {'rsi_value':44.}
    opposite.trailing_stop = 23150.
    getattr(host.args[1], 'results_'+tf)['signals'].append(opposite)
    host.scanner._detect_power_move = lambda *args, **kwargs: False
    host.processor._is_5min_impulse_reversal = lambda *args, **kwargs: False
    host.processor.process_best_signal(*host.args)
    paired = [trade for trade in host.scanner.trades.closed_trades if trade.id.startswith('H_')]
    assert len(paired) == 1
    trade = paired[0]
    expected = host.signal.timestamp + timedelta(minutes=int(tf[:-3]))
    assert trade.entry_time.replace(tzinfo=None) == expected
    assert trade.replay_exit_trace[0]['entry_time'] == pd.Timestamp(expected).isoformat()
    assert trade.signal_time == host.signal.timestamp
    assert trade.rr_ratio == 3.
    assert trade.target == pytest.approx(trade.entry_price+(trade.entry_price-trade.initial_stop)*3.)


def test_paired_explicit_futures_reports_confirmed_ut_entry_time(final_signal):
    host = final_signal(preference='FUT')
    opposite = copy.copy(host.signal)
    opposite.timestamp = host.signal.timestamp + timedelta(minutes=30)
    opposite.signal_type = 'SELL'
    opposite.uts_setup = {'rsi_value':44.}
    opposite.trailing_stop = 23150.
    host.args[1].results_5min['signals'].append(opposite)
    host.scanner._resolve_instrument_type = lambda **kwargs: 'FUT'
    host.scanner._detect_power_move = lambda *args, **kwargs: False
    host.processor._is_5min_impulse_reversal = lambda *args, **kwargs: False
    host.processor.process_best_signal(*host.args)
    paired = [trade for trade in host.scanner.trades.closed_trades if trade.id.startswith('H_')]
    assert len(paired) == 1
    trade = paired[0]
    assert trade.inst_type == 'FUT'
    assert trade.entry_time.replace(tzinfo=None) == host.signal.timestamp + timedelta(minutes=5)
    assert trade.signal_time == host.signal.timestamp
    assert (trade.target-trade.entry_price)/(trade.entry_price-trade.initial_stop) == pytest.approx(trade.rr_ratio)
    assert trade.rr_ratio == host.processor._dynamic_rr('C', .1, timeframe='5min')
