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
    event_path = root / "data_store" / "rtracker" / "events" / f"{day}.jsonl"
    if not event_path.exists():
        _event(root, day)
    path = root / "data_store" / "session_archive" / day / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": main._SESSION_ARCHIVE_SCHEMA_VERSION,
                "contract_ownership_version": (
                    main._SESSION_ARCHIVE_CONTRACT_OWNERSHIP_VERSION
                ),
                "source_event_identity": main._session_archive_event_identity(
                    root, day
                ),
                "events": 1,
                "complete": True,
                "coverage_errors": [],
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


def _incomplete_manifest(root, day, symbol="NIFTY26AUG24000CE", repair_status=None):
    path = root / "data_store" / "session_archive" / day / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 2,
        "date": day,
        "events": 1,
        "complete": False,
        "traded_symbols_recorded": [symbol],
        "contract_files_archived": 0,
        "contract_symbols_archived": [],
        "missing_contract_symbols": [symbol],
        "spot_files_archived": 12,
        "contract_files_without_day_bars": 0,
        "coverage_errors": [f"contract symbol coverage 0/1; missing {symbol}"],
    }
    if repair_status is not None:
        value["repair_status"] = repair_status
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_manifest_completion_accepts_list_and_legacy_numeric_missing_counts(tmp_path):
    _manifest(tmp_path, "2026-08-18", missing=[])
    assert main._session_archive_manifest_complete(tmp_path, "2026-08-18") is True

    _manifest(tmp_path, "2026-08-18", missing=["NIFTY26AUG24000PE"])
    assert main._session_archive_manifest_complete(tmp_path, "2026-08-18") is False

    _manifest(tmp_path, "2026-08-18", missing=0)
    assert main._session_archive_manifest_complete(tmp_path, "2026-08-18") is True


def test_legacy_complete_manifest_is_requeued_for_ownership_migration(tmp_path):
    day = "2026-08-18"
    _manifest(tmp_path, day, missing=[])
    path = tmp_path / "data_store" / "session_archive" / day / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop("schema_version")
    manifest.pop("contract_ownership_version")
    manifest.pop("source_event_identity")
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert main._session_archive_manifest_complete(tmp_path, day) is False


def test_complete_manifest_is_requeued_when_settled_ledger_changes(tmp_path):
    day = "2026-08-18"
    _manifest(tmp_path, day, missing=[])
    assert main._session_archive_manifest_complete(tmp_path, day) is True

    event_path = tmp_path / "data_store" / "rtracker" / "events" / f"{day}.jsonl"
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write("{}\n")

    assert main._session_archive_manifest_complete(tmp_path, day) is False


def test_explicitly_incomplete_manifest_cannot_pass_on_coverage_counts(tmp_path):
    day = "2026-08-18"
    _manifest(tmp_path, day, missing=[])
    path = tmp_path / "data_store" / "session_archive" / day / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["complete"] = False
    manifest["coverage_errors"] = ["event ledger changed while archive was reading it"]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert main._session_archive_manifest_complete(tmp_path, day) is False


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
    outcome = {}

    class Provider:
        def get_historical_candles(self, *args, **kwargs):
            calls.append((args, kwargs))
            return [1]

    assert main._fetch_contract_history_for_session(
        Provider(),
        "2026-07-29",
        "NIFTY26JUL24000CE",
        "12345",
        call_outcome=outcome,
    ) is True

    args, kwargs = calls[0]
    assert args[:3] == ("12345", "NFO", "1min")
    assert kwargs["to_date"].date().isoformat() == "2026-07-29"
    assert kwargs["to_date"].time() == dt_time(15, 45)
    assert kwargs["force_refresh"] is True
    assert kwargs["kite_call_outcome"] is outcome
    assert kwargs["record_kite_history_health"] is False


def test_contract_token_fallback_uses_the_composed_kite_master():
    class Kite:
        def instrument_token(self, symbol, exchange):
            assert (symbol, exchange) == ("SENSEX26SEP80000CE", "BFO")
            return 987654

    class Provider:
        kite = Kite()

    assert main._contract_token_for(Provider(), "SENSEX26SEP80000CE") == "987654"


def test_exact_invalid_instrument_token_is_terminal_archive_evidence():
    class Kite:
        @staticmethod
        def historical_rest_status():
            return {
                "kite_historical_rest_last_call_ok": False,
                "kite_historical_rest_failure": "InputException: invalid token",
            }

    outcome = main._contract_history_backfill_outcome(
        type("Provider", (), {"kite": Kite()})(), False
    )

    assert outcome["state"] == "terminal_unfillable"
    assert outcome["permanent"] is True


def test_call_local_archive_result_wins_over_concurrent_provider_status():
    class Kite:
        @staticmethod
        def historical_rest_status():
            # Simulates a newer successful live-history call completing before the
            # archive thread reads provider-wide health.
            return {
                "kite_historical_rest_last_call_ok": True,
                "kite_historical_rest_failure": "",
            }

    outcome = main._contract_history_backfill_outcome(
        type("Provider", (), {"kite": Kite()})(),
        False,
        call_outcome={
            "ok": False,
            "reason": "InputException: invalid token",
        },
    )

    assert outcome["state"] == "terminal_unfillable"
    assert outcome["permanent"] is True


def test_session_auth_failure_and_successful_empty_history_stay_retryable():
    class Kite:
        status = {
            "kite_historical_rest_last_call_ok": False,
            "kite_historical_rest_failure": "TokenException: invalid access_token",
        }

        def historical_rest_status(self):
            return dict(self.status)

    provider = type("Provider", (), {"kite": Kite()})()
    auth_failure = main._contract_history_backfill_outcome(provider, False)
    assert auth_failure["state"] == "retryable"
    assert not auth_failure.get("permanent")

    provider.kite.status = {
        "kite_historical_rest_last_call_ok": True,
        "kite_historical_rest_failure": "",
    }
    empty = main._contract_history_backfill_outcome(provider, False)
    assert empty["state"] == "retryable"
    assert "zero rows" in empty["reason"]


def test_unchanged_terminal_contract_is_reused_without_another_broker_attempt(tmp_path):
    day = "2026-08-18"
    symbol = "NIFTY26AUG24000CE"
    _event(tmp_path, day)
    path = _incomplete_manifest(tmp_path, day, symbol)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["repair_status"] = {
        "state": "terminal_unfillable",
        "missing_contracts": {
            symbol: {
                "state": "terminal_unfillable",
                "reason": "invalid instrument token",
                "permanent": True,
                "attempts": 1,
            }
        },
    }
    manifest["repair_status"]["source_identity"] = (
        main._session_archive_source_identity(tmp_path, day, manifest)
    )
    path.write_text(json.dumps(manifest), encoding="utf-8")

    rows = main._session_archive_terminal_contracts(tmp_path, day)

    assert rows[symbol]["permanent"] is True


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


def test_transient_contract_failures_back_off_but_never_become_terminal(tmp_path):
    day = "2026-08-18"
    symbol = "NIFTY26AUG24000CE"
    _event(tmp_path, day)
    _incomplete_manifest(tmp_path, day, symbol)

    for _ in range(3):
        status = main._record_session_archive_repair(
            tmp_path,
            day,
            backfill_outcomes={
                symbol: {
                    "state": "retryable",
                    "reason": "temporary provider timeout",
                    "attempted": True,
                }
            },
        )

    assert status["state"] == "retry_backoff"
    assert status["missing_contracts"][symbol]["attempts"] == 3
    assert main._session_archive_manifest_terminal(tmp_path, day) is False

    # Backoff is scheduling, not abandonment. Once due, the ledger is selected again.
    path = _incomplete_manifest(tmp_path, day, symbol, status)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["repair_status"]["next_retry_at"] = "2026-08-18T15:50:00+05:30"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    now = main.IST.localize(datetime(2026, 8, 19, 16, 0))
    assert main._next_due_session_archive_day(tmp_path, now, dt_time(15, 50), set()) == day


def test_repeated_non_contract_coverage_failures_also_back_off(tmp_path):
    day = "2026-08-18"
    symbol = "NIFTY26AUG24000CE"
    _event(tmp_path, day)
    path = _incomplete_manifest(tmp_path, day, symbol)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["coverage_errors"].insert(0, "spot coverage 4/12 index/timeframe files")
    path.write_text(json.dumps(manifest), encoding="utf-8")

    terminal = {
        symbol: {
            "state": "terminal_unfillable",
            "reason": "broker permanently rejected the exact instrument token as invalid",
            "permanent": True,
            "attempted": True,
        }
    }
    for _ in range(3):
        status = main._record_session_archive_repair(
            tmp_path,
            day,
            backfill_outcomes=terminal,
            default_reason="spot coverage 4/12 index/timeframe files",
        )

    assert status["state"] == "retry_backoff"
    assert status["archive_failures"] == 3
    assert status["missing_contracts"][symbol]["state"] == "terminal_unfillable"
    assert status["next_retry_at"]
    assert "incomplete session archive" in status["next_action"]
    assert main._session_archive_manifest_terminal(tmp_path, day) is False
    now = main.IST.localize(datetime(2026, 8, 19, 16, 0))
    assert main._next_due_session_archive_day(tmp_path, now, dt_time(15, 50), set()) is None


def test_repeated_legacy_archive_failure_without_contract_rows_backs_off(tmp_path):
    day = "2026-08-18"
    _event(tmp_path, day)
    path = _incomplete_manifest(tmp_path, day)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["traded_symbols_recorded"] = []
    manifest["missing_contract_symbols"] = []
    manifest["coverage_errors"] = ["spot coverage 4/12 index/timeframe files"]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    for _ in range(3):
        status = main._record_session_archive_repair(
            tmp_path,
            day,
            default_reason="spot coverage 4/12 index/timeframe files",
        )

    assert status["state"] == "retry_backoff"
    assert status["attempts"] == 0
    assert status["archive_failures"] == 3
    assert status["missing_contracts"] == {}
    assert status["next_retry_at"]


def test_pre_fix_manifest_derives_backoff_from_archive_failures(tmp_path):
    day = "2026-08-18"
    _event(tmp_path, day)
    _incomplete_manifest(
        tmp_path,
        day,
        repair_status={
            "state": "retryable",
            "archive_failures": 3,
            "last_attempt_at": "2026-08-19T15:55:00+05:30",
            "missing_contracts": {},
        },
    )

    before_due = main.IST.localize(datetime(2026, 8, 19, 16, 0))
    after_due = main.IST.localize(datetime(2026, 8, 19, 22, 0))
    assert main._next_due_session_archive_day(
        tmp_path, before_due, dt_time(15, 50), set()
    ) is None
    assert main._next_due_session_archive_day(
        tmp_path, after_due, dt_time(15, 50), set()
    ) == day


def test_terminal_classification_requires_explicit_permanent_evidence_and_revalidates(tmp_path):
    day = "2026-08-18"
    symbol = "NIFTY26AUG24000CE"
    _event(tmp_path, day)
    _incomplete_manifest(tmp_path, day, symbol)

    status = main._record_session_archive_repair(
        tmp_path,
        day,
        backfill_outcomes={
            symbol: {
                "state": "terminal_unfillable",
                "reason": "broker confirms instrument history permanently unavailable",
                "permanent": True,
                "attempted": True,
            }
        },
    )

    assert status["state"] == "terminal_unfillable"
    assert main._session_archive_manifest_terminal(tmp_path, day) is True

    # New session evidence invalidates the old terminal decision automatically.
    event_path = tmp_path / "data_store" / "rtracker" / "events" / f"{day}.jsonl"
    event_path.write_text("{}\n{}\n", encoding="utf-8")
    assert main._session_archive_manifest_terminal(tmp_path, day) is False
    now = main.IST.localize(datetime(2026, 8, 19, 16, 0))
    assert main._next_due_session_archive_day(tmp_path, now, dt_time(15, 50), set()) == day


def test_corrupt_repair_counters_and_rows_are_normalized(tmp_path):
    day = "2026-08-18"
    symbol = "NIFTY26AUG24000CE"
    _event(tmp_path, day)
    _incomplete_manifest(
        tmp_path,
        day,
        symbol,
        {"attempts": "corrupt", "archive_failures": [], "missing_contracts": [symbol]},
    )

    status = main._record_session_archive_repair(tmp_path, day, default_reason="child failed")

    assert status["state"] == "retryable"
    assert status["attempts"] == 0
    assert status["archive_failures"] == 1
    assert status["missing_contracts"][symbol]["attempts"] == 0
