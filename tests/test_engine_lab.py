from datetime import datetime, timedelta
import inspect
from types import SimpleNamespace

import pandas as pd

from config.settings import get_settings
from engine.engine_lab import ENGINE_LAB_MODES, EngineLab
from engine.signal_processor import SignalProcessor
from engine.ut_bot_core import UTBotSignal
from scanner import Scanner


def _sample_candles(rows: int = 60) -> pd.DataFrame:
    start = datetime(2026, 7, 1, 9, 15)
    data = []
    for idx in range(rows):
        base = 24000 + idx * 3
        data.append(
            {
                "open": base,
                "high": base + 8,
                "low": base - 5,
                "close": base + (5 if idx % 3 else -2),
                "volume": 100000 + idx * 1000,
            }
        )
    return pd.DataFrame(data, index=[start + timedelta(minutes=5 * i) for i in range(rows)])


def _lane_b_spike_candles(rows: int = 36) -> pd.DataFrame:
    start = datetime(2026, 7, 1, 9, 15)
    data = []
    for idx in range(rows):
        base = 24000 + idx * 2
        volume = 1000
        if idx in {24, 30}:
            candle_range = 42
            low = base - 8
            high = low + candle_range
            close = high - 2
            volume = 5000
            open_price = low + 8
        else:
            open_price = base
            high = base + 5
            low = base - 5
            close = base + (2 if idx % 2 else -2)
        data.append(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    return pd.DataFrame(data, index=[start + timedelta(minutes=5 * i) for i in range(rows)])


def _apex_drive_candles(rows: int = 260) -> pd.DataFrame:
    data = []
    index = []
    price = 24000.0
    day = datetime(2026, 7, 1)
    while len(data) < rows:
        ts = day.replace(hour=9, minute=35, second=0, microsecond=0)
        while len(data) < rows and ts.time() <= day.replace(hour=14, minute=55).time():
            idx = len(data)
            step = 2.5 if idx < 180 else 5.0
            open_price = price
            close = price + step + (1.5 if idx % 5 else 0.0)
            high = max(open_price, close) + 1.5
            low = min(open_price, close) - 0.8
            volume = 100000 + idx * 200
            if idx >= 205:
                volume *= 3.5
                close = high - 0.1
            data.append(
                {
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
            index.append(ts)
            price = close
            ts += timedelta(minutes=5)
        day += timedelta(days=1)
    return pd.DataFrame(data, index=index)


def _to_15m(candles: pd.DataFrame) -> pd.DataFrame:
    return (
        candles.resample("15min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )


def test_apex_hybrid_adapter_emits_lane_b_signal():
    from engine.apex_hybrid import build_apex_signals

    candles = _apex_drive_candles()
    signals = build_apex_signals(
        candles,
        instrument="NIFTY",
        timeframe="5min",
        df_5m=candles,
        df_15m=_to_15m(candles),
    )

    assert signals
    assert all(sig.source_engine == "apex_hybrid" for sig in signals)
    assert all(sig.source_lane == "B" for sig in signals)
    assert all(getattr(sig, "apex_context", None) for sig in signals)


def test_adaptive_dual_historical_replay_keeps_lane_streams_separate():
    start = datetime(2026, 7, 1, 10, 0)
    lane_a_buy = UTBotSignal(
        timestamp=start,
        signal_type="BUY",
        price=100.0,
        trailing_stop=98.0,
        stop_distance=2.0,
        atr_value=2.0,
        adx_value=30.0,
        position_state=1,
        bar_index=1,
        instrument="NIFTY",
        timeframe="5min",
        source_engine="adaptive_dual",
        source_lane="A",
    )
    lane_a_sell = UTBotSignal(
        timestamp=start + timedelta(minutes=5),
        signal_type="SELL",
        price=99.0,
        trailing_stop=101.0,
        stop_distance=2.0,
        atr_value=2.0,
        adx_value=30.0,
        position_state=-1,
        bar_index=2,
        instrument="NIFTY",
        timeframe="5min",
        source_engine="adaptive_dual",
        source_lane="A",
    )
    lane_b_buy = UTBotSignal(
        timestamp=start + timedelta(minutes=1),
        signal_type="BUY",
        price=100.5,
        trailing_stop=98.5,
        stop_distance=2.0,
        atr_value=2.0,
        adx_value=30.0,
        position_state=1,
        bar_index=3,
        instrument="NIFTY",
        timeframe="5min",
        source_engine="apex_hybrid",
        source_lane="B",
    )
    lane_b_sell = UTBotSignal(
        timestamp=start + timedelta(minutes=6),
        signal_type="SELL",
        price=99.5,
        trailing_stop=101.5,
        stop_distance=2.0,
        atr_value=2.0,
        adx_value=30.0,
        position_state=-1,
        bar_index=4,
        instrument="NIFTY",
        timeframe="5min",
        source_engine="apex_hybrid",
        source_lane="B",
    )

    ordered = SignalProcessor._historical_replay_order(
        [lane_b_sell, lane_a_buy, lane_b_buy, lane_a_sell],
        "adaptive_dual",
    )

    assert [sig.source_lane for sig in ordered] == ["A", "A", "B", "B"]
    assert SignalProcessor._historical_same_replay_stream(ordered[0], ordered[1], "adaptive_dual") is True
    assert SignalProcessor._historical_same_replay_stream(ordered[1], ordered[2], "adaptive_dual") is False
    assert SignalProcessor._historical_trade_id("H", "NIFTY", "5min", ordered[0], "adaptive_dual") == "H_NIFTY_5min_A_20260701100000"
    assert SignalProcessor._historical_trade_id("H", "NIFTY", "5min", ordered[2], "adaptive_dual") == "H_NIFTY_5min_B_20260701100100"


def _replay_signal(ts, side, source_engine="", source_engines=None):
    sig = UTBotSignal(
        timestamp=ts,
        signal_type=side,
        price=100.0,
        trailing_stop=98.0 if side == "BUY" else 102.0,
        stop_distance=2.0,
        atr_value=2.0,
        adx_value=30.0,
        position_state=1 if side == "BUY" else -1,
        bar_index=int((ts.hour * 60) + ts.minute),
        instrument="NIFTY",
        timeframe="5min",
        source_engine=source_engine,
    )
    if source_engines is not None:
        setattr(sig, "source_engines", list(source_engines))
    return sig


def test_historical_replay_splits_true_composite_engines_by_source_family():
    start = datetime(2026, 7, 1, 10, 0)
    both_buy = _replay_signal(start, "BUY", "multi", ["utbot", "ema"])
    utbot_sell = _replay_signal(start + timedelta(minutes=5), "SELL", "utbot", ["utbot"])
    ema_sell = _replay_signal(start + timedelta(minutes=10), "SELL", "ema", ["ema"])

    streams = SignalProcessor._historical_replay_streams(
        [ema_sell, both_buy, utbot_sell],
        "quad_union",
    )

    assert [
        [SignalProcessor._historical_replay_stream_key(sig, "quad_union") for sig in stream]
        for stream in streams
    ] == [["utbot", "utbot"], ["ema", "ema"]]

    ordered = SignalProcessor._historical_replay_order(
        [ema_sell, both_buy, utbot_sell],
        "quad_union",
    )
    assert [sig.source_engine for sig in ordered] == ["utbot", "utbot", "ema", "ema"]
    assert SignalProcessor._historical_same_replay_stream(ordered[1], ordered[2], "quad_union") is False
    assert SignalProcessor._historical_trade_id("H", "NIFTY", "5min", ordered[0], "quad_union") == "H_NIFTY_5min_UTBOT_20260701100000"
    assert SignalProcessor._historical_trade_id("H", "NIFTY", "5min", ordered[2], "quad_union") == "H_NIFTY_5min_EMA_20260701100000"
    lab_entries = EngineLab._expanded_replay_entries(
        "quad_union",
        [("5min", ema_sell), ("5min", both_buy), ("5min", utbot_sell)],
    )
    assert [sig.source_engine for _, sig in lab_entries] == ["utbot", "utbot", "ema", "ema"]


def test_combo_replay_splits_utbot_partner_but_router_engines_stay_single_stream():
    start = datetime(2026, 7, 1, 10, 0)
    both_buy = _replay_signal(start, "BUY", "both", ["consensus", "utbot"])
    utbot_sell = _replay_signal(start + timedelta(minutes=5), "SELL", "utbot", ["utbot"])
    consensus_sell = _replay_signal(start + timedelta(minutes=10), "SELL", "consensus", ["consensus"])

    combo_streams = SignalProcessor._historical_replay_streams(
        [consensus_sell, both_buy, utbot_sell],
        "utbot_consensus",
    )
    assert [
        [SignalProcessor._historical_replay_stream_key(sig, "utbot_consensus") for sig in stream]
        for stream in combo_streams
    ] == [["utbot", "utbot"], ["consensus", "consensus"]]

    ranked_only = _replay_signal(start, "BUY", "utbot", ["utbot", "ema", "consensus"])
    adaptive = _replay_signal(start, "BUY", "adaptive_hybrid", ["utbot", "consensus"])
    assert len(SignalProcessor._historical_replay_streams([ranked_only], "utbot_prime")) == 1
    assert SignalProcessor._historical_replay_stream_key(
        SignalProcessor._historical_replay_order([ranked_only], "utbot_prime")[0],
        "utbot_prime",
    ) == "main"
    assert len(SignalProcessor._historical_replay_streams([adaptive], "adaptive_hybrid")) == 1
    assert SignalProcessor._historical_trade_id("H", "NIFTY", "5min", adaptive, "adaptive_hybrid") == "H_NIFTY_5min_20260701100000"


def test_engine_lab_processes_all_modes_without_changing_active_engine():
    settings = get_settings()
    previous_engine = settings.scanner_engine
    settings.scanner_engine = "quad_prime"
    try:
        lab = EngineLab(settings.get_ut_engine_params())
        candles = _sample_candles()
        snapshot = lab.process(
            candles_by_instrument={
                "NIFTY": {
                    "1min": candles,
                    "5min": candles,
                    "15min": candles,
                }
            },
            active_engine=settings.scanner_engine,
            capital=100000,
            risk_pct=1.0,
            lots_by_instrument={"NIFTY": 1},
            lot_size_by_instrument={"NIFTY": 50},
        )
    finally:
        settings.scanner_engine = previous_engine

    assert settings.scanner_engine == previous_engine
    assert snapshot["enabled"] is True
    assert snapshot["modes"] == list(ENGINE_LAB_MODES)
    assert {row["engine"] for row in snapshot["rows"]} == set(ENGINE_LAB_MODES)
    assert len(snapshot["rows"]) == len(ENGINE_LAB_MODES)
    assert snapshot["summary"]["engines"] == len(ENGINE_LAB_MODES)
    assert snapshot["summary"]["latency_ms"] >= 0
    assert any(row["active"] for row in snapshot["rows"])
    for row in snapshot["rows"]:
        assert "wins" in row
        assert "losses" in row
        assert "win_rate" in row
        assert "open_rows" in row
        assert "total_points" in row
        assert "profit_factor" in row
        assert "sharpe" in row


def test_adaptive_dual_engine_lab_reports_lane_metrics():
    settings = get_settings()
    previous_engine = settings.scanner_engine
    previous_dual = getattr(settings, "dual_lane_enabled", True)
    settings.scanner_engine = "adaptive_dual"
    settings.dual_lane_enabled = True
    try:
        lab = EngineLab(settings.get_ut_engine_params())
        candles = _apex_drive_candles()
        candles_15m = _to_15m(candles)
        snapshot = lab.process(
            candles_by_instrument={
                "NIFTY": {
                    "1min": candles,
                    "5min": candles,
                    "15min": candles_15m,
                }
            },
            active_engine="adaptive_dual",
            capital=100000,
            risk_pct=1.0,
            lots_by_instrument={"NIFTY": 1},
            lot_size_by_instrument={"NIFTY": 50},
        )
    finally:
        settings.scanner_engine = previous_engine
        settings.dual_lane_enabled = previous_dual

    adaptive = next(row for row in snapshot["rows"] if row["engine"] == "adaptive_dual")
    assert "lane_A_pnl" in adaptive
    assert "lane_B_pnl" in adaptive
    assert "dual_concurrent_pnl" in adaptive
    assert "lane_jaccard" in adaptive
    assert adaptive["lane_B_signals"] > 0
    assert 0.0 <= adaptive["lane_jaccard"] <= 1.0


def test_adaptive_dual_replay_scorecard_attributes_dual_lane_pnl():
    lab = EngineLab(get_settings().get_ut_engine_params())
    start = datetime(2026, 7, 1, 9, 15)
    frame = pd.DataFrame(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"open": 101, "high": 101.5, "low": 100, "close": 101, "volume": 1000},
            {"open": 101, "high": 104, "low": 94, "close": 102, "volume": 1000},
        ],
        index=[start + timedelta(minutes=5 * i) for i in range(4)],
    )
    lane_a = UTBotSignal(
        timestamp=start + timedelta(minutes=5),
        signal_type="BUY",
        price=100.0,
        trailing_stop=98.0,
        stop_distance=2.0,
        atr_value=2.0,
        adx_value=30.0,
        position_state=1,
        bar_index=1,
        instrument="NIFTY",
        timeframe="5min",
        source_engine="adaptive_dual",
        source_lane="A",
    )
    lane_b = UTBotSignal(
        timestamp=start + timedelta(minutes=10),
        signal_type="SELL",
        price=101.0,
        trailing_stop=105.0,
        stop_distance=4.0,
        atr_value=4.0,
        adx_value=30.0,
        position_state=-1,
        bar_index=2,
        instrument="NIFTY",
        timeframe="5min",
        source_engine="apex_hybrid",
        source_lane="B",
    )

    scorecard = lab._replay_scorecard(
        mode="adaptive_dual",
        signals_by_instrument={"NIFTY": [("5min", lane_a), ("5min", lane_b)]},
        candles_by_instrument={"NIFTY": {"5min": frame}},
        now_iso=start.isoformat(),
    )

    assert scorecard["lane_A_signals"] == 1
    assert scorecard["lane_B_signals"] == 1
    assert scorecard["lane_A_pnl"] > 0
    assert scorecard["lane_B_pnl"] > 0
    assert scorecard["dual_concurrent_pnl"] > 0


def test_adaptive_dual_lane_b_can_append_in_live_mode():
    settings = get_settings()
    previous_engine = settings.scanner_engine
    previous_dual = getattr(settings, "dual_lane_enabled", True)
    settings.scanner_engine = "adaptive_dual"
    settings.dual_lane_enabled = True
    candles = _apex_drive_candles()
    candles_15m = _to_15m(candles)

    fake_scanner = SimpleNamespace(
        mode="REAL",
        candles=SimpleNamespace(get_candles=lambda _inst, _tf: candles_15m if _tf == "15min" else candles),
        _vix_data={},
        data=SimpleNamespace(get_ltp=lambda *_args, **_kwargs: 15.0),
        signals=SimpleNamespace(_grade_signal=lambda *_args, **_kwargs: ("A", 0.82, ["test grade"])),
        log_event=lambda *_args, **_kwargs: None,
    )
    mtf_result = SimpleNamespace(
        confluence_score=0.5,
        results_1min={"state": {"position": 1}, "adx": [30.0]},
        results_5min={"state": {"position": 1}, "adx": [30.0]},
        results_15min={"state": {"position": 1}, "adx": [30.0]},
    )
    candidates = []
    try:
        processor = SignalProcessor.__new__(SignalProcessor)
        processor._append_adaptive_dual_apex_candidates(
            scanner=fake_scanner,
            instrument="NIFTY",
            mtf_result=mtf_result,
            intel_result={"session_context": {}},
            intel_score=0.8,
            regime="TRENDING_UP",
            settings=settings,
            candidates_raw=candidates,
        )
    finally:
        settings.scanner_engine = previous_engine
        settings.dual_lane_enabled = previous_dual

    assert candidates
    assert all(getattr(row["sig"], "source_lane", "") == "B" for row in candidates)
    assert all(getattr(row["sig"], "source_engine", "") == "apex_hybrid" for row in candidates)


def test_historical_process_best_signal_seeds_lane_b_replay_series():
    source = inspect.getsource(SignalProcessor.process_best_signal)
    assert "_apex_replay_signals" in source
    assert "scanner_engine_mode == \"adaptive_dual\"" in source
    assert "not is_live" in source


def test_engine_lab_hash_tracks_volume_for_lane_b_cache():
    frame_a = _sample_candles(20)
    frame_b = frame_a.copy()
    frame_b.iloc[-1, frame_b.columns.get_loc("volume")] = frame_b.iloc[-1]["volume"] * 3

    assert EngineLab._hash_df(frame_a) != EngineLab._hash_df(frame_b)


def test_engine_lab_replay_closes_on_candle_path_hits():
    lab = EngineLab(get_settings().get_ut_engine_params())
    start = datetime(2026, 7, 1, 9, 15)
    frame = pd.DataFrame(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"open": 101, "high": 104, "low": 100, "close": 103, "volume": 1000},
        ],
        index=[start + timedelta(minutes=5 * i) for i in range(3)],
    )
    signal = UTBotSignal(
        timestamp=start + timedelta(minutes=5),
        signal_type="BUY",
        price=100.0,
        trailing_stop=98.0,
        stop_distance=2.0,
        atr_value=2.0,
        adx_value=30.0,
        position_state=1,
        bar_index=1,
        instrument="NIFTY",
        timeframe="5min",
    )

    scorecard = lab._replay_scorecard(
        mode="utbot",
        signals_by_instrument={"NIFTY": [("5min", signal)]},
        candles_by_instrument={"NIFTY": {"5min": frame}},
        now_iso=start.isoformat(),
    )

    assert scorecard["closed"] == 1
    assert scorecard["wins"] == 1
    assert scorecard["losses"] == 0
    assert scorecard["open"] == 0
    assert scorecard["total_points"] > 0


def test_engine_lab_marks_open_trades_without_timeframe():
    """The scanner marks shadow trades per instrument without a TF; every per-TF
    open trade must still be stop/target-managed (keys are 'mode|inst|tf')."""
    lab = EngineLab(get_settings().get_ut_engine_params())
    start = datetime(2026, 7, 1, 10, 0)
    signal = UTBotSignal(
        timestamp=start,
        signal_type="BUY",
        price=100.0,
        trailing_stop=98.0,
        stop_distance=2.0,
        atr_value=2.0,
        adx_value=30.0,
        position_state=1,
        bar_index=50,
        instrument="NIFTY",
        timeframe="5min",
    )
    lab._open_or_reverse("utbot", "NIFTY", "5min", signal, start.isoformat())
    assert list(lab._open.keys()) == ["utbot|NIFTY|5min"]

    # Price crashes through the shadow stop; the TF-less mark must close the trade.
    lab._mark_open_trade("utbot", "NIFTY", 90.0, start.isoformat())
    assert not lab._open
    assert lab._closed
    assert lab._closed[-1]["exit_reason"] == "STOP_HIT"


def test_live_engine_lab_snapshot_serves_cache_and_throttles(monkeypatch):
    """REAL mode must never compute the lab inline on the scan loop: the snapshot
    call serves the cached table and refreshes at most once per refresh window."""
    lab = EngineLab(get_settings().get_ut_engine_params())
    calls = []

    fake = SimpleNamespace(
        engine_lab=lab,
        mode="REAL",
        _engine_lab_live_refreshed_at=0.0,
        _engine_lab_refresh_inflight=False,
        _lower_current_thread_priority=lambda: None,
    )
    fake._engine_lab_collect_frames = lambda copy=False: {"NIFTY": {}}

    def _fake_compute(frames, include_actual_sweep=True):
        calls.append(include_actual_sweep)
        lab._last_snapshot = {
            "enabled": True,
            "rows": [{"engine": "utbot", "active": False}],
            "summary": {"engines": 1},
            "updated_at": "refresh",
        }
        return lab._last_snapshot

    fake._engine_lab_compute_snapshot = _fake_compute

    # No running event loop -> inline fallback refresh, then serve the snapshot.
    snapshot = Scanner._engine_lab_snapshot_nonblocking(fake)
    assert calls == [False]  # actual-sweep must never run off the scan loop
    assert fake._engine_lab_refresh_inflight is False
    assert fake._engine_lab_live_refreshed_at > 0
    assert snapshot["updated_at"] == "refresh"

    # Within the refresh window: served from cache, no recompute.
    snapshot = Scanner._engine_lab_snapshot_nonblocking(fake)
    assert calls == [False]
    assert snapshot["updated_at"] == "refresh"


def test_live_engine_lab_compute_never_touches_live_lane():
    """REAL-mode lab refresh must be fully isolated from live trading state.

    The fake scanner deliberately has NO trades / mtf / risk_manager / broker
    attributes: if the live compute path ever reaches for them, this test fails
    with AttributeError. The active engine setting must also come back untouched.
    """
    settings = get_settings()
    previous_engine = settings.scanner_engine
    lab = EngineLab(settings.get_ut_engine_params())
    candles = _sample_candles()

    fake = SimpleNamespace(
        engine_lab=lab,
        mode="REAL",
        config={"indices": {"NIFTY": {"lot_size": 50}}},
        capital_fut=100000.0,
        capital_opt=100000.0,
        risk_fut_pct=1.0,
        user_lots={"NIFTY": 1},
    )

    frames = {"NIFTY": {"1min": candles, "5min": candles, "15min": candles}}
    try:
        snapshot = Scanner._engine_lab_compute_snapshot(
            fake, frames, include_actual_sweep=False
        )
    finally:
        settings.scanner_engine = previous_engine

    assert settings.scanner_engine == previous_engine
    assert "error" not in snapshot, f"live lab compute failed: {snapshot.get('error')}"
    assert len(snapshot["rows"]) == len(ENGINE_LAB_MODES)
    assert not hasattr(fake, "trades")
    assert not hasattr(fake, "mtf")
    assert not hasattr(fake, "risk_manager")
    assert not hasattr(fake, "broker")


def test_cached_engine_lab_active_row_tracks_current_scanner_engine():
    settings = get_settings()
    previous_engine = settings.scanner_engine
    settings.scanner_engine = "utbot_consensus"
    snapshot = {
        "engine_lab": {
            "active_engine": "consensus",
            "rows": [
                {"engine": "consensus", "active": True, "signals": 76, "total_money": 8645.9},
                {
                    "engine": "utbot_consensus",
                    "active": False,
                    "signals": 171,
                    "open": 7,
                    "open_rows": [{"instrument": "NIFTY", "pnl_points": -12.5}],
                    "net_points": -1536.53,
                    "open_points": 189.04,
                    "total_points": -1347.49,
                    "gross_gain": 900.52,
                    "gross_loss": 2437.05,
                    "total_money": -1699.15,
                },
            ],
        },
        "trades": {
            "summary": {
                "total_trades": 55,
                "open_count": 0,
                "wins": 37,
                "losses": 18,
                "win_rate": 67.3,
                "daily_pnl": 27641.44,
                "fut_pnl": 0.0,
                "opt_pnl": 27641.44,
                "profit_factor": 3.37,
                "sharpe_ratio": 0.42,
                "sharpe_ratio_display": "0.42 / 6.60",
                "max_drawdown": 7156.0,
            }
        },
    }
    try:
        Scanner._sync_engine_lab_active_row(SimpleNamespace(mode="HISTORICAL"), snapshot)
    finally:
        settings.scanner_engine = previous_engine

    rows = {row["engine"]: row for row in snapshot["engine_lab"]["rows"]}
    assert snapshot["engine_lab"]["active_engine"] == "utbot_consensus"
    assert rows["consensus"]["active"] is False
    assert rows["utbot_consensus"]["active"] is True
    assert rows["utbot_consensus"]["signals"] == 55
    assert rows["utbot_consensus"]["wins"] == 37
    assert rows["utbot_consensus"]["losses"] == 18
    assert rows["utbot_consensus"]["total_money"] == 27641.44
    assert rows["utbot_consensus"]["display_basis"] == "ACTUAL BACKTEST"
    assert rows["utbot_consensus"]["raw_net_points"] == -1536.53
    assert rows["utbot_consensus"]["raw_total_points"] == -1347.49
    assert rows["utbot_consensus"]["raw_open_rows"] == [{"instrument": "NIFTY", "pnl_points": -12.5}]
    assert rows["utbot_consensus"]["net_points"] is None
    assert rows["utbot_consensus"]["open_points"] is None
    assert rows["utbot_consensus"]["total_points"] is None
    assert rows["utbot_consensus"]["gross_gain"] is None
    assert rows["utbot_consensus"]["gross_loss"] is None
    assert rows["utbot_consensus"]["open_rows"] == []


def test_engine_lab_actual_cache_survives_simulation_id_only_change():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner.simulation_id = 100
    scanner.backtest_days = 7
    scanner.active_indices = ["NIFTY"]
    scanner.user_lots = {"NIFTY": 1}
    scanner.user_lots_fut = {"NIFTY": 1}
    candles = _sample_candles(20)
    scanner.candles = SimpleNamespace(get_candles=lambda *_args: candles)

    first = Scanner._engine_lab_actual_cache_fingerprint(scanner)
    scanner.simulation_id = 101
    second = Scanner._engine_lab_actual_cache_fingerprint(scanner)

    assert first == second


def test_historical_scan_engine_lab_snapshot_defers_actual_sweep():
    calls = []
    scanner = SimpleNamespace(
        mode="HISTORICAL",
        _engine_lab_snapshot=lambda include_actual_sweep=True: calls.append(include_actual_sweep) or {
            "summary": {}
        },
    )

    snapshot = Scanner._engine_lab_snapshot_for_scan(scanner)

    assert snapshot == {"summary": {}}
    assert calls == [False]
