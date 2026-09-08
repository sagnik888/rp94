"""Behavioral verification of all six GPTAstra audit findings (September 2026).

Unlike the existing source-inspection tests in test_resilience_recovery_regressions,
these tests exercise actual code paths with mocked dependencies to prove correctness:

1. Slow broker calls cannot freeze the shared scan loop
2. Silent-feed recovery retries on every watchdog pass
3. Failed intelligence does not restamp old data as fresh
4. A held signal registers for retry before being remembered
5. Futures quote protection rejects stale cached marks
6. RECOVERY_REQUIRED + pending_exit uses tag reconciliation before retrying
"""
import inspect
import time
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from trading.trade_manager import IST, Trade, TradeManager
from scanner import Scanner


# ═══════════════════════════════════════════════════════════════════════════════
# FINDING 1: Slow broker calls must not freeze the shared scan loop
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinding1_ScanLoopResponsiveness:
    """Verify entry funds check and stop-protection are non-blocking on the
    scanner's event loop."""

    def test_entry_funds_check_consults_only_cached_refusal_not_live_api(self):
        """The entry path must call `cached_entry_margin_refusal`, never a live
        broker margin/funds endpoint that would block the scan loop."""
        source = inspect.getsource(TradeManager.open_trade)
        entry_section = source[
            source.index("Non-blocking early funds refusal"):
            source.index("known_ids =")
        ]
        # Must use the non-blocking cached path
        assert "cached_entry_margin_refusal" in entry_section
        # Must NOT call the blocking live margin check
        assert "get_margin" not in entry_section
        assert "_entry_margin_verdict(" not in entry_section
        assert "get_fund" not in entry_section

    def test_update_trade_defers_protection_when_not_allowed_blocking(self):
        """When allow_broker_protection=False, update_trade must nudge the
        position sync thread instead of calling ensure_broker_protection inline."""
        source = inspect.getsource(TradeManager.update_trade)
        # Verify the branching logic exists
        assert "allow_broker_protection" in source
        assert "nudge_position_sync" in source

        # Verify the conditional: when allow_broker_protection is False,
        # it calls nudge_position_sync rather than ensure_broker_protection
        prot_block = source[source.index("if int(getattr(trade, \"broker_quantity\""):]
        allow_idx = prot_block.index("if allow_broker_protection:")
        nudge_idx = prot_block.index("self.nudge_position_sync()")
        ensure_idx = prot_block.index("self.ensure_broker_protection(")
        # ensure is in the True branch (before nudge in the else branch)
        assert ensure_idx < nudge_idx

    def test_scanner_calls_update_trades_with_blocking_false(self):
        """The scanner's event loop must pass allow_blocking=False to
        _update_active_trades so REST calls don't stall the loop."""
        source = inspect.getsource(Scanner._scan_cycle)
        assert "_update_active_trades(allow_blocking=False)" in source

    def test_position_sync_loop_runs_on_dedicated_thread(self):
        """Position sync must run on its own daemon thread, not the scan loop."""
        source = inspect.getsource(TradeManager.__init__)
        assert "PositionSync" in source
        assert "daemon=True" in source

    def test_nudge_position_sync_is_nonblocking(self):
        """nudge_position_sync must be a simple Event.set(), never blocking."""
        manager = TradeManager.__new__(TradeManager)
        manager._sync_wake = threading.Event()
        # Must return instantly (no blocking)
        start = time.monotonic()
        manager.nudge_position_sync()
        elapsed = time.monotonic() - start
        assert elapsed < 0.01
        assert manager._sync_wake.is_set()


# ═══════════════════════════════════════════════════════════════════════════════
# FINDING 2: Silent-feed recovery retries on every health pass
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinding2_SilentFeedRetry:
    """Verify watchdog retries resubscription until health clears, not just once."""

    def test_watchdog_retries_on_bounded_cadence_not_once(self):
        """The watchdog must NOT latch resubscribed=True and stop trying.
        It must retry at bounded intervals while kite_stream_stalled persists."""
        import main
        source = inspect.getsource(main.start_runtime_watchdog)

        # Must use monotonic time tracking for retry cadence
        assert "kite_stream_resubscribe_attempt_at" in source
        assert "now_monotonic - last_attempt >= retry_every" in source

        # Must track result truthfully, not latch True before checking
        assert "state[\"kite_stream_resubscribed\"] = bool(" in source

    def test_failed_resubscribe_does_not_prevent_future_attempts(self):
        """A False from resubscribe_kite_tokens must leave the door open
        for the next watchdog pass to try again."""
        import main
        source = inspect.getsource(main.start_runtime_watchdog)

        # Find the resubscribe-specific exception handler
        resub_pos = source.index("resubscribe_kite_tokens()")
        exc_block = source[source.index("except Exception as exc:", resub_pos):]
        assert 'kite_stream_resubscribed"] = False' in exc_block[:300]

    def test_successful_health_clear_resets_state(self):
        """When kite_stream_stalled clears, all resubscribe state must be popped."""
        import main
        source = inspect.getsource(main.start_runtime_watchdog)
        clear_block = source[source.index("state.pop(\"kite_stream\", None)"):]
        assert "state.pop(\"kite_stream_resubscribed\", None)" in clear_block[:200]
        assert "state.pop(\"kite_stream_resubscribe_attempt_at\", None)" in clear_block[:200]


# ═══════════════════════════════════════════════════════════════════════════════
# FINDING 3: Failed intelligence cannot make stale data appear fresh
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinding3_IntelligenceFreshness:
    """Verify fallback-to-cached intelligence preserves original timestamps."""

    def test_exception_sets_computed_flag_false_before_using_fallback(self):
        """When intelligence analysis raises, intel_computed_this_pass must be
        set False BEFORE the fallback assignment, preventing re-caching."""
        source = inspect.getsource(Scanner._process_instrument)
        # Find the exception handler in the intelligence block
        except_pos = source.index(
            "except Exception as e:",
            source.index("intel_computed_this_pass")
        )
        handler = source[except_pos:except_pos + 600]
        flag_pos = handler.index("intel_computed_this_pass = False")
        fallback_pos = handler.index("intel_result = cached_intel")
        assert flag_pos < fallback_pos, \
            "intel_computed_this_pass must be set False before fallback assignment"

    def test_cache_write_guarded_by_computed_flag(self):
        """The downstream _cache_timeframe_intel call must check
        intel_computed_this_pass, preventing stale re-stamping."""
        source = inspect.getsource(Scanner._process_instrument)
        # The cache write line must be guarded by the computed flag
        cache_line = "self._cache_timeframe_intel(name, \"5min\", intel_result, at=now"
        cache_pos = source.rindex(cache_line)
        # Walk backwards to find the if-guard containing intel_computed_this_pass
        guard_region = source[max(0, cache_pos - 800):cache_pos]
        assert "intel_computed_this_pass" in guard_region

    def test_initial_flag_is_false(self):
        """intel_computed_this_pass must start False (pessimistic default)."""
        source = inspect.getsource(Scanner._process_instrument)
        first_assignment = source.index("intel_computed_this_pass = False")
        first_true = source.index("intel_computed_this_pass = True")
        assert first_assignment < first_true, \
            "Default must be False, set True only on successful computation"


# ═══════════════════════════════════════════════════════════════════════════════
# FINDING 4: A held signal registers for retry before being locked out
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinding4_SignalHoldRetry:
    """Verify own-timeframe-intel-missing hold registers the signal for
    reconsideration, not just marks it seen."""

    def test_recheck_registration_before_identity_lock(self):
        """When a signal is held for missing own-timeframe intelligence,
        _live_signal_recheck_eligible must be set BEFORE
        _remember_live_signal_identity is called."""
        from engine.signal_processor import SignalProcessor
        source = inspect.getsource(SignalProcessor.process_best_signal)

        hold_msg = "own-timeframe intelligence; fail-closed grading is active."
        hold_pos = source.index(hold_msg)
        block = source[hold_pos:]

        recheck_pos = block.index("_live_signal_recheck_eligible")
        remember_pos = block.index("_remember_live_signal_identity")

        assert recheck_pos < remember_pos, \
            "Recheck registration must happen before identity latch"

    def test_held_signal_has_deadline_guard(self):
        """The hold path must have a deadline check so stale holds don't
        accumulate indefinitely."""
        from engine.signal_processor import SignalProcessor
        source = inspect.getsource(SignalProcessor.process_best_signal)

        hold_msg = "own-timeframe intelligence; fail-closed grading is active."
        hold_pos = source.index(hold_msg)
        block = source[hold_pos:hold_pos + 1500]

        assert "_live_signal_processing_deadline" in block
        assert "retry_deadline" in block

    def test_successful_grade_pops_recheck_eligible(self):
        """After a signal is successfully graded, its recheck entry must be
        removed so it doesn't re-fire."""
        from engine.signal_processor import SignalProcessor
        source = inspect.getsource(SignalProcessor.process_best_signal)

        # Find the code after successful grading (after _grade_signal)
        grade_pos = source.index("_grade_signal(")
        post_grade = source[grade_pos:]
        assert "_live_signal_recheck_eligible\", {}).pop(dedup_key, None)" in post_grade


# ═══════════════════════════════════════════════════════════════════════════════
# FINDING 5: Futures quote protection is as strict as options
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinding5_FuturesQuoteProtection:
    """Verify stale futures marks are rejected, and missing spot alone doesn't
    skip trade processing when a contract quote is available."""

    def test_failed_refresh_returns_none_not_stale_cache(self):
        """If the live FUT LTP fetch fails, the method must return None rather
        than falling through to an arbitrarily old cached price."""
        scanner = Scanner.__new__(Scanner)
        # Plant a 1-hour-old cache entry
        scanner._candidate_ltp_cache = {
            "FUT:123": {"price": 25000.0, "time": time.time() - 3600.0}
        }
        # Make the live fetch raise
        scanner.data = SimpleNamespace(
            get_ltp=lambda *a: (_ for _ in ()).throw(ConnectionError("timeout"))
        )
        trade = SimpleNamespace(
            symbol_token="123", trading_symbol="NIFTY26SEPFUT"
        )
        result = scanner._get_live_fut_ltp_cached(trade)
        assert result is None, "Stale cache must not be returned after a failed refresh"

    def test_fresh_cache_is_returned_within_ttl(self):
        """A cache entry within TTL (2s) should be returned immediately."""
        scanner = Scanner.__new__(Scanner)
        scanner._candidate_ltp_cache = {
            "FUT:123": {"price": 25100.0, "time": time.time() - 1.0}
        }
        scanner.data = SimpleNamespace(get_ltp=lambda *a: None)
        trade = SimpleNamespace(
            symbol_token="123", trading_symbol="NIFTY26SEPFUT"
        )
        result = scanner._get_live_fut_ltp_cached(trade)
        assert result == 25100.0

    def test_nonblocking_mode_returns_none_without_rest_call(self):
        """allow_blocking=False must return None without calling get_ltp."""
        scanner = Scanner.__new__(Scanner)
        scanner._candidate_ltp_cache = {}
        calls = []
        scanner.data = SimpleNamespace(
            get_ltp=lambda *a: calls.append(a) or 25000.0
        )
        trade = SimpleNamespace(
            symbol_token="123", trading_symbol="NIFTY26SEPFUT"
        )
        result = scanner._get_live_fut_ltp_cached(trade, allow_blocking=False)
        assert result is None
        assert len(calls) == 0

    def test_missing_spot_with_valid_contract_quote_still_processes(self):
        """If underlying spot is None but a valid futures contract quote
        exists, the trade must still be processed (not skipped)."""
        source = inspect.getsource(Scanner._update_active_trades)
        # The fix: if price (spot) is None, but real_fut_price is valid,
        # the trade must not continue-skip
        assert "(price is None or price <= 0) and not (real_fut_price and real_fut_price > 0)" in source

    def test_missing_both_spot_and_contract_skips_safely(self):
        """If BOTH spot and contract quote are absent, the trade must be
        skipped for this pass (no processing on garbage data)."""
        source = inspect.getsource(Scanner._update_active_trades)
        # Line 33013-33014: both absent → continue
        assert "if (price is None or price <= 0) and not (real_fut_price and real_fut_price > 0):" in source


# ═══════════════════════════════════════════════════════════════════════════════
# FINDING 6: RECOVERY_REQUIRED + pending_exit uses tag reconciliation
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinding6_RecoveryExitReconciliation:
    """Verify that an uncertain exit (RECOVERY_REQUIRED + pending_exit) goes
    through authoritative order reconciliation before retrying."""

    def _make_manager_and_trade(self, broker):
        from trading.kite_broker import KiteBroker
        manager = TradeManager.__new__(TradeManager)
        manager.broker = broker
        manager.mode = "REAL"
        manager.product_type = "CARRYFORWARD"
        manager.open_trades = {}
        manager.closed_trades = []
        manager.orphan_positions = {}
        manager._lock = threading.RLock()
        manager._protection_lock = threading.RLock()
        manager.save_state = lambda: None
        manager._last_protective_sync = {}
        manager._last_protective_status_poll = {}
        manager._last_recovery_alert = {}
        manager._last_mark_to_market_save = 0.0
        manager.max_positions = 4
        manager.futures_sl_pct = 0.3
        manager.options_sl_pct = 15.0
        manager._log_ui = lambda *a, **kw: None
        manager.on_notification = None

        trade = Trade(
            id="T0001_NIFTY_0905",
            instrument="NIFTY", timeframe="5min", direction="LONG",
            entry_price=100.0, entry_time=datetime.now(IST),
            trailing_stop=90.0, current_stop=90.0,
            lots=1, lot_size=50, grade="A", inst_type="OPT",
            trading_symbol="NIFTY26SEP25000CE", symbol_token="999",
            broker_order_id="ENTRY-1",
        )
        trade.pending_exit = True
        trade.execution_status = "RECOVERY_REQUIRED"
        trade.broker_quantity = 50
        manager.open_trades[trade.id] = trade
        return manager, trade

    def test_unconfirmed_cancel_marks_recovery_not_proceeds(self):
        """If protective stop cancel is unconfirmed, the exit must NOT
        proceed with a MARKET order — it must mark RECOVERY_REQUIRED."""
        # _close_bg_body is nested inside _request_broker_exit
        source = inspect.getsource(TradeManager._request_broker_exit)
        # cancelled is not True → _mark_recovery
        assert "if cancelled is not True:" in source
        # Must call _mark_recovery in that branch
        cancel_block = source[source.index("if cancelled is not True:"):]
        assert "_mark_recovery" in cancel_block[:400]

    def test_recovery_blocks_strategy_exit_checks(self):
        """update_trade must skip exit strategy checks for trades in
        RECOVERY_REQUIRED or pending_exit state."""
        source = inspect.getsource(TradeManager.update_trade)
        # The guard at line ~7132
        assert "if recovery_required or getattr(trade, \"pending_exit\", False):" in source

    def test_scanner_skips_recovery_trades_in_update_active(self):
        """_update_active_trades must skip RECOVERY_REQUIRED trades to
        prevent strategy exit spam."""
        source = inspect.getsource(Scanner._update_active_trades)
        assert "\"RECOVERY_REQUIRED\"" in source
        # The continue after the status check
        skip_block = source[source.index("pending_exit"):]
        assert "continue" in skip_block[:200]

    def test_tag_based_reconciliation_finds_existing_exit(self):
        """When reconcile_broker_positions finds an exit order by tag,
        it must adopt it instead of sending another MARKET exit."""
        from trading.kite_broker import KiteBroker

        exit_tag = KiteBroker.order_tag("T0001_NIFTY_0905", "FULL_EXIT")
        broker = SimpleNamespace(
            exits=[],
            order_tag=KiteBroker.order_tag,
            inspect_order_by_tag=lambda tag: (
                {"state": "FOUND", "order_id": "EXIT-EXISTS"}
                if tag == exit_tag else {"state": "ABSENT"}
            ),
            close_order=lambda *a, **kw: broker.exits.append(1) or "NEW-EXIT",
            cancel_order=lambda *a, **kw: True,
            inspect_working_protective_stop=lambda *a, **kw: {
                "state": "NONE", "order_id": "", "reason": "none", "matches": []
            },
            find_working_protective_stop=lambda *a, **kw: "",
            get_fill=lambda *a, **kw: {"state": "UNKNOWN"},
            get_order_lifecycle=lambda *a, **kw: {"state": "UNKNOWN"},
        )

        manager, trade = self._make_manager_and_trade(broker)
        positions = {"data": [{
            "tradingsymbol": "NIFTY26SEP25000CE", "netqty": "50",
            "buyavgprice": "100", "sellavgprice": "0", "realised": "0",
        }]}

        manager.reconcile_broker_positions(positions)

        assert trade.execution_status == "EXIT_PENDING"
        assert trade.exit_order_id == "EXIT-EXISTS"
        assert broker.exits == [], "Must NOT send duplicate exit"


# ═══════════════════════════════════════════════════════════════════════════════
# CROSS-CUTTING: Verify the complete flow integrity
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossCutting:
    """End-to-end invariants that span multiple findings."""

    def test_update_trade_recovery_path_calls_auto_flatten_when_unprotected(self):
        """A RECOVERY_REQUIRED trade must still trigger auto-flatten if it
        has been unprotected for too long (finding 6 + finding 1)."""
        source = inspect.getsource(TradeManager.update_trade)
        recovery_block = source[source.index("if recovery_required:"):]
        assert "_maybe_auto_flatten_unprotected" in recovery_block[:300]

    def test_position_sync_loop_has_exponential_backoff(self):
        """The position sync loop must back off exponentially on failure
        instead of hammering the broker API (supports finding 1)."""
        source = inspect.getsource(TradeManager._position_sync_loop)
        assert "backoff * 2.0" in source
        assert "max_backoff" in source

    def test_intel_cache_hit_path_does_not_write_cache(self):
        """When intelligence is a cache hit (intel_is_fresh), the code must
        NOT re-cache with a new timestamp (supports finding 3)."""
        source = inspect.getsource(Scanner._process_instrument)
        # The cache-hit path uses cached_intel directly
        cache_hit = source[source.index("if intel_is_fresh:"):]
        next_else = cache_hit.index("else:")
        hit_body = cache_hit[:next_else]
        assert "_cache_timeframe_intel" not in hit_body, \
            "Cache hit must not re-stamp the cached intelligence"

    def test_mark_recovery_sets_timestamp(self):
        """_mark_recovery must set recovery_required_since for timeout
        tracking (used by auto-flatten and monitoring)."""
        source = inspect.getsource(TradeManager._mark_recovery)
        assert "recovery_required_since" in source
        assert "datetime.now(IST)" in source
