"""The market has to be recorded beside the P&L, or no exit rule can be judged.

decision_intel already freezes pcr / oi_signal / adx / atr_pct / volume_ratio /
close_above_vwap_pct / kaufman_er and a dozen more, from a cache, at zero network cost.
It captures ONE instant, at exit. One instant cannot answer the only question worth
asking of it -- "did the case change while we held?" -- so a proposed exit rule keyed on
those values cannot be replayed against history before it ships. That is precisely why
the 2026-08-25 audit of a market-aware exit engine had to end in "unmeasurable".

Sizing came from measurement, not taste. PCR only CHANGES every ~99s per instrument
(median over 21 sessions, n=3,067; SENSEX 188s) against a ~103s median hold, so:

  * a 5s timer oversamples the fastest-moving chain value by roughly 20x, and
  * a row is only KEPT when the snapshot actually differs from the last one kept,

which means the typical position stores 2-3 rows and those rows are exactly the
transitions. An unfiltered timer would have stored the same numbers twenty times over.

The cost guarantee is the whole reason this is affordable: the exit loop runs ~10x a
second and its cycle already runs a median 598ms against a 1.0s budget. The snapshot is
passed as a CALLABLE and the timer is checked BEFORE it is invoked, so on the ~9 cycles
in 10 that are not due, building it costs nothing at all.
"""
from datetime import datetime, timedelta

import pytest
import pytz

from config.settings import get_settings
from trading.trade_manager import sample_intel_path

IST = pytz.timezone("Asia/Kolkata")


class _Holder:
    # instrument/timeframe are what Scanner._entry_intel_snapshot keys the intel cache
    # on; without them it returns {} and the wiring tests silently assert nothing.
    def __init__(self, age_seconds=0.0, instrument="BANKNIFTY", timeframe="5min"):
        self.entry_timestamp = datetime.now(IST) - timedelta(seconds=age_seconds)
        self.instrument = instrument
        self.timeframe = timeframe


def _snap(**kw):
    base = {"pcr": 1.10, "adx": 21.0, "oi_signal": "NEUTRAL"}
    base.update(kw)
    return base


# ── it records ───────────────────────────────────────────────────────────────

def test_the_first_sample_is_always_kept():
    h = _Holder()

    sample_intel_path(h, lambda: _snap())

    assert len(h.intel_path) == 1
    elapsed, snap = h.intel_path[0]
    assert elapsed >= 0.0
    assert snap["pcr"] == 1.10


def test_a_changed_snapshot_is_appended():
    h = _Holder(age_seconds=600)
    sample_intel_path(h, lambda: _snap(pcr=1.10))
    h.entry_timestamp -= timedelta(seconds=60)      # push the clock past the interval

    sample_intel_path(h, lambda: _snap(pcr=1.42))

    assert [s["pcr"] for _t, s in h.intel_path] == [1.10, 1.42]


def test_an_unchanged_snapshot_is_not_appended():
    """PCR changes every ~99s against a ~103s median hold. Without this the series would
    be twenty copies of the same numbers and the transitions would be buried."""
    h = _Holder(age_seconds=600)
    sample_intel_path(h, lambda: _snap())
    for _ in range(5):
        h.entry_timestamp -= timedelta(seconds=60)
        sample_intel_path(h, lambda: _snap())

    assert len(h.intel_path) == 1


# ── it costs nothing when it is not due ──────────────────────────────────────

def test_the_snapshot_is_not_built_when_the_timer_is_not_due():
    """The cost guarantee. The exit loop runs ~10x/second against a cycle already at a
    median 598ms of a 1.0s budget; building the snapshot on every pass is the one thing
    that would make this unaffordable."""
    h = _Holder()
    calls = []

    def counted():
        calls.append(1)
        return _snap()

    sample_intel_path(h, counted)          # due: first sample
    for _ in range(20):
        sample_intel_path(h, counted)      # not due: must not invoke the callable

    assert len(calls) == 1, f"snapshot built {len(calls)} times"
    assert len(h.intel_path) == 1


def test_the_timer_is_honoured_between_samples():
    s = get_settings()
    gap = float(s.intel_path_min_interval_seconds)
    h = _Holder(age_seconds=600)

    sample_intel_path(h, lambda: _snap(pcr=1.0))
    h.entry_timestamp -= timedelta(seconds=gap / 2.0)
    sample_intel_path(h, lambda: _snap(pcr=9.9))        # inside the window, changed
    assert len(h.intel_path) == 1, "sampled inside the minimum interval"

    h.entry_timestamp -= timedelta(seconds=gap * 2.0)
    sample_intel_path(h, lambda: _snap(pcr=9.9))
    assert len(h.intel_path) == 2


# ── bounds ───────────────────────────────────────────────────────────────────

def test_the_cap_keeps_both_ends_and_thins_the_middle():
    """Matches sample_pnl_path: a long position loses resolution, never its tail."""
    s = get_settings()
    cap = int(s.intel_path_max_samples)
    h = _Holder(age_seconds=100000)

    for n in range(cap + 5):
        h.entry_timestamp -= timedelta(seconds=60)
        sample_intel_path(h, lambda n=n: _snap(pcr=float(n)))

    first, last = h.intel_path[0], h.intel_path[-1]
    assert len(h.intel_path) <= cap + 1
    assert first[1]["pcr"] == 0.0, "the first sample was thinned away"
    assert last[1]["pcr"] == float(cap + 4), "the tail was thinned away"


# ── it can never cost a position ─────────────────────────────────────────────

def test_a_snapshot_that_raises_is_swallowed():
    h = _Holder()

    def exploding():
        raise RuntimeError("intel cache down")

    sample_intel_path(h, exploding)          # must not raise

    assert getattr(h, "intel_path", []) == []


@pytest.mark.parametrize("bad", [None, {}, "not-a-dict", 42])
def test_an_unusable_snapshot_is_ignored(bad):
    h = _Holder()

    sample_intel_path(h, lambda: bad)

    assert getattr(h, "intel_path", []) == []


def test_a_holder_with_no_entry_time_is_ignored():
    class _NoEntry:
        pass

    h = _NoEntry()
    sample_intel_path(h, lambda: _snap())

    assert getattr(h, "intel_path", None) in (None, [])


def test_it_is_off_when_the_setting_is_off(monkeypatch):
    import trading.trade_manager as tm
    from config.settings import Settings

    off = Settings()
    off.intel_path_logging_enabled = False
    monkeypatch.setattr(tm, "get_settings", lambda: off, raising=False)
    import config.settings as cs
    monkeypatch.setattr(cs, "get_settings", lambda: off)

    h = _Holder()
    sample_intel_path(h, lambda: _snap())

    assert getattr(h, "intel_path", []) == []


# ── wired to the real snapshot, and affordable ───────────────────────────────

REALISTIC_INTEL = {
    "regime": {"regime": "TRENDING_UP", "confidence": 0.71, "kaufman_er": 0.42,
               "adx": 24.3, "atr_pct": 0.31, "bb_width": 0.014},
    "volume": {"buy_sell_ratio": 1.18, "volume_ratio": 1.42},
    "oi": {"signal": "LONG_BUILDUP", "cumulative_analysis": {"trend": "UP"}},
    "pcr": 1.13,
    "order_flow": {"ratio": 0.62, "source": "ohlcv_close_position"},
    "aggregate": {"score": 0.38},
    "breadth_context": {"aligned_count": 3, "bias": "BULLISH"},
    "session_context": {"day_type": "TREND_DAY_UP", "confidence": 0.66,
                        "close_above_vwap_pct": 0.58},
    "greeks": {"call": {"iv": 14.2, "delta": 0.53}, "iv_percentile": 41.0},
}


def _scanner_with_intel(intel=REALISTIC_INTEL):
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc._cached_timeframe_intel = lambda *a, **k: intel
    return sc


def test_the_real_snapshot_reaches_the_path():
    """End to end through Scanner._entry_intel_snapshot, not a hand-made dict."""
    sc = _scanner_with_intel()
    h = _Holder()

    sample_intel_path(h, lambda: sc._entry_intel_snapshot(h))

    assert len(h.intel_path) == 1
    snap = h.intel_path[0][1]
    for field in ("pcr", "adx", "volume_ratio", "oi_signal", "kaufman_er",
                  "close_above_vwap_pct", "regime_label"):
        assert field in snap, f"{field} missing from the recorded snapshot"
    assert snap["pcr"] == 1.13
    assert snap["adx"] == 24.3


def test_a_moving_market_produces_a_series_a_replay_can_read():
    """The point of the whole change: PCR moving mid-trade has to be visible."""
    intel = {k: (dict(v) if isinstance(v, dict) else v) for k, v in REALISTIC_INTEL.items()}
    sc = _scanner_with_intel(intel)
    h = _Holder(age_seconds=600)

    for pcr in (1.13, 1.13, 1.31, 1.31, 0.94):
        intel["pcr"] = pcr
        h.entry_timestamp -= timedelta(seconds=30)
        sample_intel_path(h, lambda: sc._entry_intel_snapshot(h))

    assert [s["pcr"] for _t, s in h.intel_path] == [1.13, 1.31, 0.94], (
        "the series should hold the transitions, not one row per timer tick"
    )
    assert [t for t, _s in h.intel_path] == sorted(t for t, _s in h.intel_path)


def test_the_recorded_series_is_json_safe():
    """It is written into the session_signals payload, so it must serialise."""
    import json

    sc = _scanner_with_intel()
    h = _Holder()
    sample_intel_path(h, lambda: sc._entry_intel_snapshot(h))

    serialised = [
        [float(t), dict(snap)]
        for t, snap in (getattr(h, "intel_path", None) or [])
        if isinstance(snap, dict)
    ]
    round_tripped = json.loads(json.dumps(serialised))

    assert round_tripped[0][1]["pcr"] == 1.13


def test_it_is_cheap_enough_for_a_ten_hertz_loop():
    """The exit loop runs ~10x/second on a cycle already at a median 598ms of a 1.0s
    budget. Measured at 0.0013ms to skip and 0.011ms to build, this is ~0.005% of the
    budget at three open positions. The bar here is deliberately loose -- it is a
    regression guard against someone making the snapshot expensive, not a benchmark."""
    import time

    sc = _scanner_with_intel()
    h = _Holder()
    sample_intel_path(h, lambda: sc._entry_intel_snapshot(h))     # prime the timer

    n = 500
    start = time.perf_counter()
    for _ in range(n):
        sample_intel_path(h, lambda: sc._entry_intel_snapshot(h))
    per_call_ms = (time.perf_counter() - start) / n * 1000.0

    assert per_call_ms < 1.0, f"{per_call_ms:.3f}ms per not-due call is too expensive"


# ── it has to survive the two places pnl_path already survives ───────────────

def test_the_series_is_carried_onto_the_exit_row():
    """The EXIT row is the one written to session_signals. A series collected during the
    trade and not carried across is a series discarded at the only moment it mattered."""
    import inspect
    from scanner import Scanner

    src = inspect.getsource(Scanner)
    pnl_copy = src.find('setattr(exit_candidate, "pnl_path"')
    intel_copy = src.find('setattr(exit_candidate, "intel_path"')

    assert pnl_copy != -1, "the pnl_path copy site moved; re-point this test"
    assert intel_copy != -1, "intel_path is not carried onto the exit candidate"
    # Copied, not referenced -- a shared list would keep growing after the exit.
    window = src[intel_copy:intel_copy + 260]
    assert "dict(snap)" in window, "the snapshots are shared by reference, not copied"


def test_the_series_survives_a_mid_session_restart():
    """Restored for the same reason pnl_path is: a series that begins partway through
    reads as a market that never held conditions it did hold."""
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    payload = {
        "instrument": "BANKNIFTY", "timeframe": "5min", "direction": "LONG",
        "signal_timestamp": (datetime.now(IST) - timedelta(minutes=5)).isoformat(),
        "price": 200.0, "lots": 1, "lot_size": 35, "grade": "A", "confidence": 0.8,
        "pnl_path": [[0.0, 10.0], [30.0, 120.0]],
        "intel_path": [[0.0, {"pcr": 1.13, "adx": 24.3}],
                       [30.0, {"pcr": 1.31, "adx": 26.0}]],
    }

    candidate = sc._candidate_from_payload(payload)

    assert candidate is not None
    restored = getattr(candidate, "intel_path", [])
    assert [s["pcr"] for _t, s in restored] == [1.13, 1.31]
    assert all(isinstance(t, float) and isinstance(s, dict) for t, s in restored)


@pytest.mark.parametrize("bad_rows", [
    None, [], "nonsense", [[0.0]], [["x", {"pcr": 1.0}]], [[0.0, "not-a-dict"]],
])
def test_a_malformed_stored_series_does_not_break_the_restore(bad_rows):
    """A corrupt archive row must not stop a live position being restored."""
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    payload = {
        "instrument": "BANKNIFTY", "timeframe": "5min", "direction": "LONG",
        "signal_timestamp": (datetime.now(IST) - timedelta(minutes=5)).isoformat(),
        "price": 200.0, "lots": 1, "lot_size": 35, "grade": "A", "confidence": 0.8,
        "intel_path": bad_rows,
    }

    candidate = sc._candidate_from_payload(payload)

    assert candidate is not None, "a malformed intel_path lost the whole position"
