"""Historical futures fills must survive the dashboard accounting boundary."""
import copy
from datetime import timedelta

import pandas as pd
import pytest

from engine.trade_row_adapter import reconcile_legacy_trade_row
from trading.trade_dashboard import _serialize_trade_row
from tests.test_uts_final_historical_signal_2026_09_30 import final_signal


@pytest.mark.parametrize('paired', [False, True])
@pytest.mark.parametrize('direction', ['BUY', 'SELL'])
def test_uts_futures_gap_fill_survives_dashboard_adapter(final_signal, paired, direction):
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
    assert trade.exit_price == gap
    row = _serialize_trade_row(host.scanner, trade)
    displayed = reconcile_legacy_trade_row(row)
    assert displayed['exit_price'] == gap
    assert displayed['pnl'] == pytest.approx(round(trade.pnl, 2))
    assert displayed['charges'] == pytest.approx(trade.charges)


@pytest.mark.parametrize('paired', [False, True])
def test_uts_futures_uses_canonical_chart_exit_reason(final_signal, paired):
    host = final_signal(preference='FUT', chart_exit=True)
    if paired:
        opposite = copy.copy(host.signal)
        opposite.timestamp += timedelta(minutes=30)
        opposite.signal_type = 'SELL'
        opposite.trailing_stop = 23150.
        host.args[1].results_5min['signals'].append(opposite)
        host.scanner._resolve_instrument_type = lambda **kwargs: 'FUT'
        host.scanner._detect_power_move = lambda *args, **kwargs: False
        host.processor._is_5min_impulse_reversal = lambda *args, **kwargs: False
    candles = host.scanner.candles.get_candles('NIFTY', '1min').copy()
    candles['low'], candles['high'] = 22990., 23010.
    host.scanner.candles.get_candles = lambda *args: candles
    host.processor.process_best_signal(*host.args)
    prefix = 'H_' if paired else 'EOD'
    trades = [trade for trade in host.scanner.trades.closed_trades if trade.id.startswith(prefix)]
    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == 'UTS_SMART_EXIT'
    assert trade.exit_time.replace(tzinfo=None) == pd.Timestamp('2026-09-29 10:25')


@pytest.mark.parametrize('damage', ['hash','path','exit','reason','time','provenance'])
def test_incomplete_or_unbound_futures_evidence_cannot_skip_legacy_accounting(final_signal, damage):
    host = final_signal(preference='FUT')
    candles = host.scanner.candles.get_candles('NIFTY', '1min').copy()
    candles['low'], candles['high'] = 22990., 23010.
    candles.loc['2026-09-29 10:20', ['open','low','high','close']] = [22900.,22899.,22901.,22900.]
    host.scanner.candles.get_candles = lambda *args: candles
    host.processor.process_best_signal(*host.args)
    trade = host.scanner.trades.closed_trades[0]
    row = trade.to_dict()
    evidence = row['replay_exit_trace'][0]
    assert evidence['fill_accounting'] == 'CAUSAL_INDEX_PROXY_V1'
    assert not evidence['actual_futures_prices'] and not evidence['contract_verified']
    assert evidence['state'] == 'INDEX_PRICE_PROXY'
    if damage == 'hash': evidence['input_sha256'] = ''
    elif damage == 'path': evidence['complete_path'] = False
    elif damage == 'exit': evidence['exit_price'] = 1.
    elif damage == 'reason': evidence['exit_reason'] = 'other'
    elif damage == 'time': evidence['exit_time'] = '2026-09-29T10:21:00'
    else: row['data_provenance'] = 'LIVE'
    displayed = reconcile_legacy_trade_row(row)
    assert displayed['exit_price'] == trade.initial_stop


@pytest.fixture(autouse=True)
def _without_uts_5m_intel_gate(monkeypatch):
    """These 5m fixtures predate the 6 Oct 5m intel gate and carry no measured volume.

    The gate has its own tests in test_uts_5m_intel_gate_2026_10_06.py.
    """
    from engine import uts_lane
    monkeypatch.setattr(uts_lane, "five_minute_intel_failures", lambda *a, **k: [])
