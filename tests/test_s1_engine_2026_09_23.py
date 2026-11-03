"""S1 engine (engine/s1_engine.py): port of the user's S1 TradingView script.

Two state machines:
  pine  - the chart's: trade booked at the signal bar's OPEN and managed on that bar, so the
          engine signals on exactly the bars the TradingView chart does.
  close - tradable: entry at the signal close, management from the next bar.
Backtest (68 sessions, 2026-06-17..09-23) of the pine signals: the chart's own open-entry
bookkeeping made +9..14% per timeframe; entering at the signal close lost with every exit
set tried. These tests pin the port's causality, gates and both state machines.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from config.settings import Settings
from engine.s1_engine import (
    S1Params,
    S1_FEED_BARS,
    S1_WARMUP_BARS,
    compute_features,
    get_s1_signals,
    params_from_settings,
    s1_candle_frame,
    simulate,
)

ROOT = Path(__file__).resolve().parents[1]


def _frame(seed=7, sessions=8, tf=5):
    rng = np.random.default_rng(seed)
    idx = []
    for day in pd.bdate_range("2026-08-03", periods=sessions):
        start = day + pd.Timedelta(hours=9, minutes=15)
        end = day + pd.Timedelta(hours=15, minutes=30) - pd.Timedelta(minutes=tf)
        idx += list(pd.date_range(start, end, freq=f"{tf}min"))
    n = len(idx)
    drift = np.repeat(rng.choice([-1.0, 1.0], size=n // 25 + 1), 25)[:n] * 4.0
    close = 24000 + np.cumsum(drift + rng.normal(0, 12, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 2, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 6, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 6, n))
    vol = rng.lognormal(13, 0.5, n) * np.where(np.abs(close - open_) > 15, 2.5, 1.0)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=pd.DatetimeIndex(idx),
    )


@pytest.fixture(scope="module", params=[7, 11, 3])
def run(request):
    df = _frame(request.param)
    result = simulate(df, "5min")
    assert len(result.trades) >= 5, "fixture must exercise the state machine"
    return df, result


def test_defaults_are_the_users_inputs():
    p = S1Params()
    assert (p.min_score, p.signal_cooldown, p.exit_conf_bars, p.max_consec_loss, p.circuit_pause) == (60, 0, 3, 3, 5)
    assert (p.sl_min_pct, p.sl_max_pct, p.use_sl2, p.entry_mode) == (0.10, 0.25, False, "pine")
    s = Settings()
    object.__setattr__(s, "max_consecutive_losses", 3)
    assert params_from_settings(s, "NIFTY") == p


def test_loss_streak_follows_the_system_setting_unless_customised_per_index():
    """2026-09-25 user: the S1 loss stop follows "Stop After Consecutive Losses" (6) per
    index unless that index has its own value; a win resets the run."""
    s = Settings()
    for name, value in (("max_consecutive_losses", 6), ("s1_max_consec_loss_nifty", 0),
                        ("s1_max_consec_loss_banknifty", 0), ("s1_max_consec_loss_sensex", 2),
                        ("s1_max_consec_loss_midcpnifty", 0)):
        object.__setattr__(s, name, value)
    got = [params_from_settings(s, n).max_consec_loss for n in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")]
    assert got == [6, 6, 2, 6]
    object.__setattr__(s, "max_consecutive_losses", 4)          # dashboard change applies at once
    assert params_from_settings(s, "NIFTY").max_consec_loss == 4
    assert params_from_settings(s, "SENSEX").max_consec_loss == 2


@pytest.mark.parametrize("mode", ["pine", "close"])
def test_no_lookahead_scores_and_trades_match_on_every_prefix(mode):
    df = _frame(7)
    p = S1Params(entry_mode=mode)
    full = simulate(df, "5min", p)
    for cut in (260, 333, 410, 522, 580):
        part = simulate(df.iloc[:cut], "5min", p)
        for key in ("bull_score", "bear_score", "ema9_15", "rsi5m", "vwap"):
            np.testing.assert_allclose(part.features[key], full.features[key][:cut], equal_nan=True, err_msg=key)
        np.testing.assert_array_equal(part.positions[: cut - 1], full.positions[: cut - 1])
        entries = lambda r: [(t.entry_bar, t.direction, t.entry_price) for t in r.trades if t.entry_bar < cut - 1]
        assert entries(part) == entries(full)


def test_fifteen_minute_values_read_only_closed_buckets():
    df = _frame(7)
    base = compute_features(df, "5min")
    changed = df.copy()
    at = changed.index.get_loc(pd.Timestamp("2026-08-06 09:40"))  # last 5m bar of the 09:30 bucket
    changed.iloc[at, changed.columns.get_loc("close")] += 500.0
    moved = compute_features(changed, "5min")
    for ts in ("2026-08-06 09:30", "2026-08-06 09:35", "2026-08-06 09:40"):
        i = df.index.get_loc(pd.Timestamp(ts))
        assert moved["ema9_15"][i] == base["ema9_15"][i], ts
    after = df.index.get_loc(pd.Timestamp("2026-08-06 09:45"))
    assert moved["ema9_15"][after] != base["ema9_15"][after]


def test_fifteen_minute_chart_reads_the_true_five_minute_rsi():
    """Historical lookahead_on reads the first intrabar's previous 5m RSI."""
    five = _frame(7)
    fifteen = five.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    f = compute_features(fifteen, "15min", S1Params(candle_type="standard"), df_5min=five)
    from engine.s1_engine import _rsi
    rsi5 = _rsi(five["close"], 14)
    ts = pd.Timestamp("2026-08-06 10:00")
    i = fifteen.index.get_loc(ts)
    assert f["rsi5m"][i] == pytest.approx(rsi5.loc[pd.Timestamp("2026-08-06 09:55")])


def test_pine_mode_books_the_open_and_manages_the_signal_bar(run):
    """The chart's bookkeeping: entry at the signal bar's open, stop/target checks on that bar."""
    df, _ = run
    result = simulate(df, "5min", S1Params(entry_mode="pine", candle_type="standard"))
    opens = df["open"].to_numpy()
    for t in result.trades:
        assert t.entry_price == opens[t.entry_bar]
        assert t.exit_bar < 0 or t.exit_bar >= t.entry_bar
        if t.exit_reason == "Hard SL1":
            assert t.exit_price == pytest.approx(t.sl1)   # the Pine fills at the stop


def test_close_mode_enters_at_the_close_and_manages_from_the_next_bar(run):
    df, _ = run
    result = simulate(df, "5min", S1Params(entry_mode="close", candle_type="standard"))
    close = df["close"].to_numpy()
    assert result.trades
    for t in result.trades:
        assert t.entry_price == close[t.entry_bar]
        assert t.exit_bar < 0 or t.exit_bar > t.entry_bar


def test_every_entry_passes_all_five_gates(run):
    df, result = run
    p, f = S1Params(), result.features
    for t in result.trades:
        i = t.entry_bar
        assert i >= S1_WARMUP_BARS
        assert f["adx"][i] >= p.min_adx
        assert f["vol_above"][i] or f["vol_spike"][i]
        assert abs(f["close"][i] - f["open"][i]) >= f["atr"][i] * 0.15
        mine, other = (f["bull_score"][i], f["bear_score"][i]) if t.direction == 1 else (f["bear_score"][i], f["bull_score"][i])
        assert mine >= p.min_score and mine - other >= p.conflict_margin and other < 45.0
        if t.direction == 1:
            assert f["body_pct"][i] > 0.5 and f["ema9_15"][i] > f["ema21_15"][i] and f["ema9_15_slope"][i] > 0
        else:
            assert f["body_pct"][i] < 0.5 and f["ema9_15"][i] < f["ema21_15"][i] and f["ema9_15_slope"][i] < 0


@pytest.mark.parametrize("mode", ["anchor", "atr"])
def test_dynamic_stop_is_clamped_between_the_user_bounds(run, mode):
    df, _ = run
    result = simulate(df, "5min", S1Params(sl_mode=mode, entry_mode="close"))
    for t in result.trades:
        assert 0.10 - 1e-9 <= t.sl_pct <= 0.25 + 1e-9
        risk = abs(t.entry_price - t.sl1)
        d = t.direction
        assert t.targets == pytest.approx(tuple(t.entry_price + d * risk * r for r in (2.0, 3.0, 4.0)))


@pytest.mark.parametrize("live_mode", ["anchor", "atr", "fixed"])
def test_pine_state_machine_always_uses_the_chart_stops(run, live_mode):
    """The live SL mode must not leak into the chart state machine: an anchor stop on the
    signal bar's own low stopped the open-booked trade out inside its own bar."""
    df, _ = run
    for t in simulate(df, "5min", S1Params(sl_mode=live_mode, sl1_pct=0.1, chart_sl1_pct=0.2, chart_sl2_pct=0.4)).trades:
        d, ep = t.direction, t.entry_price
        assert t.sl1 == ep * (1.0 - 0.2 / 100.0) if d == 1 else t.sl1 == ep * (1.0 + 0.2 / 100.0)
        assert t.sl2 == pytest.approx(ep * (1 - d * 0.004))
        rk = abs(ep - t.sl1)
        assert t.targets == tuple(ep + rk * r if d == 1 else ep - rk * r for r in (2.0, 3.0, 4.0))


def test_pine_sl2_substitution_matches_calcsl():
    from engine.s1_engine import _pine_levels
    sl1, sl2, _, _ = _pine_levels(S1Params(chart_sl1_pct=0.3, chart_sl2_pct=0.2), 100.0, 1)
    assert sl2 == pytest.approx(100.0 * (1 - 0.35 / 100))
    _, sl2, _, _ = _pine_levels(S1Params(chart_sl1_pct=0.2, chart_sl2_pct=0.4), 100.0, -1)
    assert sl2 == pytest.approx(100.4)


def test_no_sl2_on_the_live_order_unless_switched_on():
    df = _frame(3)
    trades = simulate(df, "5min", S1Params(entry_mode="close")).trades
    assert trades and all(t.exit_reason != "SL2 GAP (Max Loss)" for t in trades)


def test_one_trade_at_a_time(run):
    _, result = run
    for prev, nxt in zip(result.trades, result.trades[1:]):
        assert nxt.entry_bar > prev.exit_bar >= 0


def test_cooldown_counts_bars_from_the_last_signal():
    df = _frame(11)
    trades = simulate(df, "5min", S1Params(signal_cooldown=12)).trades
    assert trades
    for prev, nxt in zip(trades, trades[1:]):
        assert nxt.entry_bar - prev.entry_bar >= 12


def test_session_rules_cut_off_entries_and_close_at_session_end(run):
    df, _ = run
    result = simulate(df, "5min", S1Params(entry_mode="close", session_rules=True))
    f = result.features
    for t in result.trades:
        assert f["close_min"][t.entry_bar] <= 15 * 60
    flat_zone = (f["close_min"] >= 15 * 60 + 15) | f["last_of_session"]
    assert not result.positions[flat_zone].any()


def test_circuit_breaker_pauses_after_the_loss_streak():
    df = _frame(3)
    free = simulate(df, "5min", S1Params(max_consec_loss=1, circuit_pause=0))
    gaps = [n.entry_bar - p.exit_bar for p, n in zip(free.trades, free.trades[1:]) if p.pnl_points <= 0]
    assert min(gaps) <= 20, "fixture needs a quick re-entry after a loss for the pause to block"

    paused = simulate(df, "5min", S1Params(max_consec_loss=1, circuit_pause=20))
    for prev, nxt in zip(paused.trades, paused.trades[1:]):
        if prev.pnl_points <= 0:
            assert nxt.entry_bar > prev.exit_bar + 20


def test_impossible_score_floor_emits_nothing():
    assert simulate(_frame(7), "5min", S1Params(min_score=101)).trades == []


def test_fifteen_minute_rsi_survives_a_lagging_or_tz_aware_five_minute_feed():
    five = _frame(7)
    fifteen = five.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    full = compute_features(fifteen, "15min", df_5min=five)["rsi5m"]
    lagging = compute_features(fifteen, "15min", df_5min=five.iloc[:-1])["rsi5m"]
    assert np.isfinite(lagging[-1])               # first intrabar does not depend on the later 5m bars
    np.testing.assert_allclose(lagging[:-1], full[:-1], equal_nan=True)
    aware = five.copy()
    aware.index = aware.index.tz_localize("Asia/Kolkata")
    np.testing.assert_allclose(compute_features(fifteen, "15min", df_5min=aware)["rsi5m"], full, equal_nan=True)


def test_signals_carry_s1_identity_and_levels_from_the_close():
    df = _frame(11)
    signals, positions, stops = get_s1_signals(df, "NIFTY", "5min", S1Params())
    assert signals and len(positions) == len(df) == len(stops)
    for s in signals:
        assert s.source_engine == "s1" and s.source_engines == ["s1"]
        assert s.signal_type == ("BUY" if s.position_state == 1 else "SELL")
        assert s.price == df["close"].iloc[s.bar_index]
        assert s.s1_chart_entry == s1_candle_frame(df)["open"].iloc[s.bar_index]
        assert s.stop_distance == pytest.approx(abs(s.price - s.trailing_stop))
        assert 0.10 - 1e-9 <= s.s1_sl_pct <= 0.25 + 1e-9
        d = s.position_state
        assert s.s1_targets == pytest.approx([s.price + d * s.stop_distance * r for r in (2.0, 3.0, 4.0)])
        assert s.s1_score == (s.s1_bull_score if d == 1 else s.s1_bear_score)


def test_alt_engine_dispatches_to_s1():
    from engine.alt_scanners import AltSignalEngine

    from engine.s1_engine import drop_off_session_bars

    # Session-clean frame: the live path drops 15:15+ bars before S1 runs (see
    # tests/test_s1_tradingview_parity_2026_09_24.py for the raw-frame mapping).
    df = drop_off_session_bars(_frame(11))
    result = AltSignalEngine(signal_source="s1").process(df, instrument="NIFTY", timeframe="5min")
    expected, positions, _ = get_s1_signals(df, "NIFTY", "5min")
    got = [(s.bar_index, s.signal_type, s.source_engine) for s in result["signals"]]
    assert got == [(s.bar_index, s.signal_type, "s1") for s in expected]
    assert result["positions"] == positions.tolist()


def test_settings_accept_s1_and_reject_mantra():
    assert Settings(scanner_engine="s1").scanner_engine == "s1"
    with pytest.raises(ValidationError):
        Settings(scanner_engine="mantra_hybrid")


def test_every_s1_setting_reaches_the_engine():
    s = Settings()
    values = dict(
        s1_candle_type="standard",
        s1_min_score=81.0, s1_conflict_margin=33.0, s1_min_adx=23.0, s1_use_htf=False,
        s1_signal_cooldown=7, s1_sl_mode="atr", s1_sl1_pct=0.4, s1_sl_min_pct=0.12, s1_sl_max_pct=0.3,
        s1_sl_atr_mult=1.4, s1_use_sl2=True, s1_sl2_pct=0.9, s1_t1_r=1.5, s1_t2_r=2.5, s1_t3_r=3.5,
        s1_trail_start_r=1.25, s1_trail_mult=2.2, s1_exit_conf_bars=4, s1_circuit_pause=7,
        s1_entry_mode="close", s1_chart_sl1_pct=0.35, s1_chart_sl2_pct=0.8, s1_session_end="15:20",
        s1_sl1_pct_5min=0.12, s1_sl1_pct_15min=0.18,
        s1_max_consec_loss_nifty=2, s1_max_consec_loss_banknifty=3,
        s1_max_consec_loss_sensex=4, s1_max_consec_loss_midcpnifty=5,
    )
    for k, v in values.items():
        object.__setattr__(s, k, v)
    p = params_from_settings(s, "SENSEX")
    got = dict(
        s1_candle_type=p.candle_type,
        s1_min_score=p.min_score, s1_conflict_margin=p.conflict_margin, s1_min_adx=p.min_adx, s1_use_htf=p.use_htf,
        s1_signal_cooldown=p.signal_cooldown, s1_sl_mode=p.sl_mode, s1_sl1_pct=p.sl1_pct, s1_sl_min_pct=p.sl_min_pct,
        s1_sl_max_pct=p.sl_max_pct, s1_sl_atr_mult=p.sl_atr_mult, s1_use_sl2=p.use_sl2, s1_sl2_pct=p.sl2_pct,
        s1_t1_r=p.t1_r, s1_t2_r=p.t2_r, s1_t3_r=p.t3_r, s1_trail_start_r=p.trail_start_r, s1_trail_mult=p.trail_mult,
        s1_exit_conf_bars=p.exit_conf_bars, s1_circuit_pause=p.circuit_pause,
        s1_entry_mode=p.entry_mode, s1_chart_sl1_pct=p.chart_sl1_pct, s1_chart_sl2_pct=p.chart_sl2_pct,
        s1_session_end=p.session_end,
        s1_sl1_pct_5min=p.sl1_pct_5min, s1_sl1_pct_15min=p.sl1_pct_15min,
    )
    assert got == {k: v for k, v in values.items() if not k.startswith("s1_max_consec_loss_")}
    assert p.max_consec_loss == 4
    assert [params_from_settings(s, n).max_consec_loss for n in ("NIFTY", "BANKNIFTY", "MIDCPNIFTY")] == [2, 3, 5]
    engine_fields = {k for k in Settings.model_fields if k.startswith("s1_")}
    assert engine_fields - set(values) <= {k for k in engine_fields if k.startswith("s1_lane_")}


def test_live_feed_widens_warmup_only_for_s1(monkeypatch):
    from config import settings as settings_module
    from scanner import Scanner

    fresh = Settings()
    monkeypatch.setattr(settings_module, "get_settings", lambda: fresh)
    monkeypatch.setattr("scanner.get_settings", lambda: fresh)
    object.__setattr__(fresh, "scanner_engine", "quad_prime")
    assert Scanner._signal_engine_warmup_bars() == 0
    object.__setattr__(fresh, "scanner_engine", "s1")
    warm = Scanner._signal_engine_warmup_bars()
    assert warm == S1_FEED_BARS >= S1_WARMUP_BARS

    df = _frame(7, sessions=14)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_current_session_day = lambda: df.index[-1].date()
    window = scanner._live_analysis_window(df, "5min", warm)
    today = (window.index.normalize() == df.index[-1].normalize()).sum()
    assert len(window) - today == warm
    assert len(scanner._live_analysis_window(df, "5min")) - today == 60


def test_mantra_is_gone():
    assert not (ROOT / "engine" / "mantra_hybrid.py").exists()
    for rel in ("engine/alt_scanners.py", "scanner.py", "engine/multi_timeframe.py",
                "engine/signal_processor.py", "dashboard/server.py", "config/settings.py",
                "dashboard/static/index.html", "dashboard/static/js/app.js",
                "dashboard/static/js/dashboard-settings.js"):
        assert "mantra" not in (ROOT / rel).read_text(encoding="utf-8").lower(), rel
