"""Native output remains complete and detached through the accelerated copy path."""
import copy
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine.ut_bot_core import UTBotSignal
from engine.ut_ema_engine import UTEmaEngine, _copy_output


def candles(bars=180):
    close = 22000 + np.sin(np.arange(bars) / 7) * 80
    return pd.DataFrame(dict(open=close, high=close + 10, low=close - 10, close=close),
                        index=pd.date_range("2026-10-20 09:15", periods=bars, freq="min"))


def test_output_copy_matches_deepcopy_with_signal_extensions_aliases_and_cycles():
    signal = UTBotSignal(datetime(2026, 11, 2), "BUY", 10., 9., 1., 1., 0., 1, 0)
    signal.utema_state = dict(stop_updates=[dict(time="2026-11-02T09:15:00", stop=9.)])
    signal.extension = SimpleNamespace(shared=signal.utema_state)
    signal.raw_candle["owner"] = signal
    source = dict(signals=[signal], alias=signal.utema_state, values=[None, 1., "label"])
    cloned = _copy_output(source)
    assert type(cloned["signals"][0]) is UTBotSignal
    assert cloned["signals"][0] is not signal
    assert cloned["signals"][0].raw_candle["owner"] is cloned["signals"][0]
    assert cloned["alias"] is cloned["signals"][0].utema_state
    assert cloned["signals"][0].extension.shared is cloned["alias"]
    cloned["alias"]["stop_updates"][0]["stop"] = -1.
    assert signal.utema_state["stop_updates"][0]["stop"] == 9.
    simple = dict(events=[dict(price=10., confirmed=True)], values=[None, 1., "label"])
    assert _copy_output(simple) == copy.deepcopy(simple)


def test_datetime_with_mutable_timezone_is_detached_with_its_shared_alias():
    from datetime import timedelta, tzinfo

    class MutableTimezone(tzinfo):
        def __init__(self):
            self.notes = []

        def utcoffset(self, value):
            return timedelta(0)

    zone = MutableTimezone()
    stamp = datetime(2026, 11, 3, tzinfo=zone)
    cloned = _copy_output({"stamp": stamp, "zone": zone})
    assert cloned["stamp"] == stamp
    assert cloned["stamp"] is not stamp
    assert cloned["stamp"].tzinfo is cloned["zone"]
    cloned["zone"].notes.append("consumer mutation")
    assert zone.notes == []


def test_adapter_stream_snapshot_preserves_dynamic_aliases_and_full_isolation():
    from engine.alt_scanners import UTEmaSignalEngine

    adapter = UTEmaSignalEngine()
    signal = UTBotSignal(datetime(2026, 11, 3), "BUY", 10., 9., 1., 1., 0., 1, 0)
    signal.utema_state = {"stop_updates": [{"stop": 9.}]}
    signal.raw_candle["owner"] = signal
    adapter._stream_result = {"signals": [signal], "owner": signal.utema_state}
    original = copy.deepcopy(adapter._stream_result)
    snapshot = adapter.stream_snapshot()
    assert snapshot["signals"][0].utema_state == original["signals"][0].utema_state
    assert snapshot["signals"][0].raw_candle["owner"] is snapshot["signals"][0]
    assert snapshot["owner"] is snapshot["signals"][0].utema_state
    snapshot["owner"]["stop_updates"][0]["stop"] = -1.
    assert adapter._stream_result["owner"]["stop_updates"][0]["stop"] == 9.
    shallow = adapter.stream_snapshot(deep=False)
    assert shallow is not adapter._stream_result
    assert shallow["signals"] is adapter._stream_result["signals"]


@pytest.mark.parametrize("live", [False, True])
def test_public_outputs_isolate_every_nested_collection_and_historical_signal_state(live):
    frame = candles()
    engine = UTEmaEngine()
    now = frame.index[-1] + pd.Timedelta(seconds=1 if live else 60)
    first = engine.process(frame, "NIFTY", "1min", realtime=live, as_of=now)
    expected = copy.deepcopy(first)
    assert first["signals"] and first["utema_events"] and first["utema_records"]
    signals = first["signals"]
    signal = signals[0]
    ident = signal.utema_state["lifecycle_id"]
    sibling = next(other for other in signals[1:] if other.utema_state["lifecycle_id"] == ident)
    sibling_state = copy.deepcopy(sibling.utema_state)
    signal.utema_state["stop_updates"].append(dict(time="changed", stop=-1.))
    assert sibling.utema_state == sibling_state
    assert first["utema_records"][ident] == expected["utema_records"][ident]
    signal.utema_event["price"] = -1.
    signal.raw_candle["changed"] = []
    first["utema_records"][ident]["stop_updates"].append(dict(time="changed", stop=-2.))
    first["utema_events"][0]["price"] = -1.
    first["native_markers"][0]["text"] = "changed"
    first["indicator_overlays"][0]["values"][0] = -1.
    first["positions"][0] = 99
    first["state"]["current_price"] = -1.
    actual = (engine.stream_result() if live else
              engine.process(frame, "NIFTY", "1min", realtime=False, as_of=now))
    assert actual == expected
    assert [vars(item) for item in actual["signals"]] == [vars(item) for item in expected["signals"]]
    # The full historical signal collection and all execution-only exits survive.
    assert len(actual["signals"]) == len(expected["signals"])
    assert any(item.native_exit_only for item in actual["signals"])


def test_rolling_chart_compares_exact_arrays_without_constructing_row_series(monkeypatch):
    frame = candles()
    engine = UTEmaEngine()
    now = frame.index[-1] + pd.Timedelta(seconds=1)
    expected = engine.process(frame, "NIFTY", "1min", realtime=True, as_of=now)
    checkpoint = engine.stream_checkpoint()
    monkeypatch.setattr(pd.DataFrame, "iterrows", lambda *a, **k: pytest.fail("historical Series allocation"))
    actual = engine.chart_result(frame.iloc[1:], "NIFTY", "1min", as_of=now)
    assert actual == expected
    assert engine.stream_checkpoint() == checkpoint
    # A changed closed candle still takes the independent historical replay path.
    corrected = frame.iloc[1:].copy()
    corrected.iloc[5] += .25
    historical = UTEmaEngine().process(corrected, "NIFTY", "1min", realtime=False, as_of=now)
    assert engine.chart_result(corrected, "NIFTY", "1min", as_of=now) == historical
    assert engine.stream_checkpoint() == checkpoint
