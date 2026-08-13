"""
Trade Manager - Position Lifecycle with Smart Exits
===============================================================

Handles:
1. Trade open/close with full audit trail
2. 1-min trailing stop updates
3. Automatic trailing stop exit detection
4. Session-end auto-exit
5. P&L tracking
"""

import uuid
import inspect
import re
import threading
import time
import json
from pathlib import Path
from datetime import date, datetime, time as dtime, timedelta
import pytz
IST = pytz.timezone('Asia/Kolkata')
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field, replace
from loguru import logger
from data.sqlite_db import db
from trading.performance_metrics import annualize_sharpe, format_sharpe_pair
from trading.trade_dashboard import build_dashboard_rows
from trading.broker import order_filled_quantity
from trading.order_retry import (
    execute_order_with_retry,
    BrokerOrderAmbiguous,
    BrokerOrderRejected,
    broker_rejection,
)


def sample_pnl_path(holder, pnl=None, entry_time=None) -> None:
    """Append a bounded (seconds_since_entry, pnl) sample to `holder.pnl_path`.

    Shared by the two objects that independently track a live position's P&L: the Trade
    in this module and the TradeCandidate the scanner monitors (which is what rtracker
    actually records). One implementation, because two would drift and the whole point
    of the path is that it can be trusted.

    Called from monitor loops that run every ~100ms, so it is written to do nothing in
    the common case. Three bounds keep it cheap and the record small:

      * TIME    — at most one sample per pnl_path_min_interval_seconds
      * MOTION  — unless P&L moved by pnl_path_min_move_rs, which keeps a fast reversal
                  legible instead of letting the timer average it away
      * SIZE    — a hard cap; first and last samples are always kept and the middle is
                  thinned by dropping every other one, so a long trade loses resolution
                  rather than losing its tail

    Median hold in the measured book is 1.2 minutes, so at the 2s default a typical
    trade stores ~36 samples and the cap is never approached.
    """
    from config.settings import get_settings

    settings = get_settings()
    if not _setting_enabled(getattr(settings, "pnl_path_logging_enabled", True), True):
        return
    try:
        if entry_time is None:
            entry_time = getattr(holder, "entry_time", None) or getattr(
                holder, "entry_timestamp", None
            )
        if entry_time is None:
            return
        if pnl is None:
            pnl = getattr(holder, "pnl", 0.0)
        pnl = float(pnl or 0.0)

        path = getattr(holder, "pnl_path", None)
        if not isinstance(path, list):
            path = []
            setattr(holder, "pnl_path", path)

        ref = entry_time if getattr(entry_time, "tzinfo", None) else IST.localize(entry_time)
        elapsed = max(0.0, (datetime.now(IST) - ref).total_seconds())

        if path:
            last_t, last_pnl = path[-1]
            min_gap = float(getattr(settings, "pnl_path_min_interval_seconds", 2.0) or 2.0)
            min_move = float(getattr(settings, "pnl_path_min_move_rs", 50.0) or 50.0)
            if (elapsed - last_t) < min_gap and abs(pnl - last_pnl) < min_move:
                return

        path.append((round(elapsed, 2), round(pnl, 2)))

        cap = int(getattr(settings, "pnl_path_max_samples", 400) or 400)
        if cap > 2 and len(path) > cap:
            # Keep both ends; halve the resolution of everything between them.
            setattr(holder, "pnl_path", [path[0]] + path[1:-1:2] + [path[-1]])
    except Exception as exc:
        # Diagnostics must never interfere with a live position.
        logger.debug(f"P&L path sample skipped: {exc}")


def _setting_enabled(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(value)


@dataclass
class Trade:
    id: str
    instrument: str
    timeframe: str
    direction: str          # LONG or SHORT
    entry_price: float
    entry_time: datetime
    trailing_stop: float    # Initial trailing stop at entry
    current_stop: float     # Current trailing stop (1-min updated)
    lots: int
    lot_size: int
    grade: str
    # ═══ Execution Details ═══
    broker_order_id: str = ""
    pending_execution: bool = False
    signal_time: Optional[datetime] = None
    entry_submitted_at: Optional[datetime] = None
    entry_replace_count: int = 0
    # How many times the remainder of a partially filled exit has been re-submitted after
    # the original exit order died. Bounded so a contract the broker keeps rejecting
    # cannot spin a fresh market order on every reconcile pass.
    partial_exit_flatten_count: int = 0
    # Deliberate scale-out state. partial_derisk_done latches only on a BOOKED success, so
    # a reduce that failed can be retried on the next tick; the other two persist an
    # in-flight reduce so a restart reconciles it from broker positions instead of
    # submitting a second one against the same lots.
    partial_derisk_done: bool = False
    partial_reduce_order_id: str = ""
    partial_reduce_requested_quantity: int = 0
    # Deliberate scale-IN state, the mirror of the three above.
    #   scale_in_count   — how many add-ons this trade has taken, against the per-trade cap.
    #   scale_in_lots    — lots added since entry, so the original size stays recoverable.
    #   frozen_initial_risk_rs — the rupee risk committed at ENTRY, held across every
    #     add-on. Without it, averaging the entry price up moves the stop distance and
    #     therefore silently redefines 1R: a trade up 1.5R reads as 0.9R the moment lots
    #     are added, and every R-based exit rule changes behaviour because of a decision
    #     the operator made rather than anything the market did.
    #   scale_in_order_id / scale_in_requested_quantity — an in-flight add, persisted so a
    #     restart reconciles it from broker positions instead of sending a second one.
    scale_in_count: int = 0
    scale_in_lots: int = 0
    frozen_initial_risk_rs: float = 0.0
    scale_in_order_id: str = ""
    scale_in_requested_quantity: int = 0
    exit_order_id: str = ""
    protective_order_id: str = ""
    protective_trigger: float = 0.0
    protective_quantity: int = 0
    # Zerodha GTT trigger id. Armed ONLY as a fallback when no exchange-side SL-M could
    # be established, never alongside a working one: an SL-M and a GTT that both fire
    # exit the position twice, and the second fill opens a naked reversed leg.
    gtt_trigger_id: str = ""
    protective_updated_at: Optional[datetime] = None
    recovery_required_since: Optional[datetime] = None
    # Set when a flatten went out while a protective stop MAY still have been resting.
    # If that stop later fires it opens a position in the opposite direction, so this
    # contract needs watching until reconciliation confirms it is flat.
    reversal_watch_required: bool = False
    execution_status: str = "OPEN"
    execution_error: str = ""
    pending_exit: bool = False
    broker_quantity: int = 0
    trading_symbol: str = ""
    symbol_token: str = ""
    # ═══ NEW: Instrument & signal details ═══
    atm_strike: float = 0.0
    option_type: str = ""   # CE or PE
    target: float = 0.0     # Target price (1.5× risk)
    rr_ratio: float = 1.5   # Risk-reward ratio
    confidence: float = 0.0 # Signal confidence 0.0-1.0
    inst_type: str = "FUT"  # FUT or OPT
    instrument_multiplier: float = 1.0 # 1.0 for FUT, Delta (~0.5) for OPT
    exec_type: str = "A"    # A for Auto, M for Manual
    status: str = "OPEN"    # OPEN / CLOSED
    current_price: float = 0.0
    entry_spot: float = 0.0  # Spot price at entry (for option tracking)
    spot_stop: float = 0.0   # Original index/spot stop for premium-led option trades
    spot_target: float = 0.0 # Original index/spot target for premium-led option trades
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""
    pnl: float = 0.0
    charges: float = 0.0
    peak_pnl: float = 0.0
    max_drawdown: float = 0.0
    # Bounded (seconds_since_entry, pnl) samples. peak_pnl and max_drawdown are the only
    # shape a closed trade has ever recorded, and from those three numbers the question
    # "would a tighter trail have kept more?" is not answerable: a rule that exits at
    # f x peak fires only if the P&L fell to that level AFTER the peak, and the ORDER of
    # the peak and the trough is exactly what a max cannot carry. On the 2026-08-11 book
    # that gap put the answer between -Rs.18,114 and +Rs.10,988 — a range wide enough to
    # be useless. The path closes it. See _sample_pnl_path for the cost bound.
    pnl_path: List[Tuple[float, float]] = field(default_factory=list)
    is_ghost: bool = False
    is_reversion_scalp: bool = False
    signal_fire_mode: str = "INTRACANDLE"
    entered_intracandle: bool = True
    # Has the post-entry repaint check completed for this trade? PERSISTED, because it
    # was previously only a dynamic setattr: a restart lost it, and an intracandle trade
    # then rehydrated as "structurally unconfirmed" for the rest of the day — which
    # disables let-winners-run and arms the blunt sub-1R give-back guard on a position
    # whose source candle closed hours earlier.
    repaint_guard_checked: bool = False
    # A GTT was armed without being able to prove no prior trigger existed, or its
    # placement came back ambiguous. Either way a duplicate server-side trigger may be
    # resting against this contract with no local id, and two triggers firing exits
    # the position twice — the second fill opening a naked reversed leg.
    gtt_arm_uncertain: bool = False
    market_regime: str = ""   # Regime at entry (for post-hoc attribution)
    data_provenance: str = "" # LIVE / HISTORICAL / RECOVERY / EOD — data source of the entry
    price_action_verdict: str = "" # FAVORABLE/NEUTRAL/ADVERSE from the price-action patch (advisory)
    intelligence_score: float = 0.0
    strategy_revision: str = ""
    strategy_direction_mode: str = ""
    market_wide_reversal_entry: bool = False
    volume_spike_entry: bool = False
    power_move: bool = False
    source_engine: str = ""
    source_engines: List[str] = field(default_factory=list)
    source_lane: str = ""
    quad_union_entry: bool = False
    raw_union_entry: bool = False
    initial_stop: float = 0.0
    scalp_lock_mode: bool = False
    # ═══ Realized slippage (REAL broker fills only) ═══
    signal_entry_price: float = 0.0  # pre-fill signal/decision price (slippage reference)
    entry_slippage: float = 0.0      # adverse ₹/unit paid vs signal at entry fill
    exit_slippage: float = 0.0       # adverse ₹/unit vs exit decision price at exit fill
    execution_bid_price: float = 0.0
    execution_ask_price: float = 0.0
    execution_touch_source: str = ""
    execution_touch_age_seconds: float = 0.0
    execution_touch_usable: bool = False
    execution_touch_reason: str = ""

    @property
    def stop_loss(self) -> float:
        """Alias used by UI/risk helpers: prefer live stop, then trail, then initial."""
        for attr in ("current_stop", "trailing_stop", "initial_stop", "spot_stop"):
            try:
                val = float(getattr(self, attr, 0.0) or 0.0)
            except Exception:
                val = 0.0
            if val > 0:
                return val
        return 0.0

    @stop_loss.setter
    def stop_loss(self, value: float) -> None:
        try:
            v = float(value or 0.0)
        except Exception:
            return
        if v <= 0:
            return
        self.current_stop = v
        if not float(getattr(self, "trailing_stop", 0.0) or 0.0):
            self.trailing_stop = v

    # (deduped 2026-07-10: a second `quantity` property later in the class
    # shadowed a clamped copy here — the ACTIVE definition lives next to
    # unrealized_pnl; behavior unchanged by removing the dead one.)

    def _never_reached_market(self) -> bool:
        """True when this trade demonstrably never became a broker position.

        Deliberately narrow: it requires BOTH a terminal not-filled status AND the
        absence of any broker order id. A trade whose acknowledgement was merely
        ambiguous is NOT covered — that one may well be live at the exchange, and
        zeroing its P&L would hide real money.
        """
        status = str(getattr(self, "execution_status", "") or "").upper()
        reason = str(getattr(self, "exit_reason", "") or "").upper()
        if status not in {"REJECTED"} and reason not in {"BROKER_ERROR", "CONTRACT_MISSING"}:
            return False
        if str(getattr(self, "broker_order_id", "") or "").strip():
            return False
        return int(getattr(self, "broker_quantity", 0) or 0) <= 0

    def to_dict(self) -> Dict:
        """Convert trade to dictionary for dashboard sync"""
        display_name = self.instrument
        if self.inst_type == "OPT" and self.atm_strike is not None and self.atm_strike > 0:
            display_name = f"{self.instrument} {int(self.atm_strike)} {self.option_type}"
            
        current_display_price = self.current_price
        if self.status == "CLOSED":
            current_display_price = (
                self.current_price
                if self.current_price is not None and self.current_price > 0 and abs(self.current_price - self.exit_price) > 1e-9
                else 0.0
            )
            
        return {
            "id": self.id,
            "status": self.status,
            "is_ghost": getattr(self, 'is_ghost', False),
            "execution_status": self.execution_status,
            "execution_error": self.execution_error,
            "pending_execution": self.pending_execution,
            "signal_time": self.signal_time.isoformat() if self.signal_time else "",
            "signal_timestamp": self.signal_time.isoformat() if self.signal_time else "",
            "entry_submitted_at": self.entry_submitted_at.isoformat() if self.entry_submitted_at else "",
            "entry_replace_count": self.entry_replace_count,
            "partial_exit_flatten_count": self.partial_exit_flatten_count,
            "partial_derisk_done": self.partial_derisk_done,
            "partial_reduce_order_id": self.partial_reduce_order_id,
            "partial_reduce_requested_quantity": self.partial_reduce_requested_quantity,
            "scale_in_count": self.scale_in_count,
            "scale_in_lots": self.scale_in_lots,
            "frozen_initial_risk_rs": self.frozen_initial_risk_rs,
            "scale_in_order_id": self.scale_in_order_id,
            "scale_in_requested_quantity": self.scale_in_requested_quantity,
            # Both of these were absent from the round trip, so a restart silently reset
            # them. reversal_watch_required is the one that matters to an operator: it
            # marks a contract whose stop MAY still be resting after a flatten, and the
            # log that sets it says "watch this contract" — without serialising it there
            # was no surface naming which contract that was.
            "reversal_watch_required": self.reversal_watch_required,
            "is_reversion_scalp": self.is_reversion_scalp,
            "pending_exit": self.pending_exit,
            "broker_order_id": self.broker_order_id,
            "exit_order_id": self.exit_order_id,
            "protective_order_id": self.protective_order_id,
            "protective_trigger": self.protective_trigger,
            "protective_quantity": self.protective_quantity,
            "gtt_trigger_id": self.gtt_trigger_id,
            "protective_updated_at": self.protective_updated_at.isoformat() if self.protective_updated_at else "",
            "recovery_required_since": self.recovery_required_since.isoformat() if self.recovery_required_since else "",
            "broker_quantity": self.broker_quantity,
            # The broker contract identity MUST survive the round-trip. to_dict() is
            # both the dashboard payload and the only serializer save_state() feeds to
            # db.save_trades(), which writes INSERT OR REPLACE — so any column missing
            # here is actively NULLed on every save, not merely left alone. Dropping
            # these two stranded every 2026-07-31 trade with trading_symbol/symbol_token
            # NULL, which is exactly what emergency_flatten_trade() and
            # _ensure_protective_stop() require: a reloaded position could then neither
            # be flattened nor given an exchange-side stop.
            "trading_symbol": self.trading_symbol,
            "symbol_token": self.symbol_token,
            "instrument": display_name,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "current_price": current_display_price,
            "exit_price": self.exit_price if self.status == "CLOSED" else 0.0,
            "trailing_stop": self.trailing_stop if self.trailing_stop is not None else 0.0,
            "current_stop": self.current_stop if self.current_stop is not None else 0.0,
            "target": self.target if self.target is not None else 0.0,
            # A trade the broker never accepted has no P&L — there was no position to
            # gain or lose on. The 2026-07-31 rows carried a phantom -80 because an
            # UNREALISED mark was written into the persisted `pnl` column while the
            # entry was still pending, and it survived onto the closed row after the
            # order turned out to have been rejected.
            "pnl": 0.0 if self._never_reached_market() else round(
                (self.unrealized_pnl if self.status == "OPEN" else self.pnl) or 0.0, 0
            ),
            "charges": self.charges,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "grade": self.grade,
            "confidence": round(self.confidence or 0.0, 2),
            "rr_ratio": self.rr_ratio,
            "atm_strike": self.atm_strike,
            "option_type": self.option_type,
            "inst_type": self.inst_type,
            "timeframe": self.timeframe,
            "entry_time": self.entry_time.strftime("%d %b %H:%M:%S") if self.entry_time else "--",
            "exit_time": self.exit_time.strftime("%d %b %H:%M:%S") if self.exit_time else "--",
            "entry_timestamp": self.entry_time.timestamp() if self.entry_time else 0,
            "exit_timestamp": self.exit_time.isoformat() if self.exit_time else "",
            "exit_reason": self.exit_reason,
            "exec_type": self.exec_type,
            "entry_spot": self.entry_spot,
            "spot_stop": self.spot_stop,
            "spot_target": self.spot_target,
            "instrument_multiplier": self.instrument_multiplier,
            "peak_pnl": self.peak_pnl,
            "max_drawdown": self.max_drawdown,
            # Persisted so a mid-session restart does not leave a trade with a path that
            # starts partway through — a truncated path reads as a trade that never
            # visited the levels it actually visited.
            "pnl_path": [[float(t), float(p)] for t, p in (self.pnl_path or [])],
            "signal_fire_mode": self.signal_fire_mode,
            "entered_intracandle": self.entered_intracandle,
            "repaint_guard_checked": self.repaint_guard_checked,
            "gtt_arm_uncertain": self.gtt_arm_uncertain,
            "market_regime": self.market_regime,
            "data_provenance": self.data_provenance,
            "price_action_verdict": self.price_action_verdict,
            "intelligence_score": self.intelligence_score,
            "strategy_revision": self.strategy_revision,
            "strategy_direction_mode": self.strategy_direction_mode,
            "market_wide_reversal_entry": self.market_wide_reversal_entry,
            "volume_spike_entry": self.volume_spike_entry,
            "power_move": self.power_move,
            "source_engine": self.source_engine,
            "source_engines": list(self.source_engines or []),
            "source_lane": self.source_lane,
            "quad_union_entry": self.quad_union_entry,
            "raw_union_entry": self.raw_union_entry,
            "initial_stop": self.initial_stop if self.initial_stop is not None else 0.0,
            "scalp_lock_mode": bool(self.scalp_lock_mode),
            "signal_entry_price": self.signal_entry_price,
            "entry_slippage": self.entry_slippage,
            "exit_slippage": self.exit_slippage,
            "execution_bid_price": self.execution_bid_price,
            "execution_ask_price": self.execution_ask_price,
            "execution_touch_source": self.execution_touch_source,
            "execution_touch_age_seconds": self.execution_touch_age_seconds,
            "execution_touch_usable": self.execution_touch_usable,
            "execution_touch_reason": self.execution_touch_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'Trade':
        """Reconstruct a Trade from a serialized dictionary with IST awareness"""
        import pytz
        ist = pytz.timezone('Asia/Kolkata')
        
        ts = data.get("entry_timestamp", 0)
        entry_time = datetime.fromtimestamp(ts, ist) if ts else datetime.now(ist)
        
        exit_time = None
        if data.get("exit_timestamp"):
            try:
                exit_time = datetime.fromisoformat(str(data["exit_timestamp"]))
                if exit_time.tzinfo is None:
                    exit_time = ist.localize(exit_time)
                else:
                    exit_time = exit_time.astimezone(ist)
            except Exception as e:
                logger.debug(f"Failed to parse canonical exit timestamp: {e}")
                exit_time = None
        if exit_time is None and data.get("exit_time") and data["exit_time"] != "--":
            try:
                # Parse display string but force IST context
                exit_year = datetime.now(ist).year
                exit_time = datetime.strptime(f"{exit_year} {data['exit_time']}", "%Y %d %b %H:%M:%S")
                exit_time = ist.localize(exit_time)
                # Guard against year rollover: Dec trade loaded in Jan
                if exit_time > datetime.now(ist):
                    exit_time = exit_time.replace(year=exit_year - 1)
            except Exception as e:

                logger.debug(f"Caught bare exception: {e}")
                exit_time = None

        entry_submitted_at = None
        if data.get("entry_submitted_at"):
            try:
                entry_submitted_at = datetime.fromisoformat(
                    str(data["entry_submitted_at"])
                )
                if entry_submitted_at.tzinfo is None:
                    entry_submitted_at = ist.localize(entry_submitted_at)
                else:
                    entry_submitted_at = entry_submitted_at.astimezone(ist)
            except (TypeError, ValueError):
                entry_submitted_at = None

        def parse_ist_datetime(value):
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value))
                return ist.localize(parsed) if parsed.tzinfo is None else parsed.astimezone(ist)
            except (TypeError, ValueError):
                return None
        
        # Extract base instrument name (strip strike info like "NIFTY 24100 CE" -> "NIFTY")
        raw_instrument = data.get("instrument", "")
        base_instrument = raw_instrument.split()[0] if raw_instrument else ""
        
        return cls(
            id=data.get("id", ""),
            instrument=base_instrument,
            timeframe=data.get("timeframe", "5min"),
            direction=data.get("direction", "LONG"),
            entry_price=data.get("entry_price", 0.0),
            entry_time=entry_time,
            trailing_stop=data.get("trailing_stop", 0.0),
            current_stop=data.get("current_stop", data.get("trailing_stop", 0.0)),
            lots=data.get("lots", 1),
            lot_size=data.get("lot_size", 1),
            grade=data.get("grade", "B"),
            atm_strike=data.get("atm_strike", 0.0),
            option_type=data.get("option_type", ""),
            target=data.get("target", 0.0),
            rr_ratio=data.get("rr_ratio", 1.5),
            confidence=data.get("confidence", 0.0),
            inst_type=data.get("inst_type", "FUT"),
            instrument_multiplier=data.get("instrument_multiplier", 1.0),
            exec_type=data.get("exec_type", "A"),
            status=data.get("status", "CLOSED"),
            broker_order_id=data.get("broker_order_id", ""),
            exit_order_id=data.get("exit_order_id", ""),
            protective_order_id=data.get("protective_order_id", ""),
            gtt_trigger_id=data.get("gtt_trigger_id", ""),
            protective_trigger=float(data.get("protective_trigger", 0.0) or 0.0),
            protective_quantity=int(data.get("protective_quantity", 0) or 0),
            protective_updated_at=parse_ist_datetime(data.get("protective_updated_at")),
            recovery_required_since=parse_ist_datetime(data.get("recovery_required_since")),
            execution_status=data.get("execution_status") or data.get("status", "CLOSED"),
            execution_error=data.get("execution_error", ""),
            pending_execution=bool(data.get("pending_execution", False)),
            signal_time=parse_ist_datetime(data.get("signal_time") or data.get("signal_timestamp")),
            entry_submitted_at=entry_submitted_at,
            entry_replace_count=int(data.get("entry_replace_count", 0) or 0),
            partial_exit_flatten_count=int(data.get("partial_exit_flatten_count", 0) or 0),
            partial_derisk_done=bool(data.get("partial_derisk_done", False)),
            partial_reduce_order_id=data.get("partial_reduce_order_id", "") or "",
            partial_reduce_requested_quantity=int(
                data.get("partial_reduce_requested_quantity", 0) or 0
            ),
            scale_in_count=int(data.get("scale_in_count", 0) or 0),
            scale_in_lots=int(data.get("scale_in_lots", 0) or 0),
            frozen_initial_risk_rs=float(data.get("frozen_initial_risk_rs", 0.0) or 0.0),
            scale_in_order_id=data.get("scale_in_order_id", "") or "",
            scale_in_requested_quantity=int(
                data.get("scale_in_requested_quantity", 0) or 0
            ),
            reversal_watch_required=bool(data.get("reversal_watch_required", False)),
            is_reversion_scalp=bool(data.get("is_reversion_scalp", False)),
            pending_exit=bool(data.get("pending_exit", False)),
            broker_quantity=int(data.get("broker_quantity", 0) or 0),
            trading_symbol=data.get("trading_symbol", ""),
            symbol_token=data.get("symbol_token", ""),
            current_price=data.get("current_price", 0.0),
            entry_spot=data.get("entry_spot", 0.0),
            spot_stop=data.get("spot_stop", 0.0),
            spot_target=data.get("spot_target", 0.0),
            exit_price=data.get("exit_price", data.get("current_price", 0.0)),
            exit_time=exit_time,
            exit_reason=data.get("exit_reason", ""),
            pnl=data.get("pnl", 0.0),
            peak_pnl=data.get("peak_pnl", 0.0),
            max_drawdown=data.get("max_drawdown", 0.0),
            is_ghost=bool(data.get("is_ghost", False)),
            pnl_path=[
                (float(row[0]), float(row[1]))
                for row in (data.get("pnl_path") or [])
                if isinstance(row, (list, tuple)) and len(row) >= 2
            ],
            signal_fire_mode=str(data.get("signal_fire_mode") or "INTRACANDLE"),
            entered_intracandle=bool(data.get("entered_intracandle", True)),
            repaint_guard_checked=bool(data.get("repaint_guard_checked", False)),
            gtt_arm_uncertain=bool(data.get("gtt_arm_uncertain", False)),
            market_regime=str(data.get("market_regime") or ""),
            data_provenance=str(data.get("data_provenance") or ""),
            price_action_verdict=str(data.get("price_action_verdict") or ""),
            intelligence_score=float(data.get("intelligence_score") or 0.0),
            strategy_revision=str(data.get("strategy_revision") or ""),
            strategy_direction_mode=str(data.get("strategy_direction_mode") or ""),
            market_wide_reversal_entry=bool(data.get("market_wide_reversal_entry", False)),
            volume_spike_entry=bool(data.get("volume_spike_entry", False)),
            power_move=bool(data.get("power_move", False)),
            source_engine=str(data.get("source_engine") or ""),
            source_engines=list(data.get("source_engines") or []),
            source_lane=str(data.get("source_lane") or ""),
            quad_union_entry=bool(data.get("quad_union_entry", False)),
            raw_union_entry=bool(data.get("raw_union_entry", False)),
            initial_stop=float(data.get("initial_stop") or data.get("trailing_stop") or 0.0),
            scalp_lock_mode=bool(data.get("scalp_lock_mode", False)),
            signal_entry_price=float(data.get("signal_entry_price") or 0.0),
            entry_slippage=float(data.get("entry_slippage") or 0.0),
            exit_slippage=float(data.get("exit_slippage") or 0.0),
            execution_bid_price=float(data.get("execution_bid_price") or 0.0),
            execution_ask_price=float(data.get("execution_ask_price") or 0.0),
            execution_touch_source=str(data.get("execution_touch_source") or ""),
            execution_touch_age_seconds=float(data.get("execution_touch_age_seconds") or 0.0),
            execution_touch_usable=bool(data.get("execution_touch_usable", False)),
            execution_touch_reason=str(data.get("execution_touch_reason") or ""),
        )

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def pnl_quantity(self) -> int:
        """The size P&L should be measured on: what the broker actually filled.

        `quantity` is the INTENDED size (lots * lot_size). On a partial fill the broker
        confirms less, and reconcile records that in broker_quantity while flagging the
        trade PARTIAL. broker_quantity is already authoritative for order sizing — exit
        quantity, protective-stop quantity — but P&L was still computed on the intended
        size, overstating it by the fill ratio. A 65-intended / 30-filled option trade
        reported -860 against a true -440.

        That number feeds the daily loss cap and the loss guard, so an overstatement
        halts trading early on money that was never at risk. Falls back to the intended
        size when nothing is confirmed yet, which is the pre-fill estimate.
        """
        try:
            confirmed = int(getattr(self, "broker_quantity", 0) or 0)
        except (TypeError, ValueError):
            confirmed = 0
        return confirmed if confirmed > 0 else self.quantity

    @property
    def unrealized_pnl(self) -> float:
        """Net P&L including estimated charges and instrument multipliers"""
        if self.status != "OPEN":
            return 0.0
        
        from config.settings import get_settings
        settings = get_settings()
        from engine.trade_accounting import estimate_trade_charges
        # Size on what the broker actually filled, not on what was intended — see
        # pnl_quantity. Charges too: brokerage and taxes scale with the filled lot.
        sized = self.pnl_quantity
        est_charges = estimate_trade_charges(
            self.entry_price,
            self.current_price,
            sized,
            self.inst_type,
            settings,
            self.instrument_multiplier,
            instrument=self.instrument,
        )

        # NOTE: self.current_price is ALREADY adjusted for Delta in update_trade()
        # For Options, we are always LONG (buying CE or PE).
        if self.inst_type == "OPT":
            gross = (self.current_price - self.entry_price) * sized
        else:
            # For Futures, it depends on direction
            if self.direction == "LONG":
                gross = (self.current_price - self.entry_price) * sized
            else:
                gross = (self.entry_price - self.current_price) * sized
            
        return gross - est_charges


class TradeManager:
    """Full trade lifecycle management with persistence"""

    def _log_ui(self, message: str, level: str = "info"):
        if self.ui_logger:
            try:
                self.ui_logger(message, level)
            except Exception as e:
                logger.error(f"Failed to dispatch UI log: {e}")

    def __init__(
        self,
        broker=None,
        max_positions: int = 3,
        session_end: str = "15:25",
        product_type: str = "CARRYFORWARD",
        on_notification=None,
    ):
        self.broker = broker
        self.ui_logger = None
        # Injected desktop notifier (main.py passes send_desktop_notification). Falls
        # back to importing it directly, so behaviour is unchanged when not supplied.
        self.on_notification = on_notification
        self.max_positions = max_positions
        self.session_end = self._parse_time(session_end)
        self.product_type = str(product_type or "CARRYFORWARD").upper()
        self.open_trades: Dict[str, Trade] = {}
        self.closed_trades: List[Trade] = []
        self._trade_counter = 0
        self._lock = threading.RLock()
        # Serialises exchange-side protective-stop placement so two callers
        # (PositionSync reconcile + scanner tick) can never place a duplicate SL-M
        # for the same fresh fill. Dedicated lock (not self._lock) keeps the ~1s
        # broker round-trip from blocking unrelated trade-manager operations; its
        # callers never hold self._lock, so there is no lock-ordering inversion.
        self._protection_lock = threading.RLock()
        self._state_file = "data_store/trade_state.json"
        self._last_mark_to_market_save = 0.0
        self._last_protective_sync = {}
        self._last_protective_status_poll = {}
        self._last_recovery_alert = {}
        self.orphan_positions: Dict[str, Dict] = {}
        
        # Risk settings (updated via Scanner.configure)
        self.futures_sl_pct = 0.30
        self.options_sl_pct = 15.0
        
        # Load state on init
        self.load_state()
        
        # Start background broker position sync
        self._sync_active = True
        self._sync_thread = threading.Thread(target=self._position_sync_loop, daemon=True, name="PositionSync")
        self._sync_thread.start()

    def _position_sync_loop(self):
        """Background loop to sync internal open_trades with broker positions.

        Adaptive cadence so a rate-limited/slow broker API isn't hammered:
          * base interval while positions are OPEN (need close-reconciliation),
          * slower idle interval when FLAT (nothing to reconcile),
          * exponential backoff on a FAILED reconcile (rate-limit/timeout),
            resetting to base on the next success.
        Previously a flat every-5s get_positions() that spammed the broker even
        when flat and even while being rate-limited.
        """
        import time
        from config.settings import get_settings
        backoff = 0.0
        while getattr(self, "_sync_active", True):
            interval = 5.0
            try:
                s = get_settings()
                base = max(1.0, float(getattr(s, "position_sync_interval_seconds", 5.0) or 5.0))
                idle = max(base, float(getattr(s, "position_sync_idle_interval_seconds", 30.0) or 30.0))
                max_backoff = max(base, float(getattr(s, "position_sync_max_backoff_seconds", 60.0) or 60.0))
                result = self.reconcile_broker_positions()
                ok = isinstance(result, dict) and bool(result.get("checked", False))
                has_open = bool(getattr(self, "open_trades", {}))
                if ok:
                    backoff = 0.0
                    interval = base if has_open else idle
                else:
                    # Reconcile could not complete (rate-limit / timeout / no broker):
                    # back off instead of retrying into the same wall every 5s.
                    backoff = min(base if backoff <= 0 else backoff * 2.0, max_backoff)
                    interval = max(backoff, base if has_open else idle)
            except Exception as exc:
                logger.error(f"Error in position sync loop: {exc}")
                backoff = min(5.0 if backoff <= 0 else backoff * 2.0, 60.0)
                interval = backoff
            time.sleep(interval)

    @staticmethod
    def _position_quantity(position: Dict) -> int:
        try:
            return int(float(position.get("netqty", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _position_exit_price(trade: Trade, position: Dict) -> float:
        value = (
            position.get("sellavgprice", 0.0)
            if trade.direction in {"LONG", "BUY"} or trade.inst_type == "OPT"
            else position.get("buyavgprice", 0.0)
        )
        try:
            price = float(value or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        return price if price > 0 else (trade.current_price or trade.entry_price)

    @staticmethod
    def _confirmed_broker_quantity(trade: Trade) -> int:
        try:
            return max(0, int(getattr(trade, "broker_quantity", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _allocate_broker_quantity(trades_on_symbol, net_qty) -> Dict[str, int]:
        """Split ONE broker position row across the local trades that share the contract.

        Kite reports POSITIONS per tradingsymbol, not per order, so three trades on the
        same strike read one row. reconcile_broker_positions assigned that row's net
        quantity to every one of them:

            trade.broker_quantity = abs(net_qty)

        Three 75-lot trades against a broker net of 225 each became broker_quantity=225.
        That field is authoritative for order SIZING -- ensure_broker_protection, the
        flatten path and _request_broker_exit all size from it -- so each trade then
        armed a 225 stop against the 75 it actually held. On options an oversized SL-M
        sells more than the account owns: a naked written option, which this file
        already documents as the outcome that must never happen.

        Allocation rules, in order:
          1. One trade on the contract -> the row is unambiguously its own. Return the
             net verbatim, so every existing single-trade path (including an excess that
             callers detect by comparing against trade.quantity) is unchanged.
          2. A trade whose own ENTRY FILL was already confirmed keeps that quantity.
             Per-order fills come from the order itself and are real evidence; a
             positions-derived split is a guess and must not overwrite them.
          3. Whatever is left is handed out oldest-trade-first, each capped at the
             quantity that trade actually intended.

        Under-allocation is the safe direction: a stop sized short of the true position
        leaves part of it unprotected, while an oversized one sells stock the account
        does not hold. Positions alone genuinely cannot say which trade owns which lot
        when fills are ambiguous -- only the per-order fill path can, which is why an
        already-confirmed fill wins over this split.
        """
        try:
            total = abs(int(net_qty or 0))
        except (TypeError, ValueError):
            total = 0

        if len(trades_on_symbol) <= 1:
            return {str(tid): total for tid, _ in trades_on_symbol}

        def _order_key(item):
            tid, trade = item
            return (str(getattr(trade, "entry_time", "") or ""), str(tid))

        ordered = sorted(trades_on_symbol, key=_order_key)
        allocation: Dict[str, int] = {}
        remaining = total

        for tid, trade in ordered:
            own = TradeManager._confirmed_broker_quantity(trade)
            if own > 0:
                take = min(remaining, own)
                allocation[str(tid)] = take
                remaining -= take

        for tid, trade in ordered:
            if str(tid) in allocation:
                continue
            try:
                want = max(0, int(getattr(trade, "quantity", 0) or 0))
            except (TypeError, ValueError):
                want = 0
            take = min(remaining, want)
            allocation[str(tid)] = take
            remaining -= take

        return allocation

    @staticmethod
    def _entry_still_confirming(trade: Trade) -> bool:
        status = str(getattr(trade, "execution_status", "") or "").upper()
        return bool(getattr(trade, "pending_execution", False)) or status in {
            "ENTRY_PENDING",
            "ENTRY_SUBMITTED",
            "PARTIAL",
        }

    @staticmethod
    def _reconciliation_symbol_prefixes() -> Tuple[str, ...]:
        fallback = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
        prefixes = set(fallback)
        try:
            payload = json.loads(Path("config/instruments.json").read_text(encoding="utf-8"))
            indices = payload.get("indices", {}) if isinstance(payload, dict) else {}
            if isinstance(indices, dict):
                for name, cfg in indices.items():
                    if name:
                        prefixes.add(str(name).upper())
                    if isinstance(cfg, dict):
                        for key in ("symbol", "option_symbol_prefix"):
                            value = str(cfg.get(key) or "").strip().upper()
                            if value:
                                prefixes.add(value)
        except Exception as exc:
            logger.debug(f"Using fallback reconciliation prefixes: {exc}")
        return tuple(sorted(prefixes, key=len, reverse=True))

    def _maybe_auto_flatten_unprotected(self, trade: Trade, reason: str) -> bool:
        if getattr(self, "mode", "HISTORICAL") != "REAL" or getattr(trade, "is_ghost", False):
            return False
        if self._confirmed_broker_quantity(trade) <= 0:
            return False
        if getattr(trade, "protective_order_id", ""):
            return False
        if str(getattr(trade, "execution_status", "") or "").upper() != "RECOVERY_REQUIRED":
            return False

        from config.settings import get_settings

        recovery_since = self._aware_ist(trade.recovery_required_since)
        grace = max(
            0.0,
            float(getattr(get_settings(), "recovery_auto_flatten_seconds", 5.0) or 0.0),
        )
        if (datetime.now(IST) - recovery_since).total_seconds() < grace:
            return False
        return self.emergency_flatten_trade(
            trade,
            f"{reason} for at least {grace:.0f}s",
        )

    def _finalize_closed_trade(
        self,
        trade_id: str,
        trade: Trade,
        exit_price: float,
        reason: str,
        exit_time: Optional[datetime] = None,
        realised_pnl: Optional[float] = None,
    ) -> bool:
        from config.settings import get_settings
        from engine.trade_accounting import estimate_trade_charges

        with self._lock:
            if trade_id not in self.open_trades:
                return False
            # The decision price was stamped into exit_price by close_trade before the
            # broker exit was requested; the incoming exit_price is the real fill.
            exit_decision_price = float(trade.exit_price or 0.0)
            trade.status = "CLOSED"
            trade.execution_status = "CLOSED"
            trade.pending_execution = False
            trade.pending_exit = False
            trade.execution_error = ""
            trade.exit_price = float(exit_price or trade.current_price or trade.entry_price)
            trade.current_price = trade.exit_price
            trade.exit_time = exit_time or datetime.now(IST)
            trade.exit_reason = reason
            if getattr(trade, "exit_order_id", ""):
                self._record_exit_slippage(trade, exit_decision_price, trade.exit_price)
            # Realised P&L on the size the broker actually filled, not the intended
            # size. A partial fill otherwise books a loss (or gain) proportionally
            # larger than the position ever was, and that figure is what the daily
            # loss cap and the session summary are built from.
            sized = trade.pnl_quantity
            if trade.inst_type == "OPT":
                gross = (trade.exit_price - trade.entry_price) * sized
            elif trade.direction == "LONG":
                gross = (trade.exit_price - trade.entry_price) * sized
            else:
                gross = (trade.entry_price - trade.exit_price) * sized
            trade.charges = estimate_trade_charges(
                trade.entry_price,
                trade.exit_price,
                sized,
                trade.inst_type,
                get_settings(),
                trade.instrument_multiplier,
                instrument=trade.instrument,
            )
            trade.pnl = float(realised_pnl) if realised_pnl is not None else gross - trade.charges
            self.closed_trades.append(trade)
            del self.open_trades[trade_id]
        self.save_state()
        
        profit_str = f"+₹{trade.pnl:.2f}" if trade.pnl >= 0 else f"-₹{abs(trade.pnl):.2f}"
        msg = f"✅ Trade Closed: {trade.instrument} @ {trade.exit_price:.2f} (Reason: {reason}) | PnL: {profit_str}"
        logger.info(msg)
        self._log_ui(msg, "trade")
        
        return True

    @staticmethod
    def _expected_position_sign(trade: Trade) -> int:
        if trade.inst_type == "OPT":
            return 1
        return 1 if trade.direction in {"LONG", "BUY"} else -1

    @staticmethod
    def _slippage_sample_is_a_real_fill(trade: Trade, leg: str) -> bool:
        """Whether this leg may contribute to the realized-slippage EWMA.

        SlippageTracker.record documents itself as taking "one real fill", and
        trade_accounting lets the measured EWMA REPLACE the static per-leg assumption
        once slippage_calibration_min_samples is reached. So anything that is not a
        broker fill does not merely add noise — it overrides the model with itself.

        2026-08-11: slippage_stats.json held 12,051 FUT samples at ewma_frac exactly
        0.0, every row stamped last_leg "ENTRY". The exit leg has always required an
        exit_order_id and skipped a fill equal to its reference; the entry leg required
        neither, so simulated entries whose "fill" was the signal price recorded a
        perfect zero, 12,051 times. That drove modelled futures slippage to the floor of
        _FUT_CALIBRATION_BOUNDS (0.2bp) — a backtest grading its own execution.

        Two necessary conditions for a real fill, and both are plain dataclass fields:
        the trade is not a ghost, and the leg has a broker order id.
        """
        if bool(getattr(trade, "is_ghost", False)):
            return False
        order_id = (
            getattr(trade, "exit_order_id", "")
            if str(leg or "").upper() == "EXIT"
            else getattr(trade, "broker_order_id", "")
        )
        return bool(str(order_id or "").strip())

    @staticmethod
    def _record_entry_slippage(trade: Trade, fill_price: float) -> None:
        """Measure the real entry fill against the signal price that triggered it.

        Adverse is signed positive when the fill was worse than the signal:
        entries are BUYs for all options and LONG futures; SHORT futures enter
        by SELLING, so worse = lower fill.
        """
        try:
            reference = float(getattr(trade, "signal_entry_price", 0.0) or 0.0)
            fill = float(fill_price or 0.0)
            if reference <= 0 or fill <= 0:
                return
            # entry_slippage itself is still stamped below for any trade — it is
            # reporting. Only the shared EWMA, which overrides the cost model, is
            # restricted to real broker fills.
            record_to_ewma = TradeManager._slippage_sample_is_a_real_fill(trade, "ENTRY")
            if trade.inst_type == "OPT" or trade.direction in {"LONG", "BUY"}:
                adverse = fill - reference
            else:
                adverse = reference - fill
            trade.entry_slippage = round(adverse, 4)
            if not record_to_ewma:
                return
            from engine.slippage_tracker import get_slippage_tracker

            get_slippage_tracker().record(
                inst_type=trade.inst_type,
                instrument=trade.instrument,
                reference_price=reference,
                adverse_points=adverse,
                leg="ENTRY",
            )
        except Exception as exc:
            logger.debug(f"Entry slippage recording skipped: {exc}")

    @staticmethod
    def _record_exit_slippage(trade: Trade, decision_price: float, fill_price: float) -> None:
        """Measure the real exit fill against the price at the exit decision.

        Exits reverse the entry side: options and LONG futures exit by SELLING
        (worse = lower fill); SHORT futures exit by BUYING (worse = higher fill).
        """
        try:
            reference = float(decision_price or 0.0)
            fill = float(fill_price or 0.0)
            if reference <= 0 or fill <= 0 or abs(fill - reference) < 1e-9:
                return
            if trade.inst_type == "OPT" or trade.direction in {"LONG", "BUY"}:
                adverse = reference - fill
            else:
                adverse = fill - reference
            trade.exit_slippage = round(adverse, 4)
            if not TradeManager._slippage_sample_is_a_real_fill(trade, "EXIT"):
                return
            from engine.slippage_tracker import get_slippage_tracker

            get_slippage_tracker().record(
                inst_type=trade.inst_type,
                instrument=trade.instrument,
                reference_price=reference,
                adverse_points=adverse,
                leg="EXIT",
            )
        except Exception as exc:
            logger.debug(f"Exit slippage recording skipped: {exc}")

    @staticmethod
    def _order_status_text(order: Optional[Dict]) -> str:
        if not isinstance(order, dict):
            return ""
        return str(
            order.get("orderstatus")
            or order.get("status")
            or order.get("orderStatus")
            or ""
        ).strip().lower().replace("_", " ")

    @staticmethod
    def _aware_ist(value: Optional[datetime]) -> datetime:
        value = value or datetime.now(IST)
        return IST.localize(value) if value.tzinfo is None else value.astimezone(IST)

    def _mark_recovery(self, trade: Trade, reason: str) -> None:
        trade.execution_status = "RECOVERY_REQUIRED"
        trade.execution_error = str(reason or "Broker reconciliation required")
        if trade.recovery_required_since is None:
            trade.recovery_required_since = datetime.now(IST)
        self._alert_recovery(trade)

    def _alert_recovery(self, trade: Trade) -> None:
        if not hasattr(self, "_last_recovery_alert"):
            self._last_recovery_alert = {}
        now = time.monotonic()
        last = float(self._last_recovery_alert.get(trade.id, 0.0) or 0.0)
        if now - last < 30.0:
            return
        self._last_recovery_alert[trade.id] = now
        message = (
            f"RECOVERY REQUIRED: {trade.trading_symbol or trade.instrument}. "
            f"{trade.execution_error or 'Broker state is ambiguous.'}"
        )
        logger.critical(message)
        self._log_ui(message, "error")
        try:
            notifier = getattr(self, "on_notification", None)
            if not callable(notifier):
                from notifications import send_desktop_notification as notifier
            notifier(message, "error")
        except Exception as exc:
            logger.debug(f"Recovery desktop alert unavailable: {exc}")

    # Broker conditions that refuse EVERY order until a human changes something
    # outside this process. Matched against the broker's own refusal text.
    _BLOCKING_BROKER_CONDITIONS = (
        (
            "registered ip",
            "The broker is refusing orders from this machine's public IP ({detail}). "
            "Add that address on the Kite developer console (developers.kite.trade -> "
            "your app) and restart. Check the address family: a whitelisted IPv4 does "
            "NOT cover an IPv6 egress. An EMPTY allowed-IP list denies every order "
            "rather than allowing all. Until then NO order can reach the exchange.",
        ),
        (
            "invalid token",
            "The Kite session token is invalid. Re-login through the dashboard and "
            "restart; no order can reach the exchange until the session is renewed.",
        ),
        (
            "invalid session",
            "The Kite session has expired. Re-login through the dashboard and restart; "
            "no order can reach the exchange until the session is renewed.",
        ),
        (
            "session expired",
            "The Kite session has expired. Re-login through the dashboard and restart; "
            "no order can reach the exchange until the session is renewed.",
        ),
    )

    def _alert_blocking_broker_condition(self, error: str) -> None:
        """Escalate a refusal that no retry and no later signal can get past.

        A generic "entry failed" line scrolls past in a busy log. On 2026-07-31 the
        account refused every order from 14:51 onward with AG7002 and the session ran
        to the close still trying, because nothing separated "this order failed" from
        "nothing can succeed until you change something".
        """
        text = str(error or "").lower()
        for marker, template in self._BLOCKING_BROKER_CONDITIONS:
            if marker not in text:
                continue
            if not hasattr(self, "_last_blocking_alert"):
                self._last_blocking_alert = {}
            now = time.monotonic()
            if now - float(self._last_blocking_alert.get(marker, 0.0) or 0.0) < 600.0:
                return
            self._last_blocking_alert[marker] = now
            # The broker names the offending IP in its own message; quote it back
            # rather than guessing, because the SDK's X-ClientPublicIP header is a
            # hardcoded constant and never reflects this machine.
            found = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", str(error or ""))
            message = "TRADING BLOCKED: " + template.format(detail=found.group(0) if found else "unknown")
            logger.critical(message)
            self._log_ui(message, "error")
            try:
                notifier = getattr(self, "on_notification", None)
                if not callable(notifier):
                    from notifications import send_desktop_notification as notifier
                notifier(message, "error")
            except Exception as exc:
                logger.debug(f"Blocking-condition desktop alert unavailable: {exc}")
            return

    def _protective_exit_side(self, trade: Trade) -> str:
        return "SELL" if trade.inst_type == "OPT" or trade.direction in {"LONG", "BUY"} else "BUY"

    def _protective_trigger_for(self, trade: Trade) -> float:
        stop = float(getattr(trade, "current_stop", 0.0) or getattr(trade, "trailing_stop", 0.0) or 0.0)
        if stop > 0:
            return stop
        if trade.inst_type == "OPT":
            pct = self.options_sl_pct
        else:
            from engine.market_regime import futures_stop_tolerance_pct

            pct = futures_stop_tolerance_pct(
                self.futures_sl_pct,
                getattr(trade, "market_regime", "UNKNOWN"),
            )
        if trade.inst_type == "OPT" or trade.direction in {"LONG", "BUY"}:
            return max(0.05, trade.entry_price * (1.0 - pct / 100.0))
        return trade.entry_price * (1.0 + pct / 100.0)

    def on_broker_postback(self, payload: Dict[str, Any]) -> bool:
        """Apply a verified Zerodha order postback to the matching trade.

        Push beats polling here: the postback is the broker telling us what happened,
        so a REJECTED entry is known immediately with its reason instead of being
        inferred from an order-book scan on the next cycle.

        Deliberately conservative — it records state and surfaces the reason, and does
        NOT close or open positions. Reconciliation against actual broker positions
        stays the single authority for that, so a duplicated or out-of-order postback
        can never invent a fill.
        """
        order_id = str((payload or {}).get("order_id") or "")
        status = str((payload or {}).get("status") or "").upper()
        if not order_id:
            return False
        with self._lock:
            trade = next(
                (t for t in self.open_trades.values()
                 if str(getattr(t, "broker_order_id", "")) == order_id),
                None,
            )
        if trade is None:
            return False

        message = str(payload.get("status_message") or "").strip()
        if status == "REJECTED":
            trade.execution_error = message or "Broker rejected the order"
            logger.error(f"Postback: {trade.trading_symbol} entry REJECTED — {trade.execution_error}")
            self._alert_blocking_broker_condition(trade.execution_error)
        elif status == "COMPLETE":
            try:
                filled = int(float(payload.get("filled_quantity") or 0))
                average = float(payload.get("average_price") or 0.0)
            except (TypeError, ValueError):
                filled, average = 0, 0.0
            if filled > 0:
                trade.broker_quantity = filled
            if average > 0:
                trade.execution_error = ""
            logger.info(
                f"Postback: {trade.trading_symbol} COMPLETE filled={filled} avg={average}"
            )
        elif status == "CANCELLED":
            trade.execution_error = message or "Broker cancelled the order"
        self.save_state()
        return True

    def _withdraw_stale_entry_remainder(self, trade: Trade, filled_qty: int, expected_qty: int) -> None:
        """Cancel the unfilled tail of a partially filled ENTRY once it has gone stale.

        The 12s cancel/replace timer only runs while `position is None` and
        execution_status == 'ENTRY_SUBMITTED'. The instant a partial fill lands, a
        position row exists and the status becomes 'PARTIAL', so BOTH preconditions are
        permanently false and nothing ages the remainder again. The PARTIAL branch
        re-reads the fill and re-arms protection every pass, but never touches the order
        still resting at the exchange.

        So a 3-lot entry that fills 1 leaves 2 working all session. Hours later a spike
        sweeps them: the position becomes 3 lots while the protective stop covers 1, and
        until the next reconcile tick two lots are naked. Meanwhile P&L, risk headroom
        and the concentration guards were all sized on 1 lot for the whole interval.

        The cancel is PROVEN, not acknowledged — same rule as the stale-entry replace.
        cancel_order_confirmed answers True only for a broker-confirmed terminal
        non-fill, False when the order actually filled, and None when it cannot tell.
        A False or None must NOT be read as "the remainder is gone": the next reconcile
        pass re-reads the position and arms protection for whatever really filled.
        """
        order_id = str(getattr(trade, "broker_order_id", "") or "")
        if not order_id or filled_qty >= expected_qty:
            return
        if getattr(trade, "entry_remainder_withdrawn", False):
            return
        from config.settings import get_settings

        settings = get_settings()
        if not _setting_enabled(
            getattr(settings, "partial_entry_remainder_cancel_enabled", True), True
        ):
            return
        timeout = max(
            5.0,
            float(getattr(settings, "partial_entry_remainder_timeout_seconds", 120.0) or 120.0),
        )
        submitted = getattr(trade, "entry_submitted_at", None)
        if submitted is None:
            return
        try:
            if isinstance(submitted, str):
                submitted = datetime.fromisoformat(submitted)
            if submitted.tzinfo is None:
                submitted = IST.localize(submitted)
            age = (datetime.now(IST) - submitted.astimezone(IST)).total_seconds()
        except Exception:
            return
        if age < timeout:
            return

        confirm = getattr(self.broker, "cancel_order_confirmed", None)
        if not callable(confirm):
            return
        try:
            cancelled = confirm(order_id, "NORMAL")
        except Exception as exc:
            logger.error(
                f"Could not withdraw the unfilled remainder of {trade.id} "
                f"({trade.trading_symbol}): {exc}"
            )
            return

        if cancelled is True:
            trade.entry_remainder_withdrawn = True
            trade.pending_execution = False
            trade.execution_error = (
                f"Entry filled {filled_qty} of {expected_qty}; unfilled remainder "
                "withdrawn after it went stale"
            )
            logger.warning(
                f"[PARTIAL ENTRY] {trade.id} {trade.trading_symbol}: filled "
                f"{filled_qty}/{expected_qty} and the remainder rested {age:.0f}s. "
                "Cancel proven — position stands at the filled quantity."
            )
            self._log_ui(
                f"Withdrew the unfilled {expected_qty - filled_qty} of "
                f"{trade.trading_symbol}; the trade stands at {filled_qty}.",
                "warning",
            )
        elif cancelled is False:
            # It filled while we were cancelling. Say so and let the next reconcile
            # pass size protection on the real quantity.
            logger.warning(
                f"[PARTIAL ENTRY] {trade.id} {trade.trading_symbol}: the remainder "
                "filled during the cancel; protection will be re-armed on the full size."
            )
        else:
            logger.error(
                f"[PARTIAL ENTRY] {trade.id} {trade.trading_symbol}: remainder state "
                "UNKNOWN after a cancel attempt; leaving it for the next pass rather "
                "than assuming it is gone."
            )

    def _arm_gtt_fallback(self, trade: Trade, qty: int, side: str, trigger: float) -> bool:
        """Arm a server-side GTT stop. Only valid when NO SL-M is working.

        A GTT and an SL-M that both fire exit the position twice, and the second fill
        opens a naked reversed leg — so this must never run alongside a live stop.
        """
        placer = getattr(self.broker, "place_gtt_stop", None)
        if not callable(placer):
            return False
        if str(getattr(trade, "gtt_trigger_id", "") or ""):
            return True                      # already armed; never stack triggers
        # Nor stack one we armed but never recorded — a lost acknowledgement on a
        # previous cycle leaves an active trigger with no local id.
        finder = getattr(self.broker, "find_gtt_stop", None)
        scan_state = ""
        if callable(finder):
            def _scan() -> dict:
                try:
                    return finder(trade.trading_symbol) or {}
                except Exception as exc:
                    # A raising finder is the same evidence as UNKNOWN: we cannot
                    # tell. Collapsing it to {} made it indistinguishable from NONE.
                    return {"state": "UNKNOWN", "trigger_id": "", "reason": str(exc)}

            prior = _scan()
            if str(prior.get("state") or "") == "UNKNOWN":
                # "Cannot tell" was falling straight through to placer() — the same
                # never-conclude-from-a-failure rule the SL-M path already follows,
                # broken here. One retry usually resolves a transient scan failure.
                prior = _scan()
            scan_state = str(prior.get("state") or "")
            prior_id = str(prior.get("trigger_id") or "")
            if scan_state == "FOUND" and prior_id:
                # find_gtt_stop matches the CONTRACT, not the trade — no side, quantity
                # or trigger level. Two trades on one strike is supported, so adopting
                # blindly points both at one trigger: this trade would sit behind the
                # OTHER trade's stop level, and whichever exits first cancels it and
                # silently strips the survivor's server-side protection.
                owner = self._trade_owning_gtt(prior_id, exclude_id=trade.id)
                if owner is not None:
                    logger.error(
                        f"GTT {prior_id} on {trade.trading_symbol} belongs to trade "
                        f"{owner.id}; not adopting it for {trade.id}."
                    )
                    return False
                if not self._gtt_matches_trade(prior, trigger):
                    logger.error(
                        f"Active GTT {prior_id} on {trade.trading_symbol} does not match "
                        f"this trade's trigger {trigger:.2f}; not adopting it."
                    )
                    return False
                trade.gtt_trigger_id = prior_id
                logger.warning(
                    f"Adopted existing GTT {prior_id} for {trade.trading_symbol} "
                    "instead of arming a second one."
                )
                return True
        # Still UNKNOWN after the retry. Arm anyway — this runs only because the SL-M
        # was refused, so declining would leave the position with NO server-side stop,
        # which is worse than a possible duplicate trigger. But flag it, so a duplicate
        # is reconciled by a human rather than sitting silent until both fire.
        if scan_state == "UNKNOWN":
            logger.error(
                f"GTT scan for {trade.trading_symbol} is UNKNOWN after a retry; arming "
                "anyway because the position is otherwise unprotected. A duplicate "
                "trigger may exist."
            )
            trade.gtt_arm_uncertain = True

        try:
            trigger_id = placer(
                trade.trading_symbol, qty, side, trigger,
                last_price=float(getattr(trade, "current_price", 0.0) or trade.entry_price),
                product_type=self.product_type,
            )
        except BrokerOrderAmbiguous as exc:
            # The trigger may or may not exist server-side and we have no id for it.
            # This is exactly what creates the orphan the scan above cannot see, so
            # record it rather than returning a bare False.
            trade.gtt_arm_uncertain = True
            self._mark_recovery(
                trade,
                f"GTT arm ambiguous for {trade.trading_symbol}: {exc}. A server-side "
                "trigger may exist with no local id.",
            )
            logger.error(f"GTT fallback ambiguous for {trade.trading_symbol}: {exc}")
            return False
        except Exception as exc:
            logger.error(f"GTT fallback failed for {trade.trading_symbol}: {exc}")
            return False
        if not trigger_id:
            return False
        trade.gtt_trigger_id = str(trigger_id)
        logger.critical(
            f"GTT fallback armed for {trade.trading_symbol} at {trigger:.2f} "
            f"(id {trigger_id}) — the position is protected server-side even if this "
            "process stops. Reconcile the missing SL-M."
        )
        return True

    def _trade_owning_gtt(self, trigger_id: str, exclude_id: str = ""):
        """The open trade already holding this GTT trigger id, if any."""
        if not trigger_id:
            return None
        with self._lock:
            for other in list(getattr(self, "open_trades", {}).values()):
                if other.id == exclude_id:
                    continue
                if str(getattr(other, "gtt_trigger_id", "") or "") == str(trigger_id):
                    return other
        return None

    @staticmethod
    def _gtt_matches_trade(row: Dict, trigger: float) -> bool:
        """Only adopt a trigger armed at this trade's own stop level.

        A GTT at someone else's level is not protection for this position — it fires
        early or late by exactly the difference between the two stops.
        """
        values = (row or {}).get("trigger_values") or []
        if not values:
            return False
        try:
            return any(abs(float(v) - float(trigger)) <= 0.051 for v in values)
        except (TypeError, ValueError):
            return False

    def _release_gtt_fallback(self, trade: Trade, note: str) -> bool:
        """Cancel the GTT once real stop management takes over, or before flattening.

        Returns False when the broker refused, so callers can withhold an exit rather
        than market out on top of a trigger that is still armed.
        """
        trigger_id = str(getattr(trade, "gtt_trigger_id", "") or "")
        if not trigger_id:
            return True
        canceller = getattr(self.broker, "cancel_gtt_stop", None)
        if not callable(canceller):
            return True
        try:
            cancelled = bool(canceller(trigger_id))
        except Exception as exc:
            logger.error(f"GTT cancellation raised for {trade.trading_symbol}: {exc}")
            return False
        if not cancelled:
            logger.error(
                f"GTT {trigger_id} for {trade.trading_symbol} could NOT be cancelled; "
                "withholding actions that could double-exit the position."
            )
            return False
        trade.gtt_trigger_id = ""
        logger.info(f"GTT {trigger_id} released for {trade.trading_symbol}: {note}")
        return True

    def ensure_broker_protection(self, trade: Trade, quantity: Optional[int] = None) -> bool:
        """Create or tighten the exchange-side SL-M for a confirmed REAL position."""
        if getattr(self, "mode", "HISTORICAL") != "REAL" or getattr(trade, "is_ghost", False):
            return True
        if not self.broker or not trade.symbol_token or not trade.trading_symbol:
            self._mark_recovery(trade, "Cannot establish broker protection without broker contract details")
            return False
        qty = int(quantity or trade.broker_quantity or trade.quantity)
        if qty <= 0:
            return False
        trigger = self._protective_trigger_for(trade)
        side = self._protective_exit_side(trade)
        if trigger <= 0:
            self._mark_recovery(trade, "Protective stop trigger is invalid")
            return False

        # The read-decide-place window below is serialised so the PositionSync reconcile
        # thread and the scanner tick thread cannot both observe protective_order_id==""
        # for the same fill and each place a stop (→ duplicate SL-M → naked reversed leg
        # on trigger). RLock: safe if this thread re-enters via save_state().
        if not hasattr(self, "_protection_lock"):
            self._protection_lock = threading.RLock()
        with self._protection_lock:
            now = time.monotonic()
            if not hasattr(self, "_last_protective_sync"):
                self._last_protective_sync = {}
            if not hasattr(self, "_last_protective_status_poll"):
                self._last_protective_status_poll = {}
            if not hasattr(self, "_last_recovery_alert"):
                self._last_recovery_alert = {}
            last_sync = float(self._last_protective_sync.get(trade.id, 0.0) or 0.0)
            existing = str(getattr(trade, "protective_order_id", "") or "")
            last_status_poll = float(self._last_protective_status_poll.get(trade.id, 0.0) or 0.0)
            if existing and now - last_status_poll >= 15.0:
                getter = getattr(self.broker, "get_order_status", None)
                stop_status = self._order_status_text(getter(existing)) if callable(getter) else ""
                self._last_protective_status_poll[trade.id] = now
                if stop_status in {"rejected", "cancelled", "canceled"}:
                    trade.protective_order_id = ""
                    existing = ""
                elif stop_status in {"complete", "filled"}:
                    self._mark_recovery(
                        trade,
                        "Protective stop reports filled while broker position is still open",
                    )
                    return False
            if existing:
                tighter = (
                    trigger > float(trade.protective_trigger or 0.0)
                    if side == "SELL"
                    else trigger < float(trade.protective_trigger or float("inf"))
                )
                qty_changed = qty != int(trade.protective_quantity or 0)
                if not tighter and not qty_changed:
                    return True
                if now - last_sync < 2.0:
                    return True
                try:
                    ok = self.broker.modify_protective_stop(
                        existing, trade.trading_symbol, trade.symbol_token, qty, side,
                        trigger, self.product_type,
                    )
                except (BrokerOrderRejected, PermissionError) as exc:
                    # modify_protective_stop now requires the account lease, so it can
                    # refuse by raising. An escape here would skip _mark_recovery and
                    # leave the trade believing its stop was tightened.
                    logger.error(
                        f"Protective-stop tighten refused for {trade.trading_symbol}: {exc}"
                    )
                    ok = False
                self._last_protective_sync[trade.id] = now
                if ok:
                    trade.protective_trigger = trigger
                    trade.protective_quantity = qty
                    trade.protective_updated_at = datetime.now(IST)
                    self.save_state()
                    return True
                self._mark_recovery(trade, "Broker protective stop could not be tightened")
                self.save_state()
                return False

            # ── Idempotent placement (audit BLOCKER-1/2 fix) ──
            finder = getattr(self.broker, "find_working_protective_stop", None)

            def _adopt(order_id: str, note: str, actual_trigger=None) -> bool:
                # A working SL-M is now in charge, so the fallback trigger must go —
                # leaving both armed means two exits and a naked reversed leg. The
                # return value is the whole point of that helper and was discarded
                # here: a REFUSED cancellation left the GTT armed alongside the SL-M,
                # and the trade was then reported protected.
                # Record the id FIRST. This order is live at the exchange whether or not
                # the GTT can be cancelled, and losing its id is how the position ends up
                # with a SECOND SL-M next cycle — the duplicate this subsystem exists to
                # prevent. _adopt is also the success path for a freshly PLACED stop.
                trade.protective_order_id = str(order_id)
                if not self._release_gtt_fallback(trade, "exchange SL-M took over"):
                    trade.protective_quantity = qty
                    trade.protective_updated_at = datetime.now(IST)
                    self._last_protective_sync[trade.id] = now
                    self._mark_recovery(
                        trade,
                        f"SL-M {order_id} is live but the GTT fallback could not be "
                        "cancelled; both are armed and would exit the position twice.",
                    )
                    self.save_state()
                    return False
                # The level the EXCHANGE holds, which is not always the level we want.
                # Writing the desired trigger for a stale-trigger adoption made every
                # later tightening check compare against a value no order carried, see
                # "already tighter", and never contact the broker again — the position
                # reads as protected at a price nothing is protecting it at.
                trade.protective_trigger = (
                    float(actual_trigger) if actual_trigger is not None else trigger
                )
                trade.protective_quantity = qty
                trade.protective_updated_at = datetime.now(IST)
                trade.recovery_required_since = None
                self._last_protective_sync[trade.id] = now
                self.save_state()
                logger.info(
                    f"{note} for {trade.trading_symbol}: {order_id} qty={qty} trigger={trigger:.2f}"
                )
                return True

            # Adopt any SL-M already resting at the exchange for this contract before
            # placing a new one — covers a concurrent caller that just placed, and an
            # entry whose acknowledgement was lost but which reached the exchange.
            #
            # This reads the TRI-STATE, not the collapsed id. find_working_protective_stop
            # returns "" for AMBIGUOUS and CONFLICT just as it does for NONE, and ""
            # here means "place a new stop" — so two resting SL-Ms would get a third.
            prior_state, prior, prior_trigger = self._inspect_existing_stop(trade, side, qty, trigger)
            if prior_state == "FOUND_STALE_TRIGGER" and prior:
                # It IS this position's stop, just armed at a different level. Adopt it
                # and let the ordinary tightening path move the trigger — placing a
                # second stop here would be the duplicate-SL-M bug.
                owner = self._trade_owning_protective_order(prior, exclude_id=trade.id)
                if owner is None:
                    adopted = _adopt(
                        prior,
                        "Adopted existing broker SL-M (stale trigger)",
                        actual_trigger=prior_trigger,
                    )
                    if adopted:
                        # Clear the sync clock AND leave protective_trigger at the level
                        # the exchange actually holds, so the next pass sees a genuine
                        # divergence and issues the modify. Recording the desired level
                        # here is what let a stale stop sit un-rearmed forever.
                        self._last_protective_sync[trade.id] = 0.0
                    return adopted
                prior_state, prior = "CONFLICT", ""
            if prior_state in {"AMBIGUOUS", "CONFLICT"}:
                # Something is resting that we cannot match to this position. Placing
                # another is how a contract ends up with several stops, and each extra
                # one is a naked reversed leg waiting for the others to fill.
                self._last_protective_sync[trade.id] = now
                self._mark_recovery(
                    trade,
                    f"Existing protective stop(s) on {trade.trading_symbol} are "
                    f"{prior_state.lower()}; refusing to place another. Reconcile the "
                    "order book manually.",
                )
                self.save_state()
                return False
            if prior:
                owner = self._trade_owning_protective_order(prior, exclude_id=trade.id)
                if owner is not None:
                    # Adopting it would point two trades at one SL-M; whichever exits
                    # first cancels it and silently strips the other's protection.
                    self._last_protective_sync[trade.id] = now
                    self._mark_recovery(
                        trade,
                        f"The only working stop on {trade.trading_symbol} belongs to trade "
                        f"{owner.id}; refusing to adopt or duplicate it.",
                    )
                    self.save_state()
                    return False
                return _adopt(prior, "Adopted existing broker SL-M")

            try:
                order_id = self.broker.place_protective_stop(
                    trade.trading_symbol, trade.symbol_token, qty, side, trigger, self.product_type,
                    **self._tag_kwargs(
                        self.broker.place_protective_stop, trade, "PROTECTIVE"
                    ),
                )
            except BrokerOrderAmbiguous as exc:
                # The SL-M may already rest at the exchange despite the lost ack. NEVER
                # blindly re-place. Re-scan and adopt if found, else fail closed to
                # RECOVERY_REQUIRED for supervised reconciliation.
                self._last_protective_sync[trade.id] = now
                # Ask the TAG first. The symbol/side scan below can only answer "some
                # SL-M for this contract exists", which is already wrong when two trades
                # sit on one strike; the tag identifies this trade's own order exactly.
                recovered = self._recover_order_id_from_tag(trade, "PROTECTIVE")
                if not recovered and callable(finder):
                    try:
                        recovered = str(finder(trade.trading_symbol, trade.symbol_token, side) or "")
                    except Exception as scan_exc:
                        logger.warning(
                            f"Post-ambiguous protective-stop scan failed for {trade.trading_symbol}: {scan_exc}"
                        )
                if recovered:
                    return _adopt(recovered, "Recovered ambiguous broker SL-M")
                self._mark_recovery(trade, f"Protective stop acknowledgement ambiguous: {exc}")
                self.save_state()
                return False
            except (BrokerOrderRejected, PermissionError) as exc:
                # The OTHER two ways place_protective_stop fails: a dead broker session
                # and an account lease held by another process. Both refuse before any
                # order is sent, so nothing is resting — but neither was caught here, so
                # both unwound out of this method AND out of reconcile_broker_positions.
                # A filled position was left booked OPEN with no stop and no recovery
                # flag (a healthy-looking row on the dashboard), and every trade after it
                # in the reconcile loop went unreconciled.
                self._last_protective_sync[trade.id] = now
                # A GTT is safe when the refusal was ours to fix — nothing is resting.
                # It is NOT safe when another process owns the account: that holder is
                # managing this position's stop, and our trigger would stack on theirs.
                lease_denied = isinstance(exc, PermissionError)
                armed = False if lease_denied else self._arm_gtt_fallback(trade, qty, side, trigger)
                self._mark_recovery(
                    trade,
                    f"Protective stop refused: {exc}"
                    + ("; GTT fallback armed on the server" if armed else ""),
                )
                self.save_state()
                return False

            self._last_protective_sync[trade.id] = now
            if order_id:
                return _adopt(order_id, "Broker SL-M armed")
            # The broker REFUSED the stop outright — so unlike the ambiguous branch
            # above, nothing is resting at the exchange and a GTT cannot double up with
            # an invisible SL-M. Arm one: it lives on Zerodha's servers and fires even
            # if this process dies, which is precisely the exposure that left positions
            # unprotected on 2026-07-31. Still marked RECOVERY_REQUIRED — a GTT is a
            # safety net, not working stop management.
            armed = self._arm_gtt_fallback(trade, qty, side, trigger)
            self._mark_recovery(
                trade,
                "Confirmed broker position has no exchange-side protective stop"
                + ("; GTT fallback armed on the server" if armed else ""),
            )
            self.save_state()
            return False

    def _clear_flatten_block(self, trade: Trade) -> None:
        for attr in ("_flatten_block_counts", "_flatten_block_since", "_last_flatten_block_alert"):
            blocks = getattr(self, attr, None)
            if blocks:
                blocks.pop(trade.id, None)

    def _flatten_block_deadline_passed(self, trade: Trade, blocked_since: float) -> bool:
        """Has the operator opted into flattening anyway after being blocked this long?

        Off by default (0). Withholding is the safe default — a stop still resting plus
        our market exit is a naked reversed leg — but it can carry an NRML position past
        the close on nothing but an alert, so the trade-off is exposed as a setting
        rather than decided here.
        """
        from config.settings import get_settings

        try:
            deadline = float(
                getattr(get_settings(), "recovery_flatten_block_deadline_seconds", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            # Deliberately narrow: a broad except here swallowed a NameError from a
            # missing import and turned an opted-in deadline into a silent no-op.
            return False
        if deadline <= 0 or (time.time() - float(blocked_since or 0.0)) < deadline:
            return False
        trade.reversal_watch_required = True
        logger.critical(
            f"FLATTEN DEADLINE REACHED for {trade.trading_symbol} after "
            f"{deadline:.0f}s blocked: exiting anyway per "
            "recovery_flatten_block_deadline_seconds. A protective stop MAY still be "
            "resting — if it fires it will open a REVERSED position. Watch this contract."
        )
        return True

    def _withhold_flatten(self, trade: Trade, note: str) -> bool:
        """Refuse a flatten, count it, and escalate once it stops being transient.

        Every withholding path routes through here. Withholding is correct — a double
        exit is worse than a late one — but check_session_end retries on every pass, so
        an unbounded refusal silently carries a live NRML position past the close.
        """
        if not hasattr(self, "_flatten_block_counts"):
            self._flatten_block_counts = {}
        if not hasattr(self, "_flatten_block_since"):
            self._flatten_block_since = {}
        seen = self._flatten_block_counts.get(trade.id, 0) + 1
        self._flatten_block_counts[trade.id] = seen
        blocked_since = self._flatten_block_since.setdefault(trade.id, time.time())
        if self._flatten_block_deadline_passed(trade, blocked_since):
            return False
        if seen >= 3:
            note += (
                f" ({seen} consecutive blocked attempts). ACT MANUALLY: inspect the order "
                f"book for {trade.trading_symbol} at your Zerodha terminal, cancel any "
                "resting stop, and flatten the position by hand."
            )
            # Throttled: check_session_end runs ~1 Hz, and an unthrottled CRITICAL here
            # reproduces the log flood the recovery latch was added to stop.
            last = getattr(self, "_last_flatten_block_alert", {}).get(trade.id, 0.0)
            if time.time() - last >= 60.0:
                if not hasattr(self, "_last_flatten_block_alert"):
                    self._last_flatten_block_alert = {}
                self._last_flatten_block_alert[trade.id] = time.time()
                logger.critical(
                    f"FLATTEN BLOCKED {seen}x for {trade.trading_symbol}: neither exiting "
                    "nor leaving the position is provably safe. Manual reconciliation required."
                )
        self._mark_recovery(trade, note)
        return True

    def _inspect_existing_stop(self, trade: Trade, side: str, qty: int, trigger: float = 0.0):
        """(state, order_id, resting_trigger) for a stop already on this contract.

        Prefers the tri-state inspector and passes the position's own size/product, so
        the broker's quantity/product discrimination is actually exercised — the
        collapsed find_working_protective_stop() reports "" for AMBIGUOUS and CONFLICT
        alike, which reads as "nothing there, go ahead and place one".
        """
        inspector = getattr(self.broker, "inspect_working_protective_stop", None)
        if callable(inspector):
            try:
                found = inspector(
                    trade.trading_symbol, trade.symbol_token, side,
                    quantity=int(qty or 0), product_type=self.product_type,
                    trigger_price=float(trigger or 0.0),
                ) or {}
            except Exception as exc:
                logger.warning(
                    f"Protective-stop idempotency scan failed for {trade.trading_symbol}: {exc}"
                )
                return "UNKNOWN", "", None
            level = found.get("trigger_price")
            try:
                level = float(level) if level is not None else None
            except (TypeError, ValueError):
                level = None
            return (
                str(found.get("state") or "UNKNOWN").upper(),
                str(found.get("order_id") or ""),
                level,
            )
        finder = getattr(self.broker, "find_working_protective_stop", None)
        if not callable(finder):
            return "NONE", "", None
        try:
            oid = str(finder(trade.trading_symbol, trade.symbol_token, side) or "")
        except Exception as exc:
            logger.warning(
                f"Protective-stop idempotency scan failed for {trade.trading_symbol}: {exc}"
            )
            return "UNKNOWN", "", None
        return ("FOUND", oid, None) if oid else ("NONE", "", None)

    def _entry_margin_verdict(self, symbol: str, qty: int, side: str, price: float):
        """Can this entry be funded? None when the broker cannot answer.

        Deliberately fail-OPEN: a missing or erroring margin endpoint must never stop
        trading, only a definitive "not enough money" may. The check is one broker
        round-trip per entry attempt, which is cheap next to sending an order that is
        certain to be refused.
        """
        preflight = getattr(self.broker, "preflight_entry_margin", None)
        if not callable(preflight):
            return None
        try:
            verdict = preflight(
                symbol=symbol, qty=int(qty or 0), side=str(side or "BUY"),
                product_type=self.product_type, price=float(price or 0.0),
            ) or {}
        except Exception as exc:
            logger.debug(f"Entry margin preflight unavailable for {symbol}: {exc}")
            return None
        if not verdict.get("ok", True):
            logger.critical(
                f"ENTRY BLOCKED (funds) {symbol} x{qty}: "
                f"{verdict.get('limitation') or verdict.get('reason') or 'insufficient margin'}"
            )
        return verdict

    def _live_position_for_trade(self, trade: Trade):
        """This trade's broker position row, or None when the book says flat.

        Returns (row, readable). `readable` is False when the positions call itself
        failed — the caller must not read that as flat, which is the same rule every
        other scan in this file follows.
        """
        getter = getattr(self.broker, "get_positions", None)
        if not callable(getter):
            return None, False
        try:
            response = getter()
        except Exception as exc:
            logger.debug(f"Positions read failed during partial reduce: {exc}")
            return None, False
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            return None, False
        symbol = str(getattr(trade, "trading_symbol", "") or "").upper()
        for row in response["data"]:
            if str(row.get("tradingsymbol") or "").upper() == symbol:
                return row, True
        return None, True

    def _confirmed_reduced_quantity(
        self, trade: Trade, expected_quantity: int, timeout: float = 2.0
    ) -> Optional[int]:
        """Wait briefly for POSITIONS — not an order ack — to prove the reduce landed.

        An acknowledgement says the order was accepted, not that it filled. Booking a
        realised child off an ack is how a phantom P&L row gets written for lots that
        never left. None means unproven.
        """
        deadline = time.monotonic() + max(0.5, float(timeout or 0.5))
        while time.monotonic() < deadline:
            position, readable = self._live_position_for_trade(trade)
            if readable:
                if position is None:
                    return 0
                signed = self._position_quantity(position)
                if signed and (1 if signed > 0 else -1) != self._expected_position_sign(trade):
                    return None          # direction flipped: not our reduce
                if abs(signed) <= expected_quantity:
                    return abs(signed)
            time.sleep(0.2)
        return None

    def _book_partial_child(
        self, trade: Trade, close_lots: int, exit_price: float, reason: str,
        exit_time: Optional[datetime] = None,
    ) -> None:
        """Realise `close_lots` as a closed child row and shrink the parent."""
        from dataclasses import replace as _replace

        child = _replace(trade)
        child.id = f"{trade.id}#P{int(close_lots)}"
        child.lots = int(close_lots)
        child.status = "CLOSED"
        child.execution_status = "CLOSED"
        child.pending_exit = False
        child.pending_execution = False
        child.exit_reason = reason
        child.exit_time = exit_time or datetime.now(IST)
        child.exit_price = float(exit_price)
        child.broker_quantity = int(close_lots) * int(trade.lot_size or 1)
        child.protective_order_id = ""
        child.gtt_trigger_id = ""
        gross = (float(exit_price) - float(trade.entry_price)) * child.broker_quantity
        if trade.inst_type != "OPT" and str(trade.direction or "").upper() == "SHORT":
            gross = -gross
        try:
            from engine.trade_accounting import estimate_trade_charges
            from config.settings import get_settings

            child.charges = estimate_trade_charges(
                trade.entry_price, exit_price, child.broker_quantity, trade.inst_type,
                get_settings(), trade.instrument_multiplier, instrument=trade.instrument,
            )
        except Exception:
            child.charges = 0.0
        child.pnl = gross - float(child.charges or 0.0)
        self.closed_trades.append(child)

        remaining = max(1, int(trade.lots or 1) - int(close_lots))
        trade.lots = remaining
        trade.broker_quantity = remaining * int(trade.lot_size or 1)

    def partial_close_trade(
        self, trade_id: str, close_lots: int, exit_price: float,
        reason: str = "PARTIAL_DERISK", exit_time: Optional[datetime] = None,
    ) -> bool:
        """Bank part of a multi-lot winner and let the remainder run.

        Refuses unless the trade is cleanly OPEN with no exit already in flight, and only
        once per trade — partial_derisk_done latches on BOOKED success, so a reduce that
        failed can be retried on the next tick while one that worked cannot be repeated.

        On a REAL position the broker work is ordered so the position can never be
        over-exited or left naked; see _request_broker_partial_reduce. Simulated and ghost
        books are booked synchronously at the decision price.
        """
        with self._lock:
            trade = self.open_trades.get(trade_id)
            if not trade or trade.status != "OPEN":
                return False
            if trade.pending_exit or str(trade.execution_status or "").upper() in {
                "EXIT_PENDING", "PARTIAL_EXIT", "RECOVERY_REQUIRED",
            }:
                return False
            if bool(getattr(trade, "partial_derisk_done", False)):
                return False
            if (
                str(getattr(trade, "partial_reduce_order_id", "") or "")
                or int(getattr(trade, "partial_reduce_requested_quantity", 0) or 0) > 0
            ):
                # A persisted in-flight reduce must be reconciled against broker positions
                # before a second one may be submitted against the same lots.
                return False
            total = max(1, int(trade.lots or 1))
            close_lots = int(close_lots or 0)
            if close_lots < 1 or close_lots >= total:
                # Closing everything is an exit, not a de-risk; that path has its own
                # protective-stop handling and must not be reached through here.
                return False
            price = float(exit_price or trade.current_price or trade.entry_price or 0.0)
            if price <= 0:
                return False
            is_real = bool(
                getattr(self, "mode", "HISTORICAL") == "REAL"
                and self.broker
                and getattr(trade, "symbol_token", "")
                and not getattr(trade, "is_ghost", False)
            )
            if is_real:
                if bool(getattr(trade, "_partial_reduce_inflight", False)):
                    return False
                setattr(trade, "_partial_reduce_inflight", True)
            else:
                self._book_partial_child(trade, close_lots, price, reason, exit_time)
                trade.partial_derisk_done = True
                self.save_state()
                return True

        self._request_broker_partial_reduce(trade, close_lots, price, reason, exit_time)
        return True

    def _request_broker_partial_reduce(
        self, trade: Trade, close_lots: int, exit_price: float, reason: str,
        exit_time: Optional[datetime],
    ) -> None:
        """Reduce a REAL position by `close_lots`, in the only safe order.

          1. Cancel the full-size protective stop and PROVE it. An unproven cancel means
             the SL-M may still rest for the full size, and the partial exit on top of it
             double-exits — so an unproven cancel aborts and changes nothing.
          2. Place the partial exit. If it fails, re-arm the full-size stop before
             returning: the position is unchanged, so it must not be left unprotected.
          3. Only once POSITIONS prove the reduce, book the child and re-arm a stop for
             the remainder. If that re-arm fails the trade is marked for recovery rather
             than left running naked.
        """
        quantity = int(close_lots) * int(trade.lot_size or 1)

        def reduce_bg():
            try:
                self._run_partial_reduce(trade, close_lots, quantity, exit_price, reason, exit_time)
            except Exception as exc:
                logger.error(f"Partial reduce crashed for {trade.trading_symbol}: {exc}")
                self._mark_recovery(trade, f"Partial de-risk failed unexpectedly: {exc}")
            finally:
                setattr(trade, "_partial_reduce_inflight", False)
                trade.partial_reduce_order_id = ""
                trade.partial_reduce_requested_quantity = 0
                self.save_state()

        from engine.async_queue import execution_queue

        if execution_queue.submit(f"PartialReduce_{trade.trading_symbol}", reduce_bg) is False:
            setattr(trade, "_partial_reduce_inflight", False)
            logger.warning(
                f"Partial de-risk for {trade.trading_symbol} was refused by the execution "
                "queue; the position is untouched and fully protected."
            )

    def _run_partial_reduce(
        self, trade: Trade, close_lots: int, quantity: int, exit_price: float,
        reason: str, exit_time: Optional[datetime],
    ) -> None:
        existing_stop = str(getattr(trade, "protective_order_id", "") or "")
        if existing_stop:
            confirm = getattr(self.broker, "cancel_order_confirmed", None)
            cancelled = None
            if callable(confirm):
                try:
                    cancelled = confirm(existing_stop, "STOPLOSS")
                except (BrokerOrderRejected, PermissionError) as exc:
                    logger.error(f"Partial-reduce stop cancel refused: {exc}")
                    cancelled = None
            if cancelled is not True:
                # Unproven cancel: the full-size SL-M may still be resting, and a partial
                # exit on top of it exits the position twice.
                logger.warning(
                    f"Partial de-risk aborted for {trade.trading_symbol}: the full-size "
                    "protective stop could not be confirmed cancelled."
                )
                return
            trade.protective_order_id = ""

        side = self._protective_exit_side(trade)
        try:
            order_id = self.broker.close_order(
                trade.trading_symbol, trade.symbol_token, quantity, side,
                self.product_type,
                **self._tag_kwargs(self.broker.close_order, trade, "EXIT"),
            )
        except (BrokerOrderRejected, PermissionError) as exc:
            logger.error(f"Partial exit refused for {trade.trading_symbol}: {exc}")
            self.ensure_broker_protection(trade, trade.broker_quantity)
            return
        except BrokerOrderAmbiguous as exc:
            # The reduce MAY be live. Do not re-arm a full-size stop on top of it and do
            # not book anything; let reconciliation read the real quantity.
            self._mark_recovery(
                trade,
                f"Partial de-risk acknowledgement ambiguous ({exc}); broker quantity "
                "must be reconciled before this position is managed further.",
            )
            return
        if not order_id:
            self.ensure_broker_protection(trade, trade.broker_quantity)
            return

        trade.partial_reduce_order_id = str(order_id)
        trade.partial_reduce_requested_quantity = quantity
        self.save_state()

        expected_remaining = max(0, int(trade.broker_quantity or 0) - quantity)
        confirmed = self._confirmed_reduced_quantity(trade, expected_remaining)
        if confirmed is None:
            self._mark_recovery(
                trade,
                "Partial de-risk could not be confirmed from broker positions; the "
                "protective stop is NOT re-armed until the real quantity is known.",
            )
            return

        with self._lock:
            self._book_partial_child(trade, close_lots, exit_price, reason, exit_time)
            trade.broker_quantity = confirmed or trade.broker_quantity
            trade.partial_derisk_done = True
        self.save_state()
        logger.success(
            f"Partial de-risk booked for {trade.trading_symbol}: {close_lots} lot(s) "
            f"realised, {trade.lots} running."
        )
        if not self.ensure_broker_protection(trade, trade.broker_quantity):
            self._mark_recovery(
                trade, "Remainder of a partial de-risk could not be re-protected"
            )

    # ── scale-in ─────────────────────────────────────────────────────────────
    def scale_in_trade(
        self, trade_id: str, add_lots: Optional[int] = None, price: float = 0.0,
        reason: str = "CASCADE_ADD",
    ) -> Tuple[bool, str]:
        """Add lots to a position that is already working.

        The mirror of partial_close_trade, and deliberately ordered the other way round.
        The reduce must cancel the full-size stop FIRST, because exiting on top of a live
        SL-M double-exits. An add has the opposite failure direction: the existing stop
        already covers what is held, so the order is ADD -> confirm from broker positions
        -> re-arm the stop for the new total. If the add never fills, nothing changed and
        the position stayed protected the whole time; if it fills and the re-arm fails,
        the trade is marked for recovery rather than left running under-protected.

        Returns (accepted, reason) so a caller can log WHY an add was refused instead of
        guessing from a bare False.
        """
        from config.settings import get_settings

        settings = get_settings()
        if not _setting_enabled(getattr(settings, "scale_in_enabled", False), False):
            return False, "scale-in disabled"

        with self._lock:
            trade = self.open_trades.get(trade_id)
            if not trade or trade.status != "OPEN":
                return False, "trade is not cleanly open"
            if trade.pending_exit or str(trade.execution_status or "").upper() in {
                "EXIT_PENDING", "PARTIAL_EXIT", "RECOVERY_REQUIRED",
            }:
                return False, f"exit or recovery in flight ({trade.execution_status})"
            if bool(getattr(trade, "partial_derisk_done", False)):
                # De-risking and scaling in are opposite decisions about the same trade.
                return False, "already de-risked; not adding back"
            if (
                str(getattr(trade, "scale_in_order_id", "") or "")
                or int(getattr(trade, "scale_in_requested_quantity", 0) or 0) > 0
            ):
                return False, "an add is already in flight"
            if bool(getattr(trade, "_scale_in_inflight", False)):
                return False, "an add is already in flight"

            if add_lots is None:
                # scale_in_lots_per_add is the size of one add. A caller that knows
                # better may override it; most just say "add".
                add_lots = int(getattr(settings, "scale_in_lots_per_add", 1) or 1)
            add_lots = max(0, int(add_lots or 0))
            if add_lots < 1:
                return False, "no lots to add"
            max_adds = max(0, int(getattr(settings, "scale_in_max_adds_per_trade", 2) or 0))
            if int(getattr(trade, "scale_in_count", 0) or 0) >= max_adds:
                return False, f"add cap {max_adds} reached"
            max_total = max(1, int(getattr(settings, "scale_in_max_total_lots", 6) or 6))
            if int(trade.lots or 0) + add_lots > max_total:
                return False, f"would exceed {max_total} total lots"

            price = float(price or trade.current_price or trade.entry_price or 0.0)
            if price <= 0:
                return False, "no usable price"

            ok, why = self._scale_in_is_earned(trade, settings)
            if not ok:
                return False, why

            # An add-on is new money at risk even though it is not a new position, so it
            # faces the rupee budget. It does NOT face the per-index/bucket count caps —
            # those bound how many ideas are live, and this is the same idea.
            admitted, risk_why = self._entry_risk_admission(
                inst_type=trade.inst_type,
                direction=trade.direction,
                price=price,
                trailing_stop=float(trade.current_stop or trade.trailing_stop or 0.0),
                initial_stop=float(getattr(trade, "initial_stop", 0.0) or trade.trailing_stop or 0.0),
                lots=add_lots,
                lot_size=int(trade.lot_size or 1),
                instrument_multiplier=float(getattr(trade, "instrument_multiplier", 1.0) or 1.0),
                is_recovery=False,
            )
            if not admitted:
                return False, f"entry risk budget: {risk_why}"

            is_real = bool(
                getattr(self, "mode", "HISTORICAL") == "REAL"
                and self.broker
                and getattr(trade, "symbol_token", "")
                and not getattr(trade, "is_ghost", False)
            )
            if not is_real:
                self._book_scale_in(trade, add_lots, price, reason)
                self.save_state()
                return True, "booked"
            setattr(trade, "_scale_in_inflight", True)

        self._request_broker_scale_in(trade, add_lots, price, reason)
        return True, "submitted"

    def _scale_in_is_earned(self, trade: Trade, settings) -> Tuple[bool, str]:
        """Only add to a trade that is already working, and still with the trend.

        Adding to a loser is averaging down, which turns one bad idea into a bigger one.
        Profit is measured in R against the ORIGINAL risk, not the current unrealised
        rupees, so the bar does not get easier as the position grows.
        """
        risk = self._frozen_initial_risk_rs(trade)
        min_r = max(0.0, float(getattr(settings, "scale_in_min_profit_r", 0.75) or 0.0))
        if min_r > 0:
            if risk <= 0:
                return False, "original risk unknown, cannot measure R"
            entry = float(trade.entry_price or 0.0)
            mark = float(trade.current_price or 0.0)
            if entry <= 0 or mark <= 0:
                return False, "no mark to measure profit against"
            # OPTIONS ARE ALWAYS BOUGHT. A bearish view is a bought PE, stored with
            # direction="SHORT", and it profits when its PREMIUM RISES exactly as a CE
            # does. Reading `direction` alone inverted the sign for every PE: premium
            # 120 -> 100 is a Rs.1,300 loss on 65 qty, about -1.1R, but computed as
            # (120-100) = +20 it reported +1.11R and cleared the 0.75R bar.
            #
            # So this function did the one thing its own docstring forbids -- it added
            # size to a loser. The `mult` line just below already pins 1.0 for OPT, so
            # the option case was known here; only the sign was missed.
            signed = (
                (mark - entry)
                if (str(trade.inst_type or "").upper() == "OPT"
                    or str(trade.direction).upper() == "LONG")
                else (entry - mark)
            )
            qty = int(getattr(trade, "broker_quantity", 0) or trade.quantity or 0)
            mult = 1.0 if str(trade.inst_type or "").upper() == "OPT" else float(
                getattr(trade, "instrument_multiplier", 1.0) or 1.0
            )
            open_r = (signed * max(1, qty) * mult) / risk
            if open_r < min_r:
                return False, f"open profit {open_r:.2f}R below {min_r:.2f}R"

        if _setting_enabled(getattr(settings, "scale_in_requires_trend", True), True):
            regime = str(getattr(trade, "market_regime", "") or "").upper()
            if regime:
                bullish = "UP" in regime or "BULL" in regime
                bearish = "DOWN" in regime or "BEAR" in regime
                wants_up = str(trade.direction).upper() == "LONG"
                if (wants_up and bearish) or (not wants_up and bullish):
                    return False, f"regime {regime} opposes the trade"
                if not (bullish or bearish):
                    return False, f"regime {regime} is not trending"
        return True, ""

    def _frozen_initial_risk_rs(self, trade: Trade) -> float:
        """Rupee risk committed at ENTRY, held constant across every add-on.

        Computed once, on the first add, from the pre-scale entry and stop. After that the
        weighted-average entry has moved and the same arithmetic would give a different —
        and meaningless — answer.
        """
        stored = float(getattr(trade, "frozen_initial_risk_rs", 0.0) or 0.0)
        if stored > 0:
            return stored
        entry = float(trade.entry_price or 0.0)
        stop = float(getattr(trade, "initial_stop", 0.0) or trade.trailing_stop or 0.0)
        if entry <= 0 or stop <= 0:
            return 0.0
        qty = int(getattr(trade, "broker_quantity", 0) or trade.quantity or 0)
        mult = 1.0 if str(trade.inst_type or "").upper() == "OPT" else float(
            getattr(trade, "instrument_multiplier", 1.0) or 1.0
        )
        return abs(entry - stop) * max(1, qty) * max(0.0, mult)

    def _book_scale_in(self, trade: Trade, add_lots: int, fill_price: float, reason: str) -> None:
        """Fold a confirmed add into the parent. Caller holds the lock."""
        # Freeze R BEFORE the entry price moves — afterwards the original is unrecoverable.
        if float(getattr(trade, "frozen_initial_risk_rs", 0.0) or 0.0) <= 0:
            trade.frozen_initial_risk_rs = self._frozen_initial_risk_rs(trade)

        old_lots = max(0, int(trade.lots or 0))
        old_qty = old_lots * int(trade.lot_size or 1)
        add_qty = int(add_lots) * int(trade.lot_size or 1)
        total_qty = old_qty + add_qty
        if total_qty > 0:
            trade.entry_price = (
                float(trade.entry_price or 0.0) * old_qty + float(fill_price) * add_qty
            ) / total_qty
        trade.lots = old_lots + int(add_lots)
        trade.scale_in_lots = int(getattr(trade, "scale_in_lots", 0) or 0) + int(add_lots)
        trade.scale_in_count = int(getattr(trade, "scale_in_count", 0) or 0) + 1
        trade.broker_quantity = total_qty
        logger.success(
            f"Scale-in booked for {trade.trading_symbol}: +{add_lots} lot(s) @ "
            f"{fill_price:.2f} ({reason}); {trade.lots} lots now, average entry "
            f"{trade.entry_price:.2f}, 1R still Rs.{trade.frozen_initial_risk_rs:.0f}"
        )

    def _request_broker_scale_in(
        self, trade: Trade, add_lots: int, price: float, reason: str,
    ) -> None:
        quantity = int(add_lots) * int(trade.lot_size or 1)

        def add_bg():
            try:
                self._run_scale_in(trade, add_lots, quantity, price, reason)
            except Exception as exc:
                logger.error(f"Scale-in crashed for {trade.trading_symbol}: {exc}")
                self._mark_recovery(trade, f"Scale-in failed unexpectedly: {exc}")
            finally:
                setattr(trade, "_scale_in_inflight", False)
                trade.scale_in_order_id = ""
                trade.scale_in_requested_quantity = 0
                self.save_state()

        from engine.async_queue import execution_queue

        if execution_queue.submit(f"ScaleIn_{trade.trading_symbol}", add_bg) is False:
            setattr(trade, "_scale_in_inflight", False)
            logger.warning(
                f"Scale-in for {trade.trading_symbol} was refused by the execution queue; "
                "the position is untouched and fully protected."
            )

    def _run_scale_in(
        self, trade: Trade, add_lots: int, quantity: int, price: float, reason: str,
    ) -> None:
        entry_side = "BUY" if trade.inst_type == "OPT" else (
            "BUY" if str(trade.direction).upper() in {"LONG", "BUY"} else "SELL"
        )
        # Every new entry funnels through _entry_margin_verdict, which both checks the
        # exact Kite order margin against free cash AND reserves it so a concurrent entry
        # cannot spend the same rupees. The add path did neither. The missing reservation
        # was the sharper half: with no record that this money is committed, an ordinary
        # entry arriving seconds later reads `available - outstanding` as if the add had
        # never happened, passes its own preflight, and the account over-commits — exactly
        # the race the reservation ledger exists to prevent. Nothing else gated it either:
        # _entry_risk_admission is entry_risk_budget_enabled-gated and that ships False,
        # so an enabled scale-in had no rupee gate at all.
        #
        # Same fail-open contract as an entry: a missing or erroring margin endpoint must
        # never stop trading, only a definitive "not enough money" may. Refusing here
        # touches nothing on the parent position — it is still running, still protected by
        # the stop that covers its current quantity.
        preflight = self._entry_margin_verdict(
            trade.trading_symbol, quantity, entry_side, price
        )
        if preflight is not None and not preflight.get("ok", True):
            logger.error(
                f"Scale-in refused for {trade.trading_symbol} x{quantity}: "
                f"{preflight.get('limitation') or preflight.get('reason') or 'insufficient margin'}"
            )
            return
        reservations = [str((preflight or {}).get("reservation_id") or "")]
        reservations = [r for r in reservations if r]

        # The existing stop is NOT cancelled first. It covers the quantity currently held,
        # so leaving it armed means a failed add changes nothing and a filled add is
        # merely under-protected on the new lots until the re-arm below — never naked, and
        # never over-exited.
        try:
            order_id = self.broker.place_order(
                trade.trading_symbol, trade.symbol_token, quantity, entry_side,
                self.product_type, price=0.0,
                **self._tag_kwargs(self.broker.place_order, trade, "ADD"),
            )
        except (BrokerOrderRejected, PermissionError) as exc:
            # Definite refusal: this order consumed nothing, so give the margin back.
            self._release_margin_reservations(reservations)
            logger.error(f"Scale-in refused for {trade.trading_symbol}: {exc}")
            return
        except BrokerOrderAmbiguous as exc:
            # The add MAY be live. Book nothing and re-arm nothing: a stop sized for the
            # old quantity on a position that may now be larger is the safe direction, and
            # reconciliation will read the real quantity.
            self._mark_recovery(
                trade,
                f"Scale-in acknowledgement ambiguous ({exc}); broker quantity must be "
                "reconciled before this position is managed further.",
            )
            return
        if not order_id:
            # No order id and no exception: nothing was accepted, so the margin is free.
            # (An AMBIGUOUS acknowledgement above deliberately keeps its reservation —
            # that order may be live, and letting the next entry spend money it might
            # already be consuming is how an account over-commits on orders it cannot see.)
            self._release_margin_reservations(reservations)
            logger.warning(f"Scale-in for {trade.trading_symbol} returned no order id.")
            return

        trade.scale_in_order_id = str(order_id)
        trade.scale_in_requested_quantity = quantity
        self.save_state()

        expected_total = int(trade.broker_quantity or trade.quantity or 0) + quantity
        confirmed = self._confirmed_scaled_quantity(trade, expected_total)
        if confirmed is None:
            self._mark_recovery(
                trade,
                "Scale-in could not be confirmed from broker positions; the protective "
                "stop is NOT re-sized until the real quantity is known.",
            )
            return

        with self._lock:
            self._book_scale_in(trade, add_lots, price, reason)
            trade.broker_quantity = confirmed or trade.broker_quantity
        self.save_state()

        if not self.ensure_broker_protection(trade, trade.broker_quantity):
            self._mark_recovery(
                trade, "Scale-in filled but the enlarged position could not be re-protected"
            )

    def _confirmed_scaled_quantity(
        self, trade: Trade, expected_total: int, timeout: float = 4.0
    ) -> Optional[int]:
        """Wait for POSITIONS — not an order ack — to prove the add landed.

        The exact mirror of _confirmed_reduced_quantity, including its rule that None
        means UNKNOWN and never "it did not fill". Booking an add off an acknowledgement
        would move the average entry price for lots that may never have arrived, and every
        R and P&L figure downstream is computed from that price.
        """
        deadline = time.monotonic() + max(0.5, float(timeout or 0.5))
        while time.monotonic() < deadline:
            position, readable = self._live_position_for_trade(trade)
            if readable and position is not None:
                signed = self._position_quantity(position)
                if signed and (1 if signed > 0 else -1) != self._expected_position_sign(trade):
                    return None          # direction flipped: not our add
                if abs(signed) >= int(expected_total):
                    return abs(signed)
            time.sleep(0.2)
        return None

    def _sample_pnl_path(self, trade) -> None:
        return sample_pnl_path(trade)

    @staticmethod
    def _session_live_start() -> dtime:
        """Configured regular-session open (default 09:15:00)."""
        from config.settings import get_settings

        label = str(
            getattr(get_settings(), "real_mode_live_start", "09:15:00") or "09:15:00"
        ).strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(label, fmt).time()
            except ValueError:
                continue
        return dtime(9, 15, 0)

    def _entry_mark_admission(
        self, *, inst_type: str, price: float, signal_time: Optional[datetime],
        entry_time: Optional[datetime], touch_quote: Optional[Dict[str, Any]],
        trading_symbol: str, is_recovery: bool,
    ) -> Tuple[bool, str]:
        """Refuse an entry built on a pre-open signal or a stale entry mark.

        The 2026-07-28 loss (see config/settings.py, entry_preopen_signal_guard_enabled)
        was NOT a trading loss: a 09:10 pre-open signal executed at 09:15:34 on a
        previous-close mark of 131.70 against a live contract at 52.30, round-tripped in
        40ms for -Rs.10,402. Two independent checks, because neither covers the other:

          (a) SIGNAL FRESHNESS. A signal stamped before the session open was computed on
              pre-open data that never traded. Nothing downstream re-derives it, so the
              staleness survives all the way to the order.
          (b) MARK CONSISTENCY. An option's LTP reads yesterday's close until its first
              trade of the day prints, while the bid/ask are live from the first moment.
              A mark far outside the touch quote is therefore stale by construction.

        (b) needs a usable touch quote and (a) does not, so (a) still covers the open
        when the feed is not yet quoting. Recovery entries bypass both, matching
        _entry_risk_admission: a recovery re-establishes protection on exposure that
        already exists, and refusing it strands the very position this protects.
        """
        from config.settings import get_settings

        if is_recovery:
            return True, ""
        settings = get_settings()

        if _setting_enabled(
            getattr(settings, "entry_preopen_signal_guard_enabled", True), True
        ) and signal_time is not None and entry_time is not None:
            try:
                live_start = self._session_live_start()
                # Compare wall-clock in the session's own terms. Both stamps are IST in
                # this system; normalising defensively keeps a naive/aware mix from
                # raising inside an entry path.
                sig_local = signal_time.astimezone(IST) if signal_time.tzinfo else signal_time
                ent_local = entry_time.astimezone(IST) if entry_time.tzinfo else entry_time
                if (
                    sig_local.date() == ent_local.date()
                    and sig_local.time() < live_start
                    and ent_local.time() >= live_start
                ):
                    return False, (
                        f"signal {sig_local.strftime('%H:%M:%S')} predates the "
                        f"{live_start.strftime('%H:%M')} session open "
                        f"(entry {ent_local.strftime('%H:%M:%S')})"
                    )
            except Exception as exc:
                # A clock comparison must never stop trading.
                logger.debug(f"Pre-open signal guard skipped: {exc}")

        if not _setting_enabled(
            getattr(settings, "entry_mark_touch_validation_enabled", True), True
        ):
            return True, ""
        if str(inst_type or "").upper() != "OPT":
            return True, ""
        quote = touch_quote if isinstance(touch_quote, dict) else {}
        if not quote or not bool(quote.get("usable", False)):
            return True, ""
        try:
            bid = float(quote.get("bid") or 0.0)
            ask = float(quote.get("ask") or 0.0)
            mark = float(price or 0.0)
        except (TypeError, ValueError):
            return True, ""
        if mark <= 0 or bid <= 0 or ask <= 0 or ask < bid:
            return True, ""
        max_mult = float(getattr(settings, "entry_mark_max_ask_multiple", 1.50) or 1.50)
        min_mult = float(getattr(settings, "entry_mark_min_bid_multiple", 0.50) or 0.50)
        if max_mult > 0 and mark > ask * max_mult:
            return False, (
                f"entry mark {mark:.2f} is {mark / ask:.2f}x the live ask {ask:.2f} "
                f"on {trading_symbol or inst_type} (stale mark)"
            )
        if min_mult > 0 and mark < bid * min_mult:
            return False, (
                f"entry mark {mark:.2f} is {mark / bid:.2f}x the live bid {bid:.2f} "
                f"on {trading_symbol or inst_type} (stale mark)"
            )
        return True, ""

    def _entry_risk_admission(
        self, *, inst_type: str, direction: str, price: float, trailing_stop: float,
        initial_stop: float, lots: int, lot_size: int, instrument_multiplier: float,
        is_recovery: bool,
    ) -> Tuple[bool, str]:
        """Refuse an entry that would push the book past its stop-risk budget.

        Off by default. It earns its keep once several lots run across several indices
        with cascade scaling enabled — at one or two positions the daily circuit breaker
        already covers nearly the same ground, and the extra refusal is noise.

        Recovery entries bypass it, matching can_open_trade: a recovery is re-establishing
        protection on a position that already exists, and refusing it strands the very
        exposure this is meant to bound.
        """
        from config.settings import get_settings

        settings = get_settings()
        if not _setting_enabled(
            getattr(settings, "entry_risk_budget_enabled", False), False
        ):
            return True, ""
        if is_recovery:
            return True, ""
        try:
            from engine.entry_risk_policy import evaluate_entry_risk
        except Exception as exc:
            # A missing policy module must not silently stop trading.
            logger.debug(f"Entry-risk policy unavailable: {exc}")
            return True, ""

        candidate = {
            "inst_type": inst_type, "direction": direction, "price": price,
            "current_stop": trailing_stop, "initial_stop": initial_stop,
            "lots": lots, "lot_size": lot_size, "multiplier": instrument_multiplier,
        }
        # Same budgets the circuit breaker enforces, so there is one source of truth for
        # "how much may be lost" rather than two ladders that can drift apart.
        capital_fut = float(getattr(settings, "capital_fut", 0.0) or 0.0)
        capital_opt = float(getattr(settings, "capital_opt", 0.0) or 0.0)
        capital_total = float(getattr(settings, "capital_total", 0.0) or 0.0)
        try:
            decision = evaluate_entry_risk(
                candidate,
                list(self.open_trades.values()),
                fut_limit_rs=capital_fut * float(getattr(settings, "risk_fut_pct", 0.0) or 0.0) / 100.0,
                opt_limit_rs=capital_opt * float(getattr(settings, "risk_opt_pct", 0.0) or 0.0) / 100.0,
                global_limit_rs=capital_total * float(getattr(settings, "max_daily_loss_pct", 0.0) or 0.0) / 100.0,
                max_trade_loss_abs=float(getattr(settings, "max_trade_loss_abs", 0.0) or 0.0),
                slippage_bps=float(getattr(settings, "slippage_fut_bps", 0.0) or 0.0),
                realized_loss_by_segment=self._realized_loss_by_segment(),
            )
        except Exception as exc:
            logger.error(f"Entry-risk evaluation failed, allowing entry: {exc}")
            return True, ""
        return bool(decision.allowed), str(decision.reason)

    def _realized_loss_by_segment(self) -> Dict[str, float]:
        """Today's realised LOSSES per segment, as positive rupees.

        Folded into the budget so it shrinks as the session goes against you; without it
        the last entry of a losing day is admitted against the same headroom as the first.
        """
        today = datetime.now(IST).date()
        losses = {"FUT": 0.0, "OPT": 0.0}
        for trade in list(getattr(self, "closed_trades", []) or []):
            try:
                exit_time = getattr(trade, "exit_time", None) or getattr(trade, "entry_time", None)
                if exit_time is None:
                    continue
                stamp = exit_time if exit_time.tzinfo else IST.localize(exit_time)
                if stamp.astimezone(IST).date() != today:
                    continue
                pnl = float(getattr(trade, "pnl", 0.0) or 0.0)
            except (TypeError, ValueError, AttributeError):
                continue
            if pnl >= 0:
                continue
            segment = "FUT" if str(getattr(trade, "inst_type", "") or "").upper() == "FUT" else "OPT"
            losses[segment] += abs(pnl)
        return losses

    def _stale_entry_still_worth_filling(self, trade: Trade) -> bool:
        """Would this entry still be taken at the current mark?

        A stale LIMIT is cancelled and replaced with a MARKET order, which GUARANTEES a
        fill. That is the right move when the setup still stands and the limit merely
        missed by a tick. It is the wrong move when price has already run past the level
        the stop sits at, because the fill then enters a position that is a loser the
        instant it exists — the entry was never valid at that price, and the 12s window
        is long enough on a 1min option for exactly that to happen.

        Fails OPEN: with no usable mark or stop this returns True and the existing
        replace behaviour is unchanged. A missing price must not silently stop entries.
        """
        # An operator-supplied revalidator wins when one is wired; otherwise the
        # self-contained stop check below is what decides.
        revalidate = getattr(self, "entry_revalidator", None)
        if callable(revalidate):
            try:
                if revalidate(trade) is False:
                    return False
            except Exception as exc:
                logger.debug(f"Entry revalidation unavailable for {trade.id}: {exc}")

        try:
            mark = float(getattr(trade, "current_price", 0.0) or 0.0)
            stop = float(
                getattr(trade, "current_stop", 0.0)
                or getattr(trade, "trailing_stop", 0.0)
                or 0.0
            )
        except (TypeError, ValueError):
            return True
        if mark <= 0 or stop <= 0:
            return True
        # Options here are always long premium, so a breach is always downward.
        if str(getattr(trade, "inst_type", "") or "").upper() == "OPT":
            return mark > stop
        if str(getattr(trade, "direction", "LONG") or "LONG").upper() in {"LONG", "BUY"}:
            return mark > stop
        return mark < stop

    def _abandon_invalidated_entry(self, trade: Trade) -> None:
        """Close a cancelled entry that is no longer worth filling."""
        trade.status = "CLOSED"
        trade.pending_execution = False
        trade.execution_status = "REJECTED"
        trade.exit_reason = "SIGNAL_INVALIDATED"
        trade.exit_time = datetime.now(IST)
        trade.exit_price = trade.entry_price
        trade.execution_error = (
            "Stale entry cancelled and NOT replaced: the mark had already passed the "
            "stop, so a market fill would have opened a losing position"
        )
        with self._lock:
            if self.open_trades.pop(trade.id, None) is trade:
                self.closed_trades.append(trade)
        logger.warning(
            f"Abandoned stale entry for {trade.trading_symbol}: price moved past the "
            "stop while the limit rested, so it was not force-filled at market."
        )

    def _release_margin_reservations(self, reservation_ids) -> None:
        """Hand back margin an entry definitively did not consume.

        Never called on an ambiguous acknowledgement: an order that may be live has to
        keep holding its cash, or the next entry spends money this one is about to.
        """
        release = getattr(self.broker, "release_entry_margin_reservation", None)
        if not callable(release):
            return
        for reservation_id in list(reservation_ids or []):
            try:
                release(reservation_id)
            except Exception as exc:
                logger.debug(f"Margin reservation {reservation_id} release failed: {exc}")

    def _trade_owning_protective_order(self, order_id: str, exclude_id: str = ""):
        """The open trade that holds this protective order id, if any."""
        if not order_id:
            return None
        # Partially-constructed managers are a real pattern here (recovery paths and
        # tests), and an AttributeError would abort a flatten mid-way.
        with self._lock:
            for other in list(getattr(self, "open_trades", {}).values()):
                if other.id == exclude_id:
                    continue
                if str(getattr(other, "protective_order_id", "") or "") == str(order_id):
                    return other
        return None

    def _unowned_protective_stop_blocks_flatten(self, trade: Trade, reason: str) -> bool:
        """Deal with a working stop this trade has no order id for.

        Returns True when the flatten must be WITHHELD. Only a scan that definitively
        reports no stop, or one whose stop we successfully cancelled, may proceed —
        UNKNOWN/AMBIGUOUS/CONFLICT all mean we cannot prove the position is unprotected,
        and exiting on that basis is how a position gets closed twice.
        """
        inspector = getattr(self.broker, "inspect_working_protective_stop", None)
        if not callable(inspector):
            return False
        side = self._protective_exit_side(trade)
        try:
            found = inspector(
                trade.trading_symbol,
                trade.symbol_token,
                side,
                quantity=int(trade.broker_quantity or trade.quantity or 0),
                product_type=self.product_type,
            )
        except Exception as exc:
            return self._withhold_flatten(
                trade,
                f"{reason}; protective-stop scan failed ({exc}), market flatten withheld "
                "to avoid exiting over a stop that may be resting",
            )
        state = str((found or {}).get("state") or "UNKNOWN").upper()
        if state == "NONE":
            # Only a scan that actually cleared the contract resets the counter. Doing it
            # for FOUND too let an alternating UNKNOWN / FOUND-but-uncancellable sequence
            # withhold the flatten forever without ever reaching the escalation.
            self._clear_flatten_block(trade)
            return False
        if state == "FOUND":
            order_id = str((found or {}).get("order_id") or "")
            # Two trades on one strike is a supported configuration (same index, different
            # timeframes produce the same option symbol and the same exit side), and the
            # order-book scan matches on contract, not on trade. Cancelling a stop that
            # another OPEN trade is actively managing would strip its protection.
            owner = self._trade_owning_protective_order(order_id, exclude_id=trade.id)
            if owner is not None:
                return self._withhold_flatten(
                    trade,
                    f"{reason}; the only working stop on {trade.trading_symbol} belongs to "
                    f"trade {owner.id}, market flatten withheld to avoid stripping its protection",
                )
            try:
                cancelled = bool(order_id) and self.broker.cancel_order(order_id, "STOPLOSS")
            except (BrokerOrderRejected, PermissionError) as exc:
                # cancel_order takes the account lease and raises when another instance
                # holds it. Bare, this unwound out of check_session_end's per-trade loop
                # and aborted the EOD flatten for every remaining trade.
                return self._withhold_flatten(
                    trade, f"{reason}; orphan stop cancellation refused ({exc})"
                )
            if cancelled:
                logger.warning(
                    f"Cancelled orphan SL-M {order_id} for {trade.trading_symbol} before "
                    "flattening; the trade never held its order id."
                )
                return False
            return self._withhold_flatten(
                trade,
                f"{reason}; orphan protective stop {order_id or '(no id)'} could not be "
                "cancelled, market flatten withheld to avoid over-exit",
            )
        return self._withhold_flatten(
            trade,
            f"{reason}; protective-stop state is {state}, market flatten withheld to "
            "avoid double-exiting the position",
        )

    @staticmethod
    def _accepts_kwarg(func, name: str) -> bool:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return False
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
            return True
        return name in signature.parameters

    def _tag_kwargs(self, func, trade: Trade, leg: str, attempt: int = 0) -> Dict[str, str]:
        """`{"tag": ...}` only when the broker actually accepts it.

        SignalSimBroker and every test double predate the argument, and an unexpected
        keyword is a TypeError that aborts the whole order path — which is worse than
        going untagged. Tagging is an enhancement to recovery, so it must degrade to
        nothing rather than take the order down with it.
        """
        if not callable(func) or not self._accepts_kwarg(func, "tag"):
            return {}
        tag = self._broker_order_tag(trade, leg, attempt)
        return {"tag": tag} if tag else {}

    def _broker_order_tag(self, trade: Trade, leg: str, attempt: int = 0) -> str:
        """The exact tag this trade's `leg` order carries, or "" when unsupported."""
        tagger = getattr(self.broker, "order_tag", None)
        if not callable(tagger):
            return ""
        try:
            return str(tagger(trade.id, leg, attempt) or "")
        except TypeError:
            # Simulation brokers still on the single-argument form.
            try:
                return str(tagger(trade.id) or "")
            except Exception:
                return ""
        except Exception:
            return ""

    def _recover_order_id_from_tag(self, trade: Trade, leg: str) -> str:
        """Ask the broker whether this leg's tagged order actually reached the exchange.

        This is what a tag is FOR. Tags were already being written on entry orders and
        read by nothing, so a lost acknowledgement stayed unknowable and every caller had
        to latch and escalate to a human. The order book echoes the tag, so the question
        has a real answer: either an order carrying this exact tag exists or it does not.

        Returns the order id when exactly one match exists. Everything else returns "" --
        and the distinction matters to the caller, so AMBIGUOUS (two orders sharing a
        tag) escalates rather than being quietly treated as absent.
        """
        inspector = getattr(self.broker, "inspect_order_by_tag", None)
        if not callable(inspector):
            return ""
        attempt = (
            int(getattr(trade, "entry_replace_count", 0) or 0)
            if str(leg).upper() == "ENTRY"
            else 0
        )
        tag = self._broker_order_tag(trade, leg, attempt)
        if not tag:
            return ""
        try:
            result = inspector(tag) or {}
        except Exception as exc:
            logger.warning(f"Tagged {leg.lower()} recovery failed for {trade.id}: {exc}")
            return ""
        state = str(result.get("state") or "UNKNOWN").upper()
        if state == "AMBIGUOUS":
            self._mark_recovery(
                trade, f"Multiple broker orders share the exact {leg.lower()} tag {tag}"
            )
            return ""
        if state != "FOUND":
            return ""
        order_id = str(result.get("order_id") or "")
        if not order_id:
            self._mark_recovery(
                trade, f"Exact {leg.lower()} tag {tag} was found without an order id"
            )
        return order_id

    def _tagged_exit_state(self, trade: Trade) -> str:
        """FOUND / ABSENT / UNKNOWN for this trade's exit order, by tag."""
        inspector = getattr(self.broker, "inspect_order_by_tag", None)
        if not callable(inspector):
            return "UNKNOWN"
        tag = self._broker_order_tag(trade, "EXIT", 0)
        if not tag:
            return "UNKNOWN"
        try:
            return str((inspector(tag) or {}).get("state") or "UNKNOWN").upper()
        except Exception as exc:
            logger.warning(f"Tagged exit lookup failed for {trade.id}: {exc}")
            return "UNKNOWN"

    # The states a Kite order can finish in having NOT filled the rest of its quantity.
    # COMPLETE is deliberately absent: it means the full quantity did fill, so a residual
    # sitting alongside it is stale position data, and exiting on that would sell a
    # quantity that is no longer there and open a naked short.
    _DEAD_EXIT_STATES = {"CANCELLED", "REJECTED"}

    def _abandoned_exit_verdict(self, trade: Trade) -> str:
        """Can the exit order that partially filled still fill the remainder?

        "DEAD" only when the broker states a terminal non-fill, "WORKING" while it is
        still live, "UNKNOWN" when the question cannot be answered. UNKNOWN must never
        read as DEAD — sending a second exit on top of one that is still resting is how
        an intended exit becomes a reversed naked position.
        """
        order_id = str(getattr(trade, "exit_order_id", "") or "")
        if not order_id:
            # An exit was submitted (that is what set PARTIAL_EXIT) but we hold no id to
            # interrogate, so its fate is unknowable here.
            return "UNKNOWN"
        lifecycle = getattr(self.broker, "get_order_lifecycle", None)
        if not callable(lifecycle):
            return "UNKNOWN"
        try:
            detail = lifecycle(order_id) or {}
        except Exception as exc:
            logger.debug(f"Exit lifecycle lookup failed for {trade.id}: {exc}")
            return "UNKNOWN"
        state = str(detail.get("state") or "").upper()
        if not state or state == "UNKNOWN":
            return "UNKNOWN"
        return "DEAD" if state in self._DEAD_EXIT_STATES else "WORKING"

    def _resume_abandoned_partial_exit(self, trade: Trade, residual: int) -> bool:
        """Square off the remainder once the broker is finished with the exit order."""
        attempts = int(getattr(trade, "partial_exit_flatten_count", 0) or 0)
        if attempts >= 3:
            self._mark_recovery(
                trade,
                f"Partially filled exit left {residual} quantity and {attempts} re-exit "
                "attempts were all refused by the broker. This position is unprotected "
                "— reconcile it manually.",
            )
            return False
        trade.partial_exit_flatten_count = attempts + 1
        # The dead order cannot fill anything further, so its id must not be carried into
        # the new submission; emergency_flatten_trade stamps the replacement's own id.
        trade.exit_order_id = ""
        trade.pending_exit = False
        return self.emergency_flatten_trade(trade, "PARTIAL_EXIT_ABANDONED")

    def emergency_flatten_trade(self, trade: Trade, reason: str) -> bool:
        """Flatten a confirmed recovery position after safely cancelling its SL-M."""
        if self._live_account_send_blocked():
            # Reached mode-blind from check_session_end, risk_manager.halt_trading and
            # close_segment_trades; only _maybe_auto_flatten_unprotected checked mode.
            logger.error(
                f"Emergency flatten refused for {trade.trading_symbol}: mode is "
                f"{getattr(self, 'mode', '')!r} but the live account broker is still attached."
            )
            return False
        if not self.broker or not trade.symbol_token or not trade.trading_symbol:
            # There is no contract to submit against, so re-entering here every cycle
            # cannot ever succeed. check_session_end() calls this on every pass for as
            # long as the row stays RECOVERY_REQUIRED, which on 2026-07-31 turned into
            # CRITICAL "emergency flatten unavailable" for NIFTY and SENSEX every 30s
            # until the session ended. Latch it once and escalate to the operator: the
            # position may or may not exist at the broker, and only a human can settle
            # that — we must not invent a terminal state for an unknown position.
            if not hasattr(self, "_unflattenable_latched"):
                self._unflattenable_latched = set()
            if trade.id in self._unflattenable_latched:
                return False
            self._unflattenable_latched.add(trade.id)
            self._mark_recovery(
                trade,
                f"{reason}; emergency flatten unavailable (no broker contract on the "
                "trade). Reconcile this position manually at your Kite terminal — "
                "the system cannot act on it and will stop retrying.",
            )
            return False
        if trade.id in getattr(self, "_flatten_ambiguous_latched", set()):
            # A previous exit for this trade was never acknowledged. Sending another is
            # how one ambiguous exit becomes two real ones.
            return False
        if trade.protective_order_id:
            # Initialised BEFORE the try. `cancelled` used to be bound only by the call
            # itself, so the refused-cancel path below fell through to `if not cancelled`
            # with the name unbound and raised UnboundLocalError. check_session_end has no
            # per-trade try, so that escaped the 15:25 force exit and took every remaining
            # trade in the snapshot with it — and the orphan sweep after it. Reproduced.
            cancelled = False
            refused = False
            try:
                cancelled = bool(
                    self.broker.cancel_order(trade.protective_order_id, "STOPLOSS")
                )
            except (BrokerOrderRejected, PermissionError) as exc:
                # cancel_order takes the account lease and raises when it is held
                # elsewhere. ensure_broker_protection was hardened against exactly this;
                # this path was not.
                # _withhold_flatten returns False once the operator's blocked-flatten
                # deadline elapses ("stop withholding"). Discarding that logged
                # "exiting anyway", set reversal_watch_required — and then did not exit.
                refused = True
                if self._withhold_flatten(
                    trade, f"{reason}; protective stop cancellation refused ({exc})"
                ):
                    return False
            if not cancelled and not refused:
                # A refusal has already been through _withhold_flatten once; running it
                # again here would double-count the block deadline for one failure.
                if self._withhold_flatten(
                    trade,
                    f"{reason}; protective stop cancellation unconfirmed, market flatten "
                    "withheld to avoid over-exit",
                ):
                    return False
            if cancelled:
                trade.protective_order_id = ""
            else:
                # Past the deadline we flatten anyway, but the stop was never PROVEN gone.
                # Clearing the id here is how an SL-M ends up owned by nobody: nothing
                # scans closed trades for it, so when it later triggers it sells against a
                # flat book — a naked written option in a product the broker will not
                # square off. Keep the id and say so.
                self._mark_recovery(
                    trade,
                    f"{reason}; flattened past the block deadline with protective stop "
                    f"{trade.protective_order_id} UNCONFIRMED. It may still be resting — "
                    "cancel it at the Zerodha terminal before it triggers against a flat "
                    "book.",
                )
        elif self._unowned_protective_stop_blocks_flatten(trade, reason):
            # Holding no stop id is NOT proof that no stop exists. A lost acknowledgement
            # leaves an SL-M resting that we never learned the id of; market-exiting on
            # top of it flattens the position while the stop is still live, and when the
            # stop later triggers it opens a naked reversed leg.
            return False
        # Same rule for the server-side trigger: a GTT that survives this flatten will
        # fire later against a flat book and open a naked reversed leg.
        if not self._release_gtt_fallback(trade, "flattening the position"):
            if self._withhold_flatten(
                trade,
                f"{reason}; GTT cancellation unconfirmed, market flatten withheld to avoid over-exit",
            ):
                return False
        # NEVER size a flatten from the INTENDED quantity. broker_quantity is 0 on every
        # row whose entry the broker did not confirm: an ambiguous acknowledgement, an
        # entry not found in the order book within the timeout, a stale entry that could
        # not be safely cancelled, and every prior-session row on restart. check_session_end
        # routes all of those straight here.
        #
        # Options are always BOUGHT, so the flatten side is SELL. Selling a fill that never
        # happened does not close anything — it OPENS a naked written option in
        # CARRYFORWARD/NRML, the product chosen precisely so the broker will not square it
        # off, at 15:25 with no session left to correct it. The sibling
        # _maybe_auto_flatten_unprotected already refuses on exactly this test.
        qty = self._confirmed_broker_quantity(trade)
        if qty <= 0:
            # Ask the book once before concluding anything. A real position we simply
            # failed to record is still worth flattening; a flat book is nothing to do;
            # an unreadable book is NOT flat and must not be guessed at.
            row, readable = self._live_position_for_trade(trade)
            if not readable:
                self._mark_recovery(
                    trade,
                    f"{reason}; the broker never confirmed a fill and the positions book "
                    "is unreadable, so the size to flatten is unknown. NOT sending an "
                    "order — a market exit here could open a naked short. Reconcile this "
                    "position manually at your Kite terminal.",
                )
                return False
            try:
                qty = abs(int(float((row or {}).get("netqty") or 0)))
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                logger.info(
                    f"Flatten skipped for {trade.trading_symbol}: the broker never "
                    "confirmed a fill and the book shows flat, so there is nothing to "
                    "square off."
                )
                return False
        side = self._protective_exit_side(trade)
        try:
            order_id = self.broker.close_order(
                trade.trading_symbol, trade.symbol_token, qty, side, self.product_type,
                **self._tag_kwargs(self.broker.close_order, trade, "EXIT"),
            )
        except (BrokerOrderRejected, PermissionError) as exc:
            # close_order takes the account lease and needs a session, so it raises just
            # like the cancels above. Bare, it unwound out of check_session_end's
            # per-trade loop and aborted the EOD flatten for every remaining trade.
            self._withhold_flatten(trade, f"{reason}; emergency flatten refused ({exc})")
            return False
        except BrokerOrderAmbiguous as exc:
            # The exit MAY have reached the exchange, so it must never be re-sent blind.
            # Marking recovery alone did NOT achieve that: check_session_end re-dispatches
            # every RECOVERY_REQUIRED row to this method on each pass (~1 Hz), so a fresh
            # MARKET exit went out every second until the close. If the first one had in
            # fact filled, the account ends up SHORT and naked in an NRML product the
            # broker will not square off. Latch it: only a reconciliation that reads the
            # real net quantity may release it.
            #
            # But "may be live" is now a question with a real answer. The exit carries an
            # exact tag, so the order book can be asked directly instead of freezing and
            # escalating to a human:
            #   FOUND  -> it DID reach the exchange. Adopt the id and treat it as the
            #             submission it actually is; nothing to latch and nobody to call.
            #   ABSENT -> a SUCCESSFUL read that found no such tag proves it never
            #             arrived, so the next pass may retry safely. Still recovery, but
            #             not latched — latching here is what strands a live position.
            # Only UNKNOWN (unreadable book) and AMBIGUOUS fall through to the latch,
            # which is the honest outcome when the question genuinely cannot be answered.
            recovered_exit = self._recover_order_id_from_tag(trade, "EXIT")
            if recovered_exit:
                trade.exit_order_id = recovered_exit
                trade.pending_exit = True
                trade.pending_execution = False
                trade.execution_status = "EXIT_PENDING"
                trade.execution_error = reason
                trade.exit_reason = "RECOVERY_AUTO_FLATTEN"
                self._clear_flatten_block(trade)
                self.save_state()
                logger.warning(
                    f"Flatten acknowledgement was lost for {trade.trading_symbol}, but "
                    f"its exit tag resolved to order {recovered_exit} — adopted instead "
                    "of latching."
                )
                return True
            if self._tagged_exit_state(trade) == "ABSENT":
                self._mark_recovery(
                    trade,
                    f"{reason}; flatten acknowledgement was lost ({exc}), but the exit "
                    "tag is absent from the order book, so nothing reached the exchange. "
                    "A retry is safe.",
                )
                self.save_state()
                return False
            if not hasattr(self, "_flatten_ambiguous_latched"):
                self._flatten_ambiguous_latched = set()
            self._flatten_ambiguous_latched.add(trade.id)
            self._mark_recovery(
                trade,
                f"{reason}; emergency flatten acknowledgement ambiguous ({exc}). The exit "
                "may be live — no further exit will be sent until broker positions "
                "confirm the real quantity. Reconcile this position manually.",
            )
            logger.critical(
                f"FLATTEN AMBIGUOUS for {trade.trading_symbol}: an exit may already be "
                "resting at the exchange. Further automatic exits are latched off for "
                "this trade until reconciliation proves its net quantity."
            )
            self.save_state()
            return False
        if not order_id:
            self._withhold_flatten(trade, f"{reason}; emergency flatten order failed")
            return False
        trade.exit_order_id = str(order_id)
        trade.pending_exit = True
        trade.pending_execution = False
        trade.execution_status = "EXIT_PENDING"
        trade.execution_error = reason
        trade.exit_reason = "RECOVERY_AUTO_FLATTEN"
        self._clear_flatten_block(trade)
        self.save_state()
        logger.critical(f"Recovery fallback flatten submitted for {trade.trading_symbol}: {order_id}")
        return True

    def _contract_is_no_longer_listed(self, trade: Trade) -> Optional[bool]:
        """Is this trade's contract absent from the current instrument master?

        None when the question cannot be answered — the master is not loaded, or the
        lookup itself failed. The caller must treat None as "unknown", never as "gone":
        concluding a contract has expired from a master we could not read is the same
        mistake as concluding a position is flat from an order-book scan that errored.
        """
        try:
            from engine.expiry_manager import expiry_manager
        except Exception:
            return None
        df = getattr(expiry_manager, "df", None)
        if df is None or getattr(df, "empty", True):
            return None
        symbol = str(getattr(trade, "trading_symbol", "") or "").upper()
        token = str(getattr(trade, "symbol_token", "") or "")
        if not symbol and not token:
            return None
        try:
            if token and (df["token"].astype(str) == token).any():
                return False
            if symbol and (df["symbol"].astype(str).str.upper() == symbol).any():
                return False
        except Exception:
            return None
        return True

    def _settle_expired_contract(
        self, trade_id: str, trade: Trade, now: Optional[datetime] = None
    ) -> bool:
        """Close a row whose option contract has expired and left the broker's book.

        An expired contract never returns to the positions book, so reconciliation can
        never close this row on its own: it sat OPEN at a stale premium indefinitely,
        counted against exposure and position slots, and emergency_flatten_trade kept
        submitting exits against a contract that no longer trades.

        The settlement VALUE is genuinely unknowable here — the contract cash-settles at
        intrinsic against the closing spot, and by the time we notice, that spot is not
        something this object can reconstruct. So it books the conservative case (expired
        worthless, the worst outcome for a bought option, which can never overstate the
        account), marks the P&L as estimated, and escalates. Options are always BOUGHT
        here, so this cannot hide a liability — only understate a gain.
        """
        if str(getattr(trade, "inst_type", "") or "").upper() != "OPT":
            return False
        entry = getattr(trade, "entry_time", None)
        if entry is None:
            return False
        entry_ist = IST.localize(entry) if entry.tzinfo is None else entry.astimezone(IST)
        # Injectable so the session-boundary rule below can be tested at a fixed point.
        # Reading the wall clock unconditionally made this method behave differently
        # depending on when the process ran, and its test passed before 15:35 and failed
        # after — which is exactly how it broke, at 21:23.
        now = now or datetime.now(IST)
        if now.tzinfo is None:
            now = IST.localize(now)
        # Never intraday on the entry's own session: a contract missing from one
        # positions read is far more likely to be a slow book than an expired contract.
        if entry_ist.date() >= now.date() and now.time() < self._parse_time("15:35"):
            return False
        if self._contract_is_no_longer_listed(trade) is not True:
            return False

        trade.status = "CLOSED"
        trade.pending_exit = False
        trade.pending_execution = False
        trade.execution_status = "CLOSED"
        trade.exit_reason = "EXPIRY_SETTLED"
        trade.exit_time = now
        trade.exit_price = 0.0
        trade.execution_error = (
            "Contract expired and was cash-settled at the exchange; it is gone from the "
            "instrument master and will never return to the positions book. Booked as "
            "expired worthless — the P&L here is an ESTIMATE and must be verified against "
            "the broker's own statement."
        )
        trade.protective_order_id = ""
        trade.gtt_trigger_id = ""
        with self._lock:
            if self.open_trades.pop(trade_id, None) is trade:
                self.closed_trades.append(trade)
        logger.critical(
            f"EXPIRY SETTLED: {trade.trading_symbol} left open through expiry and has "
            "been closed locally as worthless. Verify the realised amount against your "
            "Zerodha P&L — if it settled in the money, this row understates it."
        )
        self.save_state()
        return True

    def reconcile_broker_positions(self, positions_resp: Optional[Dict] = None) -> Dict:
        """Reconcile local trades and broker-only positions in both directions."""
        if not self.broker:
            return {"checked": False, "orphans": 0, "closed": 0, "confirmed": 0}
        get_positions = getattr(self.broker, "get_positions", None)
        if positions_resp is None:
            if not callable(get_positions):
                return {"checked": False, "orphans": 0, "closed": 0, "confirmed": 0}
            positions_resp = get_positions()
        if not isinstance(positions_resp, dict):
            return {"checked": False, "orphans": 0, "closed": 0, "confirmed": 0}
        rows = positions_resp.get("data", [])
        if not isinstance(rows, list):
            return {"checked": False, "orphans": 0, "closed": 0, "confirmed": 0}

        allowed_prefixes = self._reconciliation_symbol_prefixes()
        broker_pos = {
            str(row.get("tradingsymbol") or ""): row
            for row in rows
            if str(row.get("tradingsymbol") or "") and str(row.get("tradingsymbol") or "").upper().startswith(allowed_prefixes)
        }
        closed = 0
        confirmed = 0
        changed = False
        with self._lock:
            local_snapshot = list(self.open_trades.items())

        # One position row can back several local trades on the same contract, so the
        # row's net quantity is split ONCE here rather than handed to each of them.
        # See _allocate_broker_quantity: with a single trade on the symbol this is the
        # net verbatim and nothing downstream changes.
        symbol_groups: Dict[str, list] = {}
        for _tid, _trade in local_snapshot:
            _symbol = str(_trade.trading_symbol or _trade.instrument)
            symbol_groups.setdefault(_symbol, []).append((_tid, _trade))
        broker_allocation: Dict[str, int] = {}
        for _symbol, _group in symbol_groups.items():
            _row = broker_pos.get(_symbol)
            if _row is None:
                continue
            broker_allocation.update(
                self._allocate_broker_quantity(_group, self._position_quantity(_row))
            )

        local_symbols = set()
        for trade_id, trade in local_snapshot:
            symbol = str(trade.trading_symbol or trade.instrument)
            local_symbols.add(symbol)
            position = broker_pos.get(symbol)
            # Kite keeps a squared-off day position in `net` with quantity 0 for the rest
            # of the session. For a trade whose entry has NOT been confirmed filled, that
            # row is the residue of an EARLIER trade on this contract and says nothing
            # about this order. Reading it as "the position went flat" closed a trade
            # whose entry was still working at the exchange: it booked the earlier
            # trade's realised P&L onto the new trade (feeding the daily loss breaker),
            # threw away the live order id, and left a real position with no stop that
            # even the EOD force-exit could not see.
            #
            # Treat it as "no position" so the stale-entry logic below handles it — that
            # branch already consults the order book, cancel/replaces a stale working
            # order, and marks RECOVERY_REQUIRED past the timeout.
            if (
                position is not None
                and self._position_quantity(position) == 0
                and self._entry_still_confirming(trade)
                and self._confirmed_broker_quantity(trade) <= 0
            ):
                position = None
            order_status = None
            if trade.execution_status == "ENTRY_SUBMITTED" and trade.broker_order_id:
                getter = getattr(self.broker, "get_order_status", None)
                if callable(getter):
                    order_status = getter(trade.broker_order_id)
            if position is None:
                # Before anything else: if the contract itself has expired, no future
                # reconcile can ever resolve this row. Only the ENTRY_SUBMITTED case was
                # handled below, so every other status fell through to the bare `continue`
                # at the end of this branch and the trade stayed OPEN forever.
                if self._settle_expired_contract(trade_id, trade):
                    closed += 1
                    changed = True
                    continue
                if (
                    trade.execution_status == "ENTRY_SUBMITTED"
                    and trade.entry_submitted_at is not None
                ):
                    submitted_at = trade.entry_submitted_at
                    if submitted_at.tzinfo is None:
                        submitted_at = IST.localize(submitted_at)
                    age_seconds = (
                        datetime.now(IST) - submitted_at.astimezone(IST)
                    ).total_seconds()
                    status_text = self._order_status_text(order_status)
                    if status_text in {"rejected", "cancelled", "canceled"}:
                        # A TERMINAL status is not the same as UNFILLED. Kite reports an
                        # order that partially executed and was then cancelled as
                        # CANCELLED with a non-zero filled quantity, and this branch used
                        # to read the status STRING alone — booking the trade CLOSED at
                        # pnl 0 and popping it out of open_trades while that quantity sat
                        # live at the broker with no protective stop and no local row.
                        # The positions snapshot is taken once at the top of the pass
                        # while get_order_status is called live per trade, so `position
                        # is None` here does NOT mean flat.
                        filled_qty = order_filled_quantity(order_status)
                        if filled_qty > 0:
                            trade.broker_quantity = int(filled_qty)
                            trade.execution_status = "PARTIAL"
                            trade.pending_execution = True
                            trade.execution_error = (
                                f"{status_text} after filling {int(filled_qty)}; "
                                "position is live at the broker"
                            )
                            logger.critical(
                                f"[PARTIAL FILL ON {status_text.upper()}] {trade.id} "
                                f"{trade.trading_symbol}: {int(filled_qty)} filled and "
                                "live. Arming protection instead of closing the row."
                            )
                            try:
                                self.ensure_broker_protection(trade, int(filled_qty))
                            except Exception as exc:
                                logger.error(
                                    f"Protection failed for partial fill on {trade.id}: {exc}"
                                )
                                self._mark_recovery(
                                    trade,
                                    f"{int(filled_qty)} filled on a {status_text} order "
                                    "and the protective stop could not be armed.",
                                )
                            changed = True
                            continue
                        trade.status = "CLOSED"
                        trade.pending_execution = False
                        trade.execution_status = "REJECTED"
                        trade.execution_error = (
                            str((order_status or {}).get("text") or status_text)
                        )
                        trade.exit_reason = "BROKER_REJECTED"
                        trade.exit_time = datetime.now(IST)
                        trade.exit_price = trade.entry_price
                        with self._lock:
                            if self.open_trades.pop(trade_id, None) is trade:
                                self.closed_trades.append(trade)
                        changed = True
                        continue

                    from config.settings import get_settings
                    settings = get_settings()
                    timeout = max(
                        2.0,
                        float(getattr(settings, "broker_fill_confirmation_timeout_seconds", 12.0) or 12.0),
                    )
                    working = status_text in {
                        "open", "pending", "put order req received", "validation pending",
                        "after market order req received", "modify pending",
                    }
                    if (
                        working
                        and age_seconds >= timeout
                        and trade.entry_replace_count < 1
                        and _setting_enabled(getattr(settings, "broker_cancel_replace_enabled", True), True)
                        # Reconcile runs from the PositionSync daemon every ~5s in EVERY
                        # mode; this branch cancels a resting entry and MARKET-replaces
                        # it. The read half above must keep running, so the gate is here
                        # rather than at the top of the pass.
                        and not self._live_account_send_blocked()
                    ):
                        # PROVE the cancel, do not just acknowledge it. cancel_order
                        # returns True on the API acknowledgement alone, and this order is
                        # being replaced precisely BECAUSE price is moving toward its
                        # limit — so the fill routinely wins the race against the cancel.
                        # Acting on the ack then leaves the original filled AND a MARKET
                        # replacement live: double the intended size, with the stop later
                        # armed for the doubled quantity, so one stop-out costs twice the
                        # per-trade risk the sizer approved.
                        #
                        # cancel_order_confirmed returns True only for a broker-confirmed
                        # terminal non-fill, False when the order actually filled, and
                        # None when it cannot tell. It has existed unused since the
                        # AngelOne era with exactly this docstring.
                        confirm = getattr(self.broker, "cancel_order_confirmed", None)
                        if callable(confirm):
                            try:
                                cancelled = confirm(trade.broker_order_id, "NORMAL")
                            except (BrokerOrderRejected, PermissionError) as exc:
                                logger.error(
                                    f"Stale-entry cancel refused for {trade.trading_symbol}: {exc}"
                                )
                                cancelled = None
                        else:
                            # Guarded like its sibling above. Only a double without
                            # cancel_order_confirmed reaches this, but an escape here
                            # unwinds out of the reconcile loop and abandons every
                            # remaining trade in the snapshot — the same shape that took
                            # out the force exit.
                            try:
                                cancelled = self.broker.cancel_order(
                                    trade.broker_order_id, "NORMAL"
                                )
                            except (BrokerOrderRejected, PermissionError) as exc:
                                logger.error(
                                    f"Stale-entry cancel refused for {trade.trading_symbol}: {exc}"
                                )
                                cancelled = None
                        if cancelled is True and not self._stale_entry_still_worth_filling(trade):
                            # The cancel is PROVEN, so nothing is resting and nothing
                            # will fill. Replacing it with a MARKET order here would
                            # guarantee entry into a setup that has already failed.
                            self._abandon_invalidated_entry(trade)
                            changed = True
                            continue
                        if cancelled is True:
                            entry_side = (
                                "BUY"
                                if trade.inst_type == "OPT" or trade.direction in {"LONG", "BUY"}
                                else "SELL"
                            )
                            try:
                                replacement = self.broker.place_order(
                                    trade.trading_symbol,
                                    trade.symbol_token,
                                    trade.quantity,
                                    entry_side,
                                    self.product_type,
                                    price=0.0,
                                    # The one order path that went out untagged. Every
                                    # other leg carries one -- protective, ADD, EXIT --
                                    # and the entry tag is written inside the broker.
                                    # A replacement is the leg MOST likely to need it:
                                    # it exists because the first acknowledgement was
                                    # already unreliable, and without a tag a lost ack
                                    # on the replacement can only be resolved by
                                    # contract shape, which cannot tell two trades on
                                    # one strike apart. attempt distinguishes it from
                                    # the original entry's tag.
                                    **self._tag_kwargs(
                                        self.broker.place_order,
                                        trade,
                                        "ENTRY",
                                        attempt=int(getattr(trade, "entry_replace_count", 0) or 0) + 1,
                                    ),
                                )
                            except (BrokerOrderAmbiguous, BrokerOrderRejected, PermissionError) as exc:
                                # Unguarded, this unwound out of reconcile and abandoned
                                # every remaining trade in the snapshot.
                                logger.error(
                                    f"Stale-entry replacement failed for {trade.trading_symbol}: {exc}"
                                )
                                replacement = None
                            if replacement:
                                trade.broker_order_id = str(replacement)
                                trade.entry_submitted_at = datetime.now(IST)
                                trade.entry_replace_count += 1
                                trade.execution_error = "Stale limit cancelled and replaced with MARKET"
                                changed = True
                                continue
                        if cancelled is False:
                            # The order FILLED while we were cancelling. Nothing is
                            # stale and nothing needs replacing — let the next reconcile
                            # confirm the fill normally.
                            trade.pending_execution = True
                            changed = True
                            continue
                        self._mark_recovery(trade, "Stale entry could not be safely cancelled/replaced")
                        trade.pending_execution = True
                        changed = True
                    elif age_seconds >= timeout * 2.0:
                        self._mark_recovery(
                            trade,
                            f"Submitted entry was not confirmed by order book or positions within {timeout * 2.0:.0f}s",
                        )
                        trade.pending_execution = True
                        changed = True
                continue
            net_qty = self._position_quantity(position)
            # This trade's SHARE of the contract's position, not the whole row.
            allocated_qty = broker_allocation.get(str(trade_id), abs(net_qty))
            expected_sign = self._expected_position_sign(trade)
            if net_qty and (1 if net_qty > 0 else -1) != expected_sign:
                trade.broker_quantity = allocated_qty
                trade.pending_execution = True
                self._mark_recovery(
                    trade,
                    f"Broker position direction mismatch: netqty={net_qty}, "
                    f"expected {'LONG' if expected_sign > 0 else 'SHORT'}",
                )
                changed = True
                continue
            # A position row IS the reconciliation an ambiguous flatten was waiting for:
            # the broker has told us the real net quantity, so automatic exits may resume.
            latched = getattr(self, "_flatten_ambiguous_latched", None)
            if latched:
                latched.discard(trade_id)
            trade.broker_quantity = allocated_qty
            expected_qty = max(1, trade.quantity)
            if net_qty == 0:
                if trade.protective_order_id:
                    # This call used to be bare, and both halves of that were wrong.
                    #
                    # Unguarded: cancel_order takes the account lease and RAISES when a
                    # second instance holds it, and there is no per-trade try in this
                    # loop — so one throttled contract aborted the whole reconcile pass,
                    # skipping every later trade AND the orphan-detection block below,
                    # then did it again on every 5s tick for as long as the lease was held.
                    #
                    # Result discarded: cancel_order returns False on refusal, and the
                    # next line cleared the id and closed the row anyway. The SL-M is then
                    # owned by nobody — _trade_owning_protective_order and
                    # _unowned_protective_stop_blocks_flatten only scan open_trades — so
                    # when the premium later ticks through the trigger it SELLS against a
                    # flat book: a naked written option in a product the broker will not
                    # square off.
                    stop_id = str(trade.protective_order_id)
                    cleared = False
                    try:
                        cleared = bool(
                            not self._live_account_send_blocked()
                            and self.broker.cancel_order(stop_id, "STOPLOSS")
                        )
                    except (BrokerOrderRejected, PermissionError) as exc:
                        logger.error(
                            f"Protective stop {stop_id} cancel refused while closing "
                            f"{trade.trading_symbol}: {exc}"
                        )
                    if cleared:
                        trade.protective_order_id = ""
                    else:
                        self._alert_unowned_protective_stop(trade, stop_id)
                # A GTT that survives this close fires later against a flat book with the
                # same outcome. The flatten path releases it; this one never did.
                if str(getattr(trade, "gtt_trigger_id", "") or ""):
                    try:
                        self._release_gtt_fallback(trade, "closing a flat position")
                    except Exception as exc:
                        logger.error(
                            f"GTT release failed while closing {trade.trading_symbol}: {exc}"
                        )
                reason = (
                    trade.exit_reason
                    if trade.execution_status == "EXIT_PENDING" and trade.exit_reason
                    else "MANUAL_SYNC"
                )
                realised = position.get("realised")
                try:
                    realised_pnl = float(realised) if realised not in (None, "") else None
                except (TypeError, ValueError):
                    realised_pnl = None
                if self._finalize_closed_trade(
                    trade_id,
                    trade,
                    self._position_exit_price(trade, position),
                    reason,
                    realised_pnl=realised_pnl,
                ):
                    closed += 1
                continue
            if (
                trade.execution_status in {
                    "ENTRY_PENDING",
                    "ENTRY_SUBMITTED",
                    "PARTIAL",
                    "RECOVERY_REQUIRED",
                }
                and not trade.pending_exit
            ):
                # THIS trade's share of the row, not the contract's total. The aggregate
                # here did more than mis-size the stop below: filled_qty also decides
                # pending_execution and whether the row is OPEN or PARTIAL, so three
                # fully-filled trades on one strike each saw 195 against an expected 65
                # and were all marked PARTIAL.
                filled_qty = allocated_qty
                trade.pending_execution = filled_qty != expected_qty
                trade.execution_status = "OPEN" if filled_qty == expected_qty else "PARTIAL"
                
                # ACTUAL BROKER FILL PRICE SYNC
                # Prefer this order's own executions over the position average: the
                # position blends every trade on the contract, so a second entry on the
                # same strike silently rewrites the first one's entry price. Only a
                # FILLED answer is used — UNKNOWN falls through to the position figure
                # rather than being treated as "no fill".
                fill_price = 0.0
                filler = getattr(self.broker, "get_fill", None)
                if callable(filler) and getattr(trade, "broker_order_id", ""):
                    try:
                        detail = filler(str(trade.broker_order_id))
                        if str(detail.get("state") or "") == "FILLED":
                            fill_price = float(detail.get("average_price") or 0.0)
                    except Exception as exc:
                        logger.debug(f"Order-level fill lookup failed for {trade.id}: {exc}")
                if fill_price <= 0.1:
                    fill_price = float(position.get("buyavgprice", 0.0) if trade.direction in {"LONG", "BUY"} or trade.inst_type == "OPT" else position.get("sellavgprice", 0.0))
                if fill_price > 0.1:
                    if trade.inst_type != "OPT" or self._option_premium_quote_plausible(
                        trade,
                        fill_price,
                        getattr(trade, "entry_spot", 0.0),
                    ):
                        self._record_entry_slippage(trade, fill_price)
                        trade.entry_price = fill_price
                    else:
                        logger.warning(
                            f"Ignored implausible broker fill {fill_price:.2f} "
                            f"for {trade.trading_symbol or trade.instrument}; "
                            f"kept local entry {trade.entry_price:.2f}"
                        )
                
                trade.execution_error = (
                    ""
                    if filled_qty == expected_qty
                    else f"Broker entry partially filled; {filled_qty} of {expected_qty} quantity confirmed"
                )
                confirmed += 1
                changed = True
                protected = self.ensure_broker_protection(trade, filled_qty)
                if not protected:
                    self._maybe_auto_flatten_unprotected(
                        trade,
                        "Broker protection unavailable",
                    )
                if filled_qty < expected_qty:
                    self._withdraw_stale_entry_remainder(trade, filled_qty, expected_qty)
            elif (
                trade.execution_status in {"EXIT_PENDING", "PARTIAL_EXIT"}
                and allocated_qty < expected_qty
            ):
                trade.pending_exit = True
                trade.execution_status = "PARTIAL_EXIT"
                trade.execution_error = (
                    f"Broker exit partially filled; {allocated_qty} of "
                    f"{expected_qty} quantity remains; awaiting broker completion"
                )
                changed = True
                # PARTIAL_EXIT used to be re-examined by NOTHING until the 15:25 force
                # exit: EXIT_PENDING was the only status this branch accepted, and the
                # branch below excludes PARTIAL_EXIT explicitly. So the residual sat for
                # the rest of the session with its protective stop already cancelled
                # (cancelling it is what precedes sending an exit at all).
                #
                # "Awaiting broker completion" is the right posture only while the exit
                # order can still fill the rest. Once the broker says that order is
                # CANCELLED or REJECTED, nothing is coming: the remainder is a live
                # position with no stop, no working exit, and nothing scheduled to touch
                # it before the close. That is the moment to square it off, and it is a
                # fact the broker can state rather than something to guess at.
                verdict = self._abandoned_exit_verdict(trade)
                if verdict == "DEAD":
                    self._resume_abandoned_partial_exit(trade, allocated_qty)
                elif verdict == "UNKNOWN":
                    if not hasattr(self, "_partial_exit_unknown_logged"):
                        self._partial_exit_unknown_logged = set()
                    if trade.id not in self._partial_exit_unknown_logged:
                        self._partial_exit_unknown_logged.add(trade.id)
                        logger.warning(
                            f"Cannot determine whether the exit order for "
                            f"{trade.trading_symbol} is still working; leaving "
                            f"{allocated_qty} quantity in place rather than risking a "
                            "double exit. The force-exit remains the backstop."
                        )
            elif (
                allocated_qty != expected_qty
                and trade.execution_status not in {"EXIT_PENDING", "PARTIAL_EXIT"}
            ):
                trade.execution_status = "PARTIAL"
                self.ensure_broker_protection(trade, allocated_qty)
                changed = True

        orphans = {}
        for symbol, position in broker_pos.items():
            if self._position_quantity(position) != 0 and symbol not in local_symbols:
                orphan = dict(position)
                orphan["execution_status"] = "ORPHANED"
                orphan["status"] = "ORPHANED"
                orphans[symbol] = orphan
        if orphans != self.orphan_positions:
            self.orphan_positions = orphans
            changed = True
            if orphans:
                logger.critical(
                    "Broker-only positions require reconciliation: "
                    + ", ".join(sorted(orphans))
                )
        if changed:
            self.save_state()
        return {
            "checked": True,
            "orphans": len(orphans),
            "closed": closed,
            "confirmed": confirmed,
        }

    def update_risk_settings(self, futures_sl_pct: float = None, options_sl_pct: float = None):
        """Update settings and re-calibrate open trades if needed"""
        if futures_sl_pct is not None:
            self.futures_sl_pct = futures_sl_pct
        if options_sl_pct is not None:
            self.options_sl_pct = options_sl_pct
            
        # Optional: Sync open trades to new settings (Aggressive approach)
        # For now, we just update the manager state so NEW trades get the correct SL.
        # If we want to move EXISTING stops, we would loop through self.open_trades here.
        logger.info(f"🛡️ Risk Settings Synced: FUT SL={self.futures_sl_pct}%, OPT SL={self.options_sl_pct}%")

    def save_state(self):
        """Save active and recent closed trades to SQLite for crash recovery"""
        try:
            with self._lock:
                open_tr = list(self.open_trades.values())
                self.closed_trades = self.closed_trades[-500:]
                closed = list(self.closed_trades)
                snapshot = [t.to_dict() for t in open_tr + closed]
                db.save_trades(snapshot)
        except Exception as e:
            logger.error(f"Failed to save trade state to DB: {e}")

    def load_state(self):
        """Load state from SQLite on startup"""
        try:
            open_records = db.load_open_trades()
            closed_records = db.load_closed_trades()
            
            stale_ghosts = 0
            auto_closed_missing_contracts = 0
            for data in open_records:
                tid = data.get("id", "")
                if not tid: continue
                t = Trade.from_dict(data)
                t.status = "OPEN"
                # A source candle that has already closed cannot repaint, so the guard is
                # satisfied by definition. Rows written before repaint_guard_checked was
                # persisted carry False, and the async repaint tracker is started from
                # exactly one place — the entry acknowledgement — with no rehydration
                # path, so without this a restarted trade would sit "structurally
                # unconfirmed" for the rest of the session: let-winners-run off and the
                # blunt sub-1R give-back guard armed on a position whose candle closed
                # hours ago.
                if not t.repaint_guard_checked and t.entered_intracandle:
                    try:
                        minutes = {"1min": 1, "5min": 5, "15min": 15}.get(
                            str(getattr(t, "timeframe", "") or "").lower(), 5
                        )
                        entry_time = t.entry_time
                        if entry_time.tzinfo is None:
                            entry_time = IST.localize(entry_time)
                        if datetime.now(IST) >= entry_time + timedelta(minutes=minutes):
                            t.repaint_guard_checked = True
                    except (AttributeError, TypeError, ValueError):
                        pass
                symbol = str(getattr(t, "trading_symbol", "") or "").strip()
                token = str(getattr(t, "symbol_token", "") or "").strip()
                broker_order_id = str(getattr(t, "broker_order_id", "") or "").strip()
                unusable_contract = (
                    not bool(getattr(t, "is_ghost", False))
                    and not broker_order_id
                    and (not symbol or not token)
                )
                if t.entry_time.date() < datetime.now(IST).date():
                    # Ghost/shadow rows (engine-lab, tests) never had broker
                    # orders — there is nothing to reconcile. Retaining them as
                    # RECOVERY_REQUIRED spammed CRITICAL alerts + strategy-exit
                    # loops on every restart (2026-07-10: 111 stale ghosts).
                    if bool(getattr(t, "is_ghost", False)):
                        t.status = "CLOSED"
                        t.pending_exit = False
                        t.execution_status = "CLOSED"
                        t.exit_reason = getattr(t, "exit_reason", "") or "STALE_SESSION_END"
                        t.exit_time = getattr(t, "exit_time", None) or t.entry_time
                        t.exit_price = float(
                            getattr(t, "exit_price", 0.0)
                            or getattr(t, "current_price", 0.0)
                            or getattr(t, "entry_price", 0.0)
                            or 0.0
                        )
                        self.closed_trades.append(t)
                        stale_ghosts += 1
                        continue
                    t.execution_status = "RECOVERY_REQUIRED"
                    t.execution_error = "Previous-session trade requires broker reconciliation"
                    logger.critical(f"Previous-session active trade retained for reconciliation: {tid}")
                elif unusable_contract:
                    # Same-session row the broker never acknowledged: no order id AND no
                    # contract to act on, so there is no position to reconcile and nothing
                    # emergency_flatten_trade() could ever submit. Left open it becomes the
                    # 2026-07-31 EOD loop — FORCE_EOD_KILL retrying "emergency flatten
                    # unavailable" every cycle until the session ends.
                    #
                    # Deliberately restricted to same-session rows. A trade that survived a
                    # day boundary keeps the reconciliation path above even when its
                    # metadata looks unusable, because discarding a position that may be
                    # live at the broker is far worse than one noisy alert.
                    t.status = "CLOSED"
                    t.pending_exit = False
                    t.pending_execution = False
                    t.execution_status = "REJECTED"
                    t.execution_error = (
                        "Trade discarded: broker contract metadata missing (trading_symbol/symbol_token)"
                    )
                    t.exit_reason = "CONTRACT_MISSING"
                    t.exit_time = datetime.now(IST)
                    t.exit_price = float(getattr(t, "entry_price", 0.0) or 0.0)
                    self.closed_trades.append(t)
                    auto_closed_missing_contracts += 1
                    continue
                elif not t.execution_status:
                    t.execution_status = "OPEN"
                self.open_trades[tid] = t
            if stale_ghosts:
                logger.info(
                    f"Closed {stale_ghosts} stale previous-session ghost trade(s) as "
                    "STALE_SESSION_END on load (no broker reconciliation applies)."
                )
                try:
                    self.save_state()
                except Exception:
                    pass
            if auto_closed_missing_contracts:
                logger.warning(
                    f"Closed {auto_closed_missing_contracts} trade(s) with missing broker contract metadata on load."
                )
                try:
                    self.save_state()
                except Exception:
                    pass

            for data in closed_records:
                try:
                    t = Trade.from_dict(data)
                    t.status = "CLOSED"
                    self.closed_trades.append(t)
                except Exception as e:
                    logger.warning(f"Failed to parse historical trade: {e}")
            
            self._recover_trade_counter()
            if self.open_trades or self.closed_trades:
                logger.success(f"♻️ Recovered {len(self.open_trades)} active and {len(self.closed_trades)} historical trades from DB.")
        except Exception as e:
            logger.error(f"Failed to load trade state from DB: {e}")

    def _recover_trade_counter(self) -> None:
        """Continue generated trade IDs above every persisted T-number."""
        persisted = list(self.open_trades.values()) + list(self.closed_trades)
        for trade in persisted:
            match = re.match(r"^T(\d+)_", str(getattr(trade, "id", "") or ""))
            if match:
                self._trade_counter = max(self._trade_counter, int(match.group(1)))

    def reset_pnl(self):
        """Reset all trades and P&L for a fresh simulation"""
        if getattr(self, "mode", "HISTORICAL") == "REAL" and self.has_live_exposure():
            logger.critical("Blocked P&L reset while REAL broker exposure is still active.")
            return False
        try:
            backup_path = ""
            with self._lock:
                existing_rows = db.load_all_trades()
                if existing_rows:
                    backup_dir = Path("data_store/reset_backups")
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    backup_path = str(
                        backup_dir
                        / f"trades_reset_backup_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.json"
                    )
                    Path(backup_path).write_text(
                        json.dumps(existing_rows, indent=2, default=str),
                        encoding="utf-8",
                    )
                with db.write_lock:
                    db.conn.execute("DELETE FROM trades")
                    db.conn.commit()
                self.open_trades.clear()
                self.closed_trades.clear()
        except Exception as exc:
            logger.error(f"Failed to clear persisted trades during P&L reset: {exc}")
            return False
        backup_note = f" Backup: {backup_path}" if backup_path else ""
        logger.info(f"🧹 Trade Manager: Cleared all trades and P&L from DB.{backup_note}")
        return True

    def has_live_exposure(self) -> bool:
        """Return True while broker-backed or broker-only positions need protection."""
        with self._lock:
            active = any(
                trade.status == "OPEN" and not getattr(trade, "is_ghost", False)
                for trade in self.open_trades.values()
            )
            return active or bool(self.orphan_positions)

    def reset_session(self, session_date: date) -> Dict[str, int]:
        """Start a new accounting session without deleting active broker state.

        Ghost/shadow trades (engine-lab, premarket HISTORICAL replays) never had
        broker orders — at rollover they are closed as STALE_SESSION_END instead
        of being retained for a "reconciliation" that can never happen
        (2026-07-10: 24 premarket ghosts raised a false CRITICAL at 09:00).
        """
        stale_ghosts: List[Trade] = []
        with self._lock:
            closed = len(self.closed_trades)
            for tid, trade in list(self.open_trades.items()):
                if bool(getattr(trade, "is_ghost", False)):
                    trade.status = "CLOSED"
                    trade.pending_exit = False
                    trade.execution_status = "CLOSED"
                    trade.exit_reason = getattr(trade, "exit_reason", "") or "STALE_SESSION_END"
                    trade.exit_time = getattr(trade, "exit_time", None) or datetime.now(IST)
                    trade.exit_price = float(
                        getattr(trade, "exit_price", 0.0)
                        or getattr(trade, "current_price", 0.0)
                        or getattr(trade, "entry_price", 0.0)
                        or 0.0
                    )
                    self.closed_trades.append(trade)
                    self.open_trades.pop(tid, None)
                    stale_ghosts.append(trade)
                    continue
                if trade.entry_time.date() < session_date:
                    trade.execution_status = "RECOVERY_REQUIRED"
                    trade.execution_error = "Previous-session trade requires broker reconciliation"
            active = len(self.open_trades)
        self.save_state()
        if stale_ghosts:
            logger.info(
                f"Session rollover closed {len(stale_ghosts)} ghost/shadow trade(s) as "
                "STALE_SESSION_END (no broker reconciliation applies)."
            )
        if active:
            logger.critical(
                f"Session rollover retained {active} active trade(s); broker reconciliation required."
            )
        return {"active_retained": active, "closed_history_retained": closed}

    def _parse_time(self, t: str) -> dtime:
        parts = t.split(":")
        return dtime(int(parts[0]), int(parts[1]))

    def _effective_max_positions(self) -> int:
        """Base max_positions, raised during cascade multi-TF waves (R4)."""
        base = max(1, int(getattr(self, "max_positions", 12) or 12))
        try:
            from config.settings import get_settings

            s = get_settings()
            casc_cap = max(
                0, int(getattr(s, "cascade_max_concurrent_positions", 0) or 0)
            )
            if casc_cap <= base:
                return base
            # Any live cascade entry or sticky wave flag unlocks cascade ceiling
            has_cascade = any(
                bool(getattr(t, "one_way_cascade_entry", False))
                for t in (getattr(self, "open_trades", {}) or {}).values()
                if not getattr(t, "is_ghost", False)
            )
            if has_cascade or bool(getattr(self, "_cascade_wave_capacity", False)):
                return casc_cap
        except Exception:
            pass
        return base

    def arm_cascade_wave_capacity(self, enabled: bool = True) -> None:
        """Scanner arms this before opening a multi-signal cascade batch."""
        setattr(self, "_cascade_wave_capacity", bool(enabled))

    def can_open_trade(self, is_recovery: bool = False) -> Tuple[bool, str]:
        """Check if we can open a new trade"""
        with self._lock:
            if not is_recovery and self.orphan_positions:
                return False, "Broker-only orphan position requires reconciliation"
            active_positions = sum(
                1
                for trade in self.open_trades.values()
                if not getattr(trade, "is_ghost", False)
            )
            cap = self._effective_max_positions()
            if not is_recovery and active_positions >= cap:
                return False, f"Max positions ({cap}) reached"
        return True, ""

    @staticmethod
    def _extract_order_id(result) -> str:
        if not result:
            return ""
        if isinstance(result, dict):
            if broker_rejection(result):
                return ""
            data = result.get("data", result)
            if isinstance(data, dict):
                return str(data.get("orderid") or data.get("uniqueorderid") or "")
            return str(data or "")
        return str(result)

    def open_trade(
        self,
        instrument: str,
        timeframe: str,
        direction: str,
        price: float,
        trailing_stop: float,
        lots: int,
        lot_size: int,
        grade: str,
        atm_strike: float = 0.0,
        option_type: str = "",
        target: float = 0.0,
        rr_ratio: float = 1.5,
        confidence: float = 0.0,
        instrument_multiplier: float = 1.0,
        trading_symbol: str = "",
        symbol_token: str = "",
        inst_type: str = "FUT",
        exec_type: str = "A",
        entry_spot: float = 0.0,
        spot_stop: float = 0.0,
        spot_target: float = 0.0,
        is_recovery: bool = False,
        is_ghost: bool = False,
        is_reversion_scalp: bool = False,
        is_explosive_bypass: bool = False,
        entry_time: Optional[datetime] = None,
        signal_time: Optional[datetime] = None,
        signal_fire_mode: str = "INTRACANDLE",
        entered_intracandle: bool = True,
        market_regime: str = "",
        data_provenance: str = "",
        price_action_verdict: str = "",
        intelligence_score: float = 0.0,
        entry_momentum_urgency: float = 0.0,
        strategy_revision: str = "",
        strategy_direction_mode: str = "",
        market_wide_reversal_entry: bool = False,
        volume_spike_entry: bool = False,
        power_move: bool = False,
        source_engine: str = "",
        source_engines: Optional[List[str]] = None,
        source_lane: str = "",
        quad_union_entry: bool = False,
        raw_union_entry: bool = False,
        initial_stop: float = 0.0,
        scalp_lock_mode: bool = False,
        execution_touch_quote: Optional[Dict[str, Any]] = None,
        on_execution_result: Optional[Callable[[Trade, bool], None]] = None,
    ) -> Optional[Trade]:
        """Open a new trade and execute via broker"""
        if entry_time is not None:
            now = entry_time
        elif is_recovery and signal_time is not None:
            now = signal_time
        else:
            now = datetime.now(IST)
        with self._lock:
            can, reason = self.can_open_trade(is_recovery)
            if not can:
                logger.warning(f"Cannot open trade: {reason}")
                return None
            # Is the price we are about to trade on real? Checked before anything sizes,
            # logs or reaches the broker, because a stale mark poisons the stop, the
            # target and every P&L reading derived from them.
            mark_ok, mark_reason = self._entry_mark_admission(
                inst_type=inst_type, price=price, signal_time=signal_time,
                entry_time=now, touch_quote=execution_touch_quote,
                trading_symbol=trading_symbol, is_recovery=is_recovery,
            )
            if not mark_ok:
                logger.warning(f"Entry refused on mark validation: {mark_reason}")
                self._log_ui(f"Entry refused: {mark_reason}", "warning")
                return None
            # Would accepting this candidate make the BOOK unsafe at its configured
            # stops? can_open_trade counts positions; the circuit breaker measures a book
            # that already exists. Neither refuses the entry that creates the breach.
            # Off by default — see _entry_risk_admission.
            admitted, risk_reason = self._entry_risk_admission(
                inst_type=inst_type, direction=direction, price=price,
                trailing_stop=trailing_stop, initial_stop=initial_stop,
                lots=lots, lot_size=lot_size,
                instrument_multiplier=instrument_multiplier,
                is_recovery=is_recovery,
            )
            if not admitted:
                logger.warning(f"Entry refused by projected stop-risk budget: {risk_reason}")
                self._log_ui(f"Entry refused: {risk_reason}", "warning")
                return None
            known_ids = set(self.open_trades)
            known_ids.update(str(trade.id) for trade in self.closed_trades)
            while True:
                self._trade_counter += 1
                trade_id = f"T{self._trade_counter:04d}_{instrument}_{now.strftime('%m%d%H%M')}"
                if trade_id not in known_ids:
                    break

        trade = Trade(
            id=trade_id,
            instrument=instrument,
            timeframe=timeframe,
            direction=direction,
            entry_price=price,
            entry_time=now,
            signal_time=signal_time,
            trailing_stop=trailing_stop,
            current_stop=trailing_stop,
            lots=lots,
            lot_size=lot_size,
            grade=grade,
            atm_strike=atm_strike,
            option_type=option_type,
            target=target,
            rr_ratio=rr_ratio,
            confidence=confidence,
            inst_type=inst_type,
            instrument_multiplier=instrument_multiplier,
            current_price=price,
            trading_symbol=trading_symbol,
            symbol_token=symbol_token,
            exec_type=exec_type,
            # NEVER default the index-spot field to the CONTRACT price. On an OPT,
            # `price` is the option PREMIUM (~120) against a spot of ~24,850, and every
            # spot<->premium converter keys off this field -- one missing spot tick at
            # entry fabricated lakhs of P&L and fired TARGET_HIT on it. On a FUT it is
            # the futures LTP, which differs from spot by the basis.
            #
            # 0.0 is a supported value, not a hole: every reader guards `entry_spot > 0`
            # and falls back to holding entry_price (see update_trade below, "do NOT use
            # spot price as option price"). That guard was unreachable while this line
            # guaranteed entry_spot > 0 whenever price > 0.
            entry_spot=float(entry_spot or 0.0),
            spot_stop=spot_stop,
            spot_target=spot_target,
            is_ghost=is_ghost,
            is_reversion_scalp=is_reversion_scalp,
            signal_fire_mode=str(signal_fire_mode or "INTRACANDLE"),
            entered_intracandle=bool(entered_intracandle),
            market_regime=str(market_regime or ""),
            data_provenance=str(data_provenance or ""),
            price_action_verdict=str(price_action_verdict or ""),
            intelligence_score=float(intelligence_score or 0.0),
            strategy_revision=str(strategy_revision or ""),
            strategy_direction_mode=str(strategy_direction_mode or ""),
            market_wide_reversal_entry=market_wide_reversal_entry,
            volume_spike_entry=volume_spike_entry,
            power_move=power_move,
            source_engine=str(source_engine or ""),
            source_engines=list(source_engines or []),
            source_lane=str(source_lane or ""),
            quad_union_entry=bool(quad_union_entry),
            raw_union_entry=bool(raw_union_entry),
            initial_stop=float(initial_stop or trailing_stop or 0.0),
            scalp_lock_mode=bool(scalp_lock_mode),
            signal_entry_price=float(price or 0.0),
            execution_bid_price=float((execution_touch_quote or {}).get("bid") or 0.0),
            execution_ask_price=float((execution_touch_quote or {}).get("ask") or 0.0),
            execution_touch_source=str((execution_touch_quote or {}).get("source") or ""),
            execution_touch_age_seconds=float((execution_touch_quote or {}).get("age_seconds") or 0.0),
            execution_touch_usable=bool((execution_touch_quote or {}).get("usable", False)),
            execution_touch_reason=str((execution_touch_quote or {}).get("reason") or ""),
        )
        # Not a Trade field: it matters only at the instant the entry order is placed,
        # so it never needs to persist or survive a restart.
        setattr(trade, "entry_momentum_urgency", float(entry_momentum_urgency or 0.0))

        trade.pending_execution = not is_recovery
        trade.execution_status = "OPEN" if (is_recovery or is_ghost) else "ENTRY_PENDING"
        with self._lock:
            can, reason = self.can_open_trade(is_recovery)
            if not can:
                logger.warning(f"Cannot reserve trade after concurrent update: {reason}")
                return None
            self.open_trades[trade_id] = trade
        self.save_state()

        qty = lots * lot_size
        logger.info(
            f"🟢 TRADE LOGGED: {trade_id} | {direction} {trading_symbol or instrument} @ {price:.2f} | "
            f"Stop: {trailing_stop:.2f} | {lots}×{lot_size}={qty}{' [GHOST]' if is_ghost else ''}"
        )

        # ⚡ REAL BROKER ENTRY (Background Thread) ⚡ 
        is_warmup = getattr(self, "is_warmup", False)
        if (self._live_orders_permitted() and self.broker and trading_symbol
                and symbol_token and not is_warmup and not is_ghost):
            order_side = "BUY" if inst_type == "OPT" else ("BUY" if direction == "LONG" else "SELL")
            
            def _place_bg_body():
                # Every retry runs the preflight again and takes its own reservation.
                # Collected so a definitive rejection can hand all of them back at once.
                reservations: List[str] = []

                def place_order():
                    # Ask the broker whether this order can be funded BEFORE sending it.
                    # preflight_entry_margin was written for exactly this and had no
                    # caller, so an underfunded account fired a doomed order per signal
                    # and got back a generic RMS rejection with no stated cause. It fails
                    # OPEN when it cannot verify, so an unavailable margin endpoint never
                    # blocks trading.
                    preflight = self._entry_margin_verdict(
                        trading_symbol, trade.quantity, order_side, price
                    )
                    if preflight is not None and not preflight.get("ok", True):
                        raise BrokerOrderRejected(
                            preflight.get("limitation")
                            or preflight.get("reason")
                            or "entry margin preflight failed",
                            terminal=True,
                        )
                    # An approved preflight has RESERVED this order's margin so a
                    # concurrent entry cannot spend the same cash. The reservation must
                    # be given back the moment we know this order will not consume it —
                    # but only on a definite refusal. An AMBIGUOUS acknowledgement means
                    # the order may be live, so it keeps holding its margin; letting the
                    # next entry spend that money is how an account over-commits on
                    # exactly the orders it cannot see.
                    reservation_id = str((preflight or {}).get("reservation_id") or "")
                    if reservation_id:
                        reservations.append(reservation_id)
                    try:
                        return self.broker.place_order(
                            symbol=trading_symbol,
                            token=symbol_token,
                            qty=trade.quantity,
                            side=order_side,
                            product_type=self.product_type,
                            price=price,
                            touch_quote=execution_touch_quote,
                            # Tag the order with its trade so the order book, trades
                            # and postbacks map back exactly instead of by heuristic.
                            trade_id=trade.id,
                            # Decides MARKET vs LIMIT: only a high-conviction signal
                            # is worth risking a missed fill to get a better price.
                            confidence=float(getattr(trade, "confidence", 0.0) or 0.0),
                            # How fast the premium is leaving the bid, from MACD
                            # acceleration. Not persisted: it is only meaningful at the
                            # instant the order is placed, so it never needs to survive a
                            # restart. Absent on a recovery or a replayed row, which
                            # correctly reads as "no urgency".
                            momentum_urgency=float(
                                getattr(trade, "entry_momentum_urgency", 0.0) or 0.0
                            ),
                        )
                    except TypeError as exc:
                        if not any(
                            arg in str(exc)
                            for arg in ("touch_quote", "trade_id", "confidence", "momentum_urgency")
                        ):
                            raise
                        return self.broker.place_order(
                            symbol=trading_symbol,
                            token=symbol_token,
                            qty=trade.quantity,
                            side=order_side,
                            product_type=self.product_type,
                            price=price,
                        )

                order_id, error, attempts = execute_order_with_retry(
                    place_order,
                    self._extract_order_id,
                    f"Entry order {trading_symbol}",
                )
                if order_id:
                    with self._lock:
                        if trade.id in self.open_trades:
                            trade.broker_order_id = order_id
                            trade.pending_execution = True
                            trade.entry_submitted_at = datetime.now(IST)
                            trade.execution_status = "ENTRY_SUBMITTED"
                            trade.execution_error = ""
                    self.save_state()
                    if on_execution_result:
                        on_execution_result(trade, True)
                    msg = (
                        f"✅ Broker order placed successfully for {trading_symbol}: "
                        f"{order_id} (attempt {attempts})"
                    )
                    logger.info(msg)
                    self._log_ui(msg, "trade")
                    return

                ambiguous_ack = "ambiguous" in str(error or "").lower()
                if ambiguous_ack:
                    msg = (
                        f"⚠️ Broker entry acknowledgement ambiguous for {trading_symbol}; "
                        "trade requires broker position reconciliation before any new action."
                    )
                    logger.critical(msg)
                    self._log_ui(msg, "error")
                    with self._lock:
                        if trade.id in self.open_trades:
                            trade.pending_execution = True
                            trade.entry_submitted_at = datetime.now(IST)
                            trade.execution_status = "RECOVERY_REQUIRED"
                            trade.execution_error = error or "Broker entry acknowledgement ambiguous"
                    self.save_state()
                    if on_execution_result:
                        on_execution_result(trade, False)
                    return

                # A definitive refusal consumes no margin, so give every reservation
                # this attempt took back immediately rather than waiting out the TTL and
                # blocking the next signal on money nobody is spending. The ambiguous
                # branch above deliberately does NOT reach here.
                self._release_margin_reservations(reservations)
                msg = (
                    f"❌ Broker entry failed for {trading_symbol} after "
                    f"{attempts} attempts: {error}"
                )
                logger.error(msg)
                self._log_ui(msg, "error")
                self._alert_blocking_broker_condition(error)
                with self._lock:
                    if trade.id in self.open_trades:
                        trade.status = "CLOSED"
                        trade.exit_reason = "BROKER_ERROR"
                        trade.exit_time = datetime.now(IST)
                        trade.exit_price = trade.entry_price
                        trade.pending_execution = False
                        trade.execution_status = "REJECTED"
                        trade.execution_error = error
                        self.closed_trades.append(trade)
                        del self.open_trades[trade.id]
                self.save_state()
                if on_execution_result:
                    on_execution_result(trade, False)
            

            def place_bg():
                """Run the entry task, and never let an exception vanish into the queue.

                AsyncExecutionQueue catches and logs whatever escapes a task, then moves
                on — the worker survives, but the TRADE does not get cleaned up. Every
                statement after execute_order_with_retry returns is unprotected
                (save_state, _log_ui, _alert_blocking_broker_condition, the
                on_execution_result callback), so a throw there left the row sitting in
                open_trades with pending_execution True, holding a position slot and
                counting against exposure while nothing rested at the exchange.

                RECOVERY_REQUIRED, not "closed": an arbitrary exception says nothing
                about whether the order reached the broker, and concluding "not placed"
                from a failure is the mistake this codebase keeps making. The pending
                flags are deliberately left alone for the same reason.
                """
                try:
                    return _place_bg_body()
                except Exception as exc:
                    logger.critical(
                        f"[ENTRY TASK ESCAPED] {trade.id} {trade.trading_symbol}: {exc}. "
                        "Broker state unknown; marking for reconciliation."
                    )
                    try:
                        self._mark_recovery(
                            trade,
                            f"entry task raised after submission ({exc}); the order may or "
                            "may not be live at the broker.",
                        )
                    except Exception:
                        pass
                    return None
            from engine.async_queue import execution_queue
            # `is False` and not `not ...`: submit() is documented -> bool, and a test
            # double or simulation stub that returns None means "I do not signal", not
            # "I refused". Treating None as refusal closed 20 healthy trades outright.
            if execution_queue.submit(f"PlaceOrder_{trading_symbol}", place_bg) is False:
                # place_bg never runs, so NO order is sent. Ignoring this left the row
                # pending_execution forever: it held a position slot, counted against
                # exposure and looked live on the dashboard while the broker had never
                # heard of it. Nothing is resting at the exchange, so unlike the exit
                # path this can be closed outright rather than escalated.
                error = "Execution queue refused the entry task; no order was sent"
                logger.error(f"❌ {error} for {trading_symbol}")
                self._log_ui(f"❌ {error} for {trading_symbol}", "error")
                with self._lock:
                    if trade.id in self.open_trades:
                        trade.status = "CLOSED"
                        trade.exit_reason = "BROKER_ERROR"
                        trade.exit_time = datetime.now(IST)
                        trade.exit_price = trade.entry_price
                        trade.pending_execution = False
                        trade.execution_status = "REJECTED"
                        trade.execution_error = error
                        self.closed_trades.append(trade)
                        del self.open_trades[trade.id]
                self.save_state()
                if on_execution_result:
                    on_execution_result(trade, False)
        else:
            if (
                getattr(self, "mode", "HISTORICAL") == "REAL"
                and not is_recovery
                and not is_ghost
            ):
                with self._lock:
                    trade.status = "CLOSED"
                    trade.pending_execution = False
                    trade.execution_status = "REJECTED"
                    trade.execution_error = "REAL broker or executable contract details unavailable"
                    trade.exit_reason = "BROKER_UNAVAILABLE"
                    trade.exit_time = datetime.now(IST)
                    trade.exit_price = trade.entry_price
                    self.closed_trades.append(trade)
                    self.open_trades.pop(trade.id, None)
                self.save_state()
                if on_execution_result:
                    on_execution_result(trade, False)
            else:
                trade.pending_execution = False
                trade.execution_status = "OPEN"
                if on_execution_result:
                    on_execution_result(trade, True)

        return trade

    def update_trade(
        self, 
        trade_id: str, 
        current_spot: float, 
        new_trailing_stop: float = None,
        current_time: Optional[datetime] = None,
        real_premium: Optional[float] = None,
        real_fut_price: Optional[float] = None,
    ):
        """
        Update trade with latest price and trailing stop from 1-min data.
        Automatically closes trade if trailing stop is hit.
        """
        trade = self.open_trades.get(trade_id)
        if not trade or trade.status != "OPEN":
            return
        recovery_required = (
            str(getattr(trade, "execution_status", "") or "").upper()
            == "RECOVERY_REQUIRED"
        )
        if (
            getattr(trade, "pending_execution", False)
            and not getattr(trade, "pending_exit", False)
            and not recovery_required
            and self._confirmed_broker_quantity(trade) <= 0
        ):
            return  # Wait for broker confirmation before triggering stops

        # If it's an option, current_price is the synthetic premium. 
        # Premium change = (Spot Change * Signed Delta)
        is_option = trade.inst_type == "OPT" or "CE" in getattr(trade, 'trading_symbol', '') or "PE" in getattr(trade, 'trading_symbol', '')
        
        if is_option:
            if (
                real_premium is not None
                and real_premium > 0
                and self._option_premium_quote_plausible(trade, real_premium, current_spot)
            ):
                trade.current_price = float(real_premium)
            elif real_premium is not None and real_premium > 0:
                logger.warning(
                    f"Ignored implausible option premium {float(real_premium):.2f} "
                    f"for {trade.instrument} {trade.trading_symbol}; using synthetic premium fallback."
                )
                if trade.entry_spot > 0:
                    spot_change = current_spot - trade.entry_spot
                    premium_move = spot_change * trade.instrument_multiplier
                    trade.current_price = trade.entry_price + premium_move
                else:
                    trade.current_price = trade.entry_price
            elif trade.entry_spot > 0:
                spot_change = current_spot - trade.entry_spot
                
                # Premium Move = Spot Move * Signed Delta (instrument_multiplier)
                # PE has negative delta, so spot down (-100) * delta (-0.5) = +50 premium.
                premium_move = spot_change * trade.instrument_multiplier
                trade.current_price = trade.entry_price + premium_move
            else:
                # Fallback: If entry_spot is missing, do NOT use spot price as option price!
                # Keep it at entry_price to avoid massive fake P&L.
                trade.current_price = trade.entry_price
                
            # Ensure premium doesn't go below 1.0 (option value floor)
            trade.current_price = max(0.05, trade.current_price)
        else:
            if real_fut_price is not None and real_fut_price > 0:
                trade.current_price = float(real_fut_price)
            elif trade.entry_spot > 0:
                trade.current_price = trade.entry_price + (current_spot - trade.entry_spot)
            else:
                trade.current_price = current_spot

        logger.debug(f"💓 Heartbeat {trade.id}: Price {trade.current_price:.2f} | Stop {trade.current_stop:.2f} | PnL ₹{trade.pnl:.0f}")

        # Update trailing stop (only ratchet — never move against trade).
        # Audit #9: single source of truth for units —
        #   * OPT: new_trailing_stop is PREMIUM space (scanner converts spot→prem)
        #   * FUT: new_trailing_stop is CONTRACT price space
        # Dual spot*delta math was removed; scanner owns spot→premium mapping.
        if new_trailing_stop is not None:
            try:
                incoming_stop = float(new_trailing_stop)
            except (TypeError, ValueError):
                incoming_stop = 0.0
            if incoming_stop > 0:
                if is_option:
                    # Options are always long premium: only tighten (raise) the stop.
                    if incoming_stop > float(trade.current_stop or 0.0):
                        trade.current_stop = incoming_stop
                elif trade.direction == "LONG":
                    if incoming_stop > float(trade.current_stop or 0.0):
                        trade.current_stop = incoming_stop
                elif trade.direction == "SHORT":
                    current = float(trade.current_stop or 0.0)
                    if current <= 0 or incoming_stop < current:
                        trade.current_stop = incoming_stop

        if int(getattr(trade, "broker_quantity", 0) or 0) > 0:
            self.ensure_broker_protection(trade, trade.broker_quantity)
            if recovery_required:
                self._maybe_auto_flatten_unprotected(
                    trade,
                    "Broker protection unavailable",
                )

        # Keep open-trade mark-to-market consistent with risk-manager net P&L.
        trade.pnl = trade.unrealized_pnl

        # Track peak and drawdown
        trade.peak_pnl = max(trade.peak_pnl, trade.pnl)
        trade.max_drawdown = max(trade.max_drawdown, trade.peak_pnl - trade.pnl)
        self._sample_pnl_path(trade)

        if recovery_required or getattr(trade, "pending_exit", False):
            now = time.monotonic()
            if now - self._last_mark_to_market_save >= 1.0:
                self._last_mark_to_market_save = now
                self.save_state()
            return

        # ── EXIT CHECKS (Stop Loss, Target, Early Loss-Guard) ──
        # Symmetric for FUT and OPT:
        #   * trailing stop / target trail on winners
        #   * early loss-cut on losers (mirror of profit lock on the loss side)
        # Option trades are premium-led; futures are contract-price-led.
        runner_mode = bool(getattr(trade, "runner_mode", False))
        target = float(getattr(trade, "target", 0.0) or 0.0)

        # Early loss-guard backstop (R2): use scanner-published loss_guard_cut_r
        # when present (intel-accelerated); else base 0.8R settings.
        cut_override = getattr(trade, "loss_guard_cut_r", None)
        if self._early_loss_cut_hit(trade, cut_r_override=cut_override):
            self.close_trade(
                trade_id, trade.current_price, "EARLY_LOSS_CUT", exit_time=current_time
            )
        elif trade.inst_type == "OPT":
            if trade.current_price <= trade.current_stop:
                self.close_trade(trade_id, trade.current_price, "TRAILING_STOP", exit_time=current_time)
            elif (not runner_mode) and target > 0 and trade.current_price >= target:
                self.close_trade(trade_id, trade.current_price, "TARGET_HIT", exit_time=current_time)
        elif trade.direction == "LONG":
            if trade.current_price <= trade.current_stop:
                self.close_trade(trade_id, trade.current_price, "TRAILING_STOP", exit_time=current_time)
            elif (not runner_mode) and target > 0 and trade.current_price >= target:
                self.close_trade(trade_id, trade.current_price, "TARGET_HIT", exit_time=current_time)
        else:  # SHORT FUT
            if trade.current_price >= trade.current_stop:
                self.close_trade(trade_id, trade.current_price, "TRAILING_STOP", exit_time=current_time)
            elif (not runner_mode) and target > 0 and trade.current_price <= target:
                self.close_trade(trade_id, trade.current_price, "TARGET_HIT", exit_time=current_time)
        
        if trade_id in self.open_trades:
            from config.settings import get_settings
            checkpoint = max(
                0.25,
                float(getattr(get_settings(), "trade_state_checkpoint_seconds", 1.0) or 1.0),
            )
            now = time.monotonic()
            if now - self._last_mark_to_market_save >= checkpoint:
                self._last_mark_to_market_save = now
                self.save_state()

    @staticmethod
    def _option_premium_quote_plausible(trade: Trade, premium: float, current_spot: float = 0.0) -> bool:
        """Reject option LTPs that are obviously index/futures prices."""
        try:
            premium = float(premium or 0.0)
        except (TypeError, ValueError):
            return False
        if premium <= 0:
            return False

        entry = float(getattr(trade, "entry_price", 0.0) or 0.0)
        target = float(getattr(trade, "target", 0.0) or 0.0)
        spot = float(current_spot or getattr(trade, "entry_spot", 0.0) or 0.0)
        strike = float(getattr(trade, "atm_strike", 0.0) or 0.0)
        opt_type = str(getattr(trade, "option_type", "") or "").upper()
        intrinsic = 0.0
        if spot > 0 and strike > 0:
            intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)

        bounds = [1000.0]
        if entry > 0:
            bounds.append(entry * 4.0)
        if target > 0:
            bounds.append(target * 3.0)
        if spot > 0:
            bounds.append(intrinsic + spot * 0.05)
            bounds.append(spot * 0.12)
        return premium <= max(bounds)

    @staticmethod
    def _early_loss_cut_hit(trade: "Trade", cut_r_override: Optional[float] = None) -> bool:
        """True when unrealized loss reaches the early loss-cut R.

        Base threshold is settings fut/opt_loss_cut_r (default 0.8R). Scanner may
        pass a lower cut_r_override when intel confirms a big reversal (0.55 / 0.35).
        """
        try:
            from config.settings import get_settings

            settings = get_settings()
        except Exception:
            return False

        is_opt = str(getattr(trade, "inst_type", "FUT") or "FUT").upper() == "OPT"
        if is_opt:
            if not bool(getattr(settings, "opt_early_loss_cut_enabled", True)):
                return False
            cut_r = float(getattr(settings, "opt_loss_cut_r", 0.8) or 0.8)
        else:
            if not bool(getattr(settings, "fut_early_loss_cut_enabled", True)):
                return False
            cut_r = float(getattr(settings, "fut_loss_cut_r", 0.8) or 0.8)
        if cut_r_override is not None:
            try:
                cut_r = float(cut_r_override)
            except (TypeError, ValueError):
                pass
        cut_r = max(0.05, min(1.0, cut_r))

        entry = float(getattr(trade, "entry_price", 0.0) or 0.0)
        current = float(getattr(trade, "current_price", 0.0) or 0.0)
        if entry <= 0 or current <= 0:
            return False

        initial_stop = float(
            getattr(trade, "initial_stop", 0.0)
            or getattr(trade, "trailing_stop", 0.0)
            or 0.0
        )
        if initial_stop <= 0:
            return False

        qty = max(1, int(getattr(trade, "lots", 1) or 1) * int(getattr(trade, "lot_size", 1) or 1))
        # OPT is always long-premium; FUT uses direction.
        if is_opt:
            risk_pts = max(0.0, entry - initial_stop)
            pnl_pts = current - entry
        elif str(getattr(trade, "direction", "LONG") or "LONG").upper() == "LONG":
            risk_pts = max(0.0, entry - initial_stop)
            pnl_pts = current - entry
        else:
            risk_pts = max(0.0, initial_stop - entry)
            pnl_pts = entry - current

        if risk_pts <= 0:
            return False
        risk_rs = risk_pts * qty
        pnl_rs = pnl_pts * qty
        # Only cut losers; winners are handled by profit-lock / trail.
        if pnl_rs >= 0:
            return False
        return abs(pnl_rs) >= (cut_r * risk_rs) - 1e-9

    def _request_broker_exit(
        self,
        trade: Trade,
        on_exit_result: Optional[Callable[[Trade, bool], None]] = None,
    ) -> None:
        broker = self.broker
        product_type = self.product_type

        def _close_bg_body():
            with self._lock:
                current = self.open_trades.get(trade.id)
                if (
                    current is not trade
                    or trade.status != "OPEN"
                    or not trade.pending_exit
                ):
                    logger.warning(
                        f"Skipped stale queued exit for {trade.id}; "
                        "the position is no longer pending locally."
                    )
                    if on_exit_result:
                        on_exit_result(trade, False)
                    return

            # The mode gate ran at ENQUEUE. This body runs later on the execution queue,
            # and the staleness check above only asks about the TRADE. If REAL was left
            # in between, the captured `broker` is still the live account — so authority
            # is re-asked here, against the broker this closure will actually use.
            if self._live_account_send_blocked(broker):
                logger.error(
                    f"Queued exit for {trade.id} refused at dispatch: mode is "
                    f"{getattr(self, 'mode', '')!r} and this task still holds the live "
                    "account broker."
                )
                if on_exit_result:
                    on_exit_result(trade, False)
                return

            exit_side = "SELL" if trade.direction == "LONG" or trade.inst_type == "OPT" else "BUY"
            exit_quantity = (
                int(trade.broker_quantity)
                if int(getattr(trade, "broker_quantity", 0) or 0) > 0
                else trade.quantity
            )

            if trade.protective_order_id:
                # PROVE the cancel; do not just acknowledge it. cancel_order returns True
                # whenever the HTTP call did not raise. The SL-M trigger sits at
                # protective_trigger — the same level the local trailing stop fires at —
                # so the cancel races the trigger at exactly the moment an exit is
                # decided. Kite can accept the cancel request while the triggered order
                # still fills; the full-size MARKET exit below then sells on top of an
                # SL-M that already sold, and the second fill is a naked written option
                # in CARRYFORWARD/NRML.
                #
                # _run_partial_reduce already uses this `is not True` test verbatim, with
                # the rule spelled out: "the fill routinely wins the race against the
                # cancel". This path was the one that never adopted it.
                confirm = getattr(broker, "cancel_order_confirmed", None)
                if callable(confirm):
                    try:
                        cancelled = confirm(trade.protective_order_id, "STOPLOSS")
                    except (BrokerOrderRejected, PermissionError) as exc:
                        logger.error(f"Protective stop cancel refused: {exc}")
                        cancelled = None
                else:
                    # A broker without the proven variant keeps the old behaviour rather
                    # than deadlocking; partial broker stubs are a real pattern here.
                    cancelled = True if broker.cancel_order(
                        trade.protective_order_id, "STOPLOSS"
                    ) else None
                if cancelled is not True:
                    self._mark_recovery(
                        trade,
                        "Protective stop cancellation unconfirmed; market exit withheld to avoid over-exit",
                    )
                    trade.pending_exit = False
                    self.save_state()
                    if on_exit_result:
                        on_exit_result(trade, False)
                    return
                trade.protective_order_id = ""
                self.save_state()

            exit_tag_kwargs = self._tag_kwargs(broker.close_order, trade, "EXIT")

            def close_order():
                return broker.close_order(
                        symbol=trade.trading_symbol,
                        token=trade.symbol_token,
                        qty=exit_quantity,
                        side=exit_side,
                        product_type=product_type,
                        **exit_tag_kwargs,
                    )

            order_id, last_error, attempts = execute_order_with_retry(
                close_order,
                self._extract_order_id,
                f"Exit order {trade.trading_symbol}",
            )

            if order_id:
                with self._lock:
                    if self.open_trades.get(trade.id) is not trade:
                        logger.critical(
                            f"Exit order {order_id} acknowledged after {trade.id} "
                            "left local open state; broker reconciliation is required."
                        )
                        return
                    trade.exit_order_id = order_id
                    trade.execution_status = "EXIT_PENDING"
                    trade.execution_error = ""
                self.save_state()
                if getattr(self, "mode", "HISTORICAL") != "REAL":
                    self._finalize_closed_trade(
                        trade.id,
                        trade,
                        trade.exit_price,
                        trade.exit_reason,
                        exit_time=trade.exit_time,
                    )
                if on_exit_result:
                    on_exit_result(trade, True)
                return

            ambiguous_ack = "ambiguous" in str(last_error or "").lower()
            if ambiguous_ack:
                trade.pending_exit = True
                trade.execution_status = "RECOVERY_REQUIRED"
                trade.execution_error = last_error or "Broker exit acknowledgement ambiguous"
            else:
                trade.pending_exit = False
                trade.execution_status = "EXIT_FAILED"
                trade.execution_error = last_error or "Unknown broker exit failure"
            self.save_state()
            logger.critical(
                f"[BROKER CLOSE FAILED] Position remains active for "
                f"{trade.trading_symbol} after {attempts} attempts: "
                f"{trade.execution_error}"
            )
            if on_exit_result:
                on_exit_result(trade, False)


        def close_bg():
            """Run the exit task, and never let an exception vanish into the queue.

            Same hazard as the entry task: the queue logs and continues, leaving the row
            pending_exit against what may be a live broker position.

            pending_exit is deliberately NOT cleared here. An exception raised AFTER the
            exit order was accepted would then permit a second exit on top of a live one,
            which is worse than the stranded row. RECOVERY_REQUIRED is the signal a human
            reconciles from, and the session-end sweep already routes on it.
            """
            try:
                return _close_bg_body()
            except Exception as exc:
                logger.critical(
                    f"[EXIT TASK ESCAPED] {trade.id} {trade.trading_symbol}: {exc}. "
                    "Broker state unknown; marking for reconciliation."
                )
                try:
                    self._mark_recovery(
                        trade,
                        f"exit task raised after submission ({exc}); the exit may or may "
                        "not have reached the broker.",
                    )
                except Exception:
                    pass
                return None
        from engine.async_queue import execution_queue
        if execution_queue.submit(f"CloseOrder_{trade.trading_symbol}", close_bg) is False:
            # submit() returns False on shutdown and when the queue is still full after a
            # bounded 2s wait. Both were being ignored, so close_bg simply never ran: no
            # order was sent, nothing raised, and the row sat pending_exit forever with a
            # LIVE position behind it. The queue drains at shutdown and at EOD — exactly
            # when an unexited position matters most.
            trade.pending_exit = False
            self._mark_recovery(
                trade,
                "Execution queue refused the exit task, so no exit order was ever sent. "
                "The position is still open at the broker.",
            )
            self.save_state()
            logger.critical(
                f"[EXIT NEVER QUEUED] {trade.trading_symbol} still holds a live position: "
                "the execution queue rejected the close task, so no order reached the "
                "broker. Flatten it manually if the automatic recovery does not."
            )
            if on_exit_result:
                on_exit_result(trade, False)

    def _live_orders_permitted(self) -> bool:
        """Whether this manager may send an order to a broker at all.

        Defense in depth, not the primary control. The factory does not attach a
        broker outside REAL mode, so this is a second lock on the same door —
        sited where the order is actually sent rather than where it is wired.

        Both broker-order paths previously guarded only on wiring (`self.broker`
        present, symbol/token resolved, not warmup, not ghost) and never asked
        what mode they were in, while the surrounding lifecycle checks at lines
        802, 1412, 3778 and 4692 all do. A broker injected by a test, a hot-swap,
        or a future wiring change would have reached the order path with nothing
        mode-aware in the way.

        trading_mode is Literal["HISTORICAL", "REAL"], so no legitimate mode is
        denied a fill by this: there is no third mode that trades a broker.
        """
        return str(getattr(self, "mode", "") or "").upper() == "REAL"

    def _live_account_send_blocked(self, broker=None) -> bool:
        """True when a send would reach the REAL ACCOUNT broker outside REAL mode.

        `broker` names the object the caller is actually about to send through.
        Queued work captures `broker = self.broker` at ENQUEUE time and dispatches
        later, so by then self.broker may already be the mock while the closure still
        holds the live one — checking self.broker would clear a send that is about to
        go to the real account anyway. Callers sending through self.broker pass
        nothing and are unaffected.

        _live_orders_permitted covers the two dispatch paths that ask it. Several
        broker-mutating paths never did: emergency_flatten_trade (and the flatten
        helpers below it), the three mutating branches of reconcile_broker_positions
        (cancel a resting entry, MARKET-replace it, cancel a protective stop) and the
        session-end orphan close. Their only gate is wiring — `if not self.broker`.

        In ordinary HISTORICAL running that costs nothing, because the factory attaches
        a SignalSimBroker whose order methods are stubs. The hole is the REAL ->
        HISTORICAL hot swap: scanner.configure sets self.trades.mode = "HISTORICAL"
        (scanner.py:4194) about a hundred lines before it swaps the broker back to the
        mock (scanner.py:4305), and the PositionSync daemon calls reconcile every ~5s
        throughout. In that window mode says HISTORICAL while self.broker is still the
        live KiteBroker.

        So the invariant this enforces is narrower and truer than "not REAL mode":
        never transact against the REAL ACCOUNT broker outside REAL mode. Gating those
        paths on mode alone would refuse the fake brokers that 53 test files inject in
        HISTORICAL on purpose; real_broker is wired only in main.py production startup
        (main.py:898), so it is absent under test and this predicate stays False there.
        """
        if self._live_orders_permitted():
            return False
        real = getattr(self, "real_broker", None)
        target = broker if broker is not None else getattr(self, "broker", None)
        return real is not None and target is real

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        reason: str = "MANUAL",
        exit_time: Optional[datetime] = None,
        on_exit_result: Optional[Callable[[Trade, bool], None]] = None,
    ):
        """Close a trade with IST aware timestamping"""
        needs_entry_sync = False
        with self._lock:
            trade = self.open_trades.get(trade_id)
            if not trade or trade.status != "OPEN":
                return False
            if trade.pending_exit or trade.execution_status in {"EXIT_PENDING", "PARTIAL_EXIT"}:
                return False
            requires_broker_exit = (
                getattr(self, "mode", "HISTORICAL") == "REAL"
                and not getattr(trade, "is_ghost", False)
            )
            if (
                self._entry_still_confirming(trade)
                and requires_broker_exit
                and self._confirmed_broker_quantity(trade) <= 0
                and self.broker
            ):
                needs_entry_sync = True
            elif self._entry_still_confirming(trade) and (
                not requires_broker_exit or self._confirmed_broker_quantity(trade) <= 0
            ):
                return False
            if requires_broker_exit and (not self.broker or not trade.symbol_token):
                trade.execution_status = "RECOVERY_REQUIRED"
                trade.execution_error = (
                    "REAL position cannot be closed without broker access and symbol token"
                )
                trade.pending_exit = False
                self.save_state()
                if on_exit_result:
                    on_exit_result(trade, False)
                return False

        if needs_entry_sync:
            try:
                self.reconcile_broker_positions()
            except Exception as exc:
                logger.error(f"Broker reconciliation before close failed: {exc}")

        with self._lock:
            trade = self.open_trades.get(trade_id)
            if not trade or trade.status != "OPEN":
                return False
            if trade.pending_exit or trade.execution_status in {"EXIT_PENDING", "PARTIAL_EXIT"}:
                return False
            requires_broker_exit = (
                getattr(self, "mode", "HISTORICAL") == "REAL"
                and not getattr(trade, "is_ghost", False)
            )
            if self._entry_still_confirming(trade):
                if not requires_broker_exit or self._confirmed_broker_quantity(trade) <= 0:
                    return False
            if requires_broker_exit and (not self.broker or not trade.symbol_token):
                trade.execution_status = "RECOVERY_REQUIRED"
                trade.execution_error = (
                    "REAL position cannot be closed without broker access and symbol token"
                )
                trade.pending_exit = False
                self.save_state()
                if on_exit_result:
                    on_exit_result(trade, False)
                return False
            trade.exit_price = float(exit_price or trade.current_price or trade.entry_price)
            trade.current_price = trade.exit_price
            trade.exit_time = exit_time or datetime.now(IST)
            trade.exit_reason = reason
            trade.pending_exit = True
            trade.execution_status = "EXIT_PENDING"
            trade.execution_error = ""
        self.save_state()

        if (self._live_orders_permitted() and self.broker and trade.symbol_token
                and not getattr(trade, 'is_ghost', False)):
            self._request_broker_exit(trade, on_exit_result=on_exit_result)
        else:
            self._finalize_closed_trade(
                trade_id,
                trade,
                trade.exit_price,
                reason,
                exit_time=trade.exit_time,
            )
            if on_exit_result:
                on_exit_result(trade, True)
        return True

    def check_session_end(self, current_time: Optional[datetime] = None):
        """
        Simple & Robust EOD Exit (Strict Session Bound)
        """
        if current_time:
            now = IST.localize(current_time) if current_time.tzinfo is None else current_time
        else:
            now = datetime.now(IST)
        now_t = now.time()
        
        from config.settings import get_settings
        force_exit_label = str(getattr(get_settings(), "ut_force_exit_time", "15:25") or "15:25")
        kill_threshold = self._parse_time(force_exit_label)
        
        if now_t < kill_threshold:
            return

        # ...and only on a day the exchange actually traded. This gated on the CLOCK
        # alone, so a position that survived Friday's square-off — a broker reject, an RMS
        # refusal, an IP block — kept the scanner running into the weekend, and at 15:23
        # on Saturday `now_t >= kill_threshold` was true again. It then sent exit orders
        # into a shut exchange, once a second, for two days.
        #
        # The position is real and does need closing, so this does NOT go quiet: it
        # escalates with the symbol and tells the operator to square off manually. What it
        # will not do is fire orders that can only be rejected.
        if not self._session_is_a_trading_day(now):
            self._alert_carrying_through_a_closed_day(now)
            return

        with self._lock:
            snapshot = list(self.open_trades.items())
            
        for tid, trade in snapshot:
            # Strategy-owned square-off. CARRYFORWARD avoids broker intraday auto-squareoff.
            status = str(getattr(trade, "execution_status", "") or "").upper()
            # PARTIAL_EXIT joins RECOVERY_REQUIRED here. close_trade REFUSES that status
            # (and refuses pending_exit), so routing it there meant the unfilled remainder
            # of a partially filled exit was never squared off: its protective stop had
            # already been cancelled before the exit was sent, so the residual carried
            # overnight naked in a product the broker will not auto-square-off.
            # emergency_flatten_trade is the right path — it scans for a stop it does not
            # own, withholds when it cannot prove the position is unprotected, and latches
            # after an ambiguous acknowledgement.
            # Per-trade isolation. This loop had none, so ONE raise anywhere below took
            # every remaining position in the snapshot down with it and skipped the orphan
            # sweep after the loop — which is exactly how an UnboundLocalError in
            # emergency_flatten_trade turned one refused stop cancel into an account that
            # carried its whole book overnight in a product the broker will not square off.
            # The specific raiser is fixed; this makes the next one cost one trade, loudly,
            # instead of all of them, silently.
            try:
                if status in {"RECOVERY_REQUIRED", "PARTIAL_EXIT"}:
                    self.emergency_flatten_trade(trade, "FORCE_EOD_KILL")
                else:
                    self.close_trade(tid, trade.current_price, "FORCE_EOD_KILL", exit_time=now)
            except Exception as exc:
                logger.critical(
                    f"FORCE EXIT FAILED for {getattr(trade, 'trading_symbol', tid)}: "
                    f"{type(exc).__name__}: {exc}. This position may carry overnight — "
                    "square it off at the Zerodha terminal.",
                    exc_info=True,
                )
                self._log_ui(
                    f"FORCE EXIT FAILED for {getattr(trade, 'trading_symbol', tid)} "
                    f"({type(exc).__name__}). Square off manually.",
                    "error",
                )

        # Outside the loop and outside its try: an orphan must be swept even when every
        # local exit above failed.
        try:
            self._handle_orphans_at_session_end(now)
        except Exception as exc:
            logger.critical(f"Orphan sweep failed at session end: {exc}", exc_info=True)

    def _session_is_a_trading_day(self, now: datetime) -> bool:
        """Did the exchange trade today? Weekends and holidays are not square-off days.

        Fails OPEN — an unreadable calendar returns True — because refusing to square off
        on a real trading day is far worse than sending an order that gets rejected on a
        holiday. The special-session case (a Saturday the exchange DOES open) is handled
        by is_market_holiday's own calendar, which knows about them.
        """
        provider = getattr(self, "data", None) or getattr(
            getattr(self, "scanner", None), "data", None
        )
        try:
            if now.weekday() >= 5:
                checker = getattr(provider, "is_market_special_session", None)
                if callable(checker) and checker(now):
                    return True
                # No provider to ask: a weekend is a weekend.
                if provider is None or not callable(checker):
                    return False
                return False
            holiday = getattr(provider, "is_market_holiday", None)
            if callable(holiday):
                return not bool(holiday(now))
        except Exception as exc:
            logger.debug(f"Trading-day check unavailable, assuming open: {exc}")
        return True

    def _alert_carrying_through_a_closed_day(self, now: datetime) -> None:
        """Say once per closed day that positions are sitting through it."""
        holding = [t for t in self.open_trades.values() if str(getattr(t, "status", "")) == "OPEN"]
        orphans = dict(getattr(self, "orphan_positions", {}) or {})
        if not holding and not orphans:
            return
        stamp = now.date().isoformat()
        if not hasattr(self, "_closed_day_alerted"):
            self._closed_day_alerted = set()
        if stamp in self._closed_day_alerted:
            return
        self._closed_day_alerted.add(stamp)
        names = [str(getattr(t, "trading_symbol", t.id)) for t in holding] + list(orphans)
        message = (
            f"MARKET CLOSED {stamp}: {len(names)} position(s) still open and the force exit "
            f"cannot run — {', '.join(names[:4])}"
            + (f" +{len(names) - 4} more" if len(names) > 4 else "")
            + ". Orders would only be rejected today. Square off at the Zerodha terminal, "
            "or they carry to the next session."
        )
        logger.critical(message)
        self._log_ui(message, "error")

    def _handle_orphans_at_session_end(self, now: datetime) -> None:
        """Broker positions this system did not open are NOT covered by the sweep above.

        check_session_end iterates self.open_trades only, so a broker-only position — the
        first-start-mid-session case, or a local row lost to a corrupted state file — is
        detected by reconcile, blocks new entries, and is then carried OVERNIGHT. The
        product is CARRYFORWARD/NRML precisely so the broker does not auto-square-off, so
        nothing else is going to close it either.

        Escalating is the default rather than flattening, because an orphan is not
        necessarily ours: a position taken manually at the Zerodha terminal looks
        identical from here, and squaring one off uninvited is worse than the exposure it
        removes. force_exit_flattens_orphans lets an operator who never trades manually
        opt into the sweep.
        """
        orphans = dict(getattr(self, "orphan_positions", {}) or {})
        if not orphans:
            return
        from config.settings import get_settings

        settings = get_settings()
        flatten = _setting_enabled(
            getattr(settings, "force_exit_flattens_orphans", False), False
        ) and not self._live_account_send_blocked()
        for symbol, position in orphans.items():
            quantity = self._position_quantity(position)
            if not quantity:
                continue
            if not flatten:
                self._alert_orphan_left_open(symbol, quantity)
                continue
            # Latch BEFORE sending, once per symbol per session. check_session_end runs
            # about once a second past the force-exit time, and orphan_positions is only
            # refreshed by reconcile on its 5s loop — so an un-latched success path
            # re-sends the same square-off four or five times before the book catches up.
            # On a BOUGHT option that is a naked short in a product the broker will not
            # square off, and once reconcile does update, the short is itself a
            # broker-only position, so the next pass computes the opposite side and the
            # loop oscillates, paying the spread each way.
            # Latching before the call, not after, is deliberate: an order that raises may
            # still have reached the exchange, and this file's rule everywhere else is
            # that an unacknowledged exit is never re-sent blind.
            if not hasattr(self, "_orphan_flatten_sent"):
                self._orphan_flatten_sent = set()
            if symbol in self._orphan_flatten_sent:
                continue
            self._orphan_flatten_sent.add(symbol)
            side = "SELL" if quantity > 0 else "BUY"
            try:
                order_id = self.broker.close_order(
                    str(symbol), str(position.get("symboltoken") or position.get("instrument_token") or ""),
                    abs(int(quantity)), side, self.product_type,
                )
            except (BrokerOrderRejected, PermissionError) as exc:
                logger.critical(f"Orphan square-off refused for {symbol}: {exc}")
                self._alert_orphan_left_open(symbol, quantity)
                continue
            except BrokerOrderAmbiguous as exc:
                # May be live. Never re-send blind; the operator settles it.
                logger.critical(
                    f"Orphan square-off for {symbol} was not acknowledged ({exc}). It may "
                    "be live — reconcile manually before sending another."
                )
                continue
            if order_id:
                logger.critical(
                    f"Orphan {symbol} x{abs(int(quantity))} squared off at the session "
                    f"force-exit: {order_id}"
                )
            else:
                self._alert_orphan_left_open(symbol, quantity)

    def _alert_unowned_protective_stop(self, trade, stop_id: str) -> None:
        """A stop we could not prove cancelled, on a row about to leave open_trades.

        Nothing scans closed trades for a protective order, so once the row is gone this
        SL-M has no owner in the system. Only the operator can settle it, and they need
        the id to do that. Throttled per order so the 5s reconcile loop cannot flood.
        """
        if not hasattr(self, "_unowned_stop_alerted"):
            self._unowned_stop_alerted = set()
        if stop_id in self._unowned_stop_alerted:
            return
        self._unowned_stop_alerted.add(stop_id)
        message = (
            f"UNOWNED STOP: {trade.trading_symbol} is flat at the broker and its row is "
            f"closing, but protective order {stop_id} was NOT confirmed cancelled. If it "
            "is still resting it will fire against a flat book and open a naked leg that "
            "the broker will not square off. Cancel it at the Zerodha terminal."
        )
        logger.critical(message)
        self._log_ui(message, "error")

    def _alert_orphan_left_open(self, symbol: str, quantity: int) -> None:
        """Say plainly that an unmanaged position is about to carry overnight."""
        if not hasattr(self, "_orphan_eod_alerted"):
            self._orphan_eod_alerted = set()
        if symbol in self._orphan_eod_alerted:
            return
        self._orphan_eod_alerted.add(symbol)
        message = (
            f"ORPHAN POSITION CARRYING OVERNIGHT: {symbol} x{abs(int(quantity))} is open "
            "at the broker, was not opened by this system, and is NOT covered by the "
            "force exit. CARRYFORWARD means the broker will not square it off either. "
            "Close it manually, or set force_exit_flattens_orphans to have the session "
            "end sweep it."
        )
        logger.critical(message)
        self._log_ui(message, "error")

    def check_expiry_gamma_time_exit(self, current_time: Optional[datetime] = None):
        """Square off expiry-day current-expiry (gamma) OPTION trades at the configured
        gamma time-exit, so an expiring ATM never bleeds into the afternoon theta crush.
        Gated behind expiry_day_trade_current_week and covers BOTH cohorts of expiry-day
        current-expiry trades:

          * the morning gamma cohort, entered before expiry_day_gamma_cutoff, closed at
            expiry_day_gamma_time_exit;
          * the LATE-WINDOW cohort, entered inside expiry_day_late_window (14:00-15:15),
            closed at expiry_day_late_window_time_exit.

        The late cohort previously had no time exit at all. It was unreachable twice over:
        the gamma time exit is 13:30, already past when the window opens, and the
        entry-time filter below dropped every trade entered at/after the 13:00 cutoff --
        i.e. structurally every trade the late window can create. That left a position on
        a contract expiring at 15:30 with nothing but the 15:25 force exit between it and
        settlement, in the least liquid minutes of an expiring option's life.
        """
        from config.settings import get_settings
        s = get_settings()
        if not bool(getattr(s, "expiry_day_trade_current_week", False)):
            return
        now = current_time or datetime.now(IST)
        now = IST.localize(now) if now.tzinfo is None else now.astimezone(IST)
        time_exit = self._parse_time(str(getattr(s, "expiry_day_gamma_time_exit", "13:30") or "13:30"))
        late_exit = self._parse_time(
            str(getattr(s, "expiry_day_late_window_time_exit", "15:20") or "15:20")
        )
        # Whichever cohort comes due first decides when this method starts doing work.
        if now.time() < min(time_exit, late_exit):
            return
        cutoff = self._parse_time(str(getattr(s, "expiry_day_gamma_cutoff", "13:00") or "13:00"))
        try:
            from engine.expiry_manager import expiry_manager
        except Exception:
            return
        with self._lock:
            snapshot = list(self.open_trades.items())
        for tid, trade in snapshot:
            if str(getattr(trade, "inst_type", "") or "").upper() != "OPT":
                continue
            et = getattr(trade, "entry_time", None)
            if et is None:
                continue
            et_ist = IST.localize(et) if et.tzinfo is None else et.astimezone(IST)
            try:
                is_exp = expiry_manager.is_expiry_day(trade.instrument, et_ist.date())
            except Exception:
                is_exp = False
            if not is_exp:
                continue
            # genuine T-0 gamma trade: entered on expiry day, before the morning cutoff
            if et_ist.time() < cutoff:
                if now.time() >= time_exit:
                    self.close_trade(
                        tid, trade.current_price, "EXPIRY_GAMMA_TIME_EXIT", exit_time=now
                    )
                continue
            # Entered at/after the cutoff. Only some of those sit on a contract expiring
            # TODAY -- after the cutoff the default mode rolls to the next expiry, and it
            # is the late window that re-engages the current one. in_gamma_window is the
            # exact predicate the ENTRY side used to make that choice, so asking it about
            # the entry timestamp identifies the expiring-contract cohort without
            # re-deriving the rule or touching rolled next-expiry trades.
            try:
                on_expiring_contract = bool(
                    expiry_manager.in_gamma_window(trade.instrument, et_ist)
                )
            except Exception:
                on_expiring_contract = False
            if on_expiring_contract and now.time() >= late_exit:
                self.close_trade(
                    tid, trade.current_price, "EXPIRY_LATE_WINDOW_TIME_EXIT", exit_time=now
                )

    def expire_stale_open_trades(self, now: Optional[datetime] = None, reason: str = "STALE_SESSION_END") -> int:
        """Retain previous-session trades until broker reconciliation proves closure."""
        now = now or datetime.now(IST)
        if now.tzinfo is None:
            now = IST.localize(now)
        marked = 0
        with self._lock:
            for trade in self.open_trades.values():
                entry_time = trade.entry_time
                if entry_time.tzinfo is None:
                    entry_time = IST.localize(entry_time)
                else:
                    entry_time = entry_time.astimezone(IST)
                if entry_time.date() >= now.date():
                    continue
                # Count the TRANSITION, not the state. `marked` drives the save_state()
                # plus reconcile_broker_positions() block below, and reconcile makes a
                # synchronous Kite get_positions() call. This function runs from
                # _scan_cycle on the 1s event loop, so re-marking an already-marked row
                # fired a blocking broker REST call and a full trades-table snapshot
                # EVERY SECOND for as long as a prior-session position stayed open —
                # around the clock, on the same loop that hosts the 100ms exit monitor.
                #
                # A prior-session open position is exactly the state this system reaches
                # when the 15:25 force exit fails (NRML does not auto-square-off), i.e.
                # the one where exits matter most. A dedicated PositionSync thread
                # already reconciles on its own adaptive cadence.
                already_marked = (
                    str(getattr(trade, "execution_status", "") or "").upper()
                    == "RECOVERY_REQUIRED"
                )
                trade.execution_status = "RECOVERY_REQUIRED"
                trade.execution_error = "Previous-session trade requires broker reconciliation"
                if not already_marked:
                    marked += 1
        if marked:
            self.save_state()
            try:
                self.reconcile_broker_positions()
            except Exception as exc:
                logger.error(f"Stale-position reconciliation failed: {exc}")
        return marked

    def update_stops(self, prices: Dict[str, float]):
        """Update trailing stops for all open trades based on favor-movements"""
        with self._lock:
            trades_snapshot = list(self.open_trades.items())
            
        for tid, trade in trades_snapshot:
            spot = prices.get(trade.instrument.split(' ')[0])
            if not spot: continue
            
            # Update current price for real-time PnL tracking
            self.update_trade(tid, spot)
            
            # Simple Trailing logic: if price moves in favor, trail the stop
            # Note: This is a backup; MTF usually manages this, but we keep it for safety.
            pass

    def get_dashboard_payload(self, is_historical: bool = False, backtest_days: int = 1, inst_pref: str = "AUTO") -> Dict:
        """Get filtered trades and summary based on active mode"""
        now_ist = datetime.now(IST)
        # If it's before 9:00 AM IST, we are likely reviewing the PREVIOUS day's session
        if now_ist.hour < 9:
            today = (now_ist - timedelta(days=1)).date()
        else:
            today = now_ist.date()
        
        # 1. Unified Session Identification
        today_str = today.strftime('%Y-%m-%d')
        
        def trade_session_date(trade: Trade) -> Optional[date]:
            value = getattr(trade, "entry_time", None)
            if value is None:
                return None
            try:
                import pandas as pd
                ts = pd.Timestamp(value)
                if ts.tzinfo is not None:
                    ts = ts.tz_convert(IST).tz_localize(None)
                return ts.date()
            except Exception:
                return None

        with self._lock:
            # Watch-only ghost records are represented by the scanner's session
            # signal ledger. They are not broker trades and must not affect the
            # trade-manager table, P&L, W/L, or trade count.
            snapshot_open = [
                trade for trade in self.open_trades.values()
                if not getattr(trade, "is_ghost", False)
            ]
            snapshot_closed = [
                trade for trade in self.closed_trades
                if not getattr(trade, "is_ghost", False)
            ]

        if not is_historical:
            # Live/manual mode must not project older sessions as current trades.
            session_closed = [t for t in snapshot_closed if trade_session_date(t) == today]
        else:
            all_trades = snapshot_closed + snapshot_open
            unique_dates = sorted({d for d in (trade_session_date(t) for t in all_trades) if d is not None})
            if unique_dates:
                cutoff_date = unique_dates[-min(len(unique_dates), backtest_days)]
                session_closed = [t for t in snapshot_closed if (trade_session_date(t) or date.min) >= cutoff_date]
            else:
                session_closed = []
            
        # 2. Get Open Trades
        if not is_historical:
            open_trades = [t for t in snapshot_open if trade_session_date(t) == today]
        else:
            def close_intraday_projection(trade: Trade) -> Trade:
                entry_time = trade.entry_time
                if entry_time.tzinfo is None:
                    entry_time = IST.localize(entry_time)
                else:
                    entry_time = entry_time.astimezone(IST)

                exit_price = trade.current_price if trade.current_price > 0 else trade.entry_price
                from config.settings import get_settings
                settings = get_settings()
                from engine.trade_accounting import estimate_trade_charges
                charges = trade.charges or estimate_trade_charges(
                    trade.entry_price,
                    exit_price,
                    trade.quantity,
                    trade.inst_type,
                    settings,
                    trade.instrument_multiplier,
                    instrument=trade.instrument,
                )
                if trade.inst_type == "OPT" or trade.direction == "LONG":
                    gross = (exit_price - trade.entry_price) * trade.quantity
                else:
                    gross = (trade.entry_price - exit_price) * trade.quantity

                projected = replace(trade)
                projected.status = "CLOSED"
                from config.settings import get_settings
                force_exit = self._parse_time(
                    str(getattr(get_settings(), "ut_force_exit_time", "15:25") or "15:25")
                )
                projected.exit_time = IST.localize(datetime.combine(entry_time.date(), force_exit))
                projected.exit_reason = "SESSION_END"
                projected.exit_price = exit_price
                projected.current_price = exit_price
                projected.charges = charges
                projected.pnl = gross - charges
                return projected

            filtered_open = []
            try:
                if 'unique_dates' in locals() and unique_dates:
                    filtered_open = [t for t in snapshot_open if (trade_session_date(t) or date.min) >= cutoff_date]
                else:
                    filtered_open = snapshot_open
            except Exception:
                filtered_open = snapshot_open
                
            session_closed = session_closed + [close_intraday_projection(t) for t in filtered_open]
            open_trades = []

        pref = (inst_pref or "AUTO").upper()
        if pref in {"FUT", "OPT"}:
            session_closed = [t for t in session_closed if (getattr(t, "inst_type", "FUT") or "FUT").upper() == pref]
            open_trades = [t for t in open_trades if (getattr(t, "inst_type", "FUT") or "FUT").upper() == pref]

        display_closed = self._filter_concurrency_compliant_closed(session_closed)
        display_closed = [t for t in display_closed if self._valid_closed_chronology(t)]
        display_closed = [t for t in display_closed if t.status == "CLOSED"]

        # Headline table = shadow-free + deduped (same set as get_summary).
        # Shadows remain only in summary.futures_shadow telemetry.
        from engine.trade_row_adapter import hygiene_closed_rows

        headline_closed, _ = hygiene_closed_rows(display_closed)

        # 3. Calculate Summary based on the EXACT same session pool
        summary = self.get_summary(filtered_trades=display_closed, open_trades=open_trades)

        # 4. Format already-filtered trades for UI.
        all_formatted = build_dashboard_rows(
            self,
            open_trades,
            headline_closed,
            include_orphans=not is_historical,
        )
        
        return {
            "open": [t for t in all_formatted if t["status"] in {"OPEN", "ORPHANED"}],
            "closed": [t for t in all_formatted if t["status"] == "CLOSED"],
            "summary": summary,
            "analytics": self._build_session_analytics(display_closed, open_trades),
        }

    def get_open_trades_list(self) -> List[Dict]:
        """Backward compatibility for existing API calls"""
        payload = self.get_dashboard_payload(is_historical=False)
        return payload["open"] + payload["closed"]



    def _grade_rank(self, grade: str) -> int:
        base = str(grade or "C").split()[0]
        return {"C": 0, "B": 1, "B+": 2, "A": 3, "A+": 4, "Recovered": 4}.get(base, 0)

    def _is_aplus_setup(self, trade: Trade) -> bool:
        # Delegates to the single definition in engine.signal_quality_policy so this and
        # scanner.py cannot drift apart (the expression was duplicated verbatim).
        from engine.signal_quality_policy import is_top_tier_setup

        return is_top_tier_setup(getattr(trade, "grade", ""), getattr(trade, "confidence", 0.0))

    def _valid_closed_chronology(self, trade: Trade) -> bool:
        if trade.status != "CLOSED" or not trade.entry_time or not trade.exit_time:
            return True
        entry_ts = trade.entry_time.replace(tzinfo=None) if trade.entry_time.tzinfo else trade.entry_time
        exit_ts = trade.exit_time.replace(tzinfo=None) if trade.exit_time.tzinfo else trade.exit_time
        return exit_ts > entry_ts

    def _filter_concurrency_compliant_closed(self, trades: List[Trade]) -> List[Trade]:
        """Apply live-style overlap rules to historical/session display metrics."""
        try:
            from config.settings import get_settings
            if not _setting_enabled(getattr(get_settings(), "ut_concurrency_guard", True), True):
                return sorted(
                    trades,
                    key=lambda t: (
                        t.entry_time.replace(tzinfo=None) if getattr(t.entry_time, "tzinfo", None) else t.entry_time,
                        str(getattr(t, "id", "") or ""),
                    ),
                    reverse=True,
                )
        except Exception:
            pass

        accepted: List[Trade] = []
        removed = {"same_index_cap": 0, "same_index_overlap": 0, "correlated_cap": 0}
        try:
            settings = get_settings()
            adaptive_dual_lanes = (
                str(getattr(settings, "scanner_engine", "") or "").lower() == "adaptive_dual"
                and _setting_enabled(getattr(settings, "dual_lane_enabled", True), True)
            )
        except Exception:
            adaptive_dual_lanes = False

        def is_futures_shadow(trade: Trade) -> bool:
            return (
                str(getattr(trade, "trading_symbol", "") or "").upper() == "FUT_SHADOW"
                or "FUT SHADOW" in str(getattr(trade, "grade", "") or "").upper()
            )

        def is_dual_lane_pair(left: Trade, right: Trade) -> bool:
            if not adaptive_dual_lanes:
                return False
            lanes = {
                str(getattr(left, "source_lane", "") or "").upper(),
                str(getattr(right, "source_lane", "") or "").upper(),
            }
            return lanes == {"A", "B"}

        ordered = sorted(
            trades,
            key=lambda t: (
                t.entry_time.replace(tzinfo=None) if getattr(t.entry_time, "tzinfo", None) else t.entry_time,
                -float(getattr(t, "confidence", 0.0) or 0.0),
                1 if is_futures_shadow(t) else 0,
                str(getattr(t, "instrument", "") or ""),
                str(getattr(t, "timeframe", "") or ""),
                str(getattr(t, "inst_type", "") or ""),
                str(getattr(t, "id", "") or ""),
            ),
        )

        for trade in ordered:
            entry = trade.entry_time.replace(tzinfo=None) if getattr(trade.entry_time, "tzinfo", None) else trade.entry_time
            active = []
            for existing in accepted:
                exit_time = existing.exit_time or existing.entry_time
                ex_entry = existing.entry_time.replace(tzinfo=None) if getattr(existing.entry_time, "tzinfo", None) else existing.entry_time
                ex_exit = exit_time.replace(tzinfo=None) if getattr(exit_time, "tzinfo", None) else exit_time
                if ex_entry <= entry < ex_exit:
                    active.append(existing)

            same_index = [t for t in active if t.instrument.split()[0] == trade.instrument.split()[0]]
            if len(same_index) >= 2:
                removed["same_index_cap"] += 1
                continue
            if same_index:
                existing = same_index[0]
                same_tf = getattr(existing, "timeframe", "") == getattr(trade, "timeframe", "")
                same_type = getattr(existing, "inst_type", "FUT") == getattr(trade, "inst_type", "FUT")
                higher_grade = self._grade_rank(trade.grade) > self._grade_rank(existing.grade) or self._is_aplus_setup(trade)
                if same_tf or same_type or not higher_grade:
                    if not is_dual_lane_pair(existing, trade):
                        removed["same_index_overlap"] += 1
                        continue

            same_dir = [t for t in active if t.direction == trade.direction]
            if len(same_dir) >= 2:
                all_aplus = all(self._is_aplus_setup(t) for t in same_dir) and self._is_aplus_setup(trade)
                if not all_aplus:
                    removed["correlated_cap"] += 1
                    continue

            accepted.append(trade)

        summary_key = (len(trades), len(accepted), tuple(removed.values()))
        now = time.time()
        if (
            summary_key != getattr(self, "_last_concurrency_filter_log_key", None)
            or now - getattr(self, "_last_concurrency_filter_log_time", 0.0) >= 60.0
        ):
            logger.info(
                "Historical Concurrency Guard: "
                f"accepted={len(accepted)}/{len(trades)}, removed={sum(removed.values())} "
                f"(index_cap={removed['same_index_cap']}, "
                f"overlap={removed['same_index_overlap']}, "
                f"correlated_cap={removed['correlated_cap']})."
            )
            self._last_concurrency_filter_log_key = summary_key
            self._last_concurrency_filter_log_time = now

        return sorted(
            accepted,
            key=lambda t: (
                t.entry_time.replace(tzinfo=None) if getattr(t.entry_time, "tzinfo", None) else t.entry_time,
                str(getattr(t, "id", "") or ""),
            ),
            reverse=True,
        )

    def _build_session_analytics(self, closed_trades: List[Trade], open_trades: List[Trade]) -> Dict:
        def bucketize(trades, key_fn):
            buckets = {}
            for t in trades:
                key = key_fn(t) or "--"
                bucket = buckets.setdefault(key, {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
                pnl = float(getattr(t, "pnl", 0.0) or 0.0)
                bucket["count"] += 1
                bucket["pnl"] += pnl
                if pnl > 0:
                    bucket["wins"] += 1
                elif pnl < 0:
                    bucket["losses"] += 1
            for bucket in buckets.values():
                bucket["pnl"] = round(bucket["pnl"], 0)
                closed_count = bucket["wins"] + bucket["losses"]
                bucket["win_rate"] = round((bucket["wins"] / closed_count) * 100, 1) if closed_count else 0.0
            return dict(sorted(buckets.items(), key=lambda item: abs(item[1]["pnl"]), reverse=True))

        closed = list(closed_trades or [])
        return {
            "by_timeframe": bucketize(closed, lambda t: getattr(t, "timeframe", "--")),
            "by_grade": bucketize(closed, lambda t: str(getattr(t, "grade", "--")).split()[0]),
            "by_instrument": bucketize(closed, lambda t: str(getattr(t, "instrument", "--")).split()[0]),
            "by_type": bucketize(closed, lambda t: getattr(t, "inst_type", "--")),
            "by_exit_reason": bucketize(closed, lambda t: getattr(t, "exit_reason", "--")),
            # Price-action advisory: ADVERSE bucket's pnl = what blocking them would change.
            "by_price_action": bucketize(closed, lambda t: getattr(t, "price_action_verdict", "") or "--"),
            "open_by_type": bucketize(list(open_trades or []), lambda t: getattr(t, "inst_type", "--")),
        }

    def get_summary(self, filtered_trades: Optional[List] = None, open_trades: Optional[List] = None) -> Dict:
        """Get comprehensive performance analytics for dashboard"""
        closed = list(filtered_trades) if filtered_trades is not None else list(self.closed_trades)
        closed = [
            trade for trade in closed
            if getattr(trade, "execution_status", "") != "REJECTED"
        ]

        # ── Backtest-sweep hygiene (2026-07-09 FUT-vs-OPT audit) ──
        # Shared with dashboard rebuild so headline P&L never re-includes
        # FUT_SHADOW companions or multi-engine exact replay dups.
        from engine.trade_row_adapter import hygiene_closed_rows

        closed, _hygiene = hygiene_closed_rows(closed)
        shadow_pnl = float((_hygiene.get("futures_shadow") or {}).get("pnl") or 0.0)
        replay_excess_rows = int((_hygiene.get("engine_replay") or {}).get("excess_rows") or 0)
        replay_excess_pnl = float((_hygiene.get("engine_replay") or {}).get("excess_pnl") or 0.0)
        shadow_rows_n = int((_hygiene.get("futures_shadow") or {}).get("rows") or 0)
        raw_rows_n = int((_hygiene.get("engine_replay") or {}).get("raw_rows") or 0)

        open_for_summary = list(open_trades) if open_trades is not None else list(self.open_trades.values())
        closed_total = len(closed)
        total = closed_total + len(open_for_summary)
        
        wins = sum(1 for t in closed if t.pnl > 0)
        losses = sum(1 for t in closed if t.pnl < 0)
        
        win_rate = (wins / closed_total * 100) if closed_total > 0 else 0.0
        
        # Profit Factor
        total_profit = sum(t.pnl for t in closed if t.pnl > 0)
        total_loss = abs(sum(t.pnl for t in closed if t.pnl < 0))
        profit_factor = (total_profit / total_loss) if total_loss > 0 else (total_profit if total_profit > 0 else 1.0)
        
        # Daily/base Sharpe uses sample std; yearly is annualized for display.
        sharpe = 0.0
        if closed_total > 2:
            import numpy as np
            pnl_list = [t.pnl for t in closed]
            avg = np.mean(pnl_list)
            std = np.std(pnl_list, ddof=1)
            sharpe = (avg / std) if std > 0 else 0.0
        sharpe_yearly = annualize_sharpe(sharpe)
            
        # Max Drawdown
        max_dd = 0.0
        peak = 0.0
        running_equity = 0.0
        for t in closed:
            running_equity += t.pnl
            peak = max(peak, running_equity)
            max_dd = max(max_dd, peak - running_equity)

        # Calculate P&L dynamically (Closed + Open Unrealized)
        now_ist = datetime.now(IST)
        if now_ist.hour < 9:
            today_str = (now_ist - timedelta(days=1)).strftime('%Y-%m-%d')
        else:
            today_str = now_ist.strftime('%Y-%m-%d')
        
        def is_option_trade(trade) -> bool:
            trading_symbol = str(getattr(trade, "trading_symbol", "") or "").upper()
            instrument = str(getattr(trade, "instrument", "") or "").upper()
            return (
                str(getattr(trade, "inst_type", "") or "").upper() == "OPT"
                or "CE" in trading_symbol
                or "PE" in trading_symbol
                or "CE" in instrument
                or "PE" in instrument
            )

        total_pnl = sum(t.pnl for t in closed)
        fut_pnl = sum(t.pnl for t in closed if not is_option_trade(t))
        opt_pnl = sum(t.pnl for t in closed if is_option_trade(t))
        
        fut_trades = sum(1 for t in closed if not is_option_trade(t))
        opt_trades = sum(1 for t in closed if is_option_trade(t))

        for ot in open_for_summary:
            try:
                is_opt = is_option_trade(ot)
                if is_opt:
                    opt_trades += 1
                else:
                    fut_trades += 1

                if ot.entry_time.strftime('%Y-%m-%d') == today_str:
                    unrealized = getattr(ot, "unrealized_pnl", 0.0) or 0.0
                    total_pnl += unrealized
                    if is_opt:
                        opt_pnl += unrealized
                    else:
                        fut_pnl += unrealized
            except Exception:
                pass
        
        return {
            "daily_pnl": round(total_pnl, 0),
            "fut_pnl": round(fut_pnl, 0),
            "opt_pnl": round(opt_pnl, 0),
            "total_trades": total,
            "fut_trades": fut_trades,
            "opt_trades": opt_trades,
            "open_count": len(open_for_summary),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "sharpe_ratio_daily": sharpe,
            "sharpe_ratio_yearly": sharpe_yearly,
            "sharpe_ratio_display": format_sharpe_pair(sharpe, sharpe_yearly),
            "max_drawdown": max_dd,
            # Sweep hygiene telemetry (headline above is shadow-free + deduped):
            "futures_shadow": {
                "rows": shadow_rows_n,
                "pnl": round(shadow_pnl, 0),
            },
            "engine_replay": {
                "raw_rows": raw_rows_n,
                "unique_rows": len(closed),
                "excess_rows": replay_excess_rows,
                "excess_pnl": round(replay_excess_pnl, 0),
            },
        }

