from datetime import datetime
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd

from engine.multi_timeframe import MultiTimeframeEngine
from scanner import Scanner


ROOT = Path(__file__).resolve().parents[1]
TRADE_TABLE = ROOT / "dashboard" / "static" / "js" / "trade-table.js"
NODE = shutil.which("node")

_RENDER_SIGNAL_TABLE = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const tbody = {
  children: [], querySelectorAll: () => [], querySelector: () => null,
  appendChild(row) { this.children.push(row); },
  insertBefore(row) { this.children.push(row); },
};
const nodes = new Map();
const context = {
  console, window: {}, state: { lastSimId: 1, latestData: {} },
  document: {
    addEventListener() {},
    getElementById(id) {
      if (id === 'tradesBody') return tbody;
      if (!nodes.has(id)) nodes.set(id, { style: {}, textContent: '', className: '' });
      return nodes.get(id);
    },
    createElement() {
      return {
        attrs: {}, className: '', innerHTML: '',
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k]; },
        hasAttribute(k) { return k in this.attrs; },
      };
    },
  },
  setText() {}, setHtml(node, html) { node.innerHTML = html; },
  patchRow(row, html) { row.innerHTML = html; },
  formatPrice: value => String(value),
  baseInstrumentLabel: value => String(value || '').split(/\s+/)[0],
};
vm.createContext(context);
vm.runInContext(source, context);
context.tradeHistoryTotals = trades => ({
  total: (trades.signals || []).length + (trades.open || []).length + (trades.closed || []).length,
  visible: (trades.signals || []).length + (trades.open || []).length + (trades.closed || []).length,
});
context.getTradePageInfo = trades => ({
  page: 1, pageCount: 1, total: (trades.signals || []).length, visible: (trades.signals || []).length,
});
context.renderTradePagination = () => {};
context.updateTradesPanel({
  open: [], closed: [],
  signals: [{
    instrument: 'NIFTY', direction: 'LONG', action: 'ENTRY', status: 'TRADE SIGNAL',
    accepted_by_gate: true, execution_status: 'SIGNAL_ONLY', execution_state: 'NOT_PLACED',
    data_stale: true, data_stale_reason: 'live index analysis timed out',
    signal_timestamp: '2026-09-24T11:40:00', timestamp: '2026-09-24T11:40:12',
    lifecycle_id: 'NIFTY|5min|LONG|2026-09-24T11:40:00', price: 100,
    current_price: 99, stop: 90, target: 120, confidence: 0.9, grade: 'A',
    timeframe: '5min', inst_type: 'FUT', pnl: 0,
  }], summary: {}, meta: { restored_signal_rows: 1 },
}, 1);
process.stdout.write(JSON.stringify(tbody.children.map(row => row.innerHTML)));
"""


def _candidate(*, direction="LONG", timestamp=(11, 40, 12), source=(11, 40, 0), accepted=True):
    row = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction=direction,
        inst_type="OPT",
        option_type="CE" if direction == "LONG" else "PE",
        action="ENTRY",
        signal_timestamp=datetime(2026, 9, 24, *timestamp),
        source_signal_timestamp=datetime(2026, 9, 24, *source),
        entry_timestamp=datetime(2026, 9, 24, 11, 40, 12),
        accepted_by_gate=accepted,
        status="TRADE SIGNAL",
        price=100.0,
        stop=90.0,
        target=120.0,
        trading_symbol="NIFTY2692425000CE",
    )
    return row


def _refresh_scanner(previous):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._active_session_entry_candidates = lambda _instrument: [previous]
    scanner.log_event = lambda *_args, **_kwargs: None
    return scanner


def test_same_source_candle_replay_is_deduped_before_mutable_gates():
    previous = _candidate(accepted=True)
    replay = _candidate(timestamp=(11, 40, 25))
    replay.price = 108.0
    scanner = _refresh_scanner(previous)

    assert scanner._absorb_same_direction_session_refresh(replay) is True
    assert replay._session_refresh_kind == "DEDUPED"
    assert replay.accepted_by_gate is True
    assert replay.price == previous.price == 100.0
    assert replay.lifecycle_id == scanner._candidate_lifecycle_key(previous)

    recorded = []
    scanner._rtracker_record_candidate = lambda _candidate, **kwargs: recorded.append(kwargs)
    scanner._record_absorbed_session_signal(replay)
    assert [row["decision"] for row in recorded] == ["DEDUPED"]
    assert "same_signal_dedup" == recorded[0]["source"]


def test_later_same_direction_candle_refreshes_the_existing_lifecycle():
    previous = _candidate(accepted=True)
    later = _candidate(timestamp=(11, 45, 10), source=(11, 45, 0))
    scanner = _refresh_scanner(previous)

    assert scanner._absorb_same_direction_session_refresh(later) is True
    assert later._session_refresh_kind == "REFRESHED"
    assert later.signal_timestamp == previous.source_signal_timestamp
    assert later.price == previous.price
    assert later.lifecycle_id == scanner._candidate_lifecycle_key(previous)


def test_unaccepted_session_row_cannot_absorb_a_later_entry():
    previous = _candidate(accepted=False)
    later = _candidate(timestamp=(11, 45, 10), source=(11, 45, 0), accepted=False)
    scanner = _refresh_scanner(previous)

    assert scanner._absorb_same_direction_session_refresh(later) is False
    assert later.accepted_by_gate is False
    assert not hasattr(later, "_session_refresh_kind")


def test_opposite_direction_is_left_for_reversal_handling():
    previous = _candidate(accepted=True)
    opposite = _candidate(direction="SHORT", timestamp=(11, 45, 10), source=(11, 45, 0))
    scanner = _refresh_scanner(previous)

    assert scanner._absorb_same_direction_session_refresh(opposite) is False


def test_accepted_lifecycle_survives_live_gate_pruning_and_marks_stale():
    accepted = _candidate(accepted=True)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._session_trade_candidates = {"NIFTY": {"accepted": accepted}}
    scanner._loaded_session_is_current_day = lambda: True
    scanner._candidate_matches_live_session_day = lambda _candidate: False
    scanner._live_entry_data_blocks = {"NIFTY": "live index analysis timed out after 9s"}
    scanner._passes_live_trade_ready_gate = lambda _candidate: (_ for _ in ()).throw(
        AssertionError("accepted lifecycle was revalidated")
    )
    scanner._looks_like_synthetic_option_entry = lambda _candidate: (_ for _ in ()).throw(
        AssertionError("accepted execution snapshot was rebased/pruned")
    )
    persisted = []
    scanner._persist_session_trade_candidates = lambda: persisted.append(True)

    scanner._prune_session_trade_candidates_for_live_gate()

    assert scanner._session_trade_candidates["NIFTY"]["accepted"] is accepted
    assert accepted.accepted_by_gate is True
    assert accepted.data_stale is True
    assert accepted.data_stale_reason == "live index analysis timed out after 9s"
    assert persisted == [True]


def test_accepted_lifecycle_restores_without_replaying_mutable_quality_policy():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._intracandle_signals_enabled = lambda: True
    scanner._candidate_matches_live_session_day = lambda _candidate: False
    scanner._restored_entry_quality_is_current = lambda _candidate: (_ for _ in ()).throw(
        AssertionError("accepted lifecycle was regraded")
    )

    assert scanner._candidate_valid_for_live_restore(_candidate(accepted=True)) is True


def test_unaccepted_candidate_remains_subject_to_live_gate_pruning():
    pending = _candidate(accepted=False)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._session_trade_candidates = {"NIFTY": {"pending": pending}}
    scanner._loaded_session_is_current_day = lambda: True
    scanner._candidate_matches_live_session_day = lambda _candidate: True
    scanner._looks_like_synthetic_option_entry = lambda _candidate: False
    scanner._passes_live_trade_ready_gate = lambda _candidate: False
    scanner._persist_session_trade_candidates = lambda: None

    scanner._prune_session_trade_candidates_for_live_gate()

    assert scanner._session_trade_candidates == {}


@pytest.mark.skipif(NODE is None, reason="node is required to render trade-table.js")
def test_manual_signal_row_says_awaiting_execution_and_shows_stale_badge():
    result = subprocess.run(
        [NODE, "-e", _RENDER_SIGNAL_TABLE, str(TRADE_TABLE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    html = "".join(__import__("json").loads(result.stdout))
    assert "Awaiting execution" in html
    assert "DATA STALE" in html


def test_scan_phase_diagnostics_expose_s1_and_dashboard_costs():
    class _S1Engine:
        signal_source = "s1"
        signal_mode = "confirmed"

        def process(self, **kwargs):
            return {
                "source_engine": "s1",
                "signals": [],
                "state": {"position": 0},
                "trailing_stop": [None] * len(kwargs["df"]),
            }

    frame = pd.DataFrame(
        {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10.0},
        index=pd.date_range("2026-09-24 09:15", periods=30, freq="15min"),
    )
    mtf = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
    mtf.engines = {"NIFTY_15min": _S1Engine()}
    mtf._data_cache = {"NIFTY_15min": frame, "NIFTY_5min": frame.copy()}
    mtf._result_cache = {}
    mtf._data_hash = {}
    mtf.tf_weights = {"1min": 0.15, "5min": 0.4, "15min": 0.45}

    result = mtf.process_instrument("NIFTY")
    assert result.timeframe_metadata["15min"]["signal_source"] == "s1"
    assert result.timeframe_metadata["15min"]["duration_ms"] >= 0.0

    scanner = Scanner.__new__(Scanner)
    scanner._diagnostics = {}
    scanner._record_scan_phase_ms("s1_15min_ms", result.timeframe_metadata["15min"]["duration_ms"], instrument="NIFTY")
    scanner._record_scan_phase_ms("dashboard_trade_payload_ms", 1.25)
    assert scanner._diagnostics["phase_processing_ms"]["NIFTY"]["s1_15min_ms"] >= 0.0
    assert scanner._diagnostics["phase_processing_ms"]["dashboard_trade_payload_ms"] == 1.25
