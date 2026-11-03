from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from engine.decision_record import build_decision_record, update_decision_record


def candidate(**extra):
    values = dict(instrument="MIDCPNIFTY", timeframe="5min", direction="LONG",
                  signal_timestamp=datetime(2026, 10, 5, 13, 45),
                  entry_timestamp=datetime(2026, 10, 5, 13, 50, 20),
                  entered_intracandle=False, source_lane="S1", action="ENTRY",
                  underlying_stop=13635.5, price=284.55, current_price=284.55)
    values.update(extra)
    return SimpleNamespace(**values)


def test_closed_bar_latency_excludes_confirmation_duration():
    record = build_decision_record(candidate(), rank=1, decided_at=datetime(2026, 10, 5, 13, 50, 20))
    assert record["signal_available_at"] == "2026-10-05T13:50:00"
    assert record["post_close_delay_seconds"] == 20.0
    assert record["data_age_seconds"] == 320.0


def test_intrabar_has_no_invented_close_delay():
    record = build_decision_record(candidate(entered_intracandle=True), rank=1,
                                   decided_at=datetime(2026, 10, 5, 13, 46))
    assert record["post_close_delay_seconds"] is None
    assert record["signal_available_at"] == ""


def test_acceptance_clock_survives_duplicate_observation():
    c = candidate()
    first = datetime(2026, 10, 5, 13, 50, 20)
    update_decision_record(c, status="ACCEPTED", reason="native allow", source="manual", decided_at=first)
    record = update_decision_record(c, status="DEDUPED", reason="seen", source="manual", decided_at=first + timedelta(minutes=2))
    assert record["accepted_at"] == first.isoformat()
    assert record["post_close_delay_seconds"] == 20.0


def test_gap_persists_even_when_recovered_spot_is_above_stop():
    from engine.session_continuity import observe_continuity
    c = candidate(monitoring_continuity={"schema_version": "session_monitor.v1", "last_check_at": "2026-10-05T13:58:31"})
    bars = pd.DataFrame(dict(low=[13640, 13630.1], high=[13680, 13660]),
                        index=pd.to_datetime(["2026-10-05 14:03", "2026-10-05 14:04"]))
    state, changed, alert = observe_continuity(c, now=datetime(2026, 10, 5, 14, 24, 38),
        quote_usable=True, history=bars, history_verified=True)
    assert changed and alert
    assert state["status"] == "RECOVERY_REQUIRED"
    assert state["possible_stop_breach"] is True
    assert state["first_possible_stop_breach_at"] == "2026-10-05T14:04:00"
    assert state["historical_fill_inferred"] is False
    assert c.current_price == 284.55 and c.action == "ENTRY"
    state, _, alert = observe_continuity(c, now=datetime(2026, 10, 5, 14, 24, 39), quote_usable=True)
    assert state["status"] == "RECOVERY_REQUIRED" and not alert


@pytest.mark.parametrize("verified", [False, True])
def test_partial_or_unverified_history_never_claims_no_missed_breach(verified):
    from engine.session_continuity import observe_continuity
    c = candidate(monitoring_continuity={"last_check_at": "2026-10-05T13:58:31"})
    bars = pd.DataFrame(dict(low=[13670], high=[13680]), index=pd.to_datetime(["2026-10-05 14:03"]))
    state, _, _ = observe_continuity(c, now=datetime(2026, 10, 5, 14, 24, 38),
        quote_usable=True, history=bars, history_verified=verified)
    assert state["possible_stop_breach"] is None
    assert state["history_coverage"] in {"UNVERIFIED", "INCOMPLETE"}


def test_monitor_gap_uses_check_clock_even_without_fresh_quote():
    from engine.session_continuity import observe_continuity
    c = candidate(monitoring_continuity={"last_check_at": "2026-10-05T13:58:31"})
    state, _, alert = observe_continuity(c, now=datetime(2026, 10, 5, 14, 24, 38), quote_usable=False)
    assert alert and state["status"] == "RECOVERY_REQUIRED"
    assert state["current_quote_usable"] is False
    assert state.get("last_quote_check_at", "") == ""


def test_roundtrip_continuity_and_exit_mark_are_independent_copies():
    from engine.session_continuity import snapshot_continuity, restore_continuity
    c = candidate(monitoring_continuity={"status": "RECOVERY_REQUIRED", "gaps": [{"seconds": 100}]},
                  exit_observation={"price": 265.3}, exit_price_basis="OBSERVED_LIVE_MARK")
    payload = snapshot_continuity(c)
    restored = candidate()
    restore_continuity(restored, payload)
    payload["monitoring_continuity"]["gaps"][0]["seconds"] = 0
    assert restored.monitoring_continuity["gaps"][0]["seconds"] == 100
    assert restored.exit_observation["price"] == 265.3


def test_missing_observation_clock_is_reported_without_assuming_continuity():
    from engine.session_continuity import observe_continuity
    c = candidate()
    state, _, alert = observe_continuity(c, now=datetime(2026, 10, 5, 14, 24, 38), quote_usable=True)
    assert alert and state["history_coverage"] == "UNKNOWN_START"
    assert state["historical_fill_inferred"] is False


def test_manual_poll_uses_nonblocking_quotes_and_shared_exit_lock():
    import threading
    from scanner import Scanner
    c = candidate()
    calls = []
    host = Scanner.__new__(Scanner)
    host.mode = "REAL"
    host._session_breach_lock = threading.RLock()
    host._loaded_session_is_current_day = lambda: True
    host._session_trade_candidates = {"MIDCPNIFTY": {"one": c}}
    host._active_session_entry_candidates = lambda instrument: [c]
    host.config = {"indices": {"MIDCPNIFTY": {}}}
    host._refresh_trade_candidates = lambda rows, config, *, allow_blocking=True: calls.append(("refresh", allow_blocking))
    host._check_session_candidates_breaches_locked = lambda instrument: calls.append(("check", instrument))
    host._poll_manual_signal_protection()
    assert calls == [("refresh", False), ("check", "MIDCPNIFTY")]


@pytest.mark.parametrize("start,end,expected", [
    ("2026-10-05T13:58:31", "2026-10-05T14:02:38", None),
    ("2026-10-05T13:59:00", "2026-10-05T14:02:00", False),
])
def test_complete_interior_bars_do_not_cover_partial_boundary_minutes(start, end, expected):
    from engine.session_continuity import observe_continuity
    c = candidate(monitoring_continuity={"last_check_at": start})
    bars = pd.DataFrame(dict(low=[13670] * 3, high=[13680] * 3),
                        index=pd.date_range("2026-10-05 13:59", periods=3, freq="min"))
    state, _, _ = observe_continuity(c, now=datetime.fromisoformat(end), quote_usable=True,
                                    history=bars, history_verified=True)
    assert state["possible_stop_breach"] is expected


def test_verified_short_touch_ignores_future_and_previous_session_bars():
    from engine.session_continuity import observe_continuity
    c = candidate(direction="SHORT", underlying_stop=13685,
                  monitoring_continuity={"last_check_at": "2026-10-05T14:00:00"})
    bars = pd.DataFrame(dict(low=[13660]*4, high=[13690, 13680, 13690, 13700]),
        index=pd.to_datetime(["2026-10-04 14:01", "2026-10-05 14:01", "2026-10-05 14:02", "2026-10-05 14:10"]))
    state, _, _ = observe_continuity(c, now=datetime(2026, 10, 5, 14, 5), quote_usable=True,
                                    history=bars, history_verified=True)
    assert state["first_possible_stop_breach_at"] == "2026-10-05T14:02:00"


def test_healthy_monitoring_has_no_gap_and_limits_checkpoint_writes():
    from engine.session_continuity import observe_continuity
    c = candidate()
    now = c.entry_timestamp
    state, changed, alert = observe_continuity(c, now=now, quote_usable=True)
    assert changed and not alert and state["status"] == "CONNECTED"
    _, changed, alert = observe_continuity(c, now=now + timedelta(seconds=1), quote_usable=True)
    assert not changed and not alert
    _, changed, _ = observe_continuity(c, now=now + timedelta(seconds=11), quote_usable=True)
    assert changed


def test_invalid_continuity_values_do_not_poison_session_payload():
    from engine.session_continuity import snapshot_continuity, restore_continuity
    c = candidate(monitoring_continuity={"seconds": float("nan")})
    assert snapshot_continuity(c) == {}
    restore_continuity(c, {"exit_observation": {"price": float("inf")}})
    assert not hasattr(c, "exit_observation")


@pytest.mark.asyncio
async def test_manual_worker_progresses_while_entry_analysis_waits():
    import asyncio
    from scanner import Scanner
    host = Scanner.__new__(Scanner)
    host.is_running = True
    entry_finished, observed = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    host._poll_manual_signal_protection = lambda: loop.call_soon_threadsafe(observed.set)
    entry_task = asyncio.create_task(entry_finished.wait())
    worker = asyncio.create_task(host._background_manual_signal_worker())
    try:
        await asyncio.wait_for(observed.wait(), 2)
        assert not entry_task.done()
    finally:
        host.is_running = False
        entry_finished.set()
        await entry_task
        await worker


def test_continuous_polling_does_not_hide_unavailable_quote_interval():
    from engine.session_continuity import observe_continuity
    c = candidate()
    start = c.entry_timestamp
    observe_continuity(c, now=start, quote_usable=True)
    alerts = []
    for seconds in range(1, 120):
        state, _, alert = observe_continuity(c, now=start + timedelta(seconds=seconds), quote_usable=False)
        alerts.append(alert)
    state, changed, alert = observe_continuity(c, now=start + timedelta(seconds=120), quote_usable=True)
    assert sum(alerts) == 1 and changed and not alert
    assert state["status"] == "RECOVERY_REQUIRED"
    assert state["gap_kind"] == "QUOTE_UNAVAILABLE"
    assert state["seconds"] == 120
    assert state["current_quote_usable"] is True and not state["quote_gap_open"]


def test_naive_market_bar_and_utc_acceptance_have_correct_close_delay():
    from datetime import timezone
    c = candidate()
    stamp = datetime(2026, 10, 5, 8, 20, 20, tzinfo=timezone.utc)
    record = update_decision_record(c, status="ACCEPTED", source="manual", reason="allow", decided_at=stamp)
    assert record["post_close_delay_seconds"] == 20.0
    assert record["signal_available_at"] == "2026-10-05T13:50:00"
    assert record["data_age_seconds"] == 320.0


def test_concurrent_exit_insertion_cannot_mutate_dashboard_iteration():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from scanner import Scanner
    from engine.session_continuity import session_locked
    c = candidate()
    host = Scanner.__new__(Scanner)
    host._session_breach_lock = threading.RLock()
    host._session_trade_candidates = {"MIDCPNIFTY": {"entry": c}}
    host._ensure_session_candidate_day = lambda: None
    host._repair_same_side_superseded_session_rows = lambda: False
    host._candidate_lifecycle_key = lambda row: "one"
    host._candidate_matches_live_session_day = lambda row: True
    host._candidate_chronology_valid = lambda row: True
    host._inherit_exit_entry_metadata = lambda *args: None
    reading, release, writer_waiting = threading.Event(), threading.Event(), threading.Event()
    def normalize(row):
        reading.set()
        assert release.wait(2)
    host._normalize_loaded_exit_candidate = normalize
    @session_locked
    def insert_exit(owner):
        owner._session_trade_candidates["MIDCPNIFTY"]["exit"] = candidate(action="EXIT")
    def writer():
        writer_waiting.set()
        insert_exit(host)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(host._get_session_trade_candidates, "MIDCPNIFTY")
        assert reading.wait(2)
        write = pool.submit(writer)
        assert writer_waiting.wait(2)
        assert not write.done()
        release.set()
        assert reader.result(timeout=2) == [c]
        write.result(timeout=2)
    assert "exit" in host._session_trade_candidates["MIDCPNIFTY"]


def test_distinct_quote_outages_freeze_their_own_protection_stop():
    from engine.session_continuity import observe_continuity
    c = candidate(underlying_stop=13635)
    start = c.entry_timestamp
    observe_continuity(c, now=start, quote_usable=True)
    observe_continuity(c, now=start + timedelta(seconds=1), quote_usable=False)
    observe_continuity(c, now=start + timedelta(seconds=31), quote_usable=False)
    observe_continuity(c, now=start + timedelta(seconds=32), quote_usable=True)
    c.underlying_stop = 13670
    observe_continuity(c, now=start + timedelta(seconds=33), quote_usable=True)
    observe_continuity(c, now=start + timedelta(seconds=34), quote_usable=False)
    # A native protection snapshot must survive checks without an exact quote.
    c.underlying_stop = 13000
    state, _, _ = observe_continuity(c, now=start + timedelta(seconds=65), quote_usable=False)
    quote_gaps = [gap for gap in state["gaps"] if gap["gap_kind"] == "QUOTE_UNAVAILABLE"]
    assert [gap["stop_at_gap_start"] for gap in quote_gaps] == [13635, 13670]
