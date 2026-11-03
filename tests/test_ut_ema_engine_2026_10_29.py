"""Observable Pine execution ordering, shared sizing, and causal tick replay."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from engine.ut_ema_engine import Params, Position, Timeline, UTEmaEngine, compute_indicators, initial_stop


def frame(values, start="2026-10-20 09:15", tf="5min"):
    out = pd.DataFrame(values, columns=["open", "high", "low", "close"])
    out.index = pd.date_range(start, periods=len(out), freq=tf)
    return out


def test_pine_defaults_and_outward_index_stop():
    p = Params()
    assert (p.ut_factor, p.ut_atr_len, p.ut_heikin) == (1., 10, False)
    assert (p.ema_fast_len, p.ema_slow_len, p.execution_mode) == (9, 21, "Realtime Intrabar")
    assert (p.ut_target_exit, p.ema_target_exit, p.base_stop_pct) == ("T2", "T2", .2)
    for side in (1, -1):
        stop = initial_stop(22704.85, side)
        assert side * (22704.85 - stop) >= 22704.85 * .002 - 1e-9
        assert stop / .05 == pytest.approx(round(stop / .05))
    sensex = Timeline(p, "SENSEX", "15min")
    sensex.bar_time = sensex.observed_at = pd.Timestamp("2026-10-20 10:00")
    sensex.enter("UT", 1, 73412.46)
    assert sensex.positions["UT"].stop == pytest.approx(73265.63)


@pytest.mark.parametrize("side", [1, -1])
def test_one_shared_position_and_exactly_one_alignment_lot(side):
    t = Timeline(Params(), "NIFTY", "5min")
    t.bar_time = t.observed_at = pd.Timestamp("2026-10-20 10:00")
    assert t.enter("UT", side, 1000.)
    assert t.enter("EMA", side, 1001.)
    assert not t.enter("EMA", side, 1002.)
    assert [e["action"] for e in t.shared_events] == ["ENTRY", "ADD"]
    lifecycle = t.basket["lifecycle_id"]
    assert t.exit("EMA", 1002., "RSI-50")
    assert t.basket is None
    assert t.shared_events[-1]["action"] == "EXIT"
    assert t.shared_events[-1]["lifecycle_id"] == lifecycle
    assert t.records[lifecycle]["exit_reason"] == "EMA RSI-50"
    t.exit("UT", 1002., "T2")
    assert len([e for e in t.shared_events if e["action"] == "EXIT"]) == 1


def test_disagreement_does_not_add_or_open_another_shared_trade():
    t = Timeline(Params(), "BANKNIFTY", "15min")
    t.bar_time = t.observed_at = pd.Timestamp("2026-10-20 10:00")
    t.enter("UT", 1, 1000.)
    t.enter("EMA", -1, 1001.)
    assert [e["action"] for e in t.shared_events] == ["ENTRY"]


@pytest.mark.parametrize("side", [1, -1])
def test_stop_wins_ambiguous_bar_and_gaps_execute_at_open(side):
    t = Timeline(Params(), "NIFTY", "5min")
    t.bar_time = t.observed_at = pd.Timestamp("2026-10-20 10:00")
    t.enter("UT", side, 1000.)
    assert t.levels("UT", 1000., 1005., 995., 1000.)
    assert t.marks[-1]["reason"] == "STOP"
    assert t.marks[-1]["price"] == (998. if side > 0 else 1002.)
    t.enter("UT", side, 1000.)
    gap = 995. if side > 0 else 1005.
    t.levels("UT", gap, 1010., 990., gap)
    assert t.marks[-1]["price"] == gap


def test_targets_keep_initial_risk_when_stop_ratchets():
    t = Timeline(Params(), "NIFTY", "5min")
    t.bar_time = t.observed_at = pd.Timestamp("2026-10-20 10:00")
    t.enter("UT", 1, 1000.)
    t.positions["UT"].stop = 1001.
    assert t.target("UT") == 1004.


def test_historical_realtime_mode_uses_next_open_not_signal_close():
    values = [(1000., 1000.2, 999.8, 1000.)] * 20
    values += [(1000., 1000.2, 997.8, 998.), (998., 1002.2, 997.8, 1002.),
               (1001.5, 1001.8, 1001.2, 1001.6)]
    data = frame(values)
    result = UTEmaEngine().process(data, "NIFTY", "5min", realtime=False,
        as_of=data.index[-1] + pd.Timedelta(minutes=5))
    entries = [m for m in result["utema_events"] if m["entry"] and m["bar_index"] == 22]
    assert {m["engine"] for m in entries} == {"UT", "EMA"}
    assert all(m["price"] == 1001.5 for m in entries)
    assert [s.utema_action for s in result["signals"]][-2:] == ["ENTRY", "ADD"]


def test_historical_event_prefix_does_not_use_future_bars():
    data = frame([(1000., 1000.2, 999.8, 1000.)] * 20 +
                 [(1000., 1003., 999.8, 1002.), (1002., 1003., 1001., 1002.5),
                  (997., 1005., 996., 1004.), (1004., 1010., 1003., 1009.)])
    clock = data.index[21] + pd.Timedelta(minutes=5)
    prefix = UTEmaEngine().process(data.iloc[:22], "NIFTY", "5min", realtime=False, as_of=clock)
    full = UTEmaEngine().process(data, "NIFTY", "5min", realtime=False, as_of=clock)
    assert prefix["utema_events"] == full["utema_events"]


def test_chart_reads_do_not_consume_tick_state_and_tick_checks_ignore_old_bar_extremes():
    data = frame([(1000., 1000.2, 999.8, 1000.)] * 30 + [(1000., 1010., 990., 1000.)])
    engine = UTEmaEngine()
    now = data.index[-1] + pd.Timedelta(seconds=1)
    engine.process(data, "NIFTY", "5min", realtime=True, as_of=now)
    t = engine._timeline
    t.enter("EMA", 1, 1000.)
    before = len(t.marks)
    engine.chart_result(data, "NIFTY", "5min", as_of=now)
    assert len(t.marks) == before
    engine.process(data, "NIFTY", "5min", realtime=True, as_of=now + pd.Timedelta(seconds=1))
    assert engine._timeline.positions["EMA"].direction == 1
    assert len(t.marks) == before


def test_smart_stop_breach_exits_without_committing_a_breached_stop():
    t = Timeline(Params(), "NIFTY", "5min")
    t.bar_time = t.observed_at = pd.Timestamp("2026-10-20 10:00")
    t.enter("UT", 1, 1000.)
    t.positions["UT"].extreme = 1010.
    original = t.positions["UT"].stop
    assert t.smart_reason("UT", 1005., 1006., 1., False) == "SMART SL"
    assert t.positions["UT"].stop == original


def test_chart_snapshot_reuses_only_matching_live_candles_and_returns_a_copy():
    data = frame([(1000., 1000.2, 999.8, 1000.)] * 30 + [(1000., 1002., 999.8, 1001.)])
    engine = UTEmaEngine()
    now = data.index[-1] + pd.Timedelta(seconds=1)
    observed = engine.process(data, "NIFTY", "5min", realtime=True, as_of=now)
    chart = engine.chart_result(data.copy(), "NIFTY", "5min", as_of=now)
    assert chart["utema_events"] == observed["utema_events"]
    assert chart["state"]["bar_count"] == len(data)
    chart["state"]["current_price"] = -1
    assert engine.chart_result(data, "NIFTY", "5min", as_of=now)["state"]["current_price"] == 1001.


@pytest.mark.parametrize("changed", ["history", "forming", "instrument", "timeframe", "earlier_clock", "boundary"])
def test_chart_snapshot_replays_changed_request_without_mutating_live_state(changed):
    data = frame([(1000., 1000.2, 999.8, 1000.)] * 30 + [(1000., 1002., 999.8, 1001.)])
    engine = UTEmaEngine()
    now = data.index[-1] + pd.Timedelta(seconds=2)
    engine.process(data, "NIFTY", "5min", realtime=True, as_of=now)
    timeline, cached = engine._timeline, engine._last_result
    prefix, last_tick = engine._prefix_hash, engine._last_tick
    marks = list(timeline.marks)
    request = data.copy()
    instrument, timeframe, clock = "NIFTY", "5min", now
    if changed == "history":
        request.iloc[20] = [995., 995.2, 994.8, 995.]
    elif changed == "forming":
        request.iloc[-1] = [1000., 1003., 999.8, 1002.]
    elif changed == "instrument":
        instrument = "SENSEX"
    elif changed == "timeframe":
        timeframe = "15min"
    elif changed == "earlier_clock":
        clock = now - pd.Timedelta(seconds=1)
    else:
        clock = data.index[-1] + pd.Timedelta(minutes=5)
    expected = UTEmaEngine().process(request, instrument, timeframe, realtime=False, as_of=clock)
    actual = engine.chart_result(request, instrument, timeframe, as_of=clock)
    assert actual["utema_events"] == expected["utema_events"]
    assert actual["state"] == expected["state"]
    assert actual["indicator_overlays"] == expected["indicator_overlays"]
    assert engine._timeline is timeline and engine._last_result is cached
    assert engine._prefix_hash == prefix and engine._last_tick == last_tick
    assert timeline.marks == marks


def _controlled_indicators(ema_side=1., rsi=60.):
    """Isolate Pine execution ordering from the indicator recurrence."""
    return {"ut_atr": np.array([1., 1.]), "ema_atr": np.array([1., 1.]),
        "ut_trail": np.array([np.nan, np.nan]), "UT_diff": np.array([1., 1.]),
        "EMA_diff": np.array([1., ema_side]), "UT_rsi": np.array([60., 60.]),
        "EMA_rsi": np.array([60., rsi])}


def test_live_side_changes_allow_only_two_crossovers_per_engine_per_bar():
    data = frame([(1000., 1000., 1000., 1000.)] * 2)
    timeline = Timeline(Params(), "NIFTY", "5min")
    timeline.bar_time = timeline.observed_at = data.index[0]
    timeline.enter("EMA", 1, 1000.)
    for update, side in enumerate((-1., 1., -1.)):
        timeline.step(1, data, _controlled_indicators(side), tick=True, new_bar=update == 0,
            observed_at=data.index[1] + pd.Timedelta(seconds=update))
    assert timeline.signals_this_bar["EMA"] == 2
    assert timeline.positions["EMA"].direction == 1
    assert [mark["reason"] for mark in timeline.marks if not mark["entry"]] == ["FLIP", "FLIP"]


def test_live_stop_consumes_same_tick_crossover_without_reentering():
    data = frame([(1000., 1000., 1000., 1000.), (1000., 1000., 997., 997.)])
    timeline = Timeline(Params(), "NIFTY", "5min")
    timeline.bar_time = timeline.observed_at = data.index[0]
    timeline.enter("EMA", 1, 1000.)
    for update in range(2):
        timeline.step(1, data, _controlled_indicators(-1.), tick=True, new_bar=update == 0,
            observed_at=data.index[1] + pd.Timedelta(seconds=update))
    assert timeline.signals_this_bar["EMA"] == 1
    assert timeline.positions["EMA"].direction == 0
    assert [(mark["entry"], mark["reason"]) for mark in timeline.marks] == [(True, "ENTRY"), (False, "STOP")]


def test_native_rsi_50_exit_queues_next_open_without_a_crossover():
    data = frame([(1000., 1000., 1000., 1000.), (1001., 1001., 1001., 1001.)])
    timeline = Timeline(Params(), "NIFTY", "5min")
    timeline.bar_time = timeline.observed_at = data.index[0]
    timeline.enter("EMA", 1, 1000.)
    timeline.step(1, data, _controlled_indicators(1., 49.), observed_at=data.index[1] + pd.Timedelta(minutes=5))
    assert timeline.positions["EMA"].direction == 1
    assert timeline.pending_exit["EMA"] == "RSI-50"
    timeline.bar_time = timeline.observed_at = data.index[1] + pd.Timedelta(minutes=5)
    timeline.settle_pending(1001.5)
    assert timeline.positions["EMA"].direction == 0
    assert timeline.marks[-1]["reason"] == "RSI-50"
    assert timeline.marks[-1]["price"] == 1001.5


def test_pending_flip_gap_uses_stop_reason_then_enters_opposite_at_open():
    timeline = Timeline(Params(), "NIFTY", "5min")
    timeline.bar_time = timeline.observed_at = pd.Timestamp("2026-10-20 10:00")
    timeline.enter("EMA", 1, 1000.)
    timeline.pending["EMA"] = -1
    timeline.settle_pending(995.)
    assert timeline.positions["EMA"].direction == -1
    assert [(mark["entry"], mark["price"], mark["reason"]) for mark in timeline.marks][-2:] == [
        (False, 995., "STOP"), (True, 995., "ENTRY")]


# Independent observations for the 29 October end-to-end parity re-audit.
def _stream_seed():
    prices = np.linspace(1010., 1000., 31)
    return frame([(value, value+.1, value-.1, value) for value in prices])


def test_native_rsi_does_not_invent_overbought_state_from_a_flat_seed():
    data = frame([(1000., 1000., 1000., 1000.)] * 40)
    indicators = compute_indicators(data, Params())
    assert np.isnan(indicators["UT_rsi"]).all()
    assert np.isnan(indicators["EMA_rsi"]).all()
    data.iloc[-1] = [1000., 1001., 1000., 1001.]
    indicators = compute_indicators(data, Params())
    assert indicators["EMA_rsi"][-1] == 100.
    assert np.isnan(indicators["EMA_rsi"][-2])


def test_stream_seed_never_consumes_coalesced_forming_close():
    data = _stream_seed()
    data.iloc[-1] = [1000., 1100., 999.9, 1100.]
    engine = UTEmaEngine()
    clock = data.index[-1] + pd.Timedelta(seconds=1)
    seeded = engine.seed_stream(data, "NIFTY", "5min", as_of=clock)
    assert not any(event["realtime"] for event in seeded["utema_events"])
    assert seeded["state"]["bar_count"] == len(data)-1
    engine.observe_tick(1000., clock)
    assert engine._stream_rows[-1] == (1000.,1000.,1000.,1000.)
    assert not any(event["realtime"] for event in engine.stream_result()["utema_events"])


def test_rolling_window_preserves_observed_intrabar_lifecycle_and_indicators():
    import copy
    data = _stream_seed()
    engine = UTEmaEngine()
    start = data.index[-1]
    engine.process(data,"NIFTY","5min",as_of=start+pd.Timedelta(seconds=1),realtime=True)
    data.loc[start,["high","close"]] = 1040.
    observed = engine.process(data,"NIFTY","5min",as_of=start+pd.Timedelta(seconds=2),realtime=True)
    timeline = engine._timeline
    unchanged = copy.deepcopy(engine)
    next_start = start + pd.Timedelta(minutes=5)
    following = pd.DataFrame([(1040.,1040.1,1039.9,1040.)],columns=data.columns,index=[next_start])
    expanded = pd.concat([data,following])
    full = unchanged.process(expanded,"NIFTY","5min",as_of=next_start+pd.Timedelta(seconds=1),realtime=True)
    rolling = engine.process(expanded.iloc[1:],"NIFTY","5min",as_of=next_start+pd.Timedelta(seconds=1),realtime=True)
    assert engine._timeline is timeline
    assert rolling["utema_events"] == full["utema_events"]
    assert rolling["utema_records"] == full["utema_records"]
    assert rolling["state"] == full["state"]
    assert all(event in rolling["utema_events"] for event in observed["utema_events"] if event["realtime"])
    assert engine.chart_result(expanded.iloc[1:],"NIFTY","5min",as_of=next_start+pd.Timedelta(seconds=1))["utema_events"] == rolling["utema_events"]


def test_elapsed_close_commits_without_an_extra_market_update(monkeypatch):
    data = _stream_seed()
    engine = UTEmaEngine()
    start = data.index[-1]
    engine.process(data,"NIFTY","5min",as_of=start+pd.Timedelta(seconds=1),realtime=True)
    calls=[]
    original=engine._timeline.step_values
    def observed(*args,**kwargs):
        calls.append(kwargs)
        return original(*args,**kwargs)
    monkeypatch.setattr(engine._timeline,"step_values",observed)
    before=engine.stream_result()["utema_events"]
    engine.process(data,"NIFTY","5min",as_of=start+pd.Timedelta(minutes=5),realtime=True)
    assert calls==[]
    assert engine.stream_result()["utema_events"]==before
    assert engine._processed==len(data) and engine._stream_current is None


@pytest.mark.parametrize("heikin",[False,True])
def test_incremental_tick_previews_match_closed_baseline_pine_math(heikin,monkeypatch):
    import engine.ut_ema_engine as module
    data=_stream_seed()
    params=replace(Params(),ut_heikin=heikin)
    engine=UTEmaEngine(params)
    start=data.index[-1]
    engine.seed_stream(data,"NIFTY","5min",as_of=start+pd.Timedelta(seconds=1))
    compute=module.compute_indicators
    monkeypatch.setattr(module,"compute_indicators",lambda *a:pytest.fail("full history calculation on actual tick"))
    for offset,price in [(1,1001.),(2,1040.),(3,1039.),(4,1041.),(301,1042.),(302,1041.)]:
        engine.observe_tick(price,start+pd.Timedelta(seconds=offset))
        reference=compute(engine._stream_frame(),params)
        for key in reference:
            assert engine._stream_values[key][-1] == pytest.approx(reference[key][-1],nan_ok=True)


def test_stream_new_bar_commits_last_actual_quote_once(monkeypatch):
    data=_stream_seed(); engine=UTEmaEngine(); start=data.index[-1]
    engine.seed_stream(data,"NIFTY","5min",as_of=start+pd.Timedelta(seconds=1))
    engine.observe_tick(1040.,start+pd.Timedelta(seconds=1))
    count=len(engine._timeline.marks)
    first=engine._stream_baseline.rma["ema_atr"].count
    engine.observe_tick(1040.,start+pd.Timedelta(minutes=5,seconds=1))
    assert len(engine._timeline.marks)==count
    assert engine._processed==len(data)
    previous=dict(engine._stream_baseline.previous)
    engine.observe_tick(1040.,start+pd.Timedelta(minutes=5,seconds=2))
    assert engine._stream_baseline.previous==previous


def test_checkpoint_json_roundtrip_preserves_next_tick_and_varip_limits():
    import json
    data=_stream_seed(); engine=UTEmaEngine(); start=data.index[-1]
    engine.seed_stream(data,"NIFTY","5min",as_of=start+pd.Timedelta(seconds=1))
    for offset,price in [(1,1040.),(2,1039.),(3,1038.)]:
        engine.observe_tick(price,start+pd.Timedelta(seconds=offset))
    checkpoint=json.loads(json.dumps(engine.stream_checkpoint(),allow_nan=False))
    restored=UTEmaEngine()
    assert restored.restore_stream_checkpoint(checkpoint)
    assert restored.stream_result()["utema_events"]==engine.stream_result()["utema_events"]
    for offset,price in [(4,1041.),(301,1040.),(302,1035.)]:
        now=start+pd.Timedelta(seconds=offset)
        engine.observe_tick(price,now);restored.observe_tick(price,now)
        actual,expected=restored.stream_result(),engine.stream_result()
        assert actual["utema_events"]==expected["utema_events"]
        assert actual["utema_records"]==expected["utema_records"]
        assert actual["state"]==expected["state"]
        assert restored._timeline.signals_this_bar==engine._timeline.signals_this_bar


@pytest.mark.parametrize("bad",["schema","params","owner","counter","nan","length","protection","last_tick"])
def test_checkpoint_corruption_refused_without_replacing_live_state(bad):
    import copy
    data=_stream_seed();engine=UTEmaEngine();start=data.index[-1]
    engine.seed_stream(data,"NIFTY","5min",as_of=start+pd.Timedelta(seconds=1))
    engine.observe_tick(1040.,start+pd.Timedelta(seconds=1))
    payload=copy.deepcopy(engine.stream_checkpoint())
    before=engine._timeline
    if bad=="schema":payload["schema"]="arbitrary"
    elif bad=="params":payload["params"]["rr2"]=8.
    elif bad=="owner":payload["instrument"]="BANKNIFTY"
    elif bad=="counter":payload["timeline"]["signals_this_bar"]["UT"]=999
    elif bad=="nan":payload["indicator_state"]["ema_fast"]=float("nan")
    elif bad=="length":payload["rows"].pop()
    elif bad=="last_tick":payload["last_tick"]=[]
    else:payload["positions"]["UT"]["risk"]=-1.
    with pytest.raises(ValueError):engine.restore_stream_checkpoint(payload)
    assert engine._timeline is before


def test_stream_ignores_old_quotes_and_aligns_intraday_bars():
    import json
    data=_stream_seed();engine=UTEmaEngine();start=data.index[-1]
    engine.seed_stream(data,"NIFTY","5min",as_of=start+pd.Timedelta(seconds=1))
    engine.observe_tick(1040.,start+pd.Timedelta(seconds=2))
    before=json.dumps(engine.stream_checkpoint(),sort_keys=True,allow_nan=False)
    assert engine.observe_tick(1000.,start+pd.Timedelta(seconds=1)) is None
    assert json.dumps(engine.stream_checkpoint(),sort_keys=True,allow_nan=False)==before
    assert engine._tick_bar(pd.Timestamp("2026-10-29 09:29:59"),"15min")==pd.Timestamp("2026-10-29 09:15")
    assert engine._tick_bar(pd.Timestamp("2026-10-29 15:30:00"),"15min")==pd.Timestamp("2026-10-29 15:30")


def test_empty_frame_is_a_valid_empty_snapshot():
    result=UTEmaEngine().process(None,"NIFTY","5min",realtime=False,as_of="2026-10-29 10:00")
    assert result["state"]["bar_count"]==0 and result["signals"]==[]


@pytest.mark.parametrize("gap",["intraday","overnight_tail","overnight_open","entire_session"])
def test_stream_gap_pauses_before_mutating_indicator_or_position_state(gap):
    import json
    from engine.ut_ema_engine import StreamDataGap
    if gap=="intraday":
        data=_stream_seed();now=data.index[-1]+pd.Timedelta(seconds=1)
        incoming=data.index[-1]+pd.Timedelta(minutes=20)
    else:
        day="2026-10-22 13:00" if gap=="overnight_tail" else "2026-10-22 13:00"
        count=30 if gap=="overnight_tail" else 30
        data=frame([(1000.,1000.1,999.9,1000.)]*count,start=day)
        # Last observed bar15:25; override tail case with15:20.
        if gap=="overnight_tail":data=data.iloc[:-1]
        now=data.index[-1]+pd.Timedelta(seconds=1)
        incoming=pd.Timestamp("2026-10-23 09:20" if gap=="overnight_open" else
            "2026-10-26 09:15" if gap=="entire_session" else "2026-10-23 09:15")
    engine=UTEmaEngine();engine.seed_stream(data,"NIFTY","5min",as_of=now)
    engine.observe_tick(1000.,now)
    positions=json.dumps(engine.stream_checkpoint()["positions"],sort_keys=True)
    indicator=json.dumps(engine.stream_checkpoint()["indicator_state"],sort_keys=True)
    times=list(engine._stream_times)
    with pytest.raises(StreamDataGap):engine.observe_tick(1001.,incoming)
    assert engine._stream_times==times
    assert json.dumps(engine.stream_checkpoint()["positions"],sort_keys=True)==positions
    assert json.dumps(engine.stream_checkpoint()["indicator_state"],sort_keys=True)==indicator
    assert engine.stream_result()["utema_live_path_available"] is False


def test_stream_overnight_carry_skips_only_closed_calendar_days(monkeypatch):
    import data.market_calendar as calendar
    monkeypatch.setattr(calendar,"load_nse_holidays",lambda:{"2026-10-26"})
    monkeypatch.setattr(calendar,"load_nse_special_trading_days",lambda:set())
    data=frame([(1000.,1000.1,999.9,1000.)]*30,start="2026-10-23 13:00")
    now=data.index[-1]+pd.Timedelta(seconds=1)
    engine=UTEmaEngine();engine.seed_stream(data,"NIFTY","5min",as_of=now)
    engine.observe_tick(1000.,now)
    engine.observe_tick(1000.,pd.Timestamp("2026-10-27 09:15:01"))
    assert engine._stream_times[-1]==pd.Timestamp("2026-10-27 09:15")
    assert engine.stream_result()["utema_live_path_available"] is True


def test_seed_to_first_tick_cannot_silently_jump_missing_closed_bars():
    from engine.ut_ema_engine import StreamDataGap
    data=_stream_seed().iloc[:-1]
    engine=UTEmaEngine()
    clock=data.index[-1]+pd.Timedelta(minutes=25)
    engine.seed_stream(data,"NIFTY","5min",as_of=clock)
    with pytest.raises(StreamDataGap):engine.observe_tick(1000.,clock)


def test_core_does_not_invent_a_regular_session_or_eod_gate():
    engine=UTEmaEngine()
    at=pd.Timestamp("2026-10-29 18:00:00")
    engine.seed_stream(None,"NIFTY","5min",as_of=at)
    engine.observe_tick(1000.,at)
    engine.observe_tick(1001.,at+pd.Timedelta(minutes=5))
    assert engine._stream_times==[at,at+pd.Timedelta(minutes=5)]


def test_host_can_supply_special_session_boundaries_without_false_gap(monkeypatch):
    import data.market_calendar as calendar
    monkeypatch.setattr(calendar,"load_nse_holidays",lambda:set())
    monkeypatch.setattr(calendar,"load_nse_special_trading_days",lambda:{"2026-10-24"})
    data=frame([(1000.,1000.1,999.9,1000.)]*30,start="2026-10-23 13:00")
    engine=UTEmaEngine();at=data.index[-1]+pd.Timedelta(seconds=1)
    engine.seed_stream(data,"NIFTY","5min",as_of=at)
    engine.observe_tick(1000.,at)
    evening=pd.Timestamp("2026-10-24 18:10")
    engine.observe_tick(1001.,evening+pd.Timedelta(seconds=1),session_open=evening,
        previous_session_close="2026-10-23 15:30")
    assert engine._stream_times[-1]==evening
    assert engine.stream_result()["utema_live_path_available"] is True



def test_checkpoint_keeps_special_session_bar_phase_without_repeating_bounds():
    import json
    engine=UTEmaEngine()
    opening=pd.Timestamp("2026-10-29 18:10")
    engine.seed_stream(None,"NIFTY","15min",as_of=opening)
    engine.observe_tick(1000.,opening,session_open=opening)
    restored=UTEmaEngine();restored.restore_stream_checkpoint(json.loads(json.dumps(engine.stream_checkpoint())))
    restored.observe_tick(1001.,opening+pd.Timedelta(minutes=1))
    assert restored._stream_times[-1]==opening
    restored.observe_tick(1001.,opening+pd.Timedelta(minutes=15,seconds=1))
    assert restored._stream_times[-1]==opening+pd.Timedelta(minutes=15)



def test_real_session_close_tick_updates_final_bar_without_resetting_counter():
    data=frame([(1000.,1000.1,999.9,1000.)]*30,start="2026-10-29 13:00")
    engine=UTEmaEngine();now=data.index[-1]+pd.Timedelta(seconds=1)
    engine.seed_stream(data,"NIFTY","5min",as_of=now)
    engine.observe_tick(1000.,now,session_open="2026-10-29 09:15",session_close="2026-10-29 15:30")
    engine._timeline.signals_this_bar["EMA"]=2
    engine.observe_tick(1000.,"2026-10-29 15:30",session_open="2026-10-29 09:15",session_close="2026-10-29 15:30")
    assert engine._stream_times[-1]==pd.Timestamp("2026-10-29 15:25")
    assert engine._timeline.signals_this_bar["EMA"]==2
    assert engine._timeline.observed_at==pd.Timestamp("2026-10-29 15:30")
    # Without host-provided bounds the native core does not invent this cutoff.
    assert engine._tick_bar(pd.Timestamp("2026-10-29 15:30"),"5min")==pd.Timestamp("2026-10-29 15:30")


def test_partial_special_session_final_tick_retains_its_anchor():
    assert UTEmaEngine._tick_bar(pd.Timestamp("2026-10-29 19:00"),"15min",
        session_open="2026-10-29 18:10",session_close="2026-10-29 19:00")==pd.Timestamp("2026-10-29 18:55")


@pytest.mark.parametrize("tick", [False, True])
def test_pending_settlement_keeps_actual_realtime_observation_clock(tick):
    data = frame([(1000., 1000.1, 999.9, 1000.)] * 2)
    timeline = Timeline(Params(), "NIFTY", "5min")
    timeline.pending["UT"] = 1
    observed_at = data.index[1] + pd.Timedelta(seconds=5 if tick else 300)
    timeline.step(1, data, _controlled_indicators(), tick=tick, new_bar=tick,
                  observed_at=observed_at)
    event = next(mark for mark in timeline.marks if mark["engine"] == "UT" and mark["entry"])
    assert event["price"] == 1000.
    assert event["bar_time"] == data.index[1].isoformat()
    assert event["observed_at"] == (observed_at if tick else data.index[1]).isoformat()
    assert event["realtime"] is tick
    assert timeline.observed_at == observed_at


@pytest.mark.parametrize("ohlc", [
    (100., 99., 101., 100.),
    (100., 101., 99., 102.),
    (100., 101., 100.5, 100.),
])
def test_prepare_rejects_impossible_candle_ranges_before_state_mutation(ohlc):
    engine = UTEmaEngine()
    valid = frame([(100., 101., 99., 100.)] * 20)
    engine.seed_stream(valid, "NIFTY", "5min", as_of=valid.index[-1] + pd.Timedelta(minutes=5))
    timeline, baseline = engine._timeline, engine._stream_baseline
    bad = valid.copy()
    bad.iloc[-1] = ohlc
    with pytest.raises(ValueError, match="consistent OHLC ranges"):
        engine.process(bad, "NIFTY", "5min", realtime=True,
                       as_of=bad.index[-1] + pd.Timedelta(minutes=5))
    assert engine._timeline is timeline
    assert engine._stream_baseline is baseline


@pytest.mark.parametrize("timeframe,opening,closing,bars", [
    ("5min", "2026-10-29 09:15", "2026-10-29 15:30", 75),
    ("15min", "2026-10-29 09:15", "2026-10-29 15:30", 25),
    ("15min", "2026-10-29 18:10", "2026-10-29 19:00", 4),
])
def test_cold_seed_at_session_close_leaves_final_bar_for_actual_tick(timeframe, opening, closing, bars):
    data = frame([(1000., 1000.1, 999.9, 1000.)] * bars, start=opening, tf=timeframe)
    engine = UTEmaEngine()
    engine.seed_stream(data, "NIFTY", timeframe, as_of=closing,
                       session_open=opening, session_close=closing)
    assert len(engine._stream_times) == bars - 1
    assert engine._stream_seed_context["time"] == data.index[-1]
    engine.observe_tick(1001., closing, session_open=opening, session_close=closing)
    assert len(engine._stream_times) == bars
    assert engine._stream_times[-1] == data.index[-1]
    assert engine._stream_current == bars - 1
    assert engine._stream_rows[-1][-1] == 1001.
    assert engine._timeline.observed_at == pd.Timestamp(closing)
    assert engine._last_tick is not None


def test_seed_without_session_bounds_keeps_unconstrained_clock_semantics():
    data = frame([(1000., 1000.1, 999.9, 1000.)] * 75, start="2026-10-29 09:15")
    engine = UTEmaEngine()
    engine.seed_stream(data, "NIFTY", "5min", as_of="2026-10-29 15:30")
    assert len(engine._stream_times) == 75
    assert engine._stream_current is None
    assert engine._stream_seed_context is None


@pytest.mark.parametrize("bars,previous_close,next_open,allowed", [
    (4, "2026-10-29 19:00", "2026-10-30 09:15", True),
    (3, "2026-10-29 19:00", "2026-10-30 09:15", False),
    (4, None, "2026-10-30 09:15", False),
    (4, "2026-10-29 19:00", "2026-11-02 09:15", False),
])
def test_partial_special_session_tail_carries_only_with_complete_explicit_bounds(
        bars, previous_close, next_open, allowed):
    from engine.ut_ema_engine import StreamDataGap
    data = frame([(1000., 1000.1, 999.9, 1000.)] * bars, start="2026-10-29 18:10", tf="15min")
    engine = UTEmaEngine()
    first_tick = data.index[-1] + pd.Timedelta(seconds=1)
    engine.seed_stream(data, "NIFTY", "15min", as_of=first_tick,
        session_open="2026-10-29 18:10", session_close="2026-10-29 19:00")
    engine.observe_tick(1000., first_tick, session_open="2026-10-29 18:10",
        session_close="2026-10-29 19:00")
    if bars == 4:
        engine.observe_tick(1000., "2026-10-29 19:00", session_open="2026-10-29 18:10",
            session_close="2026-10-29 19:00")
    options = {"session_open": next_open, "session_close": pd.Timestamp(next_open).normalize() + pd.Timedelta(hours=15, minutes=30)}
    if previous_close is not None:
        options["previous_session_close"] = previous_close
    if allowed:
        engine.observe_tick(1001., pd.Timestamp(next_open) + pd.Timedelta(seconds=1), **options)
        assert engine._stream_times[-1] == pd.Timestamp(next_open)
        assert engine.stream_result()["utema_live_path_available"] is True
    else:
        prior_times = list(engine._stream_times)
        with pytest.raises(StreamDataGap):
            engine.observe_tick(1001., pd.Timestamp(next_open) + pd.Timedelta(seconds=1), **options)
        assert engine._stream_times == prior_times
