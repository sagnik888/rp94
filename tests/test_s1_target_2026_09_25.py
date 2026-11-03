"""S1 target, 2026-09-25 user spec.

"Basic should always try to keep 1R in general trades, but if not much move/volume then
drop to 0.7R; open to 10R, 20R, even 30R if the market is trending in favour of the trade."

- Entry: 1R, or 0.7R when the signal bar shows a weak move and no clear trend regime backs
  the trade. Weak = at least two of: volume below 1.2x its 20-bar average, ADX below 20,
  ATR below its 50-bar average.
- In the trade: a quiet closed bar after the signal (same rule) with the S1 trend not with
  the trade brings the target in to 0.7R; it never moves it further out.
- While the S1 trend is clearly with the trade the target does not close it; the profit is
  taken at 30R (the other S1 exits still apply on the way).
- The final entry check and the rebuilt execution levels use the S1 floor (0.5R), not the
  system's 1.5R, which would refuse every 1R S1 trade.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from config.settings import Settings
from engine import s1_lane
from engine.entry_policy import ExpectedMoveForecast, evaluate_final_entry_policy


def _settings(**overrides):
    s = Settings()
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


def test_target_is_1r_normally_and_0_7r_on_a_weak_move():
    s = _settings()
    assert s1_lane.target_rr(s) == 1.0
    assert s1_lane.target_rr(s, weak=True) == 0.7


@pytest.mark.parametrize("adx,vol,atr,weak", [
    (30, 2.0, 1.3, False),   # moving: trend strength, volume and range
    (15, 2.0, 1.3, False),   # one weak sign only
    (15, 1.0, 1.3, True),    # low ADX and ordinary volume
    (30, 1.0, 0.8, True),    # ordinary volume and a shrinking range
    (15, 2.0, 0.8, True),    # low ADX and a shrinking range
    (None, None, None, False),
])
def test_weak_move_needs_two_of_volume_adx_and_range(adx, vol, atr, weak):
    assert s1_lane.weak_move(adx, vol, atr, _settings()) is weak


def test_a_clear_trend_regime_with_the_trade_is_never_weak_at_entry():
    sig = SimpleNamespace(adx_value=15.0, s1_rel_volume=1.1, s1_atr_expansion=0.9)
    assert s1_lane.entry_is_weak(sig, "RANGING", "SHORT", _settings()) is True
    assert s1_lane.entry_is_weak(sig, "TRENDING_DOWN", "SHORT", _settings()) is False
    assert s1_lane.entry_is_weak(sig, "TRENDING_UP", "SHORT", _settings()) is True


def test_signals_carry_the_bar_volume_and_range_the_rule_reads():
    from engine.s1_engine import get_s1_signals
    from tests.test_s1_engine_2026_09_23 import _frame

    signals = get_s1_signals(_frame(11), "NIFTY", "5min")[0]
    assert signals
    for sig in signals:
        assert sig.s1_rel_volume > 1.2          # every S1 signal needs above-average volume
        assert sig.s1_atr_expansion > 0


# ---------------------------------------------------------------- in the trade


def _trade(**kw):
    base = dict(instrument="NIFTY", timeframe="5min", direction="SHORT", inst_type="OPT", entry_price=150.0,
                initial_stop=135.0, underlying_entry=23100.0, underlying_stop=23123.1, s1_score=70.0,
                signal_time=datetime(2026, 9, 24, 13, 40), lots=1, lot_size=65, quantity=65, peak_pnl=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


QUIET = {"adx_value": 14.0, "s1_rel_volume": 0.6, "s1_atr_expansion": 0.9}
BUSY = {"adx_value": 32.0, "s1_rel_volume": 1.8, "s1_atr_expansion": 1.3}


def test_a_quiet_bar_brings_the_target_in_to_0_7r():
    d = s1_lane.exit_decision(_trade(), spot=23095.0, premium=152.0, settings=_settings(), market=QUIET)
    assert d.exit is False and d.target_premium == pytest.approx(150.0 + 15.0 * 0.7)
    assert s1_lane.exit_decision(_trade(), spot=23095.0, premium=152.0, settings=_settings(),
                                 market=BUSY).target_premium == 0.0


def test_a_trend_with_the_trade_keeps_the_target_away_even_on_a_quiet_bar():
    d = s1_lane.exit_decision(_trade(), spot=23095.0, premium=152.0, settings=_settings(), trend=1, market=QUIET)
    assert d.runner is True and d.target_premium == 0.0


def test_the_runner_takes_profit_at_4r_in_an_ordinary_trend():
    s = _settings()
    under = s1_lane.exit_decision(_trade(s1_peak_premium=205.0), spot=22900.0, premium=205.0, settings=s, trend=1)
    assert under.exit is False and under.runner is True                    # 3.7R: still running
    at = s1_lane.exit_decision(_trade(s1_peak_premium=210.0), spot=22900.0, premium=210.0, settings=s, trend=1)
    assert at.exit is True and at.reason == "S1_RUNNER_MAX"                 # 60 gain = 4 x 15


SPECIAL = {"s1_bull_score": 10.0, "s1_bear_score": 60.0, "adx_value": 35.0, "s1_rel_volume": 2.0,
           "s1_atr_expansion": 1.4}


def test_a_special_trend_lets_the_runner_reach_30r():
    """2026-09-25 user: 30R for special trending moments, T3 (4R) in ordinary markets."""
    s = _settings()
    kw = dict(spot=22000.0, settings=s, trend=1, market=SPECIAL, regime="TRENDING_DOWN")
    assert s1_lane.exit_decision(_trade(s1_peak_premium=590.0), premium=590.0, **kw).exit is False   # 29.3R
    at = s1_lane.exit_decision(_trade(s1_peak_premium=600.0), premium=600.0, **kw)
    assert at.exit is True and at.reason == "S1_RUNNER_MAX"                                         # 30R


@pytest.mark.parametrize("market,regime,lots_mode,special", [
    (SPECIAL, "TRENDING_DOWN", "", True),
    (SPECIAL, "RANGING", "", False),                                    # no clear trend regime
    (SPECIAL, "TRENDING_UP", "", False),                                # regime against a SHORT
    (dict(SPECIAL, s1_bull_score=30.0), "TRENDING_DOWN", "", False),   # gap 30 < 40
    (None, "RANGING", "heavy_trend", True),                             # heavy-trend entry
])
def test_special_trend_needs_a_wide_s1_gap_and_a_trend_regime_or_a_heavy_trend_entry(market, regime, lots_mode, special):
    trade = _trade(lot_sizing_mode=lots_mode)
    assert s1_lane.special_trend(trade, market, regime, _settings()) is special
    assert s1_lane.runner_ceiling_r(trade, market, regime, _settings()) == (30.0 if special else 4.0)


def test_the_profit_lock_arms_at_one_r():
    # 2026-10-09 (user): 0.5R with a 10% giveback closed live winners on the first dip.
    s = _settings()
    assert s.s1_lane_lock_arm_r == 1.0 and s.s1_lane_breakeven_arm_r == 1.0
    assert s.s1_lane_giveback_unknown == 25.0
    # 0.6R peak (premium 159) then back to 0.3R (154.5): not armed, the trade keeps running.
    d = s1_lane.exit_decision(_trade(s1_peak_premium=159.0), spot=23095.0, premium=154.5, settings=s)
    assert d.exit is False
    # A with-trend runner that peaked at 1.2R (168) and gave back 44% is locked.
    d = s1_lane.exit_decision(_trade(s1_peak_premium=168.0), spot=23095.0, premium=160.0, settings=s, trend=1)
    assert d.exit is True and d.reason == "S1_PROFIT_LOCK"


def test_live_trades_carry_the_s1_entry_facts_the_exits_read():
    from scanner import Scanner

    trade = SimpleNamespace()
    cand = SimpleNamespace(source_engine="s1", s1_score=88.0, s1_guard_pct=15.0, s1_weak_target=False,
                           lot_sizing_mode="heavy_trend")
    Scanner._carry_s1_trade_attrs(trade, cand)
    assert (trade.s1_score, trade.s1_guard_pct, trade.lot_sizing_mode) == (88.0, 15.0, "heavy_trend")
    other = SimpleNamespace()
    Scanner._carry_s1_trade_attrs(other, SimpleNamespace(source_engine="utbot", s1_score=1.0))
    assert not hasattr(other, "s1_score")


def test_a_target_only_ever_comes_closer():
    assert s1_lane.nearer_target(172.5, 160.5, 1) == 160.5
    assert s1_lane.nearer_target(160.5, 172.5, 1) == 160.5
    assert s1_lane.nearer_target(0.0, 160.5, 1) == 0.0          # no target: none invented
    assert s1_lane.nearer_target(24900.0, 24950.0, -1) == 24950.0


def test_the_session_row_loop_lowers_the_target_on_a_quiet_bar(monkeypatch):
    from tests.test_s1_lane_exits_2026_09_24 import _row, _run_rows

    row = _row(target=172.5, stop=135.0)
    state = dict(QUIET, s1_bull_score=40.0, s1_bear_score=50.0, s1_state_bar_time=datetime(2026, 9, 24, 13, 50))
    reasons, _ = _run_rows(monkeypatch, row, spot=23095.0, premium=152.0, engine_state=state)
    assert reasons == [] and row.target == pytest.approx(150.0 + 15.0 * 0.7)
    reasons, _ = _run_rows(monkeypatch, row, spot=23090.0, premium=161.0, engine_state=state)
    assert reasons == ["TARGET_HIT"]


def test_the_broker_trade_monitor_lowers_the_target_on_a_quiet_bar(monkeypatch):
    from tests.test_s1_lane_2026_09_23 import _open_trade, _scanner

    trade = _open_trade(target=115.0)
    state = dict(QUIET, s1_bull_score=50.0, s1_bear_score=40.0, s1_state_bar_time=datetime(2026, 9, 24, 10, 10))
    _, sc = _scanner(monkeypatch, mode="HISTORICAL", auto_mode=True)
    closed, updated = [], []
    sc.on_notification = None
    sc._reset_daily_counters = lambda: None
    sc.trades = SimpleNamespace(open_trades={"T1": trade}, closed_trades=[],
                                update_trade=lambda tid, price, stop, **kw: updated.append(kw))
    sc.candles = SimpleNamespace(get_latest_price=lambda inst: 23405.0,
                                 get_max_timestamp=lambda: datetime(2026, 9, 24, 10, 30))
    sc._trade_contract_tick_path = lambda t: []
    sc._get_live_premium_cached = lambda t, allow_blocking=False: 102.0
    sc._active_option_decision_mark = lambda t, px, prem: (102.0, "test", True)
    sc._get_trade_regime = lambda t, default="UNKNOWN": "RANGING"
    sc._commit_trade_contract_tick_path = lambda t: None
    sc._record_trade_manager_close = lambda *a: None
    sc.log_event = lambda *a, **k: None
    sc.mtf = SimpleNamespace(_result_cache={"NIFTY_5min": {"signals": [], "state": state}})
    sc._close_and_record = lambda tid, px, reason, exit_time=None: closed.append(reason)
    sc._update_active_trades(allow_blocking=False)
    assert closed == [] and trade.target == pytest.approx(100.0 + 10.0 * 0.7)


# ---------------------------------------------------------------- entry checks


def _forecast():
    return ExpectedMoveForecast(
        instrument="NIFTY", direction="LONG", timeframe="5min", horizon_minutes=30, horizon_bars=6,
        expected_favorable_move_pct=0.0, q10_pct=0.0, q25_pct=0.0, q50_pct=0.0, uncertainty_pct=0.0,
        probability_meeting_floor=0.0, floor_pct=0.1, sample_count=0, generated_at="", data_as_of="",
        available=False, reason="no model",
    )


def test_the_final_entry_check_admits_a_1r_s1_trade_but_not_for_other_engines():
    from engine.entry_policy import ExecutionGeometry

    ue = 23400.0
    one_r = ExecutionGeometry(
        instrument="NIFTY", direction="LONG", inst_type="OPT",
        underlying_entry=ue, underlying_stop=ue * 0.999, underlying_target=ue * 1.001,
        premium_entry=100.0, premium_stop=90.0, premium_target=110.0,
        contract_entry=100.0, contract_stop=90.0, contract_target=110.0,
        observed_at=datetime(2026, 9, 24, 10, 0).isoformat(), source="broker_ltp", verified=True,
    )
    common = dict(min_rr=1.5, expected_move_floor_pct=0.1, max_underlying_stop_pct=0.15,
                  max_forecast_age_seconds=90, max_geometry_age_seconds=10,
                  checked_at=datetime(2026, 9, 24, 10, 0), phase="entry",
                  underlying_stop_cap_override_pct=0.25, require_forecast=False)
    s1 = evaluate_final_entry_policy(one_r, _forecast(), min_rr_override=0.5, **common)
    plain = evaluate_final_entry_policy(one_r, _forecast(), **common)
    assert s1.actual_rr == pytest.approx(1.0)
    assert s1.min_rr == 0.5 and dict(s1.checks)["execution_rr"] is True
    assert plain.min_rr == 1.5 and dict(plain.checks)["execution_rr"] is False


def test_rebuilt_levels_keep_the_s1_floor(monkeypatch):
    from scanner import Scanner

    from config.settings import get_settings

    # The configured floor (0.5) applies with the 2026-10-08 target floor switched off;
    # with it on the floor is the base target (test_payoff_fixes_2026_10_08.py).
    monkeypatch.setattr(get_settings(), "s1_lane_rr_floor_at_target", False, raising=False)
    sc = Scanner.__new__(Scanner)
    assert sc._min_execution_rr(SimpleNamespace(source_engine="s1")) == 0.5
    assert sc._min_execution_rr(SimpleNamespace(source_engine="utbot")) == 1.5
    stop, target, _, _ = sc._cap_option_geometry_to_underlying_policy(
        instrument="NIFTY", regime="RANGING", spot=23100.0, premium=150.0, stop=140.0, target=160.0,
        signed_delta=0.5, rr_hint=1.0, min_rr=0.5)
    assert (target - 150.0) / (150.0 - stop) == pytest.approx(1.0)       # a 1R S1 trade stays 1R


@pytest.mark.parametrize("lane,expected_stop", [("S1", 90.0), ("", 100.0 - 10.0 / 1.5)])
def test_the_broker_fill_recheck_keeps_a_1r_s1_trade_and_its_stop(lane, expected_stop):
    """The post-fill RR recheck used to lift every trade to 1.5R by tightening its stop."""
    from trading.trade_manager import IST, Trade, TradeManager

    manager = TradeManager.__new__(TradeManager)
    manager.fresh_entry_policy_guard = lambda trade: (True, "")
    trade = Trade(id="T1", instrument="NIFTY", timeframe="5min", direction="LONG", entry_price=100.0,
                  entry_time=datetime.now(IST), trailing_stop=90.0, current_stop=90.0, target=110.0,
                  lots=1, lot_size=65, grade="A", confidence=0.8, inst_type="OPT")
    trade.source_lane = lane
    allowed, reason = manager._actual_fill_entry_policy_verdict(trade)
    assert allowed is True, reason
    assert trade.current_stop == pytest.approx(expected_stop)
