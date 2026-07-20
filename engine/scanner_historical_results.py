"""Historical dashboard trade/signal reconstruction helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict

from config.settings import get_settings
from engine.trade_row_adapter import (
    accounting_pnl,
    is_option_dashboard_row,
    is_simulated_history_row,
    normalize_dashboard_accounting_row,
    trade_row_to_signal,
)
from trading.trade_manager import IST


class HistoricalTradeResultBuilder:
    """Restore historical signal rows from session candidates and trade rows."""

    def __init__(
        self,
        scanner: Any,
        payload: Dict[str, Any],
        inst_pref_match: Callable[[Any], bool],
        reconcile_display_row: Callable[[Dict[str, Any]], Dict[str, Any]],
        row_event_seconds: Callable[[Dict[str, Any]], float],
    ) -> None:
        self.scanner = scanner
        self.payload = payload
        self.inst_pref_match = inst_pref_match
        self.reconcile_display_row = reconcile_display_row
        self.row_event_seconds = row_event_seconds

    def apply(self) -> Dict[str, Any]:
        scanner = self.scanner
        payload = self.payload
        inst_pref_match = self.inst_pref_match
        reconcile_display_row = self.reconcile_display_row
        row_event_seconds = self.row_event_seconds
        session_rows = []
        for instrument in (getattr(scanner, "_session_trade_candidates", {}) or {}).keys():
            for candidate in scanner._get_session_trade_candidates(instrument):
                if inst_pref_match(candidate):
                    action = str(
                        getattr(
                            candidate,
                            "action",
                            candidate.get("action", "ENTRY") if isinstance(candidate, dict) else "ENTRY",
                        )
                        or "ENTRY"
                    ).upper()
                    if (
                        action == "EXIT"
                        and not isinstance(candidate, dict)
                        and hasattr(scanner, "_normalize_loaded_exit_candidate")
                    ):
                        scanner._normalize_loaded_exit_candidate(candidate)
                    session_rows.append(
                        normalize_dashboard_accounting_row(
                            scanner._serialize_trade_candidate(candidate)
                        )
                    )

        filtered_rows = []
        for rows in (getattr(scanner, "_latest_filtered_candidates", {}) or {}).values():
            for candidate in rows:
                if inst_pref_match(candidate):
                    filtered_rows.append(
                        normalize_dashboard_accounting_row(
                            scanner._serialize_trade_candidate(candidate)
                        )
                    )

        if session_rows:
            # In HISTORICAL mode, merge ENTRY and EXIT rows to show complete trades
            # Only show ENTRY rows with exit info merged from matching EXIT rows
            entry_rows = [r for r in session_rows if r.get("action") == "ENTRY"]
            exit_rows = [r for r in session_rows if r.get("action") == "EXIT"]

            # Build exit lookup by (instrument, timeframe, direction, timestamp)
            exit_lookup = {
                (
                    r.get("instrument"),
                    r.get("timeframe"),
                    r.get("direction"),
                    r.get("timestamp"),
                ): r
                for r in exit_rows
            }

            def close_historical_entry_row(entry: Dict[str, Any]) -> Dict[str, Any]:
                row = dict(entry)
                entry_px = float(row.get("price") or 0.0)
                exit_px = float(row.get("current_price") or row.get("exit_price") or entry_px or 0.0)
                qty = int(row.get("lots") or 1) * int(row.get("lot_size") or 1)
                inst_type = str(row.get("inst_type") or "FUT").upper()
                direction = str(row.get("direction") or "LONG").upper()
                if inst_type == "OPT" or direction == "LONG":
                    gross = (exit_px - entry_px) * qty
                else:
                    gross = (entry_px - exit_px) * qty
                settings = get_settings()
                charges = float(settings.opt_cost) if inst_type == "OPT" else float(settings.fut_cost)

                exit_ts = ""
                raw_ts = row.get("timestamp") or row.get("signal_timestamp")
                if raw_ts:
                    try:
                        entry_dt = datetime.fromisoformat(str(raw_ts))
                        if entry_dt.tzinfo is None:
                            entry_dt = IST.localize(entry_dt)
                        else:
                            entry_dt = entry_dt.astimezone(IST)
                        force_exit = scanner._configured_session_time("ut_force_exit_time", "15:25")
                        exit_ts = IST.localize(datetime.combine(entry_dt.date(), force_exit)).isoformat()
                    except Exception:
                        exit_ts = "15:25:00"

                row.update({
                    "current_price": exit_px,
                    "exit_price": exit_px,
                    "status": "EXIT SIGNAL",
                    "action": "EXIT",
                    "is_exit": True,
                    "exit_reason": "SESSION_END",
                    "exit_timestamp": exit_ts or "15:25:00",
                    "pnl": round(gross - charges, 2),
                })
                return normalize_dashboard_accounting_row(row)

            # Active rows are entry rows that do not have a corresponding exit
            active_keys = set(exit_lookup.keys())
            active_rows = [
                entry for entry in entry_rows
                if (
                    entry.get("instrument"),
                    entry.get("timeframe"),
                    entry.get("direction"),
                    entry.get("timestamp"),
                ) not in active_keys
            ]

            # Merge exit info into entry rows
            merged_rows = []
            for entry in entry_rows:
                key = (
                    entry.get("instrument"),
                    entry.get("timeframe"),
                    entry.get("direction"),
                    entry.get("timestamp"),
                )
                exit_row = reconcile_display_row(exit_lookup.get(key)) if exit_lookup.get(key) else None
                if exit_row:
                    # Merge exit information
                    entry["exit_price"] = exit_row.get("exit_price", 0.0)
                    entry["exit_timestamp"] = exit_row.get("exit_timestamp", "")
                    entry["exit_reason"] = exit_row.get("exit_reason", "")
                    entry["pnl"] = exit_row.get("pnl", 0.0)
                    entry["accounting_pnl"] = exit_row.get("accounting_pnl", entry["pnl"])
                    if "raw_pnl" in exit_row:
                        entry["raw_pnl"] = exit_row.get("raw_pnl")
                    if exit_row.get("quote_invalid"):
                        entry["quote_invalid"] = True
                    entry["current_price"] = exit_row.get("exit_price", entry.get("current_price"))
                    entry["status"] = "EXIT SIGNAL"
                    entry["action"] = "EXIT"
                    entry["is_exit"] = True
                else:
                    entry = close_historical_entry_row(entry)
                merged_rows.append(entry)

            # --- CRITICAL BUG FIX FOR MULTI-DAY BACKTESTS ---
            # Also convert and append simulated trades from closed/open payload lists
            # whose dates are not already covered by the loaded session_rows.
            # This ensures multi-day backtests show ALL signals/trades, not just last day's!
            session_dates = set()
            for r in merged_rows:
                ts_str = r.get("timestamp") or r.get("signal_timestamp")
                if ts_str:
                    try:
                        session_dates.add(datetime.fromisoformat(str(ts_str)).date())
                    except Exception:
                        pass

            closed_trades = payload.get("closed", [])
            open_trades = payload.get("open", [])

            simulated_signals = []
            for row in open_trades + closed_trades:
                if not is_simulated_history_row(row):
                    continue

                entry_ts_sec = row.get("entry_timestamp", 0)
                entry_date = None
                if entry_ts_sec:
                    try:
                        entry_dt = datetime.fromtimestamp(entry_ts_sec, tz=IST)
                        entry_date = entry_dt.date()
                    except Exception:
                        pass

                # If this simulated trade's date is already covered by a session file, skip it
                if entry_date and entry_date in session_dates:
                    continue

                simulated_signals.append(trade_row_to_signal(row))

            all_signals = [
                normalize_dashboard_accounting_row(reconcile_display_row(r))
                for r in (merged_rows + simulated_signals)
            ]
            all_signals = sorted(all_signals, key=lambda x: x.get("timestamp") or "", reverse=True)
            # These are completed trade rows after ENTRY/EXIT merge. Keep them in
            # `closed` only; mirroring them into `signals` makes the dashboard render
            # one signal-style row plus one exit row for every lifecycle.
            payload["signals"] = []
            payload["closed"] = all_signals
            payload["open"] = []
            payload["meta"] = {
                **payload.get("meta", {}),
                "open_rows": 0,
                "closed_rows": len(all_signals),
                "restored_signal_rows": len(merged_rows),
                "display_signal_rows": 0,
                "simulated_signal_rows": len(simulated_signals),
                "source": "historical_session_plus_simulation",
            }
            accounting_rows = all_signals

            # Recalculate summary metrics for the combined list of signals
            pnls = [accounting_pnl(r) for r in accounting_rows]
            wins = len([p for p in pnls if p > 0])
            losses = len([p for p in pnls if p < 0])
            gross_win = sum(p for p in pnls if p > 0)
            gross_loss = abs(sum(p for p in pnls if p < 0))

            open_count = len([r for r in accounting_rows if r.get("action") == "ENTRY"])

            payload["summary"] = {
                **(payload.get("summary") or {}),
                "daily_pnl": round(sum(pnls), 2),
                "fut_pnl": round(sum(accounting_pnl(r) for r in accounting_rows if not is_option_dashboard_row(r)), 2),
                "opt_pnl": round(sum(accounting_pnl(r) for r in accounting_rows if is_option_dashboard_row(r)), 2),
                "fut_trades": len([r for r in accounting_rows if not is_option_dashboard_row(r)]),
                "opt_trades": len([r for r in accounting_rows if is_option_dashboard_row(r)]),
                "total_trades": len(accounting_rows),
                "open_count": open_count,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / len(accounting_rows)) * 100, 1) if accounting_rows else 0.0,
                "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.99 if gross_win > 0 else 1.0),
            }
        else:
            # No real session candidates - fall back to reconstructed closed trades
            closed_trades = payload.get("closed", [])
            open_trades = payload.get("open", [])
            all_trades = open_trades + closed_trades

            all_trades = sorted(all_trades, key=row_event_seconds, reverse=True)
            closed_count = len(closed_trades)

            hist_signal_rows = []
            for row in all_trades:
                hist_signal_rows.append(trade_row_to_signal(row))
            scanner._cached_hist_signals = hist_signal_rows
            scanner._cached_hist_closed_count = closed_count
            if not hist_signal_rows and filtered_rows:
                filtered_rows = sorted(filtered_rows, key=lambda x: x.get("timestamp") or "", reverse=True)[:50]
                payload["signals"] = filtered_rows
                payload["meta"] = {
                    **payload.get("meta", {}),
                    "filtered_signal_rows": len(filtered_rows),
                    "source": "filtered_candidates",
                }
                payload["summary"] = {
                    **(payload.get("summary") or {}),
                    "total_trades": 0,
                    "open_count": 0,
                }
            else:
                payload["signals"] = scanner._cached_hist_signals
                payload["meta"] = {
                    **payload.get("meta", {}),
                    "simulated_signal_rows": len(scanner._cached_hist_signals),
                    "source": "historical_trade_manager",
                }
        return payload


def build_historical_trade_result(
    scanner: Any,
    payload: Dict[str, Any],
    inst_pref_match: Callable[[Any], bool],
    reconcile_display_row: Callable[[Dict[str, Any]], Dict[str, Any]],
    row_event_seconds: Callable[[Dict[str, Any]], float],
) -> Dict[str, Any]:
    """Apply historical signal reconstruction to the dashboard trade payload."""
    return HistoricalTradeResultBuilder(
        scanner=scanner,
        payload=payload,
        inst_pref_match=inst_pref_match,
        reconcile_display_row=reconcile_display_row,
        row_event_seconds=row_event_seconds,
    ).apply()
