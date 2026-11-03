from datetime import datetime
from engine.runtime_build import capture_build, build_status
from trading.trade_dashboard import _serialize_trade_row
from trading.trade_manager import Trade
from types import SimpleNamespace
import asyncio
import json

import pytest

import dashboard.server as server


def test_runtime_identity_retains_import_snapshot_when_source_changes(tmp_path):
    source=tmp_path/'scanner.py';source.write_text('first')
    original=capture_build(tmp_path)
    assert build_status(original,tmp_path)['reload_required'] is False
    source.write_text('second')
    status=build_status(original,tmp_path)
    assert status['fingerprint']==original['fingerprint']
    assert status['reload_required'] is True
    assert status['source_files_changed_since_import']==['scanner.py']


def test_replay_row_exposes_original_contract_and_input_hash_without_full_path():
    trade=Trade(id='H1',instrument='NIFTY',timeframe='5min',direction='LONG',entry_price=100,
                entry_time=datetime(2026,9,11,10,5),trailing_stop=90,current_stop=95,lots=1,lot_size=65,
                grade='A',inst_type='OPT',trading_symbol='NIFTY2692223300CE',
                data_provenance='BACKTEST',replay_exit_trace=[{'guard':'replay_input','input_sha256':'abc'}])
    row=_serialize_trade_row(SimpleNamespace(options_sl_pct=15),trade)
    assert row['trading_symbol']=='NIFTY2692223300CE'
    assert row['replay_input']['input_sha256']=='abc'
    assert 'replay_price_path' not in row


def _replay_scanner(trade=None, **overrides):
    values = dict(
        mode='HISTORICAL', scan_count=1, simulation_id=42,
        trades=SimpleNamespace(closed_trades=[] if trade is None else [trade]),
        _historical_intel_coverage={'simulation_id': 42, 'by_index_timeframe': {}},
        _runtime_build_status=lambda: {'fingerprint': 'loaded-source'},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _audit_request(trade_id='H1', *, token='test-only-token', peer='127.0.0.1'):
    """Exercise the real ASGI route and middleware without network or lifespan."""
    async def run():
        messages = []
        request = asyncio.Queue()
        await request.put({'type': 'http.request', 'body': b'', 'more_body': False})
        path = '/api/replay-audit/' + trade_id
        scope = {
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
            'root_path': '', 'query_string': b'',
            'headers': [] if token is None else [(b'x-dashboard-token', token.encode())],
            'client': (peer, 12345), 'server': ('localhost', 8500),
        }
        async def send(message):
            messages.append(message)
        await server.app(scope, request.get, send)
        status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
        body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        return status, json.loads(body)
    return asyncio.run(run())


@pytest.mark.parametrize('token,peer,expected', [
    (None, '127.0.0.1', 401),
    ('wrong-token', '127.0.0.1', 401),
    ('test-only-token', '192.0.2.1', 403),
    ('test-only-token', '127.0.0.1', 200),
    ('test-only-token', '::1', 200),
])
def test_replay_audit_requires_loopback_and_dashboard_token(monkeypatch, token, peer, expected):
    monkeypatch.setattr(server, '_dashboard_token', lambda: 'test-only-token')
    monkeypatch.setattr(server, 'scanner_ref', None)
    status, body = _audit_request(token=token, peer=peer)
    assert status == expected
    if expected == 200:
        assert body['status'] == 'unavailable'
    assert 'trade' not in body


@pytest.mark.parametrize('scanner,expected', [
    (None, 'unavailable'),
    (_replay_scanner(mode='REAL'), 'unavailable'),
    (_replay_scanner(), 'unavailable'),
    (_replay_scanner(SimpleNamespace(id='H1', replay_exit_trace=[])), 'unavailable'),
    (_replay_scanner(scan_count=0), 'building'),
    (_replay_scanner(_historical_rebuild_pending=True), 'building'),
    (_replay_scanner(_recalculation_status={'status': 'applying'}), 'building'),
])
def test_replay_audit_does_not_invent_unavailable_traces(monkeypatch, scanner, expected):
    monkeypatch.setattr(server, '_dashboard_token', lambda: 'test-only-token')
    monkeypatch.setattr(server, 'scanner_ref', scanner)
    status, body = _audit_request()
    assert status == 200
    assert body['status'] == expected
    assert 'trade' not in body


def test_replay_audit_returns_original_contract_path_and_guard_evidence(monkeypatch):
    trace = [{'guard': 'replay_input', 'input_sha256': 'abc'}, {'guard': 'SL HIT', 'price': 90}]
    trade = Trade(
        id='H1', instrument='NIFTY', timeframe='5min', direction='LONG', entry_price=100,
        entry_time=datetime(2026, 9, 11, 10, 5), trailing_stop=90, current_stop=95,
        lots=1, lot_size=65, grade='A', inst_type='OPT', status='CLOSED',
        trading_symbol='NIFTY2692223300CE', data_provenance='BACKTEST',
        replay_exit_trace=trace, replay_price_path=[{'timestamp': '2026-09-11T10:05:00', 'open': 100}],
    )
    monkeypatch.setattr(server, '_dashboard_token', lambda: 'test-only-token')
    monkeypatch.setattr(server, 'scanner_ref', _replay_scanner(trade))
    status, body = _audit_request()
    assert status == 200
    assert body['status'] == 'ok'
    assert body['simulation_id'] == 42
    assert body['runtime_build']['fingerprint'] == 'loaded-source'
    assert body['historical_intelligence_coverage']['simulation_id'] == 42
    assert body['trade']['trading_symbol'] == trade.trading_symbol
    assert body['trade']['replay_price_path'] == trade.replay_price_path
    assert body['trade']['replay_exit_trace'] == trace


def test_replay_audit_rechecks_mode_after_scanner_loop_dispatch(monkeypatch):
    trade = SimpleNamespace(id='H1', replay_exit_trace=[{'guard': 'SL HIT'}], to_dict=lambda: {'id': 'H1'})
    scanner = _replay_scanner(trade)
    monkeypatch.setattr(server, 'scanner_ref', scanner)

    async def dispatch(capture):
        scanner.mode = 'REAL'
        return capture()

    monkeypatch.setattr(server, '_call_on_scanner_loop', dispatch)
    body = asyncio.run(server.api_replay_audit('H1'))
    assert body['status'] == 'unavailable'
    assert 'trade' not in body
