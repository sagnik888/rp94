"""Memory pressure warns by percentage; only exhausted commit headroom blocks entries.

2026-09-11: all four indices rallied 0.5-0.8% between 13:11 and 14:03 and every LONG
that survived stabilization (BANKNIFTY A 0.81, MIDCPNIFTY A+ 0.97, SENSEX and NIFTY 15min
A) was refused by "Auto resource guard: Windows commit 92-94% >= 92%". The machine sat at
92-95.8% commit for five hours without a single low-memory event, with 1.2-2.3 GB of commit
headroom. Across all 67 guard blocks on record, every one was a PERCENTAGE trigger (RAM
>= 94% or commit >= 92%); the absolute-headroom trigger never fired once.

The failure the guard exists for (2026-08-24) is commit EXHAUSTION: requests are denied
while Windows grows the pagefile. Remaining headroom measures that directly; a percentage
of an auto-growing commit limit does not. So the percentages now warn and the headroom
floor blocks. auto_resource_pct_blocks_entries=True restores the old behaviour.
"""
import time
from types import SimpleNamespace

from engine.scanner_telemetry import ScannerTelemetryMixin


class _Telemetry(ScannerTelemetryMixin):
    pass


def _settings(**overrides):
    values = {
        "auto_resource_guard_enabled": True,
        "auto_resource_ram_block_pct": 94.0,
        "auto_resource_commit_block_pct": 92.0,
        "auto_resource_commit_min_available_mb": 1024.0,
        "auto_resource_clear_hysteresis_pct": 4.0,
        "auto_resource_clear_consecutive_samples": 2,
        "auto_resource_sample_max_age_seconds": 5.0,
        "auto_resource_pct_blocks_entries": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# The 28.7 GB commit limit this machine had on 2026-09-11.
LIMIT_MB = 28.7 * 1024


def _metrics(ram=50.0, commit=50.0, available=None, **meta):
    if available is None:
        available = LIMIT_MB * (100.0 - commit) / 100.0
    return {
        "ram": {"total_mb": 16076.8, "used_pct": ram},
        "commit": {"total_mb": LIMIT_MB, "used_pct": commit, "available_mb": available},
        "_valid": meta.get("valid", True),
        "_sampled_at": meta.get("sampled_at", time.time()),
    }


def _telemetry(monkeypatch, samples, **settings):
    import engine.scanner_telemetry as module

    monkeypatch.setattr(module, "get_settings", lambda: _settings(**settings))
    t = _Telemetry()
    it = iter(samples)
    t._fresh_resource_memory_payload = lambda: next(it)
    return t


def test_the_1325_rally_sample_does_not_block(monkeypatch):
    """commit 93.0% with ~2.0 GB headroom — what refused BANKNIFTY/MIDCPNIFTY at 13:25."""
    t = _telemetry(monkeypatch, [_metrics(ram=90.0, commit=93.0)])
    assert t._auto_execution_resource_blockers() == []


def test_the_worst_sample_of_the_day_does_not_block(monkeypatch):
    """09:14: RAM 95.5%, commit 95.8% — still ~1.2 GB of headroom."""
    t = _telemetry(monkeypatch, [_metrics(ram=95.5, commit=95.8)])
    assert t._auto_execution_resource_blockers() == []


def test_percentage_pressure_is_still_reported_as_a_warning(monkeypatch):
    t = _telemetry(monkeypatch, [_metrics(ram=95.5, commit=94.0)])
    assert t._auto_execution_resource_blockers() == []
    warnings = list(getattr(t, "_auto_resource_pressure_warnings", []) or [])
    assert any("Windows commit 94.0%" in w for w in warnings), warnings
    assert any("system RAM 95.5%" in w for w in warnings), warnings


def test_exhausted_headroom_still_blocks(monkeypatch):
    t = _telemetry(monkeypatch, [_metrics(commit=97.0, available=900.0)])
    blockers = t._auto_execution_resource_blockers()
    assert blockers and "commit available 900 MB" in blockers[0]


def test_the_headroom_latch_clears_on_headroom_even_while_percent_stays_high(monkeypatch):
    """Clearing must not wait on a percentage that no longer blocks."""
    t = _telemetry(
        monkeypatch,
        [
            _metrics(commit=97.0, available=900.0),
            _metrics(commit=95.0, available=1400.0),   # above the floor, below floor+512
            _metrics(commit=93.0, available=2000.0),
            _metrics(commit=93.0, available=2000.0),
        ],
    )
    assert t._auto_execution_resource_blockers()
    assert t._auto_execution_resource_blockers(), "cleared without the +512 MB hysteresis"
    assert t._auto_execution_resource_blockers(), "one healthy sample is insufficient"
    assert t._auto_execution_resource_blockers() == []


def test_unknown_metrics_still_fail_closed(monkeypatch):
    t = _telemetry(monkeypatch, [{}])
    assert "unavailable or stale" in t._auto_execution_resource_blockers()[0]


def test_the_old_percentage_block_is_one_setting_away(monkeypatch):
    t = _telemetry(
        monkeypatch, [_metrics(ram=90.0, commit=93.0)], auto_resource_pct_blocks_entries=True
    )
    blockers = t._auto_execution_resource_blockers()
    assert blockers and "Windows commit 93.0% >= 92.0%" in blockers[0]


def test_the_live_entry_boundary_admits_the_rally_sample(monkeypatch):
    """The same check the 13:25 candidates hit, reached through the scanner."""
    import engine.scanner_telemetry as module
    from scanner import Scanner

    monkeypatch.setattr(module, "get_settings", lambda: _settings())
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = True
    scanner._fresh_resource_memory_payload = lambda: _metrics(ram=90.0, commit=93.0)
    assert Scanner._auto_entry_resource_blockers(scanner) == []


def test_the_watchdog_neither_inhibits_nor_stays_silent():
    import main

    scanner = SimpleNamespace(
        mode="REAL",
        auto_mode=True,
        is_running=True,
        _auto_execution_resource_blockers=lambda: [],
        _auto_resource_pressure_warnings=["Windows commit 94.0% >= 92.0%"],
    )
    pressured, _ = main._apply_runtime_resource_guard(scanner)
    assert pressured is False
    assert scanner._auto_resource_entry_inhibited is False
    warning = main._runtime_resource_warning(scanner)
    assert "Windows commit 94.0%" in warning
    assert "entries still allowed" in warning.lower()


def test_the_watchdog_warning_is_quiet_when_auto_is_off():
    import main

    scanner = SimpleNamespace(
        mode="REAL", auto_mode=False, _auto_resource_pressure_warnings=["x"]
    )
    assert main._runtime_resource_warning(scanner) == ""


def test_the_default_is_headroom_only():
    from config.settings import Settings

    assert Settings.model_fields["auto_resource_pct_blocks_entries"].default is False
    assert Settings.model_fields["auto_resource_commit_min_available_mb"].default == 1024.0
