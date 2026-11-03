"""Combined engines retain the native S1 forming-bar recompute policy.

The base engine keeps its standalone observation cache. The S1 companion can
reuse an observation during its configured interval. Corrections, closed bars,
settings and stabilization requests invalidate S1 reuse independently.
"""
from pathlib import Path
import math

import pandas as pd
import pytest

from config.settings import get_settings
from engine.alt_scanners import AltSignalEngine
from engine.combined_scanner import CombinedSignalEngine
from engine.multi_timeframe import MultiTimeframeEngine
from engine.scanner_modes import COMBINED_MODES
import engine.multi_timeframe as mtf_module
import engine.ut_smart_engine as uts_module


@pytest.fixture(scope="module")
def native_frames():
    root = Path(__file__).resolve().parents[1] / "data_store" / "candles"
    return {
        tf: pd.read_csv(root / f"NIFTY_{tf}.csv", index_col=0, parse_dates=True)
        .loc[:"2026-09-30 15:00"].tail(900)
        for tf in ("1min", "5min", "15min")
    }


def _signature(result):
    def normal(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: normal(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return tuple(normal(item) for item in value)
        return value

    signals = []
    for signal in result.get("signals", []):
        signals.append(tuple(
            getattr(signal, key, None) for key in (
                "timestamp", "signal_type", "price", "trailing_stop",
                "source_engine", "s1_exit_time", "s1_exit_reason",
                "s1_exit_price", "uts_exit_time", "uts_exit_reason", "uts_exit_price",
            )
        ))
    return normal({
        "signals": signals,
        "positions": result.get("positions"),
        "trailing_stop": result.get("trailing_stop"),
        "adx": result.get("adx"),
        "state": result.get("state"),
    })


def _pipeline(monkeypatch, mode, native_frames, *, throttle=15.0,
              timeframe="1min", fire="realtime", clock_at="2026-09-30 15:00:01"):
    settings = get_settings()
    monkeypatch.setattr(settings, "scanner_engine", mode)
    monkeypatch.setattr(settings, "s1_lane_forming_recompute_seconds", throttle)
    monkeypatch.setattr(settings, "uts_lane_allow_intrabar_entries", False)
    clock = [pd.Timestamp(clock_at, tz="Asia/Kolkata").timestamp()]
    monkeypatch.setattr(mtf_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(uts_module, "evaluation_clock", lambda as_of=None: pd.Timestamp(
        clock[0], unit="s", tz="UTC").tz_convert("Asia/Kolkata").tz_localize(None))
    pipeline = MultiTimeframeEngine({"signal_mode": fire})
    calls = {native: 0 for native in (COMBINED_MODES[mode], "s1")}
    engine = pipeline.engines[f"NIFTY_{timeframe}"]
    assert isinstance(engine, CombinedSignalEngine)
    for native, child in engine.native_engines.items():
        original = child.process

        def counted(*args, _original=original, _native=native, **kwargs):
            calls[_native] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(child, "process", counted)
    frames = {tf: frame.copy(deep=True) for tf, frame in native_frames.items()}
    # These native dependency frames are sufficient for the companion, but this
    # focused test computes only one requested chart.
    pipeline._data_cache.update({f"NIFTY_{tf}": frame for tf, frame in frames.items()})
    pipeline.engines = {f"NIFTY_{timeframe}": engine}
    pipeline.update_data("NIFTY", timeframe, frames[timeframe])
    first = getattr(pipeline.process_instrument("NIFTY"), f"results_{timeframe}")
    assert first and all(not lane.get("error") for lane in first["native_results"].values())
    assert calls == {native: 1 for native in calls}
    return pipeline, frames, calls, clock, first


def _forming_tick(pipeline, frames, clock, elapsed=2.0, timeframe="1min"):
    clock[0] += elapsed
    frames[timeframe] = frames[timeframe].copy(deep=True)
    frames[timeframe].iloc[-1, frames[timeframe].columns.get_loc("close")] += 1.0
    frames[timeframe].iloc[-1, frames[timeframe].columns.get_loc("high")] += 1.0
    pipeline.update_data("NIFTY", timeframe, frames[timeframe])
    return pipeline.process_instrument("NIFTY")


def _standalone_base_pipeline(monkeypatch, mode, frames, *, timeframe="1min", fire="realtime"):
    """Match the standalone engine's cache across the same input sequence."""
    with monkeypatch.context() as temporary:
        temporary.setattr(get_settings(), "scanner_engine", COMBINED_MODES[mode])
        reference = MultiTimeframeEngine({"signal_mode": fire})
    reference.engines = {f"NIFTY_{timeframe}": reference.engines[f"NIFTY_{timeframe}"]}
    for tf, frame in frames.items():
        reference.update_data("NIFTY", tf, frame)
    reference.process_instrument("NIFTY")
    return reference


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_forming_tick_reuses_only_s1_and_preserves_current_base_math(
    monkeypatch, mode, native_frames,
):
    pipeline, frames, calls, clock, first = _pipeline(monkeypatch, mode, native_frames)
    reference = _standalone_base_pipeline(monkeypatch, mode, frames)
    changed = _forming_tick(pipeline, frames, clock).results_1min
    assert calls["s1"] == 1
    # QP's tracking chart is realtime. UTS without intrabar opt-in retains
    # the standalone closed-candle observation while only this bar forms.
    assert calls[COMBINED_MODES[mode]] == (2 if mode == "quad_prime_s1" else 1)
    assert _signature(changed["native_results"]["s1"]) == _signature(first["native_results"]["s1"])
    for tf, frame in frames.items():
        reference.update_data("NIFTY", tf, frame)
    standalone = reference.process_instrument("NIFTY").results_1min
    assert _signature(changed["native_results"][COMBINED_MODES[mode]]) == _signature(standalone)
    # The reused companion retains native entry and exit history, including its
    # exact protective stop. It never receives the base engine's arrows.
    assert changed["native_results"]["s1"]["signals"]
    assert any(getattr(signal, "s1_exit_time", None) for signal in changed["native_results"]["s1"]["signals"])


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_s1_recomputes_at_nondefault_throttle_deadline(monkeypatch, mode, native_frames):
    pipeline, frames, calls, clock, _ = _pipeline(monkeypatch, mode, native_frames, throttle=4.5)
    _forming_tick(pipeline, frames, clock, elapsed=3.5)
    assert calls["s1"] == 1
    _forming_tick(pipeline, frames, clock, elapsed=1.1)
    assert calls["s1"] == 2


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_s1_zero_throttle_recomputes_each_change(monkeypatch, mode, native_frames):
    pipeline, frames, calls, clock, _ = _pipeline(monkeypatch, mode, native_frames, throttle=0.0)
    _forming_tick(pipeline, frames, clock)
    assert calls["s1"] == 2


@pytest.mark.parametrize("mode", COMBINED_MODES)
@pytest.mark.parametrize("mutation", ["new_bar", "closed_correction", "dependency_correction", "settings", "fresh_observation"])
def test_combined_s1_required_observations_bypass_forming_reuse(
    monkeypatch, mode, native_frames, mutation,
):
    pipeline, frames, calls, clock, _ = _pipeline(monkeypatch, mode, native_frames)
    if mutation == "new_bar":
        row = frames["1min"].iloc[[-1]].copy(deep=True)
        row.index = row.index + pd.Timedelta(minutes=1)
        frames["1min"] = pd.concat([frames["1min"], row])
    elif mutation == "closed_correction":
        frames["1min"].iloc[-5, frames["1min"].columns.get_loc("close")] += 2.0
    elif mutation == "dependency_correction":
        frames["5min"].iloc[-5, frames["5min"].columns.get_loc("close")] += 2.0
        pipeline.update_data("NIFTY", "5min", frames["5min"])
    elif mutation == "settings":
        settings = get_settings()
        monkeypatch.setattr(settings, "s1_min_score", settings.s1_min_score + 5.0)
    else:
        pipeline.require_fresh_result("NIFTY", "1min", newer_than_epoch=clock[0] + 1.0)
    result = _forming_tick(pipeline, frames, clock)
    assert calls["s1"] == 2
    standalone = AltSignalEngine(signal_source="s1", signal_mode="realtime").process(
        frames["1min"], instrument="NIFTY", timeframe="1min",
        df_5min=frames["5min"], df_15min=frames["15min"],
    )
    assert _signature(result.results_1min["native_results"]["s1"]) == _signature(standalone)


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_failed_s1_lane_retries_without_candle_change(monkeypatch, mode, native_frames):
    pipeline, frames, calls, clock, _ = _pipeline(monkeypatch, mode, native_frames)
    native = pipeline.engines["NIFTY_1min"].native_engines["s1"]
    original = native.process
    attempts = []

    def transient(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("companion observation unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(native, "process", transient)
    pipeline.require_fresh_result("NIFTY", "1min", newer_than_epoch=clock[0] + 1.0)
    failed = _forming_tick(pipeline, frames, clock)
    assert failed.results_1min["native_results"]["s1"]["error"]
    clock[0] += 1.0
    restored = pipeline.process_instrument("NIFTY")
    assert len(attempts) == 2
    assert not restored.results_1min["native_results"]["s1"].get("error")


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_confirmed_s1_keeps_native_closed_signal_and_exit_history(
    monkeypatch, mode, native_frames,
):
    pipeline, frames, calls, clock, first = _pipeline(
        monkeypatch, mode, native_frames, timeframe="5min", fire="confirmed",
    )
    changed = _forming_tick(pipeline, frames, clock, timeframe="5min").results_5min
    assert calls["s1"] == 1
    assert calls[COMBINED_MODES[mode]] == 1
    assert changed["native_results"][COMBINED_MODES[mode]] is first["native_results"][COMBINED_MODES[mode]]
    assert _signature(changed["native_results"]["s1"]) == _signature(first["native_results"]["s1"])
    standalone = AltSignalEngine(signal_source="s1", signal_mode="confirmed").process(
        frames["5min"], instrument="NIFTY", timeframe="5min",
        df_15min=frames["15min"], as_of=uts_module.evaluation_clock(),
    )
    # A confirmed native engine ignores the forming candle for signal/exit state.
    assert _signature(changed["native_results"]["s1"])["signals"] == _signature(standalone)["signals"]
    assert changed["native_results"]["s1"]["state"]["s1_state_bar_time"] == standalone["state"]["s1_state_bar_time"]


@pytest.mark.parametrize("mode", COMBINED_MODES)
@pytest.mark.parametrize("fire", ["realtime", "confirmed"])
def test_combined_terminal_bar_close_invalidates_s1_inside_throttle_without_a_tick(
    monkeypatch, mode, native_frames, fire,
):
    pipeline, frames, calls, clock, _ = _pipeline(
        monkeypatch, mode, native_frames, timeframe="5min", fire=fire,
        clock_at="2026-09-30 15:04:58",
    )
    reference = _standalone_base_pipeline(monkeypatch, mode, frames, timeframe="5min", fire=fire)
    clock[0] += 3.0
    result = pipeline.process_instrument("NIFTY")
    assert calls["s1"] == 2
    assert calls[COMBINED_MODES[mode]] == (2 if mode == "ut_smart_s1" else 1)
    standalone = reference.process_instrument("NIFTY").results_5min
    assert _signature(result.results_5min["native_results"][COMBINED_MODES[mode]]) == _signature(standalone)
    assert result.timeframe_metadata["5min"]["used_cache"] is False


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_dependency_bar_close_invalidates_s1_inside_throttle_without_a_tick(
    monkeypatch, mode, native_frames,
):
    pipeline, frames, calls, clock, _ = _pipeline(
        monkeypatch, mode, native_frames, timeframe="1min", clock_at="2026-09-30 15:04:58",
    )
    # The requested 1m terminal bar was already closed. The 5m dependency closes
    # at 15:05 without any data revision and must invalidate its S1 projection.
    clock[0] += 3.0
    result = pipeline.process_instrument("NIFTY")
    assert calls["s1"] == 2
    assert result.timeframe_metadata["1min"]["used_cache"] is False


@pytest.mark.parametrize("mode", COMBINED_MODES)
@pytest.mark.parametrize("corrected_tf", ["1min", "5min", "15min"])
def test_combined_closed_terminal_correction_invalidates_s1_inside_throttle(
    monkeypatch, mode, native_frames, corrected_tf,
):
    pipeline, frames, calls, clock, _ = _pipeline(
        monkeypatch, mode, native_frames, clock_at="2026-09-30 15:16:01",
    )
    # Every terminal bar is already closed. The light signature's usual [:-1]
    # exclusion cannot treat a corrected closed terminal bar as a forming tick.
    frames[corrected_tf].iloc[-1, frames[corrected_tf].columns.get_loc("close")] += 25.0
    frames[corrected_tf].iloc[-1, frames[corrected_tf].columns.get_loc("high")] += 25.0
    clock[0] += 2.0
    pipeline.update_data("NIFTY", corrected_tf, frames[corrected_tf])
    changed = pipeline.process_instrument("NIFTY").results_1min
    assert calls["s1"] == 2
    standalone = AltSignalEngine(signal_source="s1", signal_mode="realtime").process(
        frames["1min"], instrument="NIFTY", timeframe="1min",
        df_5min=frames["5min"], df_15min=frames["15min"], as_of=uts_module.evaluation_clock(),
    )
    assert _signature(changed["native_results"]["s1"]) == _signature(standalone)


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_parameter_reset_drops_s1_observation_before_a_fresh_request(
    monkeypatch, mode, native_frames,
):
    pipeline, frames, calls, clock, _ = _pipeline(monkeypatch, mode, native_frames)
    cutoff = clock[0] + 0.5
    pipeline.require_fresh_result("NIFTY", "1min", newer_than_epoch=cutoff)
    pipeline.apply_engine_params({"key_value": 3.7, "signal_mode": "realtime"})
    changed = _forming_tick(pipeline, frames, clock)
    assert calls["s1"] == 2
    assert calls[COMBINED_MODES[mode]] == 2
    assert changed.timeframe_metadata["1min"]["result"]["computed_at_epoch"] > cutoff
    assert pipeline._combined_s1_observations["NIFTY_1min"]["computed_at_epoch"] > cutoff


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_external_result_cache_reset_cannot_resurrect_a_native_s1_observation(
    monkeypatch, mode, native_frames,
):
    pipeline, frames, calls, clock, _ = _pipeline(monkeypatch, mode, native_frames)
    # Scanner session/recalculation reset paths clear this public result store
    # directly. The companion cannot remain eligible after its envelope is gone.
    pipeline._result_cache.clear()
    pipeline._data_hash.clear()
    _forming_tick(pipeline, frames, clock)
    assert calls["s1"] == 2
