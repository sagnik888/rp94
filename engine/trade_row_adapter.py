"""Compatibility adapters for persisted trade and dashboard signal rows."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz

from config.settings import get_settings


# Keep this low-level adapter side-effect free. Importing TradeManager here used
# to construct the global production SQLite singleton, which made read-only audit
# scripts run cleanup/VACUUM merely by importing the shared hygiene helpers.
IST = pytz.timezone("Asia/Kolkata")


def trade_row_id(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("id") or row.get("trade_id") or "")
    return str(getattr(row, "id", "") or getattr(row, "trade_id", "") or "")


def is_simulated_history_row(row: Dict[str, Any]) -> bool:
    return trade_row_id(row).startswith(("H_", "EOD_"))


def is_engine_replay_row(row: Any) -> bool:
    """True only for mutable/rebuildable historical engine output.

    The trades table contains two different kinds of records: session/live ledger
    rows and historical replay fixtures.  Exact outcomes from independent replay
    streams may be identical, but two real fills that happen to share the same
    prices and P&L are still two trades.  Keep that distinction in one place so an
    accounting caller can never silently collapse live history.

    Modern replay producers persist ``data_provenance=BACKTEST``.  The id prefixes
    remain the compatibility signal for rows created before that field existed.
    ``HISTORICAL`` is deliberately *not* sufficient: imported SESSION candidates
    use it too and are part of the session ledger, not an engine sweep.
    """
    provenance = _normalized_text(_row_attr(row, "data_provenance", ""), upper=True)
    return trade_row_id(row).startswith(("H_", "EOD_")) or provenance in {
        "BACKTEST",
        "ENGINE_REPLAY",
    }


def is_futures_shadow_row(row: Any) -> bool:
    """True for hypothetical FUT companion rows (must not enter headline P&L)."""
    if isinstance(row, dict):
        symbol = str(row.get("trading_symbol") or "").upper()
        grade = str(row.get("grade") or "").upper()
        tid = str(row.get("id") or row.get("trade_id") or "").upper()
    else:
        symbol = str(getattr(row, "trading_symbol", "") or "").upper()
        grade = str(getattr(row, "grade", "") or "").upper()
        tid = str(getattr(row, "id", "") or "").upper()
    if symbol == "FUT_SHADOW":
        return True
    if "FUT SHADOW" in grade or "FUT_SHADOW" in grade:
        return True
    # Historical shadow ids are "{opt_id}_FUT"
    if tid.endswith("_FUT") and ("SHADOW" in grade or symbol in {"", "FUT_SHADOW"}):
        return True
    return False


def _row_attr(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _normalized_text(value: Any, *, upper: bool = False) -> str:
    text = str(value or "").strip()
    return text.upper() if upper else text


def _normalized_number(value: Any, digits: int = 6) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def replay_dedup_key(row: Any) -> Tuple:
    """Collapse only materially identical multi-engine historical replays.

    IDs intentionally stay out of the signature because each engine may persist
    the same replay with a different ID. Contract-defining trade fields remain in
    the signature so separate strikes/symbols/fills in the same second are not
    accidentally discarded.
    """
    stamp = (
        _row_attr(row, "signal_time")
        or _row_attr(row, "signal_timestamp")
        or _row_attr(row, "entry_timestamp")
        or _row_attr(row, "entry_time")
        or _row_attr(row, "timestamp")
    )
    instrument = _normalized_text(_row_attr(row, "instrument", ""), upper=True)
    instrument_root = instrument.split()[0] if instrument.split() else ""
    return (
        instrument_root,
        _normalized_text(_row_attr(row, "timeframe", "")),
        _normalized_text(_row_attr(row, "direction", ""), upper=True),
        _normalized_text(_row_attr(row, "inst_type", ""), upper=True),
        _normalized_text(_row_attr(row, "trading_symbol", ""), upper=True),
        _normalized_text(_row_attr(row, "option_type", ""), upper=True),
        _normalized_number(_row_attr(row, "atm_strike"), 2),
        _normalized_number(_row_attr(row, "entry_price"), 6),
        _normalized_number(_row_attr(row, "exit_price"), 6),
        _normalized_number(_row_attr(row, "pnl", 0.0), 2),
        _normalized_number(_row_attr(row, "lots"), 0),
        _normalized_number(_row_attr(row, "lot_size"), 0),
        _normalized_text(_row_attr(row, "exit_reason", ""), upper=True),
        _normalized_text(stamp),
    )


def hygiene_closed_rows(
    rows: Iterable[Any],
) -> Tuple[List[Any], Dict[str, Any]]:
    """Shadow-free + replay-deduped set for dashboard/TM headline accounting.

    Deduplication is intentionally restricted to mutable engine-replay rows.  The
    former implementation applied the signature to every row, so two independent
    LIVE trades with identical prices, size, reason and timestamp were silently
    counted once.  Replay variants are rebuildable experiments; broker/session rows
    are ledger facts and may never be removed by a heuristic key.

    Returns (headline_rows, telemetry). Telemetry keeps shadow/replay counts for UI.
    """
    closed = list(rows or [])
    shadow_rows = [r for r in closed if is_futures_shadow_row(r)]
    non_shadow_rows = [r for r in closed if not is_futures_shadow_row(r)]
    replay_rows = [r for r in non_shadow_rows if is_engine_replay_row(r)]
    non_replay_rows = [r for r in non_shadow_rows if not is_engine_replay_row(r)]

    seen_replay: Dict[Tuple, Any] = {}
    headline: List[Any] = []
    replay_variant_rows: List[Any] = []
    # Preserve source order.  Several dashboard paths already present newest-first
    # rows and grouping all replay output at one end would reshuffle that display.
    for row in non_shadow_rows:
        if not is_engine_replay_row(row):
            headline.append(row)
            continue
        key = replay_dedup_key(row)
        if key in seen_replay:
            replay_variant_rows.append(row)
            continue
        seen_replay[key] = row
        headline.append(row)

    def _pnl(r: Any) -> float:
        try:
            return float(_row_attr(r, "pnl", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    shadow_pnl = sum(_pnl(r) for r in shadow_rows)
    replay_pnl = sum(_pnl(r) for r in replay_rows)
    distinct_replay_pnl = sum(_pnl(r) for r in seen_replay.values())
    head_pnl = sum(_pnl(r) for r in headline)
    telemetry = {
        "futures_shadow": {
            "rows": len(shadow_rows),
            "pnl": round(shadow_pnl, 2),
        },
        "engine_replay": {
            # Compatibility fields consumed by the dashboard summary.
            "raw_rows": len(non_shadow_rows) + len(shadow_rows),
            "unique_rows": len(headline),
            "excess_rows": len(replay_variant_rows),
            "excess_pnl": round(replay_pnl - distinct_replay_pnl, 2),
            # Unambiguous fields for auditors: these are equivalent outcomes from
            # replay streams, not duplicate session-ledger records.
            "candidate_rows": len(replay_rows),
            "distinct_outcomes": len(seen_replay),
            "stream_variant_rows": len(replay_variant_rows),
            "non_replay_rows": len(non_replay_rows),
        },
    }
    return headline, telemetry


def normalized_exit_reason(row: Dict[str, Any]) -> str:
    raw = row.get("exit_reason") or row.get("status") or ""
    return str(raw).upper().replace(" ", "_")


def is_quote_invalid_row(row: Dict[str, Any]) -> bool:
    return "QUOTE_INVALID" in normalized_exit_reason(row)


def accounting_pnl(row: Dict[str, Any]) -> float:
    if is_quote_invalid_row(row):
        return 0.0
    try:
        return float(row.get("accounting_pnl", row.get("pnl", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_rejected_entry_row(row: Dict[str, Any]) -> bool:
    """An entry the broker (or its margin pre-check) refused: nothing was ever filled.

    TradeManager.get_summary already leaves these out, but the dashboard rebuilds its
    headline from rows. On 2026-09-17 a MIDCPNIFTY entry refused by
    ENTRY_MARGIN_PREFLIGHT was counted as a closed losing trade carrying -Rs.80 of
    charges that were never incurred.
    """
    return str(row.get("execution_status") or "").upper() == "REJECTED"


def normalize_dashboard_accounting_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    if is_rejected_entry_row(row):
        row.setdefault("raw_pnl", row.get("pnl", 0.0))
        row["pnl"] = 0.0
        row["accounting_pnl"] = 0.0
        row["charges"] = 0.0
        row["accounting_excluded"] = "REJECTED"
        return row
    if is_quote_invalid_row(row):
        row.setdefault("raw_pnl", row.get("pnl", 0.0))
        row["pnl"] = 0.0
        row["accounting_pnl"] = 0.0
        row["quote_invalid"] = True
        if not row.get("exit_reason"):
            row["exit_reason"] = "QUOTE_INVALID"
    return row


def is_option_dashboard_row(row: Dict[str, Any]) -> bool:
    inst_type = str(row.get("inst_type") or "").upper()
    trading_symbol = str(row.get("trading_symbol") or "").upper()
    instrument = str(row.get("instrument") or "").upper()
    return inst_type == "OPT" or "CE" in trading_symbol or "PE" in trading_symbol or "CE" in instrument or "PE" in instrument


def normalized_row_inst_type(row: Dict[str, Any]) -> str:
    """Recover product identity from legacy option contract fields when needed."""
    inst_type = str(row.get("inst_type") or "").upper()
    if inst_type in {"OPT", "FUT"}:
        return inst_type
    return "OPT" if is_option_dashboard_row(row) else "FUT"


def trade_row_session_date(row: Dict[str, Any]):
    try:
        timestamp = row.get("entry_timestamp") or 0
        if timestamp:
            return datetime.fromtimestamp(float(timestamp), tz=IST).date()
    except Exception:
        pass
    for key in ("timestamp", "signal_timestamp", "entry_time"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(IST).replace(tzinfo=None)
            return parsed.date()
        except Exception:
            continue
    return None


def trade_row_event_seconds(row: Dict[str, Any], prefer_exit: bool = False) -> float:
    keys = (
        ("exit_timestamp", "entry_timestamp", "timestamp", "signal_timestamp", "entry_time")
        if prefer_exit
        else ("entry_timestamp", "timestamp", "signal_timestamp", "exit_timestamp", "entry_time")
    )
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
            return value / 1000.0 if value > 1e12 else value
        except (TypeError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(str(raw))
            if parsed.tzinfo is None:
                parsed = IST.localize(parsed)
            else:
                parsed = parsed.astimezone(IST)
            return parsed.timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def live_signal_exit_mark(row: Any) -> Optional[float]:
    """Return an observed manual exit only when the live lifecycle evidence is valid.

    Historical and unidentified legacy rows keep their existing geometry repairs.
    A live advisory mark is still not a broker fill.
    """
    def value(name: str, default=None):
        return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)

    if str(value("data_provenance", "") or "").upper() != "LIVE":
        return None
    if not (value("accepted_by_gate", False) or value("watch_only", False)):
        return None
    if str(value("action", "") or "").upper() != "EXIT":
        return None
    try:
        price = float(value("exit_price"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    entered = trade_row_event_seconds({"entry_timestamp": value("entry_timestamp")})
    exited = trade_row_event_seconds({"exit_timestamp": value("exit_timestamp")}, prefer_exit=True)
    if not (math.isfinite(entered) and math.isfinite(exited) and 0 < entered <= exited):
        return None
    return price


def reconcile_legacy_trade_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Apply final accounting guards to persisted rows before dashboard display."""
    row = normalize_dashboard_accounting_row(row)
    if is_quote_invalid_row(row):
        return row
    inst_type = normalized_row_inst_type(row)
    direction = str(row.get("direction") or "LONG").upper()
    entry_price = float(row.get("entry_price") or row.get("price") or 0.0)
    if entry_price <= 0:
        return row
    broker_fill_pair = bool(
        str(row.get("broker_order_id") or "").strip()
        and str(row.get("exit_order_id") or "").strip()
    )
    actual_exit = float(row.get("exit_price") or 0.0)
    evidence = [row.get("replay_input")]
    trace = row.get("replay_exit_trace")
    if isinstance(trace, (list, tuple)):
        evidence.extend(trace)
    verified_option_replay = (
        inst_type == "OPT"
        and _normalized_text(row.get("data_provenance"), upper=True) == "BACKTEST"
        and any(
            isinstance(item, dict)
            and item.get("guard") == "replay_input"
            and item.get("state") == "VERIFIED_PRICE_PATH"
            for item in evidence
        )
    )
    if (inst_type == "OPT" and not verified_option_replay
            and _normalized_text(row.get("data_provenance"), upper=True) == "BACKTEST_OBSERVED_OPTION_MINUTES"
            and _normalized_text(row.get("source_engine")) == "ut_ema"
            and _normalized_text(row.get("source_lane"), upper=True) == "UTEMA"
            and _normalized_text(row.get("exit_policy_owner"), upper=True) == "UTEMA"):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            digest = str(item.get("input_sha256") or "")
            if (item.get("guard") != "replay_input" or item.get("state") != "VERIFIED_PRICE_PATH"
                    or item.get("source_engine") != "ut_ema" or item.get("complete_path") is not True
                    or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
                continue
            try:
                matches = all(math.isclose(float(item[name]), float(value), rel_tol=0., abs_tol=1e-6)
                    for name, value in (("entry_price", entry_price), ("exit_price", actual_exit),
                        ("lots", row.get("lots")), ("lot_size", row.get("lot_size")),
                        ("charges", row.get("charges")), ("pnl", row.get("pnl"))))
                charges = float(item["charges"])
                expected = round((actual_exit-entry_price)*int(item["lots"])*int(item["lot_size"])-charges, 2)
                verified_option_replay = (matches and charges >= 0 and math.isfinite(charges)
                    and math.isclose(expected, float(item["pnl"]), rel_tol=0., abs_tol=.011))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if verified_option_replay:
                break
    modeled_futures_replay = False
    if inst_type == "FUT" and _normalized_text(row.get("data_provenance"), upper=True) == "BACKTEST":
        for item in evidence:
            if not isinstance(item, dict):
                continue
            digest = str(item.get("input_sha256") or "")
            if (item.get("guard") != "replay_input" or item.get("state") != "INDEX_PRICE_PROXY"
                    or item.get("fill_accounting") != "CAUSAL_INDEX_PROXY_V1"
                    or item.get("complete_path") is not True
                    or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
                continue
            try:
                prices_match = (math.isclose(float(item["entry_price"]), entry_price, rel_tol=0., abs_tol=1e-8)
                                and math.isclose(float(item["exit_price"]), actual_exit, rel_tol=0., abs_tol=1e-8))
                times_match = True
                for name in ("entry_time", "exit_time"):
                    def market_time(value):
                        if isinstance(value, (int, float)):
                            return datetime.fromtimestamp(value, IST).replace(tzinfo=None)
                        stamp = datetime.fromisoformat(str(value))
                        return stamp.astimezone(IST).replace(tzinfo=None) if stamp.tzinfo else stamp
                    row_time = row.get(name.replace("_time", "_timestamp")) or row.get(name)
                    times_match = times_match and market_time(item[name]) == market_time(row_time)
                modeled_futures_replay = prices_match and times_match and item["exit_reason"] == row.get("exit_reason")
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if modeled_futures_replay:
                break
    if (verified_option_replay or modeled_futures_replay) and math.isfinite(actual_exit) and actual_exit > 0:
        # The replay already priced the observed option path, including gap fills
        # and a raised profit stop. Legacy display stops/targets are entry geometry;
        # substituting them would rewrite both the fill and its accounting. Covered
        # futures index-proxy paths retain the same accounting, without claiming
        # that their prices came from observed futures contracts.
        row["exit_price"] = actual_exit
        row["current_price"] = actual_exit
        recorded_pnl = _normalized_number(row.get("pnl"))
        if recorded_pnl is not None and math.isfinite(recorded_pnl):
            return row
        charges = _normalized_number(row.get("charges"))
        if charges is None or not math.isfinite(charges) or charges < 0:
            charges = max(0.0, float(getattr(get_settings(), "opt_cost" if inst_type == "OPT" else "fut_cost", 0.0) or 0.0))
        quantity = int(row.get("lots") or 1) * int(row.get("lot_size") or 1)
        row["charges"] = charges
        sign = -1 if inst_type == "FUT" and direction == "SHORT" else 1
        row["pnl"] = round(sign * (actual_exit - entry_price) * quantity - charges, 2)
        return row
    if broker_fill_pair and actual_exit > 0:
        # Repair rows written before exact-fill accounting was fixed.  Never replace an
        # actual exit fill with the requested stop/target, and never add simulated
        # slippage to prices that already came from broker executions.
        quantity = int(row.get("lots") or 1) * int(row.get("lot_size") or 1)
        settings = get_settings()
        configured_cost = getattr(
            settings, "opt_cost" if inst_type == "OPT" else "fut_cost", 0.0
        )
        charges = max(0.0, float(configured_cost or 0.0))
        gross = (
            (actual_exit - entry_price) * quantity
            if inst_type == "OPT" or direction == "LONG"
            else (entry_price - actual_exit) * quantity
        )
        row["pnl"] = round(gross - charges, 2)
        return row

    stop = float(row.get("current_stop") or row.get("trailing_stop") or row.get("stop") or 0.0)
    target = float(row.get("current_target") or row.get("target") or 0.0)
    exit_reason = str(row.get("exit_reason") or "").upper()
    exit_price = float(row.get("exit_price") or row.get("current_price") or 0.0)
    live_mark = live_signal_exit_mark(row)
    if live_mark is not None:
        exit_price = live_mark
    elif exit_reason == "TARGET_HIT" and target > 0:
        exit_price = target
    elif exit_reason in {"STOP_HIT", "TRAILING_STOP", "SL HIT", "SL_HIT"} and stop > 0:
        exit_price = stop
    if exit_price <= 0:
        return row

    quantity = int(row.get("lots") or 1) * int(row.get("lot_size") or 1)
    settings = get_settings()
    charges = float(settings.fut_cost) if inst_type == "FUT" else float(settings.opt_cost)
    gross = (
        (exit_price - entry_price) * quantity
        if inst_type == "OPT" or direction == "LONG"
        else (entry_price - exit_price) * quantity
    )
    row["exit_price"] = exit_price
    row["current_price"] = exit_price
    row["pnl"] = round(gross - charges, 2)
    return row


def trade_row_to_signal(row: Dict[str, Any]) -> Dict[str, Any]:
    row = normalize_dashboard_accounting_row(row)
    entry_timestamp = row.get("entry_timestamp", 0)
    entry_iso = ""
    if entry_timestamp:
        try:
            entry_iso = datetime.fromtimestamp(entry_timestamp, tz=IST).isoformat()
        except Exception:
            pass

    exit_timestamp = row.get("exit_timestamp", 0)
    exit_iso = ""
    if exit_timestamp:
        try:
            exit_iso = datetime.fromtimestamp(exit_timestamp, tz=IST).isoformat()
        except Exception:
            pass

    is_closed = row.get("status") == "CLOSED" or row.get("exit_price", 0.0) > 0
    return {
        "instrument": row.get("instrument", ""),
        "direction": row.get("direction", ""),
        "price": row.get("entry_price", 0.0),
        "current_price": row.get("exit_price", 0.0) or row.get("current_price", 0.0),
        "pnl": row.get("pnl", 0.0),
        "stop": row.get("trailing_stop", 0.0),
        "target": row.get("target", 0.0),
        "lots": row.get("lots", 1),
        "lot_size": row.get("lot_size", 1),
        "grade": row.get("grade", ""),
        "confidence": row.get("confidence", 0.0),
        "timeframe": row.get("timeframe", ""),
        "inst_type": normalized_row_inst_type(row),
        "option_type": row.get("option_type", ""),
        "atm_strike": row.get("atm_strike", 0.0),
        "timestamp": entry_iso or row.get("entry_time", ""),
        "entry_date": row.get("entry_date", ""),
        "exit_price": row.get("exit_price", 0.0),
        "status": "EXIT SIGNAL" if is_closed else "TRADE SIGNAL",
        "action": "EXIT" if is_closed else "ENTRY",
        "exit_reason": row.get("exit_reason", ""),
        "exit_timestamp": exit_iso or row.get("exit_time", ""),
        "rr": row.get("rr_ratio", 1.5),
        "trading_symbol": "",
        "symbol_token": "",
        "exec_type": row.get("exec_type", "A"),
        "market_regime": row.get("market_regime", ""),
        "data_provenance": row.get("data_provenance", ""),
        "price_action_verdict": row.get("price_action_verdict", ""),
        "intelligence_score": row.get("intelligence_score", 0.0),
        "strategy_revision": row.get("strategy_revision", ""),
        "strategy_direction_mode": row.get("strategy_direction_mode", ""),
        "market_wide_reversal_entry": bool(row.get("market_wide_reversal_entry")),
        "volume_spike_entry": bool(row.get("volume_spike_entry")),
        "power_move": bool(row.get("power_move")),
    }
