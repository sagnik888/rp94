"""Native UT/EMA execution must not inherit unrelated repaint or polling rules."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from scanner import Scanner, IST
from engine import ut_ema_lane as lane
from tests.test_ut_ema_lane_2026_10_29 import host, candidates, native_result, settings


@pytest.fixture(autouse=True)
def explicit_intrabar_settings(monkeypatch):
    import config.settings as config
    monkeypatch.setattr(config, "get_settings", settings)


def test_native_tracker_never_uses_another_scanners_repaint_signal(monkeypatch):
    import scanner as module
    monkeypatch.setattr(module, "get_settings", lambda: settings(
        option_repaint_abort_defer_until_close=False, repaint_abort_skip_when_profitable=False))
    scanner=Scanner.__new__(Scanner)
    now=datetime.now(IST).replace(tzinfo=None)
    stamp=now.replace(minute=(now.minute//5)*5, second=0, microsecond=0)
    candidate=NS(instrument="NIFTY",timeframe="5min",direction="LONG",signal_timestamp=stamp,
        source_engine="ut_ema",source_lane="UTEMA",exit_policy_owner="UTEMA")
    trade=NS(id="NATIVE",status="OPEN",entered_intracandle=True,source_engine="ut_ema",
        source_lane="UTEMA",exit_policy_owner="UTEMA",inst_type="OPT",entry_price=100.,current_price=99.)
    scanner._cached_results={"NIFTY_5min":{"source_engine":"utbot","signals":[
        NS(timestamp=stamp-timedelta(minutes=5),signal_type="BUY")]}}
    assert scanner._repaint_signal_status(candidate,stamp)=="VANISHED"
    scanner.log_event=scanner._diag_inc=lambda *a,**kw:None
    scanner._close_and_record=Mock()
    sleep=AsyncMock()
    monkeypatch.setattr(module.asyncio,"sleep",sleep)
    asyncio.run(scanner._track_repaint_until_candle_close(trade,candidate))
    scanner._close_and_record.assert_not_called()
    sleep.assert_not_called()


def test_native_broker_ack_does_not_schedule_generic_tracker(monkeypatch):
    scanner=Scanner.__new__(Scanner)
    scanner.mode="REAL"
    scanner._latest_trade_candidates={}
    scanner._close_superseded_session_entries=scanner._mark_candidate_accepted_time=Mock()
    scanner._record_accepted_entry=scanner._remember_trade_candidates=scanner._notify=Mock()
    scanner._publish_trade_payload_now=AsyncMock()
    scanner._track_repaint_until_candle_close=Mock(return_value=None)
    candidate=NS(instrument="NIFTY",direction="LONG",timeframe="5min",price=100.,lots=2)
    trade=NS(lots=2,entered_intracandle=True,source_engine="ut_ema",source_lane="UTEMA",exit_policy_owner="UTEMA")
    import scanner as module
    scheduled=Mock()
    monkeypatch.setattr(module.asyncio,"create_task",scheduled)
    asyncio.run(scanner._handle_broker_entry_result(candidate,trade,True))
    scanner._track_repaint_until_candle_close.assert_not_called()
    scheduled.assert_not_called()


def _monitor(monkeypatch):
    import scanner as module
    monkeypatch.setattr(module,"get_settings",settings)
    scanner,processor,_=host(monkeypatch)
    row=candidates(processor)[0]
    row.price=row.current_price=100.
    row.stop=90.
    row.target=105.
    row.entry_spot=row.underlying_entry=1000.
    row.peak_pnl=row.max_drawdown=0.
    row.entry_timestamp=datetime(2026,10,29,10,0,10)
    lane.state(row)["premium_risk_pct"]=10.
    lane.persist(row)
    real=Scanner.__new__(Scanner)
    real.mode="REAL"
    real._loaded_session_is_current_day=lambda:True
    real._active_session_entry_candidates=lambda inst:[row]
    real.candles=NS(get_latest_price=lambda inst:1000.)
    real._ensure_trade_like_initial_stop=lambda c:None
    real._get_trade_regime=lambda *a,**kw:"UNKNOWN"
    real._candidate_net_pnl=real._candidate_exit_net_pnl=lambda c,px:(px-c.price)*c.lots*c.lot_size
    real._trade_like_initial_risk_rs=lambda c:150.
    real.mtf=NS(_result_cache={"NIFTY_5min":native_result()})
    real._diag_inc=real._inherit_exit_entry_metadata=real.log_event=real._notify=lambda *a,**kw:None
    real._maybe_arm_reversion_from_exit=lambda *a,**kw:None
    real._observe_manual_signal_continuity=lambda *a:False
    real._capture_exit_tracking=lambda *a,**kw:None
    real._contract_quote_age_seconds=lambda *a:0.
    real._session_row_stop_touch=Mock(return_value=None)
    real._candidate_exit_resolution=lambda c,px,reason,text:(px,reason,text)
    real._latest_exit_candidates={}
    real._persist_session_trade_candidates=lambda:None
    real._trade_contract_tick_path=Mock(return_value=[])
    real._commit_trade_contract_tick_path=Mock()
    exits=[]
    real._remember_trade_candidates=lambda items:exits.extend(items)
    real._fifteen_min_one_min_support_exit_decision=Mock(side_effect=AssertionError("legacy exit called"))
    return real,row,exits


def test_manual_native_target_touch_precedes_later_stop_and_rebound(monkeypatch):
    scanner,row,exits=_monitor(monkeypatch)
    target_at=datetime.now(IST).replace(tzinfo=None)-timedelta(seconds=2)
    scanner._trade_contract_tick_path.return_value=[(110.,target_at),(80.,target_at+timedelta(seconds=1)),
                                                   (100.,target_at+timedelta(seconds=2))]
    scanner._check_session_candidates_breaches_locked("NIFTY")
    assert len(exits)==1
    assert exits[0].exit_reason=="UTEMA_PREMIUM_TARGET"
    assert exits[0].exit_price==110.
    assert exits[0].exit_timestamp==target_at
    assert exits[0].exit_observed_at==target_at
    assert exits[0].exit_observation["observed_at"]==target_at.isoformat()
    scanner._session_row_stop_touch.assert_not_called()
    scanner._commit_trade_contract_tick_path.assert_called_once_with(row)


def test_native_paper_tick_reader_starts_at_actual_acceptance(monkeypatch):
    scanner=Scanner.__new__(Scanner)
    scanner._option_premium_quote_plausible=lambda *a:True
    accepted=datetime(2026,10,29,10,0,10)
    captured=[]
    def reader(token,**kwargs):
        captured.append(kwargs)
        return [],7,False
    scanner.data=NS(get_contract_ticks_since=reader)
    row=NS(symbol_token="1",inst_type="OPT",entry_timestamp=accepted,
        signal_timestamp=datetime(2026,10,29,10,0),source_engine="ut_ema",source_lane="UTEMA")
    assert scanner._trade_contract_tick_path(row)==[]
    assert captured[0]["not_before"]==IST.localize(accepted).timestamp()


def test_native_monitor_commits_ticks_when_no_exit(monkeypatch):
    scanner,row,exits=_monitor(monkeypatch)
    scanner._trade_contract_tick_path.return_value=[(100.,datetime.now(IST).replace(tzinfo=None))]
    scanner._check_session_candidates_breaches_locked("NIFTY")
    assert not exits
    scanner._commit_trade_contract_tick_path.assert_called_once_with(row)


def test_stop_closes_native_observation_service():
    scanner=Scanner.__new__(Scanner)
    scanner.is_running=True
    scanner._save_warm_memory=scanner._persist_entry_shadow_book=Mock()
    scanner._persist_session_trade_candidates=scanner.generate_daily_signal_report=Mock()
    scanner._utema_tick_service=NS(close=Mock())
    scanner.stop()
    assert scanner.is_running is False
    scanner._utema_tick_service.close.assert_called_once()


@pytest.mark.parametrize("mode,auto",[("HISTORICAL",False),("REAL",True)])
def test_queued_manual_native_entry_rechecks_mode_before_quote(monkeypatch,mode,auto):
    scanner=Scanner.__new__(Scanner)
    scanner.mode,scanner.auto_mode=mode,auto
    scanner._candidate_execution_touch_quote=Mock(side_effect=AssertionError("must refuse before quote"))
    trade=NS(instrument="NIFTY",timeframe="5min",exec_type="M",source_engine="ut_ema",source_lane="UTEMA")
    allowed,reason=scanner._queued_fresh_entry_policy_guard(trade)
    assert not allowed and reason
    scanner._candidate_execution_touch_quote.assert_not_called()


def test_manual_native_authority_survives_trade_manager_manual_guard_bypass():
    from trading.trade_manager import TradeManager
    scanner=Scanner.__new__(Scanner)
    scanner.mode,scanner.auto_mode="HISTORICAL",False
    scanner._candidate_execution_touch_quote=Mock(side_effect=AssertionError("mode already changed"))
    trade=NS(instrument="NIFTY",timeframe="5min",exec_type="M",source_engine="ut_ema",source_lane="UTEMA")
    manager=NS(fresh_entry_policy_guard=scanner._queued_fresh_entry_policy_guard)
    allowed,reason=TradeManager._entry_submission_guard_verdict(manager,trade,lambda holder:False)
    assert not allowed and "REAL" in reason


def test_queued_manual_to_auto_alignment_uses_current_auto_authority(monkeypatch):
    scanner=Scanner.__new__(Scanner)
    scanner.mode,scanner.auto_mode="REAL",True
    scanner._entry_policy_allows=Mock(return_value=(True,""))
    authority=Mock(return_value=(True,""))
    monkeypatch.setattr(lane,"submission_authority",authority)
    trade=NS(exec_type="M",_scale_in_inflight=True,_entry_policy_phase="scale_in_pre_submit",
        source_engine="ut_ema",source_lane="UTEMA")
    assert scanner._queued_fresh_entry_policy_guard(trade)==(True,"")
    authority.assert_called_once_with(scanner,manual=False,trade=trade)


def test_native_completed_lifecycle_is_refused_before_manual_queue_quote(monkeypatch):
    scanner,row,exits=_monitor(monkeypatch)
    scanner.auto_mode=False
    row.exec_type="M"
    record=scanner.mtf._result_cache["NIFTY_5min"]["utema_records"][lane.state(row)["lifecycle_id"]]
    record["exit_time"]="2026-10-29T10:01:00"
    scanner._candidate_execution_touch_quote=Mock(side_effect=AssertionError("source already exited"))
    allowed,reason=scanner._queued_fresh_entry_policy_guard(row)
    assert not allowed and "lifecycle exited" in reason
    scanner._candidate_execution_touch_quote.assert_not_called()


def test_native_failed_decision_does_not_commit_tick_cursor(monkeypatch):
    from engine import s1_lane
    scanner,row,exits=_monitor(monkeypatch)
    monkeypatch.setattr(s1_lane,"exit_decision",Mock(side_effect=RuntimeError("probe decision failure")))
    with pytest.raises(RuntimeError,match="probe decision failure"):
        scanner._check_session_candidates_breaches_locked("NIFTY")
    scanner._commit_trade_contract_tick_path.assert_not_called()


def test_recorded_premium_touch_remains_usable_after_latest_quote_turns_stale(monkeypatch):
    scanner,row,exits=_monitor(monkeypatch)
    row.live_quote_stale=True
    row.quote_verified=False
    touched=datetime.now(IST).replace(tzinfo=None)-timedelta(seconds=20)
    scanner._trade_contract_tick_path.return_value=[(110.,touched)]
    scanner._check_session_candidates_breaches_locked("NIFTY")
    assert len(exits)==1
    assert exits[0].exit_reason=="UTEMA_PREMIUM_TARGET"
    assert exits[0].exit_price==110.
    assert exits[0].exit_timestamp==touched
    assert exits[0].exit_quote_verified
    assert exits[0].exit_observation["quote_verified"]
    assert exits[0].exit_price_basis=="OBSERVED_CONTRACT_TICK"
    assert exits[0].exit_observation["quote_age_seconds"]>=20.
    assert exits[0].peak_pnl>=exits[0].pnl>0


def test_stale_latest_quote_without_recorded_touch_cannot_price_native_exit(monkeypatch):
    scanner,row,exits=_monitor(monkeypatch)
    row.live_quote_stale=True
    row.quote_verified=False
    scanner._trade_contract_tick_path.return_value=[(100.,datetime.now(IST).replace(tzinfo=None)-timedelta(seconds=20))]
    record=scanner.mtf._result_cache["NIFTY_5min"]["utema_records"][lane.state(row)["lifecycle_id"]]
    record.update(exit_time="2026-10-29T10:01",exit_reason="EMA RSI-50",exit_price=1000.)
    scanner._check_session_candidates_breaches_locked("NIFTY")
    assert not exits
    scanner._commit_trade_contract_tick_path.assert_not_called()


def _protected_alignment(add_price=220., stop=215., **changes):
    trade=NS(instrument="NIFTY",timeframe="5min",status="OPEN",execution_status="OPEN",
        exec_type="A",source_engine="ut_ema",source_lane="UTEMA",exit_policy_owner="UTEMA",
        entry_price=200.,price=200.,current_stop=215.,target=280.,_scale_in_inflight=True,
        _entry_policy_phase="scale_in_pre_submit",_entry_policy_prior_quantity=150,
        _entry_policy_add_quantity=75,
        _entry_policy_execution_levels=((200.*150+add_price*75)/225,stop,280.),
        utema_state={"lifecycle_id":"protected-add","side":1,"entry_spot":1000.,
            "initial_stop":998.,"premium_risk_pct":15.,"rr":2.5})
    trade.__dict__.update(changes)
    scanner=Scanner.__new__(Scanner)
    scanner.mode,scanner.auto_mode="REAL",True
    scanner._candidate_execution_touch_quote=Mock(side_effect=AssertionError("scale-in supplies its own touch geometry"))
    return scanner,trade


def test_protected_native_alignment_accepts_combined_average_below_preserved_stop():
    scanner,trade=_protected_alignment()
    before=(trade.entry_price,trade.current_stop,trade.target)
    assert scanner._queued_fresh_entry_policy_guard(trade)==(True,"")
    assert (trade.entry_price,trade.current_stop,trade.target)==before
    scanner._candidate_execution_touch_quote.assert_not_called()


def test_protected_native_alignment_does_not_relax_initial_entry_geometry():
    scanner,trade=_protected_alignment()
    assert not scanner._entry_policy_allows(trade,
        execution_levels=trade._entry_policy_execution_levels,phase="broker_queue")[0]


@pytest.mark.parametrize("add_price,stop",[(210.,215.),(215.,215.),(220.,214.)])
def test_protected_native_alignment_refuses_breached_or_relaxed_protection(add_price,stop):
    scanner,trade=_protected_alignment(add_price,stop)
    assert not scanner._queued_fresh_entry_policy_guard(trade)[0]


@pytest.mark.parametrize("add_price",[280.,290.])
def test_native_alignment_refuses_add_after_parent_premium_target(add_price):
    scanner,trade=_protected_alignment(add_price)
    assert not scanner._queued_fresh_entry_policy_guard(trade)[0]


@pytest.mark.parametrize("cap",[0.,-1.,16.,float("nan")])
def test_protected_native_alignment_requires_valid_frozen_risk(cap):
    scanner,trade=_protected_alignment()
    trade.utema_state["premium_risk_pct"]=cap
    assert not scanner._queued_fresh_entry_policy_guard(trade)[0]
