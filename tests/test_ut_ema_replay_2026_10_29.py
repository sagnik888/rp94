"""Observed-price historical parity and strict separation from broker execution."""
from threading import RLock
from types import SimpleNamespace
from dataclasses import replace

import pandas as pd
import pytest

from engine.ut_ema_replay import ReplayDataGap, _alignment_levels, replay_candidates, simulate_record


def native(**changes):
    value = dict(lifecycle_id="UTEMA|NIFTY|5min|2026-10-20T10:00:00|1|0", side=1,
        entry_engine="UT", entry_spot=1000., entry_time="2026-10-20T10:00:00",
        entry_bar="2026-10-20T10:00:00", initial_stop=998., stop=998.,
        alignment_lot=0, aligned_engine="", stop_updates=[],
        exit_time="2026-10-20T10:03:00", exit_price=1004., exit_reason="EMA T2")
    value.update(changes)
    return value


def options(rows=None, start="2026-10-20 10:00"):
    rows = rows or [(100., 100.5, 99.5, 100.)] * 6
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                         index=pd.date_range(start, periods=len(rows), freq="1min"))
    frame.attrs.update(contract_symbol="NIFTY_TEST_CE", contract_verified=True,
                       contract_expiry="2026-10-29", contract_strike=1000., contract_option_type="CE")
    return frame


def test_first_native_exit_uses_current_open_without_future_bar_range():
    bars = options()
    bars.loc[pd.Timestamp("2026-10-20 10:03")] = (103., 999., 1., 600.)
    result = simulate_record(native(), bars, 2, 2.5, 1., "2026-10-20 10:05")
    assert result["reason"] == "UTEMA_EMA_T2"
    assert result["exit_price"] == 103.
    assert result["exit_time"] == pd.Timestamp("2026-10-20 10:03")
    assert len(result["price_path"]) == 3


def test_unobserved_invalid_future_ohlc_cannot_reject_current_open_exit():
    bars = options()
    bars.loc[pd.Timestamp("2026-10-20 10:03")] = (103., float("nan"), 0., -1.)
    result = simulate_record(native(), bars, 2, 2.5, 1., "2026-10-20 10:03")
    assert result["reason"] == "UTEMA_EMA_T2"
    assert result["exit_price"] == 103.


def test_native_t2_does_not_replace_selected_premium_target():
    result = simulate_record(native(), options(), 1, 4., 1., "2026-10-20 10:05")
    assert result["target"] == 108.
    assert result["reason"] == "UTEMA_EMA_T2"


def test_ambiguous_option_bar_stop_wins_target():
    result = simulate_record(native(), options([(100., 120., 90., 105.)]), 1, 2.5, 1., "2026-10-20 10:01")
    assert result["reason"] == "UTEMA_PREMIUM_STOP"
    assert result["exit_price"] == 98.
    assert result["exit_trace"][0]["basis"] == "minute_range_stop_first"


@pytest.mark.parametrize("opening,reason", [(95., "UTEMA_PREMIUM_STOP"), (110., "UTEMA_PREMIUM_TARGET")])
def test_gaps_execute_at_open(opening, reason):
    bars = options([(100., 100.5, 99.5, 100.), (opening, opening+.1, opening-.1, opening)])
    result = simulate_record(native(), bars, 1, 2.5, 1., "2026-10-20 10:02")
    assert result["reason"] == reason
    assert result["exit_price"] == opening


def test_alignment_adds_one_lot_and_freezes_risk_percentage():
    record = native(alignment_lot=1, alignment_time="2026-10-20T10:01:00", aligned_engine="EMA")
    bars = options([(100., 100.5, 99.5, 100.), (102., 102.2, 101.8, 102.),
                    (102., 102.2, 101.8, 102.), (103., 103.2, 102.8, 103.)])
    result = simulate_record(record, bars, 2, 2.5, 1., "2026-10-20 10:04")
    assert result["lots"] == 3
    assert result["entry_price"] == pytest.approx((100*2+102)/3)
    assert result["risk_pct"] == 2.
    assert result["stop"] == 98.7
    assert result["target"] == pytest.approx(result["entry_price"] + (result["entry_price"]-98.7)*2.5)
    assert len(result["buys"]) == 2
    assert result["buys"][1]["lots"] == 1


def test_simultaneous_entry_alignment_uses_base_plus_one():
    result = simulate_record(native(alignment_lot=1, alignment_time="2026-10-20T10:00:00"),
        options(), 3, 2.5, 1., "2026-10-20 10:05")
    assert result["lots"] == 4
    assert result["entry_price"] == 100.


def test_stop_gap_prevents_alignment_add():
    record = native(alignment_lot=1, alignment_time="2026-10-20T10:01:00")
    bars = options([(100., 100.5, 99.5, 100.), (90., 91., 89., 90.)])
    result = simulate_record(record, bars, 2, 2.5, 1., "2026-10-20 10:02")
    assert result["lots"] == 2
    assert not result["alignment_added"]


def test_risk_cap_has_no_minimum_floor():
    from engine.ut_ema_lane import premium_stop_distance
    assert premium_stop_distance(100., .01, .01) == pytest.approx(.0001)
    small = simulate_record(native(initial_stop=999.9), options([(1000., 1000., 1000., 1000.)]),
                            1, 2.5, .5, "2026-10-20 10:00")
    assert small["risk_pct"] == pytest.approx(.005)
    cap = simulate_record(native(initial_stop=900.), options(), 1, 2.5, 1., "2026-10-20 10:00")
    assert cap["risk_pct"] == 15.


def test_initial_tick_rounding_keeps_stop_cap_and_selected_rr_actual_risk():
    result = simulate_record(native(initial_stop=997.96), options([(100.05, 100.05, 100.05, 100.05)]),
                             1, 3., .5, "2026-10-20 10:00")
    assert result["stop"] == 99.05
    assert result["target"] == pytest.approx(103.05)
    assert result["risk_pct"] == pytest.approx(100/100.05)


def test_unrepresentable_sub_tick_risk_is_not_expanded_to_minimum():
    with pytest.raises(ReplayDataGap, match="geometry"):
        simulate_record(native(initial_stop=999.99), options(), 1, 2.5, .01, "2026-10-20 10:00")


def test_alignment_keeps_previous_stop_when_weighted_average_falls():
    record = native(alignment_lot=1, alignment_time="2026-10-20T10:01:00")
    bars = options([(100., 100.1, 99.9, 100.), (98.5, 98.6, 98.4, 98.5)])
    result = simulate_record(record, bars, 1, 2.5, 1., "2026-10-20 10:02")
    assert result["entry_price"] == 99.25
    assert result["stop"] == 98.
    assert result["target"] == 102.375


def test_alignment_preserves_profitable_target_if_stop_meets_average():
    assert _alignment_levels(100., .02, 101., 110., 3.) == (101., 110.)
    assert _alignment_levels(100., .02, 100., 110., 3.) == (100., 110.)


def test_missing_exact_entry_does_not_borrow_future_option_open():
    with pytest.raises(ReplayDataGap, match="entry-minute"):
        simulate_record(native(), options(start="2026-10-20 10:01"), 1, 2.5, 1., "2026-10-20 10:05")


def test_missing_minute_refuses_complete_history():
    bars = options().drop(pd.Timestamp("2026-10-20 10:01"))
    with pytest.raises(ReplayDataGap, match="minute missing"):
        simulate_record(native(), bars, 1, 2.5, 1., "2026-10-20 10:05")


def test_no_forced_eod_exit_when_pine_record_stays_open():
    record = native(entry_time="2026-10-20T15:28:00", exit_time="", exit_reason="")
    result = simulate_record(record, options(start="2026-10-20 15:28").iloc[:2],
                             1, 2.5, 1., "2026-10-20 15:30")
    assert not result["closed"]
    assert result["mark_time"] == pd.Timestamp("2026-10-20 15:30")


def test_overnight_gap_rejects_missing_session_tail_and_next_open():
    record = native(entry_time="2026-10-20T15:28:00", exit_time="2026-10-21T09:18:00")
    bars = pd.concat([options(start="2026-10-20 15:28").iloc[:1],
                      options(start="2026-10-21 09:17").iloc[:2]])
    with pytest.raises(ReplayDataGap, match="session tail"):
        simulate_record(record, bars, 1, 2.5, 1., "2026-10-21 09:19")
    bars = pd.concat([options(start="2026-10-20 15:28").iloc[:2],
                      options(start="2026-10-21 09:17").iloc[:2]])
    with pytest.raises(ReplayDataGap, match="minute missing"):
        simulate_record(record, bars, 1, 2.5, 1., "2026-10-21 09:19")


def test_overnight_gap_accepts_only_complete_tail_and_next_session_open():
    record = native(entry_time="2026-10-21T15:28:00", exit_time="2026-10-22T09:16:00")
    bars = pd.concat([options(start="2026-10-21 15:28").iloc[:2],
                      options(start="2026-10-22 09:15").iloc[:2]])
    result = simulate_record(record, bars, 1, 2.5, 1., "2026-10-22 09:17")
    assert result["reason"] == "UTEMA_EMA_T2"
    assert result["exit_time"] == pd.Timestamp("2026-10-22 09:16")


def test_overnight_gap_skips_calendar_holiday_and_weekend():
    record = native(entry_time="2026-10-01T15:29:00", exit_time="2026-10-05T09:16:00")
    bars = pd.concat([options(start="2026-10-01 15:29").iloc[:1],
                      options(start="2026-10-05 09:15").iloc[:2]])
    result = simulate_record(record, bars, 1, 2.5, 1., "2026-10-05 09:17")
    assert result["closed"]


def test_overnight_gap_cannot_skip_an_entire_trading_session():
    record = native(entry_time="2026-10-21T15:29:00", exit_time="2026-10-23T09:16:00")
    bars = pd.concat([options(start="2026-10-21 15:29").iloc[:1],
                      options(start="2026-10-23 09:15").iloc[:2]])
    with pytest.raises(ReplayDataGap, match="minute missing"):
        simulate_record(record, bars, 1, 2.5, 1., "2026-10-23 09:17")


def test_open_record_requires_next_session_observations_after_overnight():
    record = native(entry_time="2026-10-21T15:29:00", exit_time="", exit_reason="")
    with pytest.raises(ReplayDataGap, match="end before"):
        simulate_record(record, options(start="2026-10-21 15:29").iloc[:1], 1, 2.5, 1., "2026-10-22 09:16")


def test_terminal_close_is_allowed_only_at_exact_native_boundary():
    result = simulate_record(native(), options().iloc[:3], 1, 2.5, 1., "2026-10-20 10:05")
    assert result["exit_time"] == pd.Timestamp("2026-10-20 10:03")
    assert result["exit_trace"][-1]["basis"] == "terminal_minute_close"
    with pytest.raises(ReplayDataGap, match="contemporaneous"):
        simulate_record(native(), options().iloc[:2], 1, 2.5, 1., "2026-10-20 10:05")


def harness(record=None):
    record = record or native()
    manager = SimpleNamespace(mode="HISTORICAL", closed_trades=[], open_trades={}, _lock=RLock())
    manager.open_trade = manager.close_trade = manager.scale_in_trade = lambda *a, **k: pytest.fail("Replay submitted an execution call")
    scanner = SimpleNamespace(mode="HISTORICAL", trades=manager, backtest_days=1,
        historical_session_cutoff_date=lambda: pd.Timestamp("2026-10-20").date(),
        allows_historical_trade_creation=lambda: True,
        candles=SimpleNamespace(get_candles=lambda *a: options().iloc[:1]),
        _days_to_expiry_for=lambda *a: 1.)
    processor = SimpleNamespace(scanner=scanner, _load_option_candles=lambda *a, **k: (options(), "NIFTY_TEST_CE", "123"),
                                _option_signed_delta=lambda *a: 1.)
    result = {"source_engine": "ut_ema", "utema_records": {record["lifecycle_id"]: record},
              "state": {"bar_time": "2026-10-20 10:00"}}
    mtf = SimpleNamespace(results_5min=result, results_15min={})
    settings = SimpleNamespace(ut_timeframe_entry_policy="PRIMARY_5", dynamic_rr_min=1., dynamic_rr_max=4.,
        dynamic_rr_5min_max=2.5, slippage_auto_calibration_enabled=False, slippage_opt_pct=.3, opt_cost=80.)
    return processor, mtf, settings


def test_history_writes_native_owned_observed_trade_and_is_idempotent():
    processor, mtf, settings = harness()
    assert replay_candidates(processor, "NIFTY", mtf, {"strike_interval": 50}, 2, 75, settings) == []
    manager = processor.scanner.trades
    assert len(manager.closed_trades) == 1
    trade = manager.closed_trades[0]
    assert trade.entry_price == 100.
    assert trade.lots == 2
    assert trade.exit_reason == "UTEMA_EMA_T2"
    assert trade.source_lane == trade.exit_policy_owner == "UTEMA"
    assert trade.quality_verdict["checks"]["utema_state"]["rr"] == 2.5
    assert trade.data_provenance == "BACKTEST_OBSERVED_OPTION_MINUTES"
    assert trade.charges > 0
    replay_candidates(processor, "NIFTY", mtf, {"strike_interval": 50}, 2, 75, settings)
    assert len(manager.closed_trades) == 1


def test_selected_rr_change_recalculates_existing_history_without_duplicate():
    processor, mtf, settings = harness()
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    settings.dynamic_rr_5min_max = 3.5
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert len(processor.scanner.trades.closed_trades) == 1
    assert processor.scanner.trades.closed_trades[0].target == 107.


def test_mode_switch_while_loading_does_not_write_real_book():
    processor, mtf, settings = harness()
    def load(*args, **kwargs):
        processor.scanner.trades.mode = "REAL"
        return options(), "NIFTY_TEST_CE", "123"
    processor._load_option_candles = load
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.closed_trades


def test_future_native_ratchet_does_not_leak_into_open_replay_state():
    record = native(exit_time="", exit_reason="", stop=1003.,
        stop_updates=[{"time": "2026-10-20T10:07:00", "stop": 1003.}])
    processor, mtf, settings = harness(record)
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    trade = next(iter(processor.scanner.trades.open_trades.values()))
    assert trade.utema_state["stop"] == 998.


@pytest.mark.parametrize("scanner_mode,manager_mode", [("REAL", "REAL"), ("HISTORICAL", "REAL"), ("REAL", "HISTORICAL")])
def test_replay_never_mutates_any_real_book(scanner_mode, manager_mode):
    processor, mtf, settings = harness()
    processor.scanner.mode, processor.scanner.trades.mode = scanner_mode, manager_mode
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.closed_trades
    assert not processor.scanner.trades.open_trades


def test_missing_observed_history_reports_gap_without_modeled_fill():
    processor, mtf, settings = harness()
    processor._load_option_candles = lambda *a, **k: (pd.DataFrame(), "", "")
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.closed_trades
    assert "unavailable" in next(iter(processor.scanner._utema_replay_coverage.values()))


def test_cutoff_excludes_today_in_progress_and_timeframe_selection_applies():
    processor, mtf, settings = harness()
    processor.scanner.historical_session_cutoff_date = lambda: pd.Timestamp("2026-10-19").date()
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.closed_trades
    processor.scanner.historical_session_cutoff_date = lambda: pd.Timestamp("2026-10-20").date()
    settings.ut_timeframe_entry_policy = "PRIMARY_15"
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.closed_trades


def test_native_open_record_stays_in_simulated_open_book():
    processor, mtf, settings = harness(native(exit_time="", exit_reason=""))
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.closed_trades
    assert len(processor.scanner.trades.open_trades) == 1
    trade = next(iter(processor.scanner.trades.open_trades.values()))
    assert trade.status == "OPEN"
    assert not trade.exit_reason


def test_disappeared_native_lifecycle_prunes_both_history_and_coverage():
    processor, mtf, settings = harness()
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert processor.scanner.trades.closed_trades
    mtf.results_5min["utema_records"] = {}
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.closed_trades
    assert not processor.scanner._utema_replay_coverage


def test_retimed_native_lifecycle_replaces_old_history():
    processor, mtf, settings = harness()
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    old = processor.scanner.trades.closed_trades[0]
    corrected = native(lifecycle_id="UTEMA|NIFTY|5min|2026-10-20T10:01:00|1|0",
                       entry_time="2026-10-20T10:01:00", entry_bar="2026-10-20T10:01:00")
    mtf.results_5min["utema_records"] = {corrected["lifecycle_id"]: corrected}
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert len(processor.scanner.trades.closed_trades) == 1
    assert processor.scanner.trades.closed_trades[0].id != old.id
    assert processor.scanner.trades.closed_trades[0].entry_time.minute == 1


def test_missing_data_after_success_removes_unverified_old_trade():
    processor, mtf, settings = harness()
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    processor._load_option_candles = lambda *a, **k: (pd.DataFrame(), "", "")
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.closed_trades
    assert "unavailable" in next(iter(processor.scanner._utema_replay_coverage.values()))


def test_disappeared_open_lifecycle_is_pruned_from_simulated_open_book():
    processor, mtf, settings = harness(native(exit_time="", exit_reason=""))
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert processor.scanner.trades.open_trades
    mtf.results_5min["utema_records"] = {}
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert not processor.scanner.trades.open_trades


def test_pruning_preserves_other_owner_instrument_timeframe_and_date():
    processor, mtf, settings = harness()
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    manager = processor.scanner.trades
    original = manager.closed_trades[0]
    retained = [replace(original, id=original.id+"other_owner", source_lane="UTS"),
                replace(original, id=original.id+"other_index", instrument="SENSEX"),
                replace(original, id=original.id+"other_frame", timeframe="15min"),
                replace(original, id=original.id+"other_date", entry_time=pd.Timestamp("2026-10-19 10:00").to_pydatetime())]
    manager.closed_trades.extend(retained)
    mtf.results_5min["utema_records"] = {}
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert manager.closed_trades == retained


def test_mode_guard_preserves_existing_book_when_replay_pruning_not_authorized():
    processor, mtf, settings = harness()
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    original = list(processor.scanner.trades.closed_trades)
    processor.scanner.trades.mode = "REAL"
    mtf.results_5min["utema_records"] = {}
    replay_candidates(processor, "NIFTY", mtf, {}, 1, 75, settings)
    assert processor.scanner.trades.closed_trades == original



def test_replay_uses_observed_contract_expiry_instead_of_calendar_dte():
    processor, mtf, settings = harness()
    processor.scanner._days_to_expiry_for = lambda *a: pytest.fail("calendar DTE used")
    observed=[]
    processor._option_signed_delta=lambda spot,strike,kind,intel,dte,fallback: observed.append(dte) or 1.
    replay_candidates(processor,"NIFTY",mtf,{},1,75,settings)
    trade=processor.scanner.trades.closed_trades[0]
    assert observed==[9.]
    assert trade.utema_state["option_dte_at_entry"]==9.
    assert trade.utema_state["option_contract_evidence"]["contract_expiry"]=="2026-10-29"
    proof=next(item for item in trade.replay_exit_trace if item.get("guard")=="replay_input")
    assert proof["state"]=="VERIFIED_PRICE_PATH" and proof["contract_verified"]
    assert proof["complete_path"] and len(proof["input_sha256"])==64
    assert proof["source_engine"]=="ut_ema"
    assert proof["entry_price"]==trade.entry_price and proof["exit_price"]==trade.exit_price
    assert proof["lots"]==trade.lots and proof["lot_size"]==trade.lot_size
    assert proof["charges"]==trade.charges and proof["pnl"]==trade.pnl


@pytest.mark.parametrize("metadata,reason",[
    ({"contract_expiry":""},"expiry"),({"contract_verified":False},"unverified"),
    ({"contract_symbol":"OTHER"},"symbol"),({"contract_strike":1050.},"strike"),
    ({"contract_option_type":"PE"},"type"),({"contract_expiry":"2026-10-19"},"expired"),
    ({"contract_expiry":"invalid"},"expiry is invalid"),
])
def test_replay_missing_or_mismatched_observed_contract_metadata_reports_gap(metadata,reason):
    processor, mtf, settings = harness()
    def load(*a,**kw):
        frame=options()
        frame.attrs.update(metadata)
        return frame,"NIFTY_TEST_CE","123"
    processor._load_option_candles=load
    replay_candidates(processor,"NIFTY",mtf,{},1,75,settings)
    assert not processor.scanner.trades.closed_trades and not processor.scanner.trades.open_trades
    error=next(iter(processor.scanner._utema_replay_coverage.values()))
    assert reason.lower() in error.lower()


def test_replay_metadata_controls_delta_and_risk_geometry():
    processor,mtf,settings=harness()
    processor.scanner._days_to_expiry_for=lambda *a:1.
    processor._option_signed_delta=lambda spot,strike,kind,intel,dte,fallback: .9 if dte==9 else .5
    replay_candidates(processor,"NIFTY",mtf,{},1,75,settings)
    trade=processor.scanner.trades.closed_trades[0]
    assert trade.utema_state["option_delta"]==.9
    assert trade.premium_stop==pytest.approx(98.2)
    assert trade.target==pytest.approx(104.5)
