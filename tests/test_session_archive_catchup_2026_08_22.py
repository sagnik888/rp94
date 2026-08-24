import json
import inspect
from datetime import datetime, time as dt_time

import pandas as pd

import main
from data.market_data import MarketDataProvider


def _event(root, day):
    path = root / "data_store" / "rtracker" / "events" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")


def _manifest(root, day, *, missing=0):
    path = root / "data_store" / "session_archive" / day / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "events": 1,
                "traded_symbols_recorded": ["NIFTY26AUG24000CE"],
                "contract_files_archived": 1,
                "contract_symbols_archived": ["NIFTY26AUG24000CE"],
                "missing_contract_symbols": [],
                "spot_files_archived": 12,
                "contract_files_without_day_bars": missing,
            }
        ),
        encoding="utf-8",
    )


def test_manifest_completion_accepts_list_and_legacy_numeric_missing_counts(tmp_path):
    _manifest(tmp_path, "2026-08-18", missing=[])
    assert main._session_archive_manifest_complete(tmp_path, "2026-08-18") is True

    _manifest(tmp_path, "2026-08-18", missing=["NIFTY26AUG24000PE"])
    assert main._session_archive_manifest_complete(tmp_path, "2026-08-18") is False

    _manifest(tmp_path, "2026-08-18", missing=0)
    assert main._session_archive_manifest_complete(tmp_path, "2026-08-18") is True


def test_duplicate_files_cannot_hide_a_missing_traded_symbol(tmp_path):
    day = "2026-08-18"
    path = tmp_path / "data_store" / "session_archive" / day / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "events": 1,
                "traded_symbols_recorded": ["A", "B"],
                # A has two cached timeframe files while B has none. The old count
                # comparison accepted this as 2/2 coverage.
                "contract_files_archived": 2,
                "contract_symbols_archived": ["A"],
                "missing_contract_symbols": ["B"],
                "spot_files_archived": 12,
                "contract_files_without_day_bars": 0,
            }
        ),
        encoding="utf-8",
    )

    assert main._session_archive_manifest_complete(tmp_path, day) is False


def test_count_only_manifest_with_contract_identity_fails_closed(tmp_path):
    day = "2026-08-18"
    path = tmp_path / "data_store" / "session_archive" / day / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "events": 1,
                "traded_symbols_recorded": ["A", "B"],
                "contract_files_archived": 2,
                "spot_files_archived": 12,
                "contract_files_without_day_bars": 0,
            }
        ),
        encoding="utf-8",
    )

    assert main._session_archive_manifest_complete(tmp_path, day) is False


def test_backlog_rotation_visits_every_incomplete_due_day_before_retry(tmp_path):
    for day in ("2026-08-18", "2026-08-19", "2026-08-20"):
        _event(tmp_path, day)
    _manifest(tmp_path, "2026-08-19", missing=[])
    now = main.IST.localize(datetime(2026, 8, 21, 16, 0))
    attempted = set()

    first = main._next_due_session_archive_day(tmp_path, now, dt_time(15, 50), attempted)
    attempted.add(first)
    second = main._next_due_session_archive_day(tmp_path, now, dt_time(15, 50), attempted)
    attempted.add(second)
    wrapped = main._next_due_session_archive_day(tmp_path, now, dt_time(15, 50), attempted)

    assert (first, second) == ("2026-08-20", "2026-08-18")
    assert wrapped == "2026-08-20"


def test_old_ledger_backfill_is_anchored_to_that_session_not_today():
    calls = []

    class Provider:
        def get_historical_candles(self, *args, **kwargs):
            calls.append((args, kwargs))
            return [1]

    assert main._fetch_contract_history_for_session(
        Provider(), "2026-07-29", "NIFTY26JUL24000CE", "12345"
    ) is True

    args, kwargs = calls[0]
    assert args[:3] == ("12345", "NFO", "1min")
    assert kwargs["to_date"].date().isoformat() == "2026-07-29"
    assert kwargs["to_date"].time() == dt_time(15, 45)
    assert kwargs["force_refresh"] is True


def test_market_data_threads_explicit_target_date_to_kite(monkeypatch):
    target = main.IST.localize(datetime(2026, 7, 29, 15, 45))
    observed = {}

    class Kite:
        is_connected = True

        def get_historical(self, token, interval, days_back=15, to_date=None):
            observed.update(
                token=token, interval=interval, days_back=days_back, to_date=to_date
            )
            idx = pd.DatetimeIndex([target], name="timestamp")
            return pd.DataFrame(
                {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
                index=idx,
            )

    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.kite = Kite()
    provider._api_hist_cache = {}
    provider._last_api_hist_fetch = {}
    provider._save_and_merge_candles = lambda frame, _path: frame

    frame = provider.get_historical_candles(
        "12345",
        "NFO",
        "1min",
        days_back=1,
        instrument_name="NIFTY26JUL24000CE",
        force_refresh=True,
        to_date=target,
    )

    assert not frame.empty
    assert observed == {
        "token": 12345,
        "interval": "1min",
        "days_back": 1,
        "to_date": target,
    }


def test_deferred_provider_not_ready_is_distinct_from_archive_only_none():
    provider, pending, reason = main._resolve_session_archive_provider(lambda: None)
    assert provider is None
    assert pending is True
    assert "not initialized" in reason

    provider, pending, reason = main._resolve_session_archive_provider(None)
    assert provider is None
    assert pending is False
    assert reason == ""


def test_scheduler_does_not_attempt_or_spawn_child_while_provider_is_pending():
    source = inspect.getsource(main.start_session_archiver_scheduler)
    resolve_at = source.index("_resolve_session_archive_provider")
    pending_at = source.index("if provider_pending", resolve_at)
    continue_at = source.index("continue", pending_at)
    attempted_at = source.index("attempted.add(session_day)", resolve_at)
    child_at = source.index("subprocess.run", resolve_at)

    assert resolve_at < pending_at < continue_at < attempted_at < child_at
