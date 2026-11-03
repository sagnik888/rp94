"""Exercise the actual opposite-exit writers and frozen exit accounting."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from engine.signal_processor import TradeCandidate
from scanner import Scanner


def host(*, broker=False, shock=False, owner="QP", timeframe="15min"):
    sc = Scanner.__new__(Scanner)
    entry = TradeCandidate(
        instrument="NIFTY", direction="LONG", price=100, stop=90, target=125,
        lots=1, lot_size=65, grade="A", confidence=.8, timeframe=timeframe,
        inst_type="OPT", option_type="CE", atm_strike=22500, multiplier=.5,
        trading_symbol="NIFTY26O1322500CE", symbol_token="1", rr=2.5, score=.8,
        signal_timestamp=datetime(2026, 10, 9, 12, 45), current_price=98,
        source_lane=owner, source_engine="s1" if owner == "S1" else "utbot",
    )
    entry.entry_timestamp = datetime(2026, 10, 9, 13, 0, 12)
    sc.mode = "REAL"
    sc.config = {"indices": {"NIFTY": {}}}
    sc.trades = SimpleNamespace(open_trades={"position": entry} if broker else {})
    sc._latest_trade_candidates = {} if broker else {"NIFTY": [entry]}
    sc._latest_exit_candidates = {}
    sc._last_opposite_exit_signal_time = {}
    sc._session_trade_candidates = {}
    sc._session_squareoff_clock = lambda: datetime(2026, 10, 9, 13, 1, 55)
    sc._market_ltp_shock_force_exit_opposite = lambda *a: shock
    sc._opposite_exit_confirm_ready = lambda *a: True
    sc._active_session_entry_candidates = lambda *a, **k: [] if broker else [entry]
    recorded = []
    sc._get_session_trade_candidates = lambda *a: recorded
    sc._get_live_premium_cached = lambda *a: 98
    sc._candidate_exit_resolution = lambda row, px, reason, text: (px, reason, text)
    sc._refresh_trade_candidates = lambda *a: None
    sc._remember_trade_candidates = lambda rows: recorded.extend(rows)
    sc._close_and_record = lambda *a, **k: recorded.append((a, k))
    sc._notify = sc.log_event = lambda *a, **k: None
    return sc, entry, recorded


@pytest.mark.parametrize("broker", [False, True])
@pytest.mark.parametrize("entry_tf,signal_tf", [("15min", "1min"), ("5min", "1min"), ("15min", "5min")])
def test_raw_lower_frame_flip_does_not_own_longer_frame_trade(monkeypatch, broker, entry_tf, signal_tf):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    sc, _, recorded = host(broker=broker, timeframe=entry_tf)
    signal = SimpleNamespace(signal_type="SELL", timestamp=datetime(2026, 10, 9, 13),
                             price=22500, source_lane="QP", source_engine="utbot")
    assert not sc._handle_opposite_signal_exit("NIFTY", signal, signal_tf)
    assert recorded == []


@pytest.mark.parametrize("broker", [False, True])
@pytest.mark.parametrize("shock,signal_tf", [(False, "15min"), (True, "1min")])
def test_own_frame_and_emergency_shock_exits_survive(monkeypatch, broker, shock, signal_tf):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    sc, _, recorded = host(broker=broker, shock=shock)
    signal = SimpleNamespace(signal_type="SELL", timestamp=datetime(2026, 10, 9, 13),
                             price=22500, source_lane="QP", source_engine="utbot")
    assert sc._handle_opposite_signal_exit("NIFTY", signal, signal_tf)
    assert len(recorded) == 1


@pytest.mark.parametrize("broker", [False, True])
def test_qp_emergency_never_takes_s1_ownership(monkeypatch, broker):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    sc, _, recorded = host(broker=broker, shock=True, owner="S1")
    signal = SimpleNamespace(signal_type="SELL", timestamp=datetime(2026, 10, 9, 13),
                             price=22500, source_lane="QP", source_engine="utbot")
    assert not sc._handle_opposite_signal_exit("NIFTY", signal, "1min")
    assert recorded == []


def test_exit_freezes_final_cost_and_preserves_gross_peak(monkeypatch):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    sc, entry, recorded = host()
    entry.round_trip_cost = 80.0
    entry.peak_pnl = 170.0
    entry.peak_pnl_basis = "NET_AFTER_ROUND_TRIP_COST"
    entry.max_drawdown = -300.0
    entry.max_drawdown_basis = "NET_AFTER_ROUND_TRIP_COST"
    entry.pnl_path_basis = "GROSS_BEFORE_ROUND_TRIP_COST"
    entry.execution_ask_price = 100.2
    signal = SimpleNamespace(signal_type="SELL", timestamp=datetime(2026, 10, 9, 13),
                             price=22500, source_lane="QP", source_engine="utbot")
    assert sc._handle_opposite_signal_exit("NIFTY", signal, "15min")
    closed = recorded[0]
    gross = (98 - 100) * 65
    assert closed.round_trip_cost == pytest.approx(gross - closed.pnl)
    assert closed.peak_pnl_gross == pytest.approx(250)
    assert closed.exit_decision_record["accounting"]["gross_pnl"] == pytest.approx(gross)
    assert entry.round_trip_cost == 80
    from intelligence.rtracker import RegimeTracker
    row = RegimeTracker.__new__(RegimeTracker)._event_from_candidate(
        closed, decision="EXIT", reason="test", source="session_candidate", extra=None)
    assert row["realized_gross_pnl"] == pytest.approx(gross)


@pytest.mark.parametrize("sampled_gross", [None, 20.])
def test_zero_clamped_peak_does_not_invent_gross_profit(sampled_gross):
    from copy import deepcopy
    from intelligence.rtracker import RegimeTracker
    sc, entry, _ = host()
    entry.round_trip_cost = 80.
    entry.peak_pnl = 0.
    entry.peak_pnl_basis = "NET_AFTER_ROUND_TRIP_COST"
    if sampled_gross is not None:
        entry.peak_pnl_gross = sampled_gross
    closed = deepcopy(entry)
    closed.pnl = -250.
    closed.exit_reason = "SL HIT"
    closed.exit_timestamp = datetime(2026, 10, 9, 13, 1)
    closed.action = "EXIT"
    sc._capture_exit_tracking(closed, entry)
    row = RegimeTracker.__new__(RegimeTracker)._event_from_candidate(
        closed, decision="EXIT", reason="test", source="session_candidate", extra=None)
    assert row["peak_pnl_gross"] == sampled_gross


def test_replay_uses_acceptance_clock_and_never_guesses_contract(tmp_path, monkeypatch):
    import json
    import pandas as pd
    from scripts import backtest_exit_lifecycle as replay

    path = tmp_path / "data_store/rtracker/events"
    path.mkdir(parents=True)
    event = dict(decision="ACCEPTED", instrument="NIFTY", direction="LONG", timeframe="15min",
                 signal_timestamp="2026-10-09T12:45:00", entry_timestamp="2026-10-09T13:00:12",
                 price=100, stop=90, target=125, regime={"adx": 30},
                 contract={"trading_symbol": "NIFTY26O1322500CE", "lots": 2, "lot_size": 65})
    (path / "2026-10-09.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    monkeypatch.setattr(replay, "ROOT", tmp_path)
    trade = replay.load_trades("2026-10-09")[0]
    assert trade["entry_ts"] == pd.Timestamp("2026-10-09T13:00:12")
    assert trade["quantity"] == 130
    other = pd.DataFrame({"timestamp": [pd.Timestamp("2026-10-09T13:00")], "close": [100.]})
    assert replay.match_contract(trade, {"NIFTY26O1322550CE_1min.csv": other}) is None
    event.pop("entry_timestamp")
    (path / "2026-10-09.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert replay.load_trades("2026-10-09") == []


@pytest.mark.parametrize("tz", [None, "Asia/Kolkata"])
def test_prior_ranges_use_ten_complete_days_and_see_corrections(tz):
    import pandas as pd
    from intelligence.session_context import SessionContextAnalyzer

    frames = []
    for day in range(1, 26):
        index = pd.date_range(f"2026-09-{day:02} 09:15", periods=75, freq="5min", tz=tz)
        # Recent incomplete sessions must not displace older complete ones.
        if day in (22, 24):
            index = index.delete(30)
        frames.append(pd.DataFrame({"high": 100 + day, "low": 100}, index=index))
    frame = pd.concat(frames)
    before_today = pd.Timestamp("2026-09-25").date()
    expected = [float(day) for day in range(1, 25) if day not in (22, 24)][-10:]
    actual = SessionContextAnalyzer._complete_prior_ranges(frame, before_today)
    assert actual.tolist() == expected
    frame.loc[frame.index.day == 23, "high"] = 150
    changed = SessionContextAnalyzer._complete_prior_ranges(frame, before_today)
    assert changed.iloc[-1] == 50
    assert changed.iloc[:-1].tolist() == expected[:-1]


def test_legacy_cross_timeframe_policy_remains_available_for_comparison(monkeypatch):
    monkeypatch.setattr(get_settings(), "scanner_engine", "quad_prime_s1")
    monkeypatch.setattr(get_settings(), "qp_cross_timeframe_opposite_exits", True)
    sc, _, recorded = host()
    signal = SimpleNamespace(signal_type="SELL", timestamp=datetime(2026, 10, 9, 13),
                             price=22500, source_lane="QP", source_engine="utbot")
    assert sc._handle_opposite_signal_exit("NIFTY", signal, "1min")
    assert len(recorded) == 1
