"""Re-aggregating a FORMING bar on every tick must not hold the candle lock.

2026-08-31, third pass. After the telemetry rewrite and the touched-row narrowing, live
scan latency was no longer catastrophic but it was bimodal: 331-535ms when ticks were
sparse, 1,826-2,688ms when they were dense, against a historical baseline of 200-300ms.

py-spy on the live process (55s, 120Hz, 175,862 samples) found the reason, and it was
still the same lock:

    57.5 THREAD-SECONDS blocked on CandleBuilder._lock inside a 55-second capture
    5,411 of those samples in get_candles, which is 58.6% of ALL scan-path samples

The holder is `_resample_and_merge`, which runs INSIDE `with self._lock:` in
update_candles. Measured against a 2000-row cache with a single incoming row:

    full critical section per tick : 14.289 ms
      1min merge (concat/restores) :  3.8 ms
      _resample_and_merge          : 10.478 ms
        _resample_window   5min    :  0.149 ms   (5 rows)
        _resample_ohlcv    5min    :  2.089 ms   (5 rows -> 1)
        _merge_candles     5min    :  2.576 ms   (1 row into a 400-row cache)
        ...and the same again for 15min

    at ~10 ticks/s x 4 indices  ->  587 ms of lock-held time PER SECOND

Almost none of that is data volume -- it is fixed pandas per-call overhead, paid to
recompute the SAME forming bucket about ten times a second. A closing bar genuinely
needs the work; a forming one does not.

So the forming bar's resample is now rate-limited and a closing bar always flushes.
Deferring never drops a row: skipped labels accumulate and are handed to the next
resample, and every finished 1-minute bar resamples unthrottled. Only the forming
bucket can lag, by at most `resample_min_interval_seconds`.

The property that matters, and the one these tests pin: **a CLOSED bucket is never
affected.** Closed buckets are what the entry gates evaluate.
"""
import datetime as dt
import random

import pandas as pd
import pytest
from loguru import logger

from data.candle_builder import CandleBuilder


def test_forming_row_can_coalesce_to_management_cadence_without_losing_ticks(monkeypatch):
    clock = {"now": 1.0}
    monkeypatch.setattr("data.candle_builder.time.monotonic", lambda: clock["now"])
    builder = CandleBuilder(
        max_candles=100,
        partial_publish_min_interval_seconds=0.1,
    )
    start = pd.Timestamp("2026-09-09 14:20:00").to_pydatetime()

    builder.update_from_tick("NIFTY", 100.0, 1000.0, start, "fyers_index")
    clock["now"] = 1.05
    builder.update_from_tick("NIFTY", 102.0, 1100.0, start, "fyers_index")
    assert float(builder.get_candles("NIFTY", "1min").iloc[-1]["close"]) == 100.0

    clock["now"] = 1.11
    builder.update_from_tick("NIFTY", 101.0, 1200.0, start, "fyers_index")
    forming = builder.get_candles("NIFTY", "1min").iloc[-1]
    assert float(forming["high"]) == 102.0
    assert float(forming["close"]) == 101.0
    assert int(forming["tick_count"]) == 3

    # A minute transition bypasses the coalescing window: the closed bar and the new
    # forming bar are both immediately visible.
    clock["now"] = 1.12
    builder.update_from_tick(
        "NIFTY", 103.0, 1300.0, start + dt.timedelta(minutes=1), "fyers_index"
    )
    frame = builder.get_candles("NIFTY", "1min")
    assert list(frame.index[-2:]) == [
        pd.Timestamp("2026-09-09 14:20:00"),
        pd.Timestamp("2026-09-09 14:21:00"),
    ]
    assert float(frame.iloc[-2]["close"]) == 101.0
    assert float(frame.iloc[-1]["close"]) == 103.0


@pytest.fixture(autouse=True)
def _quiet():
    """update_from_tick logs a line per volume-source rebase; 10k ticks is unreadable."""
    logger.disable("data.candle_builder")
    yield
    logger.enable("data.candle_builder")


def _ticks(rng, minutes=25, source="kite"):
    """A dense, realistic tick stream: several per second, one volume basis throughout."""
    base = dt.datetime(2026, 8, 31, 9, 15, 0)
    out = []
    price, cumulative = 24000.0, 0
    for minute in range(minutes):
        for k in range(rng.randint(8, 20)):
            price += rng.uniform(-6, 6)
            cumulative += rng.randint(0, 400)
            out.append(
                (
                    round(price, 2),
                    cumulative,
                    base + dt.timedelta(minutes=minute, seconds=min(59, k * 3)),
                    source,
                )
            )
    return out


def _replay(ticks, interval):
    builder = CandleBuilder(max_candles=2000, resample_min_interval_seconds=interval)
    for price, volume, stamp, source in ticks:
        builder.update_from_tick("NIFTY", price, volume, stamp, volume_source=source)
    return builder


def _closed(frame):
    """Every bucket except the last; the last is the one still forming."""
    return None if frame is None or len(frame) < 2 else frame.iloc[:-1]


# `receipt_at` stamps the wall-clock instant a tick was received, so two replays of the
# same stream never agree on it. It is not derived from the candle data and no gate reads
# it as a value, only as a freshness marker, so comparing it would only ever measure how
# far apart the two replays ran.
WALL_CLOCK = {"receipt_at"}


def _difference(left, right, label):
    if left is None or right is None:
        return None if (left is None and right is None) else f"{label}: one side empty"
    if not left.index.equals(right.index):
        return f"{label}: index {len(left)} vs {len(right)}"
    for column in left.columns:
        if column in WALL_CLOCK:
            continue
        if column not in right.columns:
            return f"{label}: missing {column}"
        a = left[column].astype(object).where(left[column].notna(), None)
        b = right[column].astype(object).where(right[column].notna(), None)
        if not a.equals(b):
            return f"{label}: values in {column}"
    return None


def test_throttling_never_changes_a_closed_bucket():
    """The whole safety case, over dense streams that force heavy deferral.

    The replay feeds thousands of ticks as fast as the CPU allows, so real elapsed time
    between them is microseconds and the 0.4s throttle defers almost every forming
    update -- the worst case the guard has to survive.
    """
    rng = random.Random(20260831)
    differences = []
    for trial in range(4):
        ticks = _ticks(rng)
        throttled = _replay(ticks, 0.4)
        reference = _replay(ticks, 0.0)  # resample on every tick, the old behaviour

        for got, want, label in (
            (throttled._candles_1min.get("NIFTY"), reference._candles_1min.get("NIFTY"), "1min"),
            (_closed(throttled._candles_5min.get("NIFTY")),
             _closed(reference._candles_5min.get("NIFTY")), "5min closed"),
            (_closed(throttled._candles_15min.get("NIFTY")),
             _closed(reference._candles_15min.get("NIFTY")), "15min closed"),
        ):
            difference = _difference(got, want, label)
            if difference:
                differences.append(f"trial {trial}: {difference}")

    assert not differences, (
        "throttling altered data a gate would read: " + "; ".join(differences[:5])
    )


def test_a_closing_bar_is_never_deferred():
    """A finished 1-minute bar must resample immediately, flushing any backlog.

    `_resample_and_merge` returns early until the 1min frame holds at least 5 rows, so
    the stream has to cross that floor before any 5min frame can exist at all.
    """
    builder = CandleBuilder(max_candles=2000, resample_min_interval_seconds=999.0)
    base = dt.datetime(2026, 8, 31, 9, 15, 0)
    cumulative = 0

    # eight full minutes: every forming update defers under a 999s floor, but each bar
    # CLOSE goes through unthrottled, so the 5min frame must still be built
    for minute in range(8):
        for k in range(6):
            cumulative += 50
            builder.update_from_tick(
                "NIFTY",
                24000.0 + minute + k,
                cumulative,
                base + dt.timedelta(minutes=minute, seconds=k * 8),
            )

    five = builder._candles_5min.get("NIFTY")
    assert five is not None and len(five) >= 1, (
        "closing bars were deferred along with the forming ones; nothing resampled"
    )
    assert builder._candles_15min.get("NIFTY") is not None

    # now defer a fresh backlog inside the current minute...
    for k in range(6):
        cumulative += 50
        builder.update_from_tick(
            "NIFTY", 24100.0 + k, cumulative, base + dt.timedelta(minutes=8, seconds=k * 8)
        )
    pending_before = builder._pending_resample_touched.get("NIFTY")
    assert pending_before is not None and len(pending_before) >= 1, (
        "the forming-bar updates were not deferred, so this test proves nothing"
    )

    # ...and close the bar; the backlog must be flushed, not carried
    before = len(five)
    builder.update_from_tick(
        "NIFTY", 24150.0, cumulative + 100, base + dt.timedelta(minutes=9, seconds=1)
    )
    # Crossing a minute emits BOTH a finalized bar and a fresh partial, so what remains
    # pending afterwards is the newly-opened forming bar -- correct, and not a backlog.
    # What must be gone is every label deferred before the close.
    pending_after = builder._pending_resample_touched.get("NIFTY")
    # `pending_after or []` would raise: a DatetimeIndex has no truth value.
    still_pending = set(pending_after) if pending_after is not None else set()
    assert not (set(pending_before) & still_pending), (
        f"a closing bar left the earlier backlog deferred: "
        f"{sorted(set(pending_before) & still_pending)}"
    )
    assert still_pending <= {pd.Timestamp("2026-08-31 09:24:00")}, (
        f"unexpected labels still deferred after a close: {sorted(still_pending)}"
    )
    assert len(builder._candles_5min["NIFTY"]) >= before


def test_deferred_labels_are_accumulated_not_dropped():
    """Every skipped label must reach the next resample.

    The stream first has to clear the cold-start rule -- while a higher-timeframe frame
    does not exist yet nothing is ever deferred -- so drive eight minutes before looking.
    """
    builder = CandleBuilder(max_candles=2000, resample_min_interval_seconds=999.0)
    base = dt.datetime(2026, 8, 31, 9, 15, 0)
    cumulative = 0
    for minute in range(8):
        for k in range(6):
            cumulative += 50
            builder.update_from_tick(
                "NIFTY",
                24000.0 + minute + k,
                cumulative,
                base + dt.timedelta(minutes=minute, seconds=k * 8),
            )
    assert builder._candles_5min.get("NIFTY") is not None, "cold start never completed"

    for k in range(6):
        cumulative += 50
        builder.update_from_tick(
            "NIFTY", 24100.0 + k, cumulative, base + dt.timedelta(minutes=8, seconds=k * 8)
        )

    pending = builder._pending_resample_touched.get("NIFTY")
    assert pending is not None and len(pending) >= 1, "nothing was deferred"
    one_min = builder._candles_1min["NIFTY"]
    assert set(pending).issubset(set(one_min.index)), (
        "a deferred label is not present in the 1min frame it will be resampled from"
    )


def test_the_1min_frame_is_never_throttled():
    """Only the higher-timeframe aggregate is deferred; 1min stays exact per tick."""
    rng = random.Random(7)
    ticks = _ticks(rng, minutes=8)
    throttled = _replay(ticks, 0.4)
    reference = _replay(ticks, 0.0)

    assert _difference(
        throttled._candles_1min["NIFTY"], reference._candles_1min["NIFTY"], "1min"
    ) is None


def test_the_resample_actually_runs_far_less_often():
    """The point of the change, stated as a count rather than a timing."""
    calls = {"n": 0}
    original = CandleBuilder._resample_and_merge

    def counted(self, instrument, touched_index=None):
        calls["n"] += 1
        return original(self, instrument, touched_index)

    rng = random.Random(11)
    # Long enough for the fixed cold-start cost to stop dominating: while a
    # higher-timeframe frame is missing every update resamples, so a short stream
    # understates the saving. Measured 185 -> 72 over 12 minutes, 377 -> 85 over 25.
    ticks = _ticks(rng, minutes=25)

    CandleBuilder._resample_and_merge = counted
    try:
        calls["n"] = 0
        _replay(ticks, 0.0)
        unthrottled = calls["n"]
        calls["n"] = 0
        _replay(ticks, 0.4)
        throttled = calls["n"]
    finally:
        CandleBuilder._resample_and_merge = original

    assert throttled < unthrottled / 3, (
        f"resample ran {throttled} times against {unthrottled} unthrottled over "
        f"{len(ticks)} ticks -- the forming-bar throttle is not engaging"
    )
    # a closing bar always forces one, so it can never collapse to zero
    assert throttled >= 25, f"only {throttled} resamples for 25 closed bars"
