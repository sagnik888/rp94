from datetime import datetime, timedelta
from types import SimpleNamespace
import time

import pytest

import scanner as scanner_module
from scanner import Scanner
from data.market_data import MarketDataProvider


NOW = datetime(2026, 9, 7, 13, 30, 30)


def _settings(*, enabled=True):
    return SimpleNamespace(
        fifteen_min_one_min_support_exit_enabled=enabled,
        fifteen_min_one_min_support_min_hold_seconds=15.0,
        fifteen_min_one_min_support_fresh_trade_seconds=60.0,
        fifteen_min_one_min_support_loss_min_r=0.35,
        fifteen_min_one_min_support_stop_near_r=0.35,
        fifteen_min_one_min_support_win_giveback_pct=0.25,
        fifteen_min_one_min_support_lock_buffer_r=0.15,
        fifteen_min_one_min_support_premium_adverse_min_r=0.10,
        fifteen_min_one_min_support_min_confirmations=2,
        loss_guard_oi_pcr_votes_losing=2,
        loss_guard_oi_pcr_votes_winning=3,
        market_ltp_shock_min_volume_ratio=1.10,
        fifteen_min_fast_intelligence_enabled=True,
        fifteen_min_fast_intelligence_min_sources=3,
        fifteen_min_fast_depth_max_age_seconds=2.0,
        fifteen_min_fast_order_flow_bullish_ratio=1.08,
        fifteen_min_fast_order_flow_bearish_ratio=0.92,
        fifteen_min_live_expected_move_enabled=True,
    )


def _context(*, own_break=True, one_min_emergency=True, reason="reversal"):
    return {
        "has_context": True,
        "one_min_context": {
            "has_context": True,
            "fast_reversal": True,
            "trend_supportive": False,
            "pullback_class": "big_reversal",
            "reason": "live 1m emergency",
        },
        "one_min_emergency": one_min_emergency,
        "fast_reversal": own_break,
        "trend_supportive": not own_break,
        "trend_votes": 0 if own_break else 3,
        "pullback_class": "structure_break" if own_break else "noise",
        "own_fast_reversal": own_break,
        "own_trend_supportive": not own_break,
        "own_trend_votes": 0 if own_break else 3,
        "own_pullback_class": "structure_break" if own_break else "noise",
        "reason": reason,
    }


def _scanner(
    *,
    shock=False,
    five_break=True,
    fifteen_break=True,
    cross_adverse=False,
    intel=None,
    depth=None,
):
    scanner = Scanner.__new__(Scanner)
    calls = []

    def reversal_context(*args, timeframe=None, closed_only=False, **kwargs):
        calls.append((timeframe, closed_only))
        if timeframe == "15min":
            return _context(own_break=fifteen_break, reason="15m")
        if timeframe == "5min":
            return _context(own_break=five_break, reason="5m")
        result = _context(own_break=True, reason="closed 1m")
        result["evaluated_bar_at"] = NOW - timedelta(minutes=1)
        return result

    scanner._profit_guard_reversal_context = reversal_context
    scanner._evaluate_market_ltp_shock = lambda *args, **kwargs: (
        {"strong": True, "reason": "core 3/3 shock"} if shock else None
    )
    scanner._market_wide_reversal_context = lambda *args, **kwargs: None
    scanner._get_trade_regime = lambda trade: "RANGING"
    scanner._live_direction_context = lambda *args, **kwargs: {
        "cross_adverse": cross_adverse,
        "adverse": True,
    }
    default_intel = {
        "as_of": NOW.timestamp(),
        "data_quality": {
            "intelligence_usable": True,
            "age_seconds": 1.0,
        },
        "pcr": {"pcr_oi": 1.2},
        "greeks": {"call": {"delta": 0.5, "gamma": 0.01, "iv": 15.0}},
    }
    scanner._fresh_exact_trade_intel = lambda *args, **kwargs: (
        default_intel if intel is None else intel
    )
    scanner._intel_evidence_is_usable = lambda intel: True
    scanner._intel_adverse_votes = lambda direction, intel: 3
    scanner._intel_reversal_is_new_evidence = lambda *args, **kwargs: (True, "")
    scanner._get_depth_context = lambda *args, **kwargs: (
        depth
        if depth is not None
        else {
            "usable": True,
            "ofr": 0.75,
            "age_seconds": 0.2,
            "source": "ws_depth",
        }
    )
    scanner.config = {"indices": {"NIFTY": {}}}
    scanner._candle_thrust_metrics = lambda *args, **kwargs: {
        "volume_ratio": 1.2,
        "range_atr_ratio": 1.2,
        "close_location": 0.8,
    }
    scanner._test_reversal_calls = calls
    return scanner


def _option_trade(*, age=120.0, peak=0.0):
    return SimpleNamespace(
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        option_type="CE",
        inst_type="OPT",
        entry_time=NOW - timedelta(seconds=age),
        entry_price=100.0,
        current_price=100.0,
        initial_stop=90.0,
        trailing_stop=90.0,
        current_stop=90.0,
        quantity=100,
        peak_pnl=peak,
        underlying_entry=25_000.0,
        expected_move_pct=0.50,
        expected_move_probability=0.65,
        expected_move_horizon_minutes=30,
    )


def test_normal_loss_exit_needs_closed_one_minute_and_higher_frame(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    scanner = _scanner()
    trade = _option_trade()

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        trade,
        25_000.0,
        NOW,
        current_mark=96.0,
    )

    assert allowed is True
    assert "state=LOSING" in reason
    assert ("1min", True) in scanner._test_reversal_calls
    assert ("5min", False) in scanner._test_reversal_calls


def test_one_higher_frame_plus_local_volume_cannot_declare_regime_change(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    scanner = _scanner(five_break=False, fifteen_break=True)
    scanner._intel_adverse_votes = lambda direction, intel: 0

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        _option_trade(), 25_000.0, NOW, current_mark=96.0
    )

    assert allowed is False
    assert "regime transition needs" in reason


@pytest.mark.parametrize("missing", ("regime", "intel", "volume"))
def test_normal_regime_transition_requires_all_durable_pillars(monkeypatch, missing):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    scanner = _scanner()
    if missing == "regime":
        scanner._live_direction_context = lambda *args, **kwargs: {
            "cross_adverse": False,
            "adverse": False,
        }
    elif missing == "intel":
        scanner._intel_adverse_votes = lambda direction, intel: 0
    else:
        scanner._candle_thrust_metrics = lambda *args, **kwargs: {
            "volume_ratio": 1.0,
        }

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        _option_trade(), 25_000.0, NOW, current_mark=96.0
    )

    assert allowed is False
    assert "regime transition needs" in reason


def test_fresh_trade_allows_only_strong_market_wide_shock(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    trade = _option_trade(age=30.0)

    held, held_reason = _scanner(shock=False)._fifteen_min_one_min_support_exit_decision(
        trade, 25_000.0, NOW, current_mark=98.0
    )
    exited, exit_reason = _scanner(shock=True)._fifteen_min_one_min_support_exit_decision(
        trade, 25_000.0, NOW, current_mark=98.0
    )

    assert held is False
    assert "fresh trade" in held_reason
    assert exited is True
    assert "catastrophic multi-index" in exit_reason


def test_fresh_trade_can_exit_on_new_multi_source_intelligence_without_strong_shock(
    monkeypatch,
):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    scanner = _scanner(cross_adverse=True)
    trade = _option_trade(age=30.0)

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        trade,
        24_950.0,
        NOW,
        current_mark=96.0,
    )

    assert allowed is True
    assert "fresh multi-source reversal" in reason
    assert "market-wide" in reason
    assert "volume" in reason
    assert "order-flow" in reason


def test_fresh_trade_rejects_stale_pre_entry_chain_and_stale_depth(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    stale_intel = {
        "as_of": NOW.timestamp(),
        "data_quality": {
            "intelligence_usable": True,
            "age_seconds": 45.0,
        },
        "pcr": {"pcr_oi": 1.2},
    }
    scanner = _scanner(
        cross_adverse=True,
        intel=stale_intel,
        depth={
            "usable": True,
            "ofr": 0.70,
            "age_seconds": 5.0,
            "source": "old_depth",
        },
    )

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        _option_trade(age=30.0),
        24_950.0,
        NOW,
        current_mark=96.0,
    )

    assert allowed is False
    assert "fresh trade" in reason


@pytest.mark.parametrize(("chain_age", "expected"), [(1.0, True), (45.0, False)])
def test_fresh_oi_pcr_votes_count_only_when_chain_was_captured_after_fill(
    monkeypatch, chain_age, expected
):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    intel = {
        "as_of": NOW.timestamp(),
        "data_quality": {
            "intelligence_usable": True,
            "age_seconds": chain_age,
        },
        "pcr": {"pcr_oi": 1.2, "signal": "BEARISH"},
        "oi": {"signal": "BEARISH"},
    }
    scanner = _scanner(
        cross_adverse=True,
        intel=intel,
        depth={"usable": False, "age_seconds": None},
    )

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        _option_trade(age=30.0), 24_950.0, NOW, current_mark=96.0
    )

    assert allowed is expected
    assert ("new-OI/PCR" in reason) is expected


def test_live_expected_move_context_refreshes_inside_fresh_window(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    scanner = _scanner(cross_adverse=False)
    scanner._intel_adverse_votes = lambda direction, intel: 0 if direction == "LONG" else 3
    scanner._get_depth_context = lambda *args, **kwargs: {
        "usable": True,
        "ofr": 1.30,
        "age_seconds": 0.1,
        "source": "ws_depth",
    }
    scanner._candle_thrust_metrics = lambda *args, signal_type=None, **kwargs: {
        "volume_ratio": 1.4,
        "range_atr_ratio": 1.2,
        "close_location": 0.8,
    }
    scanner._evaluate_market_ltp_shock = lambda _base, signal, *args, **kwargs: (
        {"strong": True, "reason": "supportive core shock"}
        if signal == "BUY"
        else None
    )
    trade = _option_trade(age=30.0)

    allowed, _reason = scanner._fifteen_min_one_min_support_exit_decision(
        trade,
        25_050.0,
        NOW,
        current_mark=105.0,
    )

    assert allowed is False
    outlook = trade.live_expected_move_forecast
    assert outlook["status"] == "SUPPORTIVE"
    assert outlook["realized_directional_move_pct"] == pytest.approx(0.20)
    assert outlook["remaining_entry_expectation_pct"] == pytest.approx(0.30)
    assert "volume" in outlook["supportive_sources"]
    assert "order-flow" in outlook["supportive_sources"]


def test_depth_snapshot_exposes_transport_age_for_fast_gate():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._ws_cache = {
        "123": {
            "bid_qty": 130,
            "ask_qty": 100,
            "source": "contract_ws",
            "time": time.time() - 0.25,
        }
    }

    depth = provider.get_order_book_snapshot("NFO", "NIFTY26SEP25000CE", "123")

    assert depth["usable"] is True
    assert 0.20 <= depth["age_seconds"] <= 2.0
    assert depth["observed_at"]


def test_catastrophic_option_exit_still_requires_premium_confirmation(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    scanner = _scanner(shock=True)

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        _option_trade(age=30.0), 25_000.0, NOW, current_mark=99.5
    )

    assert allowed is False
    assert "premium has not confirmed" in reason


@pytest.mark.parametrize(
    ("mark", "peak", "expected"),
    [
        (98.0, 0.0, False),       # only -0.20R and far from stop
        (96.0, 0.0, True),        # -0.40R: meaningful loss band
        (109.0, 1_000.0, False),  # 10% peak giveback
        (107.0, 1_000.0, True),   # 30% peak giveback
    ],
)
def test_trade_state_bands_distinguish_small_moves_from_threats(
    monkeypatch, mark, peak, expected
):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    allowed, _reason = _scanner()._fifteen_min_one_min_support_exit_decision(
        _option_trade(peak=peak), 25_000.0, NOW, current_mark=mark
    )
    assert allowed is expected


def test_exact_tick_peak_makes_intracycle_option_giveback_visible(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    scanner = _scanner()
    trade = _option_trade(peak=0.0)

    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        trade,
        25_000.0,
        NOW,
        current_mark=105.0,
        observed_contract_ticks=[(115.0, NOW), (105.0, NOW)],
    )

    assert allowed is True
    assert "giveback=67%" in reason
    assert "premium_adverse=1.00R" in reason


def test_futures_use_same_risk_state_without_option_premium_veto(monkeypatch):
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    trade = SimpleNamespace(
        instrument="NIFTY",
        timeframe="15min",
        direction="LONG",
        inst_type="FUT",
        entry_time=NOW - timedelta(minutes=3),
        entry_price=25_000.0,
        current_price=24_980.0,
        initial_stop=24_950.0,
        trailing_stop=24_950.0,
        current_stop=24_950.0,
        quantity=25,
        instrument_multiplier=1.0,
        peak_pnl=0.0,
    )

    allowed, reason = _scanner()._fifteen_min_one_min_support_exit_decision(
        trade, 24_980.0, NOW, current_mark=24_980.0
    )

    assert allowed is True
    assert "state=LOSING" in reason


def test_toggle_is_clean_ab_switch(monkeypatch):
    trade = _option_trade()
    scanner = _scanner()

    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings(enabled=True))
    on = scanner._fifteen_min_one_min_support_exit_decision(
        trade, 25_000.0, NOW, current_mark=96.0
    )[0]
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings(enabled=False))
    off = scanner._fifteen_min_one_min_support_exit_decision(
        trade, 25_000.0, NOW, current_mark=96.0
    )[0]

    assert on is True
    assert off is False
