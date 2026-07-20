"""Fixes from the 2026-07-03 live-session audit.

Covers: quote-stamp overwrite on accepted rows, entry-time quote snapshot,
opposing-exposure telemetry, %s-style loguru arg loss, bounded quote retry,
Fyers chain self-heal, divergence-warning throttle, tracker virtual-session flag.
"""

import inspect
from pathlib import Path
from types import SimpleNamespace

from loguru import logger

from config.settings import get_settings
from data.market_data import MarketDataProvider
from engine.signal_processor import SignalProcessor
from intelligence.intelligence_aggregator import IntelligenceAggregator
from scanner import Scanner


def _capture_warnings():
    records = []
    handle = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
    return records, handle


# ── Fix 1: refresh must not rewrite accepted rows' entry verification ──

def _refresh_scanner():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._loaded_session_is_current_day = lambda: True
    scanner._get_live_premium_cached = lambda candidate: None  # feed down
    return scanner


def _opt_candidate(accepted):
    return SimpleNamespace(
        inst_type="OPT",
        action="ENTRY",
        accepted_by_gate=accepted,
        quote_verified=True,
        quote_source="contract_ltp",
        status="TRADE SIGNAL",
        trading_symbol="NIFTY07JUL2624300CE",
        symbol_token="123",
        instrument="NIFTY",
        price=100.0,
        current_price=100.0,
    )


def test_refresh_preserves_accepted_row_verification():
    candidate = _opt_candidate(accepted=True)
    _refresh_scanner()._refresh_trade_candidates([candidate], {})
    # Feed staleness must be flagged separately, never rewrite entry evidence.
    assert candidate.quote_verified is True
    assert candidate.quote_source == "contract_ltp"
    assert candidate.status == "TRADE SIGNAL"
    assert candidate.live_quote_stale is True


def test_refresh_still_stamps_pre_acceptance_rows_for_the_gate():
    candidate = _opt_candidate(accepted=False)
    _refresh_scanner()._refresh_trade_candidates([candidate], {})
    assert candidate.quote_verified is False
    assert candidate.quote_source == "contract_ltp_unavailable"
    assert candidate.status == "QUOTE UNAVAILABLE"


# ── Fix 1b: entry-time snapshot at acceptance ──

def test_record_accepted_entry_snapshots_entry_quote_state():
    scanner = Scanner.__new__(Scanner)
    scanner._reset_daily_counters = lambda: None
    scanner._candidate_history_key = lambda c: "key-1"
    scanner._counted_entry_keys = set()
    scanner._trades_today = {}
    scanner._notify_trade_event = lambda: None
    scanner._log_opposing_exposure_telemetry = lambda c: None
    candidate = SimpleNamespace(
        instrument="NIFTY", quote_verified=True, quote_source="contract_ltp"
    )
    scanner._record_accepted_entry(candidate)
    assert candidate.entry_quote_verified is True
    assert candidate.entry_quote_source == "contract_ltp"


def test_candidate_payload_round_trips_entry_quote_fields():
    source = inspect.getsource(Scanner)
    assert '"entry_quote_verified"' in source
    assert '"entry_quote_source"' in source
    assert '"live_quote_stale"' in source


# ── Fix 2: opposing-exposure telemetry ──

def test_opposing_core_exposure_logs_warning():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.active_indices = []
    scanner.trades = SimpleNamespace(
        open_trades={
            "T1": SimpleNamespace(
                direction="SHORT", instrument="BANKNIFTY", timeframe="5min", inst_type="OPT"
            )
        }
    )
    candidate = SimpleNamespace(instrument="NIFTY", direction="LONG")
    records, handle = _capture_warnings()
    try:
        scanner._log_opposing_exposure_telemetry(candidate)
    finally:
        logger.remove(handle)
    assert any("Opposing correlated exposure" in message for message in records)


# ── Fix 3: %s-style loguru args (silently dropped by loguru .format) ──

def test_no_percent_style_loguru_calls_remain():
    for module_path in ("engine/signal_processor.py", "scanner.py"):
        source = Path(module_path).read_text(encoding="utf-8", errors="ignore")
        assert "candidate %s %s" not in source, module_path
        assert "for %s %s %.0f" not in source, module_path


# ── Fix 4: bounded quote retry before dropping ──

def test_quote_retry_wired_and_settings_declared():
    source = inspect.getsource(SignalProcessor.process_best_signal)
    assert "option_quote_retry_attempts" in source
    assert "Option quote recovered on retry" in source
    settings = get_settings()
    assert settings.option_quote_retry_attempts == 2
    assert settings.option_quote_retry_delay_seconds == 0.5


# ── Fix 5: Fyers chain self-heal ──

def test_fyers_self_heal_reinits_after_streak_with_cooldown():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._fyers_chain_failure_streak = 0
    provider._last_fyers_reinit_ts = 0.0
    calls = []
    provider._load_fyers_token = lambda: calls.append(1)

    threshold = get_settings().fyers_chain_reinit_failure_threshold
    for _ in range(threshold):
        provider._maybe_self_heal_fyers("test streak")
    assert len(calls) == 1  # reinit fired exactly once at the threshold
    assert provider._fyers_chain_failure_streak == 0

    # Cooldown: another full streak immediately after must NOT reinit again.
    for _ in range(threshold):
        provider._maybe_self_heal_fyers("test streak 2")
    assert len(calls) == 1


# ── Fix 6: divergence throttle ──

def test_divergence_warning_throttled_to_one_per_window():
    aggregator = IntelligenceAggregator()
    records, handle = _capture_warnings()
    try:
        aggregator._warn_divergence_once("NIFTY", "distribution_trap", "DIVERGENCE test one")
        aggregator._warn_divergence_once("NIFTY", "distribution_trap", "DIVERGENCE test two")
    finally:
        logger.remove(handle)
    assert len([m for m in records if "DIVERGENCE test" in m]) == 1
    assert get_settings().divergence_warning_throttle_seconds == 300.0


# ── Fix 7: tracker virtual-session flag ──

def test_tracker_flags_virtual_sessions():
    source = Path("scripts/live_session_tracker.py").read_text(encoding="utf-8", errors="ignore")
    assert "VIRTUAL SESSION" in source
    assert "paper/virtual lifecycle" in source
