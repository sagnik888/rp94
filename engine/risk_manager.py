import asyncio
from loguru import logger
from datetime import datetime
from typing import Dict, List, Optional
from trading.trade_manager import IST, TradeManager

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

    def update_settings(self, **kwargs):
        """Update risk parameters dynamically"""
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        logger.info(f"🛡️ RiskManager updated: {kwargs}")

    def _projected_stop_risk(self, trade) -> float:
        """Return the worst remaining loss at the configured/protective stop.

        A circuit breaker must not wait for mark-to-market P&L to turn red:
        an open trade can still lose its entire permitted stop distance.  The
        estimate uses the tighter of the live protective stop and the
        configured per-trade stop, then honours the absolute loss cap.
        """
        try:
            entry = abs(float(getattr(trade, "entry_price", 0.0) or 0.0))
            quantity = max(
                1,
                int(getattr(trade, "lots", 1) or 1)
                * int(getattr(trade, "lot_size", 1) or 1),
            )
            multiplier = abs(float(getattr(trade, "instrument_multiplier", 1.0) or 1.0))
            if entry <= 0 or multiplier <= 0:
                return 0.0

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
            return 0.0

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

        # 1. Closed Trades for today
        for t in self.trades.closed_trades:
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
        for t in self.trades.open_trades.values():
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
                    projected_open_pnl = min(
                        open_pnl,
                        -(self._projected_stop_risk(t) + slippage_buffer),
                    )
                    if t.inst_type == "FUT":
                        fut_pnl += open_pnl
                        fut_projected_pnl += projected_open_pnl
                    else:
                        opt_pnl += open_pnl
                        opt_projected_pnl += projected_open_pnl
            except Exception as exc:
                _charge_fail_closed(getattr(t, "inst_type", "OPT"), f"open trade: {exc}")
        
        # Calculate limits in absolute terms
        fut_limit = -(self.capital_fut * (self.risk_fut_pct / 100.0))
        opt_limit = -(self.capital_opt * (self.risk_opt_pct / 100.0))
        global_limit = -(self.capital_total * (self.max_daily_loss_pct / 100.0))
        
        total_pnl = fut_pnl + opt_pnl
        projected_total_pnl = fut_projected_pnl + opt_projected_pnl
        
        # ── 🚨 GLOBAL BREACH 🚨 ──
        if projected_total_pnl < global_limit and not self.daily_loss_breached:
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
        if fut_projected_pnl < fut_limit and not self.fut_loss_breached:
            self.fut_loss_breached = True
            msg = (
                "🚨 FUT DAILY LOSS LIMIT BREACHED: "
                f"actual={fut_pnl:,.0f}, projected-at-stop={fut_projected_pnl:,.0f}"
            )
            logger.warning(msg)
            if hasattr(self.trades, '_log_ui'): self.trades._log_ui(msg, "warning")
            self.close_segment_trades("FUT", "FUT_DAILY_LOSS_LIMIT")
            
        # ── 🚨 OPT BREACH 🚨 ──
        if opt_projected_pnl < opt_limit and not self.opt_loss_breached:
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
        for tid, t in list(self.trades.open_trades.items()):
            if str(getattr(t, "execution_status", "") or "").upper() == "RECOVERY_REQUIRED":
                self.trades.emergency_flatten_trade(t, reason)
                continue
            self.trades.close_trade(tid, t.current_price, reason)

    def close_segment_trades(self, segment: str, reason: str):
        """Close all positions in a specific segment (FUT or OPT)"""
        logger.warning(f"🚫 Closing all {segment} positions: {reason}")
        for tid, t in list(self.trades.open_trades.items()):
            if str(getattr(t, "execution_status", "") or "").upper() == "RECOVERY_REQUIRED":
                if t.inst_type == segment:
                    self.trades.emergency_flatten_trade(t, reason)
                continue
            if t.inst_type == segment:
                self.trades.close_trade(tid, t.current_price, reason)

    def can_trade(self, inst_type: str) -> bool:
        """Check if trading is allowed for a segment"""
        self.reset_for_session(datetime.now(IST).date())
        if self.daily_loss_breached: return False
        if inst_type == "FUT" and self.fut_loss_breached: return False
        if inst_type == "OPT" and self.opt_loss_breached: return False
        return True

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
