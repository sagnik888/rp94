"""Regression proof for S1 exits, restart state and configuration recovery."""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import Settings
from data.sqlite_db import DatabaseManager
from engine import s1_lane
from trading.trade_manager import Trade


def trade(**kwargs):
    args = dict(id="s1-restart", instrument="NIFTY", timeframe="5min", direction="LONG",
                entry_price=100., entry_time=datetime(2026, 9, 24, 10, 5), trailing_stop=90.,
                current_stop=90., lots=1, lot_size=65, grade="B", inst_type="OPT",
                initial_stop=90., underlying_entry=23400., underlying_stop=23353.2,
                entry_spot=23400., source_lane="S1", signal_time=datetime(2026, 9, 24, 10, 0))
    args.update(kwargs)
    return Trade(**args)


@pytest.mark.parametrize("zone", [None, "Asia/Kolkata", "UTC"])
def test_exit_lookup_normalizes_signal_trade_and_clock_to_exchange_time(zone):
    def stamp(value):
        t = pd.Timestamp(f"2026-09-24 {value}", tz="Asia/Kolkata")
        return t.tz_convert(zone) if zone else t.tz_localize(None)
    t = trade(signal_time=stamp("10:00"))
    sig = SimpleNamespace(source_engine="s1", timestamp=pd.Timestamp("2026-09-24 10:00"),
                          signal_type="BUY", s1_exit_reason="Momentum Exit",
                          s1_exit_time=stamp("10:20"), s1_exit_price=23420.)
    assert s1_lane.engine_exit_for(t, {"signals": [sig]}, now=stamp("10:24:59")) is None
    assert s1_lane.engine_exit_for(t, {"signals": [sig]}, now=stamp("10:25"))["reason"] == "Momentum Exit"


@pytest.mark.parametrize("value", [None, pd.NaT, "invalid"])
def test_missing_exit_time_cannot_trigger_an_unconfirmed_exit(value):
    sig = SimpleNamespace(source_engine="s1", timestamp=datetime(2026, 9, 24, 10),
                          signal_type="BUY", s1_exit_reason="Momentum Exit", s1_exit_time=value)
    assert s1_lane.engine_exit_for(trade(), {"signals": [sig]}, now=datetime(2026, 9, 24, 11)) is None


def test_missing_geometry_uses_configured_fixed_sl_instead_of_dynamic_cap():
    settings = Settings(_env_file=None, s1_sl_mode="fixed", s1_sl1_pct=.2, s1_sl_max_pct=.25)
    entry, stop, t1, dist = s1_lane.index_levels(trade(underlying_stop=0.), settings)
    assert dist == pytest.approx(46.8)
    assert stop == pytest.approx(23353.2)
    assert t1 == pytest.approx(entry + dist * 2)


def test_short_future_profit_lock_tracks_falling_contract_price():
    t = trade(inst_type="FUT", direction="SHORT", underlying_stop=23446.8,
              entry_price=23400., initial_stop=23446.8)
    settings = Settings(_env_file=None)
    assert not s1_lane.exit_decision(t, spot=23390., premium=23340., settings=settings).exit
    assert t.s1_peak_premium == 23340.
    out = s1_lane.exit_decision(t, spot=23390., premium=23360., settings=settings)
    assert out.reason == "S1_PROFIT_LOCK"


def test_short_option_still_locks_rising_premium_and_future_loss_does_not_arm():
    settings = Settings(_env_file=None)
    option = trade(direction="SHORT", underlying_stop=23446.8)
    assert not s1_lane.exit_decision(option, spot=23390., premium=120., settings=settings).exit
    assert s1_lane.exit_decision(option, spot=23390., premium=114., settings=settings).reason == "S1_PROFIT_LOCK"
    future = trade(inst_type="FUT", direction="SHORT", underlying_stop=23446.8,
                   entry_price=23400., initial_stop=23446.8)
    assert not s1_lane.exit_decision(future, spot=23390., premium=23460., settings=settings).exit
    assert not s1_lane.exit_decision(future, spot=23390., premium=23440., settings=settings).exit


def test_s1_lock_survives_sqlite_and_trade_roundtrip(tmp_path):
    db = object.__new__(DatabaseManager)
    db._init_db(str(tmp_path / "s1.sqlite"))
    try:
        t = trade()
        settings = Settings(_env_file=None)
        assert not s1_lane.exit_decision(t, spot=23500., premium=125., settings=settings).exit
        assert t.s1_t1_locked and t.s1_best_spot == 23500.
        db.save_trade(t.to_dict())
        restored = Trade.from_dict(next(r for r in db.load_all_trades() if r['id'] == t.id))
        assert s1_lane.is_s1(restored, settings)
        assert restored.s1_t1_locked
        assert restored.s1_best_spot == 23500.
        assert restored.s1_peak_premium == 125.
        # A pullback still above the original SL must hit the restored profit lock.
        decision = s1_lane.exit_decision(restored, spot=23410., premium=122., settings=settings)
        assert decision.reason == "S1_T1_LOCK"
    finally:
        db.conn.close()


def test_existing_database_gets_additive_s1_columns_without_losing_rows(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    db = object.__new__(DatabaseManager)
    db._init_db(path)
    db.save_trade(trade().to_dict())
    for column in ("s1_best_spot", "s1_t1_locked", "s1_peak_premium"):
        db.conn.execute(f"ALTER TABLE trades DROP COLUMN {column}")
    db.conn.commit()
    db.conn.close()
    reopened = object.__new__(DatabaseManager)
    reopened._init_db(path)
    try:
        row = next(r for r in reopened.load_all_trades() if r['id'] == "s1-restart")
        restored = Trade.from_dict(row)
        assert restored.underlying_entry == 23400.
        assert restored.s1_t1_locked is False
        assert restored.s1_best_spot is None
    finally:
        reopened.conn.close()


def test_engine_lab_includes_s1_with_native_frames_and_invalidates_dependencies(monkeypatch):
    from engine import engine_lab
    from config.settings import get_settings

    assert "s1" in engine_lab.ENGINE_LAB_MODES
    assert engine_lab.build_shadow_engine("s1", {}).signal_source == "s1"
    monkeypatch.setattr(engine_lab, "ENGINE_LAB_MODES", ("s1",))
    calls = []

    class Engine:
        signal_mode = "realtime"

        def process(self, **kwargs):
            calls.append(kwargs)
            return {"signals": [], "state": {}}

    monkeypatch.setattr(engine_lab, "build_shadow_engine", lambda *args: Engine())
    lab = engine_lab.EngineLab()
    frames = {tf: pd.DataFrame({"open": 100., "high": 102., "low": 99., "close": 101., "volume": 500.},
                               index=pd.date_range("2026-09-23 09:15", periods=20, freq=tf))
              for tf in ("5min", "15min")}
    data = {"NIFTY": frames}
    result = lab.process(candles_by_instrument=data, active_engine="s1")
    assert result["rows"][0]["active"] and result["rows"][0]["errors"] == 0
    assert calls[0]["df_15min"] is frames["15min"]
    assert calls[1]["df_5min"] is frames["5min"]
    lab.process(candles_by_instrument=data)
    assert len(calls) == 2
    frames["5min"].iloc[0, 0] += 1  # Prefix correction changes recursive HA values.
    lab.process(candles_by_instrument=data)
    assert len(calls) == 4
    settings = get_settings()
    monkeypatch.setattr(settings, "s1_min_score", settings.s1_min_score + 1)
    lab.process(candles_by_instrument=data)
    assert len(calls) == 6


def test_engine_lab_actual_replay_invalidates_on_s1_setting_change(monkeypatch):
    from config.settings import get_settings
    from scanner import Scanner

    holder = SimpleNamespace(active_indices=[], mode="HISTORICAL")
    before = Scanner._engine_lab_actual_cache_fingerprint(holder)
    settings = get_settings()
    monkeypatch.setattr(settings, "s1_candle_type", "standard" if settings.s1_candle_type == "heikin_ashi" else "heikin_ashi")
    assert Scanner._engine_lab_actual_cache_fingerprint(holder) != before
