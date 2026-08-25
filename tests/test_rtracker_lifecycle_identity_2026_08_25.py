from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

from intelligence.rtracker import RegimeTracker


_EXIT_TS = "2026-08-25T10:15:00"


def _exit(lifecycle_id: str, *, timestamp: str = _EXIT_TS, reason: str = "TARGET"):
    return {
        "timestamp": timestamp,
        "instrument": "NIFTY",
        "timeframe": "5m",
        "direction": "LONG",
        "decision": "EXIT",
        "reason": reason,
        "lifecycle_id": lifecycle_id,
        "pnl": 125.0,
    }


def test_exit_is_immutable_within_process(tmp_path):
    tracker = RegimeTracker(tmp_path)
    first = _exit("trade-1")
    assert tracker.record_event(first) is True

    duplicate = _exit("trade-1", timestamp="2026-08-25T10:16:00", reason="STOP")
    duplicate["id"] = "producer-specific-id"
    assert tracker.record_event(duplicate) is False

    rows = tracker.load_events(weeks=1, as_of=datetime(2026, 8, 25, 12, 0))
    assert len(rows) == 1
    assert rows[0]["reason"] == "TARGET"
    assert rows[0]["lifecycle_id"] == "trade-1"
    assert rows[0]["id"] != "producer-specific-id"


def test_exit_is_immutable_after_tracker_restart(tmp_path):
    first_tracker = RegimeTracker(tmp_path)
    assert first_tracker.record_event(_exit("trade-restarted")) is True

    restarted = RegimeTracker(tmp_path)
    duplicate = _exit(
        "trade-restarted",
        timestamp="2026-08-25T10:17:00",
        reason="TRAILING_STOP",
    )
    assert restarted.record_event(duplicate) is False

    rows = restarted.load_events(weeks=1, as_of=datetime(2026, 8, 25, 12, 0))
    assert len(rows) == 1
    assert rows[0]["reason"] == "TARGET"


def test_distinct_lifecycles_have_distinct_terminal_events(tmp_path):
    tracker = RegimeTracker(tmp_path)
    assert tracker.record_event(_exit("trade-a")) is True
    assert tracker.record_event(_exit("trade-b", timestamp="2026-08-25T10:16:00")) is True

    rows = tracker.load_events(weeks=1, as_of=datetime(2026, 8, 25, 12, 0))
    assert {row["lifecycle_id"] for row in rows} == {"trade-a", "trade-b"}
    assert len({row["id"] for row in rows}) == 2


def test_fallback_keeps_simultaneous_lanes_and_contracts_distinct(tmp_path):
    tracker = RegimeTracker(tmp_path)
    base = {
        "timestamp": "2026-08-25T10:15:00",
        "entry_timestamp": "2026-08-25T09:45:00",
        "signal_timestamp": "2026-08-25T09:44:30",
        "instrument": "NIFTY",
        "timeframe": "5min",
        "direction": "LONG",
        "decision": "EXIT",
    }
    lane_a = {**base, "source_lane": "A", "contract": {"trading_symbol": "NIFTY-A"}}
    lane_b = {**base, "source_lane": "B", "contract": {"trading_symbol": "NIFTY-B"}}

    assert tracker.record_event(lane_a) is True
    assert tracker.record_event(lane_b) is True
    rows = tracker.load_events(weeks=1, as_of=datetime(2026, 8, 25, 12, 0))
    assert len(rows) == 2
    assert len({row["lifecycle_id"] for row in rows}) == 2


def test_scanner_lifecycle_keeps_parallel_lanes_and_contracts_distinct():
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    base = dict(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        signal_timestamp=datetime(2026, 8, 25, 9, 45),
    )
    lane_a = SimpleNamespace(
        **base, source_lane="A", trading_symbol="NIFTY-A"
    )
    lane_b = SimpleNamespace(
        **base, source_lane="B", trading_symbol="NIFTY-B"
    )

    assert scanner._candidate_lifecycle_key(lane_a) != scanner._candidate_lifecycle_key(
        lane_b
    )


def test_candidate_lifecycle_fallback_is_stable_and_exit_provenance_is_additive(tmp_path):
    tracker = RegimeTracker(tmp_path)
    candidate = SimpleNamespace(
        action="EXIT",
        instrument="BANKNIFTY",
        timeframe="15m",
        direction="SHORT",
        signal_timestamp="2026-08-25T09:45:00",
        entry_timestamp="2026-08-25T09:46:00",
        exit_timestamp="2026-08-25T10:15:00",
        entry_price=200.0,
        exit_price=240.0,
        pnl=-40.0,
        pnl_path=[[0, 0], [60, -40]],
        lots=1,
        lot_size=1,
        runner_mode=True,
        stop_provenance="runner",
        runner_activation_timestamp="2026-08-25T10:00:00",
        runner_stop_at_exit=230.0,
    )

    event = tracker._event_from_candidate(
        candidate,
        decision="EXIT",
        reason="RUNNER_TRAIL_STOP",
        source="test",
        extra=None,
    )
    same_shape = tracker._event_from_candidate(
        deepcopy(candidate),
        decision="EXIT",
        reason="RUNNER_TRAIL_STOP",
        source="test",
        extra=None,
    )
    assert event["lifecycle_id"] == same_shape["lifecycle_id"]
    assert event["exit_provenance"] == {
        "runner_active": True,
        "stop_provenance": "runner",
        "runner_activation_timestamp": "2026-08-25T10:00:00",
        "runner_stop_at_exit": 230.0,
    }
    assert tracker.record_event(event) is True
    assert tracker.record_event(same_shape) is False

    rows = tracker.load_events(weeks=1, as_of=datetime(2026, 8, 25, 12, 0))
    assert rows[0]["exit_provenance"]["stop_provenance"] == "runner"


def test_scanner_freezes_one_lifecycle_across_accept_serialize_and_exit(tmp_path):
    from engine.signal_processor import TradeCandidate
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    scanner.rtracker = RegimeTracker(tmp_path)
    scanner._entry_intel_snapshot = lambda _candidate: {}
    scanner._manual_signal_exit_state = lambda _candidate: {}

    entry = TradeCandidate(
        instrument="NIFTY",
        direction="LONG",
        price=125.0,
        stop=120.0,
        target=135.0,
        lots=1,
        lot_size=65,
        grade="A",
        confidence=0.82,
        timeframe="5min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=24500.0,
        multiplier=0.5,
        trading_symbol="NIFTYTESTCE",
        symbol_token="123",
        signal_timestamp=datetime(2026, 8, 25, 10, 0),
        quote_verified=True,
        quote_source="kite_ws",
    )
    entry.accepted_by_gate = True
    entry.decision_intel = {"measured": 1}

    scanner._rtracker_record_candidate(
        entry, decision="ACCEPTED", source="test_accept"
    )
    accepted_lifecycle = entry.lifecycle_id
    payload = scanner._serialize_trade_candidate(entry)

    exit_candidate = scanner._candidate_from_payload(payload)
    assert exit_candidate is not None
    exit_candidate.action = "EXIT"
    exit_candidate.status = "CLOSED"
    exit_candidate.exit_reason = "TARGET"
    exit_candidate.exit_timestamp = datetime(2026, 8, 25, 10, 15)
    exit_candidate.pnl = 250.0
    scanner._rtracker_record_candidate(
        exit_candidate, decision="EXIT", reason="TARGET", source="test_exit"
    )

    rows = scanner.rtracker.load_events(
        weeks=1, as_of=datetime(2026, 8, 25, 12, 0)
    )
    accepted = next(row for row in rows if row["decision"] == "ACCEPTED")
    exited = next(row for row in rows if row["decision"] == "EXIT")
    assert payload["lifecycle_id"] == accepted_lifecycle
    assert accepted["lifecycle_id"] == accepted_lifecycle
    assert exited["lifecycle_id"] == accepted_lifecycle


def test_rtracker_preserves_strategy_quote_risk_and_runner_basis(tmp_path):
    tracker = RegimeTracker(tmp_path)
    candidate = SimpleNamespace(
        action="EXIT",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        signal_timestamp="2026-08-25T10:00:00",
        exit_timestamp="2026-08-25T10:15:00",
        price=100.0,
        strategy_revision="rev-42",
        strategy_direction_mode="BIDIRECTIONAL",
        data_provenance="LIVE",
        quote_verified=True,
        quote_source="kite_ws",
        entry_quote_verified=True,
        entry_quote_source="kite_touch",
        risk_geometry_source="candidate_atr_preserved",
        runner_mode=True,
        runner_stop=24520.0,
        runner_stop_basis="INDEX_SPOT",
        stop_provenance="runner",
    )
    event = tracker._event_from_candidate(
        candidate,
        decision="EXIT",
        reason="RUNNER_TRAIL_STOP",
        source="test",
        extra=None,
    )

    assert event["strategy"] == {
        "revision": "rev-42",
        "direction_mode": "BIDIRECTIONAL",
        "data_provenance": "LIVE",
    }
    assert event["quote"]["decision_verified"] is True
    assert event["quote"]["entry_verified"] is True
    assert event["quote"]["exit_verified"] is None
    assert event["flags"]["risk_geometry_source"] == "candidate_atr_preserved"
    assert event["exit_provenance"]["runner_stop_basis"] == "INDEX_SPOT"


def test_broker_fill_not_session_advisory_owns_the_terminal_exit(tmp_path):
    from scanner import Scanner

    lifecycle = "NIFTY|5min|LONG|2026-08-25T10:00:00||NIFTYTESTCE"
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.rtracker = RegimeTracker(tmp_path)
    scanner._entry_intel_snapshot = lambda _candidate: {}

    live_trade = SimpleNamespace(
        id="T-LIVE",
        lifecycle_id=lifecycle,
        status="OPEN",
        is_ghost=False,
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        trading_symbol="NIFTYTESTCE",
        signal_time=datetime(2026, 8, 25, 10, 0),
    )
    # Reproduce the precise final-fill race window: accounting has removed the Trade
    # from open_trades, but the authoritative callback has not written rtracker yet.
    scanner.trades = SimpleNamespace(
        open_trades={},
        _terminal_callback_lifecycles={lifecycle},
    )
    advisory = SimpleNamespace(
        lifecycle_id=lifecycle,
        action="EXIT",
        status="EXIT_PENDING",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        trading_symbol="NIFTYTESTCE",
        signal_timestamp=datetime(2026, 8, 25, 10, 0),
        entry_timestamp=datetime(2026, 8, 25, 10, 1),
        exit_timestamp=datetime(2026, 8, 25, 10, 15),
        exit_price=108.0,
        exit_reason="TARGET",
        pnl=800.0,
        pnl_path=[[0, 0], [840, 800]],
        decision_intel={"measured": 1},
    )

    scanner._rtracker_record_candidate(
        advisory,
        decision="EXIT",
        reason="TARGET",
        source="session_candidate",
    )

    live_trade.status = "CLOSED"
    live_trade.entry_time = datetime(2026, 8, 25, 10, 1)
    live_trade.exit_time = datetime(2026, 8, 25, 10, 16)
    live_trade.entry_price = 100.0
    live_trade.exit_price = 107.0
    live_trade.exit_reason = "TARGET"
    live_trade.pnl = 650.0
    live_trade.charges = 50.0
    live_trade.pnl_path = [[0, 0], [900, 650]]
    scanner._record_trade_manager_terminal(live_trade)

    rows = scanner.rtracker.load_events(
        weeks=1, as_of=datetime(2026, 8, 25, 12, 0)
    )
    advisory_row = next(row for row in rows if row["decision"] == "EXIT_DECISION")
    terminal = next(row for row in rows if row["decision"] == "EXIT")
    assert advisory_row["pnl"] == 0.0
    assert advisory_row["extra"]["terminal_authoritative"] is False
    assert advisory_row["extra"]["advisory_pnl"] == 800.0
    assert terminal["source"] == "trade_manager_terminal_fill"
    assert terminal["pnl"] == 650.0
    assert terminal["lifecycle_id"] == lifecycle
    assert sum(row["decision"] == "EXIT" for row in rows) == 1
