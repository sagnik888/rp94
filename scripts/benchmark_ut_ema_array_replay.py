"""Compare complete native processing against the original scalar replay."""
import json
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from engine.ut_ema_engine import Params, Timeline, UTEmaEngine


def scalar_replay(self, frame, indicators, start, count):
    period = pd.Timedelta(minutes=int(self.timeframe.replace("min", "")))
    for i in range(start, count):
        self.step(i, frame, indicators, observed_at=frame.index[i]+period)


def main():
    optimized = Timeline.replay
    close = 22000. + np.sin(np.arange(1500)/7.)*80.
    frame = pd.DataFrame(dict(open=close, high=close+10., low=close-10., close=close),
                         index=pd.date_range("2026-10-28 09:15", periods=len(close), freq="min"))
    clock = frame.index[-1]+pd.Timedelta(minutes=1)
    results, timings = [], {}
    try:
        for name, replay in (("scalar", scalar_replay), ("array", optimized)):
            Timeline.replay = replay
            samples = []
            for _ in range(7):
                core = UTEmaEngine(Params(execution_mode="Signal Bar Close"))
                started = perf_counter()
                result = core.process(frame, "NIFTY", "1min", realtime=False, as_of=clock)
                samples.append((perf_counter()-started)*1000.)
            timings[name+"_ms"] = median(samples)
            results.append(result)
    finally:
        Timeline.replay = optimized
    for key in ("utema_events", "utema_records", "native_markers", "indicator_overlays", "state", "positions"):
        assert results[0][key] == results[1][key], key
    timings.update(speedup=timings["scalar_ms"]/timings["array_ms"], identical=True,
                   basis="Median of 7 fresh 1500-bar full processes, including indicators and result materialization")
    print(json.dumps(timings, indent=2))


if __name__ == "__main__":
    main()
