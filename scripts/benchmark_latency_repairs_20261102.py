"""Measure repeated expiry resolution and causal replay without broker access."""
import json
from pathlib import Path
from statistics import median
import sys
from time import perf_counter
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from engine.expiry_manager import ArchivedExpiryContext
from engine.ut_ema_replay import _simulate_cached, simulate_record
from tests.test_ut_ema_replay_2026_10_29 import native


def measured(call, count=30):
    samples = []
    for _ in range(count):
        started = perf_counter()
        result = call()
        samples.append((perf_counter()-started)*1000.)
    return median(samples), result


def main():
    resolver = ArchivedExpiryContext()
    arguments = ("NIFTY", "2026-10-30 12:00")
    def uncached_expiry():
        resolver._resolution_cache.clear()
        return resolver.resolve(*arguments)
    uncached, expected = measured(uncached_expiry)
    cached, actual = measured(lambda: resolver.resolve(*arguments))
    assert expected == actual and actual["available"]
    report = {"expiry": dict(uncached_ms=uncached, cached_ms=cached,
                              speedup=uncached/cached, identical=True)}
    bars = pd.DataFrame(dict(open=100., high=100.1, low=99.9, close=100.),
                        index=pd.date_range("2026-10-20 10:00", periods=126, freq="min"))
    record = native(exit_time="2026-10-20T12:00:00")
    arguments = (record, bars, 1, 2.5, 1., "2026-10-20 12:05")
    processor = SimpleNamespace()
    uncached, expected = measured(lambda: simulate_record(*arguments))
    _simulate_cached(processor, *arguments)
    cached, actual = measured(lambda: _simulate_cached(processor, *arguments))
    assert expected == actual
    report["option_replay"] = dict(uncached_ms=uncached, cached_ms=cached,
                                    speedup=uncached/cached, identical=True)
    report["basis"] = "Median of 30 calls per path; component measurements, not full live scan latency"
    destination = Path("reports/latency_audit_20261102/benchmark.json")
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
