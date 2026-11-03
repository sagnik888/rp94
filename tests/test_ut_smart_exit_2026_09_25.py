"""UT SMART EXIT engine + lane (2026-09-25): the user's "UT Bot + Smart Peak Exit v6" Pine
script replaces ADAPTIVE DUAL in the engine selector, with its own ATR entries and exits.

- engine/ut_smart_engine.py: a line-by-line port (Pine-seeded RMA/EMA, trailing stop,
  crossover signals confirmed on the close, the smart peak exit state machine).
- engine/uts_lane.py: the lane. Own entries (no selection stack), own exits (the chart's
  reverse / smart / hard exit), a premium catastrophe guard, its own cutoffs and timeframes.
- s1_lane.is_lane(): the lane hooks S1 already had now serve both engines.
- reports/ut_smart_exit_study_20260925.py: the tuning study behind the settings defaults.
"""
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from engine import s1_lane, uts_lane
from engine import ut_smart_engine as uts
from tests.test_s1_lane_2026_09_23 import _monitor, _open_trade

ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides):
    s = Settings()
    for k, v in overrides.items():
        object.__setattr__(s, k, v)
    return s


def _frame(closes, start="2026-09-24 09:15", freq="15min", spread=4.0):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    index = pd.date_range(start, periods=len(closes), freq=freq)
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + spread,
                         "low": np.minimum(opens, closes) - spread, "close": closes,
                         "volume": 1000.0}, index=index)


# ---------------------------------------------------------------- Pine parity


def test_rma_and_ema_are_seeded_like_pine():
    x = np.array([np.nan, 1.0, 2.0, 3.0, 4.0, 5.0])
    rma = uts._rma(x, 3)
    assert np.isnan(rma[:3]).all() and rma[3] == pytest.approx(2.0)          # SMA of 1,2,3
    assert rma[4] == pytest.approx(2.0 + (4.0 - 2.0) / 3)
    ema = uts._ema(x, 3)
    assert ema[1] == pytest.approx(1.0) and ema[3] == pytest.approx(2.25)
    assert ema[4] == pytest.approx(0.5 * 4.0 + 0.5 * 2.25)
    assert np.allclose(uts._ema(np.array([5.0, 6.0, 7.0]), 1), [5.0, 6.0, 7.0])  # ta.ema(src, 1) == src


def test_trailing_stop_matches_the_existing_ut_bot_core():
    from engine.ut_bot_core import _nb_trailing_stop

    rng = np.random.default_rng(3)
    df = _frame(23000 + np.cumsum(rng.normal(0, 12, 400)))
    ind = uts.indicators(df, uts.UTSParams(key_value=2.0, atr_period=14))
    theirs = _nb_trailing_stop(ind["src"], np.nan_to_num(ind["atr"]) * 2.0)
    assert np.allclose(ind["trail"][100:], theirs[100:])


def test_signals_are_the_crossovers_of_the_trailing_stop():
    rng = np.random.default_rng(5)
    df = _frame(23000 + np.cumsum(rng.normal(0, 15, 500)))
    p = uts.UTSParams()
    ind = uts.indicators(df, p)
    out = uts.run(ind, p)
    src, ts = ind["src"], ind["trail"]
    for i in range(1, len(src)):
        if np.isnan(ts[i]) or np.isnan(ts[i - 1]):
            assert out["signal"][i] == 0
            continue
        buy = src[i] > ts[i] and src[i - 1] <= ts[i - 1]
        sell = src[i] < ts[i] and src[i - 1] >= ts[i - 1]
        assert out["signal"][i] == (1 if buy else -1 if sell else 0)


def test_heikin_ashi_source_is_ohlc4():
    df = _frame([100.0, 102.0, 101.0, 105.0])
    ind = uts.indicators(df, uts.UTSParams(use_heikin_ashi=True))
    assert np.allclose(ind["src"], (df.open + df.high + df.low + df.close).to_numpy() / 4.0)


def _state_machine(bars, **params):
    """Drive _run directly: bars = list of (open, high, low, close, rsi, hist, ema)."""
    arr = np.asarray(bars, dtype=float)
    n = len(arr)
    op, hi, lo, cl, rsi, hist, ema = (arr[:, k].copy() for k in range(7))
    atr = np.full(n, 10.0)
    ts = np.full(n, np.nan)
    ts[0] = cl[0] + 5.0      # price below the trail ...
    ts[1:] = cl[0] - 50.0    # ... then above it: a BUY on bar 1, no further flips
    p = uts.UTSParams(**params)
    return uts._run(cl.copy(), op, hi, lo, cl, atr, ts, rsi, hist, ema,
                    p.smart_exit, p.arm_atr, p.retrace_atr, p.hard_retrace_atr, p.min_bars,
                    p.confirmations, p.rsi_ob, p.rsi_os, p.rsi_lookback, p.wick_ratio)


def test_smart_exit_waits_for_the_arm_then_needs_the_votes():
    # entry 100 on bar 1; peak 125 (2.5 ATR); close 118 = 0.7 ATR off the peak.
    calm = (0, 0, 0, 0, 50.0, 1.0, 0.0)
    bars = [(100, 101, 99, 100) + calm[4:],
            (100, 101, 99, 100) + calm[4:],
            (100, 112, 100, 111) + calm[4:],
            (111, 125, 111, 124) + calm[4:],
            (124, 124, 117, 118, 50.0, 1.0, 0.0)]          # retrace, but no exhaustion votes
    sig, state, ex, runup, retrace = _state_machine(bars, arm_atr=2.5, retrace_atr=0.7, confirmations=2)
    assert list(sig[:2]) == [0, 1] and state[4] == 1 and ex[4] == 0
    assert runup[4] == pytest.approx(2.5) and retrace[4] == pytest.approx(0.7)
    # the same bar with MACD fading (hist down) and a close under the exit EMA: 2 votes
    bars[4] = (124, 124, 117, 118, 50.0, 0.5, 130.0)
    sig, state, ex, *_ = _state_machine(bars, arm_atr=2.5, retrace_atr=0.7, confirmations=2)
    assert ex[4] == uts.EXIT_SMART and state[4] == 0
    # not armed (run-up below arm_atr): no smart exit however strong the votes
    sig, state, ex, *_ = _state_machine(bars, arm_atr=3.0, retrace_atr=0.7, confirmations=2)
    assert ex[4] == 0 and state[4] == 1


def test_hard_exit_needs_no_votes_once_armed():
    calm = (50.0, 1.0, 0.0)
    bars = [(100, 101, 99, 100, *calm), (100, 101, 99, 100, *calm), (100, 112, 100, 111, *calm),
            (111, 125, 111, 124, *calm), (124, 124, 98, 99, *calm)]   # 2.6 ATR off the peak
    _, state, ex, *_ = _state_machine(bars, arm_atr=2.5, hard_retrace_atr=2.5, confirmations=4)
    assert ex[4] == uts.EXIT_HARD and state[4] == 0
    _, state, ex, *_ = _state_machine(bars, smart_exit=False, arm_atr=2.5, hard_retrace_atr=2.5)
    assert ex[4] == 0 and state[4] == 1                                   # switched off


def test_trades_close_on_the_opposite_signal_or_a_smart_exit():
    rng = np.random.default_rng(11)
    df = _frame(23000 + np.cumsum(rng.normal(0, 18, 600)))
    p = uts.UTSParams()
    ind = uts.indicators(df, p)
    out = uts.run(ind, p)
    rows = uts.trades(df.index, ind, out)
    assert len(rows) == int((out["signal"] != 0).sum())
    for a, b in zip(rows, rows[1:]):
        assert a["exit_bar"] is not None and a["exit_bar"] <= b["bar"]
        if a["exit_reason"] == "UT_REVERSE":
            assert a["exit_bar"] == b["bar"]
        else:
            assert a["exit_reason"] in {"SMART_EXIT", "SMART_HARD_EXIT"}
            assert out["exit"][a["exit_bar"]] != 0


# ---------------------------------------------------------------- the live engine


def _engine_result(df, monkeypatch=None):
    from engine.alt_scanners import AltSignalEngine

    return AltSignalEngine(signal_source="ut_smart").process(df, instrument="NIFTY", timeframe="15min",
                                                           as_of=df.index[-1] + pd.Timedelta(minutes=1))


def test_the_forming_candle_never_signals_and_arrays_follow_the_chart(monkeypatch):
    settings = Settings(_env_file=None)
    settings.set_signal_fire_mode('CANDLE_CLOSE')
    monkeypatch.setattr('config.settings.get_settings', lambda: settings)
    rng = np.random.default_rng(21)
    df = _frame(23000 + np.cumsum(rng.normal(0, 25, 300)), start="2026-08-03 09:15")
    df = df[(df.index.hour * 60 + df.index.minute >= 555) & (df.index.hour * 60 + df.index.minute < 915)]
    res = _engine_result(df)
    assert res.get("error") is None and res["source_engine"] == "ut_smart"
    assert len(res["positions"]) == len(df) and len(res["trailing_stop"]) == len(df)
    assert all(sig.bar_index < len(df) - 1 for sig in res["signals"])     # last candle is forming
    for sig in res["signals"]:
        assert sig.source_engine == "ut_smart" and sig.source_lane == "UTS"
        assert df.index[sig.bar_index] == pd.Timestamp(sig.timestamp)
        assert sig.stop_distance == pytest.approx(abs(sig.price - sig.uts_trail))
    # the closed-candle answer does not depend on the forming candle
    moved = df.copy()
    moved.iloc[-1, moved.columns.get_loc("close")] += 300.0
    again = _engine_result(moved)
    assert [(s.timestamp, s.signal_type) for s in again["signals"]] == [(s.timestamp, s.signal_type) for s in res["signals"]]


def test_the_engine_is_selectable_and_replaces_adaptive_dual_in_the_dashboard():
    from engine.alt_scanners import AltSignalEngine, build_scanner_engine
    import config.settings as settings_module

    base = settings_module.get_settings()
    old = base.scanner_engine
    try:
        object.__setattr__(base, "scanner_engine", "ut_smart")
        engine = build_scanner_engine({})
        assert isinstance(engine, AltSignalEngine) and engine.signal_source == "ut_smart"
    finally:
        object.__setattr__(base, "scanner_engine", old)
    html = (ROOT / "dashboard/static/index.html").read_text(encoding="utf-8")
    assert '<option value="ut_smart">UT SMART EXIT</option>' in html
    assert 'value="adaptive_dual"' not in html
    js = (ROOT / "dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    options = js[js.index("const SCANNER_ENGINE_OPTIONS"):js.index("];", js.index("const SCANNER_ENGINE_OPTIONS"))]
    assert "'ut_smart'" in options and "'adaptive_dual'" not in options


def test_engine_lab_includes_ut_smart_and_preserves_its_lane():
    from engine.engine_lab import ENGINE_LAB_MODES, EngineLab, build_shadow_engine

    assert "ut_smart" in ENGINE_LAB_MODES
    assert build_shadow_engine("ut_smart", {}).signal_source == "ut_smart"
    assert EngineLab._source_lane(SimpleNamespace(source_engine="ut_smart", source_lane="UTS"), "ut_smart") == "UTS"


def test_engine_lab_recomputes_ut_smart_when_its_settings_change(monkeypatch):
    from config.settings import get_settings
    from engine import engine_lab

    monkeypatch.setattr(engine_lab, "ENGINE_LAB_MODES", ("ut_smart",))
    calls = []

    class Engine:
        signal_mode = "realtime"

        def process(self, **kwargs):
            calls.append(kwargs)
            return {"signals": [], "state": {}}

    monkeypatch.setattr(engine_lab, "build_shadow_engine", lambda *args: Engine())
    frame = _frame(np.linspace(23000, 23100, 60))
    lab = engine_lab.EngineLab()
    data = {"NIFTY": {"15min": frame}}
    lab.process(candles_by_instrument=data)
    lab.process(candles_by_instrument=data)
    assert len(calls) == 1

    settings = get_settings()
    monkeypatch.setattr(settings, "uts_key_value", settings.uts_key_value + 0.1)
    lab.process(candles_by_instrument=data)
    assert len(calls) == 2


@pytest.mark.parametrize("intrabar", [False, True])
def test_mtf_recomputes_ut_smart_only_when_a_candle_closes(monkeypatch, intrabar):
    """CANDLE_CLOSE: forming-candle ticks do not recompute. INTRACANDLE (the general
    Intra-candle Signals toggle): they do, the forming candle can flip."""
    import config.settings as settings_module
    from engine.multi_timeframe import MultiTimeframeEngine

    base = settings_module.get_settings()
    monkeypatch.setattr(base, "live_intrabar_signal_entries", intrabar, raising=False)
    monkeypatch.setattr(base, "ut_signal_mode", "realtime" if intrabar else "confirmed", raising=False)
    monkeypatch.setattr(base, "uts_lane_allow_intrabar_entries", intrabar)

    class _UTS:
        signal_source = "ut_smart"
        signal_mode = "realtime"
        calls = 0

        def process(self, **_kw):
            _UTS.calls += 1
            return {"signals": [], "state": {"position": 0}}

    mtf = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
    mtf.engines = {"NIFTY_15min": _UTS()}
    mtf._data_cache, mtf._result_cache, mtf._data_hash = {}, {}, {}
    mtf._input_metadata, mtf._result_metadata, mtf._fresh_result_required_after = {}, {}, {}
    mtf.tf_weights = {"1min": 0.15, "5min": 0.40, "15min": 0.45}
    df = _frame(np.linspace(23000, 23100, 60))
    monkeypatch.setattr(uts, "evaluation_clock", lambda *args: df.index[-1] + pd.Timedelta(minutes=1))
    mtf.update_data("NIFTY", "15min", df)
    mtf.process_instrument("NIFTY")
    ticked = df.copy()
    ticked.iloc[-1, ticked.columns.get_loc("close")] += 7.0                  # forming candle moved
    mtf.update_data("NIFTY", "15min", ticked)
    mtf.process_instrument("NIFTY")
    assert _UTS.calls == (2 if intrabar else 1)
    mtf.update_data("NIFTY", "15min", _frame(np.linspace(23000, 23100, 61)))  # a candle closed
    mtf.process_instrument("NIFTY")
    assert _UTS.calls == (3 if intrabar else 2)


# ---------------------------------------------------------------- the lane


def test_lane_identity_covers_both_engines_and_restored_trades():
    s = _settings()
    uts_sig = SimpleNamespace(source_engine="ut_smart", source_lane="UTS")
    assert s1_lane.is_lane(uts_sig, s) and s1_lane.lane_tag(uts_sig, s) == "UTS"
    assert not s1_lane.is_s1(uts_sig, s)                                       # S1-only rules stay S1's
    assert s1_lane.lane_tag(SimpleNamespace(source_engine="", source_lane="UTS"), s) == "UTS"  # after restart
    assert s1_lane.lane_tag(SimpleNamespace(source_engine="s1"), s) == "S1"
    assert not s1_lane.is_lane(SimpleNamespace(source_engine="quad_prime", source_lane=""), s)
    assert not s1_lane.is_lane(uts_sig, _settings(uts_lane_enabled=False))
    from engine.signal_processor import SignalProcessor

    assert SignalProcessor._signal_source_lane(SimpleNamespace(source_engine="ut_smart", source_lane="")) == "UTS"


def test_lane_values_are_looked_up_per_engine():
    # s1_lane_rr_floor_at_target=False keeps this test on the per-engine lookup it checks;
    # the 2026-10-08 target floor is covered in test_payoff_fixes_2026_10_08.py.
    s = _settings(s1_sl_max_pct=0.25, uts_lane_sl_max_pct=1.0, s1_lane_min_entry_rr=0.5, uts_lane_min_entry_rr=0.8,
                  s1_lane_rr_floor_at_target=False)
    u, one = SimpleNamespace(source_engine="ut_smart"), SimpleNamespace(source_engine="s1")
    assert s1_lane.lane_sl_max_pct(u, s) == 1.0 and s1_lane.lane_sl_max_pct(one, s) == 0.25
    assert s1_lane.lane_min_entry_rr(u, s) == 0.8 and s1_lane.lane_min_entry_rr(one, s) == 0.5
    wait = s1_lane.lane_intrabar_wait_seconds(u, s)
    assert wait == 15.0, wait                            # intra-candle entry buffer (15s since 2026-10-09 user)


def test_cutoffs_and_timeframes():
    from engine.signal_processor import _entry_cutoff

    s = _settings()
    # 2026-09-26 user: every engine's last entry is the system-wide one (no lane cutoffs).
    assert _entry_cutoff(s, "15min", s1="UTS")[0] == "14:50"
    assert _entry_cutoff(s, "15min", s1="S1")[0] == "14:50" and _entry_cutoff(s, "15min", s1=True)[0] == "14:50"
    assert _entry_cutoff(_settings(ut_no_entry_after="14:30:00"), "15min", s1="UTS")[0] == "14:30"
    assert _entry_cutoff(s, "1min", s1="UTS")[0] == "15:14" and _entry_cutoff(s, "5min", s1="UTS")[0] == "15:10"
    # Timeframes: the dashboard TF toggle, as for every engine (2026-09-26 user); no lane list.
    assert not hasattr(uts_lane, "timeframe_allowed") and "uts_lane_timeframes" not in Settings.model_fields


def test_cutoff_fallbacks_match_the_declared_defaults():
    settings_without_fields = SimpleNamespace()
    assert uts_lane.entry_cutoff("15min", settings_without_fields) == "14:50"
    assert uts_lane.entry_cutoff("5min", settings_without_fields) == "15:10"
    assert uts_lane.entry_cutoff("1min", settings_without_fields) == "15:14"
    s = Settings()
    assert (s.ut_no_entry_after, s.ut_1min_no_entry_after) == ("14:50", "15:14")


def test_historical_uts_cutoff_uses_the_signal_bar_start_inclusively():
    from engine.signal_processor import SignalProcessor

    original = SimpleNamespace(
        timestamp=datetime(2026, 9, 24, 12, 45),
        source_engine="ut_smart",
        source_lane="UTS",
    )
    replay = SignalProcessor._historical_signal_for_replay(original, "15min")
    assert replay.timestamp == datetime(2026, 9, 24, 13, 0)
    assert replay.source_signal_timestamp == original.timestamp
    assert SignalProcessor._historical_entry_cutoff_allows(replay, "15min", "UTS", _settings())

    def at(hh, mm, tf="15min"):
        sig = SimpleNamespace(timestamp=datetime(2026, 9, 24, hh, mm), source_engine="ut_smart", source_lane="UTS")
        return SignalProcessor._historical_entry_cutoff_allows(
            SignalProcessor._historical_signal_for_replay(sig, tf), tf, "UTS", _settings())

    # replay enters at the bar close, which must land by the last entry (2026-09-26 user)
    assert at(13, 0) and at(14, 30)                   # were refused by the old 12:45 cutoff
    assert not at(14, 45)                             # closes 15:00 > 14:50
    assert at(15, 5, "5min") and not at(15, 10, "5min")
    assert at(15, 13, "1min") and not at(15, 14, "1min")


def test_premium_guard_never_exceeds_shared_option_risk_policy():
    s = _settings()
    # Raw 2R UT guard would be 46.7%; overall policy wins even for the UTS lane.
    assert uts_lane.premium_guard_pct(150.0, 70.0, 0.5, s, regime="TRENDING_DOWN") == 10.0
    assert uts_lane.premium_guard_pct(150.0, 70.0, 0.5, s, regime="RANGING", timeframe="15min") == 15.0
    assert uts_lane.premium_guard_pct(150.0, 70.0, 0.5, s, regime="VOLATILE", grade="C", timeframe="15min") == 15.0
    assert uts_lane.premium_guard_pct(400.0, 20.0, 0.5, s, regime="RANGING", timeframe="5min") == 10.0
    assert uts_lane.premium_stop_distance(150.0, 70.0, 0.5, s, regime="RANGING", timeframe="15min") == pytest.approx(22.5)


def test_historical_uts_guard_uses_the_same_minimum_floor_as_live():
    from engine.signal_processor import SignalProcessor

    s = _settings(uts_lane_guard_mult=2.0, uts_lane_guard_min_pct=10.0, uts_lane_guard_max_pct=50.0)
    # 10 index points x 0.5 delta x 2 = 10 premium points, only 5% of a Rs.200 option.
    # Both paths must apply the configured 10% floor and therefore risk Rs.20.
    assert SignalProcessor._historical_uts_premium_stop_distance(200.0, 10.0, 0.5, s) == 20.0


def test_option_conversion_keeps_the_uts_guard_beyond_the_regime_cap():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc._entry_policy_underlying_stop_cap_pct = lambda instrument, regime: 0.15
    kw = dict(instrument="NIFTY", regime="TRENDING_DOWN", spot=23000.0, premium=150.0, stop=80.0,
              target=360.0, signed_delta=-0.5, rr_hint=3.0, min_rr=0.5)
    capped = sc._cap_option_geometry_to_underlying_policy(**kw)
    assert capped[0] == pytest.approx(150.0 - 23000.0 * 0.0015 * 0.5)       # squeezed to 17.25 pts
    uts_cap = Scanner._lane_option_cap_override(SimpleNamespace(source_engine="ut_smart", source_lane="UTS"))
    assert uts_cap == 1.0
    kept = sc._cap_option_geometry_to_underlying_policy(**kw, cap_pct_override=uts_cap)
    assert kept[0] == pytest.approx(80.0)                                   # the 2R guard survives
    assert Scanner._lane_option_cap_override(SimpleNamespace(source_engine="s1", source_lane="S1")) is None
    import inspect

    src = inspect.getsource(Scanner._force_candidate_to_options)
    assert "cap_pct_override=Scanner._lane_option_cap_override(candidate)" in src


def _uts_trade(**kw):
    base = dict(source_engine="ut_smart", source_lane="UTS", entry_price=150.0, current_stop=80.0,
                initial_stop=80.0, underlying_stop=23330.0, timeframe="15min")
    base.update(kw)
    return _open_trade(**base)


def _uts_signal(reason, exit_at):
    return SimpleNamespace(source_engine="ut_smart", timestamp=datetime(2026, 9, 24, 10, 0), signal_type="BUY",
                           uts_exit_reason=reason, uts_exit_time=exit_at, uts_exit_price=0.0)


def test_exit_decision_follows_only_the_chart():
    trade = _uts_trade()
    held = s1_lane.exit_decision(trade, 23250.0, 70.0, engine_exit=None, trend=-1)
    assert not held.exit and held.runner and held.index_stop == 0.0 and held.premium_stop == 0.0
    out = s1_lane.exit_decision(trade, 23450.0, 190.0, engine_exit={"reason": "SMART_EXIT"})
    assert out.exit and out.reason == "UTS_SMART_EXIT"
    assert s1_lane.exit_decision(trade, 0, 0, engine_exit={"reason": "UT_REVERSE"}).reason == "UTS_REVERSE"


def test_engine_exit_is_read_once_its_candle_has_closed():
    trade = _uts_trade(signal_time=datetime(2026, 9, 24, 10, 0))
    result = {"signals": [_uts_signal("SMART_EXIT", datetime(2026, 9, 24, 11, 0))]}
    assert s1_lane.engine_exit_for(trade, result, now=datetime(2026, 9, 24, 11, 10)) is None     # bar still forming
    got = s1_lane.engine_exit_for(trade, result, now=datetime(2026, 9, 24, 11, 15))
    assert got["reason"] == "SMART_EXIT"
    # an S1 signal at the same time is not this trade's exit
    s1_sig = SimpleNamespace(source_engine="s1", timestamp=datetime(2026, 9, 24, 10, 0), signal_type="BUY",
                             s1_exit_reason="S1 exit", s1_exit_time=datetime(2026, 9, 24, 10, 15), s1_exit_price=0.0)
    assert s1_lane.engine_exit_for(trade, {"signals": [s1_sig]}, now=datetime(2026, 9, 24, 12, 0)) is None


def test_monitor_holds_a_uts_trade_through_other_engines_guards(monkeypatch):
    # index 70 points below entry and the premium down 20%: the chart has not exited, the
    # 2R catastrophe guard (stop 80) is not hit, no other engine's guard runs.
    closed, updated, other = _monitor(monkeypatch, _uts_trade(), spot=23330.0, premium=120.0)
    assert closed == [] and other == []
    assert updated and updated[0][2] == pytest.approx(80.0)                   # the guard, not a trail


def test_monitor_closes_a_uts_trade_on_the_chart_exit(monkeypatch):
    # signal 10:00, chart exit on the 10:15 5m candle (closed 10:20; harness clock 10:30)
    sig = _uts_signal("UT_REVERSE", datetime(2026, 9, 24, 10, 15))
    # (the harness caches engine results under NIFTY_5min)
    closed, _, other = _monitor(monkeypatch, _uts_trade(timeframe="5min"), spot=23410.0, premium=155.0,
                                engine_signals=[sig])
    assert closed == ["UTS_REVERSE"] and other == []


def test_uts_obeys_the_consecutive_loss_suspension_but_not_the_cooldown():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc._reset_daily_counters = lambda: None
    sc._trades_today = {"NIFTY": 1}
    sc._losses_today = {"NIFTY": 3}
    sc.max_trades_per_index = 5
    sc.max_consecutive_losses = 3
    sc._clear_loss_streak_lanes = lambda inst: None
    now = datetime(2026, 9, 24, 11, 0)
    ok, why = sc._can_trade_instrument("NIFTY", now, direction="SHORT", s1_lane="UTS")
    assert not ok and "consecutive losses" in why and sc._losses_today["NIFTY"] == 0
    sc._losses_today["NIFTY"] = 3
    # The loss suspension is shared across native lanes; a lane marker skips only
    # the generic cooldown logic, not the account-level consecutive-loss breaker.
    ok, why = sc._can_trade_instrument("NIFTY", now, direction="SHORT", s1_lane=True)
    assert not ok and "consecutive losses" in why and sc._losses_today["NIFTY"] == 0
    sc._losses_today["NIFTY"] = 1
    sc._last_exit_time = {"NIFTY": now}                       # just exited: no cooldown for the lane
    assert sc._can_trade_instrument("NIFTY", now, direction="SHORT", s1_lane="UTS") == (True, "")


def test_uts_signals_are_not_kept_for_missed_signal_reentry():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"

    def reached(_candidate):
        raise AssertionError("a UT SMART EXIT seed went on into the re-entry pool")

    sc._candidate_signal_type = reached
    cand = SimpleNamespace(source_engine="ut_smart", source_lane="UTS", instrument="NIFTY", timeframe="15min",
                           confidence=0.9)
    sc._remember_missed_reentry_seed(cand, reason="refused")          # returns before the pool
    with pytest.raises(AssertionError):                                # an ordinary signal goes on
        sc._remember_missed_reentry_seed(SimpleNamespace(source_engine="utbot", source_lane="", instrument="NIFTY",
                                                         timeframe="15min", confidence=0.9), reason="refused")


# ---------------------------------------------------------------- settings and dashboard


def test_every_uts_setting_reaches_the_engine_or_the_lane_and_is_dashboard_editable():
    fields = {k for k in Settings.model_fields if k.startswith("uts_")}
    assert set(uts_lane.UTS_SETTING_SPEC) == fields
    engine_fields = set(uts.SETTING_FIELDS) | {k for tf in uts.PER_TIMEFRAME_FIELDS.values() for k in tf}
    assert all(k in engine_fields or k.startswith("uts_lane_") for k in fields)
    js = (ROOT / "dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    block = js[js.index("const UTS_SETTINGS_FIELDS"):js.index("];", js.index("const UTS_SETTINGS_FIELDS"))]
    assert set(re.findall(r"\['(uts_[a-z0-9_]+)'", block)) == fields


def test_engine_params_come_from_settings():
    p = uts.params_from_settings(_settings(uts_key_value=2.5, uts_atr_period=21, uts_use_heikin_ashi=False,
                                           uts_confirmations=9))
    assert (p.key_value, p.atr_period, p.use_heikin_ashi, p.confirmations) == (2.5, 21, False, 4)


def test_server_validates_and_forwards_uts_settings():
    import dashboard.server as srv

    ok = {"cmd": "configure", "uts_key_value": 2.5, "ut_1min_no_entry_after": "15:14", "uts_use_heikin_ashi": False,
          "uts_lane_reclaim_start": "13:00",
          "uts_lane_repaint_check_seconds": 10}
    assert srv._valid_command_payload(dict(ok)) is None
    for policy in ("PRIMARY_1", "INCLUDE_1MIN_15MIN", "INCLUDE_1MIN_5MIN_15MIN"):
        assert srv._valid_command_payload({"cmd": "configure", "timeframe_entry_policy": policy}) is None
    assert srv._valid_command_payload({"cmd": "configure", "uts_confirmations": 5}) == "out_of_range:uts_confirmations"
    server_src = (ROOT / "dashboard/server.py").read_text(encoding="utf-8")
    assert "**{name: msg.get(name) for name in _UTS_SETTING_SPEC}" in server_src
    values = uts_lane.dashboard_values(_settings(uts_key_value=2.5))
    assert values["uts_key_value"] == 2.5 and set(values) == set(uts_lane.UTS_SETTING_SPEC)
