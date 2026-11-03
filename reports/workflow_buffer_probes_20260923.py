"""Read-only, deterministic probes of current scanner methods; no scanner startup.

Run: python -B reports/workflow_buffer_probes_20260923.py
Only --output writes a file. Inputs are synthetic, so these demonstrate mechanisms,
not the frequency or profitability of those mechanisms in real sessions.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from loguru import logger

from config.settings import get_settings
from engine.intelligence_score import aggregate_intelligence_score
from engine.market_regime import is_risky_regime, is_trending_regime
from engine.quality_verdict import QualityInputs, evaluate_quality
from engine.signal_manager import (
    SignalManager,
    confidence_from_total_score,
    intelligence_pillar_score,
    regime_pillar_score,
)
from engine.signal_quality_policy import confidence_to_grade


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    # Copy only: never alter the application's settings object or any config file.
    settings = get_settings().model_copy(update={
        "scanner_engine": "utbot", "ut_confidence_calibration_enabled": False,
        "ut_confidence_logistic": True, "ut_confidence_logistic_k": 5.0,
        "ut_dynamic_confidence": False, "stabilization_regrade_enabled": True,
        "ut_market_location_gate": True,
    })
    source = ROOT.joinpath("scanner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    scanner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Scanner")
    names = {
        "_regrade_matured_candidate", "_refresh_pending_live_signal",
        "_market_location_entry_gate", "_candidate_15m_trend_agrees",
    }
    methods = [n for n in scanner.body if isinstance(n, ast.FunctionDef) and n.name in names]
    extracted = ast.Module(body=[ast.ClassDef(
        name="ProbeScanner", bases=[], keywords=[], body=methods, decorator_list=[],
    )], type_ignores=[])
    namespace = dict(
        get_settings=lambda: settings, logger=logger, pd=pd,
        confidence_from_total_score=confidence_from_total_score,
        intelligence_pillar_score=intelligence_pillar_score,
        regime_pillar_score=regime_pillar_score,
        confidence_to_grade=confidence_to_grade,
        aggregate_intelligence_score=aggregate_intelligence_score,
        is_risky_regime=is_risky_regime, is_trending_regime=is_trending_regime,
    )
    exec("from __future__ import annotations\n" + ast.unparse(extracted), namespace)
    probe = namespace["ProbeScanner"]()
    probe.mode = "REAL"
    probe._setting_enabled = lambda value, default: bool(value)
    probe._diag_inc = lambda *a: None
    probe._one_min_timestamp = lambda value: value
    regime = ["TRENDING_UP"]
    probe._get_trade_regime = lambda c, default="UNKNOWN": regime[0]
    probe._trade_regime_is_own_timeframe = lambda c: True
    fresh = {"aggregate": {"score_norm": 0.6}, "session_context": {}}
    probe._fresh_exact_trade_intel = lambda c, **kw: fresh
    manager = SignalManager()

    def candidate():
        return SimpleNamespace(
            instrument="NIFTY", timeframe="5min", signal_type="BUY", direction="LONG",
            adx_value=30.0, stop_distance=1.0, atr_value=1.0, timestamp=None,
            signal_timestamp=datetime(2026, 9, 22, 10, 0),
            raw_candle={"open": 100.0, "close": 110.0, "high": 111.0, "low": 99.0},
            intelligence_score=0.6, inst_type="OPT", impulse_confirmed=True,
            trend_15m_agrees=True,
        )

    with patch("config.settings.get_settings", return_value=settings):
        # This is a forming source bar. A later same-bar snapshot is refused even
        # when its technical evidence has changed; maturity keeps the old tech.
        c = candidate()
        c.grade, c.confidence, c.reasons = manager._grade_signal(c, 0.6, 0.6, regime[0], vix_value=15)
        updated = copy.deepcopy(c)
        updated.raw_candle = {"open": 100.0, "close": 101.0, "high": 111.0, "low": 99.0}
        probe._pending_live_signals = {"key": {"candidate": c, "sig_timestamp": c.signal_timestamp}}
        same_bar_adopted = probe._refresh_pending_live_signal("key", updated, datetime(2026, 9, 22, 10, 0, 30))
        probe._regrade_matured_candidate(c)
        direct_grade, direct_confidence, direct_reasons = manager._grade_signal(updated, 0.6, 0.6, regime[0], vix_value=15)
        frozen_technical = {
            "same_bar_snapshot_adopted": same_bar_adopted,
            "maturity_confidence": c.confidence,
            "current_candle_full_grade_confidence": direct_confidence,
            "current_candle_full_grade": direct_grade,
            "current_candle_reasons": direct_reasons,
            "qualification": "Design mismatch if maturity is meant to re-evaluate the forming setup; frozen birth technicals are explicit policy today.",
        }
        assert not same_bar_adopted and c.confidence > direct_confidence + 0.1

        # A currently adverse book still supplies a positive text exception at
        # market-location admission because regrade leaves old reasons/score.
        c = candidate()
        c.adx_value = 40.0
        c.grade, c.confidence, c.reasons = manager._grade_signal(c, 1.0, 0.6, regime[0], vix_value=15)
        regime[0] = "RANGING"
        fresh["aggregate"]["score_norm"] = -0.6
        probe._regrade_matured_candidate(c)
        frame = pd.DataFrame({
            "open": [119.5 - i for i in range(20)],
            "high": [120.0 - i for i in range(20)],
            "low": [118.0 - i for i in range(20)],
            "close": [119.0 - i for i in range(20)],
        }, index=pd.date_range("2026-09-22 09:45", periods=20, freq="min"))
        probe.candles = SimpleNamespace(get_candles=lambda *a: frame)
        stale_allowed, stale_reason = probe._market_location_entry_gate(c)
        synchronized = copy.deepcopy(c)
        synchronized.intelligence_score = -0.6
        synchronized.reasons = intelligence_pillar_score(-0.6, "BUY")[1]
        synchronized_allowed, synchronized_reason = probe._market_location_entry_gate(synchronized)
        maturity_quality = evaluate_quality(QualityInputs(
            timeframe="5min", inst_type="OPT", confidence=c.confidence,
            regime="RANGING", adx_value=c.adx_value, impulse_confirmed=True,
            trend_15m_agrees=True,
        ), settings)
        stale_handoff = {
            "maturity_intelligence_score": c.grade_regrade["intel_score"],
            "candidate_intelligence_score": c.intelligence_score,
            "candidate_reasons": c.reasons,
            "maturity_confidence": c.confidence,
            "maturity_quality_allowed": maturity_quality.allowed,
            "location_gate_with_stale_fields": stale_allowed,
            "location_gate_with_synchronized_fields": synchronized_allowed,
            "location_reason": stale_reason,
            "synchronized_location_reason": synchronized_reason,
            "qualification": "Canonical quality also passes this synthetic setup. Other portfolio, quote, geometry and broker gates are not simulated.",
        }
        assert maturity_quality.allowed and stale_allowed and not synchronized_allowed

        # The purportedly live 15m helper consults a stored bool before live state.
        probe.mtf = SimpleNamespace(engines={"NIFTY_15min": SimpleNamespace(get_state=lambda key: {"position": -1})})
        probe.log_signal_decision_once = lambda *a: None
        stale_trend = probe._candidate_15m_trend_agrees(c)
        without_snapshot = copy.deepcopy(c)
        del without_snapshot.trend_15m_agrees
        current_trend = probe._candidate_15m_trend_agrees(without_snapshot)
        quality_args = dict(timeframe="5min", inst_type="OPT", confidence=0.70,
                            regime="RANGING", adx_value=30.0, impulse_confirmed=True)
        stale_quality = evaluate_quality(QualityInputs(**quality_args, trend_15m_agrees=stale_trend), settings)
        current_quality = evaluate_quality(QualityInputs(**quality_args, trend_15m_agrees=current_trend), settings)
        stale_mtf = {
            "live_15m_position": -1, "stored_agreement": True,
            "helper_with_stored_agreement": stale_trend,
            "helper_without_stored_agreement": current_trend,
            "quality_with_stored_agreement": stale_quality.allowed,
            "quality_with_live_agreement": current_quality.allowed,
            "qualification": "Applies whenever 15m state changes after candidate creation; buffers and queued/manual entries lengthen that interval.",
        }
        assert stale_trend and not current_trend and stale_quality.allowed and not current_quality.allowed

    result = {
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "source_method_lines": {n.name: n.lineno for n in methods},
        "scope": "Synthetic current-source methods only. No broker, scanner startup, production data/config writes, or realized-P&L claim.",
        "mode_caveat": "Stabilization regrade requires INTRACANDLE; CANDLE_CLOSE skips the buffer at scanner.py:34011-34015.",
        "frozen_forming_bar_technical": frozen_technical,
        "stale_intelligence_handoff": stale_handoff,
        "stale_15m_confirmation": stale_mtf,
    }
    payload = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
