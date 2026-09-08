import inspect
import json

import pandas as pd
import pytest

import main
import scripts.archive_session_data as archive


def test_archive_cli_returns_failure_for_a_skipped_day(monkeypatch, capsys):
    monkeypatch.setattr(archive.sys, "argv", ["archive_session_data.py", "1999-01-01", "--quiet"])
    monkeypatch.setattr(
        archive,
        "archive_day",
        lambda *args, **kwargs: {"date": "1999-01-01", "skipped": "no_events"},
    )

    with pytest.raises(SystemExit) as exc:
        archive.main()

    assert exc.value.code == 2
    assert "[archive-result]" in capsys.readouterr().err


def test_archive_cli_returns_failure_for_incomplete_manifest(monkeypatch, capsys):
    monkeypatch.setattr(archive.sys, "argv", ["archive_session_data.py", "2026-08-21", "--quiet"])
    monkeypatch.setattr(
        archive,
        "archive_day",
        lambda *args, **kwargs: {"date": "2026-08-21", "complete": False},
    )

    with pytest.raises(SystemExit) as exc:
        archive.main()

    assert exc.value.code == 2
    output = capsys.readouterr().err
    assert "[archive-result]" in output
    assert '"complete": false' in output.lower()


def test_archive_normalizes_malformed_prior_repair_metadata(monkeypatch, tmp_path):
    day = "2026-08-21"
    candles = tmp_path / "candles"
    events = tmp_path / "events"
    archived = tmp_path / "archive"
    candles.mkdir()
    events.mkdir()
    (events / f"{day}.jsonl").write_text(
        json.dumps({"contract": {"trading_symbol": "A"}}) + "\n",
        encoding="utf-8",
    )
    prior = archived / day / "manifest.json"
    prior.parent.mkdir(parents=True)
    prior.write_text(
        json.dumps(
            {"repair_status": {"attempts": "corrupt", "missing_contracts": ["A"]}}
        ),
        encoding="utf-8",
    )
    for index in archive.INDICES:
        for timeframe in ("1min", "5min", "15min"):
            (candles / f"{index}_{timeframe}.csv").write_text("placeholder", encoding="utf-8")

    frame = pd.DataFrame({"timestamp": [f"{day} 09:15:00"], "close": [1.0]})
    monkeypatch.setattr(archive, "CANDLES", candles)
    monkeypatch.setattr(archive, "EVENTS", events)
    monkeypatch.setattr(archive, "ARCHIVE", archived)
    monkeypatch.setattr(archive, "day_slice", lambda *_args, **_kwargs: frame)

    manifest = archive.archive_day(day, force=True, quiet=True)

    assert manifest["repair_status"]["attempts"] == 0
    assert manifest["repair_status"]["missing_contracts"]["A"]["state"] == "retryable"


def test_archive_tracks_exact_symbols_when_one_symbol_has_two_files_and_one_has_none(
    monkeypatch, tmp_path
):
    day = "2026-08-21"
    candles = tmp_path / "candles"
    events = tmp_path / "events"
    archived = tmp_path / "archive"
    candles.mkdir()
    events.mkdir()

    (events / f"{day}.jsonl").write_text(
        "\n".join(
            json.dumps({"contract": {"trading_symbol": symbol}})
            for symbol in ("A", "B")
        )
        + "\n",
        encoding="utf-8",
    )
    # A's extra 5-minute file is not replay-compatible and must be ignored; neither
    # file may compensate for B having no 1-minute cache file.
    for name in ("A_1min.csv", "A_5min.csv"):
        (candles / name).write_text("placeholder", encoding="utf-8")
    for index in archive.INDICES:
        for timeframe in ("1min", "5min", "15min"):
            (candles / f"{index}_{timeframe}.csv").write_text(
                "placeholder", encoding="utf-8"
            )

    frame = pd.DataFrame({"timestamp": [f"{day} 09:15:00"], "close": [1.0]})
    monkeypatch.setattr(archive, "CANDLES", candles)
    monkeypatch.setattr(archive, "EVENTS", events)
    monkeypatch.setattr(archive, "ARCHIVE", archived)
    monkeypatch.setattr(archive, "day_slice", lambda *_args, **_kwargs: frame)

    manifest = archive.archive_day(day, force=True, quiet=True)

    assert manifest["contract_files_archived"] == 1
    assert manifest["contract_symbols_archived"] == ["A"]
    assert manifest["missing_contract_symbols"] == ["B"]
    assert manifest["complete"] is False
    assert any("contract symbol coverage 1/2" in error for error in manifest["coverage_errors"])


def test_five_minute_only_contract_does_not_satisfy_replay_coverage(monkeypatch, tmp_path):
    day = "2026-08-21"
    candles = tmp_path / "candles"
    events = tmp_path / "events"
    archived = tmp_path / "archive"
    candles.mkdir()
    events.mkdir()
    (events / f"{day}.jsonl").write_text(
        json.dumps({"contract": {"trading_symbol": "A"}}) + "\n",
        encoding="utf-8",
    )
    (candles / "A_5min.csv").write_text("placeholder", encoding="utf-8")
    for index in archive.INDICES:
        for timeframe in ("1min", "5min", "15min"):
            (candles / f"{index}_{timeframe}.csv").write_text(
                "placeholder", encoding="utf-8"
            )

    frame = pd.DataFrame({"timestamp": [f"{day} 09:15:00"], "close": [1.0]})
    monkeypatch.setattr(archive, "CANDLES", candles)
    monkeypatch.setattr(archive, "EVENTS", events)
    monkeypatch.setattr(archive, "ARCHIVE", archived)
    monkeypatch.setattr(archive, "day_slice", lambda *_args, **_kwargs: frame)

    manifest = archive.archive_day(day, force=True, quiet=True)

    assert manifest["contract_files_archived"] == 0
    assert manifest["contract_symbols_archived"] == []
    assert manifest["missing_contract_symbols"] == ["A"]
    assert manifest["complete"] is False


def test_legacy_fallback_selects_only_replay_compatible_one_minute_files(
    monkeypatch, tmp_path
):
    candles = tmp_path / "candles"
    candles.mkdir()
    for name in ("A26AUG100CE_1min.csv", "A26AUG100CE_5min.csv", "A26AUG100PE_1min.csv"):
        (candles / name).write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(archive, "CANDLES", candles)

    selected = [path.name for path in archive.candidate_contract_files("2026-08-21", set())]

    assert selected == ["A26AUG100CE_1min.csv", "A26AUG100PE_1min.csv"]


def test_scheduler_checks_child_status_manifest_and_missed_latest_ledger():
    source = inspect.getsource(main.start_session_archiver_scheduler)
    selector_source = inspect.getsource(main._next_due_session_archive_day)
    assert "check=True" in source
    assert "_manifest_complete(session_day)" in source
    assert "_latest_due_event_day" in source
    assert 'event_dir.glob("????-??-??.jsonl")' in selector_source
    assert "completed.add(session_day)" in source
    success_branch = source.split("result = subprocess.run", 1)[1].split("except Exception", 1)[0]
    assert "completed.add(session_day)" in success_branch
