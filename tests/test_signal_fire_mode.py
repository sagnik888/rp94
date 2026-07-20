from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import Settings
from engine.multi_timeframe import MultiTimeframeEngine
from scanner import Scanner
from trading.trade_manager import IST, Trade


def test_signal_fire_mode_defaults_to_intracandle_and_syncs_legacy_fields():
    settings = Settings(_env_file=None)

    settings.set_signal_fire_mode("INTRACANDLE")
    assert settings.get_signal_fire_mode() == "INTRACANDLE"
    assert settings.live_intrabar_signal_entries is True
    assert settings.ut_signal_mode == "realtime"

    settings.set_signal_fire_mode("CANDLE_CLOSE")
    assert settings.get_signal_fire_mode() == "CANDLE_CLOSE"
    assert settings.live_intrabar_signal_entries is False
    assert settings.ut_signal_mode == "confirmed"


def test_preset_does_not_override_user_signal_timing():
    settings = Settings(_env_file=None)
    settings.ut_preset = "CONSERVATIVE"

    settings.set_signal_fire_mode("INTRACANDLE")
    assert settings.get_ut_engine_params()["signal_mode"] == "realtime"

    settings.set_signal_fire_mode("CANDLE_CLOSE")
    assert settings.get_ut_engine_params()["signal_mode"] == "confirmed"


@pytest.mark.parametrize(
    ("preset", "key_value", "atr_period"),
    [
        ("AGGRESSIVE", 1.0, 10),
        ("BALANCED", 1.3, 10),
        ("CONSERVATIVE", 1.6, 14),
    ],
)
def test_ut_preset_labels_match_engine_trail_params(preset, key_value, atr_period):
    settings = Settings(_env_file=None)
    settings.ut_preset = preset

    params = settings.get_ut_engine_params()

    assert params["key_value"] == key_value
    assert params["atr_period"] == atr_period


def test_closed_candle_mode_keeps_one_minute_tracking_realtime():
    engine = MultiTimeframeEngine({"signal_mode": "confirmed"})

    assert engine.engines["NIFTY_1min"].signal_mode == "realtime"
    assert engine.engines["NIFTY_5min"].signal_mode == "confirmed"
    assert engine.engines["NIFTY_15min"].signal_mode == "confirmed"

    engine.apply_engine_params({"signal_mode": "confirmed"})
    assert engine.engines["BANKNIFTY_1min"].signal_mode == "realtime"
    assert engine.engines["BANKNIFTY_5min"].signal_mode == "confirmed"


def test_confirmed_hash_ignores_forming_bar_but_realtime_hash_tracks_it():
    index = pd.date_range("2026-06-22 09:15", periods=3, freq="5min")
    base = pd.DataFrame(
        {"open": [100, 101, 102], "high": [101, 102, 103], "low": [99, 100, 101], "close": [100, 101, 102]},
        index=index,
    )
    changed = base.copy()
    changed.loc[index[-1], ["high", "close"]] = [110, 109]

    assert MultiTimeframeEngine._hash_df(base, "confirmed") == MultiTimeframeEngine._hash_df(changed, "confirmed")
    assert MultiTimeframeEngine._hash_df(base, "realtime") != MultiTimeframeEngine._hash_df(changed, "realtime")


@pytest.mark.asyncio
async def test_confirmed_trade_skips_repaint_tracker():
    trade = SimpleNamespace(id="T1", status="OPEN", entered_intracandle=False)
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        signal_timestamp=datetime.now(IST),
    )

    await Scanner._track_repaint_until_candle_close(SimpleNamespace(), trade, candidate)

    assert trade._repaint_guard_checked is True


def test_trade_persists_entry_signal_mode():
    now = datetime.now(IST)
    trade = Trade(
        id="T1",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=now,
        trailing_stop=95.0,
        current_stop=95.0,
        lots=1,
        lot_size=25,
        grade="A",
        signal_fire_mode="CANDLE_CLOSE",
        entered_intracandle=False,
    )

    restored = Trade.from_dict(trade.to_dict())
    assert restored.signal_fire_mode == "CANDLE_CLOSE"
    assert restored.entered_intracandle is False
