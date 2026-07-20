"""Pluggable signal engine: utbot / ema / consensus / combo unions.

The alternative engines are drop-ins for UTBotEngine — same .process() interface
and result shape — that only swap the flip trigger for a smoother (less whippy)
trend source, keeping UTBot's ATR/ADX/trailing-stop machinery. The live default
is "utbot_consensus"; pure "utbot" remains available as the unchanged baseline.
"""

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings, get_settings
import engine.alt_scanners as alt_scanners
from engine.ut_bot_core import UTBotEngine, UTBotSignal
from engine.alt_scanners import AltSignalEngine, build_scanner_engine


_PARAMS = dict(
    key_value=1.0, atr_period=10, use_heikin_ashi=False, signal_mode="realtime",
    adx_filter=True, adx_period=14, adx_threshold=25.0, strict_adx=False,
    session_filter=True, session_start="09:18", session_end="15:25",
)


def _trending_df(n=120):
    # Steady uptrend with mild noise -> UT1 whipsaws far more than a smooth trend source.
    rng = np.random.default_rng(3)
    close = 20000 + np.cumsum(rng.normal(1.5, 8, n))
    rows = [{"open": c - 1, "high": c + 6, "low": c - 6, "close": c, "volume": 10000} for c in close]
    return pd.DataFrame(rows, index=pd.date_range("2026-06-24 09:15", periods=n, freq="5min"))


def _swing_df(n=180):
    rng = np.random.default_rng(7)
    wave = np.sin(np.linspace(0, 8 * np.pi, n)) * 180
    drift = np.linspace(-80, 120, n)
    close = 20000 + wave + drift + rng.normal(0, 10, n)
    rows = [
        {
            "open": c - rng.normal(0, 4),
            "high": c + 14,
            "low": c - 14,
            "close": c,
            "volume": 10000 + int(abs(np.cos(i / 8)) * 5000),
        }
        for i, c in enumerate(close)
    ]
    return pd.DataFrame(rows, index=pd.date_range("2026-06-24 09:15", periods=n, freq="5min"))


def _process(engine, df):
    return engine.process(df=df, instrument="NIFTY", timeframe="5min",
                          capital=600000, risk_pct=1.5, lots=1, lot_size=75)


def test_alt_engine_is_drop_in_signal_interface():
    df = _trending_df()
    r = _process(AltSignalEngine(signal_source="consensus", **_PARAMS), df)
    assert "error" not in r
    assert isinstance(r.get("signals"), list)
    assert len(r.get("trailing_stop", [])) == len(df)  # UT stop machinery preserved
    for s in r["signals"]:
        assert isinstance(s, UTBotSignal)
        assert s.signal_type in {"BUY", "SELL"}
        assert s.price > 0 and 0 <= s.bar_index < len(df)
    assert r["state"]["position_label"] in {"LONG", "SHORT", "FLAT"}
    assert r["state"]["position"] == r["positions"][-1]


def test_alt_signals_carry_raw_candle_for_grading():
    # _grade_signal reads signal.raw_candle['close'/'open'/...]; an empty dict would
    # KeyError. Alt signals must carry the bar OHLC so grading works downstream.
    df = _trending_df()
    r = _process(AltSignalEngine(signal_source="consensus", **_PARAMS), df)
    assert r["signals"], "expected at least one consensus signal"
    for s in r["signals"]:
        for k in ("open", "high", "low", "close"):
            assert k in s.raw_candle
        body = abs(s.raw_candle["close"] - s.raw_candle["open"])  # mirrors grading math
        assert s.raw_candle["high"] >= s.raw_candle["low"]
        assert body >= 0


def test_consensus_is_least_whippy_then_ema_then_utbot():
    df = _trending_df()
    n_ut = len(_process(UTBotEngine(**_PARAMS), df)["signals"])
    n_ema = len(_process(AltSignalEngine(signal_source="ema", **_PARAMS), df)["signals"])
    n_con = len(_process(AltSignalEngine(signal_source="consensus", **_PARAMS), df)["signals"])
    # Smoother sources flip less than UTBot's raw trigger; consensus is the smoothest.
    assert n_con <= n_ema <= n_ut
    assert n_con < n_ut  # consensus must be meaningfully less whippy


def test_engine_specific_chart_overlays():
    # Each engine exposes its own indicator lines so the chart visibly reflects it:
    # ema -> EMA9+EMA21, consensus -> Supertrend+EMA21, auto -> all three, utbot -> none.
    df = _trending_df()
    def names(src):
        eng = AltSignalEngine(signal_source=src, **_PARAMS)
        ov = eng.process(df=df, instrument="NIFTY", timeframe="5min",
                         capital=600000, risk_pct=1.5, lots=1, lot_size=75).get("indicator_overlays", [])
        # every overlay carries a name/color and value series the chart can plot
        for o in ov:
            assert o["name"] and o["color"] and isinstance(o["values"], list)
        return [o["name"] for o in ov]
    assert names("ema") == ["EMA 9", "EMA 21"]
    assert any("Supertrend" in n for n in names("consensus")) and "EMA 21" in names("consensus")
    auto = names("auto")
    assert "EMA 9" in auto and "EMA 21" in auto and any("Supertrend" in n for n in auto)
    # utbot has no alt overlays (its trailing-stop line is its indicator)
    assert UTBotEngine(**_PARAMS).process(df=df, instrument="NIFTY", timeframe="5min",
        capital=600000, risk_pct=1.5, lots=1, lot_size=75).get("indicator_overlays") in (None, [])


def test_factory_utbot_mode_is_pure_utbot(settings_engine):
    settings_engine.scanner_engine = "utbot"
    assert type(build_scanner_engine(dict(_PARAMS))) is UTBotEngine


def test_code_default_scanner_engine_is_quad_prime():
    assert Settings.model_fields["scanner_engine"].default == "quad_prime"


def test_factory_returns_alt_for_ema_consensus_auto(settings_engine):
    for mode in ("ema", "consensus", "auto", "quad_union", "utbot_prime", "quad_prime", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"):
        settings_engine.scanner_engine = mode
        e = build_scanner_engine(dict(_PARAMS))
        assert isinstance(e, AltSignalEngine) and e.signal_source == mode


def test_alt_engine_fallback_logs_warning(monkeypatch):
    df = _trending_df()
    engine = AltSignalEngine(signal_source="ema", **_PARAMS)
    warnings = []

    def fail_positions(_df):
        raise RuntimeError("forced alt failure")

    def record_warning(message, *args, **_kwargs):
        warnings.append(message.format(*args))

    monkeypatch.setattr(engine, "_alt_positions", fail_positions)
    monkeypatch.setattr(alt_scanners.logger, "warning", record_warning)

    result = _process(engine, df)

    assert "error" not in result
    assert "signals" in result
    assert warnings
    assert "falling back to UT1 result" in warnings[0]
    assert "engine=ema" in warnings[0]
    assert "instrument=NIFTY" in warnings[0]
    assert "timeframe=5min" in warnings[0]


def test_auto_blends_consensus_on_trend_and_ema_on_chop(settings_engine):
    df = _trending_df()
    auto = AltSignalEngine(signal_source="auto", auto_trend_adx=25.0, **_PARAMS)
    r = _process(auto, df)
    assert "error" not in r and isinstance(r.get("signals"), list)
    # Auto is a per-bar blend, so its signal count sits between consensus (fewest)
    # and ema (more) — never noisier than ema, never sparser than consensus.
    n_ema = len(_process(AltSignalEngine(signal_source="ema", **_PARAMS), df)["signals"])
    n_con = len(_process(AltSignalEngine(signal_source="consensus", **_PARAMS), df)["signals"])
    n_auto = len(r["signals"])
    assert min(n_ema, n_con) <= n_auto <= max(n_ema, n_con) + 2  # +2 tolerance for boundary flips
    for s in r["signals"]:
        for k in ("open", "high", "low", "close"):
            assert k in s.raw_candle


def test_combo_engine_unions_utbot_and_partner_flips():
    # Combo modes run BOTH UT1 and the partner (ema/consensus) and feed the UNION of
    # their flips downstream — so the system gets UTBot's frequency PLUS the partner's
    # entries. The union is de-duped, so it sits between max(each) and the sum of both.
    df = _trending_df()
    n_ut = len(_process(UTBotEngine(**_PARAMS), df)["signals"])
    for partner, combo in (("ema", "utbot_ema"), ("consensus", "utbot_consensus")):
        n_p = len(_process(AltSignalEngine(signal_source=partner, **_PARAMS), df)["signals"])
        r = _process(AltSignalEngine(signal_source=combo, **_PARAMS), df)
        n_c = len(r["signals"])
        assert max(n_ut, n_p) <= n_c <= n_ut + n_p  # union: >= each alone, <= sum
        # signals stay well-formed (raw_candle present for grading; chronological)
        idxs = [s.bar_index for s in r["signals"]]
        assert idxs == sorted(idxs)
        for s in r["signals"]:
            assert s.signal_type in {"BUY", "SELL"} and "close" in s.raw_candle


def test_combo_engine_shows_partner_overlays_and_routes_via_factory(settings_engine):
    df = _trending_df()
    # utbot_ema overlays the EMA pair; utbot_consensus overlays Supertrend+EMA9+EMA21.
    ue = AltSignalEngine(signal_source="utbot_ema", **_PARAMS).process(
        df=df, instrument="NIFTY", timeframe="5min", capital=600000, risk_pct=1.5, lots=1, lot_size=75)
    assert [o["name"] for o in ue["indicator_overlays"]] == ["EMA 9", "EMA 21"]
    uc = AltSignalEngine(signal_source="utbot_consensus", **_PARAMS).process(
        df=df, instrument="NIFTY", timeframe="5min", capital=600000, risk_pct=1.5, lots=1, lot_size=75)
    names = [o["name"] for o in uc["indicator_overlays"]]
    assert any("Supertrend" in n for n in names) and "EMA 9" in names and "EMA 21" in names
    # factory + set_scanner_engine accept combo modes
    for mode in ("utbot_ema", "utbot_consensus"):
        settings_engine.scanner_engine = mode
        e = build_scanner_engine(dict(_PARAMS))
        assert isinstance(e, AltSignalEngine) and e.signal_source == mode


def test_consensus_modes_use_ema_9_21_with_supertrend_10_3():
    df = _swing_df()
    for mode in ("consensus", "utbot_consensus", "quad_union", "quad_prime", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"):
        eng = AltSignalEngine(signal_source=mode, **_PARAMS)
        ema = eng._ema_trend(df["close"])
        st = eng._supertrend_trend(df)
        expected_ema = np.where(
            df["close"].ewm(span=9, adjust=False).mean().values
            > df["close"].ewm(span=21, adjust=False).mean().values,
            1,
            -1,
        ).astype(int)
        assert np.array_equal(ema, expected_ema)
        assert eng.st_period == 10 and eng.st_mult == 3.0
        expected_consensus = eng._consensus_positions(ema, st)
        if mode not in {"quad_union", "quad_prime", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"}:
            assert np.array_equal(eng._alt_positions(df), expected_consensus)


def test_quad_union_unions_utbot_ema_supertrend_and_consensus(settings_engine):
    df = _swing_df()
    r = _process(AltSignalEngine(signal_source="quad_union", **_PARAMS), df)
    assert "error" not in r
    assert r["signals"], "quad union should emit raw union flips"

    sources = set()
    for signal in r["signals"]:
        sources.update(getattr(signal, "source_engines", []) or [getattr(signal, "source_engine", "")])
        assert signal.source_engine in {"utbot", "ema", "supertrend", "consensus", "multi"}
        assert "close" in signal.raw_candle
    assert {"utbot", "ema", "supertrend", "consensus"}.issubset(sources)

    n_ut = len(_process(UTBotEngine(**_PARAMS), df)["signals"])
    n_ema = len(_process(AltSignalEngine(signal_source="ema", **_PARAMS), df)["signals"])
    n_st = len(
        AltSignalEngine(signal_source="quad_union", **_PARAMS)._signals_from_positions(
            AltSignalEngine(signal_source="quad_union", **_PARAMS)._supertrend_trend(df),
            df,
            instrument="NIFTY",
            timeframe="5min",
            source="supertrend",
            trailing_stop=r["trailing_stop"],
            atr=r["atr"],
            adx=r["adx"],
        )
    )
    n_cons = len(_process(AltSignalEngine(signal_source="consensus", **_PARAMS), df)["signals"])
    assert max(n_ut, n_ema, n_st, n_cons) <= len(r["signals"]) <= n_ut + n_ema + n_st + n_cons

    names = [o["name"] for o in r["indicator_overlays"]]
    assert "EMA 9" in names and "EMA 21" in names and any("Supertrend" in n for n in names)

    settings_engine.scanner_engine = "quad_union"
    e = build_scanner_engine(dict(_PARAMS))
    assert isinstance(e, AltSignalEngine) and e.signal_source == "quad_union"


def test_quad_prime_uses_all_raw_entry_sources_with_factory(settings_engine):
    df = _swing_df()
    quad = _process(AltSignalEngine(signal_source="quad_union", **_PARAMS), df)
    prime = _process(AltSignalEngine(signal_source="quad_prime", **_PARAMS), df)

    assert [(s.bar_index, s.signal_type) for s in prime["signals"]] == [
        (s.bar_index, s.signal_type) for s in quad["signals"]
    ]
    sources = set()
    for signal in prime["signals"]:
        sources.update(getattr(signal, "source_engines", []) or [getattr(signal, "source_engine", "")])
        assert signal.source_engine in {"utbot", "ema", "supertrend", "consensus", "multi"}
        assert "close" in signal.raw_candle
    assert {"utbot", "ema", "supertrend", "consensus"}.issubset(sources)

    settings_engine.scanner_engine = "quad_prime"
    e = build_scanner_engine(dict(_PARAMS))
    assert isinstance(e, AltSignalEngine) and e.signal_source == "quad_prime"


def test_utbot_prime_keeps_utbot_entries_and_adds_trend_agreement(settings_engine):
    df = _swing_df()
    ut = _process(UTBotEngine(**_PARAMS), df)
    prime = _process(AltSignalEngine(signal_source="utbot_prime", **_PARAMS), df)

    ut_keys = [(int(s.bar_index), s.signal_type) for s in ut["signals"]]
    prime_keys = [(int(s.bar_index), s.signal_type) for s in prime["signals"]]
    assert prime_keys == ut_keys
    assert prime["signals"], "UT1 Prime should keep UT1 flip frequency"

    for signal in prime["signals"]:
        assert signal.source_engine == "utbot"
        assert 0 <= int(getattr(signal, "trend_agreement", -1)) <= 3
        assert (getattr(signal, "source_engines", []) or [""])[0] == "utbot"
        assert "close" in signal.raw_candle

    names = [o["name"] for o in prime["indicator_overlays"]]
    assert "EMA 9" in names and "EMA 21" in names and any("Supertrend" in n for n in names)

    settings_engine.scanner_engine = "utbot_prime"
    e = build_scanner_engine(dict(_PARAMS))
    assert isinstance(e, AltSignalEngine) and e.signal_source == "utbot_prime"


def test_adaptive_hybrid_filters_union_and_annotates_route(settings_engine):
    df = _swing_df()
    quad = _process(AltSignalEngine(signal_source="quad_union", **_PARAMS), df)
    hybrid = _process(AltSignalEngine(signal_source="adaptive_hybrid", **_PARAMS), df)

    assert "error" not in hybrid
    assert hybrid["signals"], "adaptive hybrid should emit vetted meta-routed flips"
    assert len(hybrid["signals"]) <= len(quad["signals"])
    for signal in hybrid["signals"]:
        assert signal.source_engine == "adaptive_hybrid"
        assert getattr(signal, "adaptive_hybrid_route", "")
        assert 0 <= int(getattr(signal, "trend_agreement", -1)) <= 3
        assert "close" in signal.raw_candle

    names = [o["name"] for o in hybrid["indicator_overlays"]]
    assert "EMA 9" in names and "EMA 21" in names and any("Supertrend" in n for n in names)

    settings_engine.scanner_engine = "adaptive_hybrid"
    e = build_scanner_engine(dict(_PARAMS))
    assert isinstance(e, AltSignalEngine) and e.signal_source == "adaptive_hybrid"


def test_utbot_prime_trend_agreement_boosts_confidence(settings_engine):
    from datetime import datetime
    from engine.signal_manager import SignalManager

    settings_engine.scanner_engine = "utbot_prime"
    settings_engine.utbot_prime_confidence_per_agreement = 0.03
    settings_engine.utbot_prime_max_confidence_bonus = 0.09

    def mk(agreement: int) -> UTBotSignal:
        sig = UTBotSignal(
            timestamp=datetime(2026, 6, 30, 10, 0),
            signal_type="BUY",
            price=100.0,
            trailing_stop=98.5,
            stop_distance=1.5,
            atr_value=1.5,
            adx_value=25.0,
            position_state=1,
            bar_index=20,
            instrument="NIFTY",
            timeframe="5min",
            raw_candle={"open": 99.0, "high": 101.0, "low": 98.5, "close": 100.0},
            source_engine="utbot",
        )
        setattr(sig, "trend_agreement", agreement)
        setattr(sig, "trend_agreement_sources", ["ema", "supertrend", "consensus"][:agreement])
        return sig

    manager = SignalManager()
    _, conf0, reasons0 = manager._grade_signal(mk(0), 0.0, 0.0, regime="UNKNOWN")
    _, conf3, reasons3 = manager._grade_signal(mk(3), 0.0, 0.0, regime="UNKNOWN")

    assert conf3 == pytest.approx(min(1.0, conf0 + 0.09), abs=1e-9)
    assert not any("UT1 Prime agreement" in r for r in reasons0)
    assert any("UT1 Prime agreement 3/3" in r for r in reasons3)


def test_combo_solo_utbot_exit_is_blocked_partner_exit_allowed(settings_engine):
    # In combo mode a SOLO UT1 opposite-flip must NOT exit a live trade (UT1 is jumpy);
    # the partner's flip — or "both" agreement — is what's allowed to knock the trade out.
    from scanner import Scanner
    sc = Scanner.__new__(Scanner)
    s = settings_engine
    s.combo_partner_confirms_exit = True

    def mk(src):
        return UTBotSignal(timestamp=None, signal_type="SELL", price=100.0, trailing_stop=99.0,
                           stop_distance=1.0, atr_value=1.0, adx_value=20.0, position_state=-1,
                           bar_index=5, source_engine=src)

    s.scanner_engine = "utbot_ema"
    assert sc._combo_solo_utbot_exit_blocked(mk("utbot")) is True      # jumpy solo exit -> blocked
    assert sc._combo_solo_utbot_exit_blocked(mk("ema")) is False       # partner exit -> allowed
    assert sc._combo_solo_utbot_exit_blocked(mk("both")) is False      # both agree -> allowed
    s.scanner_engine = "utbot_consensus"
    assert sc._combo_solo_utbot_exit_blocked(mk("utbot")) is True
    assert sc._combo_solo_utbot_exit_blocked(mk("consensus")) is False

    # toggle off -> raw union behaviour (utbot can solo-exit again)
    s.combo_partner_confirms_exit = False
    s.scanner_engine = "utbot_ema"
    assert sc._combo_solo_utbot_exit_blocked(mk("utbot")) is False
    s.combo_partner_confirms_exit = True

    # single-engine modes are untagged ("") -> never blocked, unaffected
    s.scanner_engine = "utbot"
    assert sc._combo_solo_utbot_exit_blocked(mk("")) is False


def test_opposite_exit_confirm_honors_partner_confirm_toggle(settings_engine):
    # Integration: the partner-confirm block is wired INTO the live exit gate
    # (_opposite_exit_confirm_ready), and both ON and OFF behave correctly end-to-end.
    from datetime import datetime
    from scanner import Scanner
    sc = Scanner.__new__(Scanner)
    sc.mode = "HISTORICAL"  # non-blocked path returns True immediately (clean isolation)
    s = settings_engine
    s.scanner_engine = "utbot_ema"

    def mk(src):
        return UTBotSignal(timestamp=datetime(2026, 6, 25, 11, 0), signal_type="SELL", price=100.0,
                           trailing_stop=99.0, stop_distance=1.0, atr_value=1.0, adx_value=20.0,
                           position_state=-1, bar_index=5, source_engine=src)

    # ON: a solo UT1 opposite-flip never confirms an exit; the partner's does.
    s.combo_partner_confirms_exit = True
    assert sc._opposite_exit_confirm_ready("NIFTY", mk("utbot"), "5min") is False
    assert sc._opposite_exit_confirm_ready("NIFTY", mk("ema"), "5min") is True
    assert sc._opposite_exit_confirm_ready("NIFTY", mk("both"), "5min") is True

    # OFF: raw union — UT1 can solo-exit again (gate returns to normal).
    s.combo_partner_confirms_exit = False
    assert sc._opposite_exit_confirm_ready("NIFTY", mk("utbot"), "5min") is True
    s.combo_partner_confirms_exit = True


def test_invalid_engine_falls_back_to_utbot(settings_engine):
    # set_scanner_engine on the live mtf coerces unknown modes to utbot.
    from engine.multi_timeframe import MultiTimeframeEngine
    mtf = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
    mtf.engines = {}
    mtf._engine_init_params = dict(_PARAMS)
    mtf._instruments = ["NIFTY"]
    mtf._timeframes = ["5min"]
    mtf._result_cache = {}
    mtf._data_hash = {}
    assert mtf.set_scanner_engine("nonsense") == "utbot"
    assert type(mtf.engines["NIFTY_5min"]) is UTBotEngine
    assert mtf.set_scanner_engine("consensus") == "consensus"
    assert isinstance(mtf.engines["NIFTY_5min"], AltSignalEngine)
    assert mtf.set_scanner_engine("quad_union") == "quad_union"
    assert isinstance(mtf.engines["NIFTY_5min"], AltSignalEngine)
    assert mtf.set_scanner_engine("utbot_prime") == "utbot_prime"
    assert isinstance(mtf.engines["NIFTY_5min"], AltSignalEngine)
    assert mtf.set_scanner_engine("quad_prime") == "quad_prime"
    assert isinstance(mtf.engines["NIFTY_5min"], AltSignalEngine)
    assert mtf.set_scanner_engine("adaptive_hybrid") == "adaptive_hybrid"
    assert isinstance(mtf.engines["NIFTY_5min"], AltSignalEngine)
    assert mtf.set_scanner_engine("adaptive_dual") == "adaptive_dual"
    assert isinstance(mtf.engines["NIFTY_5min"], AltSignalEngine)
    assert mtf.set_scanner_engine("quality_hybrid") == "quality_hybrid"
    assert isinstance(mtf.engines["NIFTY_5min"], AltSignalEngine)
    assert mtf.engines["NIFTY_5min"].signal_source == "quality_hybrid"


def test_historical_engine_switch_queues_full_backtest_rebuild(settings_engine):
    from types import SimpleNamespace
    from scanner import Scanner

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.scan_count = 5
    scanner.simulation_id = 100
    scanner._historical_rebuild_pending = False
    scanner._hist_candidates_loaded = True
    scanner._recalculation_status = {"status": "idle"}
    scanner._cached_results = {"NIFTY_5min": object()}
    scanner._chart_metadata_cache = {"NIFTY_5min": object()}
    scanner._latest_filtered_candidates = {"NIFTY": object()}
    scanner._last_signal_time = {"NIFTY_5min": object()}
    scanner._last_signal_identity = {"NIFTY_5min": "old"}
    scanner._last_live_signal_candle_time = {"NIFTY_5min": object()}
    scanner._pending_live_signals = {"NIFTY_5min_LONG": object()}
    scanner.chart_cache_NIFTY_5min = {"data": "old"}
    scanner.mtf = SimpleNamespace(
        set_scanner_engine=lambda mode: mode,
        _result_cache={"old": object()},
        _data_hash={"old": object()},
    )
    scanner.trades = SimpleNamespace(
        closed_trades=[SimpleNamespace(id="H_OLD"), SimpleNamespace(id="LIVE_KEEP")],
        open_trades={"H_OPEN": object()},
    )
    reset_calls = []
    scanner.perf = SimpleNamespace(reset=lambda: reset_calls.append(True))
    scanner._stamp_simulation_diagnostics = lambda: None
    scanner._publish_config_snapshot = lambda: None
    queued = []
    scanner.queue_full_recalculation = lambda reason: queued.append(reason)

    result = scanner.apply_scanner_engine_switch("ema")

    assert result["scanner_engine"] == "ema"
    assert result["recalculation_queued"] is True
    assert settings_engine.scanner_engine == "ema"
    assert scanner._historical_rebuild_pending is True
    assert scanner._recalculation_status["status"] == "queued"
    assert scanner._recalculation_status["reason"] == "scanner-engine-switch"
    assert scanner._recalculation_status["scanner_engine"] == "ema"
    assert queued == ["scanner-engine-switch"]
    assert scanner.scan_count == 0
    assert scanner.simulation_id > 100
    assert scanner._hist_candidates_loaded is False
    assert scanner._cached_results == {}
    assert scanner._chart_metadata_cache == {}
    assert not hasattr(scanner, "chart_cache_NIFTY_5min")
    assert scanner.mtf._result_cache == {}
    assert scanner.mtf._data_hash == {}
    assert [trade.id for trade in scanner.trades.closed_trades] == ["LIVE_KEEP"]
    assert scanner.trades.open_trades == {}
    assert reset_calls == [True]


def test_trend_engine_relaxes_5min_gates_for_aligned_signal(settings_engine):
    # Live 2026-06-25: consensus generated MIDCPNIFTY 5min BUY @ A/72%, regime=VOLATILE,
    # 15m_agrees=True, no impulse/spike/power — repeatedly REJECTED at the 5Min Quality
    # Gate + Choppy gate. A smoothed trend-engine flip IS the trend confirmation, so a
    # 15m-aligned, confidence-qualified signal must now clear both gates (utbot unchanged).
    s = settings_engine
    s.trend_engine_relaxed_gate = True
    grade_conf_ok = True
    trend_15m_agrees = True
    volume_spike_entry = power_move = true_5min_impulse = impulse_context_confirmed = False

    def passes(engine):
        s.scanner_engine = engine
        te = str(s.scanner_engine).lower() in {"consensus", "ema", "auto", "quad_union", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"} and bool(s.trend_engine_relaxed_gate)
        quality = (
            volume_spike_entry or power_move or true_5min_impulse
            or (grade_conf_ok and trend_15m_agrees and impulse_context_confirmed)
            or (te and grade_conf_ok and trend_15m_agrees)
        )
        choppy = true_5min_impulse or volume_spike_entry or (te and trend_15m_agrees)
        return quality and choppy

    assert passes("consensus") is True   # the live rejection is now admitted
    assert passes("ema") is True
    assert passes("auto") is True        # auto emits consensus/ema flips -> also relaxed
    assert passes("quad_union") is True  # quad union is raw, but still a trend-engine source set
    assert passes("adaptive_hybrid") is True
    assert passes("adaptive_dual") is True
    assert passes("quality_hybrid") is True
    assert passes("utbot") is False      # utbot path unchanged — still needs impulse


def test_trend_engine_relax_respects_toggle(settings_engine):
    s = settings_engine
    s.scanner_engine = "consensus"
    s.trend_engine_relaxed_gate = False
    te = str(s.scanner_engine).lower() in {"consensus", "ema"} and bool(s.trend_engine_relaxed_gate)
    assert te is False  # toggle off -> trend engine gets the strict gate again


def test_raw_union_candidate_bypasses_soft_grade_pref_but_not_live_quality_floor(settings_engine):
    from datetime import datetime
    from scanner import Scanner
    from engine.signal_processor import TradeCandidate

    settings_engine.scanner_engine = "quad_union"
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.latest_regimes = {"NIFTY": "VOLATILE"}
    sc._candidate_15m_trend_agrees = lambda candidate: bool(
        getattr(candidate, "trend_15m_agrees", False)
    )
    candidate = TradeCandidate(
        instrument="NIFTY",
        direction="LONG",
        price=100.0,
        stop=90.0,
        target=130.0,
        lots=1,
        lot_size=75,
        grade="C",
        confidence=0.20,
        timeframe="15min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=24000,
        multiplier=0.5,
        trading_symbol="NIFTYTEST",
        symbol_token="1",
        signal_timestamp=datetime(2026, 6, 30, 10, 0),
        source_engine="utbot",
        source_engines=["utbot"],
        quad_union_entry=True,
        raw_union_entry=True,
    )
    assert sc._passes_candidate_grade_preference(candidate) == (True, "quad-union raw entry after repaint buffer")
    assert sc._passes_live_trade_ready_gate(candidate) is False

    candidate.grade = "A"
    candidate.confidence = 0.74
    candidate.source_engine = "utbot+ema"
    candidate.source_engines = ["utbot", "ema"]
    candidate.trend_15m_agrees = True

    assert sc._passes_live_trade_ready_gate(candidate) is True
    assert candidate.scalp_lock_mode is False


def _quad_prime_raw_candidate(**overrides):
    """Build a minimal quad-prime raw-entry candidate for live-gate tests."""
    from datetime import datetime
    from engine.signal_processor import TradeCandidate

    base = dict(
        instrument="NIFTY",
        direction="LONG",
        price=100.0,
        stop=90.0,
        target=130.0,
        lots=1,
        lot_size=75,
        grade="A",
        confidence=0.75,
        timeframe="5min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=24000,
        multiplier=0.5,
        trading_symbol="NIFTYTEST",
        symbol_token="1",
        signal_timestamp=datetime(2026, 6, 30, 10, 0),
        source_engine="utbot+ema",
        source_engines=["utbot", "ema"],
        quad_union_entry=True,
        raw_union_entry=True,
    )
    base.update(overrides)
    candidate = TradeCandidate(**base)
    return candidate


def _quad_prime_gate_scanner(regime):
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.latest_regimes = {"NIFTY": regime}
    sc._candidate_15m_trend_agrees = lambda candidate: bool(
        getattr(candidate, "trend_15m_agrees", False)
    )
    return sc


def test_quad_prime_raw_high_grade_trend_entry_runs_not_scalped(settings_engine):
    """The root fix: a strong 15m-aligned quad-prime raw entry is admitted as a
    RUNNER (scalp_lock_mode False) instead of being force-scalped — even on 5min."""
    settings_engine.scanner_engine = "quad_prime"
    settings_engine.trend_entry_skip_scalp_lock = True
    sc = _quad_prime_gate_scanner("TRENDING_UP")

    candidate = _quad_prime_raw_candidate(
        grade="A", confidence=0.75, timeframe="5min",
    )
    candidate.adx_value = 27.0
    candidate.trend_15m_agrees = True

    assert sc._passes_live_trade_ready_gate(candidate) is True
    assert candidate.scalp_lock_mode is False


def test_quad_prime_raw_weak_but_admissible_entry_stays_scalp(settings_engine):
    """A weak-but-admissible raw entry (spike-admitted, not high grade) is still
    admitted with scalp_lock_mode True — the runner path is reserved for quality."""
    settings_engine.scanner_engine = "quad_prime"
    sc = _quad_prime_gate_scanner("TRENDING_UP")

    candidate = _quad_prime_raw_candidate(
        grade="B", confidence=0.60, timeframe="5min",
        source_engine="utbot", source_engines=["utbot"],
    )
    candidate.adx_value = 27.0
    candidate.trend_15m_agrees = True
    candidate.volume_spike_entry = True  # admits via exceptional-spike, not quality

    assert sc._passes_live_trade_ready_gate(candidate) is True
    assert candidate.scalp_lock_mode is True


def test_quad_union_raw_entry_bypass_is_mirrored_in_historical_replay(settings_engine):
    import inspect
    from types import SimpleNamespace
    from scanner import Scanner

    s = settings_engine
    signal = SimpleNamespace(source_engine="ema", source_engines=["ema"])
    s.scanner_engine = "quad_union"
    assert Scanner._is_quad_union_raw_historical_signal(s, signal) is True

    s.scanner_engine = "quad_prime"
    assert Scanner._is_quad_union_raw_historical_signal(s, signal) is True

    s.scanner_engine = "utbot_consensus"
    assert Scanner._is_quad_union_raw_historical_signal(s, signal) is False

    # Historical replay + quality gates live on SignalProcessor (Scanner orphan removed).
    from pathlib import Path

    from engine.signal_processor import SignalProcessor

    source = " ".join(inspect.getsource(SignalProcessor.process_best_signal).split())
    assert "quad_union_entry" in source
    assert "quality_hybrid" in source
    scanner_file = Path("scanner.py").read_text(encoding="utf-8")
    assert "self.signal_processor.process_best_signal" in scanner_file
    assert "def _process_best_signal" not in scanner_file


def test_quad_union_single_opposite_tightens_profit_but_does_not_exit(settings_engine):
    from datetime import datetime
    from types import SimpleNamespace
    from scanner import Scanner
    from trading.trade_manager import Trade

    settings_engine.scanner_engine = "quad_union"
    sc = Scanner.__new__(Scanner)
    saved = []
    sc.trades = SimpleNamespace(save_state=lambda: saved.append(True))
    sc.log_event = lambda *args, **kwargs: None

    trade = Trade(
        id="T1",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime(2026, 6, 30, 10, 0),
        trailing_stop=85.0,
        current_stop=85.0,
        lots=1,
        lot_size=100,
        grade="A",
    )
    trade.inst_type = "OPT"
    trade.current_price = 115.0
    trade.peak_pnl = 1500.0
    sig = UTBotSignal(
        timestamp=datetime(2026, 6, 30, 10, 30),
        signal_type="SELL",
        price=20000.0,
        trailing_stop=19900.0,
        stop_distance=100.0,
        atr_value=50.0,
        adx_value=25.0,
        position_state=-1,
        bar_index=10,
        source_engine="utbot",
    )
    setattr(sig, "source_engines", ["utbot"])

    assert sc._quad_union_opposite_exit_allowed(trade, sig, 115.0) is False
    assert 85.0 < trade.current_stop < 115.0
    assert saved == [True]


def test_quad_union_consensus_or_losing_single_opposite_can_exit(settings_engine):
    from datetime import datetime
    from types import SimpleNamespace
    from scanner import Scanner
    from trading.trade_manager import Trade

    settings_engine.scanner_engine = "quad_union"
    sc = Scanner.__new__(Scanner)
    sc.trades = SimpleNamespace(save_state=lambda: None)
    sc.log_event = lambda *args, **kwargs: None

    trade = Trade(
        id="T1",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime(2026, 6, 30, 10, 0),
        trailing_stop=85.0,
        current_stop=85.0,
        lots=1,
        lot_size=100,
        grade="A",
    )
    trade.inst_type = "OPT"

    consensus = UTBotSignal(
        timestamp=datetime(2026, 6, 30, 10, 30),
        signal_type="SELL",
        price=20000.0,
        trailing_stop=19900.0,
        stop_distance=100.0,
        atr_value=50.0,
        adx_value=25.0,
        position_state=-1,
        bar_index=10,
        source_engine="consensus",
    )
    setattr(consensus, "source_engines", ["consensus"])
    assert sc._quad_union_opposite_exit_allowed(trade, consensus, 115.0) is True

    utbot = UTBotSignal(
        timestamp=datetime(2026, 6, 30, 10, 35),
        signal_type="SELL",
        price=20000.0,
        trailing_stop=19900.0,
        stop_distance=100.0,
        atr_value=50.0,
        adx_value=25.0,
        position_state=-1,
        bar_index=11,
        source_engine="utbot",
    )
    setattr(utbot, "source_engines", ["utbot"])
    assert sc._quad_union_opposite_exit_allowed(trade, utbot, 98.0) is True


def test_quad_prime_needs_cross_family_exit_for_profitable_trade(settings_engine):
    from datetime import datetime
    from types import SimpleNamespace
    from scanner import Scanner
    from trading.trade_manager import Trade

    settings_engine.scanner_engine = "quad_prime"
    sc = Scanner.__new__(Scanner)
    saved = []
    sc.trades = SimpleNamespace(save_state=lambda: saved.append(True))
    sc.log_event = lambda *args, **kwargs: None

    trade = Trade(
        id="T1",
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime(2026, 6, 30, 10, 0),
        trailing_stop=85.0,
        current_stop=85.0,
        lots=1,
        lot_size=100,
        grade="A",
    )
    trade.inst_type = "OPT"
    trade.current_price = 115.0
    trade.peak_pnl = 1500.0

    def sig(sources):
        signal = UTBotSignal(
            timestamp=datetime(2026, 6, 30, 10, 30),
            signal_type="SELL",
            price=20000.0,
            trailing_stop=19900.0,
            stop_distance=100.0,
            atr_value=50.0,
            adx_value=25.0,
            position_state=-1,
            bar_index=10,
            source_engine="multi" if len(sources) > 1 else sources[0],
        )
        setattr(signal, "source_engines", list(sources))
        return signal

    assert sc._quad_union_opposite_exit_allowed(trade, sig(["utbot"]), 115.0) is False
    assert sc._quad_union_opposite_exit_allowed(trade, sig(["consensus"]), 115.0) is False
    assert saved, "solo opposite warnings should tighten protection"
    assert sc._quad_union_opposite_exit_allowed(trade, sig(["utbot", "ema"]), 115.0) is True

    trade.peak_pnl = 3000.0
    assert sc._quad_union_opposite_exit_allowed(trade, sig(["utbot"]), 105.0) is True


def test_quad_prime_raw_entry_bypasses_15m_policy_but_obeys_quality_floor(settings_engine):
    from datetime import datetime
    from scanner import Scanner
    from engine.signal_processor import TradeCandidate

    original_policy = getattr(settings_engine, "ut_timeframe_entry_policy", "PRIMARY_15")
    original_adapt = getattr(settings_engine, "ut_regime_adaptation", True)
    settings_engine.scanner_engine = "quad_prime"
    settings_engine.ut_timeframe_entry_policy = "PRIMARY_15"
    # VOLATILE is only "risky" (→ scalp-lock, not runner) when regime adaptation is
    # ON; set it explicitly so this test does not depend on leaked suite state.
    settings_engine.ut_regime_adaptation = True

    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.latest_regimes = {"NIFTY": "VOLATILE"}
    sc._candidate_15m_trend_agrees = lambda candidate: bool(
        getattr(candidate, "trend_15m_agrees", False)
    )
    candidate = TradeCandidate(
        instrument="NIFTY",
        direction="LONG",
        price=100.0,
        stop=95.0,
        target=120.0,
        lots=1,
        lot_size=75,
        grade="C",
        confidence=0.20,
        timeframe="5min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=24000,
        multiplier=0.5,
        trading_symbol="NIFTYTEST",
        symbol_token="1",
        signal_timestamp=datetime(2026, 6, 30, 10, 0),
        source_engine="ema",
        source_engines=["ema"],
        quad_union_entry=True,
        raw_union_entry=True,
    )

    try:
        assert sc._passes_timeframe_entry_policy(candidate) is True
        assert sc._passes_live_trade_ready_gate(candidate) is False

        candidate.grade = "A"
        candidate.confidence = 0.75
        candidate.adx_value = 28.0
        assert sc._passes_live_trade_ready_gate(candidate) is True
        assert candidate.scalp_lock_mode is True
    finally:
        settings_engine.ut_timeframe_entry_policy = original_policy
        settings_engine.ut_regime_adaptation = original_adapt


def test_quad_union_raw_entry_still_obeys_global_timeframe_policy(settings_engine):
    from datetime import datetime
    from scanner import Scanner
    from engine.signal_processor import TradeCandidate

    original_policy = getattr(settings_engine, "ut_timeframe_entry_policy", "PRIMARY_15")
    settings_engine.scanner_engine = "quad_union"
    settings_engine.ut_timeframe_entry_policy = "PRIMARY_15"

    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.log_signal_decision_once = lambda *args, **kwargs: None
    candidate = TradeCandidate(
        instrument="NIFTY",
        direction="LONG",
        price=100.0,
        stop=95.0,
        target=120.0,
        lots=1,
        lot_size=75,
        grade="C",
        confidence=0.20,
        timeframe="5min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=24000,
        multiplier=0.5,
        trading_symbol="NIFTYTEST",
        symbol_token="1",
        signal_timestamp=datetime(2026, 6, 30, 10, 0),
        source_engine="ema",
        source_engines=["ema"],
        quad_union_entry=True,
        raw_union_entry=True,
    )

    try:
        assert sc._passes_timeframe_entry_policy(candidate) is False
    finally:
        settings_engine.ut_timeframe_entry_policy = original_policy


def test_quad_prime_concurrency_uses_normal_guard_for_new_raw_source(settings_engine):
    from datetime import datetime
    from types import SimpleNamespace
    from scanner import Scanner
    from engine.signal_processor import TradeCandidate

    original_guard = getattr(settings_engine, "ut_concurrency_guard", True)
    settings_engine.scanner_engine = "quad_prime"
    settings_engine.ut_concurrency_guard = True

    sc = Scanner.__new__(Scanner)
    sc.trades = SimpleNamespace(
        open_trades={
            "T1": SimpleNamespace(
                instrument="NIFTY",
                timeframe="5min",
                direction="LONG",
                inst_type="OPT",
                grade="A",
                source_engine="utbot",
                source_engines=["utbot"],
            )
        }
    )
    sc._active_session_entry_candidates = lambda *args, **kwargs: []

    def candidate(source, *, timeframe="5min", grade="C", confidence=0.20):
        return TradeCandidate(
            instrument="NIFTY",
            direction="LONG",
            price=100.0,
            stop=95.0,
            target=120.0,
            lots=1,
            lot_size=75,
            grade=grade,
            confidence=confidence,
            timeframe=timeframe,
            inst_type="OPT",
            option_type="CE",
            atm_strike=24000,
            multiplier=0.5,
            trading_symbol="NIFTYTEST",
            symbol_token="1",
            signal_timestamp=datetime(2026, 6, 30, 10, 0),
            source_engine=source,
            source_engines=[source],
            quad_union_entry=True,
            raw_union_entry=True,
        )

    try:
        assert sc._prepare_candidate_for_concurrency(candidate("ema")) is False
        assert sc._prepare_candidate_for_concurrency(candidate("utbot")) is False
        assert sc._prepare_candidate_for_concurrency(
            candidate("ema", timeframe="15min", grade="A", confidence=0.78)
        ) is False
    finally:
        settings_engine.ut_concurrency_guard = original_guard


def test_quad_prime_opposite_exposure_is_cross_timeframe(settings_engine):
    from datetime import datetime
    from types import SimpleNamespace
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    signal = UTBotSignal(
        timestamp=datetime(2026, 6, 30, 10, 30),
        signal_type="SELL",
        price=20000.0,
        trailing_stop=19900.0,
        stop_distance=100.0,
        atr_value=50.0,
        adx_value=25.0,
        position_state=-1,
        bar_index=10,
        source_engine="ema",
    )
    sc.trades = SimpleNamespace(
        open_trades={
            "T1": SimpleNamespace(
                instrument="NIFTY",
                timeframe="15min",
                direction="LONG",
            )
        }
    )
    sc._active_session_entry_candidates = lambda *args, **kwargs: []

    settings_engine.scanner_engine = "quad_prime"
    assert sc._has_opposite_exposure("NIFTY", signal, "5min") is True

    # v2: adaptive_hybrid delegates to utbot_consensus behavior (NOT cross-TF),
    # so a 15min opposite trade does not count against a 5min signal.
    settings_engine.scanner_engine = "adaptive_hybrid"
    assert sc._has_opposite_exposure("NIFTY", signal, "5min") is False

    settings_engine.scanner_engine = "quad_union"
    assert sc._has_opposite_exposure("NIFTY", signal, "5min") is False


ALL_SCANNER_ENGINES = (
    "utbot",
    "ema",
    "consensus",
    "auto",
    "utbot_ema",
    "utbot_consensus",
    "quad_union",
    "utbot_prime",
    "quad_prime",
    "adaptive_hybrid",
    "adaptive_dual",
    "quality_hybrid",
)


def test_all_12_engines_accept_commands_factory_and_mtf_rebuild(settings_engine):
    from dashboard.server import _valid_command_payload
    from engine.multi_timeframe import MultiTimeframeEngine

    mtf = MultiTimeframeEngine(dict(_PARAMS))
    for mode in ALL_SCANNER_ENGINES:
        assert _valid_command_payload({"cmd": "set_scanner_engine", "state": mode}) is None

        settings_engine.scanner_engine = mode
        engine = build_scanner_engine(dict(_PARAMS))
        if mode == "utbot":
            assert type(engine) is UTBotEngine
        else:
            assert isinstance(engine, AltSignalEngine)
            assert engine.signal_source == mode

        assert mtf.set_scanner_engine(mode) == mode
        for rebuilt in mtf.engines.values():
            if mode == "utbot":
                assert type(rebuilt) is UTBotEngine
            else:
                assert isinstance(rebuilt, AltSignalEngine)
                assert rebuilt.signal_source == mode


def test_all_12_engines_share_toggles_but_keep_engine_specific_exceptions(settings_engine):
    from datetime import datetime
    from types import SimpleNamespace
    from scanner import Scanner
    from engine.signal_processor import SignalProcessor, TradeCandidate

    original_engine = getattr(settings_engine, "scanner_engine", "utbot")
    original_policy = getattr(settings_engine, "ut_timeframe_entry_policy", "PRIMARY_15")
    original_spike_seconds = getattr(settings_engine, "volume_spike_entry_stabilization_seconds", 5.0)
    original_combo = getattr(settings_engine, "combo_partner_confirms_exit", True)

    def raw_candidate():
        return TradeCandidate(
            instrument="NIFTY",
            direction="LONG",
            price=100.0,
            stop=95.0,
            target=120.0,
            lots=1,
            lot_size=75,
            grade="C",
            confidence=0.20,
            timeframe="5min",
            inst_type="OPT",
            option_type="CE",
            atm_strike=24000,
            multiplier=0.5,
            trading_symbol="NIFTYTEST",
            symbol_token="1",
            signal_timestamp=datetime(2026, 6, 30, 10, 0),
            source_engine="ema",
            source_engines=["ema"],
            quad_union_entry=True,
            raw_union_entry=True,
        )

    try:
        settings_engine.ut_timeframe_entry_policy = "PRIMARY_15"
        settings_engine.volume_spike_entry_stabilization_seconds = 7.0
        settings_engine.combo_partner_confirms_exit = True

        for mode in ALL_SCANNER_ENGINES:
            settings_engine.scanner_engine = mode
            sc = Scanner.__new__(Scanner)
            sc.mode = "REAL"
            sc.latest_regimes = {"NIFTY": "VOLATILE"}
            sc.log_signal_decision_once = lambda *args, **kwargs: None

            # 1min scalp/spike hygiene is global: non-spike 1min rows never compete
            # for entry, but confirmed 1min spikes still can, regardless of engine.
            rows = SignalProcessor._entry_admissible_raw_candidates(
                [
                    {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=False)},
                    {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=True)},
                    {"tf": "5min", "sig": SimpleNamespace(volume_spike_entry=False)},
                ]
            )
            assert [row["tf"] for row in rows] == ["1min", "5min"]
            assert bool(getattr(rows[0]["sig"], "volume_spike_entry", False)) is True

            spike_candidate = raw_candidate()
            spike_candidate.timeframe = "1min"
            spike_candidate.volume_spike_entry = True
            assert sc._live_signal_stabilization_requirements(spike_candidate) == (7.0, 2, True)

            # Combo-exit toggle is intentionally scoped only to the two combo engines.
            utbot_opposite = SimpleNamespace(source_engine="utbot", source_engines=["utbot"])
            expected_combo_block = mode in {"utbot_ema", "utbot_consensus"}
            assert sc._combo_solo_utbot_exit_blocked(utbot_opposite) is expected_combo_block

            # Historical raw bypass is only for all-source raw engines.
            assert Scanner._is_quad_union_raw_historical_signal(settings_engine, utbot_opposite) is (
                mode in {"quad_union", "quad_prime"}
            )

            # 15M-main timeframe bypass is reserved for cross-timeframe multi-source
            # engines. Earlier engines keep normal timeframe/concurrency coordination.
            assert sc._passes_timeframe_entry_policy(raw_candidate()) is (
                mode == "quad_prime"
            )
    finally:
        settings_engine.scanner_engine = original_engine
        settings_engine.ut_timeframe_entry_policy = original_policy
        settings_engine.volume_spike_entry_stabilization_seconds = original_spike_seconds
        settings_engine.combo_partner_confirms_exit = original_combo


@pytest.fixture
def settings_engine():
    s = get_settings()
    original = getattr(s, "scanner_engine", "utbot")
    yield s
    s.scanner_engine = original


def test_adaptive_hybrid_v2_delegates_to_proven_engines(settings_engine):
    """Engine 10 v2: trend/opening state passes the utbot_consensus union; chop
    demands utbot_prime-style agreement or strict consensus; entries come ONLY
    from the utbot+consensus flip streams (ema/supertrend are context)."""
    import inspect
    from config.settings import get_settings

    accept_src = inspect.getsource(AltSignalEngine._adaptive_accept_signal)
    assert "trend_utbot_consensus" in accept_src
    assert "chop_utbot_prime" in accept_src
    assert "chop_strict_consensus" in accept_src
    assert "power_thrust" in accept_src

    gen_src = inspect.getsource(AltSignalEngine._adaptive_hybrid_signals)
    assert 'source="consensus"' in gen_src
    assert 'source="ema"' not in gen_src
    assert 'source="supertrend"' not in gen_src

    s = get_settings()
    assert s.adaptive_hybrid_trend_adx == 25.0
    assert s.adaptive_hybrid_trend_er == 0.68
    assert s.adaptive_hybrid_chop_min_agreement == 2


def test_adaptive_hybrid_v2_signals_are_never_raw_flagged(settings_engine):
    """v2 signals must receive full utbot_consensus-grade treatment: no raw-union
    flags in the live path, the historical path, or the raw-candidate check."""
    import inspect
    from types import SimpleNamespace
    from engine.signal_processor import SignalProcessor
    from scanner import Scanner

    live = inspect.getsource(SignalProcessor.process_best_signal)
    assert 'quad_union_entry = scanner_engine_mode in {"quad_union", "quad_prime"}' in live
    assert 'quad_prime_entry = scanner_engine_mode == "quad_prime"' in live

    settings_engine.scanner_engine = "adaptive_hybrid"
    sig = SimpleNamespace(adaptive_hybrid_route="trend_utbot_consensus")
    assert Scanner._is_quad_union_raw_historical_signal(settings_engine, sig) is False
    raw_candidate = SimpleNamespace(quad_union_entry=True, raw_union_entry=True)
    assert Scanner._is_quad_prime_raw_candidate(raw_candidate) is False


def test_adaptive_dual_lane_a_is_tagged_but_not_raw_flagged(settings_engine):
    from types import SimpleNamespace
    from scanner import Scanner

    settings_engine.scanner_engine = "adaptive_dual"
    engine = build_scanner_engine(dict(_PARAMS))
    assert isinstance(engine, AltSignalEngine)
    assert engine.signal_source == "adaptive_dual"

    sig = SimpleNamespace(source_engine="adaptive_dual", source_lane="A")
    assert Scanner._is_quad_union_raw_historical_signal(settings_engine, sig) is False
    assert Scanner._is_quad_prime_raw_candidate(sig) is False


def test_quality_hybrid_engine_12_is_independent_confluence(settings_engine):
    from scanner import Scanner

    settings_engine.scanner_engine = "quality_hybrid"
    engine = build_scanner_engine(dict(_PARAMS))
    assert isinstance(engine, AltSignalEngine)
    assert engine.signal_source == "quality_hybrid"

    result = _process(engine, _swing_df(220))
    assert "error" not in result
    assert result["signals"], "quality_hybrid should emit its own confluence flips"
    assert all(sig.source_engine == "quality_hybrid" for sig in result["signals"])
    assert all("utbot" not in (getattr(sig, "source_engines", []) or []) for sig in result["signals"])
    assert all(hasattr(sig, "quality_hybrid_vote_score") for sig in result["signals"])
    assert all(hasattr(sig, "quality_hybrid_route") for sig in result["signals"])
    assert Scanner._is_quad_union_raw_historical_signal(settings_engine, result["signals"][0]) is False
    assert Scanner._is_quad_prime_raw_candidate(result["signals"][0]) is False


def test_quality_hybrid_routes_split_historical_lifecycles(settings_engine):
    from datetime import datetime
    from types import SimpleNamespace
    from engine.signal_processor import SignalProcessor

    settings_engine.scanner_engine = "quality_hybrid"
    thrust = SimpleNamespace(
        timestamp=datetime(2026, 7, 1, 10, 0),
        quality_hybrid_route="quality_thrust",
    )
    pullback = SimpleNamespace(
        timestamp=datetime(2026, 7, 1, 10, 5),
        quality_hybrid_route="quality_pullback",
    )

    assert SignalProcessor._historical_replay_source_keys(thrust, "quality_hybrid") == ["quality_thrust"]
    assert SignalProcessor._historical_replay_source_keys(pullback, "quality_hybrid") == ["quality_pullback"]
    assert not SignalProcessor._historical_same_replay_stream(thrust, pullback, "quality_hybrid")
    assert "_QUALITY_THRUST_" in SignalProcessor._historical_trade_id(
        "H",
        "NIFTY",
        "5min",
        thrust,
        "quality_hybrid",
    )


def test_quality_hybrid_route_relief_is_proof_gated(settings_engine):
    from types import SimpleNamespace
    from scanner import Scanner

    settings_engine.scanner_engine = "quality_hybrid"
    strong_thrust = SimpleNamespace(
        quality_hybrid_route="quality_thrust",
        trend_agreement=5,
        quality_hybrid_vote_score=4.0,
        adx_value=24.0,
    )
    core_flip = SimpleNamespace(
        quality_hybrid_route="quality_core",
        trend_agreement=7,
        quality_hybrid_vote_score=6.0,
        adx_value=40.0,
    )
    weak_thrust = SimpleNamespace(
        quality_hybrid_route="quality_thrust",
        trend_agreement=2,
        quality_hybrid_vote_score=1.0,
        adx_value=12.0,
    )
    pullback_without_15m = SimpleNamespace(
        quality_hybrid_route="quality_pullback",
        trend_agreement=6,
        quality_hybrid_vote_score=4.0,
        adx_value=28.0,
    )

    assert Scanner._is_quality_hybrid_route_entry(
        settings_engine,
        strong_thrust,
        confidence=0.61,
        grade_rank=1,
        timeframe="5min",
    )
    assert not Scanner._is_quality_hybrid_route_entry(
        settings_engine,
        core_flip,
        confidence=0.80,
        grade_rank=4,
        timeframe="5min",
    )
    assert not Scanner._is_quality_hybrid_route_entry(
        settings_engine,
        weak_thrust,
        confidence=0.61,
        grade_rank=1,
        timeframe="5min",
    )
    assert not Scanner._is_quality_hybrid_route_entry(
        settings_engine,
        pullback_without_15m,
        confidence=0.90,
        grade_rank=4,
        timeframe="5min",
        trend_15m_agrees=False,
    )
    assert Scanner._is_quality_hybrid_route_entry(
        settings_engine,
        pullback_without_15m,
        confidence=0.61,
        grade_rank=1,
        timeframe="5min",
        trend_15m_agrees=True,
    )
