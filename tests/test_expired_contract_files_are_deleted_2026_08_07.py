"""Expired contract CSVs were row-pruned and then kept forever.

_prune_candles rewrites each file with rows newer than the retention cutoff. When a
weekly option expires no new rows ever arrive, so eventually every row fails the cutoff
and the file is written back holding nothing but its header. On the next run
`if df.empty: continue` skipped it — and the stub became immortal.

Measured on disk: 808 CSVs under data_store/candles/, including BANKNIFTY 26MAY26 strikes
that expired months ago. Every one is re-opened with pd.read_csv on each daily run, so the
cost of maintenance grows linearly with dead contracts. Across four indices with weekly
expiries that is several hundred new orphan files a year.

Only dated CONTRACT files are removed. An index file (NIFTY_5min.csv) with no rows is
empty for some other reason — fresh install, failed first fetch — and refills on the next
sync, so deleting it would only cost a re-download.
"""
import pandas as pd
import pytest

from data.data_manager import DataManager


def _manager(tmp_path):
    m = DataManager.__new__(DataManager)
    m.candles_dir = tmp_path
    # Retention lives on the instance, set in __init__, which __new__ skips. Without
    # these the prune raises AttributeError per file, the broad `except Exception`
    # swallows it, and every file survives — which looked exactly like the deletion
    # not working.
    m.RETENTION_1MIN = 30
    m.RETENTION_5MIN = 30
    m.RETENTION_15MIN = 65
    return m


def _write(tmp_path, name, rows):
    f = tmp_path / name
    if rows:
        idx = pd.date_range("2026-08-01 09:15", periods=len(rows), freq="5min")
        pd.DataFrame({"close": rows}, index=idx).to_csv(f)
    else:
        f.write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")
    return f


def test_an_expired_contract_stub_is_removed(tmp_path):
    """The live shape: a header-only file for a contract that expired months ago."""
    f = _write(tmp_path, "BANKNIFTY26MAY2653000CE_1min.csv", [])
    _manager(tmp_path)._prune_candles()
    assert not f.exists(), "the expired contract stub survived another prune"


def test_a_contract_whose_rows_all_aged_out_is_removed(tmp_path):
    """One run earlier: the rows are there but every one is past the cutoff."""
    f = tmp_path / "NIFTY26JUN2624500CE_5min.csv"
    old = pd.date_range("2025-01-02 09:15", periods=5, freq="5min")
    pd.DataFrame({"close": [1.0] * 5}, index=old).to_csv(f)
    _manager(tmp_path)._prune_candles()
    assert not f.exists()


def test_an_index_file_is_never_deleted(tmp_path):
    """Empty for some other reason, and it refills on the next sync."""
    f = _write(tmp_path, "NIFTY_5min.csv", [])
    _manager(tmp_path)._prune_candles()
    assert f.exists(), "an index candle file was deleted; it only needed a re-fetch"


def test_a_live_contract_with_recent_rows_is_kept(tmp_path):
    f = tmp_path / "NIFTY26AUG2624500CE_5min.csv"
    recent = pd.date_range(pd.Timestamp.now().floor("min") - pd.Timedelta(minutes=20),
                           periods=5, freq="5min")
    pd.DataFrame({"close": [1.0] * 5}, index=recent).to_csv(f)
    _manager(tmp_path)._prune_candles()
    assert f.exists(), "a live contract's file was deleted"


def test_a_contract_keeps_its_rows_inside_retention(tmp_path):
    f = tmp_path / "NIFTY26AUG2624500PE_5min.csv"
    recent = pd.date_range(pd.Timestamp.now().floor("min") - pd.Timedelta(minutes=20),
                           periods=4, freq="5min")
    pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=recent).to_csv(f)
    _manager(tmp_path)._prune_candles()
    assert len(pd.read_csv(f, index_col=0, parse_dates=True)) == 4


def test_an_unreadable_file_does_not_stop_the_sweep(tmp_path):
    """One bad file must not leave every later one unpruned."""
    bad = tmp_path / "NIFTY26MAY2624500CE_5min.csv"
    bad.write_text("this is not a csv\x00\x00", encoding="utf-8")
    good = _write(tmp_path, "BANKNIFTY26MAY2653000PE_1min.csv", [])
    _manager(tmp_path)._prune_candles()
    assert not good.exists(), "the sweep stopped at the unreadable file"


def test_a_missing_directory_is_not_an_error(tmp_path):
    m = DataManager.__new__(DataManager)
    m.candles_dir = tmp_path / "nope"
    m._prune_candles()


def test_deleting_is_tolerant_of_a_locked_file(tmp_path):
    """Windows can hold a handle open; that must not raise out of the sweep."""
    m = _manager(tmp_path)

    class _Stubborn:
        stem = "NIFTY26MAY2624500CE_5min"
        name = "NIFTY26MAY2624500CE_5min.csv"

        def unlink(self):
            raise OSError("in use by another process")

    m._delete_dead_candle_file(_Stubborn())


@pytest.mark.parametrize("name,should_delete", [
    # monthly-style expiries
    ("BANKNIFTY26MAY2653000CE_1min", True),
    ("NIFTY26AUG2624500PE_5min", True),
    ("SENSEX26AUG2680000CE_15min", True),
    # WEEKLY-style expiries have no three-letter month. A first version of this fix
    # matched only the monthly pattern and silently kept every weekly: checked against
    # the real directory, 200 of 483 dead files survived, all of them weeklies.
    ("SENSEX2660474100PE_1min", True),
    ("NIFTY2680724500CE_5min", True),
    ("MIDCPNIFTY2681213000PE_15min", True),
    # index spot files, which refill on the next sync
    ("NIFTY_5min", False),
    ("BANKNIFTY_1min", False),
    ("MIDCPNIFTY_15min", False),
    ("SENSEX_1min", False),
])
def test_only_dated_contract_names_are_eligible(tmp_path, name, should_delete):
    f = _write(tmp_path, f"{name}.csv", [])
    _manager(tmp_path)._prune_candles()
    assert f.exists() is (not should_delete)


def test_the_timeframe_suffix_is_not_mistaken_for_a_strike(tmp_path):
    """"5min" and "15min" contain digits; only the SYMBOL may make it a contract."""
    for name in ("NIFTY_5min", "BANKNIFTY_15min", "SENSEX_1min"):
        f = _write(tmp_path, f"{name}.csv", [])
        _manager(tmp_path)._prune_candles()
        assert f.exists(), f"{name} was deleted because its timeframe has a digit in it"


def test_a_weekly_contract_stub_is_removed(tmp_path):
    """The 200 files the first version of this fix missed."""
    f = _write(tmp_path, "SENSEX2660474100PE_1min.csv", [])
    _manager(tmp_path)._prune_candles()
    assert not f.exists()
