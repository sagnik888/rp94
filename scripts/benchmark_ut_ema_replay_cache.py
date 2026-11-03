"""Measure identical native replay requests with and without retained caching."""
import json
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from engine.ut_ema_engine import UTEmaEngine


def main():
    close = 22000. + np.sin(np.arange(300)/7.)*80.
    frame = pd.DataFrame(dict(open=close, high=close+10., low=close-10., close=close),
                         index=pd.date_range("2026-10-28 09:15", periods=300, freq="5min"))
    clock = frame.index[-1]+pd.Timedelta(minutes=5)
    measured = {}
    expected = None
    for retained in (False, True):
        core = UTEmaEngine()
        core.process(frame, "NIFTY", "5min", realtime=False, as_of=clock)
        started = perf_counter()
        for _ in range(12):
            if not retained:
                core._historical_cache_key = None
            result = core.process(frame, "NIFTY", "5min", realtime=False, as_of=clock)
        measured["cached_ms" if retained else "uncached_ms"] = (perf_counter()-started)*1000/12
        events = result["utema_events"]
        if expected is None:
            expected = events
        assert expected == events
    measured["speedup"] = measured["uncached_ms"]/measured["cached_ms"]
    measured["basis"] = "12 repeated identical 300-bar OHLC requests; native events identical"
    print(json.dumps(measured, indent=2))


if __name__ == "__main__":
    main()
