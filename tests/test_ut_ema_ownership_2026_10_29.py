"""UT/EMA native exit ownership and session state survive switches and restoration."""
import json
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace

import pytest

from engine import s1_lane as lane


def _settings(**kwargs):
    return SimpleNamespace(scanner_engine="ut_ema", s1_lane_enabled=False, **kwargs)


def test_native_identity_and_frozen_owner_survive_scanner_switch():
    trade = SimpleNamespace(source_engine="ut_ema", source_lane="UTEMA")
    assert lane.is_lane(trade, _settings())
    assert lane.lane_tag(trade, _settings()) == "UTEMA"
    assert lane.stamp_exit_owner(trade, _settings()) == "UTEMA"
    trade.source_engine = ""
    switched = SimpleNamespace(scanner_engine="quad_prime", s1_lane_enabled=False)
    assert lane.managed_lane(trade, switched) == "UTEMA"
    assert lane.is_managed_lane(trade, switched)
    # A restored frozen owner remains authoritative even if a consumer lost tags.
    restored = SimpleNamespace(exit_policy_owner="UTEMA")
    assert lane.lane_tag(restored, switched) == "UTEMA"
    assert lane.managed_lane(restored, switched) == "UTEMA"
    generic = SimpleNamespace(source_engine="ut_ema", source_lane="UTEMA", exit_policy_owner="GENERIC")
    assert lane.managed_lane(generic, switched) == ""


def test_shared_state_json_roundtrip_preserves_alignment_and_stop_history():
    source = SimpleNamespace(source_lane="UTEMA", exit_policy_owner="UTEMA", utema_state={
        "lifecycle_id": "NIFTY|5min|UTEMA|2026-10-29T10:00:00",
        "entry_engine": "UT", "side": 1, "entry_spot": 22700., "initial_stop": 22654.6,
        "alignment_event_ids": ["EMA|2026-10-29T10:05:00|1"], "alignment_lots": 1,
        "base_lots": 2, "alignment_applied": True,
        "stop_updates": [{"time": datetime(2026, 10, 29, 10, 10), "stop": 22705.}],
        "exit_reason": "", "exit_time": None,
    })
    payload = json.loads(json.dumps(lane.snapshot_session_state(source)))
    restored = SimpleNamespace(source_lane="UTEMA")
    lane.restore_session_state(restored, payload)
    assert restored.utema_state == payload["utema_state"]
    assert restored.utema_state["alignment_lots"] == 1
    assert restored.utema_state["alignment_applied"] is True
    assert restored.utema_state["stop_updates"][0]["time"] == "2026-10-29T10:10:00"
    assert lane.managed_lane(restored, _settings()) == "UTEMA"
    restored.utema_state["alignment_event_ids"].append("later")
    assert source.utema_state["alignment_event_ids"] == ["EMA|2026-10-29T10:05:00|1"]
    assert payload["utema_state"]["alignment_event_ids"] == ["EMA|2026-10-29T10:05:00|1"]


@pytest.mark.parametrize("invalid", [None, [], "not-a-state", 3., {"stop": float("nan")}])
def test_invalid_restored_state_does_not_invent_native_evidence(invalid):
    restored = SimpleNamespace(source_lane="UTEMA")
    lane.restore_session_state(restored, {"utema_state": invalid})
    assert not hasattr(restored, "utema_state")


def test_native_policy_has_no_foreign_wait_rr_floor_or_signal_only_switch():
    settings = _settings(s1_lane_signal_only=True, s1_lane_intrabar_wait_seconds=30.,
        s1_min_entry_rr=3., s1_sl_max_pct=.1)
    trade = SimpleNamespace(source_lane="UTEMA", timeframe="5min")
    assert lane.lane_sl_max_pct(trade, settings) == .21
    assert lane.lane_min_entry_rr(trade, settings) == 0.
    assert lane.lane_intrabar_wait_seconds(trade, settings) == 0.
    assert lane.signal_only(trade, settings) is False


def test_native_dispatch_bypasses_foreign_exit_and_trend_logic(monkeypatch):
    import engine
    from engine import uts_lane, s12_lane

    native = ModuleType("engine.ut_ema_lane")
    calls = []
    expected = lane.S1ExitDecision(True, "UTEMA_SMART_EXIT", 22654.6)
    def exit_decision(*args, **kwargs):
        calls.append(("decision", args, kwargs))
        return expected
    def engine_exit_for(*args):
        calls.append(("engine", args, {}))
        return {"reason": "RSI-50"}
    def closed_bar_state(*args):
        calls.append(("state", args, {}))
        return {"stop": 22660.}
    def lane_exit_fields(*args):
        calls.append(("fields", args, {}))
        return "SMART EXIT", datetime(2026, 10, 29, 10, 5), 22750.
    native.exit_decision, native.engine_exit_for = exit_decision, engine_exit_for
    native.closed_bar_state, native.lane_exit_fields = closed_bar_state, lane_exit_fields
    monkeypatch.setitem(sys.modules, "engine.ut_ema_lane", native)
    monkeypatch.setattr(engine, "ut_ema_lane", native, raising=False)
    monkeypatch.setattr(uts_lane, "exit_decision", lambda *a, **kw: pytest.fail("UTS policy used"))
    monkeypatch.setattr(s12_lane, "exit_decision", lambda *a, **kw: pytest.fail("S12 policy used"))
    trade = SimpleNamespace(exit_policy_owner="UTEMA")
    now = datetime(2026, 10, 29, 10, 5)
    market = {"stop": 22660.}
    # This deliberately lacks .trend/.known: native dispatch must precede generic evidence access.
    assert lane.exit_decision(trade, 22750., 200., now=now, market=market,
        observed_contract_ticks=[(now, 200.)], trend_evidence=object(), settings=_settings()) is expected
    assert calls[0][2]["market"] is market
    result = {"native_results": {"quad_prime": {"state": {}}}}
    assert lane.engine_exit_for(trade, result, now) == {"reason": "RSI-50"}
    assert calls[1][1][1] is result
    assert lane.closed_bar_state(trade, result) == {"stop": 22660.}
    assert lane.lane_exit_fields(trade) == ("SMART EXIT", now, 22750.)
    assert [item[0] for item in calls] == ["decision", "engine", "state", "fields"]


def test_existing_uts_owner_still_uses_uts_dispatch(monkeypatch):
    from engine import uts_lane

    expected = lane.S1ExitDecision(False, "", 99.)
    seen = []
    monkeypatch.setattr(uts_lane, "exit_decision", lambda *args, **kwargs: seen.append(args) or expected)
    trade = SimpleNamespace(exit_policy_owner="UTS", source_lane="UTS")
    assert lane.exit_decision(trade, 100., 5., settings=SimpleNamespace(s1_lane_enabled=True)) is expected
    assert seen[0][0] is trade
    assert lane.managed_lane(trade) == "UTS"


def test_existing_s1_owner_keeps_its_own_index_stop():
    trade = SimpleNamespace(exit_policy_owner="S1", source_lane="S1", direction="LONG",
        inst_type="FUT", entry_spot=100., s1_sl1=99., entry_price=100., initial_stop=99.,
        lots=1, lot_size=1)
    decision = lane.exit_decision(trade, 98., 98., settings=SimpleNamespace(s1_lane_enabled=True))
    assert decision.exit
    assert decision.reason == "S1_SL1"
    assert not hasattr(trade, "utema_state")
