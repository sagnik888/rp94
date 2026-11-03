"""S1 CPU at the open (live, 2026-09-25 09:28).

Scans took 12-24s ("exceeded the 9s per-index budget", "Skipping NIFTY: previous
processing task is still running", periodic trades updater timeouts). A 45s py-spy
sample of main.py put S1 at 37% of all samples: every scan re-hashed three full candle
frames per chart and recomputed all 12 S1 charts because the forming bar ticked, and the
chart overlays were converted to lists one element at a time (6% alone).

Now, while only the forming bar changes, an S1 chart is recomputed at most every
s1_lane_forming_recompute_seconds; a newly closed bar, a changed dependency frame, changed S1
settings or a stabilization request for a fresh observation recompute at once.
"""
import numpy as np
import pandas as pd
import pytest

import engine.multi_timeframe as mtf_module
from engine.multi_timeframe import MultiTimeframeEngine


def _frame(rows=40, *, forming_close=None, extra_bar=False, freq="1min"):
    n = rows + (1 if extra_bar else 0)
    index = pd.date_range("2026-09-25 09:15", periods=n, freq=freq)
    close = [100.0 + i * 0.1 for i in range(n)]
    if forming_close is not None:
        close[-1] = forming_close
    return pd.DataFrame({"open": close, "high": [c + 0.5 for c in close], "low": [c - 0.5 for c in close],
                         "close": close, "volume": [1000.0] * n}, index=index)


class _S1:
    signal_source = "s1"
    signal_mode = "realtime"

    def __init__(self):
        self.calls = 0

    def process(self, **_kw):
        self.calls += 1
        return {"signals": [], "state": {"position": 0}}


def _mtf(monkeypatch, now):
    engine = _S1()
    mtf = MultiTimeframeEngine.__new__(MultiTimeframeEngine)
    mtf.engines = {"NIFTY_1min": engine}
    mtf._data_cache = {}
    mtf._result_cache = {}
    mtf._data_hash = {}
    mtf._input_metadata = {}
    mtf._result_metadata = {}
    mtf._fresh_result_required_after = {}
    mtf.tf_weights = {"1min": 0.15, "5min": 0.40, "15min": 0.45}
    monkeypatch.setattr(mtf_module.time, "time", lambda: now[0])
    mtf.update_data("NIFTY", "5min", _frame(freq="5min"))
    mtf.update_data("NIFTY", "15min", _frame(freq="15min"))
    return mtf, engine


def test_a_forming_bar_tick_reuses_the_s1_result_until_the_throttle_expires(monkeypatch):
    now = [1_000.0]
    mtf, engine = _mtf(monkeypatch, now)
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.0))
    mtf.process_instrument("NIFTY")
    assert engine.calls == 1
    now[0] += 5
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.4))      # only the forming bar moved
    res = mtf.process_instrument("NIFTY")
    assert engine.calls == 1 and res.timeframe_metadata["1min"].get("forming_bar_throttle") is True
    now[0] += 11                                                        # 16s since the compute
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.7))
    mtf.process_instrument("NIFTY")
    assert engine.calls == 2


def test_a_newly_closed_bar_recomputes_at_once(monkeypatch):
    now = [1_000.0]
    mtf, engine = _mtf(monkeypatch, now)
    mtf.update_data("NIFTY", "1min", _frame())
    mtf.process_instrument("NIFTY")
    now[0] += 2
    mtf.update_data("NIFTY", "1min", _frame(extra_bar=True))
    mtf.process_instrument("NIFTY")
    assert engine.calls == 2


def test_a_stabilization_request_for_a_fresh_observation_recomputes(monkeypatch):
    now = [1_000.0]
    mtf, engine = _mtf(monkeypatch, now)
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.0))
    mtf.process_instrument("NIFTY")
    now[0] += 3
    mtf.require_fresh_result("NIFTY", "1min", newer_than_epoch=now[0])
    now[0] += 1
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.2))
    mtf.process_instrument("NIFTY")
    assert engine.calls == 2


def test_a_changed_dependency_frame_recomputes(monkeypatch):
    now = [1_000.0]
    mtf, engine = _mtf(monkeypatch, now)
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.0))
    mtf.process_instrument("NIFTY")
    now[0] += 2
    mtf.update_data("NIFTY", "5min", _frame(freq="5min", extra_bar=True))   # a 5m bar closed
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.3))
    mtf.process_instrument("NIFTY")
    assert engine.calls == 2


def test_throttle_zero_recomputes_every_change(monkeypatch):
    from config import settings as settings_module

    base = settings_module.get_settings()
    monkeypatch.setattr(base, "s1_lane_forming_recompute_seconds", 0.0, raising=False)
    now = [1_000.0]
    mtf, engine = _mtf(monkeypatch, now)
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.0))
    mtf.process_instrument("NIFTY")
    mtf.update_data("NIFTY", "1min", _frame(forming_close=110.4))
    mtf.process_instrument("NIFTY")
    assert engine.calls == 2


def test_chart_overlay_values_convert_like_before():
    values = np.array([1.5, np.nan, np.inf, -2.0, -np.inf, 0.0])
    old = [float(v) if np.isfinite(v) else None for v in values]
    arr = np.asarray(values, dtype=float)
    out = arr.astype(object)
    out[~np.isfinite(arr)] = None
    new = out.tolist()
    assert new == old and all(type(v) in (float, type(None)) for v in new)


def test_the_live_s1_result_overlays_are_plain_floats_or_none():
    from engine.alt_scanners import AltSignalEngine
    from tests.test_s1_engine_2026_09_23 import _frame as s1_frame

    df = s1_frame(11)
    res = AltSignalEngine(signal_source="s1").process(df, instrument="NIFTY", timeframe="5min")
    for series in [o["values"] for o in res["indicator_overlays"]] + [res["adx"], res["trailing_stop"]]:
        assert len(series) == len(df)
        assert all(v is None or type(v) is float for v in series)
