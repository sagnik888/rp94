"""S1+S2 Dual lanes (engine/s12_lane.py): identity, settings spec, native admission, the
premium catastrophe cap, admission levels, the exit order (premium cap, index stop, engine
EXIT, square-off) and D1/D2 independence."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from engine import s12_engine as E
from engine import s12_lane as L
from engine.s1_lane import S1ExitDecision
from engine.s12dual.engine import hard_stop_price, nearest_stop
from tests.s12dual.generate_demo import make
from tests.test_s12_engine_core import host_frame

D1_ID = "S1|NSE:NIFTY|5|2026-10-09T10:00:00+05:30|TRADE"
D2_ID = "S2|NSE:NIFTY|5|2026-10-09T10:00:00+05:30|TRADE"


def cfg(**values):
    return SimpleNamespace(**{"options_sl_pct": 15.0, "s12_options_operator_cap_pct": 10.0, **values})


def signal(**values):
    base = dict(source_engine="s1s2_dual", source_lane="D1", s12_lane="D1", signal_type="BUY", price=25000.0,
                s12_stop=24950.0, s12_stop_pct=0.2, s12_r_ladder=[2.0, 3.0, 4.0], s12_t1=25100.0,
                s12_t2=25150.0, s12_t3=25200.0, timeframe="5min", instrument="NIFTY", s12_trade_id=D1_ID,
                s12_event_id=D1_ID.replace("TRADE", "SETUP"), s12_score=72.0, s12_setup="Trend",
                s12_forming=False, s12_fill_cancel="", s12_exit_reason="", s12_exit_time=None,
                timestamp=datetime(2026, 10, 9, 10, 0))
    base.update(values)
    return SimpleNamespace(**base)


def trade(**values):
    base = dict(s12_lane="D1", source_lane="D1", exit_policy_owner="D1", direction="LONG", instrument="NIFTY",
                timeframe="5min", inst_type="OPT", entry_price=200.0, s12_trade_id=D1_ID, s12_stop=24950.0,
                s12_stop_initial=24950.0, s12_stop_pct=0.2, s12_r_ladder=[2.0, 3.0, 4.0],
                signal_time=datetime(2026, 10, 9, 10, 0), entry_time=datetime(2026, 10, 9, 10, 5, 1),
                quality_verdict={})
    base.update(values)
    return SimpleNamespace(**base)


NOON = datetime(2026, 10, 9, 12, 0)


# ─────────────────────────────────────────────────────────────── identity and settings


def test_lane_identity_comes_from_s12_fields_never_from_the_s1_tags():
    assert L.lane_of(signal()) == "D1"
    assert L.lane_of(SimpleNamespace(source_lane="D2")) == "D2"
    assert L.lane_of(SimpleNamespace(exit_policy_owner="D1")) == "D1"
    assert L.lane_of(SimpleNamespace(source_engine="s1s2_dual", s12_trade_id=D2_ID)) == "D2"
    for other in ("S1", "UTS", "A", "B", "QP", "GENERIC"):
        assert L.lane_of(SimpleNamespace(source_lane=other, exit_policy_owner=other)) == ""
    assert L.lane_of(SimpleNamespace(source_engine="s1")) == "" and L.lane_of(None) == ""
    off = cfg(s12_d1_enabled=False)
    assert L.is_lane(signal(), off) is True and L.is_s12(signal(), off) is True
    assert L.admission_enabled(signal(), off) is False
    assert L.admission_enabled(signal(s12_lane="D2", source_lane="D2"), off) is True


def test_setting_spec_covers_every_engine_and_lane_setting():
    from engine.s1_lane import S1_SETTING_SPEC
    from engine.uts_lane import UTS_SETTING_SPEC

    assert set(E.SETTING_FIELDS) | set(L.LANE_SETTING_SPEC) == set(L.S12_SETTING_SPEC)
    assert not set(L.S12_SETTING_SPEC) & (set(S1_SETTING_SPEC) | set(UTS_SETTING_SPEC))
    for name, spec in L.S12_SETTING_SPEC.items():
        assert name.startswith("s12_") and not name.startswith(("s1_", "uts_", "dual_lane_"))
        kind = spec[0]
        assert kind in {"float", "int", "bool", "time", "enum"}
        default = L.SETTING_DEFAULTS[name]
        if kind in {"float", "int"}:
            assert spec[1] < spec[2] and spec[1] <= default <= spec[2]
        elif kind == "enum":
            assert default in spec[1]
        elif kind == "bool":
            assert isinstance(default, bool)
        else:
            assert L._hhmm(default, -1) >= 0
    assert L.S12_SETTING_SPEC["s12_timeframes"] == ("enum", L.TIMEFRAME_CHOICES)
    assert L.SETTING_DEFAULTS["s12_signal_only"] is False
    assert L.SETTING_DEFAULTS["s12_timeframes"] == "1min,5min,15min"
    assert L.SETTING_DEFAULTS["s12_square_off"] == "15:15"


def test_dashboard_values_report_the_current_or_default_value():
    values = L.dashboard_values(cfg(s12_d2_orb_minutes=45, s12_signal_only=True))
    assert set(values) == set(L.S12_SETTING_SPEC)
    assert values["s12_d2_orb_minutes"] == 45 and values["s12_signal_only"] is True
    assert values["s12_d1_stop_pct"] == 0.1 and values["s12_timeframes"] == "1min,5min,15min"


def test_lane_switches_and_values():
    assert L.enabled_timeframes(cfg()) == ("1min", "5min", "15min")
    assert L.timeframe_enabled("5min", cfg()) and L.timeframe_enabled("1min", cfg())
    assert L.timeframe_enabled("1min", cfg(s12_timeframes="1min,5min,15min"))
    assert L.enabled_timeframes(cfg(s12_timeframes="3min")) == ("1min", "5min", "15min")
    assert L.signal_only(cfg()) is False and L.signal_only(cfg(s12_signal_only=True)) is True
    assert L.square_off(cfg()) == "15:15" and L.square_off(cfg(s12_square_off="25:99")) == "15:15"
    assert L.square_off(cfg(s12_square_off="15:00:00")) == "15:00"
    assert (L.sl_max_pct(cfg()), L.min_entry_rr(cfg()), L.target_rr(cfg())) == (1.5, 0.5, 4.0)
    assert L.intrabar_enabled(cfg()) is False and L.intrabar_wait_seconds(signal(), cfg()) == 0.0
    assert L.entry_cutoff("5min", cfg()) == "15:10"


# ─────────────────────────────────────────────────────────────── premium cap


@pytest.mark.parametrize("instrument,timeframe,expected", [
    ("NIFTY", "1min", 5.0), ("NIFTY", "5min", 10.0), ("NIFTY", "15min", 10.0),
    ("BANKNIFTY", "1min", 5.0), ("BANKNIFTY", "5min", 10.0), ("BANKNIFTY", "15min", 10.0),
    ("SENSEX", "1min", 5.0), ("SENSEX", "5min", 10.0), ("SENSEX", "15min", 10.0),
    ("MIDCPNIFTY", "1min", 3.5), ("MIDCPNIFTY", "5min", 7.0), ("MIDCPNIFTY", "15min", 10.0),
])
def test_premium_cap_uses_the_uts_tiers_by_timeframe(instrument, timeframe, expected):
    obj = signal(instrument=instrument, timeframe=timeframe)
    assert L.premium_cap_pct(obj, cfg()) == expected
    assert L.premium_cap_pct(obj, cfg(options_sl_pct=8.0)) == min(8.0, expected)
    assert L.premium_stop_distance(200.0, 50.0, 0.5, cfg(), setup=obj, timeframe=timeframe) == pytest.approx(2 * expected)


@pytest.mark.parametrize("instrument,expected", [
    ("NIFTY", 5.0), ("BANKNIFTY", 5.0), ("SENSEX", 5.0), ("MIDCPNIFTY", 3.5),
])
def test_one_minute_native_burst_and_spike_keep_the_index_tight_tier(instrument, expected):
    burst = signal(instrument=instrument, timeframe="1min", s12_burst=True)
    spike = signal(instrument=instrument, timeframe="1min", volume_spike_entry=True)

    assert L.premium_cap_pct(burst, cfg()) == expected
    assert L.premium_cap_pct(spike, cfg()) == expected


def test_premium_cap_approved_at_admission_is_frozen():
    approved = L.entry_verdict(signal(timeframe="15min"), cfg(options_sl_pct=15.0)).to_dict()
    assert approved["checks"]["s12_premium_cap_pct"] == 10.0
    held = trade(timeframe="5min", quality_verdict=approved)
    assert L.premium_cap_pct(held, cfg(options_sl_pct=15.0)) == 10.0
    assert L.premium_cap_pct(held, cfg(options_sl_pct=8.0)) == 8.0


def test_s12_option_cap_does_not_lower_global_cap_for_other_engines():
    from config.settings import Settings
    from engine import uts_lane
    from pydantic import ValidationError

    settings = Settings(_env_file=None)
    assert settings.options_sl_pct == 15.0
    assert L.premium_cap_pct(signal(timeframe="15min"), settings) == 10.0
    assert uts_lane.premium_cap_tiers(signal(timeframe="15min")) == (5.0, 10.0, 15.0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, s12_options_operator_cap_pct=10.01)


# ─────────────────────────────────────────────────────────────── admission


def test_engine_signal_is_admitted_natively():
    verdict = L.entry_verdict(signal(), cfg())
    assert verdict.allowed and verdict.reason_code == "S12_NATIVE_ENTRY"
    assert verdict.route == "s12_native" and verdict.policy_version == L.POLICY_VERSION == "s12_native.v1"
    assert verdict.checks["grade_filter_enabled"] is False
    assert verdict.checks["s12_lane"] == "D1" and verdict.checks["s12_trade_id"] == D1_ID
    assert verdict.checks["s12_premium_cap_pct"] == 10.0
    assert verdict.checks["s12_premium_cap_profile"] == "standard.v1"
    assert L.entry_verdict(signal(instrument="MIDCPNIFTY"), cfg()).checks["s12_premium_cap_profile"] == "midcap.v1"


@pytest.mark.parametrize("values,settings,code", [
    ({"s12_lane": "", "source_lane": "", "s12_trade_id": ""}, {}, "S12_UNKNOWN_LANE"),
    ({}, {"s12_d1_enabled": False}, "S12_LANE_DISABLED"),
    ({"s12_lane": "D2", "source_lane": "D2"}, {"s12_d2_enabled": False}, "S12_LANE_DISABLED"),
    ({"timeframe": "1min"}, {"s12_timeframes": "5min,15min"}, "S12_TIMEFRAME_DISABLED"),
    ({"s12_forming": True}, {}, "S12_FORMING_BAR"),
    ({"s12_stop": 25010.0}, {}, "S12_BAD_LEVELS"),
    ({"signal_type": "SELL"}, {}, "S12_BAD_LEVELS"),
    ({"price": 0.0}, {}, "S12_BAD_LEVELS"),
    ({"s12_stop": 24500.0}, {}, "S12_STOP_TOO_WIDE"),
    ({"s12_fill_cancel": "INTRADAY_CUTOFF"}, {}, "S12_D1_FILL_CANCELLED"),
])
def test_sanity_checks_refuse_bad_signals(values, settings, code):
    verdict = L.entry_verdict(signal(**values), cfg(**settings))
    assert not verdict.allowed and verdict.reason_code == code


def test_d2_signal_ignores_the_d1_fill_rule_and_wider_stops_follow_the_ceiling():
    d2 = signal(s12_lane="D2", source_lane="D2", s12_trade_id=D2_ID, s12_fill_cancel="INTRADAY_CUTOFF",
                s12_stop=24700.0)
    assert L.entry_verdict(d2, cfg()).allowed
    assert L.entry_verdict(d2, cfg(s12_sl_max_pct=1.0)).reason_code == "S12_STOP_TOO_WIDE"


def test_signal_whose_engine_exit_has_closed_is_refused_only_with_a_clock():
    exited = signal(s12_exit_reason="INITIAL STOP", s12_exit_time=datetime(2026, 10, 9, 10, 20))
    assert L.entry_verdict(exited, cfg()).allowed
    assert L.entry_verdict(exited, cfg(), now=datetime(2026, 10, 9, 10, 24)).allowed
    late = L.entry_verdict(exited, cfg(), now=datetime(2026, 10, 9, 10, 25))
    assert not late.allowed and late.reason_code == "S12_ENGINE_EXITED"
    assert L.entry_verdict(signal(action="EXIT", s12_forming=True), cfg()).allowed


def test_d1_levels_are_re_measured_from_the_actual_entry():
    levels = L.admission_levels(signal(), 25012.3, cfg())
    stop = nearest_stop(hard_stop_price(25012.3, True, 0.2), 0.05, True)
    risk = 25012.3 - stop
    assert levels["s12_stop"] == levels["s12_stop_initial"] == stop
    assert (levels["s12_t1"], levels["s12_t2"], levels["s12_t3"]) == pytest.approx(
        (25012.3 + 2 * risk, 25012.3 + 3 * risk, 25012.3 + 4 * risk))
    assert levels["s12_stop_pct"] == 0.2 and levels["s12_entry_spot"] == 25012.3
    assert (levels["s12_stop"], levels["s12_t1"]) == E.d1_levels(25012.3, True, 0.2, [2, 3, 4])[:2]
    short = L.admission_levels(signal(signal_type="SELL", s12_stop=25050.0), 24990.0, cfg())
    assert short["s12_stop"] == nearest_stop(hard_stop_price(24990.0, False, 0.2), 0.05, False) > 24990.0


def test_d2_levels_keep_the_package_stop_and_targets():
    d2 = signal(s12_lane="D2", source_lane="D2", s12_stop=24900.0, s12_t1=25200.0, s12_t2=25300.0, s12_t3=25400.0)
    levels = L.admission_levels(d2, 25010.0, cfg())
    assert (levels["s12_stop"], levels["s12_t1"], levels["s12_t2"], levels["s12_t3"]) == (24900.0, 25200.0, 25300.0, 25400.0)
    assert levels["s12_stop_pct"] == pytest.approx(110.0 / 25010.0 * 100.0)


# ─────────────────────────────────────────────────────────────── exits


def test_no_exit_keeps_the_trade_running_with_the_cap_as_broker_stop():
    decision = L.exit_decision(trade(), 25020.0, 195.0, now=NOON, settings=cfg())
    assert isinstance(decision, S1ExitDecision)
    assert not decision.exit and decision.runner is True and decision.target_premium == 0.0
    assert decision.index_stop == 24950.0 and decision.premium_stop == pytest.approx(180.0)


def test_premium_catastrophe_cap_comes_first():
    decision = L.exit_decision(trade(), 24900.0, 179.9, now=NOON, settings=cfg(),
                               engine_exit={"reason": "MOMENTUM EXIT"})
    assert decision.exit and decision.reason == "S12_PREMIUM_CAP"
    fifteen = L.exit_decision(trade(timeframe="15min"), 25020.0, 180.1, now=NOON, settings=cfg())
    assert not fifteen.exit and fifteen.premium_stop == pytest.approx(180.0)
    capped = L.exit_decision(trade(timeframe="15min"), 25020.0, 179.9, now=NOON, settings=cfg())
    assert capped.exit and capped.reason == "S12_PREMIUM_CAP"


def test_premium_cap_reads_the_contract_ticks_since_the_last_pass():
    ticks = [(181.0, datetime(2026, 10, 9, 11, 59, 50)), (179.5, datetime(2026, 10, 9, 11, 59, 55)),
             (190.0, datetime(2026, 10, 9, 11, 59, 58))]
    decision = L.exit_decision(trade(), 25020.0, 190.0, now=NOON, settings=cfg(), observed_contract_ticks=ticks)
    assert decision.exit and decision.reason == "S12_PREMIUM_CAP" and "179.50" in decision.detail
    before_entry = [(150.0, datetime(2026, 10, 9, 10, 4))]
    assert not L.exit_decision(trade(), 25020.0, 190.0, now=NOON, settings=cfg(),
                               observed_contract_ticks=before_entry).exit


def test_index_stop_cross_on_the_engine_stop_both_sides():
    long_exit = L.exit_decision(trade(), 24950.0, 195.0, now=NOON, settings=cfg())
    assert long_exit.exit and long_exit.reason == "S12_STOP" and long_exit.index_stop == 24950.0
    short = trade(direction="SHORT", s12_stop=25050.0, s12_stop_initial=25050.0)
    assert not L.exit_decision(short, 25049.0, 195.0, now=NOON, settings=cfg()).exit
    assert L.exit_decision(short, 25050.5, 195.0, now=NOON, settings=cfg()).reason == "S12_STOP"


def test_engine_stop_updates_ratchet_from_the_following_bar():
    view = {"lane": "D1", "trade_id": D1_ID, "side": "LONG",
            "stop_updates": [{"time": datetime(2026, 10, 9, 10, 10), "effective_from": datetime(2026, 10, 9, 10, 15),
                              "stop": 24990.0},
                             {"time": datetime(2026, 10, 9, 10, 15), "effective_from": datetime(2026, 10, 9, 10, 20),
                              "stop": 24970.0}],
            "milestones": {"T1": {"time": datetime(2026, 10, 9, 10, 10), "price": 25100.0}}}
    held = trade()
    early = L.exit_decision(held, 24980.0, 195.0, now=datetime(2026, 10, 9, 10, 14, 59), settings=cfg(), market=view)
    assert not early.exit and held.s12_stop == 24950.0 and not getattr(held, "s12_t1_hit", False)
    later = L.exit_decision(held, 24980.0, 195.0, now=datetime(2026, 10, 9, 10, 21), settings=cfg(), market=view)
    assert later.exit and later.reason == "S12_TRAIL_STOP" and later.index_stop == 24990.0
    assert held.s12_stop == 24990.0      # the lower 10:15 update never loosens it
    assert held.s12_t1_hit is True and not getattr(held, "s12_t2_hit", False)


def test_milestones_are_recorded_not_exits():
    view = {"lane": "D1", "trade_id": D1_ID, "side": "LONG", "stop_updates": [],
            "milestones": {k: {"time": datetime(2026, 10, 9, 10, 10), "price": 1.0} for k in ("T1", "T2", "T3")}}
    held = trade()
    decision = L.exit_decision(held, 25300.0, 260.0, now=NOON, settings=cfg(), market=view)
    assert not decision.exit and decision.runner is True
    assert held.s12_t1_hit and held.s12_t2_hit and held.s12_t3_hit


@pytest.mark.parametrize("engine_reason,reason", [
    ("MOMENTUM EXIT", "S12_MOMENTUM_EXIT"), ("INTRADAY EOD (CLOSE EST)", "S12_EOD"), ("INTRADAY EOD", "S12_EOD"),
    ("TRAIL/LOCK STOP", "S12_ENGINE_TRAIL_STOP"), ("T3 TARGET", "S12_T3"), ("SMART EXIT", "S12_SMART_EXIT"),
    ("EARLY LOSS CUT", "S12_EARLY_LOSS_CUT"), ("EARLY LOSS STOP", "S12_EARLY_LOSS_STOP"),
    ("SETUP_CANCELLED:INTRADAY_CUTOFF", "S12_SETUP_CANCELLED"), ("SOMETHING NEW", "S12_EXIT"),
])
def test_engine_exit_closes_the_trade_with_its_reason(engine_reason, reason):
    decision = L.exit_decision(trade(), 25020.0, 195.0, now=NOON, settings=cfg(), engine_exit={"reason": engine_reason})
    assert decision.exit and decision.reason == reason and engine_reason in decision.detail


def test_index_stop_comes_before_the_engine_exit_and_square_off_last():
    both = L.exit_decision(trade(), 24940.0, 195.0, now=datetime(2026, 10, 9, 15, 20), settings=cfg(),
                           engine_exit={"reason": "MOMENTUM EXIT"})
    assert both.reason == "S12_STOP"
    late = L.exit_decision(trade(), 25020.0, 195.0, now=datetime(2026, 10, 9, 15, 15), settings=cfg(),
                           engine_exit={"reason": "INTRADAY EOD"})
    assert late.reason == "S12_EOD"


def test_square_off_at_1515_and_for_anything_left_overnight():
    assert not L.exit_decision(trade(), 25020.0, 195.0, now=datetime(2026, 10, 9, 15, 14, 59), settings=cfg()).exit
    off = L.exit_decision(trade(), 25020.0, 195.0, now=datetime(2026, 10, 9, 15, 15), settings=cfg())
    assert off.exit and off.reason == "S12_SQUARE_OFF"
    assert L.exit_decision(trade(), 25020.0, 195.0, now=datetime(2026, 10, 9, 14, 30),
                           settings=cfg(s12_square_off="14:30")).reason == "S12_SQUARE_OFF"
    assert L.exit_decision(trade(), 25020.0, 195.0, now=datetime(2026, 10, 10, 9, 20), settings=cfg()).reason == "S12_SQUARE_OFF"


def test_d1_stop_falls_back_to_the_entry_spot_when_not_yet_stamped():
    held = trade(s12_stop=None, s12_stop_initial=None, underlying_entry=25000.0)
    decision = L.exit_decision(held, 24960.0, 195.0, now=NOON, settings=cfg())
    assert decision.index_stop == 24950.0 and held.s12_stop == 24950.0 and not decision.exit


# ─────────────────────────────────────────────────────────────── engine view and independence


def _result(exit_d1=None, exit_d2=None):
    def record(lane, trade_id, exit_info):
        return {"lane": lane, "trade_id": trade_id, "side": "LONG", "stop_updates": [], "milestones": {},
                "exit": exit_info}
    return {"source_engine": "s1s2_dual", "instrument": "NIFTY", "timeframe": "5min", "signals": [],
            "s12_trades": {D1_ID: record("D1", D1_ID, exit_d1), D2_ID: record("D2", D2_ID, exit_d2)}}


def test_engine_exit_waits_for_its_bar_to_close():
    result = _result(exit_d1={"reason": "MOMENTUM EXIT", "time": datetime(2026, 10, 9, 10, 30), "price": 25020.0})
    held = trade()
    assert L.engine_exit_for(held, result, now=datetime(2026, 10, 9, 10, 34, 59)) is None
    found = L.engine_exit_for(held, result, now=datetime(2026, 10, 9, 10, 35))
    assert found["reason"] == "MOMENTUM EXIT" and found["trade_id"] == D1_ID
    same_bar = _result(exit_d1={"reason": "INITIAL STOP", "time": datetime(2026, 10, 9, 10, 0), "price": 1.0})
    assert L.engine_exit_for(held, same_bar, now=NOON) is None
    assert L.engine_exit_for(held, {"error": "x"}, now=NOON) is None
    assert L.engine_exit_for(held, {**result, "source_engine": "s1"}, now=NOON) is None


def test_d1_and_d2_never_close_each_others_trades():
    result = _result(exit_d1={"reason": "MOMENTUM EXIT", "time": datetime(2026, 10, 9, 10, 30), "price": 25020.0})
    d1 = trade()
    d2 = trade(s12_lane="D2", source_lane="D2", exit_policy_owner="D2", s12_trade_id=D2_ID,
               s12_stop=24900.0, s12_stop_initial=24900.0)
    assert L.engine_exit_for(d1, result, now=NOON)["trade_id"] == D1_ID
    assert L.engine_exit_for(d2, result, now=NOON) is None
    d1_decision = L.exit_decision(d1, 24940.0, 195.0, now=NOON, settings=cfg(), result=result)
    d2_decision = L.exit_decision(d2, 24940.0, 195.0, now=NOON, settings=cfg(), result=result)
    assert d1_decision.exit and d1_decision.reason == "S12_STOP"
    assert not d2_decision.exit and d2_decision.index_stop == 24900.0
    crossed = trade(s12_lane="D2", source_lane="D2", exit_policy_owner="D2", s12_trade_id=D1_ID)
    assert L.trade_record(crossed, result) is None
    d1_view = L.trade_record(d1, result)
    assert L.apply_engine_view(d2, d1_view, NOON) is False and d2.s12_stop == 24900.0


@pytest.fixture(scope="module")
def adapter_result():
    m5 = host_frame(make(12), 5)
    clock = pd.Timestamp(m5.index[-1].date()) + pd.Timedelta(hours=15, minutes=30)
    return E.process(m5, "NIFTY", "5min", settings=SimpleNamespace(), as_of=clock, synchronous=True)


@pytest.mark.parametrize("lane", ["D1", "D2"])
def test_lane_follows_the_adapter_result_for_its_own_trade(adapter_result, lane):
    exited = [s for s in adapter_result["signals"] if s.s12_lane == lane and s.s12_exit_time is not None
              and not s.s12_exit_reason.startswith("SETUP_CANCELLED")]
    assert exited
    sig = exited[-1]
    held = SimpleNamespace(s12_lane=lane, source_lane=lane, exit_policy_owner=lane, instrument="NIFTY",
                           timeframe="5min", inst_type="OPT", entry_price=200.0,
                           direction="LONG" if sig.signal_type == "BUY" else "SHORT",
                           signal_time=sig.timestamp, entry_time=sig.timestamp, quality_verdict={},
                           s12_stop_pct=sig.s12_stop_pct, s12_r_ladder=sig.s12_r_ladder)
    for key, value in L.admission_levels(sig, sig.price, cfg()).items():
        setattr(held, key, value)
    record = L.trade_record(held, adapter_result)       # found from the signal bar, no trade id stamped yet
    assert record is not None and record["trade_id"] == sig.s12_trade_id
    close = pd.Timestamp(sig.s12_exit_time) + pd.Timedelta(minutes=5)
    assert L.engine_exit_for(held, adapter_result, now=close - pd.Timedelta(seconds=1)) is None
    decision = L.exit_decision(held, None, 200.0, now=close.to_pydatetime(), settings=cfg(), result=adapter_result)
    assert decision.exit and decision.reason == L.exit_reason_for(sig.s12_exit_reason)
    assert held.s12_trade_id == sig.s12_trade_id
    other = "D2" if lane == "D1" else "D1"
    mismatched = SimpleNamespace(**{**vars(held), "s12_lane": other, "source_lane": other, "exit_policy_owner": other})
    assert L.trade_record(mismatched, adapter_result) is None


def test_session_state_fields_are_s12_bookkeeping():
    assert all(name.startswith("s12_") for name in L.SESSION_STATE_FIELDS)
    assert set(L.admission_levels(signal(), 25000.0, cfg())) <= set(L.SESSION_STATE_FIELDS)
    for group in (L.SESSION_BOOL_FIELDS, L.SESSION_TIME_FIELDS, L.SESSION_TEXT_FIELDS, L.SESSION_LIST_FIELDS):
        assert group <= set(L.SESSION_STATE_FIELDS)
