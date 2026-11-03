"""Read-only, synthetic probes for the September 11 regime/session audit.

Run from the repository root with venv/Scripts/python.exe. Uses no broker or DB
objects. Scanner helpers are extracted with AST to avoid scanner startup imports.
The only output is JSON on stdout.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from config.settings import get_settings
from engine.alt_scanners import AltSignalEngine
from engine.multi_timeframe import MultiTimeframeEngine
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.regime_detector import RegimeDetector
from intelligence.session_context import SessionContextAnalyzer


def scanner_helpers(*names):
    tree = ast.parse(Path("scanner.py").read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Scanner")
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in methods:
        node.decorator_list = []
    module = ast.Module(body=ast.parse("from __future__ import annotations").body + methods, type_ignores=[])
    ns = {"pd": pd, "logger": SimpleNamespace(debug=lambda *_args: None)}
    exec(compile(module, "scanner.py", "exec"), ns)
    return ns


def main():
    analyzer = SessionContextAnalyzer()
    settings = get_settings()
    result = {"settings": {key: getattr(settings, key, None) for key in (
        "scanner_engine", "regime_smoothing_enabled", "regime_smoothing_window",
        "regime_er_veto_enabled", "regime_range_day_adx_override_needs_er",
    )}}
    index = pd.date_range("2026-09-11 09:15", periods=14, freq="5min")
    zero = pd.DataFrame({"open": 100.9, "high": 101.0, "low": 99.0, "close": 100.9, "volume": 0.0}, index=index)
    context = analyzer.analyze(zero, instrument="NIFTY")
    aggregator = IntelligenceAggregator.__new__(IntelligenceAggregator)
    top_down = aggregator._top_down_context_adjustment(
        session_context={"session_delta": context["session_delta"]}, anchor_regime={}, breadth_context={}
    )
    result["zero_volume_directional_vote"] = {
        "session_delta": context["session_delta"], "vwap_source": context["vwap_source"], "top_down": top_down,
    }

    frames = []
    for day in ("2026-09-09", "2026-09-10"):
        frame = pd.DataFrame({"open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0, "volume": 100.0},
                             index=pd.date_range(day + " 09:15", periods=75, freq="5min"))
        frame.iloc[0, frame.columns.get_loc("high")] = 140.0
        frame.iloc[0, frame.columns.get_loc("low")] = 90.0
        frames.append(frame)
    current = zero.copy()
    current["high"], current["low"], current["volume"] = 105.0, 95.0, 100.0
    whole = pd.concat(frames + [current])
    helpers = scanner_helpers("_live_analysis_window", "_get_trade_regime", "_trade_regime_is_own_timeframe")
    scanner = SimpleNamespace(mode="REAL", _get_current_session_day=lambda: "2026-09-11")
    window = helpers["_live_analysis_window"](scanner, whole, "5min")
    full, live = analyzer.analyze(whole), analyzer.analyze(window)
    keys = ("pdh", "pdl", "avg_10day_range_points", "range_vs_10day")
    result["truncated_session_history"] = {
        "full_bars": len(whole), "live_window_bars": len(window),
        "complete_history": {key: full[key] for key in keys}, "live_window": {key: live[key] for key in keys},
    }

    older_extreme = whole.copy()
    older_extreme.loc[pd.Timestamp("2026-09-09 09:15"), "high"] = 180.0
    result["previous_day_high_uses_older_session"] = {
        "actual_previous_day_high": 140.0,
        "reported_pdh": analyzer.analyze(older_extreme)["pdh"],
    }
    result["timeframe_horizon_contract"] = []
    for minutes in (1, 5, 15):
        frame = zero.iloc[:6].copy()
        frame.index = pd.date_range("2026-09-11 09:15", periods=6, freq=f"{minutes}min")
        value = analyzer.analyze(frame)
        result["timeframe_horizon_contract"].append({
            "bar_minutes": minutes, "last_bar_start": str(frame.index[-1]),
            "opening_status": value["opening_range_status"],
            "minute": value["mood_horizons"]["minute"], "quarter": value["mood_horizons"]["quarter"],
        })

    engine = AltSignalEngine(signal_source="quad_prime")
    ema, supertrend, utbot = np.array([1, -1]), np.array([1, 1]), np.array([1, -1])
    positions, votes = engine._quad_union_consensus_positions(ema, supertrend, utbot)
    result["quad_held_consensus_vote"] = {
        "ema": ema.tolist(), "supertrend": supertrend.tolist(), "utbot": utbot.tolist(),
        "held_consensus": votes["consensus"].tolist(), "position": positions.tolist(),
    }
    updated_volume = zero.copy()
    updated_volume.iloc[-1, updated_volume.columns.get_loc("volume")] = 999999.0
    result["mtf_volume_only_update_hash"] = {
        "same_hash": MultiTimeframeEngine._hash_df(zero) == MultiTimeframeEngine._hash_df(updated_volume),
        "scope": "Matters for volume-dependent adaptive_dual; current quad_prime raw signals are price-only.",
    }
    prices = np.linspace(100.0, 120.0, 80)
    long_frame = pd.DataFrame({"open": prices - 0.1, "high": prices + 0.1,
                               "low": prices - 0.2, "close": prices, "volume": 100.0},
                              index=pd.date_range("2026-09-10 09:15", periods=80, freq="5min"))
    full_regime = RegimeDetector().detect(long_frame, "PROBE", "5min")
    pocket_regime = RegimeDetector().detect(long_frame.tail(36), "PROBE", "panel_5min_lb36")
    regime_keys = ("regime", "candle_count", "readiness", "full_confidence", "confidence", "bb_width", "breakout_direction")
    result["dashboard_36_bar_hour_vs_full_5m"] = {
        "full": {key: full_regime[key] for key in regime_keys},
        "dashboard_hour": {key: pocket_regime[key] for key in regime_keys},
    }
    scanner._timeframe_intel_key = lambda name, tf: f"{name}_{tf}"
    scanner._timeframe_regimes = {"NIFTY_15min": "TRENDING_UP"}
    scanner.latest_regimes = {"NIFTY": "RANGING"}
    scanner._intel_cache_time = {"NIFTY_15min": 1.0}
    trade = SimpleNamespace(instrument="NIFTY", timeframe="15min")
    result["stale_exact_regime"] = {
        "label_with_epoch_one_timestamp": helpers["_get_trade_regime"](scanner, trade),
        "counts_as_own_timeframe": helpers["_trade_regime_is_own_timeframe"](scanner, trade),
    }
    scanner._timeframe_regimes.clear()
    result["missing_15m_regime"] = {
        "returned_label": helpers["_get_trade_regime"](scanner, trade),
        "counts_as_own_timeframe": helpers["_trade_regime_is_own_timeframe"](scanner, trade),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
