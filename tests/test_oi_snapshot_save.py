"""Verify the WinError-32 retry/backoff in MarketDataProvider._save_option_oi_snapshots."""
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import data.market_data as md


def _fake_provider(snapshots):
    fake = SimpleNamespace()
    fake._option_oi_snapshots = snapshots
    fake._option_oi_snapshot_guard = lambda: contextlib.nullcontext()
    return fake


def test_oi_snapshot_save_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _fake_provider({"NIFTY": {"24000CE": 12345}})

    calls = {"n": 0}
    real_replace = Path.replace

    def flaky_replace(self, target):
        calls["n"] += 1
        if calls["n"] < 3:          # fail twice (WinError 32 -> PermissionError)
            raise PermissionError(32, "file in use by another process")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    md.MarketDataProvider._save_option_oi_snapshots(fake)

    saved = Path("data_store/option_oi_snapshots.json")
    assert saved.exists(), "snapshot should be written after retries"
    assert json.loads(saved.read_text(encoding="utf-8"))["NIFTY"]["24000CE"] == 12345
    assert calls["n"] == 3, "should retry until the rename succeeds"
    assert not list(Path("data_store").glob("*.tmp")), "temp file consumed by successful replace"


def test_oi_snapshot_save_gives_up_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = _fake_provider({"NIFTY": {}})

    def always_locked(self, target):
        raise PermissionError(32, "file in use by another process")

    monkeypatch.setattr(Path, "replace", always_locked)

    # Persistent lock must NOT raise (non-order-path, graceful) and must not leak temp files.
    md.MarketDataProvider._save_option_oi_snapshots(fake)

    assert not Path("data_store/option_oi_snapshots.json").exists()
    assert not list(Path("data_store").glob("*.tmp")), "temp file cleaned up on give-up"
