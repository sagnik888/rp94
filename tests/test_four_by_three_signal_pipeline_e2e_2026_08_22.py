"""Deterministic 4-index × 3-timeframe signal-pipeline regression.

This deliberately stops at MANUAL acceptance: it exercises the same coordinator and
all pre-entry state transitions without ever constructing or calling a broker adapter.
The business-policy gates are deterministic pass-throughs here so the test isolates the
contract under audit: lane identity -> virgin filter -> stabilization -> acceptance.
"""

import json
from collections import Counter
from datetime import datetime, time as clock_time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import scanner as scanner_module
from engine.multi_timeframe import MultiTimeframeEngine
from engine.signal_processor import SignalProcessor, TradeCandidate
from engine.ut_bot_core import UTBotSignal
from scanner import Scanner


_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "instruments.json"
INDICES = tuple(json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))["indices"])
TIMEFRAMES = ("1min", "5min", "15min")
SIGNAL_AT = datetime(2026, 8, 21, 9, 59, 30)
SESSION_NOW = datetime(2026, 8, 21, 10, 0, 0)


def _frame(timeframe: str) -> pd.DataFrame:
    freq = {"1min": "1min", "5min": "5min", "15min": "15min"}[timeframe]
    index = pd.date_range("2026-08-21 09:15", periods=20, freq=freq)
    values = [100.0 + i * 0.25 for i in range(20)]
    return pd.DataFrame(
        {
            "open": values,
            "high": [value + 1.0 for value in values],
            "low": [value - 1.0 for value in values],
            "close": values,
            "volume": [3000.0] * 20,
        },
        index=index,
    )


class _LaneEngine:
    signal_mode = "realtime"

    def __init__(self, instrument: str, timeframe: str):
        self.instrument = instrument
        self.timeframe = timeframe
        self.calls = 0

    def process(self, **_kwargs):
        self.calls += 1
        signal = UTBotSignal(
            timestamp=SIGNAL_AT,
            signal_type="BUY",
            price=100.0,
            trailing_stop=95.0,
            stop_distance=5.0,
            atr_value=2.0,
            adx_value=30.0,
            position_state=1,
            bar_index=19,
            instrument=self.instrument,
            timeframe=self.timeframe,
            source_engine="test_lane",
        )
        return {
            "signals": [signal],
            "state": {"position": 1, "is_trending": True},
        }


def _candidate(signal: UTBotSignal) -> TradeCandidate:
    candidate = TradeCandidate(
        instrument=signal.instrument,
        direction="LONG",
        price=signal.price,
        stop=signal.trailing_stop,
        target=110.0,
        lots=1,
        lot_size=1,
        grade="A+",
        confidence=0.99,
        timeframe=signal.timeframe,
        inst_type="FUT",
        option_type="",
        atm_strike=0.0,
        multiplier=1.0,
        trading_symbol=f"{signal.instrument}-{signal.timeframe}",
        symbol_token=f"{signal.instrument}-{signal.timeframe}",
        rr=2.0,
        score=0.99,
        reasons=["deterministic 4x3 pipeline probe"],
        signal_timestamp=signal.timestamp,
    )
    candidate.source_signal_type = signal.signal_type
    candidate.source_engine = signal.source_engine
    candidate.quote_verified = True
    candidate.quote_source = "deterministic_test"
    candidate.market_regime = "TRENDING_UP"
    candidate.intelligence_score = 0.75
    candidate.adx_value = 30.0
    # A live 1-minute entry is only admissible through the spike lane.  Preserve that
    # production meaning even though the unrelated volume-quality gate is stubbed.
    candidate.volume_spike_entry = signal.timeframe == "1min"
    candidate.volume_ratio = 3.0 if signal.timeframe == "1min" else 1.2
    return candidate


def _build_mtf():
    mtf = MultiTimeframeEngine({"signal_mode": "realtime"})
    lane_engines = {}
    for instrument in INDICES:
        for timeframe in TIMEFRAMES:
            key = f"{instrument}_{timeframe}"
            lane_engines[key] = _LaneEngine(instrument, timeframe)
    mtf.engines = lane_engines

    frames = {timeframe: _frame(timeframe) for timeframe in TIMEFRAMES}
    for instrument in INDICES:
        for timeframe in TIMEFRAMES:
            mtf.update_data(instrument, timeframe, frames[timeframe])
    return mtf, lane_engines, frames


def _result_lanes(results):
    lanes = {}
    for instrument in INDICES:
        mtf_result = results[instrument]
        for timeframe in TIMEFRAMES:
            result = getattr(mtf_result, f"results_{timeframe}")
            lanes[(instrument, timeframe)] = (
                result,
                mtf_result.timeframe_metadata[timeframe],
                result["signals"][-1],
            )
    return lanes


def _scanner_harness(mtf, initial_lanes, monkeypatch):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False  # acceptance only; never touch an order adapter
    scanner.scan_count = 1
    scanner.mtf = mtf
    scanner.data = SimpleNamespace(is_market_open=lambda: True)
    scanner.trades = SimpleNamespace(open_trades={})
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 100.0)
    scanner.latest_regimes = {instrument: "TRENDING_UP" for instrument in INDICES}

    scanner._pending_live_signals = {}
    scanner._signal_preflight_cache = {}
    scanner._last_signal_time = {}
    scanner._last_signal_identity = {}
    scanner._last_live_signal_candle_time = {}
    scanner._candidate_process_cache = {}
    scanner._raw_utbot_activity_keys = set()
    scanner._latest_trade_candidates = {}
    scanner._cached_results = {}
    scanner._cached_result_metadata = {}

    for (instrument, timeframe), (result, metadata, _signal) in initial_lanes.items():
        key = f"{instrument}_{timeframe}"
        scanner._cached_results[key] = result
        scanner._cached_result_metadata[key] = {
            **dict(metadata),
            "result_object": result,
        }

    diagnostics = Counter()
    accepted_log = []
    remembered = []
    rtracker = []
    scanner._diag_inc = lambda section, key, *_a, **_k: diagnostics.update([f"{section}.{key}"])
    scanner._live_clock_is_sane = lambda: True
    scanner._session_squareoff_clock = lambda: SESSION_NOW
    scanner._configured_session_time = lambda _name, _default: clock_time(15, 25)
    scanner._candidate_source_is_intrabar = lambda _candidate, _now: True
    scanner._signal_fire_mode = lambda: "INTRACANDLE"
    scanner._entry_cutoff_allows_candidate = lambda _candidate, _now: True
    scanner._live_signal_stabilization_requirements = lambda _candidate: (0.0, 1, False)
    scanner._save_warm_memory = lambda *a, **k: False
    scanner._apply_stabilization_preflight = lambda *a, **k: None
    scanner._maybe_prepare_stabilization_preflight = lambda *a, **k: None
    scanner._forget_signal_display_rows = lambda *a, **k: None
    scanner._drop_missed_reentry_seed = lambda *a, **k: None
    scanner._stabilization_repaint_override_context = lambda *a, **k: None
    scanner._rtracker_record_candidate = lambda candidate, **kw: rtracker.append(
        (candidate.instrument, candidate.timeframe, kw.get("decision"), kw.get("source"))
    )
    scanner.log_event = lambda *a, **k: None
    scanner.log_signal_decision_once = lambda *a, **k: None

    # Downstream business-policy gates are intentionally transparent.  The matrix is
    # offered one matured lane at a time, so this does not bypass a selection conflict;
    # it isolates whether lane state leaks across an instrument/timeframe boundary.
    scanner._ingest_wave_heavy_trend_votes = lambda *_a, **_k: None
    scanner._apply_multi_signal_mixed_book = lambda candidates: list(candidates)
    scanner._apply_strict_instrument_pref_to_entries = lambda candidates: list(candidates)
    scanner._resolve_unbuilt_opt_entries = lambda candidates: list(candidates)
    scanner._one_way_cascade_context = lambda *a, **k: None
    scanner._market_wide_reversal_context = lambda *a, **k: None
    scanner._evaluate_market_ltp_shock = lambda *a, **k: None
    scanner._passes_candidate_grade_preference = lambda _candidate: (True, "")
    scanner._can_trade_instrument = lambda *a, **k: (True, "")
    scanner._passes_live_trade_ready_gate = lambda _candidate: True
    scanner._candidate_quote_ready_for_lifecycle = lambda _candidate: True
    scanner._absorb_same_direction_session_refresh = lambda _candidate: False
    scanner._prepare_candidate_for_concurrency = lambda _candidate: True
    scanner._bucket_capacity_allows = lambda _candidate: True
    scanner._passes_correlated_index_guard = lambda _candidate, _direction: True
    scanner._ensure_trade_like_initial_stop = lambda _candidate: None
    scanner._candidate_entry_mark_refusal = lambda _candidate: ""
    scanner._entry_policy_allows = lambda *_args, **_kwargs: (True, "")
    scanner._close_superseded_session_entries = lambda _candidates: None
    scanner._mark_candidate_accepted_time = lambda _candidate, _at=None: None
    scanner._remember_trade_candidates = lambda candidates: remembered.extend(candidates)
    scanner._record_accepted_entry = lambda candidate, trade=None: accepted_log.append(
        (candidate.instrument, candidate.timeframe)
    )
    scanner._notify = lambda *a, **k: None

    async def _publish():
        return None

    scanner._publish_trade_payload_now = _publish
    monkeypatch.setattr(
        scanner_module,
        "get_notification_manager",
        lambda *_a, **_k: SimpleNamespace(send_trade_notification=lambda **_kw: None),
    )
    return scanner, diagnostics, accepted_log, remembered, rtracker


@pytest.mark.asyncio
async def test_every_four_by_three_lane_is_virgin_buffered_stabilized_and_accepted(monkeypatch):
    assert len(INDICES) == 4
    assert set(INDICES) == {"NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"}
    mtf, lane_engines, frames = _build_mtf()
    config = {
        "indices": {
            instrument: {"lot_size": 1}
            for instrument in INDICES
        }
    }

    initial_results = mtf.process_all(config, user_lots={instrument: 1 for instrument in INDICES})
    initial_lanes = _result_lanes(initial_results)
    expected_lanes = {(instrument, timeframe) for instrument in INDICES for timeframe in TIMEFRAMES}
    assert set(initial_lanes) == expected_lanes
    assert all(engine.calls == 1 for engine in lane_engines.values())

    scanner, diagnostics, accepted_log, remembered, rtracker = _scanner_harness(
        mtf, initial_lanes, monkeypatch
    )

    # Virgin filtering: use the exact same timestamp and direction on every lane.  The
    # scope key, not accidental timestamp uniqueness, must keep all 12 independent.
    candidates = []
    for (instrument, timeframe), (_result, _metadata, signal) in initial_lanes.items():
        lane_key = f"{instrument}_{timeframe}"
        identity = f"{SIGNAL_AT.isoformat()}|BUY"
        assert SignalProcessor._live_signal_is_new(
            SIGNAL_AT,
            "BUY",
            scanner._last_signal_time.get(lane_key, datetime.min),
            scanner._last_signal_identity.get(lane_key),
        )
        SignalProcessor._remember_live_signal_identity(
            scanner, lane_key, SIGNAL_AT, identity
        )
        candidates.append(_candidate(signal))

    assert set(scanner._last_signal_identity) == {
        f"{instrument}_{timeframe}" for instrument, timeframe in expected_lanes
    }
    assert all(value == {f"{SIGNAL_AT.isoformat()}|BUY"} for value in scanner._last_signal_identity.values())
    assert all(
        not SignalProcessor._live_signal_is_new(
            SIGNAL_AT,
            "BUY",
            scanner._last_signal_time[f"{instrument}_{timeframe}"],
            scanner._last_signal_identity[f"{instrument}_{timeframe}"],
        )
        for instrument, timeframe in expected_lanes
    ), "a duplicate scan reopened at least one supposedly consumed lane identity"

    # First coordinator pass buffers all lanes.  Every cached result predates its own
    # buffered-at cutoff, so none may mature yet.
    assert await scanner._coordinate_and_execute(candidates) == []
    expected_buffer_keys = {
        f"{instrument}_{timeframe}_LONG"
        for instrument, timeframe in expected_lanes
    }
    assert set(scanner._pending_live_signals) == expected_buffer_keys
    assert diagnostics["stabilization.buffered"] == 12
    assert diagnostics["stabilization.matured"] == 0
    assert all(item["stable_scans"] == 0 for item in scanner._pending_live_signals.values())

    # The buffer armed a forced pass in every exact MTF lane. Re-run unchanged data;
    # each engine must execute again rather than returning its pre-buffer cached arrow.
    fresh_lanes = {}
    for instrument in INDICES:
        for timeframe in TIMEFRAMES:
            mtf.update_data(instrument, timeframe, frames[timeframe])
        refreshed = mtf.process_instrument(instrument)
        for timeframe in TIMEFRAMES:
            result = getattr(refreshed, f"results_{timeframe}")
            metadata = refreshed.timeframe_metadata[timeframe]
            cutoff = scanner._pending_live_signals[
                f"{instrument}_{timeframe}_LONG"
            ]["result_required_after_epoch"]
            assert metadata["status"] == "recomputed"
            assert metadata["result"]["computed_at_epoch"] > cutoff
            fresh_lanes[(instrument, timeframe)] = (result, metadata)
    assert all(engine.calls == 2 for engine in lane_engines.values())

    # Expose one fresh lane at a time. The other eleven remain UNKNOWN and must stay in
    # their own buffers; exactly the exposed lane is allowed to mature and be accepted.
    accepted = []
    for ordinal, lane in enumerate(sorted(expected_lanes), start=1):
        instrument, timeframe = lane
        lane_key = f"{instrument}_{timeframe}"
        buffer_key = f"{lane_key}_LONG"
        result, metadata = fresh_lanes[lane]
        scanner._cached_results[lane_key] = result
        scanner._cached_result_metadata[lane_key] = {
            **dict(metadata),
            "result_object": result,
        }

        before = set(scanner._pending_live_signals)
        wave = await scanner._coordinate_and_execute([])
        after = set(scanner._pending_live_signals)

        assert [(item.instrument, item.timeframe) for item in wave] == [lane]
        assert before - after == {buffer_key}
        assert len(after) == 12 - ordinal
        accepted.extend(wave)

    accepted_lanes = [(candidate.instrument, candidate.timeframe) for candidate in accepted]
    assert len(accepted_lanes) == 12
    assert set(accepted_lanes) == expected_lanes
    assert len(set(accepted_lanes)) == 12
    assert accepted_log == accepted_lanes
    assert [(candidate.instrument, candidate.timeframe) for candidate in remembered] == accepted_lanes
    assert scanner._pending_live_signals == {}
    assert diagnostics["stabilization.matured"] == 12
    assert {
        (instrument, timeframe)
        for instrument, timeframe, decision, source in rtracker
        if decision == "STABILIZED" and source == "stabilization_gate"
    } == expected_lanes
