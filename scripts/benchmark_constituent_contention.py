"""Compare scalar and aligned constituent aggregation with identical output."""
import json
from pathlib import Path
import sys
import threading
from datetime import date
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from data.constituent_volume import ConstituentVolumeService, Membership
from tests.test_latency_contention_2026_10_30 import reference


def main():
    service = ConstituentVolumeService.__new__(ConstituentVolumeService)
    service._lock = threading.RLock()
    members = tuple(("NSE", f"STOCK{i}") for i in range(50))
    service.memberships = (Membership("NIFTY", date(2026, 1, 1), date(2027, 1, 1),
                                      "benchmark", "benchmark", members),)
    bars = pd.date_range("2026-10-20 09:15", periods=2000, freq="min")
    stocks = {key: pd.DataFrame(dict(volume=float(i)), index=bars) for i, key in enumerate(members)}
    now = bars[-1]+pd.Timedelta(minutes=1)
    started = perf_counter()
    old = reference(service, bars, stocks, now)
    scalar_ms = (perf_counter()-started)*1000
    started = perf_counter()
    new = service.aggregate("NIFTY", bars, stocks, "kite", now=now)
    vector_ms = (perf_counter()-started)*1000
    pd.testing.assert_frame_equal(new, old)
    print(json.dumps(dict(scalar_ms=scalar_ms, aligned_ms=vector_ms,
        speedup=scalar_ms/vector_ms, bars=len(bars), members=len(members), identical=True), indent=2))


if __name__ == "__main__":
    main()
