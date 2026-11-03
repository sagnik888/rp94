"""Read-only UTS stop/quality research. No broker calls or production edits.

Production helpers are probed with detached objects. Archived P&L paths are
sampled diagnostics, not executable quotes or counterfactual fills.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import socket
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def blocked_network(*args, **kwargs):
    raise RuntimeError("Network disabled in UTS research")


socket.socket.connect = blocked_network
socket.create_connection = blocked_network

from engine import uts_lane
from engine.market_regime import option_stop_tolerance_pct
from engine.quality_verdict import QualityInputs, evaluate_quality
from trading.trade_manager import TradeManager


SOURCE_FILES = (
    "engine/uts_lane.py", "engine/ut_smart_engine.py", "engine/entry_policy.py",
    "engine/exit_cycle.py", "engine/quality_verdict.py", "engine/signal_manager.py",
    "engine/signal_processor.py", "engine/market_regime.py", "scanner.py",
    "trading/trade_manager.py", "intelligence/confidence_calibration.py",
    "config/settings.py",
)


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def compact_archive(row, day):
    contract = row.get("contract") or {}
    quality = row.get("quality") or {}
    return {
        "id": row.get("id"), "day": day, "instrument": row.get("instrument"),
        "timeframe": row.get("timeframe"), "direction": row.get("direction"),
        "symbol": contract.get("trading_symbol"), "grade": row.get("grade"),
        "confidence": row.get("confidence"), "net": row.get("pnl"),
        "peak_gross": row.get("peak_pnl"), "entry": row.get("price"),
        "recorded_stop": row.get("stop"), "quantity":
            float(contract.get("lots") or 1) * float(contract.get("lot_size") or 1),
        "entry_time": row.get("entry_timestamp"), "exit_reason": row.get("exit_reason"),
        "quality_allowed": quality.get("allowed"),
        "quality_reason_code": quality.get("reason_code"),
        "confidence_semantics": (quality.get("checks") or {}).get("confidence_semantics"),
        "pnl_path": row.get("pnl_path") or [], "evidence": "archived_terminal_fill",
    }


def filled_rows():
    rows = []
    for path in sorted((ROOT / "data_store/session_archive").glob("*/trades.json")):
        for row in read_json(path):
            if (row.get("source") == "trade_manager_terminal_fill"
                    and row.get("source_lane") == "UTS" and row.get("status") == "CLOSED"):
                rows.append(compact_archive(row, path.parent.name))
    uri = (ROOT / "data_store/utbot_data.db").as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        cols = "id,instrument,timeframe,direction,trading_symbol,grade,confidence,pnl,peak_pnl,entry_price,initial_stop,lots,lot_size,entry_filled_at,exit_reason,quality_verdict"
        for record in conn.execute(f"SELECT {cols} FROM trades WHERE is_ghost=0 AND source_lane='UTS' AND status='CLOSED' AND execution_status='CLOSED'"):
            row = dict(record)
            quality = json.loads(row.get("quality_verdict") or "{}")
            stamp = row.get("entry_filled_at") or ""
            rows.append({
                "id": row["id"], "day": stamp[:10], "instrument": row["instrument"].split()[0],
                "timeframe": row["timeframe"], "direction": row["direction"],
                "symbol": row["trading_symbol"], "grade": row["grade"],
                "confidence": row["confidence"], "net": row["pnl"],
                "peak_gross": row["peak_pnl"], "entry": row["entry_price"],
                "recorded_stop": row["initial_stop"],
                "quantity": float(row["lots"] or 1) * float(row["lot_size"] or 1),
                "entry_time": stamp, "exit_reason": row["exit_reason"],
                "quality_allowed": quality.get("allowed"),
                "quality_reason_code": quality.get("reason_code"),
                "confidence_semantics": (quality.get("checks") or {}).get("confidence_semantics"),
                "pnl_path": [], "evidence": "current_sqlite_completed_real_trade",
            })
    unique = {}
    for row in rows:
        key = (row["day"], row["symbol"], row["entry_time"])
        unique.setdefault(key, row)
    return list(unique.values())


def cohort(rows):
    out = {}
    for name, allowed in (("allowed", True), ("rejected", False), ("unknown", None)):
        selected = [r for r in rows if r["quality_allowed"] is allowed]
        out[name] = {
            "count": len(selected), "wins": sum(float(r["net"] or 0) > 0 for r in selected),
            "net": round(sum(float(r["net"] or 0) for r in selected), 2),
        }
    return out


def detached_trade(stop):
    return SimpleNamespace(inst_type="OPT", entry_price=100.0, initial_stop=stop,
        current_stop=stop, premium_stop=stop, stop=stop, is_recovery=False,
        market_regime="TRENDING_UP", confidence=0.8, grade="A", s1_peak_premium=None)


def lock_probes():
    settings = SimpleNamespace(uts_lane_lock_arm_r=1.0, uts_lane_lock_giveback_pct=30.0)
    rows = []
    for cap in (5, 10, 15):
        for peak in (102, 105, 108, 110, 120):
            trade = detached_trade(100.0 - cap)
            decision = uts_lane.exit_decision(trade, 0.0, float(peak), settings=settings)
            armed = decision.premium_stop > 0
            floor = float(decision.premium_stop)
            if armed:
                touch = uts_lane.exit_decision(trade, 0.0, floor, settings=settings)
                assert touch.exit and touch.reason == "UTS_PROFIT_LOCK"
            assert armed == (peak - 100 >= cap)
            rows.append({"cap_pct": cap, "peak": peak, "arm_premium": 100 + cap,
                "armed": armed, "profit_floor": floor if armed else None,
                "hard_stop": 100 - cap})
    return rows


def stop_cap_probes():
    rows = []
    cases = (("TRENDING_UP", "A", 0.8, False, 10.0),
             ("RANGING", "A", 0.8, False, 15.0),
             ("VOLATILE", "A", 0.8, False, 15.0),
             ("RANGING", "C", 0.3, False, 5.0),
             ("TRENDING_UP", "A", 0.8, True, 5.0))
    for regime, grade, confidence, fast, expected in cases:
        trade = detached_trade(50.0)
        trade.market_regime, trade.grade, trade.confidence = regime, grade, confidence
        trade.volume_spike_entry = fast
        TradeManager._cap_option_entry_stop(SimpleNamespace(options_sl_pct=15.0), trade)
        cap = option_stop_tolerance_pct(15.0, regime, confidence=confidence, grade=grade, fast_entry=fast)
        assert cap == expected and trade.current_stop >= 100 * (1 - cap / 100)
        rows.append({"regime": regime, "grade": grade, "fast": fast,
            "initial_atr_stop": 50.0, "cap_pct": cap, "effective_stop": trade.current_stop})
    return rows


def quality_probes():
    rows = []
    for regime in ("TRENDING_UP", "RANGING", "VOLATILE"):
        for confidence in (0.3, 0.6, 0.8):
            inputs = QualityInputs(timeframe="5min", inst_type="OPT", confidence=confidence,
                regime=regime, adx_value=30, trend_15m_agrees=True,
                source_count=1, volume_available=False, volume_basis="UNAVAILABLE")
            verdict = evaluate_quality(inputs, SimpleNamespace())
            rows.append({"regime": regime, "confidence": confidence,
                "one_source": True, "trend_15m_agrees": True, "adx": 30,
                "allowed": verdict.allowed, "reason_code": verdict.reason_code})
    return rows


def sampled_path_diagnostics(row):
    entry, qty = float(row["entry"] or 0), float(row["quantity"] or 0)
    if entry <= 0 or qty <= 0:
        return {}
    # Since September 17, these paths and peak_pnl are gross before costs.
    points = sorted((float(t), float(p)) for t, p in row["pnl_path"])
    out = {"sample_count": len(points),
        "recorded_peak_premium_pct": round(float(row["peak_gross"] or 0) / qty / entry * 100, 4)}
    by_cap = {}
    for cap in (5, 10, 15):
        stop_gross = -entry * qty * cap / 100
        first_cross = next(((t, p) for t, p in points if p <= stop_gross), None)
        later_recovery = bool(first_cross and any(
            t > first_cross[0] and p > 0 for t, p in points))
        by_cap[str(cap)] = {"sampled_stop_cross_seconds": first_cross[0] if first_cross else None,
            "sampled_positive_recovery_after_cross": later_recovery,
            "recorded_peak_reaches_1r": float(row["peak_gross"] or 0) >= entry * qty * cap / 100}
    out["by_cap_pct"] = by_cap
    out["scope"] = "Retained gross P&L samples only; peak may omit terminal fills; no executable fill or after-exit path claim"
    return out


def observed_contract_coverage(rows):
    import pyarrow.parquet as pq
    table = pq.read_table(ROOT / "data_store/option_history_bundle.parquet", columns=["contract", "timestamp"])
    contracts, stamps = table.column("contract").to_pylist(), table.column("timestamp").to_pylist()
    counts = Counter((str(contract), str(stamp)[:10]) for contract, stamp in zip(contracts, stamps))
    symbols = {str(contract) for contract in contracts}
    results = []
    for symbol, day in sorted({(r["symbol"], r["day"]) for r in rows}):
        direct = ROOT / "data_store/candles" / f"{symbol}_1min.csv"
        archived = ROOT / "data_store/session_archive" / day / "candles" / f"{symbol}_1min.csv"
        results.append({"symbol": symbol, "day": day, "direct_csv_exists": direct.exists(),
            "session_csv_exists": archived.exists(), "bundle_contract_present": symbol in symbols,
            "bundle_rows_on_day": counts[(symbol, day)]})
    return results


def main():
    before = source_hashes()
    fills = filled_rows()
    for row in fills:
        row["path_diagnostics"] = sampled_path_diagnostics(row)
        row.pop("pnl_path")
    calibration = list(csv.DictReader((ROOT / "data_store/outcome_calibration.csv").open(encoding="utf-8")))
    segments = Counter((r.get("instrument"), r.get("timeframe"), r.get("regime")) for r in calibration)
    report = {
        "captured_at_ist": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
        "scope": "Read-only research; app-recorded realized cohorts; detached production-helper probes",
        "filled_trade_count": len(fills), "filled_trades": fills,
        "quality_cohort_all": cohort(fills),
        "quality_cohort_by_day": {day: cohort([r for r in fills if r["day"] == day])
                                  for day in sorted({r["day"] for r in fills})},
        "profit_lock_probes": lock_probes(), "atr_stop_cap_probes": stop_cap_probes(),
        "quality_rule_probes": quality_probes(),
        "calibration": {"record_count": len(calibration), "max_segment_count": max(segments.values(), default=0),
            "required_global_default": 200, "required_segment_default": 50,
            "has_strategy_or_source_lane_column": bool(calibration and any(k in calibration[0] for k in ("strategy", "source_lane", "source_engine")))},
        "observed_contract_coverage": observed_contract_coverage(fills),
        "prior_observed_option_replay": read_json(ROOT / "reports/ut_smart_fixed_replay_20260930_verified.json")["summary"],
        "source_sha256": before,
        "limitations": [
            "Cohorts are selected executed trades under old exits, not a causal filter backtest.",
            "App costs are not broker contract-note reconciliation.",
            "Only ten completed UTS fills are available; correlated index trades are not independent observations.",
            "Sampled P&L paths omit ticks and end at actual exits; later recovery cannot be inferred.",
            "Recorded peak can omit a terminal fill: MIDCPNIFTY final gross gain exceeds its stored peak.",
            "Missing observed contract paths block stop/re-entry and grade counterfactual validation.",
            "Detached rule probes prove code behavior, not market profitability.",
        ],
    }
    assert source_hashes() == before, "Production sources changed during research"
    report["production_sources_unchanged"] = True
    target = ROOT / "reports/ut_smart_cap_quality_research_20260930.json"
    target.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("filled_trade_count", "quality_cohort_all", "calibration", "production_sources_unchanged")}, indent=2))
    print("Contract coverage:", json.dumps(report["observed_contract_coverage"]))
    print("Evidence:", target)


if __name__ == "__main__":
    main()
