import asyncio
import copy
import math
import threading
from functools import wraps
from loguru import logger
from datetime import datetime
from typing import Dict, List, Optional
from trading.trade_manager import IST, TradeManager


def _risk_serialized(method):
    """Serialize breaker reset/latch/close decisions across scanner threads."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        lock = getattr(self, "_risk_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._risk_lock = lock
        with lock:
            return method(self, *args, **kwargs)
    return wrapped

class RiskManager:
    """
    Handles all risk-related logic:
    - Daily loss limits (Circuit Breakers)
    - Stop loss & Target management
    - Position sizing
    - Trailing SL logic
    """
    
    def __init__(self, trade_manager: TradeManager, capital_total: float):
        self.trades = trade_manager
        self.capital_total = capital_total
        
        # State
        self.daily_loss_breached = False
        self.fut_loss_breached = False
        self.opt_loss_breached = False
        self._session_date = datetime.now(IST).date()
        self._risk_lock = threading.RLock()
        
        # Risk settings
        self.max_daily_loss_pct = 3.0
        self.risk_fut_pct = 2.0
        self.risk_opt_pct = 8.0
        self.futures_sl_pct = 0.30
        self.options_sl_pct = 15.0
        # 0 = disabled (default): no absolute per-trade rupee backstop — the
        # user's stop caps + segment daily budgets govern. Scanner overrides
        # with the live setting either way.
        self.max_trade_loss_abs = 0.0
        self.circuit_breaker_slippage_bps = 10.0
        
        # Capital allocation
        self.capital_fut = capital_total * 0.5
        self.capital_opt = capital_total * 0.5

    @_risk_serialized
    def update_settings(self, **kwargs):
        """Update risk parameters dynamically"""
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        logger.info(f"🛡️ RiskManager updated: {kwargs}")

    def _projected_stop_risk(self, trade) -> Optional[float]:
        """Return the worst remaining loss at the configured/protective stop.

        A circuit breaker must not wait for mark-to-market P&L to turn red:
        an open trade can still lose its entire permitted stop distance.  The
        estimate uses the tighter of the live protective stop and the
        configured per-trade stop, then honours the absolute loss cap.
        """
        try:
            entry = abs(float(getattr(trade, "entry_price", 0.0) or 0.0))
            lots = int(getattr(trade, "lots", 0) or 0)
            lot_size = int(getattr(trade, "lot_size", 0) or 0)
            quantity = lots * lot_size
            raw_multiplier = getattr(trade, "instrument_multiplier", 1.0)
            multiplier = abs(
                float(1.0 if raw_multiplier is None else raw_multiplier)
            )
            if (
                entry <= 0
                or quantity <= 0
                or multiplier <= 0
                or not all(math.isfinite(value) for value in (entry, multiplier))
            ):
                return None

            inst_type = str(getattr(trade, "inst_type", "FUT") or "FUT").upper()
            sl_pct = self.futures_sl_pct if inst_type == "FUT" else self.options_sl_pct
            configured_risk = entry * quantity * multiplier * max(0.0, float(sl_pct or 0.0)) / 100.0

            stop = getattr(trade, "trailing_stop", None)
            if stop is None:
                stop = getattr(trade, "stop", None)
            try:
                stop = float(stop)
            except (TypeError, ValueError):
                stop = 0.0
            if not math.isfinite(stop):
                return None

            direction = str(getattr(trade, "direction", "LONG") or "LONG").upper()
            if stop > 0:
                stop_distance = max(0.0, entry - stop) if direction == "LONG" else max(0.0, stop - entry)
                stop_risk = min(configured_risk, stop_distance * quantity * multiplier)
            else:
                stop_risk = configured_risk

            cap = max(0.0, float(self.max_trade_loss_abs or 0.0))
            return min(stop_risk, cap) if cap > 0 else stop_risk
        except Exception as exc:
            logger.warning(f"Unable to estimate projected stop risk: {exc}")
            return None

    @_risk_serialized
    def check_circuit_breaker(self) -> bool:
        """
        Hard stop if daily loss exceeds limits.
        Returns True if system should HALT.
        """
        self.reset_for_session(datetime.now(IST).date())
        if self.capital_total <= 0: return False
        
        today = datetime.now(IST).date()
        fut_pnl = 0.0
        opt_pnl = 0.0
        fut_projected_pnl = 0.0
        opt_projected_pnl = 0.0
        
        # Calculate Realized + Unrealized per segment
        # Audit #8: never silently drop a trade from the daily sum — fail closed
        # by charging against its segment so the breaker still fires. When the
        # absolute per-trade cap is DISABLED (0), charge the segment's FULL
        # daily budget instead: one unparseable trade halts that segment, so
        # disabling the rupee backstop can never turn this into fail-open.
        abs_cap = max(0.0, float(self.max_trade_loss_abs or 0.0))

        def _fail_closed_charge_for(inst_type: str) -> float:
            if abs_cap > 0:
                return abs_cap
            if str(inst_type or "").upper() == "FUT":
                return max(0.0, self.capital_fut * float(self.risk_fut_pct or 0.0) / 100.0)
            return max(0.0, self.capital_opt * float(self.risk_opt_pct or 0.0) / 100.0)

        def _charge_fail_closed(inst_type: str, reason: str) -> None:
            nonlocal fut_pnl, opt_pnl, fut_projected_pnl, opt_projected_pnl
            fail_closed_charge = _fail_closed_charge_for(inst_type)
            charge = -fail_closed_charge if fail_closed_charge > 0 else 0.0
            logger.error(
                f"RiskManager PnL parse failure ({reason}); "
                f"counting {inst_type} as {charge:,.0f} (fail-closed)."
            )
            if str(inst_type or "").upper() == "FUT":
                fut_pnl += charge
                fut_projected_pnl += charge
            else:
                opt_pnl += charge
                opt_projected_pnl += charge

        # Take one immutable view for this complete risk pass.  Reconciliation
        # can move a Trade from open -> closed and replace orphan state at the
        # same time; snapshots taken in separate critical sections can count it
        # twice or miss it entirely.
        manager_lock = getattr(self.trades, "_lock", None)
        try:
            if manager_lock:
                with manager_lock:
                    closed_snapshot = [copy.copy(t) for t in self.trades.closed_trades]
                    open_snapshot = [copy.copy(t) for t in self.trades.open_trades.values()]
                    orphan_snapshot = copy.deepcopy(
                        getattr(self.trades, "orphan_positions", {}) or {}
                    )
            else:
                closed_snapshot = [copy.copy(t) for t in self.trades.closed_trades]
                open_snapshot = [copy.copy(t) for t in self.trades.open_trades.values()]
                orphan_snapshot = copy.deepcopy(
                    getattr(self.trades, "orphan_positions", {}) or {}
                )
        except Exception as exc:
            logger.critical(f"Risk snapshot failed; halting both segments: {exc}")
            closed_snapshot = []
            open_snapshot = []
            orphan_snapshot = {
                "RISK_SNAPSHOT_UNKNOWN_FUT": {"instrumenttype": "FUT"},
                "RISK_SNAPSHOT_UNKNOWN_CE": {"instrumenttype": "OPT"},
            }

        # 1. Closed Trades for today
        for t in closed_snapshot:
            try:
                et = t.entry_time.astimezone(IST) if t.entry_time.tzinfo else IST.localize(t.entry_time)
                if et.date() == today:
                    if t.inst_type == "FUT":
                        fut_pnl += t.pnl
                        fut_projected_pnl += t.pnl
                    else:
                        opt_pnl += t.pnl
                        opt_projected_pnl += t.pnl
            except Exception as exc:
                _charge_fail_closed(getattr(t, "inst_type", "OPT"), f"closed trade: {exc}")
                
        # 2. Open Trades (Unrealized)
        for t in open_snapshot:
            if getattr(t, "is_ghost", False):
                continue
            try:
                et = t.entry_time.astimezone(IST) if t.entry_time.tzinfo else IST.localize(t.entry_time)
                if et.date() == today:
                    open_pnl = float(getattr(t, 'unrealized_pnl', 0) or 0.0)
                    exit_price = float(getattr(t, "current_price", 0.0) or getattr(t, "entry_price", 0.0) or 0.0)
                    quantity = max(1, int(getattr(t, "lots", 1) or 1) * int(getattr(t, "lot_size", 1) or 1))
                    multiplier = abs(float(getattr(t, "instrument_multiplier", 1.0) or 1.0))
                    slippage_buffer = (
                        exit_price
                        * quantity
                        * multiplier
                        * max(0.0, float(self.circuit_breaker_slippage_bps or 0.0))
                        / 10000.0
                    )
                    open_pnl -= slippage_buffer
                    projected_stop_risk = self._projected_stop_risk(t)
                    if projected_stop_risk is None:
                        raise ValueError("projected stop risk is invalid")
                    projected_open_pnl = min(
                        open_pnl,
                        -(projected_stop_risk + slippage_buffer),
                    )
                    if t.inst_type == "FUT":
                        fut_pnl += open_pnl
                        fut_projected_pnl += projected_open_pnl
                    else:
                        opt_pnl += open_pnl
                        opt_projected_pnl += projected_open_pnl
            except Exception as exc:
                _charge_fail_closed(getattr(t, "inst_type", "OPT"), f"open trade: {exc}")

        # Broker-only, excess, and ambiguous positions are real exposure even
        # though they are not represented by a managed Trade.  Their valuation
        # cannot be trusted here, so consume the full relevant segment budget.
        # This prevents a reconciliation failure from becoming invisible risk.
        for symbol, position in orphan_snapshot.items():
            text = str(symbol or "").upper()
            position_type = (
                str((position or {}).get("instrumenttype") or "").upper()
                if isinstance(position, dict)
                else ""
            )
            inst_type = (
                "OPT"
                if position_type in {"OPT", "OPTCUR", "OPTIDX", "OPTSTK"}
                or text.endswith("CE")
                or text.endswith("PE")
                else "FUT"
            )
            _charge_fail_closed(inst_type, f"unreconciled broker position {symbol}")
        
        # Calculate limits in absolute terms
        fut_limit = -(self.capital_fut * (self.risk_fut_pct / 100.0))
        opt_limit = -(self.capital_opt * (self.risk_opt_pct / 100.0))
        global_limit = -(self.capital_total * (self.max_daily_loss_pct / 100.0))
        
        total_pnl = fut_pnl + opt_pnl
        projected_total_pnl = fut_projected_pnl + opt_projected_pnl
        
        # ── 🚨 GLOBAL BREACH 🚨 ──
        if global_limit < 0 and projected_total_pnl <= global_limit and not self.daily_loss_breached:
            self.daily_loss_breached = True
            msg = (
                "💀 GLOBAL DAILY LOSS LIMIT BREACHED: "
                f"actual={total_pnl:,.0f}, projected-at-stop={projected_total_pnl:,.0f}"
            )
            logger.error(msg)
            if hasattr(self.trades, '_log_ui'): self.trades._log_ui(msg, "error")
            self.halt_trading("GLOBAL_DAILY_LOSS_LIMIT")
            return True
            
        # ── 🚨 FUT BREACH 🚨 ──
        if fut_limit < 0 and fut_projected_pnl <= fut_limit and not self.fut_loss_breached:
            self.fut_loss_breached = True
            msg = (
                "🚨 FUT DAILY LOSS LIMIT BREACHED: "
                f"actual={fut_pnl:,.0f}, projected-at-stop={fut_projected_pnl:,.0f}"
            )
            logger.warning(msg)
            if hasattr(self.trades, '_log_ui'): self.trades._log_ui(msg, "warning")
            self.close_segment_trades("FUT", "FUT_DAILY_LOSS_LIMIT")
            
        # ── 🚨 OPT BREACH 🚨 ──
        if opt_limit < 0 and opt_projected_pnl <= opt_limit and not self.opt_loss_breached:
            self.opt_loss_breached = True
            msg = (
                "🚨 OPT DAILY LOSS LIMIT BREACHED: "
                f"actual={opt_pnl:,.0f}, projected-at-stop={opt_projected_pnl:,.0f}"
            )
            logger.warning(msg)
            if hasattr(self.trades, '_log_ui'): self.trades._log_ui(msg, "warning")
            self.close_segment_trades("OPT", "OPT_DAILY_LOSS_LIMIT")
            
        return self.daily_loss_breached

    def halt_trading(self, reason: str):
        """Emergency stop: Close ALL positions and disable entry"""
        logger.error(f"🛑 HALTING ALL TRADING: {reason}")
        manager_lock = getattr(self.trades, "_lock", None)
        if manager_lock:
            with manager_lock:
                snapshot = list(self.trades.open_trades.items())
        else:
            snapshot = list(self.trades.open_trades.items())
        for tid, t in snapshot:
            if str(getattr(t, "execution_status", "") or "").upper() == "RECOVERY_REQUIRED":
                self.trades.emergency_flatten_trade(t, reason)
                continue
            self.trades.close_trade(tid, t.current_price, reason)

    def close_segment_trades(self, segment: str, reason: str):
        """Close all positions in a specific segment (FUT or OPT)"""
        logger.warning(f"🚫 Closing all {segment} positions: {reason}")
        manager_lock = getattr(self.trades, "_lock", None)
        if manager_lock:
            with manager_lock:
                snapshot = list(self.trades.open_trades.items())
        else:
            snapshot = list(self.trades.open_trades.items())
        for tid, t in snapshot:
            if str(getattr(t, "execution_status", "") or "").upper() == "RECOVERY_REQUIRED":
                if t.inst_type == segment:
                    self.trades.emergency_flatten_trade(t, reason)
                continue
            if t.inst_type == segment:
                self.trades.close_trade(tid, t.current_price, reason)

    @_risk_serialized
    def can_trade(self, inst_type: str) -> bool:
        """Check if trading is allowed for a segment"""
        self.reset_for_session(datetime.now(IST).date())
        if self.daily_loss_breached: return False
        if inst_type == "FUT" and self.fut_loss_breached: return False
        if inst_type == "OPT" and self.opt_loss_breached: return False
        return True

    @_risk_serialized
    def reset_for_session(self, session_date) -> bool:
        """Reset latched daily breakers exactly once when the session date changes."""
        if self._session_date == session_date:
            return False
        self._session_date = session_date
        self.daily_loss_breached = False
        self.fut_loss_breached = False
        self.opt_loss_breached = False
        logger.info(f"Risk circuit breakers reset for session {session_date}.")
        return True
