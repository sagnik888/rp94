"""Native settings remain independent when scanners run with an S1 companion."""
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import config.settings as settings_module
from config.settings import Settings
from engine import s1_lane, uts_lane
from engine.alt_scanners import AltSignalEngine
from engine.combined_scanner import CombinedSignalEngine
from engine.s1_engine import params_from_settings as s1_params
from engine.ut_smart_engine import params_from_settings as uts_params
from tests.test_s1_companion_trial_2026_10_04 import signature


MODES = {"quad_prime_s1": "quad_prime", "ut_smart_s1": "ut_smart"}
SPEC = {**s1_lane.S1_SETTING_SPEC, **uts_lane.UTS_SETTING_SPEC}
NATIVE_OVERRIDES = {
    "s1_min_score": 43.0,
    "s1_conflict_margin": 13.0,
    "s1_min_adx": 17.0,
    "s1_use_htf": False,
    "s1_signal_cooldown": 2,
    "s1_candle_type": "standard",
    "s1_sl_mode": "fixed",
    "s1_sl1_pct": 0.18,
    "s1_use_sl2": True,
    "s1_sl2_pct": 0.45,
    "s1_t1_r": 1.3,
    "s1_t2_r": 2.1,
    "s1_t3_r": 3.8,
    "s1_trail_start_r": 0.9,
    "s1_trail_mult": 1.6,
    "s1_exit_conf_bars": 2,
    "s1_circuit_pause": 7,
    "s1_max_consec_loss_nifty": 2,
    "s1_max_consec_loss_banknifty": 3,
    "s1_max_consec_loss_sensex": 4,
    "s1_max_consec_loss_midcpnifty": 5,
    "s1_lane_guard_pct_5min": 8.0,
    "s1_lane_base_rr": 1.2,
    "s1_lane_lock_arm_r": 0.8,
    "uts_key_value": 1.8,
    "uts_atr_period": 13,
    "uts_key_value_5min": 2.2,
    "uts_atr_period_5min": 17,
    "uts_key_value_1min": 2.7,
    "uts_atr_period_1min": 19,
    "uts_use_heikin_ashi": True,
    "uts_smart_exit": False,
    "uts_arm_atr": 1.1,
    "uts_retrace_atr": 0.6,
    "uts_hard_retrace_atr": 1.4,
    "uts_min_bars": 3,
    "uts_confirmations": 3,
    "uts_rsi_length": 16,
    "uts_rsi_ob": 75.0,
    "uts_rsi_os": 25.0,
    "uts_rsi_lookback": 7,
    "uts_exit_ema": 8,
    "uts_wick_ratio": 0.4,
    "uts_lane_intrabar_wait_seconds": 24.0,
    "uts_lane_intrabar_wait_seconds_1min": 7.0,
    "uts_lane_lock_arm_r": 1.3,
    "uts_lane_guard_mult": 2.4,
    "uts_lane_sl_max_pct": 0.8,
    "uts_lane_min_entry_rr": 0.65,
    "live_intrabar_signal_entries": False,
    "ut_signal_mode": "confirmed",
    "uts_lane_allow_intrabar_entries": False,
}


@pytest.mark.parametrize("mode", ["ut_smart", "ut_smart_s1"])
@pytest.mark.parametrize("enabled", [False, True])
def test_main_timing_alias_updates_uts_in_both_modes(mode, enabled):
    settings = Settings(
        _env_file=None,
        scanner_engine=mode,
        uts_lane_allow_intrabar_entries=not enabled,
    )
    settings.set_signal_fire_mode("INTRACANDLE" if enabled else "CANDLE_CLOSE")
    assert settings.uts_lane_allow_intrabar_entries is enabled
    assert uts_lane.intrabar_enabled(settings) is enabled


@pytest.mark.parametrize("mode", ["quad_prime_s1", "s1"])
def test_main_timing_does_not_rewrite_inactive_uts_preferences(mode):
    settings = Settings(_env_file=None, scanner_engine=mode,
                        uts_lane_allow_intrabar_entries=False)
    settings.set_signal_fire_mode("INTRACANDLE")
    assert settings.uts_lane_allow_intrabar_entries is False


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("lane_enabled", [False, True])
def test_native_settings_round_trip_without_resetting_lane_toggles(
        tmp_path, monkeypatch, mode, lane_enabled):
    monkeypatch.chdir(tmp_path)
    # Process environment intentionally takes precedence over dotenv files. Other
    # collected application tests may load the real dotenv into that environment.
    # Isolate only the settings whose temporary-file round trip is asserted here.
    for name in {*SPEC, *NATIVE_OVERRIDES, "scanner_engine", "s1_lane_enabled", "uts_lane_enabled"}:
        monkeypatch.delenv(name.upper(), raising=False)
    settings = Settings(_env_file=None, scanner_engine=mode, **NATIVE_OVERRIDES,
                        s1_lane_enabled=lane_enabled, uts_lane_enabled=lane_enabled)
    expected = {name: getattr(settings, name) for name in SPEC}
    settings.save_to_env()
    restored = Settings(_env_file=tmp_path / ".env")
    assert restored.scanner_engine == mode
    assert {name: getattr(restored, name) for name in SPEC} == expected
    assert restored.s1_lane_enabled is lane_enabled
    assert restored.uts_lane_enabled is lane_enabled
    assert restored.uts_lane_allow_intrabar_entries is False


@pytest.mark.parametrize("mode", MODES)
def test_engine_construction_does_not_override_disabled_native_lane_settings(monkeypatch, mode):
    settings = Settings(_env_file=None, scanner_engine=mode,
                        s1_lane_enabled=False, uts_lane_enabled=False,
                        uts_lane_allow_intrabar_entries=False)
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    before = settings.model_dump()
    engine = CombinedSignalEngine(mode, signal_mode="confirmed")
    assert set(engine.native_engines) == {MODES[mode], "s1"}
    assert settings.model_dump() == before
    assert not s1_lane.is_s1(SimpleNamespace(source_engine="s1"), settings)
    assert not uts_lane.is_uts(SimpleNamespace(source_engine="ut_smart"), settings)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("index", ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"])
def test_companion_reads_the_same_s1_parameters_and_per_index_loss_pause(mode, index):
    combined = Settings(_env_file=None, scanner_engine=mode, **NATIVE_OVERRIDES)
    standalone = Settings(_env_file=None, scanner_engine="s1", **NATIVE_OVERRIDES)
    actual = s1_params(combined, index)
    assert asdict(actual) == asdict(s1_params(standalone, index))
    assert actual.max_consec_loss == NATIVE_OVERRIDES[f"s1_max_consec_loss_{index.lower()}"]
    assert actual.min_score == 43.0 and actual.use_htf is False
    assert actual.sl1_pct == 0.18 and actual.use_sl2 is True


@pytest.mark.parametrize("tf,key,period", [
    ("15min", 1.8, 13), ("5min", 2.2, 17), ("1min", 2.7, 19),
])
def test_uts_companion_keeps_own_timeframe_trail_and_exit_parameters(tf, key, period):
    combined = Settings(_env_file=None, scanner_engine="ut_smart_s1", **NATIVE_OVERRIDES)
    standalone = Settings(_env_file=None, scanner_engine="ut_smart", **NATIVE_OVERRIDES)
    actual = uts_params(combined, tf)
    assert asdict(actual) == asdict(uts_params(standalone, tf))
    assert actual.key_value == key and actual.atr_period == period
    assert actual.smart_exit is False and actual.use_heikin_ashi is True
    assert actual.arm_atr == 1.1 and actual.confirmations == 3


@pytest.mark.parametrize("mode", MODES)
def test_real_combined_signal_math_matches_native_engines_with_custom_settings(monkeypatch, mode):
    settings = Settings(_env_file=None, scanner_engine=mode, **NATIVE_OVERRIDES)
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    root = Path(__file__).resolve().parents[1]
    frames = {
        tf: pd.read_csv(root / f"data_store/candles/NIFTY_{tf}.csv",
                        index_col="timestamp", parse_dates=True).tail(600)
        for tf in ("1min", "5min", "15min")
    }
    frame = frames["5min"]
    common = dict(instrument="NIFTY", timeframe="5min", df_5min=frame,
                  df_15min=frames["15min"], capital=100000.0, risk_pct=1.0,
                  lots=1, lot_size=25)
    combined = CombinedSignalEngine(mode, signal_mode="confirmed").process(
        frame, as_of=frame.index[-1]+timedelta(minutes=5), **common)
    assert "error" not in combined
    for native in (MODES[mode], "s1"):
        kwargs = dict(common)
        if native != "quad_prime":
            kwargs["as_of"] = frame.index[-1]+timedelta(minutes=5)
        expected = AltSignalEngine(signal_source=native, signal_mode="confirmed").process(
            frame, **kwargs)
        actual = combined["native_results"][native]
        assert "error" not in actual
        assert signature(actual) == signature(expected)


@pytest.mark.parametrize("mode", MODES)
def test_native_stops_targets_and_intrabar_wait_remain_separate(mode):
    settings = Settings(_env_file=None, scanner_engine=mode, **NATIVE_OVERRIDES)
    s1 = SimpleNamespace(source_engine="s1", source_lane="S1", timeframe="5min")
    uts = SimpleNamespace(source_engine="ut_smart", source_lane="UTS", timeframe="5min")
    assert s1_lane.lane_intrabar_wait_seconds(s1, settings) == settings.s1_lane_intrabar_wait_seconds == 15.0
    assert s1_lane.lane_intrabar_wait_seconds(uts, settings) == 24.0
    assert s1_lane.lane_sl_max_pct(s1, settings) == 0.25
    assert s1_lane.lane_sl_max_pct(uts, settings) == 0.8
    # 2026-10-08: the S1 floor is now the base target (s1_lane_rr_floor_at_target); the
    # configured 0.5 floor still applies when that switch is off.
    assert s1_lane.lane_min_entry_rr(s1, settings) == pytest.approx(max(0.5, settings.s1_lane_base_rr - 0.02))
    object.__setattr__(settings, "s1_lane_rr_floor_at_target", False)
    assert s1_lane.lane_min_entry_rr(s1, settings) == 0.5
    assert s1_lane.lane_min_entry_rr(uts, settings) == 0.65
