"""No 1-minute trade has ever fired, live or in backtest. This is why.

`_candle_thrust_metrics` computes the surge as `last["volume"] / prior_mean`, and the
2.2x floor it feeds is — per the code's own comment — "the only route a 1min signal has
into a live entry".

`_surge_prior_mean` was hardened once already: zero-volume bars were being averaged into
the DENOMINATOR, dragging it down and fabricating 4x and 10x surges. Excluding them was
correct. But the NUMERATOR was never guarded, and a cash index publishes no traded volume
in real time — it arrives only with the periodic Fyers backfill. Measured on the stored
candles:

    timeframe    zero-volume bars in the last 20
      1min                 13 / 20
      5min                  1 / 20
     15min                  0 / 20

A fresh 1min signal sits exactly in that dead zone, so it computed 0 / prior_mean =
0.00x and could never clear 2.2x. The fix for the false positive created a permanent
false negative — and it is confined to 1min, which is precisely the lane that never
produced a trade while 5min and 15min traded normally.

0.00x is a DEFINITE claim ("no surge") assembled from ABSENT data — this codebase's
cardinal error, the same shape as the empty scan cache reading as a repaint. Unmeasurable
must read as unmeasurable, so the metric now abstains.

The tick_count fallback originally shipped off. It is now enabled after replacing the
incompatible fixed traded-volume ratio with a calibrated, basis-independent percentile
floor; missing tick evidence still abstains.
"""
import pandas as pd
import pytest

from scanner import Scanner


@pytest.fixture(autouse=True)
def _pinned():
    from config.settings import get_settings

    s = get_settings()
    saved = getattr(s, "surge_tick_proxy_enabled", False)
    yield s
    s.surge_tick_proxy_enabled = saved


def _frame(last_volume, *, prior_volume=1000.0, bars=25, ticks=None, last_ticks=None):
    idx = pd.date_range("2026-08-11 09:15", periods=bars, freq="min")
    df = pd.DataFrame({
        "open": [100.0] * bars, "high": [102.0] * bars,
        "low": [99.0] * bars, "close": [101.5] * bars,
        "volume": [prior_volume] * (bars - 1) + [last_volume],
    }, index=idx)
    if ticks is not None:
        df["tick_count"] = [ticks] * (bars - 1) + [last_ticks if last_ticks is not None else ticks]
    return df


def _metrics(df, sc=None):
    sc = sc or Scanner.__new__(Scanner)
    sc.candles = type("C", (), {"get_candles": staticmethod(lambda i, t: df)})()
    return Scanner._candle_thrust_metrics(sc, "NIFTY", "1min", "BUY")


# ── the bug ──────────────────────────────────────────────────────────────────

def test_an_unmeasurable_signal_bar_abstains_instead_of_scoring_zero():
    m = _metrics(_frame(last_volume=0.0))
    assert m is None, f"absent volume still produced a definite reading: {m}"


def test_a_real_surge_is_still_measured():
    m = _metrics(_frame(last_volume=3000.0))
    assert m is not None
    assert m["volume_ratio"] == pytest.approx(3.0)
    assert m["volume_basis"] == "TRADED"


def test_an_ordinary_bar_is_still_measured():
    m = _metrics(_frame(last_volume=1000.0))
    assert m is not None and m["volume_ratio"] == pytest.approx(1.0)


def test_the_basis_is_reported_so_a_gate_can_tell_them_apart():
    assert _metrics(_frame(last_volume=2500.0))["volume_basis"] == "TRADED"


# ── the calibrated tick fallback ─────────────────────────────────────────────

def test_the_tick_proxy_is_now_calibrated_and_on(_pinned):
    """Superseded within the same night. This shipped OFF while its threshold was
    uncalibrated; the follow-up cut a basis-independent floor from the real candles
    (percentile rank, matched to the traded 2.2x strictness) and switched it on.
    See test_surge_is_basis_independent_2026_08_11.py."""
    assert bool(getattr(_pinned, "surge_tick_proxy_enabled", False)) is True
    m = _metrics(_frame(last_volume=0.0, ticks=100, last_ticks=400))
    assert m is not None and m["volume_basis"] == "TICK_ACTIVITY"


def test_the_tick_proxy_measures_participation_when_enabled(_pinned):
    _pinned.surge_tick_proxy_enabled = True
    m = _metrics(_frame(last_volume=0.0, ticks=100, last_ticks=400))
    assert m is not None
    assert m["volume_ratio"] == pytest.approx(4.0)
    assert m["volume_basis"] == "TICK_ACTIVITY"


def test_the_tick_proxy_still_abstains_without_tick_data(_pinned):
    _pinned.surge_tick_proxy_enabled = True
    assert _metrics(_frame(last_volume=0.0)) is None


def test_the_tick_proxy_excludes_zero_bars_like_the_volume_path(_pinned):
    """Averaging zero bars into the denominator is what fabricated 4x/10x surges."""
    _pinned.surge_tick_proxy_enabled = True
    df = _frame(last_volume=0.0, ticks=100, last_ticks=200)
    df.loc[df.index[:18], "tick_count"] = 0          # only 6 measured prior bars remain
    m = _metrics(df)
    assert m is not None and m["volume_ratio"] == pytest.approx(2.0)


def test_the_tick_proxy_needs_five_measured_bars(_pinned):
    _pinned.surge_tick_proxy_enabled = True
    df = _frame(last_volume=0.0, ticks=100, last_ticks=200)
    df.loc[df.index[:21], "tick_count"] = 0          # fewer than 5 measured priors
    assert _metrics(df) is None


# ── the guard that already existed must be untouched ─────────────────────────

def test_the_denominator_guard_still_rejects_a_thin_window():
    """_surge_prior_mean requires 5 measured prior bars; zeros there fabricated surges."""
    df = _frame(last_volume=3000.0)
    df.loc[df.index[:21], "volume"] = 0.0
    assert _metrics(df) is None


def test_a_degenerate_zero_range_candle_is_still_rejected():
    df = _frame(last_volume=3000.0)
    df.loc[df.index[-1], ["high", "low", "close"]] = [100.0, 100.0, 100.0]
    assert _metrics(df) is None


# ── the premise, recorded ────────────────────────────────────────────────────

def test_the_dead_zone_is_a_1min_phenomenon():
    """If 5min/15min ever start showing the same zero-volume edge, this abstention
    would silence them too and the tick proxy becomes urgent rather than optional."""
    import os

    import pandas as _pd

    f = "data_store/candles/NIFTY_5min.csv"
    if not os.path.exists(f):
        pytest.skip("no stored candles in this checkout")
    v = _pd.read_csv(f)["volume"].astype(float)
    assert int((v.tail(20) == 0).sum()) <= 3, (
        "the 5min right edge has gone dark too; 5min entries are now at risk"
    )
