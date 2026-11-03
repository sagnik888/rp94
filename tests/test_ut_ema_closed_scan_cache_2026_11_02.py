"""Confirmed OHLC replay does not change when only the forming candle changes."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pandas as pd
import pytest

from config.settings import Settings
from engine.multi_timeframe import MultiTimeframeEngine


def host(monkeypatch, intrabar=False):
    import config.settings as settings_module
    import engine.multi_timeframe as module
    settings = Settings(scanner_engine="ut_ema", trading_mode="REAL",
                        ut_signal_mode="realtime" if intrabar else "confirmed",
                        live_intrabar_signal_entries=intrabar)
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)

    class Clock(datetime):
        current = pd.Timestamp("2026-11-02 10:57:30", tz="Asia/Kolkata").to_pydatetime()

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", Clock)
    mtf = MultiTimeframeEngine()
    frame = pd.DataFrame(dict(open=22000., high=22010., low=21990., close=22000., volume=100.),
                         index=pd.date_range("2026-11-02 09:15", periods=21, freq="5min"))
    engine = mtf.engines["NIFTY_5min"]
    engine.process = Mock(wraps=engine.process)
    mtf.update_data("NIFTY", "5min", frame)
    mtf.process_instrument("NIFTY")
    return mtf, engine, frame, Clock


def test_confirmed_replay_reuses_result_when_only_forming_candle_changes(monkeypatch):
    mtf, engine, frame, _ = host(monkeypatch)
    first = mtf._result_cache["NIFTY_5min"]
    frame.iloc[-1, frame.columns.get_loc("close")] = 22005.
    mtf.update_data("NIFTY", "5min", frame)
    current = mtf.process_instrument("NIFTY")
    assert engine.process.call_count == 1
    assert current.timeframe_metadata["5min"]["used_cache"]
    assert current.results_5min["utema_events"] == first["utema_events"]
    assert current.results_5min["state"] == first["state"]


def test_closed_correction_and_close_boundary_invalidate_cache(monkeypatch):
    mtf, engine, frame, clock = host(monkeypatch)
    frame.iloc[-2, frame.columns.get_loc("close")] = 22005.
    mtf.update_data("NIFTY", "5min", frame)
    mtf.process_instrument("NIFTY")
    assert engine.process.call_count == 2
    clock.current += pd.Timedelta(minutes=3)
    mtf.process_instrument("NIFTY")
    assert engine.process.call_count == 3


def test_intrabar_replay_still_evaluates_forming_candle_changes(monkeypatch):
    mtf, engine, frame, _ = host(monkeypatch, intrabar=True)
    frame.iloc[-1, frame.columns.get_loc("close")] = 22005.
    mtf.update_data("NIFTY", "5min", frame)
    mtf.process_instrument("NIFTY")
    assert engine.process.call_count == 2


def test_observed_stream_still_evaluates_each_observation(monkeypatch):
    mtf, engine, _, _ = host(monkeypatch)
    engine._utema_stream_owned = True
    engine._utema_last_observed_at = "2026-11-02 10:57:31"
    mtf.process_instrument("NIFTY")
    engine._utema_last_observed_at = "2026-11-02 10:57:32"
    mtf.process_instrument("NIFTY")
    assert engine.process.call_count == 3


@pytest.mark.parametrize("bad", [22020., float("nan"), float("inf"), -1.])
def test_invalid_forming_ohlc_cannot_reuse_valid_closed_result(monkeypatch, bad):
    mtf, engine, frame, _ = host(monkeypatch)
    frame.iloc[-1, frame.columns.get_loc("close")] = bad
    mtf.update_data("NIFTY", "5min", frame)
    actual = mtf.process_instrument("NIFTY")
    assert not actual.timeframe_metadata["5min"]["used_cache"]
    assert engine.process.call_count == 2
    assert actual.results_5min is None
    assert "NIFTY_5min" not in mtf._result_cache


def test_duplicate_forming_index_cannot_reuse_valid_closed_result(monkeypatch):
    mtf, engine, frame, _ = host(monkeypatch)
    frame = pd.concat([frame, frame.iloc[-1:]])
    mtf.update_data("NIFTY", "5min", frame)
    actual = mtf.process_instrument("NIFTY")
    assert not actual.timeframe_metadata["5min"]["used_cache"]
    assert engine.process.call_count == 2
    assert actual.results_5min is None


@pytest.mark.parametrize("bad_input", ["range", "order", "overflow"])
def test_invalid_future_rows_still_fail_and_valid_input_recovers(monkeypatch, bad_input):
    mtf, engine, frame, _ = host(monkeypatch)
    future = frame.iloc[-1:].copy()
    future.index += pd.Timedelta(minutes=10)
    later = future.copy()
    later.index += pd.Timedelta(minutes=5)
    frame = pd.concat([frame, future, later])
    if bad_input == "order":
        frame = frame.iloc[list(range(len(frame)-2)) + [len(frame)-1, len(frame)-2]]
    elif bad_input == "overflow":
        frame["close"] = frame["close"].astype(object)
        frame.iloc[-2, frame.columns.get_loc("close")] = 10**1000
    else:
        frame.iloc[-2, frame.columns.get_loc("close")] = 22020.
    mtf.update_data("NIFTY", "5min", frame)
    actual = mtf.process_instrument("NIFTY")
    assert actual.results_5min is None
    assert not actual.timeframe_metadata["5min"]["used_cache"]
    assert engine.process.call_count == 2
    frame = frame.sort_index()
    frame["close"] = 22000.
    mtf.update_data("NIFTY", "5min", frame)
    assert mtf.process_instrument("NIFTY").results_5min is not None


def test_fresh_result_requirement_still_forces_confirmed_recomputation(monkeypatch):
    mtf, engine, _, _ = host(monkeypatch)
    previous = mtf._result_metadata["NIFTY_5min"]["computed_at_epoch"]
    mtf.require_fresh_result("NIFTY", "5min", newer_than_epoch=previous)
    actual = mtf.process_instrument("NIFTY")
    assert engine.process.call_count == 2
    assert not actual.timeframe_metadata["5min"]["used_cache"]
