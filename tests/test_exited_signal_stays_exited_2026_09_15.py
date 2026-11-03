"""A closed manual signal renders once, as history, and is never executable again; a later
re-signal on the same candle stays actionable.

2026-09-15, REAL mode, Auto Mode off.

09:17 NIFTY 23450CE and SENSEX 75200CE fired as manual signals and EARLY_LOSS_CUT closed
both at 09:17:53. Commit 02729d7 (2026-09-09) made the server payload and the trade table
drop completed signal lifecycles, but the table's re-scrape of instrument snapshots dropped
EXIT rows before pairing them, so each cut ENTRY came back as a live row: P&L Rs.0, no exit
time, LTP far below the stop, and an enabled ENTRY button. The backend would have taken the
click: execute_manual_signal found the ENTRY (still accepted_by_gate) and re-anchored the
option levels to the live ask, so nothing downstream refused it.

With the ghosts gone the "signals" panel stayed empty while Diagnostics reported "3 sig /
5 exit": every signal that session closed within seconds to minutes, and completed
lifecycles were hidden by design. Each completed lifecycle now renders once as its EXIT
row, labelled a signal-tracker mark rather than money.

Two identity defects surfaced in the ledger:

* An EXIT row inherits its entry's trade_id and session_signals was keyed on id alone, so
  INSERT OR REPLACE kept one row per lifecycle. MIDCPNIFTY 15min was persisted as an open
  TRADE SIGNAL although LIVE_REVERSAL_EXIT closed it at 11:08:17; a restart would have
  restored it as an actionable entry with no exit left to pair against.
* A re-signal on the same candle and contract shares the lifecycle key of the signal that
  repaint-aborted before it (11:01 abort, 11:08:12 re-signal). Pairing on the key alone hid
  and blocked the live re-signal, so an exit only closes an entry accepted at or before it.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from data.sqlite_db import DatabaseManager
from engine.scanner_dashboard import ScannerTradePayloadBuilder
from engine.signal_processor import TradeCandidate
from scanner import Scanner

ROOT = Path(__file__).resolve().parents[1]
TRADE_TABLE = ROOT / "dashboard" / "static" / "js" / "trade-table.js"
NODE = shutil.which("node")

# Runs trade-table.js as the browser does (a classic script sharing globals with app.js)
# with just enough DOM to drive both renderers.
DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const [scriptPath, casePath] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(casePath, 'utf8'));
const htmlWrites = [];
const tbody = {
    children: [],
    querySelectorAll: () => [],
    querySelector: () => null,
    appendChild(tr) { this.children.push(tr); },
    insertBefore(tr) { this.children.push(tr); },
};
const context = {
    console,
    window: {},
    state: { latestData: { instruments: input.instruments, mode: 'REAL' } },
    document: {
        addEventListener() {},
        getElementById: id => (id === 'tradesBody' ? tbody : null),
        createElement: () => ({ setAttribute() {}, className: '', innerHTML: '' }),
    },
    setHtml: (el, html) => htmlWrites.push(html),
    patchRow: (tr, html) => { tr.innerHTML = html; },
    formatPrice: p => String(p),
    baseInstrumentLabel: v => String(v || '').split(/\s+/)[0],
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), context, { filename: 'trade-table.js' });
const rows = context.collectSessionSignalRows(input.existingRows, input.backendSignals);
const summary = context.buildSessionSummary(input.baseSummary, [], [], rows);
context.updateSignalsFeed(input.instruments);
process.stdout.write(JSON.stringify({
    rows: rows.map(r => ({
        instrument: r.base_instrument,
        status: r.status,
        exec_type: r.exec_type,
        exit_reason: r.exit_reason,
        pnl: r.pnl,
        lifecycle_id: r.lifecycle_id,
    })),
    summary,
    feedRows: tbody.children.map(tr => tr.innerHTML),
    htmlWrites,
}));
"""


def _snapshot_row(instrument, symbol, action, *, candle="09:15:00", lane="", lifecycle=True,
                  entered="09:17:05", exited="09:17:53", pnl=-258.75):
    """A trade_candidates row shaped like Scanner._serialize_trade_candidate output."""
    source_ts = f"2026-09-15T{candle}"
    entry_ts = f"2026-09-15T{entered}"
    is_exit = action == "EXIT"
    row = {
        "id": f"SESSION|{instrument}|5min|ENTRY|LONG|{source_ts}",
        "instrument": instrument,
        "timeframe": "5min",
        "direction": "LONG",
        "trade_side": "BUY CE",
        "inst_type": "OPT",
        "option_type": "CE",
        "atm_strike": 23450,
        "trading_symbol": symbol,
        "source_lane": lane,
        "action": action,
        "status": "EXIT SIGNAL" if is_exit else "TRADE SIGNAL",
        "is_exit": is_exit,
        "exit_reason": "EARLY_LOSS_CUT" if is_exit else "",
        "price": 88.45,
        "current_price": 85.7 if is_exit else 63.5,
        "exit_price": 85.7 if is_exit else 0.0,
        "stop": 84.0275,
        "target": 0 if is_exit else 95.08,
        "rr": 1.5,
        "lots": 1,
        "lot_size": 65,
        "grade": "A+",
        "confidence": 0.96,
        # An open signal carries a mark-to-market estimate the table must not show.
        "pnl": pnl if is_exit else -1701.75,
        "accepted_by_gate": True,
        "watch_only": False,
        "signal_timestamp": source_ts,
        "source_signal_timestamp": source_ts,
        "timestamp": entry_ts,
        "entry_timestamp": entry_ts,
        "exit_timestamp": f"2026-09-15T{exited}" if is_exit else "",
    }
    if lifecycle:
        row["lifecycle_id"] = f"{instrument}|5min|LONG|{source_ts}|{lane}|{symbol}"
    return row


def _live_cuts():
    return {
        "NIFTY": {"trade_candidates": [
            _snapshot_row("NIFTY", "NIFTY2691523450CE", "ENTRY"),
            _snapshot_row("NIFTY", "NIFTY2691523450CE", "EXIT"),
        ]},
        "SENSEX": {"trade_candidates": [
            _snapshot_row("SENSEX", "SENSEX2691775200CE", "ENTRY", entered="09:17:02"),
            _snapshot_row("SENSEX", "SENSEX2691775200CE", "EXIT", entered="09:17:02", pnl=-336.0),
        ]},
    }


def _render(tmp_path, instruments, backend_signals=(), existing_rows=(), base_summary=None):
    driver = tmp_path / "driver.js"
    driver.write_text(DRIVER, encoding="utf-8")
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps({
            "instruments": instruments,
            "backendSignals": list(backend_signals),
            "existingRows": list(existing_rows),
            "baseSummary": dict(base_summary or {}),
        }),
        encoding="utf-8",
    )
    out = subprocess.run(
        [NODE, str(driver), str(TRADE_TABLE), str(case)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node is required to execute trade-table.js")


@needs_node
def test_each_cut_signal_renders_once_as_a_completed_signal_row(tmp_path):
    result = _render(tmp_path, _live_cuts())

    rows = sorted(result["rows"], key=lambda row: row["instrument"])
    assert [
        (row["instrument"], row["status"], row["exec_type"], row["exit_reason"], row["pnl"])
        for row in rows
    ] == [
        ("NIFTY", "EXIT_SIGNAL", "SIG", "EARLY_LOSS_CUT", -258.75),
        ("SENSEX", "EXIT_SIGNAL", "SIG", "EARLY_LOSS_CUT", -336.0),
    ], "a cut signal must render once, as its exit, never as a live entry"
    assert result["feedRows"] == [], "the open-signal feed offered a cut signal"
    assert any("No final trade signals yet" in html for html in result["htmlWrites"])


@needs_node
def test_the_server_and_snapshot_copies_of_one_exit_render_one_row(tmp_path):
    backend = [_snapshot_row("NIFTY", "NIFTY2691523450CE", "EXIT")]
    result = _render(tmp_path, _live_cuts(), backend_signals=backend)

    assert sorted(row["instrument"] for row in result["rows"]) == ["NIFTY", "SENSEX"]


@needs_node
def test_signal_tracker_totals_are_never_labelled_money(tmp_path):
    live_lane = {"lane": "LIVE", "pnl_label": "DAILY P&L", "pnl_is_money": True, "is_signal_tracker": False}
    summary = _render(tmp_path, _live_cuts(), base_summary=live_lane)["summary"]

    assert summary["daily_pnl"] == -594.75
    assert summary["total_trades"] == 2
    assert summary["lane"] == "SIGNAL_TRACKER"
    assert summary["pnl_label"] == "SIGNAL P&L"
    assert summary["pnl_is_money"] is False
    assert summary["is_signal_tracker"] is True


@needs_node
def test_a_lifecycle_the_trade_manager_executed_shows_only_the_broker_row(tmp_path):
    broker_row = {
        "id": "T0021",
        "row_kind": "manager_closed",
        "status": "CLOSED",
        "instrument": "NIFTY 23450 CE",
        "timeframe": "5min",
        "direction": "LONG",
        "lifecycle_id": "NIFTY|5min|LONG|2026-09-15T09:15:00||NIFTY2691523450CE",
    }
    result = _render(tmp_path, _live_cuts(), existing_rows=[broker_row])

    assert [row["instrument"] for row in result["rows"]] == ["SENSEX"]


@needs_node
def test_a_signal_that_was_not_cut_is_still_offered(tmp_path):
    instruments = {"NIFTY": {"trade_candidates": [
        _snapshot_row("NIFTY", "NIFTY2691523450CE", "ENTRY"),
    ]}}
    result = _render(tmp_path, instruments)

    assert [(row["status"], row["pnl"]) for row in result["rows"]] == [("SIGNAL", 0)]
    assert len(result["feedRows"]) == 1
    assert 'data-action="execute-signal"' in result["feedRows"][0]


@needs_node
def test_unacknowledged_or_refused_candidate_never_enters_trade_panel(tmp_path):
    rejected = _snapshot_row("SENSEX", "SENSEX26O0872500CE", "ENTRY")
    rejected.update({
        "accepted_by_gate": False,
        "status": "REJECTED - TRADE MANAGER",
        "exit_reason": "ENTRY_MARK_VALIDATION",
    })
    instruments = {"SENSEX": {"trade_candidates": [rejected]}}

    result = _render(
        tmp_path,
        instruments,
        backend_signals=[rejected],
        base_summary={
            "lane": "SIGNAL_TRACKER",
            "is_signal_tracker": True,
            "pnl_is_money": False,
            "total_trades": 1,
            "open_count": 1,
            "daily_pnl": -80,
        },
    )

    assert result["rows"] == []
    assert result["feedRows"] == []
    assert any("No final trade signals yet" in html for html in result["htmlWrites"])
    assert result["summary"]["total_trades"] == 0
    assert result["summary"]["open_count"] == 0
    assert result["summary"]["daily_pnl"] == 0


@needs_node
def test_a_resignal_after_an_earlier_exit_on_the_same_candle_stays_actionable(tmp_path):
    symbol = "MIDCPNIFTY26SEP14500PE"
    instruments = {"MIDCPNIFTY": {"trade_candidates": [
        _snapshot_row("MIDCPNIFTY", symbol, "EXIT", candle="11:00:00",
                      entered="11:01:36", exited="11:02:37", pnl=-344.0),
        _snapshot_row("MIDCPNIFTY", symbol, "ENTRY", candle="11:00:00", entered="11:08:12"),
    ]}}
    result = _render(tmp_path, instruments)

    assert sorted(row["status"] for row in result["rows"]) == ["EXIT_SIGNAL", "SIGNAL"]
    assert len(result["feedRows"]) == 1
    assert 'data-action="execute-signal"' in result["feedRows"][0]


@needs_node
def test_an_exit_from_an_earlier_lifecycle_does_not_hide_a_new_signal(tmp_path):
    instruments = {"NIFTY": {"trade_candidates": [
        _snapshot_row("NIFTY", "NIFTY2691523450CE", "EXIT", candle="09:05:00",
                      entered="09:06:10", exited="09:07:40"),
        _snapshot_row("NIFTY", "NIFTY2691523450CE", "ENTRY", candle="09:15:00"),
    ]}}
    result = _render(tmp_path, instruments)

    open_rows = [row for row in result["rows"] if row["status"] == "SIGNAL"]
    assert [row["lifecycle_id"] for row in open_rows] == [
        "NIFTY|5min|LONG|2026-09-15T09:15:00||NIFTY2691523450CE"
    ]
    assert sorted(row["status"] for row in result["rows"]) == ["EXIT_SIGNAL", "SIGNAL"]


@needs_node
def test_rows_without_a_lifecycle_id_pair_on_the_same_identity_as_the_scanner(tmp_path):
    cut = {"NIFTY": {"trade_candidates": [
        _snapshot_row("NIFTY", "NIFTY2691523450CE", "ENTRY", lifecycle=False),
        _snapshot_row("NIFTY", "NIFTY2691523450CE", "EXIT", lifecycle=False),
    ]}}
    assert [row["status"] for row in _render(tmp_path, cut)["rows"]] == ["EXIT_SIGNAL"]

    # Parallel lanes on the same candle and contract are separate positions
    # (Scanner._candidate_lifecycle_key); one lane's exit must not hide the other.
    parallel = {"NIFTY": {"trade_candidates": [
        _snapshot_row("NIFTY", "NIFTY2691523450CE", "ENTRY", lane="QUAD", lifecycle=False),
        _snapshot_row("NIFTY", "NIFTY2691523450CE", "EXIT", lane="RAW", lifecycle=False),
    ]}}
    assert sorted(row["status"] for row in _render(tmp_path, parallel)["rows"]) == [
        "EXIT_SIGNAL", "SIGNAL",
    ]


def _builder_scanner(session_rows):
    return SimpleNamespace(
        mode="REAL",
        backtest_days=1,
        scan_count=1,
        simulation_id=1,
        inst_pref="AUTO",
        active_indices=["NIFTY"],
        _session_trade_candidates={"NIFTY": {}},
        _session_candidate_day="2026-09-15",
        trades=SimpleNamespace(
            get_dashboard_payload=lambda **kwargs: {
                "open": [], "closed": [], "signals": [], "summary": {}, "analytics": {},
            }
        ),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-09-15",
        _loaded_session_is_current_day=lambda: True,
        _get_session_trade_candidates=lambda instrument: session_rows,
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: dict(candidate),
    )


def test_the_panel_payload_keeps_a_resignal_beside_the_exit_it_followed():
    def row(life, action, pnl, entered, exited="", reason=""):
        return {
            "id": f"SESSION|{life}", "lifecycle_id": life, "action": action, "inst_type": "OPT",
            "pnl": pnl, "exit_reason": reason, "entry_timestamp": f"2026-09-15T{entered}",
            "exit_timestamp": f"2026-09-15T{exited}" if exited else "",
            "accepted_by_gate": True,
        }

    session_rows = [
        row("nifty", "ENTRY", -1701.75, "09:17:05"),
        row("nifty", "EXIT", -258.75, "09:17:05", "09:17:53", "EARLY_LOSS_CUT"),
        row("midcp", "EXIT", -344.0, "11:01:36", "11:02:37", "REPAINT_ABORT"),
        row("midcp", "ENTRY", -62.0, "11:08:12"),
    ]
    payload = ScannerTradePayloadBuilder(_builder_scanner(session_rows)).build()

    assert sorted((r["action"], r["lifecycle_id"]) for r in payload["signals"]) == [
        ("ENTRY", "midcp"), ("EXIT", "midcp"), ("EXIT", "nifty"),
    ]
    assert payload["summary"]["daily_pnl"] == -602.75
    assert payload["summary"]["pnl_is_money"] is False
    assert payload["session_closed_total"] == 2
    assert payload["session_open_total"] == 1


def test_panel_payload_excludes_trade_manager_refusal_from_signal_lane():
    refused = {
        "id": "SESSION|sensex-refused",
        "lifecycle_id": "sensex-refused",
        "instrument": "SENSEX",
        "timeframe": "5min",
        "direction": "LONG",
        "action": "ENTRY",
        "inst_type": "OPT",
        "status": "REJECTED - TRADE MANAGER",
        "accepted_by_gate": False,
        "watch_only": False,
        "entry_timestamp": "2026-09-30T09:20:34",
        "pnl": 0.0,
    }

    payload = ScannerTradePayloadBuilder(_builder_scanner([refused])).build()

    assert payload["signals"] == []
    assert payload["session_rows_total"] == 0
    assert payload["summary"]["total_trades"] == 0


def test_an_entry_and_its_exit_both_survive_the_session_ledger(tmp_path):
    ledger = object.__new__(DatabaseManager)
    ledger._init_db(str(tmp_path / "ledger.db"))
    shared_id = "SESSION|MIDCPNIFTY|15min|ENTRY|SHORT|2026-09-15T11:00:00"
    exit_row = {
        "id": shared_id, "action": "EXIT", "instrument": "MIDCPNIFTY", "timeframe": "15min",
        "direction": "SHORT", "signal_timestamp": "2026-09-15T11:00:00",
        "timestamp": "2026-09-15T11:08:12", "exit_timestamp": "2026-09-15T11:08:17",
        "exit_reason": "LIVE_REVERSAL_EXIT",
    }
    entry_row = {**exit_row, "action": "ENTRY", "exit_timestamp": "", "exit_reason": ""}
    try:
        # Book order that day: the exit's key predated the re-signal's entry key, so the
        # entry was written last and replaced the exit.
        ledger.save_session_signals("2026-09-15", [exit_row, entry_row])
        restored = ledger.load_session_signals("2026-09-15")
    finally:
        ledger.conn.close()

    assert sorted(row["action"] for row in restored) == ["ENTRY", "EXIT"]
    assert {row["id"] for row in restored} == {shared_id}, "payload ids feed the dashboard"


LIFECYCLE = "NIFTY|5min|LONG|2026-09-15T09:15:00||NIFTY2691523450CE"


def _candidate(action="ENTRY", lifecycle=LIFECYCLE, entered=(9, 17, 5), exited=(9, 17, 53)):
    row = TradeCandidate(
        instrument="NIFTY", direction="LONG", price=88.45, stop=84.0275, target=95.08,
        lots=1, lot_size=65, grade="A+", confidence=0.96, timeframe="5min",
        inst_type="OPT", option_type="CE", atm_strike=23450.0, multiplier=0.5,
        trading_symbol="NIFTY2691523450CE", symbol_token="0",
        signal_timestamp=datetime(2026, 9, 15, 9, 15),
        action=action,
        status="EXIT SIGNAL" if action == "EXIT" else "TRADE SIGNAL",
    )
    row.accepted_by_gate = True
    row.watch_only = False
    row.lifecycle_id = lifecycle
    row.trade_id = "SESSION|NIFTY|5min|ENTRY|LONG|2026-09-15T09:15:00"
    row.entry_timestamp = datetime(2026, 9, 15, *entered)
    if action == "EXIT":
        row.exit_timestamp = datetime(2026, 9, 15, *exited)
    return row


def _manual_scanner(session_rows, latest_rows=()):
    """A REAL/manual scanner whose first post-lookup step is a probe, not an order."""
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.auto_mode = False
    sc.trades = SimpleNamespace(open_trades={})
    sc._session_trade_candidates = {"NIFTY": {str(i): row for i, row in enumerate(session_rows)}}
    sc._latest_trade_candidates = {"NIFTY": list(latest_rows)} if latest_rows else {}
    reached = []

    def probe(candidate):
        reached.append(candidate)
        return False, "probe"

    async def publish():
        return None

    sc._enforce_strict_instrument_pref_on_candidate = probe
    sc._strict_instrument_pref = lambda: "OPT"
    sc._remember_trade_candidates = lambda rows: None
    sc._publish_trade_payload_now = publish
    return sc, reached


def test_a_cut_signal_cannot_be_executed_from_a_stale_dashboard_row():
    sc, reached = _manual_scanner([_candidate("ENTRY"), _candidate("EXIT")])

    out = asyncio.run(sc.execute_manual_signal(lifecycle_id=LIFECYCLE))

    assert out == {"status": "blocked", "reason": "signal_already_exited"}
    assert reached == [], "a cut lifecycle reached order construction"


def test_an_exit_still_in_the_latest_scan_blocks_too():
    sc, reached = _manual_scanner([_candidate("ENTRY")], latest_rows=[_candidate("EXIT")])

    out = asyncio.run(sc.execute_manual_signal(lifecycle_id=LIFECYCLE))

    assert out["reason"] == "signal_already_exited"
    assert reached == []


def test_a_live_signal_and_a_new_lifecycle_still_reach_execution():
    sc, reached = _manual_scanner([_candidate("ENTRY")])
    assert asyncio.run(sc.execute_manual_signal(lifecycle_id=LIFECYCLE))["reason"] == "instrument_pref_probe"
    assert len(reached) == 1

    earlier = "NIFTY|5min|LONG|2026-09-15T09:05:00||NIFTY2691523450CE"
    sc, reached = _manual_scanner([_candidate("EXIT", lifecycle=earlier), _candidate("ENTRY")])
    assert asyncio.run(sc.execute_manual_signal(lifecycle_id=LIFECYCLE))["reason"] == "instrument_pref_probe"
    assert len(reached) == 1


def test_a_resignal_after_an_earlier_exit_on_the_same_candle_can_be_executed():
    aborted = _candidate("EXIT", entered=(11, 1, 36), exited=(11, 2, 37))
    resignal = _candidate("ENTRY", entered=(11, 8, 12))
    sc, reached = _manual_scanner([aborted, resignal])

    out = asyncio.run(sc.execute_manual_signal(lifecycle_id=LIFECYCLE))

    assert out["reason"] == "instrument_pref_probe", "an older exit blocked a live re-signal"
    assert reached == [resignal]
