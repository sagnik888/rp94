"""Compare archive-window ordering with identical chronological boundaries."""
from bisect import bisect_left, bisect_right
import json
from pathlib import Path
import random
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_derivative_archive_sort_latency_2026_11_02 import indexed_store


def main():
    report = {}
    for shape in ("shuffled", "flush_batches"):
        store, start, events = indexed_store(Path("reports/latency_afternoon_20261102/unused"), 200000)
        bucket = store._archive_obs[store._session(start)]["NIFTY"]
        if shape == "flush_batches":
            sequence = list(range(len(events)))
            rng = random.Random(4)
            for first in range(0, len(sequence), 128):
                tail = sequence[first:first+128]
                rng.shuffle(tail)
                sequence[first:first+128] = tail
            bucket["seqs"] = sequence
        cutoff, end = start+1., start+1900.
        def original():
            lo, hi = bisect_left(bucket["ats"], cutoff), bisect_right(bucket["ats"], end)
            pairs = sorted(zip(bucket["seqs"][lo:hi], bucket["events"][lo:hi]), key=lambda pair: pair[0])
            return [event for _, event in pairs]
        samples = {}
        for name, call in (("original", original), ("native", lambda: store._archived_observations("NIFTY", end, cutoff))):
            timings = []
            for _ in range(7):
                began = perf_counter()
                result = call()
                timings.append((perf_counter()-began)*1000)
            samples[name+"_ms"] = median(timings)
            if name == "original": expected = result
            else: assert result == expected
        samples.update(speedup=samples["original_ms"]/samples["native_ms"], identical=True, observations=len(expected))
        report[shape] = samples
    report["basis"] = "Median of seven 200000-event synthetic windows; not complete live scan latency"
    Path("reports/latency_afternoon_20261102/benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
