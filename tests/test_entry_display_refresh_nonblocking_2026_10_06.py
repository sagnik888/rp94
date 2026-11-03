"""Display work in an entry worker must not hold the ledger lock across REST."""

import ast
import inspect
import textwrap
import threading
from types import SimpleNamespace

import pytest

from engine.entry_analysis_service import EntryAnalysisService
from scanner import Scanner


@pytest.fixture
def display_refresh():
    """Run the production display block with real Scanner quote/locking methods."""
    body = ast.parse(textwrap.dedent(inspect.getsource(EntryAnalysisService.process))).body[0].body
    start = next(i for i, node in enumerate(body)
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "display_candidates"
                         for target in node.targets))
    end = next(i for i in range(start, len(body))
               if isinstance(body[i], ast.Expr)
               and isinstance(body[i].value, ast.Call)
               and isinstance(body[i].value.func, ast.Attribute)
               and body[i].value.func.attr == "_refresh_trade_candidates")
    code = compile(ast.fix_missing_locations(ast.Module(body=body[start:end + 1], type_ignores=[])),
                   "<production entry display refresh>", "exec")
    return lambda host: exec(code, {"scanner": host, "name": "NIFTY", "cfg": {}})


def _host(candidate, quote=0.0):
    host = Scanner.__new__(Scanner)
    host.mode = "REAL"
    host._session_breach_lock = threading.RLock()
    host._loaded_session_is_current_day = lambda: True
    host._get_session_trade_candidates = lambda _: [candidate]
    host._premium_cache = {}
    host._candidate_ltp_cache = {}
    host._premium_refresh_guard = threading.Lock()
    host._premium_refresh_inflight = set()
    host._premium_refresh_at = {}
    host.market_info = {}
    host._candidate_net_pnl = lambda row, mark: mark - row.price
    host._option_premium_quote_plausible = lambda row, mark: True
    host._get_option_chain_premium = lambda *a, **kw: 0.0
    host._contract_quote_age_seconds = lambda *a: 0.0
    host.candles = SimpleNamespace(get_latest_price=lambda _: 24000.0)
    host.broker_calls = []
    host.data = SimpleNamespace(
        get_cached_contract_ltp=lambda *a, **kw: quote,
        subscribe_tokens=lambda *a, **kw: None,
        get_ltp=lambda *a: host.broker_calls.append(threading.get_ident()) or 210.0,
    )
    return host


def _candidate(inst_type="OPT", **extra):
    values = dict(instrument="NIFTY", inst_type=inst_type, action="ENTRY",
                  trading_symbol="NIFTY26OCT24000CE" if inst_type == "OPT" else "NIFTY26OCTFUT",
                  symbol_token="1234", price=200.0, current_price=200.0, pnl=0.0,
                  quote_verified=True, accepted_by_gate=True, status="TRADE SIGNAL")
    values.update(extra)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("inst_type", ["OPT", "FUT"])
def test_entry_display_uses_fresh_contract_mark_without_broker(display_refresh, inst_type):
    row = _candidate(inst_type)
    host = _host(row, quote=210.0)
    display_refresh(host)
    assert host.broker_calls == []
    assert row.current_price == 210.0 and row.pnl == 10.0
    assert row.live_quote_stale is False


@pytest.mark.parametrize("inst_type", ["OPT", "FUT"])
def test_slow_contract_refresh_releases_entry_worker_and_shared_ledger(display_refresh, inst_type):
    row = _candidate(inst_type)
    host = _host(row)
    broker_started, broker_release, finished = threading.Event(), threading.Event(), threading.Event()
    premium_finished = threading.Event()
    failures = []
    premium_worker = host._premium_refresh_worker

    def refresh_premium(*args):
        try:
            premium_worker(*args)
        finally:
            premium_finished.set()

    def lookup(*args):
        host.broker_calls.append(threading.get_ident())
        broker_started.set()
        assert broker_release.wait(2.0)
        return 210.0

    def refresh():
        try:
            display_refresh(host)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    host.data.get_ltp = lookup
    host._premium_refresh_worker = refresh_premium
    worker = threading.Thread(target=refresh)
    worker.start()
    try:
        assert broker_started.wait(1.0), "a stale contract did not schedule recovery"
        assert finished.wait(0.3), "entry worker waits on REST while owning the ledger lock"
        acquired = host._session_breach_lock.acquire(blocking=False)
        assert acquired, "timed-out scanner fallback cannot read the ledger"
        if acquired:
            host._session_breach_lock.release()
        assert not failures
        assert len(host.broker_calls) == 1
        assert host.broker_calls[0] != worker.ident
        assert row.live_quote_stale is True
        assert row.current_price == 200.0 and row.quote_verified is True
    finally:
        broker_release.set()
        worker.join(2.0)
        if host._premium_refresh_inflight:
            assert premium_finished.wait(2.0)
    cache = host._premium_cache if inst_type == "OPT" else host._candidate_ltp_cache
    key = row.symbol_token if inst_type == "OPT" else f"FUT:{row.symbol_token}"
    assert cache[key]["price"] == 210.0
    display_refresh(host)
    assert row.current_price == 210.0 and row.pnl == 10.0
    assert row.live_quote_stale is False
    assert len(host.broker_calls) == 1


def test_entry_display_retains_observed_exit_price(display_refresh):
    row = _candidate(action="EXIT", exit_price=185.0)
    host = _host(row, quote=220.0)
    display_refresh(host)
    assert row.current_price == 185.0
    assert row.exit_price == 185.0
    assert host.broker_calls == []
