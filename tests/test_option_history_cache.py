from datetime import date
from types import SimpleNamespace

import pandas as pd

from engine.option_history_store import OptionHistoryStore
from engine.signal_processor import SignalProcessor


def _fetch_or_skip_settings():
    return SimpleNamespace(
        ut_option_history_mode="fetch_or_skip",
        option_history_miss_ttl_seconds=900.0,
    )


def test_option_contract_history_is_fetched_once_for_multiple_trade_days(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "engine.signal_processor.get_settings", _fetch_or_skip_settings
    )
    calls = []
    index = pd.to_datetime([
        "2026-06-11 09:15:00",
        "2026-06-12 09:15:00",
    ])
    history = pd.DataFrame(
        {"open": [100.0, 110.0], "high": [101.0, 111.0],
         "low": [99.0, 109.0], "close": [100.5, 110.5]},
        index=index,
    )
    data = SimpleNamespace(
        get_option_token=lambda *args, **kwargs: {
            "symbol": "NIFTY26JUNTESTCE",
            "token": "123",
            "exch_seg": "NFO",
        },
        get_historical_candles=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or history
        ),
    )
    scanner = SimpleNamespace(
        data=data,
        backtest_days=30,
        _diag_inc=lambda *args, **kwargs: None,
    )
    processor = SignalProcessor(scanner)

    first, _, _ = processor._load_option_candles(
        scanner, "NIFTY", 23000, "CE", "1min", date(2026, 6, 11)
    )
    second, _, _ = processor._load_option_candles(
        scanner, "NIFTY", 23000, "CE", "1min", date(2026, 6, 12)
    )

    assert len(calls) == 1
    assert first.index[0].date() == date(2026, 6, 11)
    assert second.index[0].date() == date(2026, 6, 12)


def test_recent_empty_contract_fetch_is_not_repeated_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "engine.signal_processor.get_settings", _fetch_or_skip_settings
    )
    calls = []
    symbol = "NIFTY26JUNEMPTYCE"
    data = SimpleNamespace(
        get_option_token=lambda *args, **kwargs: {
            "symbol": symbol,
            "token": "999",
            "exch_seg": "NFO",
        },
        get_historical_candles=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or pd.DataFrame()
        ),
    )
    scanner = SimpleNamespace(
        data=data,
        backtest_days=30,
        _diag_inc=lambda *args, **kwargs: None,
    )
    miss_path = tmp_path / "misses.json"

    first = SignalProcessor(scanner)
    first._option_history_store = OptionHistoryStore(miss_path=miss_path)
    first._load_option_candles(
        scanner, "NIFTY", 23000, "CE", "1min", date.today()
    )

    restarted = SignalProcessor(scanner)
    restarted._option_history_store = OptionHistoryStore(miss_path=miss_path)
    restarted._load_option_candles(
        scanner, "NIFTY", 23000, "CE", "1min", date.today()
    )

    assert len(calls) == 1


def test_existing_contract_without_requested_day_fetches_verified_history_once(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "engine.signal_processor.get_settings", _fetch_or_skip_settings
    )
    monkeypatch.chdir(tmp_path)
    cache_dir = tmp_path / "data_store" / "candles"
    cache_dir.mkdir(parents=True)
    symbol = "NIFTY26JUNPARTIALCE"
    pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5]},
        index=pd.to_datetime(["2026-06-10 09:15:00"]),
    ).to_csv(cache_dir / f"{symbol}_1min.csv")
    calls = []
    diagnostics = []
    data = SimpleNamespace(
        get_option_token=lambda *args, **kwargs: {
            "symbol": symbol,
            "token": "321",
            "exch_seg": "NFO",
        },
        get_historical_candles=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or pd.DataFrame()
        ),
    )
    scanner = SimpleNamespace(
        data=data,
        backtest_days=30,
        _diag_inc=lambda *args: diagnostics.append(args),
    )
    processor = SignalProcessor(scanner)
    processor._option_history_store = OptionHistoryStore(
        miss_path=tmp_path / "misses.json",
        bundle_path=tmp_path / "bundle.parquet",
    )

    frame, _, _ = processor._load_option_candles(
        scanner, "NIFTY", 23000, "CE", "1min", date(2026, 6, 11)
    )

    assert frame.empty
    assert len(calls) == 1
    assert ("option_history", "attempts") in diagnostics
    assert ("option_history", "misses") in diagnostics


def test_historical_lookup_uses_archived_contract_with_matching_trade_day(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "engine.signal_processor.get_settings", _fetch_or_skip_settings
    )
    cache_dir = tmp_path / "data_store" / "candles"
    cache_dir.mkdir(parents=True)
    symbol = "NIFTY26MAY2624000CE"
    pd.DataFrame(
        {"open": [100.0], "high": [102.0], "low": [99.0], "close": [101.0]},
        index=pd.to_datetime(["2026-05-21 09:15:00"]),
    ).to_csv(cache_dir / f"{symbol}_1min.csv")
    diagnostics = []
    data = SimpleNamespace(
        get_option_token=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("live instrument master must not be used")
        ),
    )
    scanner = SimpleNamespace(
        data=data,
        backtest_days=30,
        _diag_inc=lambda *args: diagnostics.append(args),
    )
    processor = SignalProcessor(scanner)
    processor._option_history_store = OptionHistoryStore(
        miss_path=tmp_path / "misses.json",
        bundle_path=tmp_path / "bundle.parquet",
    )

    frame, resolved_symbol, token = processor._load_option_candles(
        scanner, "NIFTY", 24000, "CE", "1min", date(2026, 5, 21)
    )

    assert not frame.empty
    assert resolved_symbol == symbol
    assert token == ""
    assert ("option_history", "archived_hits") in diagnostics
