"""Per-index scan overruns, 2026-09-23: 431 "exceeded the 9s per-index budget" warnings.

py-spy on the live process (wall clock, idle threads included) put the per-index scan time
here, none of it CPU work:

    21.7%  waiting on CandleBuilder._lock in get_candles, behind the tick thread's resample
    10.8%  boolean date-mask slicing in Scanner._live_analysis_window (pyarrow take)
    ~9%    ExpiryManager.in_gamma_window re-filtering the 168k-row instrument master
     2.0%  boolean slicing in pre_candle_predictor._slice_from

Each rewrite below is a performance change only. Every test pins it against the original
implementation, kept verbatim here as the reference, on randomized inputs.
"""
import random
import threading
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from data.candle_builder import CandleBuilder
from engine import pre_candle_predictor
from scanner import Scanner

WARMUP = {"1min": 180, "5min": 60, "15min": 40}
FALLBACK = {"1min": 360, "5min": 120, "15min": 80}


def _reference_live_window(df, timeframe, session_day):
    """The pre-2026-09-23 REAL-mode path, verbatim."""
    idx_dates = pd.Series(df.index.date, index=df.index)
    today_df = df[idx_dates == session_day]
    warmup_bars = WARMUP.get(timeframe, 60)
    fallback_bars = FALLBACK.get(timeframe, 120)
    if today_df.empty:
        return df.tail(fallback_bars)
    warmup_df = df[df.index < today_df.index[0]].tail(warmup_bars)
    window = pd.concat([warmup_df, today_df])
    return window[~window.index.duplicated(keep="last")].sort_index()


def _frame(days, bars_per_day, tf_minutes, seed):
    rng = np.random.default_rng(seed)
    stamps = []
    for d in days:
        start = pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15)
        stamps.extend(start + pd.Timedelta(minutes=tf_minutes * i) for i in range(bars_per_day))
    n = len(stamps)
    close = 100 + rng.normal(0, 1, n).cumsum()
    return pd.DataFrame(
        {
            "open": close, "high": close + 1, "low": close - 1, "close": close,
            "volume": rng.integers(1, 10_000, n).astype(float),
            "volume_basis": pd.array(rng.choice(["fyers_index", ""], n), dtype="string"),
        },
        index=pd.DatetimeIndex(stamps),
    )


def _scanner(session_day):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_current_session_day = lambda: session_day
    return scanner


@pytest.mark.parametrize("seed", range(40))
def test_live_window_fast_path_matches_the_original(seed):
    rnd = random.Random(seed)
    tf = rnd.choice(["1min", "5min", "15min"])
    tf_minutes = int(tf[:-3])
    base = date(2026, 9, 1)
    days = sorted({base + timedelta(days=rnd.randint(0, 20)) for _ in range(rnd.randint(1, 6))})
    bars = rnd.randint(1, 375 // tf_minutes)
    df = _frame(days, bars, tf_minutes, seed)
    # Sometimes ask for a day that is present, sometimes one that is not.
    session_day = rnd.choice(days + [base + timedelta(days=25), base - timedelta(days=3)])

    got = Scanner._live_analysis_window(_scanner(session_day), df, tf)
    want = _reference_live_window(df, tf, session_day)

    pd.testing.assert_frame_equal(got, want)


def test_live_window_keeps_the_original_path_for_an_unsorted_frame():
    df = _frame([date(2026, 9, 21), date(2026, 9, 22)], 30, 5, 1).iloc[::-1]

    got = Scanner._live_analysis_window(_scanner(date(2026, 9, 22)), df, "5min")
    want = _reference_live_window(df, "5min", date(2026, 9, 22))

    pd.testing.assert_frame_equal(got, want)


@pytest.mark.parametrize("seed", range(20))
def test_slice_from_matches_the_mask(seed):
    rnd = random.Random(seed)
    df = _frame([date(2026, 9, 22)], rnd.randint(1, 300), 1, seed)
    start = df.index[0] + pd.Timedelta(minutes=rnd.randint(-5, len(df) + 5), seconds=rnd.choice([0, 30]))

    got = pre_candle_predictor._slice_from(df, start)
    want = df[df.index >= start].copy()

    pd.testing.assert_frame_equal(got, want)


# ── in_gamma_window: the per-day cache must not change the answer ────────────


class _Master:
    """A minimal ExpiryManager-shaped object over a synthetic instrument master."""


def _gamma_subject(monkeypatch, expiries):
    from engine.expiry_manager import ExpiryManager

    monkeypatch.setattr(
        "config.settings.get_settings",
        lambda: type("S", (), {"expiry_day_trade_current_week": True})(),
    )
    em = ExpiryManager.__new__(ExpiryManager)
    rows = []
    for name, exps in expiries.items():
        for e in exps:
            rows.append({"name": name, "instrumenttype": "OPTIDX", "expiry_dt": pd.Timestamp(e)})
    rows.append({"name": "NIFTY", "instrumenttype": "FUTIDX", "expiry_dt": pd.Timestamp("2026-09-29")})
    em.df = pd.DataFrame(rows).astype({"name": "string", "instrumenttype": "string"})
    em._ensure_expiry_dates = lambda: None
    return em


def test_gamma_window_cache_returns_the_uncached_answer(monkeypatch):
    em = _gamma_subject(monkeypatch, {
        "NIFTY": ["2026-09-22", "2026-09-29", "2026-10-06"],
        "BANKNIFTY": ["2026-09-24", "2026-09-30", "2026-10-28"],
    })
    seen = []
    em.expiry_day_override = lambda cur, nxt, td, now: seen.append((cur, nxt, td)) or "current"

    for name in ("NIFTY", "BANKNIFTY"):
        for when in (datetime(2026, 9, 22, 10), datetime(2026, 9, 22, 14), datetime(2026, 9, 23, 10)):
            em.__dict__.pop("_gamma_expiry_cache", None)
            cold = em.in_gamma_window(name, when)
            cold_args = seen[-1]
            warm = em.in_gamma_window(name, when)
            assert warm == cold
            assert seen[-1] == cold_args, f"{name} {when}: cached expiries differ from a cold read"


def test_gamma_window_cache_misses_when_the_master_is_reloaded(monkeypatch):
    em = _gamma_subject(monkeypatch, {"NIFTY": ["2026-09-22", "2026-09-29"]})
    seen = []
    em.expiry_day_override = lambda cur, nxt, td, now: seen.append(cur) or "current"
    when = datetime(2026, 9, 22, 10)

    em.in_gamma_window("NIFTY", when)
    em.df = _gamma_subject(monkeypatch, {"NIFTY": ["2026-09-23", "2026-09-30"]}).df
    em.in_gamma_window("NIFTY", when)

    assert seen == [pd.Timestamp("2026-09-22"), pd.Timestamp("2026-09-23")]


# ── lock-free candle reads ────────────────────────────────────────────────────


def _builder():
    return CandleBuilder.__new__(CandleBuilder)


def test_readers_return_the_published_frames():
    b = _builder()
    b._lock = threading.RLock()
    one = _frame([date(2026, 9, 23)], 10, 1, 3)
    five = _frame([date(2026, 9, 23)], 4, 5, 4)
    b._candles_1min = {"NIFTY": one}
    b._candles_5min = {"NIFTY": five}
    b._candles_15min = {}

    pd.testing.assert_frame_equal(b.get_candles("NIFTY", "1min"), one)
    pd.testing.assert_frame_equal(b.get_candles("NIFTY", "5min"), five)
    assert b.get_candles("NIFTY", "15min") is None
    assert b.get_candles("NIFTY", "60min") is None
    assert b.latest_candle_time("NIFTY", "1min") == one.index[-1]
    assert b.latest_candle_time("NIFTY", "60min") is None
    assert b.get_candle_count("NIFTY", "5min") == 4
    assert b.get_candle_count("SENSEX", "5min") == 0


def test_readers_never_wait_on_the_writer_lock():
    """The 21.7%: a reader must return while a writer holds the lock."""
    b = _builder()
    b._lock = threading.Lock()
    b._candles_1min = {"NIFTY": _frame([date(2026, 9, 23)], 5, 1, 5)}
    b._candles_5min = {}
    b._candles_15min = {}
    done = threading.Event()

    with b._lock:  # simulate the tick thread mid-resample
        t = threading.Thread(target=lambda: (b.get_candles("NIFTY", "1min"), done.set()))
        t.start()
        finished = done.wait(timeout=2.0)
    t.join()

    assert finished, "get_candles blocked behind the writer lock"


def test_a_reader_always_sees_one_complete_frame_while_writers_replace_entries():
    b = _builder()
    b._lock = threading.RLock()
    frames = [_frame([date(2026, 9, 23)], n, 1, n) for n in range(5, 60)]
    b._candles_1min = {"NIFTY": frames[0]}
    b._candles_5min = {}
    b._candles_15min = {}
    stop = threading.Event()
    bad = []

    def writer():
        i = 0
        while not stop.is_set():
            with b._lock:
                b._candles_1min["NIFTY"] = frames[i % len(frames)]
            i += 1

    def reader():
        while not stop.is_set():
            df = b.get_candles("NIFTY", "1min")
            if df is None or len(df) not in {len(f) for f in frames} or not df.index.is_monotonic_increasing:
                bad.append(df)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    threading.Event().wait(0.5)
    stop.set()
    for t in threads:
        t.join()

    assert not bad


def test_a_published_frame_is_never_mutated_by_a_later_update():
    """The invariant the lock-free read rests on."""
    b = CandleBuilder()
    first = _frame([date(2026, 9, 23)], 20, 1, 9)
    b.update_candles("NIFTY", first, "1min", mark_receipt=False)
    held = b._candles_1min["NIFTY"]
    snapshot = held.copy()

    later = first.tail(3).copy()
    later["close"] = later["close"] + 50.0
    later["volume"] = 0.0
    b.update_candles("NIFTY", later, "1min", mark_receipt=False)

    pd.testing.assert_frame_equal(held, snapshot)
    assert b._candles_1min["NIFTY"] is not held
