"""2026-10-08 payoff-audit fixes: context gate, S1 RR floor/breakeven, v10 shadow,
equal-risk lots, closed-candle replay timing."""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from engine import entry_context_gate as gate
from engine import s1_lane
from engine.position_sizing import resolve_trade_lots
from engine.quality_verdict import QualityInputs, evaluate_quality
from engine.signal_processor import SignalProcessor


def _s(**kw):
    base = dict(ctx_gate_mode="shadow", ctx_peer_alignment_mode="enforce",
                ctx_peer_alignment_required_indices="MIDCPNIFTY", ctx_peer_alignment_timeframes="15min",
                ctx_peer_alignment_min=2)
    base.update(kw)
    return SimpleNamespace(**base)


def _inputs(**kw):
    base = dict(lane="QP", instrument="NIFTY", timeframe="5min", direction="LONG", regime="TRENDING_UP",
                adx=25.0, rsi=58.0, volume_ratio=1.1, volume_measured=True, atr_pct=0.08,
                range_bound=False, peers_aligned=2, peers_checked=3)
    base.update(kw)
    return gate.ContextInputs(**base)


def test_counter_trend_is_recorded_in_shadow_and_refused_when_enforced():
    shadow = gate.evaluate(_inputs(direction="SHORT", rsi=40.0), _s())
    assert shadow.allowed and any("counter-trend" in r for r in shadow.shadow_reasons)
    enforced = gate.evaluate(_inputs(direction="SHORT", rsi=40.0), _s(ctx_gate_mode="enforce"))
    assert not enforced.allowed and enforced.rule_id == "CONTEXT_COUNTER_TREND"


def test_clean_trend_setup_passes_enforced_table():
    assert gate.evaluate(_inputs(), _s(ctx_gate_mode="enforce")).allowed


def test_midcap_15m_choppy_needs_two_aligned_peers():
    blocked = gate.evaluate(_inputs(instrument="MIDCPNIFTY", timeframe="15min", regime="RANGING",
                                    peers_aligned=1), _s())
    assert not blocked.allowed and blocked.rule_id == "PEER_ALIGNMENT"
    passed = gate.evaluate(_inputs(instrument="MIDCPNIFTY", timeframe="15min", regime="RANGING",
                                   peers_aligned=2), _s())
    assert passed.allowed
    trending = gate.evaluate(_inputs(instrument="MIDCPNIFTY", timeframe="15min", peers_aligned=0), _s())
    assert trending.allowed  # clear trend with the trade: rule does not apply
    other_tf = gate.evaluate(_inputs(instrument="MIDCPNIFTY", timeframe="5min", regime="RANGING",
                                     peers_aligned=0), _s())
    assert other_tf.allowed


def test_missing_peer_data_fails_the_required_rule():
    v = gate.evaluate(_inputs(instrument="MIDCPNIFTY", timeframe="15min", regime="CHOPPY",
                              peers_aligned=None, peers_checked=0), _s())
    assert not v.allowed


def test_opposing_pcr_vetoes_in_volatile_regime_only_when_enforced():
    v = gate.evaluate(_inputs(regime="VOLATILE", pcr={"available": True, "signal": "BEARISH"}),
                      _s(ctx_gate_mode="enforce"))
    assert not v.allowed and any("PCR" in r for r in v.reasons)


def test_market_measures_use_only_closed_bars():
    idx = pd.date_range("2026-10-07 09:15", periods=40, freq="5min")
    frames = {}
    for i, name in enumerate(gate.INDICES):
        close = pd.Series(range(100 + i, 140 + i), index=idx, dtype=float)
        frames[name] = pd.DataFrame({"open": close - 0.5, "high": close + 1, "low": close - 1, "close": close})
    provider = lambda inst, tf: frames[inst]
    m = gate.market_measures(provider, "MIDCPNIFTY", "5min", idx[30], "LONG")
    assert m["peers_checked"] == 3 and m["peers_aligned"] == 3
    assert m["atr_pct"] is not None and m["atr_pct"] > 0


def _s1_settings(**kw):
    base = dict(s1_lane_min_entry_rr=0.5, s1_lane_base_rr=1.0, s1_lane_weak_rr=0.7,
                s1_lane_rr_floor_at_target=True, s1_lane_breakeven_enabled=True,
                s1_lane_breakeven_arm_r=0.25, s1_lane_breakeven_cost_rs=130.0,
                s1_lane_lock_arm_r=0.5, s1_lane_giveback_neutral=10.0, s1_lane_giveback_with_trend=25.0,
                s1_lane_giveback_against=0.0, s1_lane_giveback_unknown=10.0, s1_lane_t1_lock_r=0.7,
                s1_t1_r=2.0, s1_sl_mode="fixed", s1_sl1_pct=0.10, s1_lane_guard_pct_1min=5.0,
                s1_lane_guard_pct_5min=10.0, s1_lane_guard_pct_15min=15.0, s1_lane_guard_strong_score=80.0,
                s1_lane_guard_late_after="14:30", s1_lane_runner_max_r=30.0, s1_lane_enabled=True,
                uts_lane_enabled=False, s1_lane_weak_rel_volume=1.2, s1_lane_weak_adx=20.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_s1_entry_rr_floor_is_the_base_target():
    trade = SimpleNamespace(source_lane="S1", direction="LONG")
    assert abs(s1_lane.lane_min_entry_rr(trade, _s1_settings()) - 0.98) < 1e-9
    assert s1_lane.lane_min_entry_rr(trade, _s1_settings(s1_lane_rr_floor_at_target=False)) == 0.5


def test_s1_breakeven_raises_premium_stop_after_quarter_r():
    s = _s1_settings()
    trade = SimpleNamespace(direction="LONG", inst_type="OPT", entry_price=100.0, initial_stop=90.0,
                            timeframe="5min", source_lane="S1", exit_policy_owner="S1",
                            underlying_entry=0.0, underlying_stop=0.0, s1_peak_premium=100.0,
                            lots=1, lot_size=65)
    early = s1_lane.exit_decision(trade, 0.0, 101.0, now=datetime(2026, 10, 7, 10, 0), settings=s)
    assert early.premium_stop < 100.0  # +0.1R: no breakeven yet
    armed = s1_lane.exit_decision(trade, 0.0, 103.0, now=datetime(2026, 10, 7, 10, 1), settings=s)
    assert armed.premium_stop >= 100.0 + 130.0 / 65 - 1e-9


def test_v10_shadow_records_without_changing_the_v9_decision():
    settings = SimpleNamespace(quality_policy_multi_source_mode="shadow")
    inputs = QualityInputs(timeframe="5min", inst_type="OPT", confidence=0.72, regime="TRENDING_UP",
                           adx_value=30.0, trend_15m_agrees=True, source_count=2)
    verdict = evaluate_quality(inputs, settings)
    legacy = evaluate_quality(inputs, SimpleNamespace(quality_policy_multi_source_mode="legacy"))
    assert verdict.allowed == legacy.allowed
    assert "v10_shadow_allowed" in verdict.checks and "v10_shadow_allowed" not in legacy.checks


def test_equal_risk_lots_scale_low_risk_index_and_keep_one_lot_when_over_budget():
    s = SimpleNamespace(risk_based_lots_enabled=True, risk_per_trade_rs=2000.0, risk_based_lots_max=3,
                        risk_based_lots_over_budget_action="allow_one", risk_based_lots_over_budget_tolerance=0.5,
                        auto_risk_sizing_enabled=False, market_lot_size_enabled=False)
    nifty = resolve_trade_lots(settings=s, inst_type="OPT", requested_lots=1, lot_size=65,
                               entry_price=150.0, stop_price=135.0)  # Rs.975 a lot
    assert nifty.lots == 2 and nifty.mode == "equal_risk"
    bank = resolve_trade_lots(settings=s, inst_type="OPT", requested_lots=1, lot_size=30,
                              entry_price=1000.0, stop_price=900.0)  # Rs.3000 a lot
    assert bank.lots == 1
    s.risk_based_lots_over_budget_action = "refuse"
    assert resolve_trade_lots(settings=s, inst_type="OPT", requested_lots=1, lot_size=30,
                              entry_price=1000.0, stop_price=850.0).lots == 0


def test_replay_window_moves_entry_and_next_signal_to_candle_close():
    start, end = SignalProcessor._closed_candle_replay_window(
        datetime(2026, 10, 7, 10, 0), "5min", datetime(2026, 10, 7, 10, 30), datetime(2026, 10, 7, 15, 25), False)
    assert start == datetime(2026, 10, 7, 10, 5) and end == datetime(2026, 10, 7, 10, 35)


def _bare_scanner():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc._option_premium_quote_plausible = lambda row, price: True
    return sc


def test_session_row_stop_touch_prices_the_first_tick_through_the_stop():
    sc = _bare_scanner()
    accepted = pd.Timestamp("2026-10-07 10:00:00").tz_localize("Asia/Kolkata").timestamp()
    ticks = [(1, 101.0, accepted + 5, 0), (2, 95.5, accepted + 9, 0), (3, 93.0, accepted + 12, 0)]
    calls = []

    def reader(token, after_sequence=0, not_before=0.0):
        calls.append((after_sequence, not_before))
        return [t for t in ticks if t[0] > after_sequence and t[2] >= not_before], 3, False

    sc.data = SimpleNamespace(get_contract_ticks_since=reader)
    row = SimpleNamespace(inst_type="OPT", stop=96.0, symbol_token="123", instrument="NIFTY",
                          entry_timestamp="2026-10-07T10:00:00")
    touch = sc._session_row_stop_touch(row)
    assert touch is not None and touch[0] == 95.5
    assert calls[0][1] == accepted and row._stop_tick_cursor == 3


def test_session_row_stop_touch_needs_an_acceptance_time():
    sc = _bare_scanner()
    sc.data = SimpleNamespace(get_contract_ticks_since=lambda *a, **k: ([(1, 1.0, 0.0, 0)], 1, False))
    row = SimpleNamespace(inst_type="OPT", stop=96.0, symbol_token="123", instrument="NIFTY")
    assert sc._session_row_stop_touch(row) is None


def test_tracker_exit_pnl_uses_executable_ask_and_bid():
    sc = _bare_scanner()
    sc._is_premium_long_candidate = lambda c: True
    row = SimpleNamespace(action="ENTRY", inst_type="OPT", direction="LONG", instrument="NIFTY 22400 CE",
                          price=100.0, execution_ask_price=101.0, lots=1, lot_size=65, multiplier=1.0)
    mark = sc._candidate_net_pnl(row, 110.0)
    executable = sc._candidate_exit_net_pnl(row, 110.0)
    assert executable < mark
    assert abs(executable - ((110.0 * (1 - 0.00122) - 101.0) * 65 - 80.0)) < 0.05


def test_recent_uts_repaint_exit_is_found_for_the_same_signal_side():
    from datetime import timedelta

    sc = _bare_scanner()
    now = datetime(2026, 9, 28, 9, 21, 50)
    closed = SimpleNamespace(exit_reason="UTS_REPAINT", instrument="NIFTY 23000 CE", timeframe="5min",
                             direction="LONG", exit_timestamp=(now - timedelta(seconds=70)).isoformat())
    sc.trades = SimpleNamespace(closed_trades=[closed])
    cand = SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG")
    assert sc._recent_uts_repaint_exit(cand, now) == now - timedelta(seconds=70)
    assert sc._recent_uts_repaint_exit(SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="SHORT"), now) is None


def test_midcap_peer_rule_skips_clear_trend_even_inside_the_recent_range():
    trend = gate.evaluate(_inputs(instrument="MIDCPNIFTY", timeframe="15min", regime="TRENDING_UP",
                                  adx=24.0, range_bound=True, peers_aligned=0), _s())
    assert trend.allowed and not trend.checks.get("peer_rule_applies")
    unknown_in_range = gate.evaluate(_inputs(instrument="MIDCPNIFTY", timeframe="15min", regime="UNKNOWN",
                                             adx=24.0, range_bound=True, peers_aligned=0), _s())
    assert not unknown_in_range.allowed
    weak_trend = gate.evaluate(_inputs(instrument="MIDCPNIFTY", timeframe="15min", regime="TRENDING_UP",
                                       adx=15.0, range_bound=False, peers_aligned=1), _s())
    assert not weak_trend.allowed
