from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from config.settings import (
    Settings,
    normalize_timeframe_entry_policy,
    timeframe_entry_timeframes,
)
from dashboard.server import _valid_command_payload
from engine.signal_processor import SignalProcessor
from scanner import Scanner


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("PRIMARY_5", {"5min"}),
        ("PRIMARY_15", {"15min"}),
        ("INCLUDE_5MIN", {"5min", "15min"}),
        ("INCLUDE_1MIN_5MIN", {"1min", "5min"}),
        ("INCLUDE_1MIN_5MIN_15MIN", {"1min", "5min", "15min"}),
    ],
)
def test_timeframe_policy_modes_have_exact_frame_sets(policy, expected):
    assert timeframe_entry_timeframes(policy) == expected
    assert normalize_timeframe_entry_policy(policy.lower()) == policy


def test_invalid_timeframe_policy_falls_back_to_safe_default():
    assert normalize_timeframe_entry_policy("unknown") == "INCLUDE_5MIN"
    assert timeframe_entry_timeframes("unknown") == {"5min", "15min"}


def test_settings_accept_each_dashboard_timeframe_policy():
    for policy in (
        "PRIMARY_5",
        "PRIMARY_15",
        "INCLUDE_5MIN",
        "INCLUDE_1MIN_5MIN",
        "INCLUDE_1MIN_5MIN_15MIN",
    ):
        assert Settings(ut_timeframe_entry_policy=policy).ut_timeframe_entry_policy == policy


@pytest.mark.parametrize(
    ("policy", "timeframe", "expected"),
    [
        ("PRIMARY_5", "5min", True),
        ("PRIMARY_5", "1min", False),
        ("PRIMARY_5", "15min", False),
        ("INCLUDE_1MIN_5MIN", "1min", True),
        ("INCLUDE_1MIN_5MIN", "5min", True),
        ("INCLUDE_1MIN_5MIN", "15min", False),
        ("INCLUDE_1MIN_5MIN_15MIN", "1min", True),
        ("INCLUDE_1MIN_5MIN_15MIN", "5min", True),
        ("INCLUDE_1MIN_5MIN_15MIN", "15min", True),
    ],
)
def test_live_policy_gate_honors_all_new_modes(monkeypatch, policy, timeframe, expected):
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(ut_timeframe_entry_policy=policy),
    )
    scanner = Scanner.__new__(Scanner)
    scanner._gate_block = lambda candidate, message: False
    candidate = SimpleNamespace(
        instrument="NIFTY",
        direction="LONG",
        timeframe=timeframe,
        volume_spike_entry=False,
        is_reversion_scalp=False,
        market_ltp_shock_entry=False,
        market_wide_reversal_entry=False,
    )

    assert scanner._passes_timeframe_entry_policy(candidate) is expected


@pytest.mark.parametrize("policy", ("PRIMARY_15", "INCLUDE_5MIN"))
@pytest.mark.parametrize(
    "exception_flag",
    (
        "volume_spike_entry",
        "is_reversion_scalp",
        "market_ltp_shock_entry",
        "market_wide_reversal_entry",
        "late_session_reclaim_entry",
        "one_way_cascade_entry",
    ),
)
def test_legacy_modes_keep_confirmed_one_minute_special_entries(
    monkeypatch, policy, exception_flag
):
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(ut_timeframe_entry_policy=policy),
    )
    scanner = Scanner.__new__(Scanner)
    scanner._gate_block = lambda candidate, message: False
    candidate = SimpleNamespace(
        instrument="NIFTY",
        direction="LONG",
        timeframe="1min",
        volume_spike_entry=True,
        is_reversion_scalp=False,
        market_ltp_shock_entry=False,
        market_wide_reversal_entry=False,
        late_session_reclaim_entry=False,
        one_way_cascade_entry=False,
    )
    setattr(candidate, exception_flag, True)

    assert scanner._passes_timeframe_entry_policy(candidate) is True


def test_primary_five_blocks_even_fast_one_minute_entry(monkeypatch):
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(ut_timeframe_entry_policy="PRIMARY_5"),
    )
    scanner = Scanner.__new__(Scanner)
    scanner._gate_block = lambda candidate, message: False
    candidate = SimpleNamespace(
        instrument="NIFTY",
        direction="LONG",
        timeframe="1min",
        volume_spike_entry=True,
        is_reversion_scalp=False,
        market_ltp_shock_entry=False,
        market_wide_reversal_entry=False,
    )

    assert scanner._passes_timeframe_entry_policy(candidate) is False


def test_explicit_one_minute_policy_never_admits_raw_one_minute_row():
    row = {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=False)}

    assert SignalProcessor._entry_admissible_raw_candidates([row]) == []


@pytest.mark.parametrize(
    "exception_flag",
    (
        "volume_spike_entry",
        "market_ltp_shock_entry",
        "market_wide_reversal_entry",
        "late_session_reclaim_entry",
        "one_way_cascade_entry",
    ),
)
def test_confirmed_one_minute_special_rows_reach_their_own_gate(exception_flag):
    signal = SimpleNamespace(volume_spike_entry=False)
    setattr(signal, exception_flag, True)

    assert SignalProcessor._entry_admissible_raw_candidates(
        [{"tf": "1min", "sig": signal}]
    )


def test_one_minute_emergency_exit_toggle_off_skips_exit(monkeypatch):
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            fifteen_min_one_min_support_exit_enabled=False,
            fifteen_min_one_min_support_min_hold_seconds=0.0,
        ),
    )
    scanner = Scanner.__new__(Scanner)
    scanner._profit_guard_reversal_context = lambda *args, **kwargs: pytest.fail(
        "toggle OFF must not evaluate emergency reversal"
    )
    trade = SimpleNamespace(
        timeframe="15min",
        entry_time=datetime(2026, 9, 6, 10, 0, 0),
    )

    assert scanner._fifteen_min_one_min_support_exit_decision(
        trade, 25_000.0, datetime(2026, 9, 6, 10, 1, 0)
    ) == (False, "")


def test_dashboard_controls_are_wired_end_to_end():
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    app_js = Path("dashboard/static/js/app.js").read_text(encoding="utf-8")

    assert 'id="fifteenMinOneMinSupportExitSwitch"' in html
    for value in (
        "PRIMARY_5",
        "PRIMARY_15",
        "INCLUDE_5MIN",
        "INCLUDE_1MIN_5MIN",
        "INCLUDE_1MIN_5MIN_15MIN",
    ):
        assert f'value="{value}"' in html
    assert "fifteen_min_one_min_support_exit_enabled: isChecked" in app_js
    assert "data.config.fifteen_min_one_min_support_exit_enabled" in app_js
    assert "data.config.ut_timeframe_entry_policy" in app_js


def test_dashboard_command_accepts_boolean_emergency_exit_toggle():
    assert _valid_command_payload(
        {
            "cmd": "configure",
            "fifteen_min_one_min_support_exit_enabled": False,
        }
    ) is None
    assert _valid_command_payload(
        {
            "cmd": "configure",
            "fifteen_min_one_min_support_exit_enabled": "off",
        }
    ) == "invalid_boolean:fifteen_min_one_min_support_exit_enabled"
