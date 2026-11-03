"""Replay expiry decisions use archived contracts and the decision's IST clock."""

import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from config.settings import get_settings
from engine.expiry_manager import ArchivedExpiryContext
from scanner import Scanner


@pytest.fixture(autouse=True)
def _expiry_policy(monkeypatch):
    settings = get_settings()
    for field, value in {
        "option_rollover_working_days": 1,
        "expiry_day_trade_current_week": False,
        "expiry_day_include_t1": False,
        "expiry_day_gamma_cutoff": "13:00",
        "expiry_day_after_cutoff_mode": "rollover",
        "expiry_day_late_window": True,
        "expiry_day_late_window_start": "14:00",
        "expiry_day_late_window_end": "15:15",
    }.items():
        monkeypatch.setattr(settings, field, value)
    return settings


def _master(root, name="NIFTY", expiries=("18AUG2026", "25AUG2026"), *, filename="kite_instruments.json"):
    root.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "name": name,
            "symbol": f"{name}{expiry}24500CE",
            "expiry": expiry,
            "instrumenttype": "OPTIDX",
            "token": str(i + 1),
            "strike": 2450000,
        }
        for i, expiry in enumerate(expiries)
    ]
    (root / filename).write_text(json.dumps(rows), encoding="utf-8")


def _option_candle(root, symbol, at="2026-08-14 10:00:00+05:30"):
    candles = root / "candles"
    candles.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {"open": [100.0], "high": [102.0], "low": [99.0], "close": [101.0], "volume": [120]},
        index=pd.DatetimeIndex([pd.Timestamp(at)], name="timestamp"),
    ).to_csv(candles / f"{symbol}_1min.csv")


def _expiry(result):
    return pd.Timestamp(result["contract_expiry"]).date()


@pytest.mark.parametrize("name", ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"])
def test_missing_archive_is_unavailable_instead_of_a_guessed_dte(tmp_path, name):
    result = ArchivedExpiryContext(tmp_path).resolve(name, datetime(2026, 8, 14, 10))
    assert result["available"] is False
    assert result["days_to_expiry"] is None
    assert result["contract_expiry"] is None
    assert result["is_expiry_day"] is False
    assert result["in_gamma_window"] is False


def test_earlier_weekly_archive_survives_a_current_master_without_that_contract(tmp_path):
    _master(tmp_path, expiries=("15SEP2026", "22SEP2026"))
    _option_candle(tmp_path, "NIFTY2681824500CE")
    result = ArchivedExpiryContext(tmp_path).resolve("NIFTY", datetime(2026, 8, 14, 10))
    assert result["available"] is True
    assert _expiry(result) == date(2026, 8, 18)
    assert result["days_to_expiry"] == 4
    assert result["source"]


@pytest.mark.parametrize("name,symbol", [
    ("BANKNIFTY", "BANKNIFTY26AUG57000CE"),
    ("MIDCPNIFTY", "MIDCPNIFTY26AUG13000PE"),
])
def test_monthly_symbol_alone_cannot_establish_an_exact_expiry(tmp_path, name, symbol):
    _option_candle(tmp_path, symbol)
    result = ArchivedExpiryContext(tmp_path).resolve(name, datetime(2026, 8, 14, 10))
    assert result["available"] is False
    assert result["days_to_expiry"] is None


@pytest.mark.parametrize("filename", ["kite_instruments.json", "instruments.json"])
def test_archived_monthly_metadata_keeps_the_actual_expiry_date(tmp_path, filename):
    # The dated metadata, rather than a guessed weekday/month-end rule, is the evidence.
    _master(tmp_path, "BANKNIFTY", ("25AUG2026", "29SEP2026"), filename=filename)
    result = ArchivedExpiryContext(tmp_path).resolve("BANKNIFTY", datetime(2026, 8, 20, 10))
    assert result["available"] is True
    assert _expiry(result) == date(2026, 8, 25)
    assert result["days_to_expiry"] == 5


def test_other_index_and_futures_metadata_cannot_supply_option_expiry(tmp_path):
    _master(tmp_path, "BANKNIFTY", ("25AUG2026",))
    rows = json.loads((tmp_path / "kite_instruments.json").read_text())
    rows.append({"name": "NIFTY", "symbol": "NIFTY26AUGFUT", "expiry": "25AUG2026", "instrumenttype": "FUTIDX"})
    (tmp_path / "kite_instruments.json").write_text(json.dumps(rows), encoding="utf-8")
    result = ArchivedExpiryContext(tmp_path).resolve("NIFTY", datetime(2026, 8, 14, 10))
    assert result["available"] is False


def test_expired_weekly_archive_is_not_selected_after_expiry(tmp_path):
    _option_candle(tmp_path, "NIFTY2681824500CE")
    result = ArchivedExpiryContext(tmp_path).resolve("NIFTY", datetime(2026, 8, 19, 10))
    assert result["available"] is False
    assert result["days_to_expiry"] is None


@pytest.mark.parametrize("master", [{"unexpected": "schema"}, [None], [{"name": "NIFTY", "expiry": "bad-date"}]])
def test_malformed_local_master_does_not_break_dated_archive_fallback(tmp_path, master):
    (tmp_path / "kite_instruments.json").write_text(json.dumps(master), encoding="utf-8")
    _option_candle(tmp_path, "NIFTY2681824500CE")
    result = ArchivedExpiryContext(tmp_path).resolve("NIFTY", datetime(2026, 8, 14, 10))
    assert result["available"] is True
    assert _expiry(result) == date(2026, 8, 18)


def test_rollover_and_expiry_day_are_distinct_facts(tmp_path):
    _master(tmp_path)
    resolver = ArchivedExpiryContext(tmp_path)
    before_rollover = resolver.resolve("NIFTY", datetime(2026, 8, 14, 10))
    on_rollover = resolver.resolve("NIFTY", datetime(2026, 8, 17, 10))
    expiry_day = resolver.resolve("NIFTY", datetime(2026, 8, 18, 10))
    assert _expiry(before_rollover) == date(2026, 8, 18)
    assert _expiry(on_rollover) == date(2026, 8, 25)
    assert on_rollover["days_to_expiry"] == 8
    assert on_rollover["is_expiry_day"] is False
    assert _expiry(expiry_day) == date(2026, 8, 25)
    assert expiry_day["is_expiry_day"] is True
    assert expiry_day["in_gamma_window"] is False


@pytest.mark.parametrize("clock,expected_expiry,gamma", [
    ("2026-08-18 12:59:00+05:30", date(2026, 8, 18), True),
    ("2026-08-18 13:00:00+05:30", date(2026, 8, 25), False),
    ("2026-08-18 14:00:00+05:30", date(2026, 8, 18), True),
    ("2026-08-18 15:15:00+05:30", date(2026, 8, 25), False),
    ("2026-08-18 07:29:00+00:00", date(2026, 8, 18), True),
    ("2026-08-18 07:30:00+00:00", date(2026, 8, 25), False),
])
def test_gamma_uses_asof_intraday_clock_in_ist(tmp_path, _expiry_policy, clock, expected_expiry, gamma):
    _expiry_policy.expiry_day_trade_current_week = True
    _master(tmp_path)
    result = ArchivedExpiryContext(tmp_path).resolve("NIFTY", pd.Timestamp(clock))
    assert _expiry(result) == expected_expiry
    assert result["in_gamma_window"] is gamma
    assert result["is_expiry_day"] is True


def test_same_resolver_does_not_cache_a_later_intraday_gamma_decision(tmp_path, _expiry_policy):
    _expiry_policy.expiry_day_trade_current_week = True
    _master(tmp_path)
    resolver = ArchivedExpiryContext(tmp_path)
    assert resolver.resolve("NIFTY", datetime(2026, 8, 18, 13, 10))["in_gamma_window"] is False
    assert resolver.resolve("NIFTY", datetime(2026, 8, 18, 10))["in_gamma_window"] is True


def test_include_t1_gamma_setting_changes_selected_contract(tmp_path, _expiry_policy):
    _expiry_policy.expiry_day_trade_current_week = True
    _master(tmp_path)
    resolver = ArchivedExpiryContext(tmp_path)
    at = datetime(2026, 8, 17, 14)
    assert _expiry(resolver.resolve("NIFTY", at)) == date(2026, 8, 25)
    _expiry_policy.expiry_day_include_t1 = True
    result = resolver.resolve("NIFTY", at)
    assert _expiry(result) == date(2026, 8, 18)
    assert result["in_gamma_window"] is True
    assert result["is_expiry_day"] is False


def test_verified_execution_contract_dte_does_not_roll_to_another_expiry(tmp_path):
    _master(tmp_path)
    result = ArchivedExpiryContext(tmp_path).resolve(
        "NIFTY", datetime(2026, 8, 17, 10), contract_expiry="2026-08-18", contract_verified=True,
    )
    assert result["available"] is True
    assert _expiry(result) == date(2026, 8, 18)
    assert result["days_to_expiry"] == 1


def test_unverified_execution_expiry_cannot_fall_through_to_another_contract(tmp_path):
    _master(tmp_path)
    result = ArchivedExpiryContext(tmp_path).resolve(
        "NIFTY", datetime(2026, 8, 17, 10), contract_expiry="2026-08-18", contract_verified=False,
    )
    assert result["available"] is False
    assert result["days_to_expiry"] is None


@pytest.mark.parametrize("populated", [False, True])
def test_scanner_historical_helpers_ignore_live_manager_state(tmp_path, populated):
    _master(tmp_path)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner._historical_expiry_resolver = ArchivedExpiryContext(tmp_path)
    scanner.expiry = SimpleNamespace(
        df=pd.DataFrame([{"expiry": "15SEP2026"}]) if populated else None,
        expiries={"NIFTY": {"active_weekly": "15SEP2026"}} if populated else {},
        get_dte=Mock(side_effect=AssertionError("historical DTE consulted live state")),
        is_expiry_day=Mock(side_effect=AssertionError("historical expiry day consulted live state")),
        in_gamma_window=Mock(side_effect=AssertionError("historical gamma consulted live state")),
    )
    at = datetime(2026, 8, 18, 13, 10)
    assert scanner._days_to_expiry_for("NIFTY", at) == 7
    assert scanner._is_expiry_day_for("NIFTY", at) is True
    assert scanner._in_gamma_window_for("NIFTY", at) is False
    scanner.expiry.get_dte.assert_not_called()
    scanner.expiry.is_expiry_day.assert_not_called()
    scanner.expiry.in_gamma_window.assert_not_called()


def test_scanner_unknown_historical_expiry_stays_unavailable(tmp_path):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "HISTORICAL"
    scanner._historical_expiry_resolver = ArchivedExpiryContext(tmp_path)
    scanner.expiry = SimpleNamespace(get_dte=lambda *_: 3, is_expiry_day=lambda *_: True, in_gamma_window=lambda *_: True)
    at = datetime(2026, 8, 18, 10)
    assert scanner._days_to_expiry_for("NIFTY", at) is None
    assert scanner._is_expiry_day_for("NIFTY", at) is False
    assert scanner._in_gamma_window_for("NIFTY", at) is False


def test_real_helpers_preserve_live_manager_delegation():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._historical_expiry_resolver = SimpleNamespace(resolve=Mock(side_effect=AssertionError("live path used archived expiry")))
    scanner.expiry = SimpleNamespace(get_dte=Mock(return_value=2), is_expiry_day=Mock(return_value=True), in_gamma_window=Mock(return_value=True))
    at = datetime(2026, 8, 18, 10)
    assert scanner._days_to_expiry_for("NIFTY", at) == 2
    assert scanner._is_expiry_day_for("NIFTY", at) is True
    assert scanner._in_gamma_window_for("NIFTY", at) is True
    scanner.expiry.get_dte.assert_called_once_with("NIFTY", at.date())
    scanner.expiry.is_expiry_day.assert_called_once_with("NIFTY", at.date())
    scanner.expiry.in_gamma_window.assert_called_once_with("NIFTY", at)
