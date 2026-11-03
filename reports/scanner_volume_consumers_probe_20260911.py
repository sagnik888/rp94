"""Offline audit counterexamples; executes selected production AST, no app startup.

Run: python reports/scanner_volume_consumers_probe_20260911.py
Only writes the adjacent JSON evidence file. Does not import scanner or load .env.
These assert CURRENT defects, not desired regression-test behavior.
"""
from __future__ import annotations

import ast
import json
import logging
import math
import sys
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytz

ROOT = Path(__file__).resolve().parents[1]
settings = SimpleNamespace(surge_tick_proxy_enabled=True, peak_threat_exit_min_age_enabled=False)
mod = ModuleType("offline_scanner_volume_audit")
sys.modules[mod.__name__] = mod
ns = mod.__dict__
ns.update(pd=pd, np=np, math=math, datetime=datetime, timedelta=timedelta,
          dtime=dtime, IST=pytz.timezone("Asia/Kolkata"), logger=logging.getLogger(__name__),
          get_settings=lambda: settings)


def compile_nodes(nodes, filename):
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), filename, "exec"), ns)


# Pure grade helpers and the real quality policy, without importing app settings.
policy = ast.parse((ROOT / "engine/signal_quality_policy.py").read_text(encoding="utf-8-sig"))
compile_nodes([node for node in policy.body if
               isinstance(node, ast.FunctionDef) and node.name in {"confidence_to_grade", "grade_min_confidence", "grade_rank"}
               or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"GRADE_BANDS", "GRADE_RANKS"} for t in node.targets)], "engine/signal_quality_policy.py")
quality = ast.parse((ROOT / "engine/quality_verdict.py").read_text(encoding="utf-8-sig"))
compile_nodes([node for node in quality.body if not (isinstance(node, ast.ImportFrom) and node.module in {"__future__", "engine.signal_quality_policy"})], "engine/quality_verdict.py")

source = ast.parse((ROOT / "scanner.py").read_text(encoding="utf-8-sig"))
sc_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "Scanner")
names = {
    "_trade_like_market_intelligence_context", "_trade_like_signed_intelligence",
    "_regime_conviction_vector_from_context", "_trade_direction_view", "_direction_signal_for_view",
    "_view_aligned_intel_score", "_first_present_number", "_peak_threat_flow_points",
    "_choppy_profit_lock_bypass_peak_threshold", "_quality_verdict_for_candidate",
    "_candle_thrust_metrics", "_surge_prior_mean", "_surge_percentile",
    "_surge_tick_ratio", "_volume_window_is_homogeneous", "_setting_enabled",
    "_peak_threat_exit_decision", "_intel_signal_opposes_view", "_intel_signal_aligns_view",
    "_exit_confirmation_ready", "_clear_exit_confirmation",
}
selected = [node for node in sc_class.body if isinstance(node, ast.FunctionDef) and node.name in names]
assert {node.name for node in selected} == names
compile_nodes([ast.ClassDef(name="Scanner", bases=[], keywords=[], body=selected, decorator_list=[])], "scanner.py")
Scanner = ns["Scanner"]
Scanner.SURGE_RATIO_IMPLAUSIBLE = 1000.0
Scanner._profit_threshold_scale = lambda self, trade, risk: 1.0
ns["aggregate_intelligence_score"] = lambda aggregate: float(aggregate.get("score_norm", aggregate.get("score", 0.0) / 100.0))
ns["normalize_intelligence_score"] = lambda score: score / 100.0


def scanner():
    sc = Scanner()
    sc.mode = "REAL"
    sc._live_direction_context = lambda *a, **k: {}
    sc._regime_opposes_direction = lambda *a: False
    sc._intraday_phase_from_session_context = lambda *a: ("UNKNOWN", 0.0)
    sc._eod_protect_active = lambda *a: False
    sc._candidate_15m_trend_agrees = lambda *a: False
    sc._get_trade_regime = lambda *a, **k: "TRENDING_UP"
    sc._clear_loss_threat_confirmation = lambda *a: None
    sc._profit_guard_reversal_context = lambda *a, **k: {"has_context": False, "reason": "no price evidence in this isolated probe"}
    return sc


results = {}
sc = scanner()
cache_reads = []
live = {"aggregate": {"score_norm": -0.8, "signal": "SELL"}, "volume": {"relative_volume_ratio": 0.4}, "oi": {"signal": "BEARISH"}}
sc._cached_timeframe_intel = lambda *a, **k: cache_reads.append((a, k)) or live
trade = SimpleNamespace(instrument="NIFTY", timeframe="5min", inst_type="OPT", option_type="CE", direction="LONG",
                        volume_ratio=2.0, current_price=110.0,
                        market_intelligence_context={"aggregate": {"score_norm": 0.8, "signal": "BUY"}})
ctx = sc._trade_like_market_intelligence_context(trade)
results["saved_entry_context_wins"] = {"entry_score": 0.8, "live_score": -0.8,
    "resolved_score": sc._trade_like_signed_intelligence(trade, ctx), "live_cache_reads": len(cache_reads),
    "oi_present": bool(ctx.get("oi"))}
assert results["saved_entry_context_wins"]["resolved_score"] == 0.8 and not cache_reads

trade.regime_conviction_vector = {"trend_up_prob": 0.9}
results["saved_vector_wins"] = sc._regime_conviction_vector_from_context(trade, {"anchor_regime": {"conviction_vector": {"trend_down_prob": 0.9}}})
del trade.regime_conviction_vector

def lock_threshold(volume_ratio, ctx):
    trade.volume_ratio = volume_ratio
    trade.market_intelligence_context = ctx
    return sc._choppy_profit_lock_bypass_peak_threshold(trade, regime_text="RANGING", latest_time=None,
        entry_price=100.0, qty=100, peak_pnl=1000.0)

base = {"aggregate": {"score_norm": 0.8, "signal": "BUY"}, "volume": {"relative_volume_ratio": 0.4}}
results["choppy_lock_volume_lookup"] = {
    "actual_sibling_volume": lock_threshold(0, base),
    "wrongly_nested_volume": lock_threshold(0, {"aggregate": {**base["aggregate"], "volume": base["volume"]}}),
    "saved_positive_entry_ratio": lock_threshold(2.0, {"aggregate": {**base["aggregate"], "volume": base["volume"]}}),
}
assert results["choppy_lock_volume_lookup"]["actual_sibling_volume"][0] == 1000
assert results["choppy_lock_volume_lookup"]["wrongly_nested_volume"][0] == 750

results["unavailable_flow_still_votes"] = sc._peak_threat_flow_points(trade, {"available": False, "ratio": 0.2}, "LONG")
assert results["unavailable_flow_still_votes"][0] == 1

def peak(ctx, entry_volume=0):
    t = SimpleNamespace(instrument="NIFTY", timeframe="5min", inst_type="OPT", option_type="CE", direction="LONG", volume_ratio=entry_volume, market_intelligence_context=ctx)
    args = dict(current_price=110, current_pnl_rs=1000, peak_pnl=1000, initial_risk_rs=1000,
                regime="TRENDING_UP", inst_type="OPT", option_type="CE", direction="LONG")
    first = sc._peak_threat_exit_decision(t, latest_time=datetime(2026,9,11,11,0), **args)
    second = sc._peak_threat_exit_decision(t, latest_time=datetime(2026,9,11,11,0,6), **args)
    return {"first": first, "second": second}

results["buy_sell_imbalance_read_as_weak_participation"] = peak({"volume": {"buy_sell_ratio": 0.4}})
assert "weak volume 0.40x" in results["buy_sell_imbalance_read_as_weak_participation"]["second"][2]
results["saved_entry_ratio_masks_current_weak_volume"] = {
    "without_entry_ratio": peak({"volume": {"relative_volume_ratio": 0.4}}),
    "with_entry_ratio": peak({"volume": {"relative_volume_ratio": 0.4}}, 2.0),
}
results["unavailable_pillars_can_confirm_peak_exit"] = peak({
    "aggregate": {"score_norm": -0.8, "signal": "SELL", "available": False},
    "oi": {"signal": "BEARISH", "available": False},
})
assert results["unavailable_pillars_can_confirm_peak_exit"]["second"][0] is True

def candidate_quality(volume_ratio, volume_spike_entry=False, timeframe="15min"):
    candidate = SimpleNamespace(instrument="NIFTY", timeframe=timeframe, inst_type="OPT", confidence=.75,
        grade="A", adx_value=30.0, volume_ratio=volume_ratio, volume_spike_entry=volume_spike_entry,
        volume_basis="TICK_ACTIVITY", market_intelligence_context={"volume": {"available": True, "relative_volume_ratio": 3.0, "volume_basis": "TRADED"}})
    return sc._quality_verdict_for_candidate(candidate, regime="TRENDING_UP").to_dict()

results["quality_ignores_measured_context_volume"] = candidate_quality(0.0)
results["quality_accepts_tick_ratio_as_volume"] = candidate_quality(1.3, timeframe="5min")
assert not results["quality_ignores_measured_context_volume"]["allowed"]
assert results["quality_accepts_tick_ratio_as_volume"]["allowed"]

frame = pd.DataFrame({"open": [100]*7, "high": [102]*7, "low": [99]*7, "close": [101]*7,
                      "volume": [100]*6+[300], "volume_basis": ["fyers_index"]*7},
                     index=pd.date_range("2026-09-11 10:00", periods=7, freq="5min"))
sc.candles = SimpleNamespace(get_candles=lambda *a: frame)
sc._one_min_entry_freshness = lambda *a: {"fresh": False}
sc._record_one_min_thrust_state = lambda *a: None
results["asof_timezone_slice_fails_open"] = {
    "requested_as_of": "2026-09-11T10:05:00+05:30", "last_frame_bar": str(frame.index[-1]),
    "result": sc._candle_thrust_metrics("NIFTY", "5min", "BUY", pd.Timestamp("2026-09-11T10:05:00+05:30")),
}
assert results["asof_timezone_slice_fails_open"]["result"]["volume_ratio"] == 3.0

frame.loc[frame.index[-1], "volume"] = 10
results["forming_bar_raw_ratio"] = {"scenario": "5m bar after 30s at the same volume rate as prior 100-unit complete 5m bars",
    "observed": sc._candle_thrust_metrics("NIFTY", "5min", "BUY"), "equal_rate_ratio": 1.0}
assert results["forming_bar_raw_ratio"]["observed"]["volume_ratio"] == 0.1

out = Path(__file__).with_suffix(".json")
out.write_text(json.dumps({"method": "Production AST methods with isolated collaborators; defects asserted, no broker calls or P&L claim", "results": results}, indent=2), encoding="utf-8")
print(json.dumps({"output": str(out), "counterexample_groups": len(results), "assertions": "passed"}))
