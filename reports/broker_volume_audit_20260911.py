"""Offline audit probes: extract current method AST; do not import/start the app.

Run from the repository: venv/Scripts/python.exe reports/broker_volume_audit_20260911.py
All broker methods below receive in-memory fake responses. No network or persistence.
The assertions reproduce defects, not the desired corrected behavior.
"""
import ast
import json
import logging
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import *
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("offline_broker_audit")
INDEX_EXCHANGE = {"NIFTY": ("NFO", "NSE")}
FYERS_SYMBOL_TO_INDEX = {}


def extracted(path, class_name, names):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = dict(globals())
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(ROOT / path), "exec"), ns)
    return ns[class_name]


def run():
    results = {}
    provider_type = extracted("data/market_data.py", "MarketDataProvider", {
        "_record_index_futures_volume_snapshot", "get_index_futures_minute_volume_context",
        "_augment_chain_oi_from_kite", "_chain_oi_is_usable",
        "_apply_option_oi_snapshot_deltas_locked",
    })
    base = 1_788_849_000.0  # exact minute boundary
    p = provider_type()
    for minute in range(7):
        p._record_index_futures_volume_snapshot("NIFTY", {
            "tradingsymbol": "NIFTY26SEPFUT", "volume": 10_000 + minute * 100,
        }, now_ts=base + minute * 60 + 45)
    before = p.get_index_futures_minute_volume_context("NIFTY", now_ts=base + 6 * 60 + 59)
    after = p.get_index_futures_minute_volume_context("NIFTY", now_ts=base + 7 * 60 + 1)
    assert before["reliable"] and after["reliable"]
    assert before["projected_ratio"] == 1.0169 and after["projected_ratio"] == 4.0
    results["futures_rollover_without_new_data"] = {"before": before, "after": after}

    p = provider_type()
    for stamp, cumulative in ((base + 59, 10_000), (base + 61, 11_000)):
        p._record_index_futures_volume_snapshot("NIFTY", {
            "tradingsymbol": "NIFTY26SEPFUT", "volume": cumulative,
        }, now_ts=stamp)
    state = dict(p._index_futures_minute_state["NIFTY"])
    state["completed"] = list(state["completed"])
    assert state["current_volume"] == 1000 and state["completed"] == [0.0]
    results["cross_boundary_delta_charged_to_new_minute"] = state

    kite_type = extracted("data/kite_market_data.py", "KiteDataSource", {
        "index_futures_activity", "_key",
    })
    k = kite_type()
    k._lock = threading.RLock()
    today = datetime.now(IST).date()
    k._instruments = {"NFO": [{
        "name": "NIFTY", "instrument_type": "FUT", "expiry": today + timedelta(days=10),
        "tradingsymbol": "NIFTY26SEPFUT", "instrument_token": 1,
    }]}
    stale_at = datetime.now(IST) - timedelta(days=1)
    k._call = lambda *a, **kw: {"NFO:NIFTY26SEPFUT": {
        "volume": 10_000, "oi": 50_000, "last_price": 25_000,
        "timestamp": stale_at, "last_trade_time": stale_at,
    }}
    stale = k.index_futures_activity("NIFTY")
    assert stale["available"] and "timestamp" not in stale and "exchange_at" not in stale
    results["day_old_futures_quote_accepted"] = stale

    p = provider_type()
    p._KITE_OI_OVERLAY_MIN_INTERVAL = 60
    p._ltp_cache = {}
    calls = []
    def kite_chain(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        frame = pd.DataFrame([{"strike": 25000, "call_oi": 100, "put_oi": 200}])
        frame.attrs["expiry"] = today + timedelta(days=3)
        return frame
    p.kite = SimpleNamespace(is_connected=True, get_option_chain=kite_chain)
    chain = pd.DataFrame([{
        "strike": 25000, "call_oi": 0, "put_oi": 0,
        "call_symbol": "NIFTY_NEXT_EXPIRY_CE", "put_symbol": "NIFTY_NEXT_EXPIRY_PE",
    }])
    chain.attrs["expiry"] = today + timedelta(days=10)
    mixed = p._augment_chain_oi_from_kite("NIFTY", chain)
    assert "expiry" not in calls[0]["kwargs"] and mixed.iloc[0]["call_oi"] == 100
    results["strike_only_oi_overlay_crosses_expiry"] = {
        "calls": calls, "rows": mixed.to_dict(orient="records"), "attrs": mixed.attrs,
    }

    p = provider_type()
    expiry = "22SEP2026"
    p._load_option_oi_snapshots = lambda: {f"NIFTY|{expiry}": {
        "provider": "fyers", "session": datetime.now().date().isoformat(),
        "captured_at": time.time() - 90,
        "rows": {"25000.0": {"call_oi": 1000, "put_oi": 1000}},
    }}
    p._save_option_oi_snapshots = lambda: None
    current = pd.DataFrame([{"strike": 25000, "call_oi": 1100, "put_oi": 1100,
                             "call_oi_change": 500, "put_oi_change": 500}])
    fyers = p._apply_option_oi_snapshot_deltas_locked("NIFTY", current, "fyers", expiry)
    current[["call_oi_change", "put_oi_change"]] = 0
    angel = p._apply_option_oi_snapshot_deltas_locked("NIFTY", current, "angelone", expiry)
    assert fyers.iloc[0]["call_oi_change"] == 500 and angel.iloc[0]["call_oi_change"] == 100
    results["oi_change_horizon_changes_with_provider"] = {
        "fyers": fyers.to_dict(orient="records"), "angel": angel.to_dict(orient="records"),
    }

    expiry_type = extracted("engine/expiry_manager.py", "ExpiryManager", {"get_token"})
    exp = expiry_type()
    exp._ensure_ready = lambda: True
    exp.in_gamma_window = lambda name: True
    exp.expiries = {"NIFTY": {"weekly": "15SEP2026", "active_weekly": "22SEP2026"}}
    exp._token_cache = {}
    exp.df = pd.DataFrame([{
        "name": "NIFTY", "expiry": e, "strike": 2500000.0,
        "symbol": s, "token": token,
    } for e, s, token in (("15SEP2026", "NIFTY_CURRENT_CE", "current"),
                          ("22SEP2026", "NIFTY_NEXT_CE", "next"))])
    execution = exp.get_token("NIFTY", 25000, "CE", "active_weekly")
    assert execution["expiry"] != exp.expiries["NIFTY"]["active_weekly"]
    results["gamma_execution_differs_from_chain_active_label"] = {
        "chain_active_label": exp.expiries["NIFTY"]["active_weekly"],
        "execution_contract": execution,
    }
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
