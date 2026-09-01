"""Restoring candle telemetry must not cost one column rebuild per cell.

2026-08-31, market open. The operator saw a stream of

    ERROR | Error in periodic trades updater: TimeoutError:

every six seconds. That is the dashboard's 1s poll plus a 5s _call_on_scanner_loop
budget, timing out at the FULL budget every time -- the loop was the casualty, not the
cause. The live process reported latency 75,706ms, scan_count 5 -> 5 across twenty
seconds and is_stale true, while both brokers were healthy (fyers_tick_age 0.01s,
broker_degraded false).

py-spy resolved it. Out of 35 threads exactly one was active and holding the GIL:

    check_array_indexer       (pandas/core/indexers/utils.py:542)
    __setitem__               (pandas/core/arrays/arrow/array.py:2224)
    ...
    _restore_candle_telemetry (data/candle_builder.py)
    _merge_candles            (data/candle_builder.py)
    _resample_and_merge       (data/candle_builder.py)
    update_from_tick          (data/candle_builder.py)
    on_ticks                  (data/market_data.py)

pandas 3.0 made the default string dtype PyArrow-backed, and an Arrow array is
IMMUTABLE. The old implementation assigned one cell at a time, so each write to a
string column such as `source` rebuilt that entire column -- on the Kite tick path, per
tick, holding the GIL. Measured on pandas 3.0.2: 35ms at 50 overlapping rows, 130ms at
200, 327ms at 500, 670ms at 1000.

Two kinds of test here, guarding different things:

  * equivalence -- the function's OUTPUT, values and dtypes alike, against a verbatim
    copy of the old per-cell implementation. This is the one that matters. Two rewrites
    were rejected by this harness before the current one passed: a batched write raised
    "Invalid value '[nan]' for dtype 'int64'" where the per-cell path silently upcast,
    and an inferred-dtype column rebuild turned an all-None object column into float64.
  * cost -- measured RELATIVE to that same reference implementation in the same process,
    so it states the property that actually regressed and does not become a flake on a
    loaded machine.
"""
import random
import time

import numpy as np
import pandas as pd
import pytest

from data.candle_builder import CandleBuilder

FIELDS = ("tick_count", "volume_basis", "source", "receipt_at", "provider_time")


def reference_restore(combined, existing):
    """The implementation in force until 2026-08-31, kept verbatim as the oracle."""
    if combined is None or existing is None or len(combined) == 0 or len(existing) == 0:
        return combined
    overlap = combined.index.intersection(existing.index)
    for idx in overlap:
        old = existing.loc[idx]
        new = combined.loc[idx]
        incoming_history = str(new.get("source", "") or "").lower() in {
            "fyers_history",
            "history",
        }
        prior_source = str(old.get("source", "") or "").lower()
        for field in FIELDS:
            if field not in combined.columns or field not in existing.columns:
                continue
            value = new.get(field)
            missing = value is None or (isinstance(value, str) and not value.strip())
            if not missing:
                try:
                    missing = bool(pd.isna(value))
                except (TypeError, ValueError):
                    pass
            if incoming_history and prior_source not in {"", "history", "fyers_history"}:
                missing = True
            if field == "tick_count" and not missing:
                try:
                    missing = float(value or 0) <= 0
                except (TypeError, ValueError):
                    missing = True
            if missing:
                prior = old.get(field)
                if prior is not None:
                    combined.loc[idx, field] = prior
    return combined


def _frame(index, *, history, duplicate, integral, rng):
    tick = (
        [rng.choice([0, 3, 7]) for _ in index]
        if integral
        else [rng.choice([0, 0.0, 3, 7, None, np.nan]) for _ in index]
    )
    sources = ["kite", "fyers_history", "history", "", "fyers_ws", None]
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
            "tick_count": tick,
            "volume_basis": [rng.choice(["ticks", "", "shares", None]) for _ in index],
            "source": [
                rng.choice(["fyers_history", "history", "kite"])
                if history
                else rng.choice(sources)
                for _ in index
            ],
            "receipt_at": [rng.choice(["2026-08-31T09:15:00", "", None]) for _ in index],
            "provider_time": [
                rng.choice(["2026-08-31T09:15:00", "", None]) for _ in index
            ],
        },
        index=index,
    )
    if duplicate and len(frame) > 1:
        # Duplicate labels make a single-label lookup return a FRAME rather than a row,
        # which the restore logic does not model; the implementation keeps the per-cell
        # path there, and this harness has to prove that path still agrees.
        frame = pd.concat([frame, frame.iloc[[0]]])
    return frame


def _outcome(function, combined, existing):
    """The frame, or the exception type if the call raises.

    Duplicate index labels are NOT supported by this function and never were: a
    single-label lookup then returns a Series, and `str(series or "")` raises
    ValueError: The truth value of a Series is ambiguous. That is pre-existing
    behaviour, so the rewrite has to reproduce the raise, not paper over it.
    """
    try:
        return function(combined, existing)
    except Exception as exc:  # noqa: BLE001 - the exception IS the observation
        return type(exc).__name__


def _difference(left, right):
    if isinstance(left, str) or isinstance(right, str):
        if left is not right and left != right:
            return f"outcome: {left if isinstance(left, str) else 'ok'} vs " \
                   f"{right if isinstance(right, str) else 'ok'}"
        return None
    if list(left.columns) != list(right.columns):
        return "column order"
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


def test_the_rewrite_matches_the_old_implementation_exactly():
    """Values and dtypes, over overlapping, disjoint and duplicate-label frames."""
    rng = random.Random(20260831)
    differences = []
    duplicates = 0
    raised = 0
    for trial in range(200):
        rows = rng.randint(1, 40)
        shift = rng.randint(0, 10)
        duplicate = rng.random() < 0.15
        integral = rng.random() < 0.5
        duplicates += 1 if duplicate else 0
        index = pd.date_range("2026-08-31 09:15", periods=rows, freq="1min")
        existing = _frame(
            index, history=False, duplicate=duplicate, integral=integral, rng=rng
        )
        incoming = _frame(
            index.shift(shift),
            history=rng.random() < 0.5,
            duplicate=duplicate,
            integral=integral,
            rng=rng,
        )
        expected = _outcome(reference_restore, incoming.copy(), existing.copy())
        actual = _outcome(
            CandleBuilder._restore_candle_telemetry, incoming.copy(), existing.copy()
        )
        if isinstance(expected, str):
            raised += 1
        difference = _difference(expected, actual)
        if difference:
            differences.append(f"trial {trial}: {difference}")

    assert duplicates > 0, "the duplicate-label path was never exercised"
    assert raised > 0, "the pre-existing duplicate-label raise was never reached"
    assert not differences, "the rewrite changed behaviour: " + "; ".join(differences[:5])


def test_a_nan_restored_into_an_int_column_still_upcasts():
    """The specific case that broke the first rewrite, pinned on its own.

    A batched write resolves the target dtype once for the whole array and raises
    TypeError: Invalid value '[nan]' for dtype 'int64'. The per-cell path upcast the
    column instead, and that is the behaviour callers already have.
    """
    index = pd.date_range("2026-08-31 09:15", periods=4, freq="1min")
    existing = pd.DataFrame(
        {
            "tick_count": pd.Series([np.nan, 5, np.nan, 9], index=index, dtype=object),
            "volume_basis": "ticks",
            "source": "kite",
            "receipt_at": "2026-08-31T09:15:00",
            "provider_time": "2026-08-31T09:15:00",
        },
        index=index,
    )
    incoming = pd.DataFrame(
        {
            "tick_count": pd.Series([0, 0, 0, 0], index=index, dtype="int64"),
            "volume_basis": "",
            "source": "fyers_history",
            "receipt_at": "",
            "provider_time": "",
        },
        index=index,
    )

    restored = CandleBuilder._restore_candle_telemetry(incoming.copy(), existing)

    assert restored["tick_count"].isna().tolist() == [True, False, True, False]
    assert restored["tick_count"].dropna().tolist() == [5, 9]
    assert restored["source"].tolist() == ["kite"] * 4


def test_live_telemetry_still_survives_a_history_refresh():
    """The behaviour the function exists for, stated directly rather than by oracle."""
    index = pd.date_range("2026-08-31 09:15", periods=3, freq="1min")
    existing = pd.DataFrame(
        {
            "tick_count": [11, 12, 13],
            "volume_basis": "ticks",
            "source": "kite",
            "receipt_at": "2026-08-31T09:15:00",
            "provider_time": "2026-08-31T09:15:01",
        },
        index=index,
    )
    incoming = pd.DataFrame(
        {
            "tick_count": [0, 0, 0],
            "volume_basis": "",
            "source": "fyers_history",
            "receipt_at": "",
            "provider_time": "",
        },
        index=index,
    )

    restored = CandleBuilder._restore_candle_telemetry(incoming.copy(), existing)

    assert restored["tick_count"].tolist() == [11, 12, 13]
    assert restored["volume_basis"].tolist() == ["ticks"] * 3
    assert restored["source"].tolist() == ["kite"] * 3
    assert restored["receipt_at"].tolist() == ["2026-08-31T09:15:00"] * 3


def test_a_populated_incoming_value_still_wins():
    """Restoring must not clobber telemetry the incoming frame genuinely carries."""
    index = pd.date_range("2026-08-31 09:15", periods=2, freq="1min")
    existing = pd.DataFrame(
        {
            "tick_count": [11, 12],
            "volume_basis": "ticks",
            "source": "kite",
            "receipt_at": "old",
            "provider_time": "old",
        },
        index=index,
    )
    incoming = pd.DataFrame(
        {
            "tick_count": [4, 5],
            "volume_basis": "shares",
            "source": "kite",
            "receipt_at": "new",
            "provider_time": "new",
        },
        index=index,
    )

    restored = CandleBuilder._restore_candle_telemetry(incoming.copy(), existing)

    assert restored["tick_count"].tolist() == [4, 5]
    assert restored["volume_basis"].tolist() == ["shares", "shares"]
    assert restored["receipt_at"].tolist() == ["new", "new"]


@pytest.mark.parametrize("rows", [200, 1000])
def test_the_cost_does_not_scale_with_cells_written(rows):
    """The regression itself: cost per (row, field) rather than per field.

    Stated relative to the reference implementation measured in the same process, so a
    slow or loaded machine moves both numbers together. On pandas 3.0.2 the observed
    ratio is roughly 23x at 200 rows and 39x at 1000; 4x is a floor, not a target.
    """
    index = pd.date_range("2026-08-31 09:15", periods=rows, freq="1min")
    existing = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
            "tick_count": np.arange(1, rows + 1),
            "volume_basis": "ticks",
            "source": "kite",
            "receipt_at": "2026-08-31T09:15:00",
            "provider_time": "2026-08-31T09:15:00",
        },
        index=index,
    )
    incoming = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
            "tick_count": 0,
            "volume_basis": "",
            "source": "fyers_history",
            "receipt_at": "",
            "provider_time": "",
        },
        index=index,
    )

    def elapsed(function):
        function(incoming.copy(), existing.copy())  # warm
        best = float("inf")
        for _ in range(3):
            candidate = incoming.copy()
            started = time.perf_counter()
            function(candidate, existing.copy())
            best = min(best, time.perf_counter() - started)
        return best

    baseline = elapsed(reference_restore)
    current = elapsed(CandleBuilder._restore_candle_telemetry)

    assert current * 4 < baseline, (
        f"{rows} overlapping rows: {current*1000:.1f}ms against a per-cell baseline of "
        f"{baseline*1000:.1f}ms -- the per-cell write is back on the tick path"
    )
