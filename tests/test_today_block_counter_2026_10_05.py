"""A candle-close wait is not a refusal, and a top-N list is not a total."""
import ast
from collections import deque
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pandas as pd
import pytest
import scanner as scanner_module
import engine.signal_processor as processor_module
from scanner import Scanner
from tests.test_ut_smart_live_path_2026_09_26 import live


def _scanner():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.is_warmup = False
    scanner._diagnostics = {"rejects": {}, "exit_reasons": {}}
    scanner._diagnostic_unique_events = set()
    scanner._session_trade_candidates = {}
    scanner.activity_log = deque(maxlen=200)
    scanner.active_indices = []
    scanner.latest_results = {}
    scanner.scan_count = 0
    scanner.warm_memory = None
    scanner.data = SimpleNamespace(get_source_health=lambda: {})
    scanner.candles = SimpleNamespace(get_candles=lambda *_args: pd.DataFrame())
    scanner._scanner_freshness_payload = lambda: {}
    scanner._recalculation_status_payload = lambda: {}
    scanner._get_system_metrics_payload = lambda: {}
    scanner._system_performance_payload = lambda: {}
    return scanner


def test_partial_candle_wait_is_persisted_without_counting_as_reject(monkeypatch):
    scanner = _scanner()
    saved = []
    monkeypatch.setattr(scanner_module.db, "save_signal_decision", lambda row: saved.append(row))
    message = "Signal waiting: NIFTY BUY 5min @ 25000.00 - partial candle deferred until 10:00:00"
    scanner._persist_rejected_signal_event(message)
    assert len(saved) == 1
    assert saved[0]["status"] == "WAITING"
    assert scanner._diagnostics["rejects"] == {}


def test_restart_does_not_turn_legacy_candle_wait_into_reject(monkeypatch):
    scanner = _scanner()
    rows = [
        {"id": "wait-old", "status": "MISSED", "reason": "partial candle deferred until 10:00:00",
         "message": "Signal waiting: NIFTY BUY 5min @ 25000.00 - partial candle deferred until 10:00:00"},
        {"id": "reject-real", "status": "REJECTED", "reason": "OPT circuit breaker is active",
         "message": "Signal rejected: NIFTY BUY 5min @ 25000.00 - OPT circuit breaker is active"},
        {"id": "missed-real", "status": "MISSED", "reason": "stale buffered signal",
         "message": "Discarded signal: NIFTY BUY 5min - stale buffered signal"},
    ]
    monkeypatch.setattr(scanner_module.db, "load_signal_decisions", lambda _day: rows)
    scanner._rehydrate_live_diagnostics_from_session("2026-10-05")
    assert scanner._diagnostics["rejects"] == {
        "OPT circuit breaker is active": 1,
        "stale buffered signal": 1,
    }


def test_full_reject_total_survives_top_twelve_display_limit():
    scanner = _scanner()
    scanner._diagnostics["rejects"] = {f"Reject reason {n}": n for n in range(1, 15)}
    payload = scanner._get_diagnostics_payload()
    assert len(payload["rejects"]) == 12
    assert sum(payload["rejects"].values()) == 102
    assert payload["reject_summary"]["total"] == 105
    assert payload["reject_summary"]["reason_count"] == 14


def test_restored_decision_is_not_counted_again_on_later_identical_emission(monkeypatch):
    first = _scanner()
    saved = []
    monkeypatch.setattr(scanner_module.db, "save_signal_decision", lambda row: saved.append(row))
    message = "Signal rejected: NIFTY BUY 5min @ 25000.00 - OPT circuit breaker is active"
    first._persist_rejected_signal_event(message)
    assert len(saved) == 1
    restored = _scanner()
    monkeypatch.setattr(scanner_module.db, "load_signal_decisions", lambda _day: saved[:1])
    restored._rehydrate_live_diagnostics_from_session(saved[0]["date"])
    assert restored._diagnostics["rejects"] == {"OPT circuit breaker is active": 1}
    restored._persist_rejected_signal_event(message)
    assert saved[0]["id"] == saved[1]["id"]
    assert restored._diagnostics["rejects"] == {"OPT circuit breaker is active": 1}


@pytest.mark.parametrize("suppress_tape", [False, True])
def test_real_refusal_counts_once_even_when_same_signal_is_rechecked(live, monkeypatch, suppress_tape):
    h = live(tf="5min")
    h.sc._diagnostics = {"rejects": {}}
    h.sc._diagnostic_unique_events = set()
    h.sc.activity_log = deque(maxlen=200)
    h.sc.log_signal_decision_once = Scanner.log_signal_decision_once.__get__(h.sc, Scanner)
    h.sc.log_event = Scanner.log_event.__get__(h.sc, Scanner)
    h.sc.trades.can_open_trade = lambda: (False, "test max trades")
    if suppress_tape:
        h.processor._suppress_1min_no_spike_tape = lambda *_args, **_kwargs: True
    saved = []
    monkeypatch.setattr(scanner_module.db, "save_signal_decision", lambda row: saved.append(row))
    for _ in range(2):
        h.sc._last_signal_time = {"NIFTY_5min": h.sig.timestamp - timedelta(seconds=1)}
        h.sc._live_signal_consumed_identities = {}
        result = h.processor.process_best_signal(*h.args)
        assert len(result) == 1 and result[0].status == "REJECTED - Max Trades"
    assert len(saved) == (0 if suppress_tape else 1)
    assert h.sc._diagnostics["rejects"] == {"Max overall trades reached or capital depleted": 1}


NODE = shutil.which("node") or "C:/Program Files/nodejs/node.exe"
ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function updateDiagnosticsPanel(diagnostics) {');
const end = source.indexOf('\nfunction updateAnalyticsStrip(', start);
if (start < 0 || end < 0) throw Error('Production diagnostics renderer not found');
const nodes = new Map();
const document = {getElementById(id) {
  if (!nodes.has(id)) nodes.set(id, {style: {}, textContent: '', innerHTML: '', innerText: ''});
  return nodes.get(id);
}};
const context = {
  document, state: {latestData: {mode: 'REAL'}}, updateHardwarePanel() {},
  setText(id, value) {document.getElementById(id).textContent = value;},
  escapeHtml(value) {return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;');},
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
context.updateDiagnosticsPanel(input);
process.stdout.write(JSON.stringify(Object.fromEntries([...nodes.entries()].map(([id, node]) => [id, node.textContent]))));
"""


def _render_diagnostics(payload, script=None):
    result = subprocess.run(
        [NODE, "-e", DIAGNOSTICS_DRIVER, str(script or ROOT / "dashboard/static/js/app.js")],
        input=json.dumps(payload), capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_shipped_renderer_uses_complete_total_and_separate_waits():
    output = _render_diagnostics({
        "rejects": {f"Reject reason {n}": n for n in range(3, 15)},
        "reject_summary": {"total": 105, "reason_count": 14},
        "waiting_summary": {"total": 93},
    })
    assert output["diagRejectTotal"] == "105"
    assert output["diagRejectTotalLabel"] == "Total Blocks"
    assert output["diagRejectDiversity"] == "14 reasons"
    assert output["diagWaitingTotal"] == "93 waits; not rejected"


def test_shipped_renderer_zeros_rejects_and_marks_legacy_waits_as_shown():
    output = _render_diagnostics({"rejects": {"partial candle deferred until 10:00:00": 43}})
    assert output["diagRejectTotal"] == "0"
    assert output["diagRejectTotalLabel"] == "Shown Blocks"
    assert output["diagWaitingTotal"] == "43 shown waits; not rejected"


def test_shipped_renderer_retains_legacy_real_blocks_without_summary():
    output = _render_diagnostics({"rejects": {"OPT circuit breaker is active": 2}})
    assert output["diagRejectTotal"] == "2"
    assert output["diagRejectTotalLabel"] == "Shown Blocks"
    assert output["diagRejectDiversity"] == "1 reasons"


@pytest.mark.parametrize("missing", [None, "", False])
def test_incomplete_summary_falls_back_to_available_counts(missing):
    output = _render_diagnostics({
        "rejects": {"real reject": 2}, "waiting": {"candle close": 3},
        "reject_summary": {"total": missing, "reason_count": missing},
        "waiting_summary": {"total": missing},
    })
    assert output["diagRejectTotalLabel"] == "Shown Blocks"
    assert output["diagRejectTotal"] == "2"
    assert output["diagRejectDiversity"] == "1 reasons"
    assert output["diagWaitingTotal"] == "3 shown waits; not rejected"


def _daily_report(scanner, tmp_path, monkeypatch):
    legacy = {
        "id": "wait-legacy", "status": "MISSED", "instrument": "NIFTY", "timeframe": "5min", "direction": "BUY",
        "timestamp": "2026-10-05T10:00:00", "reason": "partial candle deferred until 10:05:00",
        "message": "Signal waiting: NIFTY BUY 5min @ 25000.00 - partial candle deferred until 10:05:00",
    }
    rows = [legacy, {
        "id": "wait-new", "status": "WAITING", "instrument": "NIFTY", "timeframe": "5min", "direction": "BUY",
        "timestamp": "2026-10-05T10:01:00", "reason": "partial candle deferred until 10:05:00",
        "message": "Signal waiting: NIFTY BUY 5min @ 25002.00 - partial candle deferred until 10:05:00",
    }, {
        "id": "reject-real", "status": "REJECTED", "instrument": "NIFTY", "timeframe": "5min", "direction": "BUY",
        "timestamp": "2026-10-05T10:02:00", "reason": "OPT circuit breaker is active",
        "message": "Signal rejected: NIFTY BUY 5min @ 25004.00 - OPT circuit breaker is active",
    }]
    scanner.activity_log.append({"time": "10:03:00", "msg": "Signal waiting: NIFTY 5min deferred until candle close."})
    monkeypatch.setattr(scanner_module.db, "load_signal_decisions", lambda _day: rows)
    monkeypatch.setattr(scanner_module.db, "load_session_signals", lambda _day: [])
    monkeypatch.chdir(tmp_path)
    candle_dir = tmp_path / "data_store/candles"
    candle_dir.mkdir(parents=True)
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-10-05T10:00:00", periods=35, freq="min"),
                          "open": 25000.0, "high": 25010.0, "low": 24995.0, "close": 25005.0})
    frame.to_csv(candle_dir / "NIFTY_1min.csv", index=False)
    report = scanner.generate_daily_signal_report("2026-10-05")
    assert legacy["status"] == "MISSED", "Report normalization must not mutate stored rows"
    return report


def test_daily_report_separates_waits_without_erasing_decisions(tmp_path, monkeypatch):
    report = _daily_report(_scanner(), tmp_path, monkeypatch)
    assert report["summary"]["by_status"] == {"REJECTED": 1, "WAITING": 3}
    assert report["summary"]["missed_or_rejected"] == 1
    assert report["summary"]["waiting"] == 3
    assert report["summary"]["decision_events"] == 4
    assert len(report["decisions"]) == 4
    assert report["summary"]["top_reasons"] == {"OPT circuit breaker is active": 1}


def test_daily_report_never_scores_waits_as_rejected_opportunities(tmp_path, monkeypatch):
    report = _daily_report(_scanner(), tmp_path, monkeypatch)
    assert report["replay_scored"]["scored_clusters"] == 1
    assert report["replay_scored"]["unique_rejected_clusters"] == 1


@pytest.mark.parametrize("mode,native", [("quad_prime_s1", "quad_prime"), ("ut_smart_s1", "ut_smart")])
@pytest.mark.parametrize("suppress_tape", [False, True])
def test_combined_owners_share_reason_but_not_diagnostic_identity(monkeypatch, mode, native, suppress_tape):
    """Run the exact production emitter, with identical signals from two owners."""
    scanner = _scanner()
    saved = []
    monkeypatch.setattr(scanner_module.db, "save_signal_decision", lambda row: saved.append(row))
    tree = ast.parse(Path(processor_module.__file__).read_text(encoding="utf-8"))
    emitter = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_emit_signal_decision")
    namespace = dict(vars(processor_module), scanner=scanner, instrument="NIFTY",
                     settings=SimpleNamespace(scanner_engine=mode),
                     self=SimpleNamespace(
                         _is_s12_native_burst=processor_module.SignalProcessor._is_s12_native_burst,
                         _suppress_1min_no_spike_tape=lambda *_args, **_kwargs: suppress_tape))
    exec(compile(ast.Module(body=[emitter], type_ignores=[]), processor_module.__file__, "exec"), namespace)
    for source in (native, "s1"):
        signal = SimpleNamespace(source_engine=source, signal_type="BUY", price=25000.0,
                                 timestamp=datetime(2026, 10, 5, 10, 0), volume_spike_entry=False)
        for _ in range(2):
            namespace["_emit_signal_decision"]({"sig": signal, "tf": "5min"},
                                               "Max overall trades reached or capital depleted", "REJECTED - Max Trades")
    assert scanner._diagnostics["rejects"] == {"Max overall trades reached or capital depleted": 2}
    assert len(saved) == (0 if suppress_tape else 2)
    if not suppress_tape:
        assert saved[0]["id"] != saved[1]["id"]
