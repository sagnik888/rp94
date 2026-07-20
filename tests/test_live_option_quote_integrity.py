from types import SimpleNamespace
from datetime import datetime, timedelta

import pandas as pd
import pytest

from engine.signal_processor import TradeCandidate
from scanner import Scanner


def _option_candidate(**overrides):
    values = {
        "instrument": "NIFTY",
        "direction": "SHORT",
        "price": 289.50,
        "stop": 246.07,
        "target": 467.54,
        "lots": 1,
        "lot_size": 65,
        "grade": "A+",
        "confidence": 1.0,
        "timeframe": "15min",
        "inst_type": "OPT",
        "option_type": "PE",
        "atm_strike": 24150,
        "multiplier": -0.5,
        "trading_symbol": "NIFTY30JUN2624150PE",
        "symbol_token": "79735",
    }
    values.update(overrides)
    candidate = TradeCandidate(**values)
    candidate.entry_spot = 24125.0
    return candidate


def test_spot_percentage_option_entry_is_identified_as_synthetic():
    candidate = _option_candidate(price=24125.0 * 0.012)
    assert Scanner._looks_like_synthetic_option_entry(candidate) is True


def test_spot_percentage_fingerprint_survives_small_entry_spot_refresh():
    candidate = _option_candidate(price=289.4994)
    candidate.entry_spot = 24137.75
    assert Scanner._looks_like_synthetic_option_entry(candidate) is True


def test_real_option_premium_is_not_identified_as_synthetic():
    candidate = _option_candidate(price=160.50)
    assert Scanner._looks_like_synthetic_option_entry(candidate) is False


def test_execution_is_blocked_when_live_option_quote_is_missing():
    scanner = Scanner.__new__(Scanner)
    scanner.trades = SimpleNamespace(options_sl_pct=15.0)
    scanner._get_live_premium_cached = lambda candidate: None
    scanner.log_signal_decision_once = lambda *args, **kwargs: None

    assert scanner._execution_levels(_option_candidate()) == (0.0, 0.0, 0.0)


def test_execution_rebases_levels_on_verified_live_option_quote():
    scanner = Scanner.__new__(Scanner)
    scanner.trades = SimpleNamespace(options_sl_pct=15.0)
    scanner._get_live_premium_cached = lambda candidate: 160.50
    scanner.log_signal_decision_once = lambda *args, **kwargs: None

    entry, stop, target = scanner._execution_levels(_option_candidate())

    assert entry == 160.50
    assert round(stop, 3) == 136.425
    assert target > entry


def test_execution_uses_scanner_option_sl_cap_over_stale_trade_manager():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    scanner.risk_opt_pct = 7.0
    scanner.trades = SimpleNamespace(options_sl_pct=7.0)
    scanner.latest_regimes = {"NIFTY": "TRENDING_UP"}
    scanner._get_live_premium_cached = lambda candidate: 208.50
    scanner.log_signal_decision_once = lambda *args, **kwargs: None

    entry, stop, target = scanner._execution_levels(_option_candidate(option_type="CE"))

    assert entry == 208.50
    assert round(stop, 3) == 177.225
    assert target > entry


def test_live_option_premium_falls_back_to_option_chain_ltp():
    scanner = Scanner.__new__(Scanner)
    scanner._premium_cache = {}
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 14406.0)
    scanner.data = SimpleNamespace(
        get_ltp=lambda exchange, symbol, token: None,
        get_option_chain=lambda instrument, exchange: pd.DataFrame(
            [
                {
                    "strike": 14425.0,
                    "call_ltp": 565.1,
                    "put_ltp": 812.35,
                    "put_symbol": "MIDCPNIFTY28JUL2614425PE",
                    "put_token": "63490",
                }
            ]
        ),
    )
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    candidate = _option_candidate(
        instrument="MIDCPNIFTY",
        price=806.85,
        target=1033.33,
        atm_strike=14425,
        trading_symbol="MIDCPNIFTY28JUL2614425PE",
        symbol_token="63490",
    )

    assert scanner._get_live_premium_cached(candidate) == 812.35


def test_live_option_premium_prefers_exact_token_ltp_over_strike_only_chain_ltp():
    scanner = Scanner.__new__(Scanner)
    scanner._premium_cache = {}
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 14406.0)
    scanner.data = SimpleNamespace(
        get_ltp=lambda exchange, symbol, token: 806.85,
        get_option_chain=lambda instrument, exchange: pd.DataFrame(
            [{"strike": 14425.0, "call_ltp": 565.1, "put_ltp": 302.55}]
        ),
    )
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    candidate = _option_candidate(
        instrument="MIDCPNIFTY",
        price=806.85,
        target=1033.33,
        atm_strike=14425,
        trading_symbol="MIDCPNIFTY28JUL2614425PE",
        symbol_token="63490",
    )

    assert scanner._get_live_premium_cached(candidate) == 806.85
    assert candidate.quote_source == "broker_contract_ltp"


def test_strike_only_option_chain_cannot_mark_exact_contract_when_token_ltp_missing():
    scanner = Scanner.__new__(Scanner)
    scanner._premium_cache = {}
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 14406.0)
    scanner.data = SimpleNamespace(
        get_ltp=lambda exchange, symbol, token: None,
        get_option_chain=lambda instrument, exchange: pd.DataFrame(
            [{"strike": 14425.0, "call_ltp": 565.1, "put_ltp": 302.55}]
        ),
    )
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    candidate = _option_candidate(
        instrument="MIDCPNIFTY",
        price=806.85,
        target=1033.33,
        atm_strike=14425,
        trading_symbol="MIDCPNIFTY28JUL2614425PE",
        symbol_token="63490",
    )

    assert scanner._get_live_premium_cached(candidate) is None


def test_stale_exact_option_token_ltp_is_not_used_as_live_mark():
    scanner = Scanner.__new__(Scanner)
    scanner._premium_cache = {}
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 14406.0)
    old_time = datetime.now() - timedelta(seconds=45)
    scanner.data = SimpleNamespace(
        _ws_cache={},
        _ltp_cache={"MIDCPNIFTY28JUL2614325CE": {"ltp": 607.55, "time": old_time}},
        get_ltp=lambda exchange, symbol, token: 607.55,
        get_option_chain=lambda instrument, exchange: pd.DataFrame(
            [{"strike": 14325.0, "call_ltp": 607.55, "put_ltp": 0.0}]
        ),
    )
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    candidate = _option_candidate(
        instrument="MIDCPNIFTY",
        direction="LONG",
        option_type="CE",
        price=607.55,
        target=737.68,
        atm_strike=14325,
        trading_symbol="MIDCPNIFTY28JUL2614325CE",
        symbol_token="63481",
    )

    assert scanner._get_live_premium_cached(candidate) is None


def test_refresh_marks_option_quote_unavailable_instead_of_using_entry_fallback():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._premium_cache = {}
    scanner._loaded_session_is_current_day = lambda: True
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 14406.0)
    scanner.data = SimpleNamespace(
        _ws_cache={},
        _ltp_cache={},
        get_ltp=lambda exchange, symbol, token: None,
        get_option_chain=lambda instrument, exchange: pd.DataFrame(
            [{"strike": 14325.0, "call_ltp": 607.55, "put_ltp": 0.0}]
        ),
    )
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    scanner._candidate_net_pnl = lambda candidate, price: 123.0
    candidate = _option_candidate(
        instrument="MIDCPNIFTY",
        direction="LONG",
        option_type="CE",
        price=607.55,
        current_price=607.55,
        target=737.68,
        atm_strike=14325,
        trading_symbol="MIDCPNIFTY28JUL2614325CE",
        symbol_token="63481",
    )

    scanner._refresh_trade_candidates([candidate], {})

    assert candidate.status == "QUOTE UNAVAILABLE"
    assert candidate.quote_verified is False
    assert candidate.quote_source == "contract_ltp_unavailable"
    assert candidate.pnl == 0.0


def test_quote_unverified_option_cannot_enter_session_lifecycle():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    missing = _option_candidate()
    missing.status = "QUOTE UNAVAILABLE"
    missing.quote_verified = False
    missing.quote_source = "contract_ltp_unavailable"
    verified = _option_candidate()
    verified.quote_verified = True
    verified.quote_source = "broker_contract_ltp"

    assert scanner._candidate_quote_ready_for_lifecycle(missing) is False
    assert scanner._candidate_quote_ready_for_lifecycle(verified) is True


def test_legacy_synthetic_signal_is_rebased_to_verified_quote():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    scanner.trades = SimpleNamespace(options_sl_pct=15.0)
    scanner._candidate_net_pnl = lambda candidate, price: -80.0
    candidate = _option_candidate(price=289.4994)

    repaired = scanner._rebase_option_candidate_to_verified_quote(candidate, 160.80)

    assert repaired is True
    assert candidate.price == 160.80
    assert candidate.current_price == 160.80
    assert round(candidate.stop, 2) == 136.68
    assert candidate.target > candidate.price
    assert candidate.quote_verified is True


def test_open_option_signal_pnl_uses_configured_cost_without_hypothetical_slippage():
    scanner = Scanner.__new__(Scanner)
    candidate = _option_candidate(price=160.80)

    assert scanner._candidate_net_pnl(candidate, 159.70) == -151.50


def test_entry_serializer_keeps_original_signal_levels_after_management_tightens():
    scanner = Scanner.__new__(Scanner)
    scanner._candidate_history_key = lambda candidate: "NIFTY|15min|ENTRY"
    scanner._candidate_lifecycle_key = lambda candidate: "NIFTY|15min|SHORT|12:00"
    candidate = _option_candidate(price=160.80, stop=146.30, target=220.975)
    candidate.display_stop = 144.72
    candidate.display_target = 227.53

    row = scanner._serialize_trade_candidate(candidate)

    assert row["stop"] == 144.72
    assert row["target"] == 227.53
    assert row["current_stop"] == 146.30
    assert row["current_target"] == 220.975
    assert row["entry_price"] == 160.80
    assert row["entry_time"] == row["entry_timestamp"]
    assert row["option_symbol"] == row["trading_symbol"]


def test_accepted_signal_contract_cannot_be_replaced_by_same_candle_rescan():
    scanner = Scanner.__new__(Scanner)
    scanner._ensure_session_candidate_day = lambda: None
    scanner._persist_session_trade_candidates = lambda: None
    scanner._candidate_history_key = lambda candidate: "NIFTY|15min|SHORT|12:00"
    option = _option_candidate(price=160.80)
    option.accepted_by_gate = True
    future = _option_candidate(
        price=24147.10,
        inst_type="FUT",
        option_type="",
        trading_symbol="NIFTY30JUN26FUT",
        symbol_token="62329",
    )
    scanner._session_trade_candidates = {"NIFTY": {"NIFTY|15min|SHORT|12:00": option}}

    scanner._remember_trade_candidates([future])

    saved = scanner._session_trade_candidates["NIFTY"]["NIFTY|15min|SHORT|12:00"]
    assert saved.trading_symbol == "NIFTY30JUN2624150PE"
    assert saved.price == 160.80


class _Sentinel(Exception):
    """Raised by the control path to prove the exit loop proceeded past the hold."""


def _breach_scanner(candidate):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._loaded_session_is_current_day = lambda: True
    scanner._active_session_entry_candidates = lambda instrument: [candidate]
    scanner.diag_events = []
    scanner._diag_inc = lambda key, sub: scanner.diag_events.append((key, sub))
    scanner.log_lines = []
    scanner.log_event = lambda msg, level="info": scanner.log_lines.append(str(msg))
    scanner._notify = lambda *args, **kwargs: None

    def _raise_sentinel(*args, **kwargs):
        raise _Sentinel()

    # First helper the loop touches after the quote hold; a sentinel here proves
    # the candidate was NOT held.
    scanner._ensure_trade_like_initial_stop = _raise_sentinel
    return scanner


def test_unverified_option_quote_holds_all_session_exit_management():
    candidate = _option_candidate()
    candidate.instrument = "MIDCPNIFTY"
    candidate.quote_verified = False
    candidate.quote_source = "contract_ltp_unavailable"
    scanner = _breach_scanner(candidate)

    scanner._check_session_candidates_breaches_locked("MIDCPNIFTY")

    assert ("quote_guard", "option_exit_skipped_unverified") in scanner.diag_events
    assert any("exit checks skipped" in line for line in scanner.log_lines)


def test_verified_option_quote_proceeds_to_session_exit_management():
    candidate = _option_candidate()
    candidate.instrument = "MIDCPNIFTY"
    candidate.quote_verified = True
    candidate.quote_source = "broker_contract_ltp"
    scanner = _breach_scanner(candidate)

    with pytest.raises(_Sentinel):
        scanner._check_session_candidates_breaches_locked("MIDCPNIFTY")

    assert ("quote_guard", "option_exit_skipped_unverified") not in scanner.diag_events
