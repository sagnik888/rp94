"""Observed replay fills must drive admission, sizing and report accounting."""
import copy
from datetime import timedelta

import pandas as pd
import pytest

from tests.test_uts_final_historical_signal_2026_09_30 import final_signal


def paired_host(final_signal, monkeypatch, *, observed_premium, estimated_premium):
    host = final_signal()
    opposite = copy.copy(host.signal)
    opposite.timestamp += timedelta(minutes=30)
    opposite.signal_type = 'SELL'
    opposite.uts_setup = {'rsi_value':44.}
    opposite.trailing_stop = 23150.
    host.args[1].results_5min['signals'].append(opposite)
    host.scanner._detect_power_move = lambda *args, **kwargs: False
    host.processor._is_5min_impulse_reversal = lambda *args, **kwargs: False
    index = pd.date_range('2026-09-29 09:15', '2026-09-29 15:24', freq='1min')
    options = pd.DataFrame(dict(open=observed_premium, high=observed_premium+4.,
                                low=observed_premium-1., close=observed_premium+1.), index=index)
    host.processor._load_option_candles = lambda *args, **kwargs: (options, 'TESTCE', 'test-token')
    monkeypatch.setattr('engine.signal_processor._estimate_opt_premium',
                        lambda *args, **kwargs: estimated_premium)
    for key, value in dict(auto_risk_sizing_enabled=True, capital_opt=100000.,
                           risk_opt_pct=2., risk_per_trade_opt_pct=1.).items():
        object.__setattr__(host.settings, key, value)
    return host


@pytest.mark.parametrize('observed_premium,estimated_premium,expected_count', [
    (300., 100., 0),  # Actual one-lot risk 1,950 exceeds the 1,000 budget.
    (100., 300., 1),  # Actual one-lot risk 650 fits, despite the provisional estimate.
])
def test_paired_uts_sizes_from_observed_fill_not_provisional_estimate(
        final_signal, monkeypatch, observed_premium, estimated_premium, expected_count):
    host = paired_host(final_signal, monkeypatch, observed_premium=observed_premium,
                       estimated_premium=estimated_premium)
    host.processor.process_best_signal(*host.args)
    paired = [trade for trade in host.scanner.trades.closed_trades if trade.id.startswith('H_')]
    assert len(paired) == expected_count
    for trade in paired:
        assert (trade.entry_price-trade.initial_stop)*trade.lot_size*trade.lots <= 1000.


def test_paired_uts_futures_cannot_exit_before_confirmed_entry(final_signal):
    host = final_signal(preference='FUT')
    opposite = copy.copy(host.signal)
    opposite.timestamp += timedelta(minutes=30)
    opposite.signal_type = 'SELL'
    opposite.uts_setup = {'rsi_value':44.}
    opposite.trailing_stop = 23150.
    host.args[1].results_5min['signals'].append(opposite)
    host.scanner._resolve_instrument_type = lambda **kwargs: 'FUT'
    host.scanner._detect_power_move = lambda *args, **kwargs: False
    host.processor._is_5min_impulse_reversal = lambda *args, **kwargs: False
    candles = host.scanner.candles.get_candles('NIFTY', '1min').copy()
    candles['low'] = 22990.
    candles['high'] = 23010.
    candles.loc['2026-09-29 10:00', 'low'] = 22900.
    host.scanner.candles.get_candles = lambda *args: candles
    host.processor.process_best_signal(*host.args)
    paired = [trade for trade in host.scanner.trades.closed_trades if trade.id.startswith('H_')]
    assert len(paired) == 1
    trade = paired[0]
    assert trade.exit_time >= trade.entry_time
    assert trade.exit_reason != 'SL HIT'


def test_final_uts_futures_checks_resting_stop_before_recovery(final_signal):
    host = final_signal(preference='FUT')
    candles = host.scanner.candles.get_candles('NIFTY', '1min').copy()
    candles['low'] = 22990.
    candles['high'] = 23010.
    candles.loc['2026-09-29 10:20', 'low'] = 22900.
    host.scanner.candles.get_candles = lambda *args: candles
    host.processor.process_best_signal(*host.args)
    assert len(host.scanner.trades.closed_trades) == 1
    trade = host.scanner.trades.closed_trades[0]
    assert trade.exit_reason == 'SL HIT'
    assert trade.exit_price == pytest.approx(trade.initial_stop)


def test_observed_paired_quantity_rescales_path_and_charges(final_signal, monkeypatch):
    from engine.signal_processor import estimate_trade_charges
    host = paired_host(final_signal, monkeypatch, observed_premium=100., estimated_premium=300.)
    host.scanner.user_lots['NIFTY'] = 2
    host.processor.process_best_signal(*host.args)
    paired = [trade for trade in host.scanner.trades.closed_trades if trade.id.startswith('H_')]
    assert len(paired) == 1
    trade = paired[0]
    assert trade.lots == 1
    assert trade.replay_price_path[0]['pnl'] == pytest.approx(65.)
    assert trade.replay_price_path[0]['peak_pnl'] == pytest.approx(260.)
    charges = estimate_trade_charges(trade.entry_price, trade.exit_price, 65, 'OPT', host.settings)
    assert trade.charges == pytest.approx(charges)
    assert trade.pnl == pytest.approx((trade.exit_price-trade.entry_price)*65-charges)


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('direction', ['BUY', 'SELL'])
def test_uts_futures_gap_stop_preserves_observed_fill(final_signal, paired, direction):
    host = final_signal(preference='FUT', signal_type=direction)
    if paired:
        opposite = copy.copy(host.signal)
        opposite.timestamp += timedelta(minutes=30)
        opposite.signal_type = 'SELL' if direction == 'BUY' else 'BUY'
        opposite.trailing_stop = 23150. if direction == 'BUY' else 22850.
        host.args[1].results_5min['signals'].append(opposite)
        host.scanner._resolve_instrument_type = lambda **kwargs: 'FUT'
        host.scanner._detect_power_move = lambda *args, **kwargs: False
        host.processor._is_5min_impulse_reversal = lambda *args, **kwargs: False
    candles = host.scanner.candles.get_candles('NIFTY', '1min').copy()
    candles['low'], candles['high'] = 22990., 23010.
    gap = 22900. if direction == 'BUY' else 23100.
    candles.loc['2026-09-29 10:20', ['open','low','high','close']] = [gap,gap-1.,gap+1.,gap]
    host.scanner.candles.get_candles = lambda *args: candles
    host.processor.process_best_signal(*host.args)
    prefix = 'H_' if paired else 'EOD'
    trades = [trade for trade in host.scanner.trades.closed_trades if trade.id.startswith(prefix)]
    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == 'SL HIT'
    assert trade.exit_price == gap
    assert trade.exit_time.replace(tzinfo=None) == pd.Timestamp('2026-09-29 10:20')


@pytest.fixture(autouse=True)
def _without_uts_5m_intel_gate(monkeypatch):
    """These 5m fixtures predate the 6 Oct 5m intel gate and carry no measured volume.

    The gate has its own tests in test_uts_5m_intel_gate_2026_10_06.py.
    """
    from engine import uts_lane
    monkeypatch.setattr(uts_lane, "five_minute_intel_failures", lambda *a, **k: [])
