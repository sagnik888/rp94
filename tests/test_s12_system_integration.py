"""End-to-end S1+S2 Dual wiring checks beyond the vendored Pine port."""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from engine import s12_lane
from engine.alt_scanners import S12SignalEngine, build_scanner_engine
from engine.engine_lab import EngineLab, ShadowTrade
from engine.scanner_modes import configured_mode, owner, scanner_mode
from engine.signal_processor import SignalProcessor
from engine import s12_engine


def test_mode_factory_and_exit_ownership_route_to_s12(monkeypatch):
    settings = SimpleNamespace(scanner_engine="s1s2_dual", scanner_auto_trend_adx=25.0)
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    engine = build_scanner_engine({})
    assert isinstance(engine, S12SignalEngine)
    assert configured_mode(settings) == "s1s2_dual"
    assert scanner_mode(settings) == "s1s2_dual"
    assert scanner_mode(settings, SimpleNamespace(source_engine="s1s2_dual", source_lane="D1")) == "d1"
    assert owner(SimpleNamespace(source_engine="s1s2_dual", source_lane="D2")) == "d2"


def test_host_adapter_drops_signals_from_a_stale_worker_snapshot(monkeypatch):
    monkeypatch.setattr("config.settings.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(s12_lane, "timeframe_enabled", lambda timeframe, settings: True)
    stale_signal = SimpleNamespace(signal_type="BUY")
    monkeypatch.setattr(s12_engine, "process", lambda **kwargs: {
        "signals": [stale_signal],
        "s12_evaluation": {"current": False, "signature": "old"},
        "s12_overlays": {"D1": {}},
    })

    result = S12SignalEngine().process(pd.DataFrame(), "NIFTY", "5min")

    assert result["signals"] == []
    assert result["s12_evaluation"]["signature"] == "old"
    assert result["s12_overlays"] == {"D1": {}}


def test_futures_replay_does_not_apply_an_option_premium_cap():
    held = SimpleNamespace(
        source_lane="D1", s12_lane="D1", direction="LONG", inst_type="FUT",
        entry_price=100.0, s12_stop=90.0, s12_stop_initial=90.0,
        s12_stop_pct=10.0, s12_r_ladder=[2.0, 3.0, 4.0], timeframe="5min",
    )
    decision = s12_lane.exit_decision(
        held, spot=95.0, premium=80.0, now=datetime(2026, 10, 9, 12, 0),
        settings=SimpleNamespace(options_sl_pct=15.0, s12_square_off="15:15"),
    )
    assert not decision.exit
    assert decision.index_stop == 90.0


def test_s12_futures_replay_uses_native_index_stop():
    index = pd.DatetimeIndex(["2026-10-09 10:00:00"])
    bars = pd.DataFrame(
        [(100.0, 102.0, 94.0, 98.0)],
        columns=["open", "high", "low", "close"],
        index=index,
    )
    signal = SimpleNamespace(
        source_lane="D1", s12_lane="D1", s12_trade_id="D1:test",
        timestamp=datetime(2026, 10, 9, 9, 55), timeframe="5min",
        s12_t1_hit=False, s12_t2_hit=False, s12_t3_hit=False,
    )
    market = {
        "lane": "D1", "trade_id": "D1:test", "stop_updates": [], "milestones": {},
    }
    replay = SignalProcessor._simulate_s12_futures_exit_path(
        bars,
        signal=signal,
        levels={"s12_stop": 95.0, "s12_stop_initial": 95.0},
        market=market,
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        settings=SimpleNamespace(options_sl_pct=15.0, s12_square_off="15:15"),
    )
    assert replay["reason"] == "S12_STOP"
    assert replay["price"] == 95.0
    assert replay["stop_hit"] is True


def test_option_replay_uses_s12_index_exit_and_persists_milestones(monkeypatch):
    settings = SimpleNamespace(
        options_sl_pct=15.0, ut_force_exit_time="15:25", s12_square_off="15:15",
        replay_s12_native_exits=True,
    )
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    monkeypatch.setattr("engine.signal_processor.get_settings", lambda: settings)
    index = pd.date_range("2026-10-09 10:00", periods=5, freq="1min")
    option_bars = pd.DataFrame(
        [(100, 102, 99, 101), (101, 103, 100, 102), (102, 104, 101, 103),
         (103, 104, 102, 103), (103, 105, 102, 104)],
        columns=["open", "high", "low", "close"], index=index,
    )
    spot_bars = pd.DataFrame(
        [(25000, 25005, 24998, 25002)] * len(index),
        columns=["open", "high", "low", "close"], index=index,
    )
    setup = SimpleNamespace(
        source_engine="s1s2_dual", source_lane="D2", s12_lane="D2",
        s12_trade_id="S2|NSE:NIFTY|1|2026-10-09T09:59:00|TRADE",
        direction="LONG", signal_type="BUY", price=25000.0, timestamp=datetime(2026, 10, 9, 9, 59),
        timeframe="1min", instrument="NIFTY", s12_stop=24990.0, s12_stop_initial=24990.0,
        s12_stop_pct=0.04, s12_r_ladder=[2.0, 3.0, 4.0], s12_t1=25020.0,
        s12_t2=25030.0, s12_t3=25040.0, s12_t1_hit=False, s12_t2_hit=False, s12_t3_hit=False,
        s12_stop_updates=[], s12_milestones={"T1": datetime(2026, 10, 9, 10, 1)},
        s12_exit_reason="SMART EXIT", s12_exit_time=datetime(2026, 10, 9, 10, 2),
        s12_exit_price=25010.0, s12_premium_cap_pct=5.0,
    )
    scanner = SimpleNamespace(candles=SimpleNamespace(
        get_candles=lambda instrument, timeframe: spot_bars if timeframe == "1min" else pd.DataFrame()
    ))
    result = SignalProcessor.__new__(SignalProcessor)._simulate_actual_option_exit(
        option_bars, index[0], index[-1] + pd.Timedelta(minutes=1), 90.0, 3.0,
        natural_sl_distance=20.0, max_sl_pct=0.15, tf15_exit_scanner=scanner,
        tf15_instrument="NIFTY", tf15_direction="LONG", tf15_option_type="CE",
        tf15_timeframe="1min", replay_source_lane="D2", replay_setup=setup,
        live_exits=True,
    )
    assert result is not None
    assert result["reason"] == "S12_SMART_EXIT"
    assert result["exit_time"] == pd.Timestamp("2026-10-09 10:03")
    assert result["target"] == 0.0
    assert result["s12_state"]["s12_t1_hit"] is True


def test_engine_lab_closes_s12_at_configured_square_off(monkeypatch):
    settings = SimpleNamespace(s12_square_off="15:15")
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    trade = ShadowTrade(
        engine="s1s2_dual", instrument="NIFTY", timeframe="1min", direction="LONG",
        entry_price=100.0, entry_time="2026-10-09T15:13:00", stop=90.0, target=0.0,
    )
    frame = pd.DataFrame(
        [(100, 101, 99, 100)], columns=["open", "high", "low", "close"],
        index=pd.DatetimeIndex(["2026-10-09 15:14:00"]),
    )
    result = EngineLab.__new__(EngineLab)._simulate_trade_path(trade, frame)
    assert result["reason"] == "S12_SQUARE_OFF"
    assert result["time"] == "2026-10-09T15:15:00"

