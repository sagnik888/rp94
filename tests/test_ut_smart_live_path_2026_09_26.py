"""UT SMART EXIT through the REAL live path (audit 2026-09-26).

The unit tests of 2026-09-25 covered the engine and the lane helpers. This drives the
production seams with a UT SMART EXIT signal from the real engine: the candidate builder
(SignalProcessor.process_best_signal, REAL mode), the live admission gate, the final entry
policy's lane geometry, and the reversal: an open UT SMART trade meeting the opposite flip.

Found by this audit:
- the lane's timeframe list was enforced only at the live gate, so the historical replay
  traded 5m signals on a 15m-only lane (seven 5m replay trades on 2026-09-25, all losers);
- the reversal flip waited in the generic 30-60s repaint confirm, so the old trade was
  still open when the reversal entry was built and the entry vanished (no candidate, no
  rejection row) -- a closed-candle UT flip cannot repaint.
"""
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import engine.signal_processor as sp
import scanner as scanner_module
from config.settings import Settings
from engine.alt_scanners import AltSignalEngine
from engine import uts_lane
from engine.signal_manager import SignalManager
from engine.signal_processor import SignalProcessor
from scanner import Scanner
from trading.trade_manager import IST

INDEX, LOT, STRIKE = "NIFTY", 65, 50


def _session_frame(days=12, freq="15min", seed=4):
    rng = np.random.default_rng(seed)
    step = int(freq.replace("min", ""))
    stamps = []
    for d in pd.bdate_range("2026-09-01", periods=days):
        stamps.extend(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), d + pd.Timedelta(hours=15, minutes=15),
                                    freq=freq, inclusive="left"))
    closes = 23000 + np.cumsum(rng.normal(0, 18.0 * (step / 15) ** 0.5, len(stamps)))
    opens = np.r_[closes[0], closes[:-1]]
    spread = np.abs(rng.normal(0, 8, len(stamps))) + 4
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + spread,
                         "low": np.minimum(opens, closes) - spread, "close": closes, "volume": 1000.0},
                        index=pd.DatetimeIndex(stamps))


def _pick(frame, tf):
    res = AltSignalEngine(signal_source="ut_smart").process(frame, instrument=INDEX, timeframe=tf)
    signals = [s for s in res["signals"] if (s.timestamp.hour, s.timestamp.minute) < (12, 30)]
    for sig in reversed(signals):
        earlier = [s for s in res["signals"] if s.timestamp < sig.timestamp]
        if earlier:
            return sig
    pytest.skip("synthetic series produced no usable flip")


class _Data:
    def get_ltp(self, exchange, symbol, token=None, *a, **k):
        return 12.0 if "VIX" in str(symbol).upper() else 150.0

    def get_option_token(self, instrument, strike, option_type, *a, **k):
        sym = f"{instrument}26SEP{int(strike)}{option_type}"
        return {"symbol": sym, "token": "123456", "tradingsymbol": sym, "instrument_token": "123456",
                "expiry": "2026-09-29", "lot_size": LOT, "strike": strike}


@pytest.fixture
def live(monkeypatch):
    """A REAL-mode scanner shell (real methods, stub broker data) at the flip's bar close."""

    def build(tf="15min", settings_overrides=None):
        full = _session_frame(freq=tf)
        sig0 = _pick(full, tf)
        cut = full.index.get_loc(pd.Timestamp(sig0.timestamp)) + 2      # signal bar closed + 1 forming
        frame = full.iloc[:cut]
        res = AltSignalEngine(signal_source="ut_smart").process(frame, instrument=INDEX, timeframe=tf)
        sig = res["signals"][-1]
        tf_min = int(tf.replace("min", ""))
        now = (sig.timestamp + timedelta(minutes=tf_min, seconds=20)).replace(tzinfo=None)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return IST.localize(now) if tz else now

        settings = Settings()
        object.__setattr__(settings, "scanner_engine", "ut_smart")
        for key, value in (settings_overrides or {}).items():
            object.__setattr__(settings, key, value)
        for mod in (sp, scanner_module):
            monkeypatch.setattr(mod, "datetime", Clock)
            monkeypatch.setattr(mod, "get_settings", lambda: settings)
        monkeypatch.setattr(sp, "_scanner_is_expiry_day", lambda *a: False)
        monkeypatch.setattr(sp, "build_identity", lambda *a: {})
        spot = float(frame["close"].iloc[-1])
        logs = []
        sc = Scanner.__new__(Scanner)
        sc.mode, sc.inst_pref, sc.options_sl_pct, sc.futures_sl_pct = "REAL", "OPT", 15.0, 0.15
        sc.data = _Data()
        sc.candles = SimpleNamespace(get_candles=lambda i, t: frame if t == tf else None,
                                     get_latest_price=lambda i: spot, get_max_timestamp=lambda: now)
        sc.signals = SignalManager()
        sc.risk_manager = SimpleNamespace(can_trade=lambda *a: True)
        sc.trades = SimpleNamespace(open_trades={}, closed_trades=[], can_open_trade=lambda: (True, ""))
        sc._last_signal_time = {f"{INDEX}_{tf}": sig.timestamp - timedelta(seconds=1)}
        for name in ("_vix_data", "_chain_cache", "_premium_cache", "_pending_opposite_exit",
                     "_last_opposite_exit_signal_time", "_latest_trade_candidates", "_session_trade_candidates"):
            setattr(sc, name, {})
        sc.log_event = lambda *a, **k: logs.append(str(a[0]))
        sc.log_signal_decision_once = lambda *a, **k: logs.append(str(a[1]))
        sc.config = {"indices": {INDEX: {"strike_interval": STRIKE, "lot_size": LOT}}}
        sc.user_lots, sc.user_lots_fut, sc.auto_mode, sc.active_indices = {INDEX: 1}, {INDEX: 1}, True, [INDEX]
        processor = SignalProcessor.__new__(SignalProcessor)
        processor.scanner = sc
        sc.signal_processor = processor
        processor._build_timeframe_contexts = lambda *a: None
        mtf = SimpleNamespace(results_1min=res if tf == "1min" else None, results_5min=res if tf == "5min" else None,
                              results_15min=res if tf == "15min" else None, confluence_score=0.5)
        intel = {"regime": {"regime": "TRENDING_UP"}, "greeks": {}, "aggregate": {}}
        args = (INDEX, mtf, intel, 0.3, "TRENDING_UP", 1, LOT, sc.config["indices"][INDEX], spot,
                round(spot / STRIKE) * STRIKE)
        return SimpleNamespace(sc=sc, processor=processor, args=args, sig=sig, res=res, logs=logs, spot=spot)

    return build


def test_the_live_candidate_is_a_ut_smart_lane_entry_on_the_ut_stop(live):
    h = live()
    out = h.processor.process_best_signal(*h.args)
    assert len(out) == 1
    c = out[0]
    assert (c.status, c.action, c.inst_type) == ("TRADE SIGNAL", "ENTRY", "OPT")
    assert (c.source_engine, c.source_lane, c.s1_lane) == ("ut_smart", "UTS", True)
    assert c.direction == ("LONG" if h.sig.signal_type == "BUY" else "SHORT")
    # index stop = the chart's UT stop distance, from the entry spot
    assert abs(c.spot_stop - c.entry_spot) == pytest.approx(h.sig.stop_distance, rel=1e-6)
    # Premium stop uses the selected capital cap; target remains nominal 3R.
    assert c.s1_guard_pct == pytest.approx((c.price - c.stop) / c.price * 100.0)
    assert c.rr == 3.0 and c.target == pytest.approx(c.price + (c.price - c.stop) * 3.0)


def test_the_live_gate_applies_uts_quality_without_other_selection_gates(live, monkeypatch):
    h = live()
    cand = h.processor.process_best_signal(*h.args)[0]
    # monkeypatch, not assign + del: `del` removed the real Scanner method for every later test
    monkeypatch.setattr(Scanner, "_session_bias_standaside",
                        lambda *a: pytest.fail("selection stack reached for a lane signal"))
    assert h.sc._passes_live_trade_ready_gate(cand) is True
    assert cand.quality_verdict['allowed'] is True
    assert cand.quality_verdict['policy_version'] == 'uts_native.v1'
    monkeypatch.setattr(Scanner, "_quality_verdict_for_candidate", lambda *a, **kw: SimpleNamespace(allowed=True))
    assert h.sc._passes_live_trade_ready_gate(cand) is True


def test_the_final_policy_applies_the_lane_geometry(live):
    h = live()
    cand = h.processor.process_best_signal(*h.args)[0]
    verdict = h.sc._final_entry_policy_verdict(cand, phase="pre_submit", fresh_exposure=True)
    checks = dict(verdict.checks)
    assert verdict.min_rr == 0.5 and verdict.max_underlying_stop_pct == 1.0     # the lane's, not 1.5 / 0.15
    assert checks["execution_rr"] and checks["underlying_stop_cap"] and checks["option_cross_geometry"]


@pytest.mark.parametrize("policy,trades_5m", [
    ("PRIMARY_15", False), ("PRIMARY_1", False), ("INCLUDE_1MIN_15MIN", False),
    ("PRIMARY_5", True), ("INCLUDE_5MIN", True), ("INCLUDE_1MIN_5MIN", True), ("INCLUDE_1MIN_5MIN_15MIN", True),
])
def test_the_dashboard_tf_toggle_decides_the_ut_smart_frames(live, policy, trades_5m):
    """2026-09-26 user: UT SMART EXIT trades the frames the TF toggle selects (all seven
    1m/5m/15m combinations); the lane has no timeframe list of its own."""
    h = live(tf="5min", settings_overrides={"ut_timeframe_entry_policy": policy})
    out = h.processor.process_best_signal(*h.args)
    if trades_5m:
        assert out and out[0].timeframe == "5min" and out[0].source_lane == "UTS"
        assert h.sc._passes_timeframe_entry_policy(out[0]) is True
    else:
        assert out == []


@pytest.mark.parametrize("policy,trades_15m", [
    ("PRIMARY_15", True), ("INCLUDE_1MIN_15MIN", True), ("INCLUDE_5MIN", True), ("INCLUDE_1MIN_5MIN_15MIN", True),
    ("PRIMARY_1", False), ("PRIMARY_5", False), ("INCLUDE_1MIN_5MIN", False),
])
def test_15m_follows_the_toggle_too(live, policy, trades_15m):
    h = live(settings_overrides={"ut_timeframe_entry_policy": policy})
    out = h.processor.process_best_signal(*h.args)
    assert bool(out and out[0].timeframe == "15min") is trades_15m


def test_the_top_bar_offers_every_timeframe_combination():
    html = (Path(__file__).resolve().parents[1] / "dashboard/static/index.html").read_text(encoding="utf-8")
    block = html[html.index('id="topTimeframePolicy"'):html.index("</select>", html.index('id="topTimeframePolicy"'))]
    from config.settings import TIMEFRAME_ENTRY_POLICY_TIMEFRAMES

    offered = set(re.findall(r'value="([A-Z0-9_]+)"', block))
    assert offered == set(TIMEFRAME_ENTRY_POLICY_TIMEFRAMES)
    combos = {frozenset(v) for v in TIMEFRAME_ENTRY_POLICY_TIMEFRAMES.values()}
    assert len(combos) == 7                                     # every non-empty 1m/5m/15m set
    app_js = (Path(__file__).resolve().parents[1] / "dashboard/static/js/app.js").read_text(encoding="utf-8")
    for policy in TIMEFRAME_ENTRY_POLICY_TIMEFRAMES:
        assert f"{policy}: '" in app_js


def test_the_reversal_closes_the_old_trade_then_enters_the_new_side(live):
    h = live()
    prev = [s for s in h.res["signals"] if s.timestamp < h.sig.timestamp][-1]
    closed = []
    h.sc.trades.open_trades = {"T-prev": SimpleNamespace(
        id="T-prev", instrument=INDEX, timeframe="15min", direction="LONG" if prev.signal_type == "BUY" else "SHORT",
        inst_type="OPT", status="OPEN", entry_price=140.0, current_price=150.0, source_engine="ut_smart",
        source_lane="UTS", signal_time=prev.timestamp, trading_symbol="NIFTYPREV", lots=1, lot_size=LOT)}
    h.sc._get_live_premium_cached = lambda t, allow_blocking=False: 155.0

    def close_and_record(tid, px, reason, exit_time=None):
        closed.append(reason)
        h.sc.trades.open_trades.pop(tid, None)

    h.sc._close_and_record = close_and_record
    first = h.processor.process_best_signal(*h.args)
    assert closed == ["UTS_REVERSE"]                                   # no 30-60s repaint wait
    assert [c.status for c in first] == ["REJECTED - Opposite Signal Wait"]   # recheck-eligible
    second = h.processor.process_best_signal(*h.args)
    assert [(c.status, c.source_lane) for c in second] == [("TRADE SIGNAL", "UTS")]
    assert second[0].direction == ("LONG" if h.sig.signal_type == "BUY" else "SHORT")


def test_other_engines_still_wait_for_the_repaint_confirm(live):
    h = live()
    h.sc.trades.open_trades = {"T-x": SimpleNamespace(
        id="T-x", instrument=INDEX, timeframe="15min", direction="LONG" if h.sig.signal_type == "SELL" else "SHORT",
        inst_type="OPT", status="OPEN", source_engine="utbot", source_lane="")}
    other = SimpleNamespace(signal_type=h.sig.signal_type, timestamp=h.sig.timestamp, source_engine="utbot",
                            source_lane="", instrument=INDEX)
    assert h.sc._opposite_exit_confirm_ready(INDEX, other, "15min") is False  # pending, as before
    assert h.sc._opposite_exit_confirm_ready(INDEX, h.sig, "15min") is True


def test_the_live_engine_gets_enough_history_to_match_the_chart(monkeypatch):
    from engine.ut_smart_engine import FEED_BARS

    monkeypatch.setattr(scanner_module, "get_settings", lambda: SimpleNamespace(scanner_engine="ut_smart"))
    assert Scanner._signal_engine_warmup_bars() == FEED_BARS >= 100


def test_the_replay_applies_the_lane_cutoff_not_s1s():
    """Source pin for the HISTORICAL replay gate (the offline production replay proved the
    runtime effect: 8 of 32 UT SMART trades entered after 12:45 on S1's 14:50 cutoff)."""
    import inspect

    src = inspect.getsource(SignalProcessor.process_best_signal)
    gate = src[src.index("# ── No-Entry After Gate ──"):]
    gate = gate[:gate.index("continue")]
    assert "self._historical_entry_cutoff_allows(" in gate
    assert "s1, tf, hist_lane_tag, settings" in gate
    assert "s1=hist_s1)" not in gate
    from engine.signal_processor import _entry_cutoff

    assert _entry_cutoff(Settings(), "15min", s1="UTS")[0] == "14:50"


@pytest.mark.parametrize("tf,bar,now_at,allowed", [
    # 2026-09-26 user: normal signals trade 09:15 -> last entry (15m 14:50, 5m 15:10, 1m 15:14);
    # the lane's old 12:45 / 14:00 cutoffs stopped them early.
    ("15min", "12:45", "13:00:05", True), ("15min", "13:00", "13:15:05", True),
    ("15min", "14:30", "14:45:05", True), ("15min", "14:45", "15:00:05", False),
    ("15min", "14:45", "14:50:30", True), ("15min", "14:45", "14:51:00", False),     # forming bar, clock
    ("5min", "14:30", "14:35:05", True), ("5min", "15:05", "15:10:05", True), ("5min", "15:10", "15:15:05", False),
    ("1min", "15:13", "15:14:05", True), ("1min", "15:14", "15:15:05", False),
    ("15min", "13:00", "13:17:00", False),                                          # stale closed bar
])
def test_normal_ut_smart_signals_trade_until_the_last_entry(monkeypatch, tf, bar, now_at, allowed):
    settings = Settings()
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    sc = Scanner.__new__(Scanner)
    sc._live_signal_stabilization_requirements = lambda c: (0.0, 1, False)
    day = "2026-09-24"
    cand = SimpleNamespace(timeframe=tf, source_engine="ut_smart", source_lane="UTS",
                           signal_timestamp=datetime.strptime(f"{day} {bar}", "%Y-%m-%d %H:%M"))
    now = IST.localize(datetime.strptime(f"{day} {now_at}", "%Y-%m-%d %H:%M:%S"))
    assert sc._entry_cutoff_allows_candidate(cand, now) is allowed


# ================================================================ intra-candle mode (2026-09-26 user)
# UT SMART EXIT follows the general "Intra-candle Signals" toggle: a flip on the forming
# 5m/15m candle may enter after a 20s buffer; the trade is re-checked every 10s until the
# candle closes and exits UTS_REPAINT if the flip is gone or the close does not confirm it;
# reversals happen only on closed candles.

from engine import s1_lane, uts_lane  # noqa: E402


def _intrabar_settings(on):
    s = Settings()
    object.__setattr__(s, "uts_lane_allow_intrabar_entries", bool(on))
    object.__setattr__(s, "live_intrabar_signal_entries", bool(on))
    object.__setattr__(s, "ut_signal_mode", "realtime" if on else "confirmed")
    return s


def _forming_frame():
    """Session 15m candles whose LAST candle is still forming by the wall clock."""
    import pytz

    now = datetime.now(pytz.timezone("Asia/Kolkata")).replace(tzinfo=None, second=0, microsecond=0)
    last_start = now.replace(minute=(now.minute // 15) * 15)
    frame = _session_frame(days=12)
    frame.index = frame.index + (last_start - frame.index[-1])
    return frame, last_start


@pytest.mark.parametrize("on", [True, False])
def test_the_engine_fires_on_the_forming_candle_only_with_the_toggle_on(monkeypatch, on):
    import config.settings as settings_module
    import engine.ut_smart_engine as uts_module

    settings = _intrabar_settings(on)
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    # the session filter must not drop the forming candle whatever the wall-clock hour
    monkeypatch.setattr(uts_module, "drop_off_session_bars", lambda df, end="15:15": df)
    frame, last_start = _forming_frame()
    fired = []
    for push in (+400.0, -400.0):
        f = frame.copy()
        close = f["close"].iloc[-1] + push
        f.iloc[-1, f.columns.get_loc("close")] = close
        f.iloc[-1, f.columns.get_loc("high")] = max(f["high"].iloc[-1], close) + 2
        f.iloc[-1, f.columns.get_loc("low")] = min(f["low"].iloc[-1], close) - 2
        res = AltSignalEngine(signal_source="ut_smart").process(f, instrument=INDEX, timeframe="15min")
        fired += [s for s in res["signals"] if getattr(s, "uts_forming", False)]
    if on:
        assert fired and all(pd.Timestamp(s.timestamp) == last_start for s in fired)
        assert all(s.uts_exit_reason == "" for s in fired)
    else:
        assert fired == []


def test_a_closed_frame_never_yields_a_forming_signal(monkeypatch):
    import config.settings as settings_module

    settings = _intrabar_settings(True)
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    frame = _session_frame()                          # September dates: every candle closed
    res = AltSignalEngine(signal_source="ut_smart").process(frame, instrument=INDEX, timeframe="15min")
    assert not any(getattr(s, "uts_forming", False) for s in res["signals"])


def test_timezone_aware_live_candles_can_emit_a_forming_signal(monkeypatch):
    import config.settings as settings_module
    import engine.ut_smart_engine as uts_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: _intrabar_settings(True))
    monkeypatch.setattr(uts_module, "drop_off_session_bars", lambda df, end="15:15": df)
    frame, last_start = _forming_frame()
    frame.index = frame.index.tz_localize("Asia/Kolkata")
    fired = []
    for push in (+400.0, -400.0):
        candidate = frame.copy()
        close = candidate["close"].iloc[-1] + push
        candidate.iloc[-1, candidate.columns.get_loc("close")] = close
        candidate.iloc[-1, candidate.columns.get_loc("high")] = max(candidate["high"].iloc[-1], close) + 2
        candidate.iloc[-1, candidate.columns.get_loc("low")] = min(candidate["low"].iloc[-1], close) - 2
        result = AltSignalEngine(signal_source="ut_smart").process(
            candidate, instrument=INDEX, timeframe="15min"
        )
        fired += [signal for signal in result["signals"] if getattr(signal, "uts_forming", False)]
    assert fired
    assert all(pd.Timestamp(signal.timestamp).tz_localize(None) == last_start for signal in fired)


def _trade_at(signal_time, direction="LONG", timeframe="15min"):
    return SimpleNamespace(source_engine="ut_smart", source_lane="UTS", instrument="NIFTY", signal_time=signal_time,
                           direction=direction, timeframe=timeframe)


def _flip(ts, side="BUY", forming=True):
    return SimpleNamespace(source_engine="ut_smart", timestamp=ts, signal_type=side, uts_forming=forming,
                           uts_exit_reason="", uts_exit_time=None, uts_exit_price=None)


def _uts_result(ts, now, signals=(), closed=False, timeframe="15min"):
    minutes = int(timeframe.replace('min', ''))
    return dict(source_engine="ut_smart", instrument="NIFTY", timeframe=timeframe, signals=list(signals),
                uts_evaluation=dict(observed_at=now, first_bar=ts-timedelta(days=1),
                                    last_closed_bar=ts if closed else ts-timedelta(minutes=minutes),
                                    forming_bar=None if closed else ts))


def test_repaint_checks_every_10s_while_forming_then_once_on_the_close():
    t0 = datetime(2026, 9, 28, 10, 0)
    trade = _trade_at(t0)
    here = _uts_result(t0, t0+timedelta(minutes=2), [_flip(t0)])
    gone = _uts_result(t0, t0+timedelta(minutes=2,seconds=10))
    assert uts_lane.repaint_exit(trade, here, t0 + timedelta(minutes=2)) == ""            # holds
    # vanished 5s later: not re-checked yet (every 10s)
    assert uts_lane.repaint_exit(trade, gone, t0 + timedelta(minutes=2, seconds=5)) == ""
    assert uts_lane.repaint_exit(trade, gone, t0 + timedelta(minutes=2, seconds=10)) == "REPAINT"
    # at the close: a result still computed on the forming candle is waited for
    trade2 = _trade_at(t0)
    assert uts_lane.repaint_exit(trade2, here, t0 + timedelta(minutes=15, seconds=2)) == ""
    assert not getattr(trade2, "uts_confirmed", False)
    # the closed candle confirms it: never checked again
    closed_flip = _uts_result(t0,t0+timedelta(minutes=15,seconds=4),[_flip(t0, forming=False)],closed=True)
    assert uts_lane.repaint_exit(trade2, closed_flip, t0 + timedelta(minutes=15, seconds=4)) == ""
    assert trade2.uts_confirmed is True
    assert uts_lane.repaint_exit(trade2, gone, t0 + timedelta(minutes=40)) == ""
    # the close without the flip: repaint
    trade3 = _trade_at(t0)
    gone = _uts_result(t0,t0+timedelta(minutes=15,seconds=3),closed=True)
    assert uts_lane.repaint_exit(trade3, gone, t0 + timedelta(minutes=15, seconds=3)) == "REPAINT"
    # a closed-candle entry confirms on its first check
    trade4 = _trade_at(t0)
    closed_flip['uts_evaluation']['observed_at'] = t0+timedelta(minutes=16)
    assert uts_lane.repaint_exit(trade4, closed_flip, t0 + timedelta(minutes=16)) == ""
    assert trade4.uts_confirmed is True


def test_a_repainted_intrabar_trade_exits_through_the_monitor(monkeypatch):
    from tests.test_s1_lane_2026_09_23 import _monitor, _open_trade

    # harness clock 10:30; a 5m trade from the forming 10:30 candle whose flip is gone
    def trade():
        return _open_trade(source_engine="ut_smart", source_lane="UTS", entry_price=150.0, current_stop=80.0,
                           initial_stop=80.0, timeframe="5min", signal_time=datetime(2026, 9, 24, 10, 30))

    result = _uts_result(datetime(2026,9,24,10,30),datetime(2026,9,24,10,30),timeframe='5min')
    closed, _, other = _monitor(monkeypatch, trade(), spot=23405.0, premium=150.0, engine_result=result)
    assert closed == ["UTS_REPAINT"] and other == []
    flip = _flip(datetime(2026, 9, 24, 10, 30), side="BUY", forming=True)
    result['signals'] = [flip]
    closed, _, _ = _monitor(monkeypatch, trade(), spot=23405.0, premium=150.0, engine_result=result)
    assert closed == []


def test_the_intra_candle_entry_wait_is_15s():
    # 20s until 2026-10-09; the user set every forming-bar wait to 15s (5s on 1min).
    cand = SimpleNamespace(source_engine="ut_smart", source_lane="UTS", timeframe="15min")
    assert s1_lane.lane_intrabar_wait_seconds(cand, Settings()) == 15.0
    assert Scanner._live_signal_stabilization_requirements.__doc__   # the lane branch returns (wait, 1, False)


def test_reversals_happen_only_on_closed_candles(live, monkeypatch):
    h = live(settings_overrides={"ut_signal_mode": "realtime", "live_intrabar_signal_entries": True})
    clock = [h.sig.timestamp + timedelta(minutes=7)]           # mid-candle

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(clock[0]) if tz else clock[0]

    monkeypatch.setattr(sp, "datetime", Clock)
    monkeypatch.setattr(scanner_module, "datetime", Clock)
    prev = [s for s in h.res["signals"] if s.timestamp < h.sig.timestamp][-1]
    closed = []
    h.sc.trades.open_trades = {"T-prev": SimpleNamespace(
        id="T-prev", instrument=INDEX, timeframe="15min", direction="LONG" if prev.signal_type == "BUY" else "SHORT",
        inst_type="OPT", status="OPEN", entry_price=140.0, current_price=150.0, source_engine="ut_smart",
        source_lane="UTS", signal_time=prev.timestamp, trading_symbol="NIFTYPREV", lots=1, lot_size=LOT)}
    h.sc._get_live_premium_cached = lambda t, allow_blocking=False: 155.0

    def close_and_record(tid, px, reason, exit_time=None):
        closed.append(reason)
        h.sc.trades.open_trades.pop(tid, None)

    h.sc._close_and_record = close_and_record
    # the opposite flip is still on the forming candle: no reversal
    h.sig.uts_forming = True
    assert h.sc._opposite_exit_confirm_ready(INDEX, h.sig, "15min") is False
    for minute in (7, 9, 12):
        clock[0] = h.sig.timestamp + timedelta(minutes=minute)
        held = h.processor.process_best_signal(*h.args)
        assert closed == [] and "T-prev" in h.sc.trades.open_trades
        assert [c.status for c in held] == ["REJECTED - Opposite Signal Wait"]     # kept for recheck
    # the candle closes with the flip: the reversal happens, then the new side enters
    clock[0] = h.sig.timestamp + timedelta(minutes=15, seconds=20)
    h.sig.uts_forming = False
    first = h.processor.process_best_signal(*h.args)
    assert closed == ["UTS_REVERSE"]
    assert [c.status for c in first] == ["REJECTED - Opposite Signal Wait"]
    second = h.processor.process_best_signal(*h.args)
    assert [(c.status, c.source_lane) for c in second] == [("TRADE SIGNAL", "UTS")]


def test_a_replay_never_trades_a_forming_flip(live):
    h = live()
    h.sc.mode = "HISTORICAL"
    h.sig.uts_forming = True
    graded = []
    h.sc.signals._grade_signal = lambda *a, **k: graded.append(1) or ("A", 0.9, [])
    try:
        h.processor.process_best_signal(*h.args)
    except Exception:
        pass
    assert graded == []


def test_a_forming_flip_while_flat_becomes_a_forming_candidate_for_the_buffer(live, monkeypatch):
    h = live(settings_overrides={"ut_signal_mode": "realtime", "live_intrabar_signal_entries": True})
    clock = [h.sig.timestamp + timedelta(minutes=6)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(clock[0]) if tz else clock[0]

    monkeypatch.setattr(sp, "datetime", Clock)
    monkeypatch.setattr(scanner_module, "datetime", Clock)
    h.sig.uts_forming = True
    out = h.processor.process_best_signal(*h.args)
    assert [(c.status, c.source_lane) for c in out] == [("TRADE SIGNAL", "UTS")]
    assert h.processor._live_signal_candle_is_closed(out[0].signal_timestamp, "15min", clock[0]) is False
    assert h.sc._live_signal_stabilization_requirements(out[0]) == (15.0, 1, False)


# ================================================================ late big-move check (2026-09-26 user)
# Normal trading runs to the last entries; from 13:00 the late_session_reclaim test is UT
# SMART's "extra sensitive" big-move check on every timeframe, voting on the move itself
# (volume, strong close, reclaim on each index's last closed 1m and 5m bars). It admits a 1m
# signal without the 1m frame and labels 5m/15m entries. The other engines keep 5m/15m in
# 14:25-15:10 with UT flips on 1m and 5m.


def _reclaim_scanner(monkeypatch, engine, flips=True, volume=1.6):
    s = Settings()
    object.__setattr__(s, "scanner_engine", engine)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: s)
    sc = Scanner.__new__(Scanner)
    sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    flip = SimpleNamespace(timestamp=None, signal_type="BUY")
    sc._cached_signal_near = lambda inst, tf, side, ts, skew: (flip, 0.0) if flips else (None, None)
    sc._entry_signal_clock = lambda sig, tf: None
    sc._candle_thrust_metrics = lambda *a, **k: {"close_location": 0.8, "volume_ratio": volume}
    sc._directional_reclaim_snapshot = lambda *a, **k: {"reclaimed": True, "close_location": 0.8}
    return sc


@pytest.mark.parametrize("engine,tf,at,fires", [
    ("ut_smart", "5min", "13:00", True), ("ut_smart", "5min", "12:55", False), ("ut_smart", "1min", "13:30", True),
    ("ut_smart", "15min", "14:45", True), ("ut_smart", "15min", "15:00", False),
    ("ut_smart", "5min", "15:10", True), ("ut_smart", "5min", "15:15", False),
    ("ut_smart", "1min", "15:14", True), ("ut_smart", "1min", "15:15", False),
    ("quad_prime", "5min", "13:30", False), ("quad_prime", "1min", "14:40", False), ("quad_prime", "5min", "14:40", True),
])
def test_the_late_move_window_and_timeframes(monkeypatch, engine, tf, at, fires):
    sc = _reclaim_scanner(monkeypatch, engine)
    ts = datetime.strptime(f"2026-09-24 {at}", "%Y-%m-%d %H:%M")
    ctx = sc._late_session_reclaim_context("NIFTY", tf, "BUY", ts)
    assert bool(ctx and ctx.get("type") == "late_session_reclaim") is fires


@pytest.mark.parametrize("engine,volume,fires", [
    ("ut_smart", 1.6, True),        # the move itself, no UT flips on the other indices
    ("ut_smart", 0.9, False),       # without volume it is no big move
    ("quad_prime", 1.6, False),     # the other engines still need the UT flips
])
def test_ut_smart_votes_on_the_move_not_on_flip_coincidence(monkeypatch, engine, volume, fires):
    """UT SMART's wide trails flip about once a day, so needing each index's own 1m AND 5m
    flips within 240s fired 4 times in 44 sessions -- never 'extra sensitive'."""
    sc = _reclaim_scanner(monkeypatch, engine, flips=False, volume=volume)
    ts = datetime(2026, 9, 24, 14, 5)
    ctx = sc._late_session_reclaim_context("NIFTY", "5min", "SELL", ts)
    assert bool(ctx and ctx.get("type") == "late_session_reclaim") is fires


def test_the_move_is_read_on_closed_bars_at_the_signal(monkeypatch):
    sc = _reclaim_scanner(monkeypatch, "ut_smart", flips=False)
    sc.mode = "HISTORICAL"
    seen = []
    sc._candle_thrust_metrics = lambda inst, tf, side, as_of=None: seen.append((tf, as_of)) or {
        "close_location": 0.8, "volume_ratio": 1.6}
    sc._late_session_reclaim_context("NIFTY", "15min", "BUY", datetime(2026, 9, 24, 13, 30))
    assert set(seen) == {("1min", datetime(2026, 9, 24, 13, 45)), ("5min", datetime(2026, 9, 24, 13, 45))}
    seen.clear()
    sc.mode = "REAL"                                     # live slices by bar start: last closed bars
    later = datetime(2026, 9, 24, 13, 47, 30)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(later) if tz else later

    monkeypatch.setattr(scanner_module, "datetime", Clock)
    sc._late_session_reclaim_context("NIFTY", "15min", "BUY", datetime(2026, 9, 24, 13, 30))
    assert set(seen) == {("1min", datetime(2026, 9, 24, 13, 44)), ("5min", datetime(2026, 9, 24, 13, 40))}


@pytest.mark.parametrize("tf,signal_at,now_at,allowed", [
    ("15min", "12:45", None, True), ("5min", "13:00", None, True),
    ("15min", "14:30", None, True), ("15min", "14:45", None, False),        # closes 14:45 / 15:00
    ("15min", "14:45", "14:47:20", True), ("15min", "14:45", "14:51:00", False),   # forming, by the clock
    ("5min", "15:05", None, True), ("5min", "15:05", "15:10:40", True), ("5min", "15:10", None, False),
    ("1min", "15:13", None, True), ("1min", "15:13", "15:14:30", True),
    ("1min", "15:14", None, False), ("1min", "15:14", "15:15:01", False),
])
def test_each_timeframe_has_its_own_last_entry(tf, signal_at, now_at, allowed):
    from engine import entry_window

    day = "2026-09-24"
    signal = datetime.strptime(f"{day} {signal_at}", "%Y-%m-%d %H:%M")
    now = datetime.strptime(f"{day} {now_at}", "%Y-%m-%d %H:%M:%S") if now_at else None
    assert entry_window.entry_allowed(signal, tf, Settings(), now=now) is allowed


def test_the_last_entry_times_are_the_dashboard_settings():
    from engine import entry_window

    s = Settings()
    assert (s.ut_no_entry_after, s.ut_5min_no_entry_after, s.ut_1min_no_entry_after,
            s.uts_lane_reclaim_start) == ("14:50", "15:10", "15:14", "13:00")
    object.__setattr__(s, "ut_no_entry_after", "14:20")
    at_1400 = datetime(2026, 9, 24, 14, 0)                       # closes 14:15
    assert entry_window.entry_allowed(at_1400, "15min", s) is True
    assert entry_window.entry_allowed(at_1400 + timedelta(minutes=15), "15min", s) is False
    assert not {k for k in Settings.model_fields if k.startswith(("uts_lane_cutoff", "s1_lane_cutoff",
                                                                   "uts_lane_reclaim_last_entry"))}


@pytest.mark.parametrize("reclaim,at,admitted", [
    (False, "13:30", True), (False, "14:30", True),     # normal signals after 13:00 (were NO ENTRY - AFTER 12:45)
    (False, "14:45", False),                            # closes 15:00, past the 14:50 last entry
    (True, "13:30", True),                              # a late big move is labelled
])
def test_the_candidate_builder_trades_normal_signals_after_1300(live, monkeypatch, reclaim, at, admitted):
    h = live()
    day = h.sig.timestamp.date()
    ts = datetime.combine(day, datetime.strptime(at, "%H:%M").time())
    clock = [ts + timedelta(minutes=15, seconds=20)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(clock[0]) if tz else clock[0]

    monkeypatch.setattr(sp, "datetime", Clock)
    monkeypatch.setattr(scanner_module, "datetime", Clock)
    h.sig.timestamp = ts
    h.sc._last_signal_time = {f"{INDEX}_15min": ts - timedelta(seconds=1)}
    h.sc._entry_reversal_relief_context = lambda *a, **k: (
        {"type": "late_session_reclaim", "reason": "synchronized reclaim (test)"} if reclaim else None
    )
    out = h.processor.process_best_signal(*h.args)
    assert len(out) == 1
    if admitted:
        assert (out[0].status, out[0].action) == ("TRADE SIGNAL", "ENTRY")
        assert bool(out[0].late_session_reclaim_entry) is reclaim
    else:
        assert out[0].action == "NO_ENTRY" and out[0].status.startswith("NO ENTRY - AFTER 14:50")


# ================================================================ 1m rule (2026-09-26 user)
# A plain 1m signal enters only when the TF toggle is 1M alone; every other policy --
# 1M+5M, 1M+15M and 1M+5M+15M included -- takes 1m bursts / snapbacks / cascades /
# reclaims only.

_ONE_MIN_POLICIES = ["PRIMARY_1"]
_OTHER_POLICIES = ["INCLUDE_1MIN_5MIN", "INCLUDE_1MIN_15MIN", "INCLUDE_1MIN_5MIN_15MIN",
                   "PRIMARY_15", "PRIMARY_5", "INCLUDE_5MIN"]


@pytest.mark.parametrize("policy", _ONE_MIN_POLICIES + _OTHER_POLICIES)
@pytest.mark.parametrize("tag", [None, "volume_spike_entry", "one_way_cascade_entry", "late_session_reclaim_entry"])
def test_the_live_gate_takes_plain_1m_only_under_1m_alone(monkeypatch, policy, tag):
    s = Settings()
    object.__setattr__(s, "ut_timeframe_entry_policy", policy)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: s)
    sc = Scanner.__new__(Scanner)
    sc._gate_block = lambda c, msg: False
    sc._is_quad_prime_raw_candidate = lambda c: False
    cand = SimpleNamespace(timeframe="1min", instrument=INDEX, direction="LONG")
    if tag:
        setattr(cand, tag, True)
    assert sc._passes_timeframe_entry_policy(cand) is bool(tag or policy in _ONE_MIN_POLICIES)


def test_the_admission_filter_keeps_plain_1m_only_when_asked():
    plain = {"tf": "1min", "sig": SimpleNamespace()}
    burst = {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=True)}
    reclaim = {"tf": "1min", "sig": SimpleNamespace(late_session_reclaim_entry=True)}
    five = {"tf": "5min", "sig": SimpleNamespace()}
    rows = [plain, burst, reclaim, five]
    assert SignalProcessor._entry_admissible_raw_candidates(rows) == [burst, reclaim, five]
    assert SignalProcessor._entry_admissible_raw_candidates(rows, plain_one_min=True) == rows


def test_a_1m_cascade_is_tagged_before_admission():
    """The cascade/reclaim tags used to be computed after selection on the winner only,
    so a 1m cascade or reclaim row was filtered out before it could be tagged."""
    seen = []
    sc = SimpleNamespace(
        _late_session_reclaim_context=lambda *a: seen.append(("reclaim",) + a) or None,
        _one_way_cascade_context=lambda *a: seen.append(("cascade",) + a) or {"type": "one_way_cascade", "reason": "t"},
    )
    ts = datetime(2026, 9, 24, 11, 0)
    one = {"tf": "1min", "sig": SimpleNamespace(signal_type="SELL", timestamp=ts)}
    five = {"tf": "5min", "sig": SimpleNamespace(signal_type="SELL", timestamp=ts)}
    SignalProcessor._tag_one_min_fast_contexts(sc, INDEX, [one, five])
    assert one["sig"].one_way_cascade_entry is True and one["sig"].one_way_cascade_context["type"] == "one_way_cascade"
    assert not hasattr(five["sig"], "one_way_cascade_entry")
    assert seen == [("reclaim", INDEX, "1min", "SELL", ts), ("cascade", INDEX, "1min", "SELL", ts)]
    assert SignalProcessor._entry_admissible_raw_candidates([one]) == [one]


@pytest.mark.parametrize("policy,enters", [
    ("PRIMARY_1", True),
    ("INCLUDE_1MIN_5MIN", False), ("INCLUDE_1MIN_15MIN", False), ("INCLUDE_1MIN_5MIN_15MIN", False),
    ("INCLUDE_5MIN", False), ("PRIMARY_5", False),
])
def test_a_plain_ut_smart_1m_flip_end_to_end(live, monkeypatch, policy, enters):
    # the shipped 1m trail (key 15) flips too rarely on the synthetic series
    from engine import ut_smart_engine
    monkeypatch.setitem(ut_smart_engine.PER_TIMEFRAME_FIELDS, "1min", {})
    h = live(tf="1min", settings_overrides={"ut_timeframe_entry_policy": policy,
                                            "one_min_scalping_enabled": True,
                                            "volume_spike_entry_enabled": True,
                                            "one_min_raw_entry_enabled": False})
    h.sc._detect_volume_spike_entry = lambda *a, **k: None          # a plain flip: no burst
    h.sc._one_way_cascade_context = lambda *a, **k: None
    h.sc._late_session_reclaim_context = lambda *a, **k: None
    out = h.processor.process_best_signal(*h.args)
    entries = [c for c in out if c.action == "ENTRY" and c.status == "TRADE SIGNAL"]
    if enters:
        assert len(entries) == 1 and entries[0].timeframe == "1min" and entries[0].one_min_raw_entry is True
        assert h.sc._passes_timeframe_entry_policy(entries[0]) is True
    else:
        assert entries == []


def test_a_flip_survives_an_exit_that_has_not_filled_yet(live):
    """REAL mode: close_trade marks the old trade EXIT_PENDING and it stays OPEN until the
    broker fill. A scan in between used to refuse the new side as 'Existing Active Trade'
    (not recheckable) and the reversal entry was lost for good."""
    h = live()
    prev = [s for s in h.res["signals"] if s.timestamp < h.sig.timestamp][-1]
    old = SimpleNamespace(
        id="T-prev", instrument=INDEX, timeframe="15min", direction="LONG" if prev.signal_type == "BUY" else "SHORT",
        inst_type="OPT", status="OPEN", entry_price=140.0, current_price=150.0, source_engine="ut_smart",
        source_lane="UTS", signal_time=prev.timestamp, trading_symbol="NIFTYPREV", lots=1, lot_size=LOT,
        pending_exit=False, execution_status="")
    h.sc.trades.open_trades = {"T-prev": old}
    h.sc._get_live_premium_cached = lambda t, allow_blocking=False: 155.0
    exits = []

    def close_and_record(tid, px, reason, exit_time=None):   # exit order out, not filled
        exits.append(reason)
        old.pending_exit, old.execution_status = True, "EXIT_PENDING"

    h.sc._close_and_record = close_and_record
    first = h.processor.process_best_signal(*h.args)
    assert exits == ["UTS_REVERSE"] and [c.status for c in first] == ["REJECTED - Opposite Signal Wait"]
    second = h.processor.process_best_signal(*h.args)          # fill still pending
    assert [c.status for c in second] == ["REJECTED - Existing Opposite Trade"]
    assert h.processor._recheckable_rejection_status("REJECTED - Existing Opposite Trade")
    h.sc.trades.open_trades.pop("T-prev")                      # the broker fill lands
    third = h.processor.process_best_signal(*h.args)
    assert [(c.status, c.source_lane) for c in third] == [("TRADE SIGNAL", "UTS")]
    assert exits == ["UTS_REVERSE"]                            # one exit order, not three


# ================================================================ per-timeframe trail
# Defaults match the uploaded Pine script; explicit per-timeframe overrides remain.


def test_each_timeframe_gets_its_own_trail():
    from engine import ut_smart_engine as uts_engine

    s = Settings()
    assert (uts_engine.params_from_settings(s, "15min").key_value, uts_engine.params_from_settings(s, "15min").atr_period) == (1.0, 10)
    assert (uts_engine.params_from_settings(s, "5min").key_value, uts_engine.params_from_settings(s, "5min").atr_period) == (1.0, 10)
    assert (uts_engine.params_from_settings(s, "1min").key_value, uts_engine.params_from_settings(s, "1min").atr_period) == (1.0, 10)
    object.__setattr__(s, "uts_key_value_5min", 5.5)
    assert uts_engine.params_from_settings(s, "5min").key_value == 5.5
    assert uts_engine.params_from_settings(s, "15min").key_value == 1.0     # untouched
    assert "uts_lane_cutoff_5min" not in Settings.model_fields             # no lane cutoffs


def test_the_live_engine_uses_the_timeframes_trail(monkeypatch):
    import config.settings as settings_module
    from engine import ut_smart_engine as uts_engine

    s = Settings()
    object.__setattr__(s, "uts_key_value_5min", 4.5)
    object.__setattr__(s, "uts_atr_period_5min", 21)
    monkeypatch.setattr(settings_module, "get_settings", lambda: s)
    frame = _session_frame(freq="5min", days=10)
    res = AltSignalEngine(signal_source="ut_smart").process(frame, instrument=INDEX, timeframe="5min")
    p = uts_engine.params_from_settings(s, "5min")
    closed = uts_engine.drop_off_session_bars(frame)
    ind = uts_engine.indicators(closed, p)
    expected = [(r["time"], r["direction"]) for r in uts_engine.trades(closed.index, ind, uts_engine.run(ind, p))]
    got = [(pd.Timestamp(sg.timestamp), 1 if sg.signal_type == "BUY" else -1)
           for sg in res["signals"] if not getattr(sg, "uts_forming", False)]
    assert got == expected and got
    narrow = uts_engine.indicators(closed, uts_engine.params_from_settings(s, "15min"))
    assert int((uts_engine.run(narrow, uts_engine.params_from_settings(s, "15min"))["signal"] != 0).sum()) > len(got)


def test_a_timeframe_trail_change_recomputes_that_chart(monkeypatch):
    import config.settings as settings_module
    from engine.multi_timeframe import MultiTimeframeEngine

    s = Settings()
    object.__setattr__(s, "live_intrabar_signal_entries", False)
    monkeypatch.setattr(settings_module, "get_settings", lambda: s)

    class _E:
        signal_source, signal_mode, calls = "ut_smart", "realtime", 0

        def process(self, **_kw):
            _E.calls += 1
            return {"signals": [], "state": {"position": 0}}

    mtf = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
    mtf.engines = {"NIFTY_5min": _E()}
    mtf._data_cache, mtf._result_cache, mtf._data_hash = {}, {}, {}
    mtf._input_metadata, mtf._result_metadata, mtf._fresh_result_required_after = {}, {}, {}
    mtf.tf_weights = {"1min": 0.15, "5min": 0.40, "15min": 0.45}
    mtf.update_data("NIFTY", "5min", _session_frame(freq="5min", days=3))
    mtf.process_instrument("NIFTY")
    mtf.process_instrument("NIFTY")
    assert _E.calls == 1
    object.__setattr__(s, "uts_key_value_5min", 6.0)
    mtf.process_instrument("NIFTY")
    assert _E.calls == 2


@pytest.mark.parametrize("cascade", [True, False])
def test_a_1m_cascade_enters_without_the_1m_frame_selected(live, monkeypatch, cascade):
    from engine import ut_smart_engine
    monkeypatch.setitem(ut_smart_engine.PER_TIMEFRAME_FIELDS, "1min", {})
    h = live(tf="1min", settings_overrides={"ut_timeframe_entry_policy": "INCLUDE_5MIN",
                                            "one_min_scalping_enabled": True,
                                            "volume_spike_entry_enabled": True,
                                            "one_min_raw_entry_enabled": False})
    side = h.sig.signal_type
    h.sc._detect_volume_spike_entry = lambda *a, **k: None
    h.sc._late_session_reclaim_context = lambda *a, **k: None
    h.sc._one_way_cascade_context = lambda *a, **k: (
        {"type": "one_way_cascade", "reason": f"one-way {side} cascade (test)", "indices": [INDEX]} if cascade else None
    )
    out = h.processor.process_best_signal(*h.args)
    entries = [c for c in out if c.action == "ENTRY" and c.status == "TRADE SIGNAL"]
    if cascade:
        assert len(entries) == 1 and entries[0].timeframe == "1min" and entries[0].one_way_cascade_entry is True
        assert h.sc._passes_timeframe_entry_policy(entries[0]) is True
    else:
        assert entries == []


def test_a_closed_candle_or_restored_trade_is_not_read_as_a_repaint():
    """A closed candle cannot repaint (the rule trade_manager applies on restore). The UTS
    confirmation is in memory only, so after a restart a drifted result -- a changed trail,
    a vendor backfill that nudged a bar -- read a closed-candle entry, or an intracandle one
    confirmed hours earlier, as REPAINT and closed it."""
    t0 = datetime(2026, 9, 28, 10, 0)
    gone = {"signals": []}
    closed_entry = _trade_at(t0)
    closed_entry.entered_intracandle = False
    assert uts_lane.repaint_exit(closed_entry, gone, t0 + timedelta(minutes=15, seconds=3)) == ""
    restored = _trade_at(t0)                      # intracandle entry rehydrated a bar after its close
    restored.entered_intracandle = True
    assert uts_lane.repaint_exit(restored, gone, t0 + timedelta(minutes=30)) == ""
    assert not getattr(restored, 'uts_confirmed', False)  # elapsed time is not positive confirmation
    # within one bar of the close a running process still judges the close
    fresh = _trade_at(t0)
    fresh.entered_intracandle = True
    fresh_result = _uts_result(t0,t0+timedelta(minutes=29),closed=True)
    assert uts_lane.repaint_exit(fresh, fresh_result, t0 + timedelta(minutes=29)) == "REPAINT"


@pytest.mark.parametrize("reclaim,at,admitted", [
    (True, "15:13", True),              # closes 15:14 = the 1m last entry
    (True, "15:14", False),             # closes 15:15, the squareoff
    (False, "15:13", False),            # a plain 1m flip under 5M+15M
])
def test_a_1m_reclaim_enters_at_1514_under_the_5m_15m_toggle(live, monkeypatch, reclaim, at, admitted):
    from engine import ut_smart_engine
    monkeypatch.setitem(ut_smart_engine.PER_TIMEFRAME_FIELDS, "1min", {})
    h = live(tf="1min", settings_overrides={"ut_timeframe_entry_policy": "INCLUDE_5MIN",
                                            "one_min_scalping_enabled": True,
                                            "volume_spike_entry_enabled": True,
                                            "one_min_raw_entry_enabled": False})
    day = h.sig.timestamp.date()
    ts = datetime.combine(day, datetime.strptime(at, "%H:%M").time())
    clock = [ts + timedelta(minutes=1, seconds=5)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return IST.localize(clock[0]) if tz else clock[0]

    monkeypatch.setattr(sp, "datetime", Clock)
    monkeypatch.setattr(scanner_module, "datetime", Clock)
    h.sig.timestamp = ts
    h.sc._last_signal_time = {f"{INDEX}_1min": ts - timedelta(seconds=1)}
    side = h.sig.signal_type
    h.sc._detect_volume_spike_entry = lambda *a, **k: None
    h.sc._one_way_cascade_context = lambda *a, **k: None
    h.sc._late_session_reclaim_context = lambda *a, **k: (
        {"type": "late_session_reclaim", "reason": f"synchronized {side} reclaim (test)"} if reclaim else None
    )
    h.sc._live_signal_stabilization_requirements = lambda c: (0.0, 1, False)
    out = h.processor.process_best_signal(*h.args)
    entries = [c for c in out if c.action == "ENTRY" and c.status == "TRADE SIGNAL"]
    if admitted:
        assert len(entries) == 1 and entries[0].timeframe == "1min" and entries[0].late_session_reclaim_entry is True
        assert h.sc._passes_timeframe_entry_policy(entries[0]) is True
        assert h.sc._entry_cutoff_allows_candidate(entries[0], IST.localize(clock[0])) is True
    else:
        assert entries == []
