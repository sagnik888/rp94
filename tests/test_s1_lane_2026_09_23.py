"""S1 lane (engine/s1_lane.py and its hooks): an S1 signal skips the other engines'
selection gates and is managed only by S1 exits plus the premium guard, while every
safety control still applies. User request 2026-09-23."""
import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import Settings
from engine import s1_lane
from engine.entry_policy import ExecutionGeometry, ExpectedMoveForecast, evaluate_final_entry_policy

ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides):
    s = Settings()
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


# ---------------------------------------------------------------- identity


def test_is_s1_covers_signal_candidate_and_restored_trade():
    s = _settings()
    assert s1_lane.is_s1(SimpleNamespace(source_engine="s1"), s)
    assert s1_lane.is_s1(SimpleNamespace(source_engine="", source_lane="S1"), s)  # after restart
    assert not s1_lane.is_s1(SimpleNamespace(source_engine="utbot", source_lane=""), s)
    assert not s1_lane.is_s1(SimpleNamespace(source_engine="s1"), _settings(s1_lane_enabled=False))


def test_s1_signals_carry_the_persisted_lane_tag(monkeypatch):
    from engine.signal_processor import SignalProcessor
    # The fallback assertion concerns single-engine mode, not the user's combined-mode .env.
    monkeypatch.setattr("engine.signal_processor.configured_mode", lambda *_a: "s1")

    assert SignalProcessor._signal_source_lane(SimpleNamespace(source_engine="s1", source_lane="")) == "S1"
    assert SignalProcessor._signal_source_lane(SimpleNamespace(source_engine="ema", source_lane="")) == ""


# ---------------------------------------------------------------- guard and stops


@pytest.mark.parametrize("tf,when,score,expected", [
    ("1min", "10:00", 70, 5.0), ("5min", "10:00", 70, 10.0), ("15min", "10:00", 70, 15.0),
    ("15min", "14:45", 70, 10.0), ("5min", "14:45", 70, 5.0), ("1min", "14:45", 70, 5.0),
    ("5min", "10:00", 90, 15.0), ("15min", "10:00", 90, 15.0), ("5min", "14:45", 90, 10.0),
])
def test_premium_guard_by_timeframe_time_and_strength(tf, when, score, expected):
    t = datetime.strptime(f"2026-09-24 {when}", "%Y-%m-%d %H:%M")
    assert s1_lane.premium_guard_pct(tf, t, score, _settings()) == expected


def test_premium_stop_is_the_tighter_of_the_mapped_index_stop_and_the_guard():
    assert s1_lane.premium_stop_distance(100.0, 23.4, 0.5, 10.0) == pytest.approx(10.0)   # guard binds
    assert s1_lane.premium_stop_distance(100.0, 12.0, 0.5, 10.0) == pytest.approx(6.0)    # index stop binds
    assert s1_lane.premium_stop_distance(100.0, 12.0, -0.5, 10.0) == pytest.approx(6.0)   # PE delta


# ---------------------------------------------------------------- exit decisions


def _trade(**kw):
    base = dict(direction="LONG", entry_price=100.0, initial_stop=90.0, underlying_entry=23400.0,
                underlying_stop=23376.6, entry_spot=23400.0, timeframe="5min",
                signal_time=datetime(2026, 9, 24, 10, 0))
    base.update(kw)
    return SimpleNamespace(**base)


def test_index_sl1_exits():
    d = s1_lane.exit_decision(_trade(), spot=23376.0, premium=90.5, settings=_settings())
    assert d.exit and d.reason == "S1_SL1"


def test_t1_locks_the_index_stop_at_entry_plus_0_7r_then_exits_on_the_pullback():
    t, s = _trade(), _settings()
    risk = 23.4
    assert not s1_lane.exit_decision(t, spot=23400 + 2 * risk + 1, premium=124.0, settings=s).exit
    assert t.s1_t1_locked
    wide = _settings(s1_lane_giveback_neutral=95.0)
    held = s1_lane.exit_decision(t, spot=23400 + 0.7 * risk + 1, premium=113.0, settings=wide)
    assert not held.exit and held.index_stop == pytest.approx(23400 + 0.7 * risk)
    out = s1_lane.exit_decision(t, spot=23400 + 0.7 * risk - 1, premium=112.0, settings=s)
    assert out.exit and out.reason == "S1_T1_LOCK"


def test_short_side_mirrors():
    t = _trade(direction="SHORT", underlying_stop=23423.4)
    assert s1_lane.exit_decision(t, spot=23424.0, premium=90.0, settings=_settings()).reason == "S1_SL1"


def test_engine_exit_closes_the_trade():
    d = s1_lane.exit_decision(_trade(), spot=23405.0, premium=102.0,
                              engine_exit={"reason": "Momentum Exit"}, settings=_settings())
    assert d.exit and d.reason == "S1_EXIT" and "Momentum" in d.detail


@pytest.mark.parametrize("trend,holds_at,exits_at", [
    (1, 115.0, 114.9),    # trend with the trade: 25% giveback of a 20-point peak gain
    (0, 118.0, 117.9),    # no clear trend: 10%
    (-1, 120.0, 119.9),   # trend against: 0% - the first tick below the peak
])
def test_profit_lock_giveback_follows_the_s1_trend(trend, holds_at, exits_at):
    t, s = _trade(), _settings()
    assert not s1_lane.exit_decision(t, spot=23405.0, premium=108.0, settings=s, trend=trend if trend >= 0 else 0).exit
    assert not s1_lane.exit_decision(t, spot=23410.0, premium=120.0, settings=s, trend=trend).exit   # peak gain 20 (armed)
    assert not s1_lane.exit_decision(t, spot=23408.0, premium=holds_at, settings=s, trend=trend).exit
    out = s1_lane.exit_decision(t, spot=23407.0, premium=exits_at, settings=s, trend=trend)
    assert out.exit and out.reason == "S1_PROFIT_LOCK"


def test_a_clear_trend_against_cuts_the_trade_before_it_earns_its_lock():
    s = _settings()
    out = s1_lane.exit_decision(_trade(), spot=23398.0, premium=97.0, settings=s, trend=-1)
    assert out.exit and out.reason == "S1_TREND_AGAINST"
    armed = _trade(s1_peak_premium=112.0)   # peak gain 12 >= 1R of 10: the lock manages it
    assert s1_lane.exit_decision(armed, spot=23405.0, premium=111.9, settings=s, trend=-1).reason == "S1_PROFIT_LOCK"


def test_trend_with_the_trade_makes_it_a_runner_and_widens_the_premium_trail():
    s = _settings()
    t = _trade(s1_peak_premium=140.0)
    with_trend = s1_lane.exit_decision(t, spot=23430.0, premium=138.0, settings=s, trend=1, regime="RANGING")
    neutral = s1_lane.exit_decision(_trade(s1_peak_premium=140.0), spot=23430.0, premium=138.0, settings=s, trend=0, regime="RANGING")
    assert with_trend.runner and not neutral.runner
    assert with_trend.premium_stop == pytest.approx(140.0 * 0.85)      # 5m rung +1 with the trend
    assert neutral.premium_stop == pytest.approx(140.0 * 0.90)
    capped = s1_lane.exit_decision(_trade(s1_peak_premium=140.0), spot=23430.0, premium=138.0, settings=s, trend=1, regime="TRENDING_UP")
    assert capped.premium_stop == pytest.approx(140.0 * 0.90)          # clear trend regime caps at 10%


def test_engine_exit_lookup_matches_the_signal_and_waits_for_the_bar_close():
    sig = SimpleNamespace(source_engine="s1", timestamp=datetime(2026, 9, 24, 10, 0), signal_type="BUY",
                          s1_exit_reason="TSL", s1_exit_time=datetime(2026, 9, 24, 10, 20), s1_exit_price=23420.0)
    other = SimpleNamespace(source_engine="s1", timestamp=datetime(2026, 9, 24, 9, 30), signal_type="BUY",
                            s1_exit_reason="Hard SL1", s1_exit_time=datetime(2026, 9, 24, 9, 40), s1_exit_price=1.0)
    result = {"signals": [other, sig]}
    t = _trade()
    assert s1_lane.engine_exit_for(t, result, now=datetime(2026, 9, 24, 10, 22)) is None       # 10:20 bar open
    got = s1_lane.engine_exit_for(t, result, now=datetime(2026, 9, 24, 10, 25))
    assert got and got["reason"] == "TSL"
    assert s1_lane.engine_exit_for(_trade(direction="SHORT"), result, now=datetime(2026, 9, 24, 11, 0)) is None


# ---------------------------------------------------------------- scanner hooks (runtime)


def _scanner(monkeypatch, **attrs):
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    for k, v in attrs.items():
        setattr(sc, k, v)
    return Scanner, sc


def _gate_scanner(monkeypatch, quality_calls):
    Scanner, sc = _scanner(monkeypatch, mode="REAL")
    monkeypatch.setattr(Scanner, "_auto_entry_resource_blockers", lambda self: [])
    monkeypatch.setattr(Scanner, "_session_bias_standaside", lambda self, c: (False, ""))
    monkeypatch.setattr(Scanner, "_unanimous_book_blocker", lambda self, c: "")
    monkeypatch.setattr(Scanner, "_candidate_quality_context", lambda self, c: ("RANGING", True, True, False))
    monkeypatch.setattr(Scanner, "_live_entry_data_quality", lambda self, c: {"allowed": True})
    monkeypatch.setattr(Scanner, "_option_chain_entry_freshness_blocker", lambda self, c, r: "")

    def quality(self, candidate, **kw):
        quality_calls.append(candidate)
        return SimpleNamespace(allowed=False, reason="B+ floor", reason_code="BPLUS", checks={})

    monkeypatch.setattr(Scanner, "_quality_verdict_for_candidate", quality)
    sc._late_entry_scalp_active = lambda c: False
    sc._passes_timeframe_entry_policy = lambda c: True
    sc._fifteen_min_choppy_scalp_context = lambda c, r: (True, "")
    sc._gate_block = lambda c, msg: False
    sc.log_signal_decision_once = lambda *a, **k: None
    return Scanner, sc


def _candidate(**kw):
    base = dict(instrument="NIFTY", timeframe="5min", direction="LONG", inst_type="OPT", action="ENTRY",
                confidence=0.4, grade="C", adx_value=12.0, source_engine="s1", source_lane="S1")
    base.update(kw)
    return SimpleNamespace(**base)


def test_live_gate_admits_s1_without_the_quality_verdict_but_not_other_engines(monkeypatch):
    calls = []
    _, sc = _gate_scanner(monkeypatch, calls)
    s1 = _candidate()
    assert sc._passes_live_trade_ready_gate(s1) is True
    assert calls == [] and s1.s1_lane is True

    ut = _candidate(source_engine="utbot", source_lane="")
    assert sc._passes_live_trade_ready_gate(ut) is False
    assert calls == [ut]


def test_live_gate_s1_still_honours_the_safety_checks_before_it(monkeypatch):
    from scanner import Scanner

    calls = []
    _, sc = _gate_scanner(monkeypatch, calls)
    monkeypatch.setattr(Scanner, "_live_entry_data_quality", lambda self, c: {"allowed": False, "reason": "stale"})
    assert sc._passes_live_trade_ready_gate(_candidate()) is False
    monkeypatch.setattr(Scanner, "_live_entry_data_quality", lambda self, c: {"allowed": True})
    monkeypatch.setattr(Scanner, "_auto_entry_resource_blockers", lambda self: ["RAM low"])
    assert sc._passes_live_trade_ready_gate(_candidate()) is False


def test_s1_keeps_chart_cooldown_but_honors_actual_loss_skip_and_daily_cap(monkeypatch):
    Scanner, sc = _scanner(monkeypatch, mode="REAL", max_trades_per_index=3, max_consecutive_losses=2,
                           index_cooldown_minutes=30)
    sc._reset_daily_counters = lambda: None
    sc._trades_today = {"NIFTY": 1}
    sc._losses_today = {"NIFTY": 5}
    sc._last_exit_time = {"NIFTY": datetime.now()}
    sc._clear_loss_streak_lanes = lambda inst: None
    sc._dynamic_trade_budget = lambda inst: (1, "range day")
    ok, why = sc._can_trade_instrument("NIFTY", datetime.now(), s1_lane=True)
    assert not ok and "Same-index loss suspension" in why
    assert sc._losses_today["NIFTY"] == 0
    ok, why = sc._can_trade_instrument("NIFTY", datetime.now(), s1_lane=True)
    assert ok and why == ""
    sc._trades_today = {"NIFTY": 3}
    ok, why = sc._can_trade_instrument("NIFTY", datetime.now(), s1_lane=True)
    assert not ok and "hard cap" in why
    sc._trades_today = {"NIFTY": 1}
    ok, _ = sc._can_trade_instrument("NIFTY", datetime.now(), s1_lane=False)
    assert not ok   # the other engines still get the day-character budget / loss skip


def test_premium_stop_for_an_s1_candidate_is_its_guard(monkeypatch):
    _, sc = _scanner(monkeypatch)
    cand = _candidate(s1_guard_pct=10.0, grade="A", confidence=0.8)
    assert sc._option_premium_stop_distance_for_candidate(150.0, cand) == pytest.approx(15.0)


def test_s1_forming_bar_gets_one_fixed_buffer(monkeypatch):
    _, sc = _scanner(monkeypatch)
    sc._get_trade_regime = lambda c, default="UNKNOWN": "RANGING"
    assert sc._live_signal_stabilization_requirements(_candidate(timeframe="15min", instrument="MIDCPNIFTY")) == (15.0, 1, False)
    assert sc._regrade_matured_candidate(_candidate()) is None


def _geometry(stop_pct):
    ue = 23400.0
    return ExecutionGeometry(
        instrument="NIFTY", direction="LONG", inst_type="OPT",
        underlying_entry=ue, underlying_stop=ue * (1 - stop_pct / 100), underlying_target=ue * (1 + 4 * stop_pct / 100),
        premium_entry=100.0, premium_stop=90.0, premium_target=140.0,
        observed_at=datetime(2026, 9, 24, 10, 0).isoformat(), source="broker_ltp", verified=True,
    )


def test_entry_policy_lets_the_s1_stop_and_missing_forecast_through_only_for_s1():
    geometry = _geometry(0.20)
    forecast = ExpectedMoveForecast(
        instrument="NIFTY", direction="LONG", timeframe="5min", horizon_minutes=30, horizon_bars=6,
        expected_favorable_move_pct=0.0, q10_pct=0.0, q25_pct=0.0, q50_pct=0.0, uncertainty_pct=0.0,
        probability_meeting_floor=0.0, floor_pct=0.1, sample_count=0, generated_at="", data_as_of="",
        available=False, reason="no model",
    )
    common = dict(min_rr=1.5, expected_move_floor_pct=0.1, max_underlying_stop_pct=0.15,
                  max_forecast_age_seconds=90, max_geometry_age_seconds=10,
                  checked_at=datetime(2026, 9, 24, 10, 0), phase="entry")
    plain = evaluate_final_entry_policy(geometry, forecast, **common)
    lane = evaluate_final_entry_policy(geometry, forecast, underlying_stop_cap_override_pct=0.25,
                                       require_forecast=False, **common)
    blocked = {k for k, ok in plain.checks if not ok}
    assert {"underlying_stop_cap", "forecast_evidence"} <= blocked
    lane_checks = dict(lane.checks)
    assert lane_checks["underlying_stop_cap"] is True
    assert "forecast_evidence_advisory" in lane_checks
    assert lane.allowed == all(ok for k, ok in lane.checks if not k.endswith("_advisory"))
    too_wide = evaluate_final_entry_policy(_geometry(0.30), forecast, underlying_stop_cap_override_pct=0.25,
                                           require_forecast=False, **common)
    assert dict(too_wide.checks)["underlying_stop_cap"] is False


# ---------------------------------------------------------------- monitor branch (runtime)


def _monitor(monkeypatch, trade, spot, premium, engine_signals=(), auto_mode=True, notes=None, engine_result=None,
             observed_ticks=()):
    from scanner import Scanner

    Scanner_, sc = _scanner(monkeypatch, mode="HISTORICAL", auto_mode=auto_mode)
    closed, updated, other = [], [], []
    sc.on_notification = (lambda msg, kind: notes.append(msg)) if notes is not None else None
    sc._reset_daily_counters = lambda: None
    sc.trades = SimpleNamespace(open_trades={"T1": trade}, closed_trades=[],
                                update_trade=lambda tid, price, stop, **kw: updated.append((tid, price, stop, kw)))
    sc.candles = SimpleNamespace(get_latest_price=lambda inst: spot, get_max_timestamp=lambda: datetime(2026, 9, 24, 10, 30))
    sc._trade_contract_tick_path = lambda t: list(observed_ticks)
    sc._get_live_premium_cached = lambda t, allow_blocking=False: premium
    sc._active_option_decision_mark = lambda t, px, prem: (premium, "test", True)
    sc._get_trade_regime = lambda t, default="UNKNOWN": "RANGING"
    sc._commit_trade_contract_tick_path = lambda t: None
    sc._record_trade_manager_close = lambda *a: None
    sc.log_event = lambda *a, **k: None
    sc.mtf = SimpleNamespace(_result_cache={"NIFTY_5min": engine_result or {"signals": list(engine_signals)}})
    sc._close_and_record = lambda tid, px, reason, exit_time=None: closed.append(reason)

    def other_guard(*a, **k):
        other.append("tf15")
        return True, "other engine guard"

    sc._fifteen_min_one_min_support_exit_decision = other_guard
    sc._update_active_trades(allow_blocking=False)
    return closed, updated, other


def _open_trade(**kw):
    base = dict(id="T1", instrument="NIFTY", timeframe="5min", direction="LONG", inst_type="OPT", status="OPEN",
                execution_status="", pending_exit=False, entry_price=100.0, current_stop=90.0, initial_stop=90.0,
                entry_spot=23400.0, underlying_entry=23400.0, underlying_stop=23376.6, instrument_multiplier=0.5,
                source_engine="s1", source_lane="S1", signal_time=datetime(2026, 9, 24, 10, 0),
                unrealized_pnl=0.0, pnl=0.0, lots=1, lot_size=65, quantity=65, peak_pnl=0.0, current_price=100.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_monitor_closes_an_s1_trade_on_its_index_stop_without_other_guards(monkeypatch):
    closed, updated, other = _monitor(monkeypatch, _open_trade(), spot=23370.0, premium=96.0)
    assert closed == ["S1_SL1"] and other == [] and updated == []


def test_monitor_hands_the_s1_stop_to_trade_manager_when_holding(monkeypatch):
    closed, updated, other = _monitor(monkeypatch, _open_trade(), spot=23405.0, premium=102.0)
    assert closed == [] and other == []
    # the stop never loosens: max(initial 90, index SL mapped, premium trail 102 x 0.90)
    assert updated and updated[0][2] == pytest.approx(max(90.0, 100.0 + (23376.6 - 23400.0) * 0.5, 102.0 * 0.90))


def test_monitor_follows_the_s1_engine_exit(monkeypatch):
    sig = SimpleNamespace(source_engine="s1", timestamp=datetime(2026, 9, 24, 10, 0), signal_type="BUY",
                          s1_exit_reason="Momentum Exit", s1_exit_time=datetime(2026, 9, 24, 10, 20), s1_exit_price=0.0)
    closed, _, other = _monitor(monkeypatch, _open_trade(), spot=23405.0, premium=102.0, engine_signals=[sig])
    assert closed == ["S1_EXIT"] and other == []


def test_monitor_keeps_the_other_engines_on_their_own_guards(monkeypatch):
    closed, _, other = _monitor(monkeypatch, _open_trade(source_engine="utbot", source_lane=""), spot=23370.0, premium=96.0)
    assert other == ["tf15"] and closed == ["TF15_FAST_REVERSAL"]


# ---------------------------------------------------------------- settings and dashboard


def test_every_s1_setting_is_declared_and_dashboard_editable():
    fields = {k for k in Settings.model_fields if k.startswith("s1_")}
    assert set(s1_lane.S1_SETTING_SPEC) == fields
    js = (ROOT / "dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    block = js[js.index("const S1_SETTINGS_FIELDS"):js.index("];", js.index("const S1_SETTINGS_FIELDS"))]
    assert set(re.findall(r"\['(s1_[a-z0-9_]+)'", block)) == fields


def test_server_validates_and_forwards_s1_settings():
    import dashboard.server as srv

    ok = {"cmd": "configure", "s1_candle_type": "heikin_ashi", "s1_min_score": 65, "s1_sl_mode": "atr", "s1_lane_enabled": True,
          "s1_lane_guard_late_after": "14:15", "s1_max_consec_loss_sensex": 4}
    assert srv._valid_command_payload(dict(ok)) is None
    assert srv._valid_command_payload({"cmd": "configure", "s1_candle_type": "standard"}) is None
    assert srv._valid_command_payload({"cmd": "configure", "s1_candle_type": "renko"}) == "invalid_choice:s1_candle_type"
    assert srv._valid_command_payload({"cmd": "configure", "s1_sl_mode": "wide"}) == "invalid_choice:s1_sl_mode"
    assert srv._valid_command_payload({"cmd": "configure", "s1_sl_max_pct": 9}) == "out_of_range:s1_sl_max_pct"
    assert srv._valid_command_payload({"cmd": "configure", "s1_exit_conf_bars": 2.5}) == "invalid_integer:s1_exit_conf_bars"
    server_src = (ROOT / "dashboard/server.py").read_text(encoding="utf-8")
    assert "**{name: msg.get(name) for name in _S1_SETTING_SPEC}" in server_src


def test_broadcast_config_carries_the_s1_settings():
    values = s1_lane.dashboard_values(_settings(s1_min_score=66.0, s1_candle_type="standard"))
    assert values["s1_min_score"] == 66.0 and set(values) == set(s1_lane.S1_SETTING_SPEC)
    assert values["s1_candle_type"] == "standard"


def test_s1_candle_source_default_matches_manual_chart_and_validates_independently():
    from pydantic import ValidationError

    assert Settings.model_fields["s1_candle_type"].default == "heikin_ashi"
    s = Settings(s1_candle_type="standard", ut_use_heikin_ashi=False)
    assert s.s1_candle_type == "standard" and s.ut_use_heikin_ashi is False
    s = Settings(s1_candle_type="heikin_ashi", ut_use_heikin_ashi=False)
    assert s.s1_candle_type == "heikin_ashi" and s.ut_use_heikin_ashi is False
    assert Settings.model_validate(s.model_dump()).s1_candle_type == "heikin_ashi"
    with pytest.raises(ValidationError):
        Settings(s1_candle_type="renko")


def test_auto_mode_hands_the_stop_to_trade_manager_without_its_early_loss_cut(monkeypatch):
    _, updated, _ = _monitor(monkeypatch, _open_trade(), spot=23405.0, premium=102.0)
    assert updated and updated[0][3]["early_loss_cut_decision"] is False


def test_manual_mode_never_ticks_trade_manager_and_alerts_through_close_and_record(monkeypatch):
    closed, updated, other = _monitor(monkeypatch, _open_trade(), spot=23405.0, premium=102.0, auto_mode=False)
    assert updated == [] and closed == [] and other == []
    closed, updated, _ = _monitor(monkeypatch, _open_trade(), spot=23370.0, premium=96.0, auto_mode=False)
    assert closed == ["S1_SL1"] and updated == []
    # premium stop breached while the index stop still holds
    closed, updated, _ = _monitor(monkeypatch, _open_trade(current_stop=99.0), spot=23405.0, premium=98.0, auto_mode=False)
    assert closed == ["TRAILING_STOP"] and updated == []


def test_a_live_position_is_never_closed_while_the_scanner_runs_historical(monkeypatch):
    notes = []
    trade = _open_trade(is_live=True)
    closed, updated, other = _monitor(monkeypatch, trade, spot=23370.0, premium=96.0, notes=notes)
    assert closed == [] and updated == [] and other == []
    assert notes and "S1_SL1" in notes[0]


def test_a_chart_exit_inside_the_signal_bar_is_not_the_live_trades_exit():
    same_bar = SimpleNamespace(source_engine="s1", timestamp=datetime(2026, 9, 24, 10, 0), signal_type="BUY",
                               s1_exit_reason="Hard SL1", s1_exit_time=datetime(2026, 9, 24, 10, 0), s1_exit_price=1.0)
    assert s1_lane.engine_exit_for(_trade(), {"signals": [same_bar]}, now=datetime(2026, 9, 24, 11, 0)) is None


def test_equal_guard_percentages_keep_three_rungs():
    s = _settings(s1_lane_guard_pct_1min=10.0, s1_lane_guard_pct_5min=10.0, s1_lane_guard_pct_15min=15.0)
    late = datetime(2026, 9, 24, 14, 45)
    assert s1_lane.premium_guard_pct("15min", late, 70, s) == 10.0
    assert s1_lane.premium_guard_pct("5min", late, 70, s) == 10.0


def test_profit_lock_peak_survives_a_restart_through_the_persisted_rupee_peak():
    restored = _trade(peak_pnl=20.0 * 65, quantity=65)          # peak premium 120, no s1_peak_premium
    out = s1_lane.exit_decision(restored, spot=23405.0, premium=114.0, settings=_settings())
    assert out.exit and out.reason == "S1_PROFIT_LOCK"


def test_manual_execute_does_not_regrade_an_s1_signal(monkeypatch):
    from scanner import Scanner

    _, sc = _scanner(monkeypatch)
    monkeypatch.setattr(Scanner, "_passes_candidate_grade_preference", lambda self, c: (False, "grade A only"))
    assert sc._current_manual_admission_decision(_candidate(), lane="MANUAL") == (True, "", "")
    assert sc._current_manual_admission_decision(_candidate(source_engine="utbot", source_lane=""), lane="MANUAL")[0] is False


def test_geometry_rebase_keeps_the_s1_stop_up_to_its_own_cap(monkeypatch):
    _, sc = _scanner(monkeypatch, mode="REAL")
    sc._entry_policy_live_index_quote = lambda inst, at: (23400.0, "2026-09-24T10:00:00", "live")
    sc._entry_policy_underlying_stop_cap_pct = lambda inst, regime: 0.10
    holder = lambda **kw: SimpleNamespace(**dict(dict(
        instrument="NIFTY", direction="LONG", inst_type="OPT", underlying_entry=23400.0,
        underlying_stop=23400.0 * (1 - 0.002), underlying_target=23400.0 * (1 + 0.008),
        market_regime="RANGING", instrument_multiplier=0.5, source_engine="s1", source_lane="S1"), **kw))
    s1_geo = sc._entry_execution_geometry(holder(), (100.0, 90.0, 140.0), phase="broker_queue",
                                          observed_at=datetime(2026, 9, 24, 10, 0))
    assert (s1_geo.underlying_entry - s1_geo.underlying_stop) / 23400.0 * 100 == pytest.approx(0.20)
    other = sc._entry_execution_geometry(holder(source_engine="utbot", source_lane=""), (100.0, 90.0, 140.0),
                                         phase="broker_queue", observed_at=datetime(2026, 9, 24, 10, 0))
    assert (other.underlying_entry - other.underlying_stop) / 23400.0 * 100 == pytest.approx(0.10)


def test_s1_replay_keeps_the_rupee_price_path_without_the_live_guards(monkeypatch):
    import pandas as pd
    from engine.signal_processor import SignalProcessor

    idx = pd.date_range("2026-09-24 10:00", periods=20, freq="1min")
    prem = pd.DataFrame({"open": 100.0, "high": [101.0 + i * 0.2 for i in range(20)],
                         "low": 99.5, "close": [100.5 + i * 0.2 for i in range(20)]}, index=idx)
    sp = SignalProcessor.__new__(SignalProcessor)
    res = sp._simulate_actual_option_exit(prem, idx[0], idx[-1] + pd.Timedelta(minutes=1), 90.0, 4.0,
                                          natural_sl_distance=10.0, max_sl_pct=0.10, qty=65, live_exits=False)
    assert res and any(row["pnl"] != 0 for row in res["price_path"])
