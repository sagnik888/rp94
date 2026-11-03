"""The operator's fast lane, and the wait it is supposed to use.

Spec: 1min and burst signals wait 0-5/7s; 5min and 15min wait 20-30s; the wait shortens
in a clean trend. Two things stopped that being true.

1. A raw 1min arrow needs a volume burst, a post-win snapback or a confirmed cascade, so
   only 4 raw 1min signals reached the buffer in 52 sessions. There is no sample on which
   to judge the open lane, so it ships OFF behind `one_min_raw_entry_enabled` rather than
   being opened silently; when ON it uses the short 1m window, not the 30s 5m/15m wait.
2. The dashboard clamped live_signal_stabilization_seconds_5min/15min to a 30s FLOOR
   while the code default is 15s, so a shorter clean-trend wait could not be configured
   at all - anything under 30 was silently raised.
"""
from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from dashboard.server import _NUMERIC_FIELD_BOUNDS
from scanner import Scanner


# ── the configurable wait ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field", ["live_signal_stabilization_seconds_5min", "live_signal_stabilization_seconds_15min"]
)
def test_the_dashboard_floor_no_longer_sits_above_the_code_default(field):
    low, high = _NUMERIC_FIELD_BOUNDS[field]
    code_default = float(getattr(Settings(), field))

    assert low <= code_default, (
        f"{field}: the dashboard floor ({low}s) silently raised the configured wait "
        f"above the code default ({code_default}s)"
    )
    assert low >= 10.0, "a repaint buffer of under 10s on 5m/15m is not a buffer"
    assert high == 120.0


# ── the 1min lane ─────────────────────────────────────────────────────────────


def test_the_one_min_lane_ships_off():
    assert Settings().one_min_raw_entry_enabled is False


def _candidate(**overrides):
    values = dict(
        instrument="NIFTY", timeframe="1min", direction="LONG", inst_type="OPT",
        volume_spike_entry=False, is_reversion_scalp=False, one_min_raw_entry=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _requirements(candidate):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_trade_regime = lambda c, default="UNKNOWN": "TRENDING_UP"
    scanner._setting_enabled = lambda value, default: bool(default if value is None else value)
    return Scanner._live_signal_stabilization_requirements(scanner, candidate)


def test_a_raw_one_min_entry_uses_the_short_one_minute_window():
    settings = get_settings()
    spike_max = float(getattr(settings, "volume_spike_entry_stabilization_seconds_max", 15.0))

    seconds, scans, adaptive = _requirements(_candidate(one_min_raw_entry=True))

    assert seconds <= spike_max, "a 1min entry must not inherit the 30s 5m/15m wait"
    assert seconds <= 15.0
    assert scans >= 1 and adaptive is True


def test_a_qualifying_one_min_signal_waits_the_one_minute_window():
    """2026-10-09 (user, and this file's spec): a 1min signal that passed the filters waits
    0-5s. A quad-prime A+ 1m entry used to inherit the legacy 30s window."""
    seconds, scans, _ = _requirements(_candidate())

    assert seconds == float(get_settings().live_signal_stabilization_seconds_1min) == 5.0
    assert scans == 1


def test_a_risky_one_min_option_signal_asks_for_a_second_scan_not_thirty_seconds():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_trade_regime = lambda c, default="UNKNOWN": "VOLATILE"
    scanner._setting_enabled = lambda value, default: bool(default if value is None else value)
    seconds, scans, adaptive = Scanner._live_signal_stabilization_requirements(scanner, _candidate())

    assert seconds == 5.0 and scans == 2 and adaptive is True


def test_the_burst_lane_is_unchanged():
    spike, _, _ = _requirements(_candidate(volume_spike_entry=True))
    raw, _, _ = _requirements(_candidate(one_min_raw_entry=True))

    assert spike == raw, "both are the same 1m anti-repaint chassis"


def test_the_gate_reads_the_setting():
    """The admission branch must be behind the flag, not hard-coded open."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath(
        "engine", "signal_processor.py"
    ).read_text(encoding="utf-8")
    # The first occurrence is a label list; the gate itself is the last one.
    weak = source.rindex("REJECTED - Weak 1min")
    window = source[weak - 1600:weak]
    assert "one_min_raw_entry_enabled" in window
    assert 'setattr(best_sig, "one_min_raw_entry", True)' in window
