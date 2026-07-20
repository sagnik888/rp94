from datetime import date

import pandas as pd

from engine.option_history_store import OptionHistoryStore


def _write_history(path, rows=6):
    index = pd.date_range("2026-06-11 09:15:00", periods=rows, freq="6h")
    pd.DataFrame(
        {
            "open": range(rows),
            "high": range(1, rows + 1),
            "low": range(rows),
            "close": range(1, rows + 1),
            "volume": range(rows),
        },
        index=index,
    ).to_csv(path)


def test_option_history_store_reuses_valid_bundle_without_csv_parse(
    tmp_path, monkeypatch
):
    csv_path = tmp_path / "NIFTYTESTCE_1min.csv"
    _write_history(csv_path)
    bundle_path = tmp_path / "bundle.parquet"

    first_store = OptionHistoryStore(bundle_path=bundle_path)
    result = first_store.ensure_bundle(tmp_path)
    first = first_store.load(csv_path)
    assert len(first) == 6
    assert result["rebuilt"] is True
    assert bundle_path.exists()

    second_store = OptionHistoryStore(bundle_path=bundle_path)
    monkeypatch.setattr(
        pd,
        "read_csv",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("valid sidecar should avoid CSV parsing")
        ),
    )
    result = second_store.ensure_bundle(tmp_path)
    second = second_store.load(csv_path)

    assert result["rebuilt"] is False
    pd.testing.assert_frame_equal(first, second)


def test_option_history_store_rebuilds_bundle_when_csv_changes(
    tmp_path, monkeypatch
):
    csv_path = tmp_path / "NIFTYTESTPE_1min.csv"
    bundle_path = tmp_path / "bundle.parquet"
    _write_history(csv_path, rows=4)
    OptionHistoryStore(bundle_path=bundle_path).ensure_bundle(tmp_path)

    _write_history(csv_path, rows=8)
    calls = []
    real_read_csv = pd.read_csv
    monkeypatch.setattr(
        pd,
        "read_csv",
        lambda *args, **kwargs: (
            calls.append(args[0]) or real_read_csv(*args, **kwargs)
        ),
    )

    store = OptionHistoryStore(bundle_path=bundle_path)
    result = store.ensure_bundle(tmp_path)
    refreshed = store.load(csv_path)

    assert result["rebuilt"] is True
    assert len(calls) == 1
    assert len(refreshed) == 8


def test_option_history_day_filter_uses_sorted_index_slice():
    index = pd.date_range(
        "2026-06-11 09:15:00",
        "2026-06-12 15:30:00",
        freq="15min",
        tz="Asia/Kolkata",
    )
    frame = pd.DataFrame({"close": range(len(index))}, index=index)

    selected = OptionHistoryStore.filter_day(frame, date(2026, 6, 12))

    assert not selected.empty
    assert {timestamp.date() for timestamp in selected.index} == {
        date(2026, 6, 12)
    }


def test_option_history_miss_ledger_survives_new_store(tmp_path, monkeypatch):
    miss_path = tmp_path / "misses.json"
    monkeypatch.setattr("engine.option_history_store.time.time", lambda: 1000.0)
    OptionHistoryStore(miss_path=miss_path).record_miss(
        "NIFTYTESTCE", "1min", 90
    )

    restarted = OptionHistoryStore(miss_path=miss_path)
    assert restarted.has_recent_miss("NIFTYTESTCE", "1min", 90, 900)

    monkeypatch.setattr("engine.option_history_store.time.time", lambda: 2001.0)
    assert not restarted.has_recent_miss("NIFTYTESTCE", "1min", 90, 900)
