"""Observed live manual marks survive display/restore without becoming ideal fills."""
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from engine.candidate_normalization import normalize_loaded_exit_candidate
from engine.trade_row_adapter import reconcile_legacy_trade_row


def row(**changes):
    result = dict(instrument="BANKNIFTY", inst_type="OPT", direction="SHORT",
                  action="EXIT", status="EXIT SIGNAL", price=781.15, entry_price=781.15,
                  stop=742.0925, target=820.2075, initial_stop=742.0925, rr=1.0,
                  lots=1, lot_size=30, exit_price=735.15, current_price=735.15,
                  exit_reason="STOP_HIT", pnl=-1460.0, reasons=[],
                  data_provenance="LIVE", accepted_by_gate=True,
                  entry_timestamp=datetime(2026, 10, 5, 12, 1, 35),
                  exit_timestamp=datetime(2026, 10, 5, 12, 3, 56))
    result.update(changes)
    return result


def normalize(candidate):
    return normalize_loaded_exit_candidate(
        candidate, futures_sl_pct=0.1, options_sl_pct=15.0,
        candidate_exit_resolution=lambda c, px, reason, text: (px, reason, text),
        candidate_net_pnl=lambda c, px: round((px-c.price)*c.lots*c.lot_size-80., 2))


@pytest.mark.parametrize("reason,exit_price,expected", [
    ("STOP_HIT", 735.15, -1460.0), ("TRAILING_STOP", 734.8, -1470.5),
    ("SL HIT", 735.15, -1460.0), ("SL_HIT", 735.15, -1460.0),
    ("TARGET_HIT", 828.0, 1325.5),
])
def test_observed_live_exit_survives_repeated_candidate_normalization(reason, exit_price, expected):
    candidate = NS(**row(exit_reason=reason, exit_price=exit_price,
                         current_price=exit_price, pnl=expected))
    original_levels = candidate.stop, candidate.target
    normalize(candidate)
    normalize(candidate)
    assert candidate.exit_price == exit_price
    assert candidate.current_price == exit_price
    assert candidate.pnl == expected
    assert (candidate.stop, candidate.target) == original_levels
    assert candidate.exit_reason == reason


def test_live_exit_is_not_repriced_when_current_settings_change():
    candidate = NS(**row(stop=650.0, initial_stop=650.0, target=950.0))
    normalize(candidate)
    assert (candidate.stop, candidate.target, candidate.exit_price, candidate.pnl) == (650., 950., 735.15, -1460.)


def test_observed_watch_only_live_exit_is_preserved():
    candidate = NS(**row(accepted_by_gate=False, watch_only=True))
    normalize(candidate)
    assert candidate.exit_price == 735.15


def test_live_observation_retains_its_price_precision():
    candidate = NS(**row(exit_price=735.151234567, current_price=735.151234567))
    normalize(candidate)
    assert candidate.exit_price == candidate.current_price == 735.151234567


@pytest.mark.parametrize("changes", [
    {"data_provenance": "BACKTEST"}, {"data_provenance": ""},
    {"accepted_by_gate": False}, {"exit_timestamp": None},
    {"exit_timestamp": "broken"}, {"exit_timestamp": datetime(2026, 10, 5, 11, 59)},
])
def test_legacy_or_missing_live_evidence_keeps_existing_geometry_repair(changes):
    candidate = NS(**row(**changes))
    normalize(candidate)
    assert candidate.exit_price == candidate.stop == 742.0925
    assert candidate.pnl == -1251.73


@pytest.mark.parametrize("reason,exit_price", [("STOP_HIT", 735.15), ("TARGET_HIT", 828.0)])
def test_live_manual_row_adapter_keeps_observed_mark_and_does_not_mutate_input(monkeypatch, reason, exit_price):
    monkeypatch.setattr("engine.trade_row_adapter.get_settings", lambda: NS(opt_cost=80., fut_cost=200.))
    original = row(exit_reason=reason, exit_price=exit_price, current_price=exit_price)
    original["entry_timestamp"] = original["entry_timestamp"].isoformat()
    original["exit_timestamp"] = original["exit_timestamp"].isoformat()
    repaired = reconcile_legacy_trade_row(original)
    assert repaired["exit_price"] == exit_price
    assert repaired["pnl"] == round((exit_price-781.15)*30-80., 2)
    assert original["exit_price"] == exit_price


def test_quote_invalid_live_row_remains_excluded():
    candidate = NS(**row(exit_reason="QUOTE_INVALID", exit_price=305., current_price=305., pnl=0.))
    normalize(candidate)
    assert candidate.exit_price == 305.
    assert candidate.pnl == 0.


@pytest.mark.parametrize("inst_type,direction,entry,stop,exit_,expected", [
    ("OPT", "LONG", 100., 95., 93., -430.),
    ("OPT", "SHORT", 100., 95., 93., -430.),
    ("FUT", "LONG", 20000., 19990., 19985., -950.),
    ("FUT", "SHORT", 20000., 20010., 20015., -950.),
])
def test_adapter_keeps_mark_with_correct_product_price_direction(monkeypatch, inst_type, direction, entry, stop, exit_, expected):
    monkeypatch.setattr("engine.trade_row_adapter.get_settings", lambda: NS(opt_cost=80., fut_cost=200.))
    original = row(inst_type=inst_type, direction=direction, price=entry, entry_price=entry,
                   stop=stop, exit_price=exit_, current_price=exit_, lot_size=50)
    result = reconcile_legacy_trade_row(original)
    assert result["exit_price"] == exit_
    assert result["pnl"] == expected


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1., 0.])
def test_invalid_explicit_exit_is_not_treated_as_verified_live_mark(monkeypatch, price):
    monkeypatch.setattr("engine.trade_row_adapter.get_settings", lambda: NS(opt_cost=80., fut_cost=200.))
    repaired = reconcile_legacy_trade_row(row(exit_price=price))
    assert repaired["exit_price"] == 742.0925
