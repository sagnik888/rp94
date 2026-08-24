"""Mixed timezone objects in the candle cache froze the dashboard.

2026-08-19, repeatedly:

    ERROR | scanner:process_task:6791 - Error processing NIFTY: Invalid comparison
    between dtype=datetime64[us, tzoffset(None, 19800)] and Timestamp

`process_task` catches that and returns `{"error": ...}` for the index, which the
dashboard renders as unknown data — so a comparison bug looks like a frozen system.

Two flavours appeared in one log, both meaning "+05:30" and neither comparable with a
naive Timestamp:

    datetime64[us, UTC+05:30]              10 occurrences
    datetime64[us, tzoffset(None, 19800)]   8 occurrences

Every live producer already ends in `.tz_localize(None)`, so candles in memory are
naive. The cache is where the tz-aware ones come from: `parse_dates=True` on a CSV
whose timestamps carry a `+05:30` suffix builds a dateutil `tzoffset`. Merge that with
a naive live frame and every later `df.index <= as_of` raises.

Timing corroborates the read path exactly. All 18 occurrences in the log fall within
16-501 seconds of a restart, and none outside one — that is when the cache is read and
merged. Restarts BEFORE any of this session's edits errored identically, so this is a
long-standing latent bug rather than a regression.
"""
import pandas as pd
import pytest

from data.market_data import MarketDataProvider


def _frame(tz=None, n=5):
    idx = pd.date_range("2026-08-19 09:15", periods=n, freq="1min")
    if tz is not None:
        idx = idx.tz_localize(tz)
    return pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}, index=idx
    )


def _norm(df):
    return MarketDataProvider._naive_kolkata_index(df)


# ── the normaliser ───────────────────────────────────────────────────────────

def test_a_naive_frame_is_untouched():
    df = _frame()
    out = _norm(df)
    assert out.index.tz is None
    assert list(out.index) == list(df.index)


@pytest.mark.parametrize("tz", ["Asia/Kolkata", "UTC", "America/New_York"])
def test_any_aware_frame_comes_back_naive(tz):
    out = _norm(_frame(tz=tz))
    assert out.index.tz is None


def test_the_dateutil_tzoffset_flavour_is_handled():
    """The exact dtype from the log: parse_dates=True on a '+05:30' CSV."""
    from dateutil.tz import tzoffset

    df = _frame(tz=tzoffset(None, 19800))
    assert df.index.tz is not None
    out = _norm(df)
    assert out.index.tz is None


def test_the_wall_clock_is_preserved_for_kolkata():
    """A +05:30 frame must keep its local reading, not shift by 5.5 hours."""
    df = _frame(tz="Asia/Kolkata")
    out = _norm(df)
    assert str(out.index[0]) == "2026-08-19 09:15:00"


def test_a_utc_frame_is_converted_not_merely_stripped():
    """09:15 UTC is 14:45 in Kolkata. Stripping without converting would silently
    move every candle 5.5 hours and corrupt the cache."""
    out = _norm(_frame(tz="UTC"))
    assert str(out.index[0]) == "2026-08-19 14:45:00"


def test_a_frame_it_cannot_normalise_is_returned_intact():
    """Defensive tidy-up on a read path must never lose candles."""
    df = pd.DataFrame({"close": [1, 2, 3]}, index=[0, 1, 2])
    out = _norm(df)
    assert len(out) == 3


# ── the failure this prevents ────────────────────────────────────────────────

def test_the_reported_comparison_now_succeeds():
    """Reproduces scanner:process_task's slice, which is what actually raised."""
    from dateutil.tz import tzoffset

    aware = _frame(tz=tzoffset(None, 19800))
    as_of = pd.Timestamp("2026-08-19 09:17")

    with pytest.raises(TypeError):
        _ = aware[aware.index <= as_of]          # the live failure

    fixed = _norm(aware)
    assert len(fixed[fixed.index <= as_of]) == 3  # same slice, no raise


def test_a_cached_frame_and_a_live_frame_can_be_concatenated():
    """The merge in _save_and_merge_candles: cache (aware) + live (naive)."""
    from dateutil.tz import tzoffset

    cached = _norm(_frame(tz=tzoffset(None, 19800), n=3))
    live = _frame(n=3)
    merged = pd.concat([cached, live])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    assert merged.index.tz is None
    # And the merged frame is still sliceable against a naive Timestamp.
    assert len(merged[merged.index <= pd.Timestamp("2026-08-19 09:16")]) == 2


# ── wiring ───────────────────────────────────────────────────────────────────

def test_both_cache_read_sites_normalise():
    """A normaliser that only covers one of the two read paths leaves the bug live."""
    import inspect

    src = inspect.getsource(MarketDataProvider)
    reads = src.count("pd.read_csv(cache_file, index_col=0, parse_dates=True)")
    guarded = src.count("self._naive_kolkata_index(\n                    pd.read_csv(cache_file, index_col=0, parse_dates=True)\n                )")
    assert reads >= 2, f"expected the two cache reads, found {reads}"
    assert guarded == reads, (
        f"{reads} cache reads but only {guarded} normalised — an unguarded read "
        f"reintroduces the mixed-dtype merge"
    )
