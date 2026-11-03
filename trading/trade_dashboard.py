"""Dashboard serialization helpers for trade-manager state."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List

import pandas as pd
import pytz


IST = pytz.timezone("Asia/Kolkata")

# Longest first: BANKNIFTY must not be matched as NIFTY.
_INDEX_PREFIXES = ("MIDCPNIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX", "NIFTY")


def option_strike_from_symbol(symbol: str) -> float:
    """Strike out of a Kite index-option tradingsymbol, or 0.0 when unreadable.

    There are two live formats and the naive "first run of digits" reads the EXPIRY
    YEAR out of the monthly one, which is how an orphan row came to display as
    "25CE"/"26CE" instead of its real strike:

        monthly   NIFTY26AUG24350CE   -> 26 AUG   strike 24350
        weekly    NIFTY2681824350CE   -> 26 8 18  strike 24350

    The weekly month is one character: 1-9 for Jan-Sep, then O/N/D. Validated against
    every index option in the contract master (7,220 symbols across all six indices,
    zero mismatches). Stock options are deliberately not handled — this system trades
    index derivatives only, and 0.0 is the honest answer for anything else.
    """
    text = str(symbol or "").upper().strip()
    rest = None
    for prefix in sorted(_INDEX_PREFIXES, key=len, reverse=True):
        if text.startswith(prefix):
            rest = text[len(prefix):]
            break
    if rest is None or not rest.endswith(("CE", "PE")):
        return 0.0
    body = rest[:-2]
    match = re.match(r"^\d{2}[A-Z]{3}(\d+)$", body)      # monthly
    if match:
        return float(match.group(1))
    match = re.match(r"^\d{2}[1-9OND]\d{2}(\d+)$", body)  # weekly
    if match:
        return float(match.group(1))
    return 0.0


def _index_base_name(symbol: str) -> str:
    text = str(symbol or "").upper().strip()
    for prefix in sorted(_INDEX_PREFIXES, key=len, reverse=True):
        if text.startswith(prefix):
            return prefix
    return text


def _to_ist_naive(value: Any) -> datetime | None:
    if value is None:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        return timestamp.tz_convert(IST).tz_localize(None).to_pydatetime()
    return timestamp.to_pydatetime()


def _entry_sort_value(trade: Any) -> datetime:
    return pd.to_datetime(trade.entry_time).tz_localize(None)


def _serialize_trade_row(manager: Any, trade: Any) -> Dict[str, Any]:
    display_name = trade.instrument
    if trade.inst_type == "OPT" and trade.atm_strike is not None and trade.atm_strike > 0:
        display_name = f"{trade.instrument} {int(trade.atm_strike)} {trade.option_type}"

    display_stop = trade.current_stop
    display_target = trade.target
    if trade.inst_type == "OPT" and trade.entry_spot is not None and trade.entry_spot > 0:
        looks_spot_stop = trade.current_stop is not None and trade.current_stop > (trade.entry_price * 5.0)
        looks_spot_target = trade.target is not None and trade.target > (trade.entry_price * 5.0)
        if looks_spot_stop or looks_spot_target:
            safe_stop = trade.current_stop if trade.current_stop is not None else trade.entry_price
            safe_target = trade.target if trade.target is not None else trade.entry_price
            stop_distance = abs(trade.entry_spot - safe_stop)
            target_distance = abs(safe_target - trade.entry_spot)
            multiplier = abs(trade.instrument_multiplier)
            display_stop = max(0.1, trade.entry_price - (stop_distance * multiplier))
            display_target = trade.entry_price + (target_distance * multiplier)
            risk_stop = trade.entry_price - (trade.entry_price * (manager.options_sl_pct / 100.0))
            display_stop = max(display_stop, risk_stop)

    entry_display = _to_ist_naive(trade.entry_time)
    signal_display = _to_ist_naive(getattr(trade, "signal_time", None))
    exit_display = _to_ist_naive(trade.exit_time)
    current_price = trade.current_price
    if trade.status == "CLOSED":
        current_price = (
            trade.current_price
            if trade.current_price is not None
            and trade.current_price > 0
            and abs(trade.current_price - trade.exit_price) > 1e-9
            else 0.0
        )

    return {
        "id": trade.id,
        # The signal tracker and TradeManager describe the same executed idea in
        # different stores.  Preserve their canonical join key so the payload builder
        # can prefer the broker-backed row instead of rendering both as two trades.
        "lifecycle_id": str(getattr(trade, "lifecycle_id", "") or trade.id or ""),
        "status": trade.status,
        "execution_status": trade.execution_status,
        "execution_error": trade.execution_error,
        "pending_execution": trade.pending_execution,
        "pending_exit": trade.pending_exit,
        "broker_order_id": trade.broker_order_id,
        "exit_order_id": trade.exit_order_id,
        "broker_quantity": trade.broker_quantity,
        "instrument": display_name,
        "direction": trade.direction,
        "entry_price": trade.entry_price,
        "current_price": current_price,
        "exit_price": trade.exit_price if trade.status == "CLOSED" else 0.0,
        "trailing_stop": round(display_stop, 2) if display_stop is not None else 0.0,
        "target": round(display_target, 2) if display_target is not None and display_target > 0 else 0,
        "pnl": round(trade.pnl, 2) if trade.pnl is not None else 0.0,
        "lots": trade.lots,
        "lot_size": trade.lot_size,
        "grade": trade.grade,
        "confidence": round(trade.confidence, 2) if trade.confidence is not None else 0.0,
        "rr_ratio": trade.rr_ratio,
        "entry_policy_actual_rr": float(getattr(trade, "entry_policy_actual_rr", 0.0) or 0.0),
        "atm_strike": trade.atm_strike,
        "option_type": trade.option_type,
        "inst_type": trade.inst_type,
        "timeframe": trade.timeframe,
        "entry_time": entry_display.strftime("%H:%M:%S") if entry_display else "--",
        "entry_date": entry_display.strftime("%d %b %y") if entry_display else "--",
        "entry_timestamp": IST.localize(entry_display).timestamp() if entry_display else 0,
        "signal_timestamp": IST.localize(signal_display).isoformat() if signal_display else "",
        "source_signal_timestamp": IST.localize(signal_display).isoformat() if signal_display else "",
        "exit_time": exit_display.strftime("%H:%M:%S") if exit_display else None,
        "exit_date": exit_display.strftime("%d %b %y") if exit_display else None,
        "exit_timestamp": IST.localize(exit_display).isoformat() if exit_display else "",
        "exit_reason": trade.exit_reason,
        "exec_type": trade.exec_type,
        "source_engine": str(getattr(trade, "source_engine", "") or ""),
        "source_engines": list(getattr(trade, "source_engines", []) or []),
        "source_lane": str(getattr(trade, "source_lane", "") or ""),
        "trading_symbol": str(getattr(trade, "trading_symbol", "") or ""),
        "data_provenance": str(getattr(trade, "data_provenance", "") or ""),
        "market_regime": str(getattr(trade, "market_regime", "") or ""),
        "entry_intelligence_score": float(getattr(trade, "entry_intelligence_score", 0.0) or 0.0),
        "initial_stop": float(getattr(trade, "initial_stop", 0.0) or 0.0),
        "charges": float(getattr(trade, "charges", 0.0) or 0.0),
        "replay_input": next((dict(row) for row in getattr(trade, "replay_exit_trace", [])
                              if row.get("guard") == "replay_input"), {}),
    }


def _serialize_orphan_position(manager: Any, symbol: str, position: Dict[str, Any]) -> Dict[str, Any]:
    quantity = manager._position_quantity(position)
    return {
        "id": f"ORPHAN|{symbol}",
        "status": "ORPHANED",
        "execution_status": "ORPHANED",
        "execution_error": "Broker position has no matching local trade",
        "pending_execution": False,
        "pending_exit": False,
        "broker_order_id": "",
        "exit_order_id": "",
        "broker_quantity": abs(quantity),
        "instrument": symbol,
        # The unambiguous identity. Without it the UI had only `instrument` to work
        # from and guessed the strike out of it with a regex.
        "trading_symbol": symbol,
        "base_instrument": _index_base_name(symbol),
        "direction": "LONG" if quantity > 0 else "SHORT",
        # `average_price` added 2026-08-16. Kite reports the day's buy/sell prices as 0
        # for a position CARRIED from a previous session — the buy did not happen today
        # — so a carried orphan rendered with a blank entry price beside a populated
        # P&L, which reads as a fabricated number. `average_price` is the net position's
        # own average and is populated in exactly that case.
        "entry_price": float(
            position.get("buyavgprice")
            or position.get("sellavgprice")
            or position.get("average_price")
            or 0.0
        ),
        # `ltp` is the AngelOne name. Kite sends `last_price`, and get_positions maps
        # buyavgprice/sellavgprice/producttype/netqty but never mapped this one, so the
        # orphan row's current price was ALWAYS 0.0.
        "current_price": float(
            position.get("ltp") or position.get("last_price") or 0.0
        ),
        "exit_price": 0.0,
        "trailing_stop": 0.0,
        "target": 0.0,
        "pnl": float(position.get("unrealised") or position.get("pnl") or 0.0),
        "lots": 0,
        "lot_size": 0,
        "grade": "RECOVERY",
        "confidence": 0.0,
        "rr_ratio": 0.0,
        # Was a hard 0.0, which is FALSY in JS — so trade-table.js fell through to its
        # "first run of digits in the symbol" fallback and rendered the expiry YEAR as
        # the strike ("25CE" for a 2025-dated contract, "26CE" for a 2026 one).
        "atm_strike": option_strike_from_symbol(symbol),
        "option_type": "CE" if "CE" in symbol else ("PE" if "PE" in symbol else ""),
        "inst_type": "OPT" if "CE" in symbol or "PE" in symbol else "FUT",
        "timeframe": "--",
        "entry_time": "--",
        "entry_date": "--",
        "entry_timestamp": 0,
        "exit_time": None,
        "exit_date": None,
        "exit_timestamp": "",
        "exit_reason": "BROKER_ORPHAN",
        "exec_type": "B",
        "source_engine": "",
        "source_engines": [],
        "source_lane": "",
    }


def build_dashboard_rows(
    manager: Any,
    open_trades: Iterable[Any],
    closed_trades: Iterable[Any],
    include_orphans: bool,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    rows = [
        _serialize_trade_row(manager, trade)
        for trade in sorted(
            list(open_trades) + list(closed_trades),
            key=_entry_sort_value,
            reverse=True,
        )[:limit]
    ]
    if include_orphans:
        rows.extend(
            _serialize_orphan_position(manager, symbol, position)
            for symbol, position in sorted(manager.orphan_positions.items())
        )
    return rows
