"""Preserve decisions while removing repeated Python loops and snapshot copies."""
import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from data.constituent_volume import ConstituentVolumeService
from engine.ut_ema_engine import Timeline, UTEmaEngine
from tests.test_kite_angel_replacement_2026_10_23 import manifest


def reference(service, bars, stocks, now):
    bars = pd.DatetimeIndex(bars)
    if bars.tz is not None:
        bars = bars.tz_convert("Asia/Kolkata").tz_localize(None)
    stocks = {k: service.local_frame(v) for k, v in stocks.items()}
    output = []
    for stamp in bars:
        membership = service.membership("NIFTY", stamp.date())
        values = []
        if membership and stamp == stamp.floor("min") and stamp+pd.Timedelta(minutes=1) <= now:
            for key in membership.members:
                frame = stocks.get(key)
                try:
                    value = float(frame.at[stamp, "volume"]) if frame is not None else math.nan
                    if not math.isfinite(value) or value < 0:
                        continue
                    if "volume_complete" in frame and frame.at[stamp, "volume_complete"] != True:
                        continue
                    values.append(value)
                except (KeyError, TypeError, ValueError):
                    continue
        expected = len(membership.members) if membership else 0
        complete = bool(expected and len(values) == expected)
        output.append(dict(volume=sum(values) if complete else math.nan,
            volume_complete=complete, volume_basis="constituent_shares" if complete else "UNAVAILABLE",
            volume_provider="kite", volume_members_observed=len(values), volume_members_expected=expected,
            membership_version=membership.version if membership else ""))
    return pd.DataFrame(output, index=bars)


@pytest.mark.parametrize("offset", [0, 30])
def test_constituent_vectorization_matches_scalar_rule(tmp_path, offset):
    service = ConstituentVolumeService(manifest(tmp_path))
    first = service.memberships[0]
    service.memberships = (replace(first, effective_until=pd.Timestamp("2026-10-24").date()),
        replace(first, effective_from=pd.Timestamp("2026-10-24").date(),
                version="changed", members=(("NSE", "BBB"), ("NSE", "CCC"))))
    bars = pd.date_range("2026-10-23 09:15", periods=100, freq="min", tz="Asia/Kolkata")
    bars = bars.append(bars+pd.Timedelta(days=1))+pd.Timedelta(seconds=offset)
    rng = np.random.default_rng(30)
    stocks = {}
    for name in ("AAA", "BBB", "CCC"):
        values = rng.integers(0, 100, len(bars)).astype(object)
        values[0:6] = [0, -1, np.nan, np.inf, "bad", "2"]
        frame = pd.DataFrame(dict(volume=values, volume_complete=True), index=bars)
        frame.iloc[8, frame.columns.get_loc("volume_complete")] = False
        stocks[("NSE", name)] = frame.drop(frame.index[12])
    now = pd.Timestamp("2026-10-24 10:50")
    expected = reference(service, bars, stocks, now)
    actual = service.aggregate("NIFTY", bars, stocks, "kite", now=now)
    pd.testing.assert_frame_equal(actual, expected)


def test_appended_closed_bar_does_not_replay_prior_timeline(monkeypatch):
    frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100.),
                         index=pd.date_range("2026-10-29 09:15", periods=50, freq="min"))
    original = Timeline.step_values
    calls = []
    def observed(self, i, *args, **kw):
        calls.append(i)
        return original(self, i, *args, **kw)
    monkeypatch.setattr(Timeline, "step_values", observed)
    core = UTEmaEngine()
    core.process(frame.iloc[:-1], "NIFTY", "1min", realtime=False, as_of=frame.index[-1])
    calls.clear()
    result = core.process(frame, "NIFTY", "1min", realtime=False,
                          as_of=frame.index[-1]+pd.Timedelta(minutes=1))
    assert calls == [49]
    expected = UTEmaEngine().process(frame, "NIFTY", "1min", realtime=False,
                                    as_of=frame.index[-1]+pd.Timedelta(minutes=1))
    assert result["utema_events"] == expected["utema_events"]
    assert result["state"] == expected["state"]


@pytest.mark.parametrize("correction", [False, True])
def test_reused_timeline_matches_fresh_native_events(correction):
    close = 22000. + np.sin(np.arange(90)/3.)*100.
    frame = pd.DataFrame(dict(open=close, high=close+10., low=close-10., close=close),
                         index=pd.date_range("2026-10-29 09:15", periods=90, freq="min"))
    core = UTEmaEngine()
    core.process(frame.iloc[:60], "NIFTY", "1min", realtime=False, as_of=frame.index[60])
    if correction:
        frame.iloc[10] = [22000., 22010., 21990., 22000.]
    for length in (70, 90, 50, 90):
        part = frame.iloc[:length]
        clock = part.index[-1]+pd.Timedelta(minutes=1)
        actual = core.process(part, "NIFTY", "1min", realtime=False, as_of=clock)
        expected = UTEmaEngine().process(part, "NIFTY", "1min", realtime=False, as_of=clock)
        assert actual["utema_events"] == expected["utema_events"]
        assert actual["state"] == expected["state"]
        assert actual["indicator_overlays"] == expected["indicator_overlays"]


def test_held_monitor_does_not_materialize_full_history(monkeypatch):
    from types import SimpleNamespace as NS
    from tests.test_ut_ema_stream_2026_10_29 import real_host
    from engine.ut_ema_lane import result_for_monitor
    scanner, adapter, frame = real_host(monkeypatch)
    core = adapter._native
    core.seed_stream(frame, "NIFTY", "5min", as_of="2026-10-29 12:35")
    timeline = core._timeline
    record = dict(lifecycle_id="held", stop=999., entry_time="2026-10-29T12:30:00", exit_time="")
    timeline.records["held"] = record
    adapter._utema_executable_event_ids = {"held|ENTRY|1"}
    def forbidden():
        raise AssertionError("Protection must not materialize the chart history")
    monkeypatch.setattr(adapter, "stream_snapshot", forbidden)
    trade = NS(instrument="NIFTY", timeframe="5min", utema_state={"lifecycle_id": "held"})
    result = result_for_monitor(scanner, trade)
    assert result["utema_records"] == {"held": record}
    assert result["utema_executable_event_ids"] == ["held|ENTRY|1"]
    result["utema_records"]["held"]["stop"] = -1
    assert record["stop"] == 999.
