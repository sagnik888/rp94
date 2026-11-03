"""Regression cases for the guard-audit items left open after the 2026-10-07 fixes.

1. Exit-policy owner is frozen at admission: lane switches govern admission only.
2. S1 trend UNKNOWN is distinguished from a measured NEUTRAL, carries its evidence
   source and age, and is priced by a bounded s1_lane_giveback_unknown.
3. Replay applies the same profit-lock HOLD contract as live and manual.
4. One shared HOLD resolver (engine/profit_lock_hold.py) and a persisted exit decision
   record (engine/exit_decision_record.py).

Each behaviour is paired with a control so a guard that stopped firing would fail too.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from config.settings import Settings, get_settings
from engine import exit_decision_record, profit_lock_hold, s1_lane, uts_lane

ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides):
    s = Settings()
    for key, value in overrides.items():
        object.__setattr__(s, key, value)
    return s


# ── 1. frozen exit-policy owner ─────────────────────────────────────────────────────

def test_owner_is_stamped_once_and_survives_the_admission_switch():
    row = SimpleNamespace(source_engine="s1", source_lane="S1")
    assert s1_lane.stamp_exit_owner(row, _settings()) == "S1"
    disabled = _settings(s1_lane_enabled=False)
    assert s1_lane.is_s1(row, disabled) is False, "admission still follows the switch"
    assert s1_lane.managed_lane(row, disabled) == "S1", "management follows the frozen owner"
    assert s1_lane.stamp_exit_owner(row, disabled) == "S1", "a stamp is never overwritten"


def test_a_trade_admitted_outside_the_lane_stays_generic_when_the_lane_returns():
    row = SimpleNamespace(source_engine="s1", source_lane="S1")
    assert s1_lane.stamp_exit_owner(row, _settings(s1_lane_enabled=False)) == "GENERIC"
    assert s1_lane.managed_lane(row, _settings(s1_lane_enabled=True)) == ""


def test_a_legacy_unstamped_position_is_frozen_on_its_first_management_pass():
    row = SimpleNamespace(source_engine="ut_smart", source_lane="UTS")
    assert s1_lane.managed_lane(row, _settings()) == "UTS"
    assert row.exit_policy_owner == "UTS"
    assert s1_lane.managed_lane(row, _settings(uts_lane_enabled=False)) == "UTS"


def test_exit_decision_keeps_a_uts_trade_on_uts_exits_after_uts_is_switched_off(monkeypatch):
    seen = []
    monkeypatch.setattr(uts_lane, "exit_decision", lambda *a, **k: seen.append("uts") or "UTS")
    trade = SimpleNamespace(source_engine="ut_smart", source_lane="UTS", exit_policy_owner="UTS")
    assert s1_lane.exit_decision(trade, 0.0, 0.0, settings=_settings(uts_lane_enabled=False)) == "UTS"
    assert seen == ["uts"]


def test_session_state_round_trips_the_owner_and_rejects_invalid_values():
    source = SimpleNamespace(exit_policy_owner="S1", exit_decision_record={"schema": "exit_decision.v1"})
    payload = s1_lane.snapshot_session_state(source)
    restored = SimpleNamespace()
    s1_lane.restore_session_state(restored, payload)
    assert restored.exit_policy_owner == "S1"
    assert restored.exit_decision_record == {"schema": "exit_decision.v1"}
    bad = SimpleNamespace()
    s1_lane.restore_session_state(bad, {"exit_policy_owner": "QP_MAYBE"})
    assert not hasattr(bad, "exit_policy_owner")


def test_trade_persistence_round_trips_owner_and_exit_record():
    from data.sqlite_db import TRADE_PERSISTED_COLUMNS, _TRADE_JSON_COLUMNS
    from trading.trade_manager import IST, Trade

    trade = Trade(
        id="T-OWN", instrument="NIFTY", timeframe="5min", direction="LONG", entry_price=100.0,
        entry_time=datetime.now(IST), trailing_stop=90.0, current_stop=90.0, lots=1,
        lot_size=65, grade="A", inst_type="OPT", exit_policy_owner="S1",
        exit_decision_record={"controlling_reason": "S1_PROFIT_LOCK"},
    )
    restored = Trade.from_dict(trade.to_dict())
    assert restored.exit_policy_owner == "S1"
    assert restored.exit_decision_record == {"controlling_reason": "S1_PROFIT_LOCK"}
    assert {"exit_policy_owner", "exit_decision_record"} <= set(TRADE_PERSISTED_COLUMNS)
    assert "exit_decision_record" in _TRADE_JSON_COLUMNS


def test_open_trade_freezes_the_owner_at_admission(monkeypatch):
    from tests.test_guard_audit_consistency_2026_10_07 import _Broker, _manager
    from trading.trade_manager import IST

    monkeypatch.setattr("trading.order_retry.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda _name, func, *args, **kwargs: func(*args, **kwargs),
    )

    def open_one(**extra):
        manager = _manager(_Broker(), guard=None)
        return manager.open_trade(
            instrument="NIFTY", timeframe="5min", direction="LONG", price=100.0,
            trailing_stop=95.0, lots=1, lot_size=10, grade="A", target=110.0,
            trading_symbol="NIFTYTESTFUT", symbol_token="TOKEN-NIFTY", inst_type="FUT",
            exec_type="M", entry_time=IST.localize(datetime(2026, 10, 7, 10, 0)), **extra,
        )

    assert open_one(source_engine="s1", source_lane="S1").exit_policy_owner == "S1"
    assert open_one(source_engine="quad_prime").exit_policy_owner == "GENERIC"
    assert open_one(source_engine="s1", source_lane="S1", exit_policy_owner="GENERIC").exit_policy_owner == "GENERIC"


def test_broker_monitor_keeps_a_frozen_s1_trade_on_s1_exits_after_the_switch(monkeypatch):
    from tests.test_s1_lane_2026_09_23 import _monitor, _open_trade

    monkeypatch.setattr(get_settings(), "s1_lane_enabled", False)
    closed, _updated, other = _monitor(
        monkeypatch, _open_trade(exit_policy_owner="S1"), spot=23370.0, premium=96.0
    )
    assert closed == ["S1_SL1"] and other == []


def test_control_an_unstamped_trade_after_the_switch_goes_to_the_generic_guards(monkeypatch):
    from tests.test_s1_lane_2026_09_23 import _monitor, _open_trade

    monkeypatch.setattr(get_settings(), "s1_lane_enabled", False)
    trade = _open_trade()
    closed, _updated, other = _monitor(monkeypatch, trade, spot=23370.0, premium=96.0)
    assert other == ["tf15"] and closed == ["TF15_FAST_REVERSAL"]
    assert trade.exit_policy_owner == "GENERIC"


def test_management_sites_read_the_frozen_owner():
    from scanner import Scanner

    manual = inspect.getsource(Scanner._check_session_candidates_breaches_locked)
    live = (ROOT / "engine" / "exit_cycle.py").read_text(encoding="utf-8-sig")
    assert "if s1_lane_policy.is_managed_lane(previous, settings):" in manual
    assert "if s1_lane_policy.is_managed_lane(trade):" in live
    remember = inspect.getsource(Scanner._remember_trade_candidates)
    assert "s1_lane_policy.stamp_exit_owner(candidate)" in remember


# ── 2. S1 UNKNOWN trend ─────────────────────────────────────────────────────────────

def _s1_row(**extra):
    base = dict(
        source_engine="s1", source_lane="S1", exit_policy_owner="S1", instrument="NIFTY",
        timeframe="5min", inst_type="OPT", direction="LONG", price=100.0, initial_stop=90.0,
        lots=1, lot_size=65, underlying_entry=25000.0, underlying_stop=24950.0,
        s1_peak_premium=105.0, s1_score=70.0, signal_time=datetime(2026, 10, 6, 11, 0),
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _state(bar, bull=70.0, bear=30.0):
    return {"state": {"s1_state_bar_time": bar, "s1_bull_score": bull, "s1_bear_score": bear}}


def test_trend_evidence_separates_unknown_from_neutral():
    row = _s1_row()
    now = datetime(2026, 10, 6, 11, 16)
    missing = s1_lane.closed_bar_trend_evidence(row, None, _settings(), now=now)
    assert (missing.known, missing.source, missing.label) == (False, "engine_state_missing", "UNKNOWN")
    first_bar = s1_lane.closed_bar_trend_evidence(row, _state(datetime(2026, 10, 6, 11, 0)), _settings(), now=now)
    assert (first_bar.known, first_bar.source) == (False, "post_signal_bar_overdue")
    assert first_bar.age_seconds == 660.0
    no_scores = s1_lane.closed_bar_trend_evidence(
        row, {"state": {"s1_state_bar_time": datetime(2026, 10, 6, 11, 5)}}, _settings(), now=now)
    assert (no_scores.known, no_scores.source) == (False, "closed_bar_scores_missing")
    neutral = s1_lane.closed_bar_trend_evidence(
        row, _state(datetime(2026, 10, 6, 11, 5), 55.0, 45.0), _settings(), now=now)
    assert (neutral.known, neutral.trend, neutral.label) == (True, 0, "NEUTRAL")
    assert neutral.age_seconds == pytest.approx(360.0)


def test_stale_evidence_never_widens_but_keeps_an_against_reading():
    row = _s1_row()
    now = datetime(2026, 10, 6, 11, 40)  # bar closed 11:10, 30 min > 3 bars
    stale_with = s1_lane.closed_bar_trend_evidence(row, _state(datetime(2026, 10, 6, 11, 5)), _settings(), now=now)
    assert (stale_with.trend, stale_with.known, stale_with.source) == (0, False, "stale_closed_bar")
    stale_against = s1_lane.closed_bar_trend_evidence(
        row, _state(datetime(2026, 10, 6, 11, 5), 20.0, 80.0), _settings(), now=now)
    assert (stale_against.trend, stale_against.known) == (-1, True)
    fresh_with = s1_lane.closed_bar_trend_evidence(
        row, _state(datetime(2026, 10, 6, 11, 5)), _settings(), now=datetime(2026, 10, 6, 11, 12))
    assert (fresh_with.trend, fresh_with.known) == (1, True)


def _decide(premium, evidence, **settings):
    return s1_lane.exit_decision(
        _s1_row(), 25010.0, premium, now=datetime(2026, 10, 6, 12, 0),
        settings=_settings(**settings), regime="TRENDING_UP", market=None,
        trend_evidence=evidence,
    )


UNKNOWN = s1_lane.TrendEvidence(0, False, "no_closed_bar_after_signal")
NEUTRAL = s1_lane.TrendEvidence(0, True, "closed_bar", 60.0)


def test_unknown_gets_the_with_trend_giveback_since_2026_10_09():
    # Audit example: entry 100, stop 90, peak 105 (0.5R; the lock is pinned to arm there).
    # Neutral 10% exits at 104.49; UNKNOWN now allows 25% (user, 2026-10-09) down to 103.75.
    arm = dict(s1_lane_lock_arm_r=0.5)
    assert not _decide(104.49, UNKNOWN, **arm).exit
    assert _decide(104.49, NEUTRAL, **arm).exit
    unknown = _decide(103.74, UNKNOWN, **arm)
    assert unknown.exit and unknown.reason == "S1_PROFIT_LOCK"
    assert "trend UNKNOWN (no_closed_bar_after_signal)" in unknown.detail


def test_unknown_giveback_is_its_own_setting_and_bounded_by_the_with_trend_value():
    arm = dict(s1_lane_lock_arm_r=0.5)
    assert not _decide(104.0, UNKNOWN, s1_lane_giveback_unknown=25.0, **arm).exit
    assert _decide(104.0, NEUTRAL, s1_lane_giveback_unknown=25.0, **arm).exit, "neutral is not unknown"
    assert _decide(104.0, UNKNOWN, s1_lane_giveback_unknown=10.0, **arm).exit
    # 50% is capped at with-trend 25%: exit below gain 3.75 (premium 103.75).
    assert _decide(103.74, UNKNOWN, s1_lane_giveback_unknown=50.0, **arm).exit
    assert not _decide(103.9, UNKNOWN, s1_lane_giveback_unknown=50.0, **arm).exit


def test_unknown_giveback_is_declared_and_dashboard_editable():
    assert "s1_lane_giveback_unknown" in s1_lane.S1_SETTING_SPEC
    # 2026-10-09 (user): UNKNOWN takes the with-trend giveback, no longer the neutral 10%.
    assert Settings().s1_lane_giveback_unknown == pytest.approx(Settings().s1_lane_giveback_with_trend) == 25.0
    js = (ROOT / "dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    assert "'s1_lane_giveback_unknown'" in js


def test_both_s1_monitors_pass_trend_evidence():
    from scanner import Scanner

    manual = inspect.getsource(Scanner._check_session_candidates_breaches_locked)
    live = (ROOT / "engine" / "exit_cycle.py").read_text(encoding="utf-8-sig")
    assert "trend_evidence=s1_lane_policy.closed_bar_trend_evidence(" in manual
    assert "trend_evidence=s1_lane_policy.closed_bar_trend_evidence(" in live


# ── 4a. the shared resolver ─────────────────────────────────────────────────────────

def _host(*, defer=False, clamp=None, raise_defer=False, raise_breathing=False):
    host = SimpleNamespace()

    def breathing(trade, stop, **kw):
        if raise_breathing:
            raise RuntimeError("breathing broke")
        return (clamp, 2.0, True) if clamp is not None else (stop, 0.0, False)

    def deferral(*a, **kw):
        if raise_defer:
            raise RuntimeError("trend data broke")
        return (True, "valid trend hold") if defer else (False, "trend not supportive")

    host._apply_option_stop_breathing_room = breathing
    host._should_defer_trend_option_profit_lock = deferral
    return host


def _resolve(host, *, mark=109.6, lock_stop=110.125, pre=85.0, **kw):
    trade = SimpleNamespace()
    hold = profit_lock_hold.resolve(
        host, trade, lock_reason="SMART_PROFIT_LOCK", lock_stop=lock_stop,
        proposed_stop=max(pre, lock_stop), pre_lock_stop=pre, mark=mark, option_space=True,
        current_pnl_rs=960.0, peak_pnl_rs=1500.0, latest_time=datetime(2026, 10, 7, 11, 0),
        regime="TRENDING_UP", **kw,
    )
    return hold, trade


def test_resolver_trend_hold_restores_the_pass_start_stop_and_records_the_suppression():
    hold, trade = _resolve(_host(defer=True))
    assert (hold.hit, hold.trend_deferred, hold.restored, hold.stop) == (False, True, True, 85.0)
    assert hold.lock_reason == "" and hold.lock_stop == 0.0
    assert any(item["state"] == "SUPPRESSED_BY_HOLD" for item in trade._profit_lock_trace)


def test_resolver_breathing_clamp_keeps_the_protected_stop_and_moves_the_floor():
    hold, _ = _resolve(_host(clamp=108.0))
    assert (hold.hit, hold.breathing_clamped, hold.stop, hold.effective_lock_stop) == (False, True, 108.0, 108.0)


def test_resolver_controls_crossed_lock_hits_and_unverified_marks_do_not():
    hold, _ = _resolve(_host())
    assert hold.hit and hold.lock_reason == "SMART_PROFIT_LOCK" and hold.stop == 110.125
    unverified, _ = _resolve(_host(), mark_verified=False)
    assert not unverified.hit


def test_resolver_fails_protective_when_hold_or_breathing_cannot_be_evaluated():
    hold, trade = _resolve(_host(raise_defer=True, defer=True))
    assert hold.hit and not hold.trend_deferred
    assert any(item["state"] == "ERROR_NOT_APPLIED" for item in trade._profit_lock_trace)
    clamp_error, _ = _resolve(_host(raise_breathing=True, clamp=108.0))
    assert clamp_error.stop == 110.125 and not clamp_error.breathing_clamped


def test_resolver_cancels_a_lock_that_would_not_bank_a_net_gain():
    hold, _ = _resolve(_host(), stop_viable=lambda stop: stop > 200.0)
    assert hold.lock_reason == "" and hold.stop == 85.0 and not hold.hit


def test_resolver_non_option_hit_is_evaluated_on_the_resolved_stop():
    trade = SimpleNamespace()
    hold = profit_lock_hold.resolve(
        _host(), trade, lock_reason="SMART_PROFIT_LOCK", lock_stop=25_100.0,
        proposed_stop=25_100.0, pre_lock_stop=25_000.0, mark=25_090.0, option_space=False,
        current_pnl_rs=500.0, peak_pnl_rs=900.0, latest_time=datetime(2026, 10, 7, 11, 0),
        non_option_hit=lambda stop: 25_090.0 <= stop,
    )
    assert hold.hit and hold.stop == 25_100.0


def test_all_three_paths_use_the_shared_resolver():
    from scanner import Scanner
    from engine.signal_processor import SignalProcessor

    manual = inspect.getsource(Scanner._check_session_candidates_breaches_locked)
    live = (ROOT / "engine" / "exit_cycle.py").read_text(encoding="utf-8-sig")
    replay = inspect.getsource(SignalProcessor._historical_live_management_probe)
    for source in (manual, live, replay):
        assert "profit_lock_hold_policy.resolve(" in source
        assert "_smart_profit_retention_eligible(" in source


# ── broker path behaviour (real ExitCycle pass) ─────────────────────────────────────

def test_broker_trend_hold_restores_the_stop_and_does_not_close(monkeypatch):
    from tests.test_workflow_exit_authority_2026_09_23 import _scanner, _trade

    trade = _trade()
    sc = _scanner(monkeypatch, trade, mark=109.0)
    sc._should_defer_trend_option_profit_lock = lambda *a, **kw: (True, "valid trend hold")
    sc._update_active_trades()
    sc._close_and_record.assert_not_called()
    assert sc.trades.update_trade.call_args.args[2] < 110.432
    assert any("Deferred SMART_PROFIT_LOCK" in str(c.args[0]) for c in sc.log_event.call_args_list)


def test_control_broker_crossed_lock_without_hold_exits_at_the_mark(monkeypatch):
    from tests.test_workflow_exit_authority_2026_09_23 import _scanner, _trade

    trade = _trade()
    sc = _scanner(monkeypatch, trade, mark=109.0)
    sc._update_active_trades()
    assert sc._close_and_record.call_args.args == (trade.id, 109.0, "SMART_PROFIT_LOCK")


# ── 3. replay HOLD parity (real replay probe) ───────────────────────────────────────

def _replay_probe(*, defer):
    from engine.signal_processor import SignalProcessor

    frame = pd.DataFrame(
        [(100, 101, 99, 100)] * 120, columns=["open", "high", "low", "close"],
        index=pd.date_range("2026-09-11 09:15", periods=120, freq="1min"),
    )
    source = SimpleNamespace(
        candles=SimpleNamespace(get_candles=lambda *a: frame), active_indices=["NIFTY"],
        _compute_historical_intel_at=lambda *a: (0.0, "TRENDING_UP"),
    )
    processor = SignalProcessor(source)
    evaluate = processor._historical_live_management_probe(
        source, instrument="NIFTY", direction="LONG", option_type="CE", timeframe="5min",
        entry_time=pd.Timestamp("2026-09-11 10:00"), entry_price=100.0, stop_price=85.0,
        target_price=150.0, qty=100,
    )
    probe = inspect.getclosurevars(evaluate).nonlocals["probe"]
    probe._profit_lock_floor = lambda *a, **kw: (110.125, "SMART_PROFIT_LOCK", 0.675)
    probe._apply_option_stop_breathing_room = lambda t, stop, **kw: (stop, 0.0, False)
    probe._should_defer_trend_option_profit_lock = lambda *a, **kw: (
        (True, "valid trend hold") if defer else (False, "trend not supportive")
    )
    probe._big_move_expected = lambda *a, **kw: (False, "")
    probe._peak_giveback_cap_exit = lambda *a, **kw: (False, "")
    probe._loss_guard_decision = lambda *a, **kw: (False, "", 0.8)
    probe._rupee_loss_band_guard_decision = lambda *a, **kw: (False, "", "")
    probe._peak_threat_exit_decision = lambda *a, **kw: (False, "", "")
    probe._profit_lock_min_peak = lambda *a, **kw: 250.0
    probe._peak_giveback_or_reversal_exit = lambda *a, **kw: (False, "", "")
    probe._eod_loss_minimize_exit = lambda *a, **kw: (False, "")
    probe._major_win_guard_ladder_arm_rs = lambda *a, **kw: 1e9
    probe._stagnation_exit_decision = lambda *a, **kw: (False, "")
    return evaluate


def test_replay_trend_hold_is_honoured_and_not_overruled_by_the_backstop():
    evaluate = _replay_probe(defer=True)
    reason, _detail, next_stop, _regime, trace, _guards = evaluate(
        pd.Timestamp("2026-09-11 10:20"), 109.6, 110.0, 1500.0
    )
    assert reason == "", f"replay exited through the HOLD with {reason}"
    assert next_stop == pytest.approx(85.0)
    assert any(item.get("state") == "SUPPRESSED_BY_HOLD" for item in trace)


def test_control_replay_crossed_lock_without_hold_exits_at_the_lock_floor():
    evaluate = _replay_probe(defer=False)
    reason, _detail, next_stop, *_ = evaluate(pd.Timestamp("2026-09-11 10:20"), 109.6, 110.0, 1500.0)
    assert reason == "SMART_PROFIT_LOCK"
    assert next_stop == pytest.approx(110.125)


# ── 4b. exit decision record ────────────────────────────────────────────────────────

def test_exit_record_names_owner_controlling_rule_and_suppressed_proposals():
    hold, trade = _resolve(_host(defer=True))
    trade.exit_policy_owner = "GENERIC"
    trade.peak_pnl = 1500.0
    record = exit_decision_record.build(
        trade, reason="PROFIT_GIVEBACK_GUARD", mark=104.0,
        decided_at=datetime(2026, 10, 7, 11, 0), path="manual", detail="later rule",
    )
    assert record["schema"] == "exit_decision.v1"
    assert (record["owner"], record["policy"], record["controlling_reason"]) == (
        "GENERIC", "generic_exit_stack", "PROFIT_GIVEBACK_GUARD")
    assert record["suppressed_by_hold"] and record["suppressed_by_hold"][0]["guard"] == "SMART_PROFIT_LOCK"
    assert record["profit_lock_current"] is True


def test_exit_record_marks_legacy_rows_and_never_raises():
    record = exit_decision_record.build(object(), reason="STOP_HIT", mark=float("nan"),
                                        decided_at=None, path="broker")
    assert record["owner"] == "LEGACY_UNSTAMPED" and record["mark"] is None


def test_exit_record_is_attached_on_both_exit_paths():
    from scanner import Scanner

    broker_close = inspect.getsource(Scanner._close_and_record_internal)
    manual = inspect.getsource(Scanner._check_session_candidates_breaches_locked)
    assert 'exit_decision_record_policy.build(' in broker_close and 'path="broker"' in broker_close
    assert '_capture_exit_tracking(exit_candidate, previous' in manual
    capture = inspect.getsource(Scanner._capture_exit_tracking)
    assert 'exit_decision_record_policy.build(' in capture and 'path="manual"' in capture
