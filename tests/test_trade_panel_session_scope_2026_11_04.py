"""The live panel shows only entries accepted on the current IST session day."""
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.scanner_dashboard import ScannerTradePayloadBuilder


@pytest.mark.parametrize("entry_day", ["2026-11-03", "2026-11-04"])
def test_payload_shows_only_today_entry_cohort(entry_day):
    row = dict(id="carry-exit", lifecycle_id="carry", instrument="NIFTY", timeframe="5min",
               direction="LONG", inst_type="OPT", action="EXIT", accepted_by_gate=True,
               source_engine="ut_ema", signal_timestamp="2026-11-03T15:00:00",
               timestamp="2026-11-03T15:05:01", entry_timestamp="2026-11-03T15:05:01",
               exit_timestamp="2026-11-04T09:15:02", pnl=-100., exit_reason="UTEMA_INDEX_STOP")
    row["entry_timestamp"] = f"{entry_day}T09:21:29"
    candidate = SimpleNamespace(**{**row, "signal_timestamp": datetime(2026, 11, 3, 15)})
    scanner = SimpleNamespace(
        mode="REAL", backtest_days=1, inst_pref="AUTO", active_indices=["NIFTY"],
        trades=SimpleNamespace(get_dashboard_payload=lambda **kw: {
            "open": [], "closed": [], "summary": {}, "analytics": {}}),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-11-04",
        _loaded_session_is_current_day=lambda: True,
        _get_session_trade_candidates=lambda instrument: [candidate],
        _candidate_matches_live_session_day=lambda value: True,
        _normalize_loaded_exit_candidate=lambda value: None,
        _serialize_trade_candidate=lambda value: dict(row),
    )
    result = ScannerTradePayloadBuilder(scanner).build()
    assert result["meta"]["signals_authoritative"] is True
    if entry_day != "2026-11-04":
        assert result["signals"] == []
        return
    assert len(result["signals"]) == 1
    assert result["signals"][0]["entry_timestamp"] == row["entry_timestamp"]
    assert result["meta"]["session_day"] == "2026-11-04"
    assert result["meta"]["signals_authoritative"] is True
    assert result["session_closed_total"] == 1


NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not NODE, reason="Node is required for dashboard behavior tests")
def test_browser_session_scope_and_authoritative_empty_payload():
    program = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const ctx = {window: {}, document: {addEventListener() {}}, assert, state: {latestData: {mode: 'REAL', timestamp: '2026-11-04T09:20:00+05:30',
    trades: {meta: {session_day: '2026-11-04'}}, instruments: {}}}, console};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('dashboard/static/js/trade-table.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync('dashboard/static/js/trade-pagination.js', 'utf8'), ctx);
vm.runInContext(`
const entry = {id:'old', lifecycle_id:'old', instrument:'NIFTY', timeframe:'5min',
    direction:'LONG', inst_type:'OPT', action:'ENTRY', accepted_by_gate:true,
    timestamp:'2026-11-03T15:05:01', entry_timestamp:'2026-11-03T15:05:01'};
state.latestData.instruments = {NIFTY:{trade_candidates:[entry]}};
assert.equal(collectSessionSignalRows().length, 0, 'yesterday generic signal leaked');
const exited = {...entry, action:'EXIT', exit_timestamp:'2026-11-03T15:10:00'};
state.latestData.instruments.NIFTY.trade_candidates = [exited];
assert.equal(collectSessionSignalRows().length, 0, 'yesterday exit leaked');
const carry = {...entry, source_engine:'ut_ema', utema_state:{lifecycle_id:'old'}};
state.latestData.instruments.NIFTY.trade_candidates = [carry];
assert.equal(collectSessionSignalRows().length, 0, 'yesterday carry entered today table');
const todayExit = {...carry, action:'EXIT', exit_timestamp:'2026-11-04T09:15:02'};
state.latestData.instruments.NIFTY.trade_candidates = [carry, todayExit];
const rows = collectSessionSignalRows();
assert.equal(rows.length, 0, 'today exit resurrected yesterday entry');
const fresh = {...entry, id:'fresh', lifecycle_id:'fresh', timestamp:'2026-11-04T09:21:29', entry_timestamp:'2026-11-04T09:21:29'};
assert.equal(collectSessionSignalRows([], [fresh]).length, 1);
assert.equal(tradeSessionDay('2026-11-03T20:00:00Z'), '2026-11-04');
assert.equal(tradeSessionDay('2026-11-03T23:59:00'), '2026-11-03');
state.latestData.trades.meta.signals_authoritative = true;
assert.equal(collectSessionSignalRows([], []).length, 0, 'snapshot resurrected authoritative empty ledger');
assert.equal(collectSessionSignalRows([], [todayExit]).length, 0);
assert.equal(collectSessionSignalRows([], [fresh]).length, 1);
const a = tradePayloadContextKey({}, {meta:{mode:'REAL',session_day:'2026-11-03'}});
const b = tradePayloadContextKey({}, {meta:{mode:'REAL',session_day:'2026-11-04'}});
assert.notEqual(a,b, 'pagination must reset on session rollover');
state.latestData.mode = 'HISTORICAL';
state.latestData.trades.meta.signals_authoritative = false;
state.latestData.instruments.NIFTY.trade_candidates = [entry];
assert.equal(collectSessionSignalRows().length,1,'historical row was hidden');
assert.equal(collectSessionSignalRows()[0].carried_from_session,'','historical row mislabeled as carry');
`, ctx);
'''
    result = subprocess.run([NODE, "-e", program], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
