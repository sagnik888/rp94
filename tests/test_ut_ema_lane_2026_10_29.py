"""Native lane integration without legacy intelligence or broker execution."""
import asyncio
import copy
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock

import pandas as pd
import pytest

from engine import ut_ema_lane as lane
from engine.ut_ema_engine import Timeline, Params, market_stamp as stamp
from engine.signal_processor import SignalProcessor
from engine.ut_ema_engine import UTEmaEngine


def settings(**changes):
    values = dict(scanner_engine="ut_ema", ut_timeframe_entry_policy="INCLUDE_5MIN",
        dynamic_rr_min=1.5, dynamic_rr_max=4., dynamic_rr_5min_max=2.5,
        execution_touch_quote_max_age_seconds=1.5)
    values.update(changes)
    return NS(**values)


def native_result(tf="5min", side=1, align=True):
    t = Timeline(Params(), "NIFTY", tf)
    t.bar_time = t.observed_at = pd.Timestamp("2026-10-29 10:00")
    t.bar_index, t.live = 0, True
    t.enter("UT", side, 1000.)
    if align:
        t.enter("EMA", side, 1000.)
    frame = pd.DataFrame([(1000.,1001.,999.,1000.)], columns=["open","high","low","close"],index=[t.bar_time])
    indicators = {"ut_atr": [1.]}
    # result uses other computed overlay series, so use real indicators.
    from engine.ut_ema_engine import compute_indicators
    return UTEmaEngine().result(t, frame, compute_indicators(frame, Params()), 1)


def host(monkeypatch, **changes):
    import config.settings as config
    # These fixtures model live intrabar signals explicitly, independent of .env.
    monkeypatch.setattr(config, "get_settings", settings)
    monkeypatch.setattr(lane, "market_stamp", lambda value=None: stamp(value or "2026-10-29 10:01"))
    rows = []
    scanner = NS(mode="REAL",auto_mode=False,
        trades=NS(open_trades={},closed_trades=[],open_trade=Mock(side_effect=AssertionError("broker called"))),
        data=NS(get_option_token=Mock(return_value={"symbol":"NIFTY1000CE","token":"1","strike_val":1000.})),
        _active_session_entry_candidates=lambda inst,timeframe=None: [r for r in rows if r.timeframe==timeframe and r.action=="ENTRY"],
        _get_session_trade_candidates=lambda inst: rows,
        _days_to_expiry_for=lambda *a: 1,
        _mark_candidate_accepted_time=Mock(),_record_accepted_entry=Mock(),log_event=Mock(),
        _publish_trade_payload_now=AsyncMock(),_latest_trade_candidates={})
    def remember(candidates):
        for candidate in candidates:
            if not any(r is candidate for r in rows): rows.append(candidate)
    scanner._remember_trade_candidates = remember
    def quote(probe, **kw):
        probe.quote_verified, probe.quote_source = True, "test_observed"
        return 100.
    scanner._get_live_premium_cached = quote
    scanner.__dict__.update(changes)
    processor = NS(scanner=scanner,_option_signed_delta=lambda *a: .5)
    return scanner, processor, rows


def candidates(processor, configuration=None, results=None):
    return lane.build_candidates(processor,"NIFTY",results or NS(results_5min=native_result(),results_15min=native_result("15min")),
        configuration or {"strike_interval":50},2,75,settings())


def test_shared_same_observation_alignment_and_independent_timeframes(monkeypatch):
    scanner, processor, rows = host(monkeypatch)
    signals = candidates(processor)
    assert [c.action for c in signals] == ["ENTRY","ADD","ENTRY","ADD"]
    accepted = asyncio.run(lane.coordinate(scanner,signals))
    assert len(rows)==2
    assert {r.timeframe for r in rows}=={"5min","15min"}
    assert all(r.lots==3 and lane.state(r)["alignment_added"] for r in rows)
    assert len(accepted)==4
    assert candidates(processor)==[]
    asyncio.run(lane.coordinate(scanner,signals))
    assert all(r.lots==3 for r in rows)
    scanner.trades.open_trade.assert_not_called()


def test_closed_premium_trade_cannot_reenter_same_native_lifecycle(monkeypatch):
    scanner, processor, rows = host(monkeypatch)
    signals = candidates(processor)
    asyncio.run(lane.coordinate(scanner,signals))
    for row in rows: row.action="EXIT"
    assert candidates(processor)==[]


def test_missing_verified_atm_quote_refuses_entry(monkeypatch):
    scanner, processor, rows = host(monkeypatch,_get_live_premium_cached=lambda *a,**kw:100.)
    assert candidates(processor)==[]


def test_contract_resolver_cannot_substitute_itm(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    scanner.data.get_option_token.return_value["strike_val"]=950.
    assert candidates(processor)==[]


def test_put_option_geometry_is_long_premium(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signals=candidates(processor,results=NS(results_5min=native_result(side=-1),results_15min={}))
    assert signals[0].direction=="SHORT" and signals[0].option_type=="PE"
    assert signals[0].stop < signals[0].price < signals[0].target
    assert lane.entry_allowed(signals[0])[0]


def test_native_stop_mapping_has_no_floor_and_caps_fifteen_percent():
    assert lane.premium_stop_distance(100.,.2,.5)==pytest.approx(.1)
    assert lane.premium_stop_distance(100.,200.,.5)==15.


def test_execution_freezes_risk_from_final_ask(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signal=candidates(processor)[0]
    levels=lane.execution_levels(scanner,signal,{"bid":89.,"ask":90.,"usable":True,"age_seconds":.1})
    args=lane.open_kwargs(scanner,signal,levels,"M")
    assert lane.state(signal)["premium_risk_pct"]==pytest.approx(100/90)
    assert (levels[2]-levels[0])/(levels[0]-levels[1])==2.5
    assert args["quality_verdict"]["checks"]["utema_state"]["premium_risk_pct"]==pytest.approx(100/90)
    assert args["entry_submission_guard"](None)[0]
    scanner.auto_mode=True
    assert not args["entry_submission_guard"](None)[0]


@pytest.mark.parametrize("touch",[{"bid":1.,"ask":2.,"usable":True,"age_seconds":2.},
    {"bid":2.,"ask":1.,"usable":True,"age_seconds":.1},
    {"bid":1.,"ask":2.,"usable":False,"age_seconds":.1}])
def test_stale_or_invalid_execution_quote_refused(monkeypatch,touch):
    scanner,processor,rows=host(monkeypatch)
    signal=candidates(processor)[0]
    assert lane.execution_levels(scanner,signal,touch)==(0.,0.,0.)


def test_selected_option_target_and_native_exit_remain_separate(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signal=candidates(processor)[0]
    native=lane.state(signal)
    assert signal.spot_target==1004. and signal.target==102.5
    assert lane.exit_decision(signal,1000.,101.,now="2026-10-29 10:01",engine_exit={"reason":"EMA T2"}).reason=="UTEMA_EMA_T2"
    assert lane.exit_decision(signal,1000.,102.5,now="2026-10-29 10:01").reason=="UTEMA_PREMIUM_TARGET"
    assert lane.exit_decision(signal,1000.,100.,now="2026-10-29 10:01",observed_contract_ticks=[(98.,None),(100.,None)]).reason=="UTEMA_PREMIUM_STOP"


def test_future_index_ratchet_does_not_apply_early(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signal=candidates(processor)[0]
    result=lane.exit_decision(signal,1000.,100.,now="2026-10-29 10:01",
        market={"utema_record":{"stop_updates":[{"time":"2026-10-29T10:02","stop":1001.}]}})
    assert not result.exit and result.index_stop==998.


def test_signal_processor_bypasses_legacy_intelligence(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    import engine.signal_processor as module
    monkeypatch.setattr(module,"get_settings",settings)
    processor._prepare_signal_pipeline_inputs=Mock(side_effect=AssertionError("legacy gate"))
    result=SignalProcessor.process_best_signal(processor,"NIFTY",NS(results_5min=native_result(),results_15min={}),
        {},0.,"UNAVAILABLE",2,75,{"strike_interval":50},1000.,1000.)
    assert [r.action for r in result]==["ENTRY","ADD"]
    processor._prepare_signal_pipeline_inputs.assert_not_called()


def test_execution_service_bypasses_global_stabilization(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    import engine.execution_service as module
    monkeypatch.setattr(module,"get_settings",settings)
    scanner._signal_fire_mode=Mock(side_effect=AssertionError("legacy timing"))
    result=asyncio.run(module.ExecutionService(scanner).coordinate_and_execute(candidates(processor)))
    assert len(result)==4 and len(rows)==2
    scanner._signal_fire_mode.assert_not_called()


def test_pending_actual_exit_still_occupies_timeframe(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signal=candidates(processor)[0]
    signal.pending_exit=True
    scanner.trades.open_trades["real"]=signal
    assert len(lane.active_rows(scanner,"NIFTY","5min"))==1
    assert not any(row.timeframe=="5min" for row in candidates(processor))


def test_verified_rest_recovery_cache_verifies_new_probe(monkeypatch):
    from scanner import Scanner
    import scanner as module
    monkeypatch.setattr(module,"get_settings",settings)
    scanner=Scanner.__new__(Scanner)
    scanner._premium_cache={"1":{"price":100.,"time":module.time.time(),"verified":True,"source":"broker_contract_ltp"}}
    scanner.data=NS(get_cached_contract_ltp=lambda *a,**kw:None)
    scanner._option_premium_quote_plausible=lambda *a:True
    scanner._request_premium_refresh=Mock(side_effect=AssertionError("should use verified cache"))
    probe=NS(instrument="NIFTY",trading_symbol="NIFTY1000CE",symbol_token="1",quote_verified=False)
    assert scanner._get_live_premium_cached(probe,allow_blocking=False)==100.
    assert probe.quote_verified and probe.quote_source=="broker_contract_ltp"


def test_analysis_pipeline_never_fetches_legacy_intelligence(monkeypatch):
    import engine.entry_analysis_service as module
    monkeypatch.setattr(module,"get_settings",settings)
    scanner,processor,rows=host(monkeypatch)
    scanner.user_lots={"NIFTY":2}
    scanner.capital_fut=scanner.capital_opt=100000.
    scanner.risk_fut_pct=.2
    scanner.candles=NS(get_latest_price=lambda *a:1000.,get_candles=lambda *a:None)
    scanner.data.get_cached_index_quote_snapshot=lambda *a:{}
    scanner.data.get_cached_previous_close=lambda *a:990.
    scanner.data.get_market_source_snapshot=lambda *a:{"entry_eligible":True}
    scanner._prepare_live_signal_data=lambda *a:a[-1]
    result_5m = native_result()
    # The one-bar fixture has no warmed native RMA.  Supply a measured 5m ATR
    # to prove the native display path preserves an actual engine reading.
    result_5m["state"].update(atr_value=7.5, atr_timeframe="5min", atr_reason="native_ut_atr")
    scanner.mtf=NS(update_data=Mock(),process_instrument=lambda *a:NS(results_5min=result_5m,results_15min={},results_1min={}))
    scanner._cached_results={}
    scanner._cached_result_metadata={}
    scanner._intel_cache={"NIFTY_5min": {
        "volume":{"available":True,"volume_ratio":1.2},
        "oi":{"available":False,"reason":"Option chain unavailable"},
        "pcr":{"available":False,"reason":"Option chain unavailable"},
    }}
    scanner.signal_processor=processor
    scanner._refresh_trade_candidates=Mock()
    scanner._check_session_candidates_breaches=Mock()
    scanner._dashboard_quote_session=lambda q:("Live","REGULAR",False,None)
    scanner.chart_stream_enabled=False
    scanner.signals=NS(get_active_signals=lambda **kw:[])
    scanner._get_option_chain_cached=Mock(side_effect=AssertionError("legacy chain called"))
    scanner._regime_history_ready=Mock(side_effect=AssertionError("legacy warmup called"))
    view,result=module.EntryAnalysisService(scanner).process("NIFTY",{"lot_size":75,"strike_interval":50})
    assert view["ltp"]==1000. and len(result)==2
    assert view["trade_state"]["atr_value"] == pytest.approx(7.5)
    assert view["trade_state"]["atr_exhaustion"] == "ATR 7.50 pts"
    assert view["intelligence"]["volume"]["volume_ratio"] == pytest.approx(1.2)
    assert view["intelligence"]["oi"]["available"] is False
    assert view["intelligence"]["pcr"]["available"] is False
    scanner._get_option_chain_cached.assert_not_called()
    scanner._regime_history_ready.assert_not_called()


def test_native_manual_monitor_first_exit_skips_legacy_guards(monkeypatch):
    from scanner import Scanner
    import scanner as module
    monkeypatch.setattr(module,"get_settings",settings)
    scanner,processor,rows=host(monkeypatch)
    row=candidates(processor)[0]
    row.price=row.current_price=100.
    row.entry_spot=row.underlying_entry=1000.
    row.peak_pnl=row.max_drawdown=0.
    result=native_result()
    record=result["utema_records"][lane.state(row)["lifecycle_id"]]
    record.update(exit_time="2026-10-29T10:01",exit_reason="EMA RSI-50",exit_price=1000.)
    real=Scanner.__new__(Scanner)
    real.mode="REAL"
    real._loaded_session_is_current_day=lambda:True
    real._active_session_entry_candidates=lambda inst:[row]
    real.candles=NS(get_latest_price=lambda inst:1000.)
    real._ensure_trade_like_initial_stop=lambda c:None
    real._get_trade_regime=lambda *a,**kw:"UNKNOWN"
    real._candidate_net_pnl=lambda c,px:(px-c.price)*c.lots*c.lot_size
    real._trade_like_initial_risk_rs=lambda c:150.
    real.mtf=NS(_result_cache={"NIFTY_5min":result})
    real._diag_inc=real._inherit_exit_entry_metadata=real.log_event=real._notify=lambda *a,**kw:None
    real._maybe_arm_reversion_from_exit=lambda *a,**kw:None
    real._observe_manual_signal_continuity=lambda *a:False
    real._capture_exit_tracking=lambda *a,**kw:None
    real._contract_quote_age_seconds=lambda *a:0.
    real._session_row_stop_touch=lambda *a:None
    real._candidate_exit_resolution=lambda c,px,reason,text:(px,reason,text)
    real._latest_exit_candidates={}
    real._persist_session_trade_candidates=lambda:None
    exits=[]
    real._remember_trade_candidates=lambda items:exits.extend(items)
    real._fifteen_min_one_min_support_exit_decision=Mock(side_effect=AssertionError("legacy exit called"))
    real._check_session_candidates_breaches_locked("NIFTY")
    assert [e.exit_reason for e in exits]==["UTEMA_EMA_RSI-50"]
    real._fifteen_min_one_min_support_exit_decision.assert_not_called()


def test_restart_ignores_reconstructed_exits_but_honors_new_observed_exit(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    row=candidates(processor)[0]
    event={"engine":"EMA","entry":False,"reason":"RSI-50","price":1001.,
        "observed_at":"2026-10-29T10:01:00","realtime":False,"sequence":1}
    result={"source_engine":"ut_ema","utema_records":{},"utema_events":[event]}
    assert lane.engine_exit_for(row,result,now="2026-10-29 10:01") is None
    event["realtime"]=True
    assert lane.engine_exit_for(row,result,now="2026-10-29 10:01")["reason"]=="EMA RSI-50"
    assert lane.engine_exit_for(row,result,now="2026-10-29 10:00") is None


def test_mode_switch_to_historical_cannot_admit_live_candidates(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signals=candidates(processor)
    scanner.mode="HISTORICAL"
    assert asyncio.run(lane.coordinate(scanner,signals))==[]
    assert rows==[]
    scanner.trades.open_trade.assert_not_called()


@pytest.mark.parametrize("changes",[{"status":"CLOSED"},{"pending_exit":True},{"execution_status":"EXIT_PENDING"},
    {"execution_status":"PARTIAL_EXIT"},{"execution_status":"RECOVERY_REQUIRED"}])
def test_queued_native_add_cannot_buy_after_parent_exit(monkeypatch,changes):
    scanner,processor,rows=host(monkeypatch)
    signal=candidates(processor)[0]
    scanner.auto_mode=True
    signal._scale_in_inflight=True
    signal.status="OPEN"
    signal.__dict__.update(changes)
    ok,reason=lane.submission_authority(scanner,trade=signal)
    assert not ok and reason


def test_earlier_native_exit_outranks_later_current_premium_breach(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signal=candidates(processor)[0]
    result=lane.exit_decision(signal,1000.,80.,now="2026-10-29 10:02",
        engine_exit={"reason":"EMA RSI-50","time":"2026-10-29T10:01"})
    assert result.reason=="UTEMA_EMA_RSI-50"


def test_known_premium_target_before_native_exit_wins_chronologically(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signal=candidates(processor)[0]
    result=lane.exit_decision(signal,1000.,100.,now="2026-10-29 10:02",
        engine_exit={"reason":"EMA RSI-50","time":"2026-10-29T10:01:30"},
        observed_contract_ticks=[(104.,"2026-10-29 10:01"),(80.,"2026-10-29 10:01:40")])
    assert result.reason=="UTEMA_PREMIUM_TARGET" and result.observed_price==104.


def test_stream_requires_explicit_fresh_event_authority(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    scanner._utema_tick_service=NS(_closed=False)
    result=native_result()
    result["utema_entry_eligible"]=True
    view=NS(results_5min=result,results_15min={})
    assert candidates(processor,results=view)==[]
    result["utema_executable_event_ids"]=[signal.utema_event_id for signal in result["signals"]]
    assert [c.action for c in candidates(processor,results=view)]==["ENTRY","ADD"]
    scanner._utema_stream_health={"NIFTY_5min":{"status":"ERROR"}}
    assert candidates(processor,results=view)==[]


def test_broker_queue_rechecks_event_authority(monkeypatch):
    monkeypatch.setattr("config.settings.get_settings",settings)
    scanner,processor,rows=host(monkeypatch)
    candidate=candidates(processor)[0]
    lane.state(candidate)["applied_event_ids"]=[candidate.utema_event_id]
    scanner.auto_mode=True
    scanner._utema_tick_service=NS(_closed=False)
    result=native_result()
    result["utema_executable_event_ids"]=[candidate.utema_event_id]
    scanner.mtf=NS(_result_cache={"NIFTY_5min":result})
    assert lane.submission_authority(scanner,trade=candidate)[0]
    result["utema_executable_event_ids"]=[]
    assert not lane.submission_authority(scanner,trade=candidate)[0]


def test_paper_handoff_rechecks_event_authority(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    signals=candidates(processor)
    scanner._utema_tick_service=NS(_closed=False)
    scanner.mtf=NS(_result_cache={})
    assert asyncio.run(lane.coordinate(scanner,signals))==[]
    assert rows==[]


def test_queued_native_entry_is_refused_after_selector_change(monkeypatch):
    scanner,processor,rows=host(monkeypatch)
    candidate=candidates(processor)[0]
    scanner.auto_mode=True
    scanner._utema_tick_service=NS(_closed=False)
    monkeypatch.setattr("config.settings.get_settings",lambda:settings(scanner_engine="ema"))
    assert not lane.submission_authority(scanner,trade=candidate)[0]


@pytest.mark.parametrize("change",[{"system_power":"OFF"},{"is_running":False}])
def test_native_handoff_and_broker_queue_respect_stop_authority(monkeypatch,change):
    scanner,processor,rows=host(monkeypatch)
    signals=candidates(processor)
    scanner.__dict__.update(change)
    assert asyncio.run(lane.coordinate(scanner,signals))==[]
    assert not rows
    assert not lane.submission_authority(scanner,manual=True,trade=signals[0])[0]
