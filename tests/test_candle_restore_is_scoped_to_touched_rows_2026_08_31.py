"""The per-tick candle merge must not re-scan the whole history under the lock.

2026-08-31, second pass. After _restore_candle_telemetry was made 39x faster earlier the
same morning, live scan latency fell from 137,254ms to 1,687-9,705ms -- but all four
indices still blew their 9s per-index budget and served stale fallbacks, and the
dashboard still logged "Error in periodic trades updater: TimeoutError".

py-spy on the live process (40s, 100Hz, 122,990 samples) found the reason. 8,973 samples
-- 89.7 THREAD-SECONDS inside a 40-second capture -- were threads blocked on a single
threading.Lock, CandleBuilder._lock:

    5,543  get_candles            (scan workers, warm-memory writer, dashboard)
    1,694  update_from_tick       (the Kite websocket thread itself)
    1,048  update_candles
      638  update_latest_price

Nobody was slow at copying. They were queued behind whoever held the lock. Benchmarked,
2000-row history against the ONE-ROW frame a tick actually delivers:

    full merge block held under _lock : 25.763 ms   per tick
      pd.concat + duplicated + sort   :  0.528 ms
      _restore_zeroed_volume          :  0.475 ms
      _restore_candle_telemetry       : 24.911 ms   <-- 97%

At ~10 ticks/sec across four indices that is 1,031 ms of lock-held time per second. The
lock was oversubscribed by itself, so the tick thread and the scan workers starved each
other.

The cause is scope, not speed: both restore helpers computed overlap against the ENTIRE
existing history (~2000 labels) when only the labels the incoming frame delivered could
possibly have changed. For any other label the merge kept the existing row verbatim, so
the restore either declines to write or writes a value back onto itself -- a provable
no-op, paid 2000 times per tick.

These tests pin the equivalence of narrowing that scope, and the cost property itself.
"""
import random

import numpy as np
import pandas as pd
import pytest

from data.candle_builder import CandleBuilder

SOURCES = ["kite", "fyers_history", "history", "", "fyers_ws", "live_tick"]


def _frame(index, *, history, rng):
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": [rng.choice([0, 250, 1000]) for _ in index],
            "tick_count": [rng.choice([0, 0.0, 3, 7, None, np.nan]) for _ in index],
            "volume_basis": [rng.choice(["ticks", "", "shares", None]) for _ in index],
            "source": [
                rng.choice(["fyers_history", "history", "kite"])
                if history
                else rng.choice(SOURCES)
                for _ in index
            ],
            "receipt_at": [rng.choice(["2026-08-31T09:15:00", "", None]) for _ in index],
            "provider_time": [rng.choice(["2026-08-31T09:15:00", "", None]) for _ in index],
        },
        index=index,
    )


def _merge(existing, new_data):
    """The dedupe the production path performs before the restores run."""
    combined = pd.concat([existing, new_data])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def _same(left, right):
    if not left.index.equals(right.index):
        return "index"
    for column in left.columns:
        a = left[column].astype(object).where(left[column].notna(), None)
        b = right[column].astype(object).where(right[column].notna(), None)
        if not a.equals(b):
            return f"values in {column}"
        if str(left[column].dtype) != str(right[column].dtype):
            return f"dtype in {column}: {left[column].dtype} vs {right[column].dtype}"
    return None


SHAPES = ("tail_tick", "multi_row_backfill", "disjoint_range", "full_refresh")


def _incoming_index(shape, index, rows, rng):
    if shape == "tail_tick":
        return index[[-1]]
    if shape == "multi_row_backfill":
        start = rng.randint(0, rows - 3)
        return index[start:][: rng.randint(2, 5)]
    if shape == "disjoint_range":
        return index.shift(rows + 5)[:3]
    return index


@pytest.mark.parametrize("restore", ["telemetry", "volume"])
def test_narrowing_to_touched_rows_changes_nothing(restore):
    """Scoping the restore to what the tick delivered is observably identical.

    Covers the four shapes the merge actually sees: a one-row tail tick (the hot path),
    a multi-row backfill, a disjoint history range, and a full refresh that overlaps
    everything.
    """
    rng = random.Random(31082026)
    differences = []
    seen = {shape: 0 for shape in SHAPES}

    for trial in range(200):
        rows = rng.randint(3, 60)
        index = pd.date_range("2026-08-31 09:15", periods=rows, freq="1min")
        existing = _frame(index, history=False, rng=rng)
        shape = SHAPES[trial % len(SHAPES)]
        seen[shape] += 1
        new_data = _frame(
            _incoming_index(shape, index, rows, rng), history=rng.random() < 0.6, rng=rng
        )
        combined = _merge(existing, new_data)

        if restore == "telemetry":
            function = CandleBuilder._restore_candle_telemetry
        else:
            function = CandleBuilder._restore_zeroed_volume

        wide = function(combined.copy(), existing)
        narrow = function(combined.copy(), existing, new_data.index)

        difference = _same(wide, narrow)
        if difference:
            differences.append(f"trial {trial} [{shape}]: {difference}")

    assert all(seen.values()), f"a merge shape was never exercised: {seen}"
    assert not differences, (
        "narrowing changed behaviour: " + "; ".join(differences[:5])
    )


def test_a_tick_no_longer_scans_the_whole_history():
    """The regression: cost scaled with the HISTORY, not with what the tick delivered.

    Stated as a ratio between a 2000-row history and a 100-row one for the same one-row
    tick. Scoped correctly the two are indistinguishable; scoped to the full history the
    2000-row case costs roughly twenty times the 100-row case.
    """
    import time

    def cost(rows):
        index = pd.date_range("2026-08-29 09:15", periods=rows, freq="1min")
        existing = pd.DataFrame(
            {
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                "volume": 1000, "tick_count": np.arange(1, rows + 1),
                "volume_basis": "ticks", "source": "kite",
                "receipt_at": "2026-08-31T09:15:00",
                "provider_time": "2026-08-31T09:15:00",
            },
            index=index,
        )
        new_data = pd.DataFrame(
            [{
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.4,
                "volume": 55, "tick_count": 0, "volume_basis": "",
                "source": "fyers_history", "receipt_at": "", "provider_time": "",
            }],
            index=[index[-1]],
        )
        combined = _merge(existing, new_data)
        touched = new_data.index

        CandleBuilder._restore_candle_telemetry(combined.copy(), existing, touched)
        best = float("inf")
        for _ in range(5):
            candidate = combined.copy()
            started = time.perf_counter()
            CandleBuilder._restore_candle_telemetry(candidate, existing, touched)
            best = min(best, time.perf_counter() - started)
        return best

    small = cost(100)
    large = cost(2000)

    assert large < small * 6, (
        f"a one-row tick cost {large*1000:.2f}ms against a 2000-row history but only "
        f"{small*1000:.2f}ms against a 100-row one -- the restore is scanning the whole "
        "history again, and it holds CandleBuilder._lock while it does"
    )


def test_the_lock_is_released_before_the_copy():
    """get_candles must not hold the lock while copying.

    Every writer REBINDS the dict entry and nothing mutates a published frame in place,
    so the reference taken under the lock stays a valid immutable snapshot. The copy is
    only 0.021ms on a 2000-row frame -- it was never the cost. The WAIT was: this line
    held 5,543 py-spy samples, all of them threads queued behind a lock the tick thread
    was holding for 25ms at a time.
    """
    builder = CandleBuilder(max_candles=2000)
    index = pd.date_range("2026-08-31 09:15", periods=50, freq="1min")
    builder._candles_1min["NIFTY"] = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}, index=index
    )

    held = {"during_copy": None}
    real_lock = builder._lock

    class WatchedFrame(pd.DataFrame):
        @property
        def _constructor(self):
            return WatchedFrame

        def copy(self, *args, **kwargs):
            held["during_copy"] = real_lock.locked()
            return pd.DataFrame(self).copy(*args, **kwargs)

    builder._candles_1min["NIFTY"] = WatchedFrame(builder._candles_1min["NIFTY"])

    result = builder.get_candles("NIFTY", "1min")

    assert result is not None and len(result) == 50
    assert held["during_copy"] is False, (
        "get_candles copied the frame while still holding _lock"
    )
    assert not real_lock.locked(), "the lock was left held"


def test_incremental_resample_matches_a_full_rebuild_for_the_touched_buckets():
    """A one-row tick updates 5m/15m exactly as a full-history resample would."""
    index = pd.date_range("2026-08-28 09:15", periods=600, freq="1min")
    base = pd.DataFrame(
        {
            "open": np.arange(600, dtype=float) + 100.0,
            "high": np.arange(600, dtype=float) + 101.0,
            "low": np.arange(600, dtype=float) + 99.0,
            "close": np.arange(600, dtype=float) + 100.5,
            "volume": np.arange(600, dtype=float) + 1.0,
        },
        index=index,
    )
    builder = CandleBuilder(max_candles=2000)
    builder._candles_1min["NIFTY"] = base.copy()
    builder._candles_5min["NIFTY"] = builder._resample_ohlcv(base, "5min")
    builder._candles_15min["NIFTY"] = builder._resample_ohlcv(base, "15min")

    stamp = index[-2]
    tick = pd.DataFrame(
        [{"open": 700.0, "high": 750.0, "low": 690.0, "close": 740.0, "volume": 999.0}],
        index=[stamp],
    )
    expected_base = pd.concat([base, tick])
    expected_base = expected_base[~expected_base.index.duplicated(keep="last")].sort_index()

    builder.update_candles("NIFTY", tick, "1min")

    pd.testing.assert_frame_equal(
        builder.get_candles("NIFTY", "5min"),
        builder._resample_ohlcv(expected_base, "5min"),
        check_freq=False,
    )
    pd.testing.assert_frame_equal(
        builder.get_candles("NIFTY", "15min"),
        builder._resample_ohlcv(expected_base, "15min"),
        check_freq=False,
    )


def test_one_tick_resamples_only_its_complete_higher_timeframe_bucket():
    index = pd.date_range("2026-08-28 09:15", periods=5000, freq="1min")
    base = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        index=index,
    )

    five = CandleBuilder._resample_window(base, index[[-1]], "5min")
    fifteen = CandleBuilder._resample_window(base, index[[-1]], "15min")

    assert 1 <= len(five) <= 5
    assert 1 <= len(fifteen) <= 15
    assert index[-1] in five.index and index[-1] in fifteen.index


def test_narrowing_the_resample_does_not_change_which_basis_a_bucket_inherits():
    """The one real divergence the narrowing introduced, found and pinned.

    _resample_ohlcv takes the single-unit volume_basis decision with a frame-wide
    nunique() and then stamps that ONE scalar onto EVERY bucket -- including buckets
    whose own rows carry no label at all. So the label set of the INPUT decides what an
    unlabelled bucket inherits, and feeding it a narrow window silently changed that: a
    5min bucket with no labelled row came out '' from a window where the full frame gave
    it 'ticks'. A 25-replay tick-stream harness caught it, 1 divergence in 25.

    The effect was benign -- _volume_window_is_homogeneous and volume_profiler both skip
    blank values ("unknown is not a unit"), so it can only shrink the set of distinct
    units seen and can never flip homogeneous to mixed. But scoping the resample is a
    PERFORMANCE change and must not move a gate input at all, so the basis decision is
    now taken over the full 1min cache via basis_source while only the touched window is
    aggregated.
    """
    index = pd.date_range("2026-08-31 09:15", periods=12, freq="1min")
    # the first bucket is labelled, the later ones carry nothing
    base = pd.DataFrame(
        {
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10,
            "tick_count": 1,
            "volume_basis": ["ticks"] * 5 + [""] * 7,
            "source": "kite", "receipt_at": "r", "provider_time": "p",
        },
        index=index,
    )

    full = CandleBuilder._resample_ohlcv(base, "5min")
    window = CandleBuilder._resample_window(base, index[[-1]], "5min")
    narrowed = CandleBuilder._resample_ohlcv(window, "5min", basis_source=base)

    unlabelled_bucket = full.index[-1]
    assert unlabelled_bucket in narrowed.index

    assert narrowed.loc[unlabelled_bucket, "volume_basis"] == full.loc[
        unlabelled_bucket, "volume_basis"
    ], (
        "the touched-window resample gave the bucket a different volume_basis than a "
        "full-frame resample would have; the basis decision is being taken over the "
        "window instead of the whole 1min cache"
    )


def test_a_bucket_spanning_a_unit_failover_is_still_marked_mixed():
    """basis_source must not flatten a genuine mid-bucket unit switch to one label."""
    index = pd.date_range("2026-08-31 09:15", periods=10, freq="1min")
    base = pd.DataFrame(
        {
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10,
            "tick_count": 1,
            # the 09:15 bucket spans a cash -> contracts failover
            "volume_basis": ["ticks", "ticks", "shares", "shares", "shares"] + ["shares"] * 5,
            "source": "kite", "receipt_at": "r", "provider_time": "p",
        },
        index=index,
    )

    full = CandleBuilder._resample_ohlcv(base, "5min")
    window = CandleBuilder._resample_window(base, index[[0]], "5min")
    narrowed = CandleBuilder._resample_ohlcv(window, "5min", basis_source=base)

    bucket = index[0].floor("5min")
    assert full.loc[bucket, "volume_basis"] == narrowed.loc[bucket, "volume_basis"]
    assert narrowed.loc[bucket, "volume_basis"] == "MIXED", (
        "a bucket spanning two volume units must stay MIXED so the surge guard can "
        "refuse it; collapsing it to one unit manufactures a ratio out of a failover"
    )
