"""Isolated, read-only probes of current entry code; writes only its report JSON.

Run: python -B reports/workflow_entry_probes_20260923.py
Production functions are AST-extracted. No scanner/config/database module is imported,
and no broker, network, candle store, or running application is instantiated.
These demonstrate mechanical inconsistencies, not trading profitability.
"""
from __future__ import annotations

import ast
import copy
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def tree(relative):
    return ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"))


def function(relative, name, owner=None):
    nodes = tree(relative).body
    if owner:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == owner).body
    result = copy.deepcopy(next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name))
    result.decorator_list = []
    return result


def install_function(namespace, relative, name, owner=None):
    node = function(relative, name, owner)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / relative), "exec"), namespace)
    return namespace[name]


def main():
    settings = SimpleNamespace(
        ut_dynamic_confidence=True,
        ut_confidence_logistic=True,
        ut_confidence_logistic_k=5.0,
        ut_exhaustion_confidence_cap=0.82,
        scanner_engine="quad_prime",
        utbot_prime_confidence_per_agreement=0.03,
        utbot_prime_max_confidence_bonus=0.09,
        stabilization_regrade_enabled=True,
        range_day_regime_pillar_damp_enabled=True,
        regime_veto_enabled=True,
        regime_veto_adx_floor=18.0,
    )
    # Imports inside extracted production functions resolve only to these stubs.
    config_package = types.ModuleType("config")
    config_package.__path__ = []
    config_module = types.ModuleType("config.settings")
    config_module.get_settings = lambda: settings
    intel_package = types.ModuleType("intelligence")
    intel_package.__path__ = []
    calibrator = types.ModuleType("intelligence.confidence_calibration")
    calibrator.calibrate = lambda confidence, *args, **kwargs: confidence
    sys.modules.update({"config": config_package, "config.settings": config_module,
                        "intelligence": intel_package, "intelligence.confidence_calibration": calibrator})
    logger = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None)
    env = {"logger": logger, "get_settings": lambda: settings,
           "GRADE_BANDS": (("A+", .85), ("A", .70), ("B+", .55), ("B", .40))}
    # These two modules are pure helpers; execute their source without importing the app.
    for relative in ("engine/intelligence_score.py", "engine/market_regime.py"):
        exec(compile((ROOT / relative).read_text(encoding="utf-8-sig"), relative, "exec"), env)
    for name in ("setting_enabled", "confidence_to_grade", "is_choppy_regime"):
        install_function(env, "engine/signal_quality_policy.py", name)
    env["_setting_enabled"] = env["setting_enabled"]
    for name in ("intelligence_pillar_score", "regime_pillar_score", "confidence_from_total_score"):
        install_function(env, "engine/signal_manager.py", name)
    grade = install_function(env, "engine/signal_manager.py", "_grade_signal", "SignalManager")
    regrade = install_function(env, "scanner.py", "_regrade_matured_candidate", "Scanner")
    context = install_function(env, "engine/signal_processor.py", "_candidate_timeframe_context", "SignalProcessor")
    stamp = install_function(env, "engine/signal_processor.py", "_graded_candidate_context", "SignalProcessor")
    veto = install_function(env, "engine/signal_processor.py", "directional_fut_veto")

    def signal(adx=45.0):
        return SimpleNamespace(signal_type="BUY", adx_value=adx, stop_distance=1.0,
                               atr_value=1.0, raw_candle=None, instrument="NIFTY",
                               timeframe="5min", trend_agreement=0)

    buffer_results = []
    for birth_intel, fresh_intel, birth_regime, fresh_regime in (
        (.8, .0, "TRENDING_UP", "RANGING"),
        (.0, .8, "RANGING", "TRENDING_UP"),
    ):
        birth, fresh = signal(), signal()
        bg, bc, _ = grade(None, birth, .7, birth_intel, birth_regime, vix_value=15)
        fg, fc, _ = grade(None, fresh, .7, fresh_intel, fresh_regime, vix_value=15)
        candidate = SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG",
                                    adx_value=45., grade=bg, confidence=bc, grade_pillars=birth.grade_pillars)
        scanner = SimpleNamespace(
            _diag_inc=lambda *a, **k: None, _setting_enabled=env["setting_enabled"],
            _fresh_exact_trade_intel=lambda *a, **k: {"aggregate": {"score_norm": fresh_intel}, "session_context": {}},
            _get_trade_regime=lambda *a, **k: fresh_regime,
        )
        regrade(scanner, candidate)
        buffer_results.append({"birth": {"grade": bg, "confidence": bc},
                               "stored_birth_adjustment": birth.grade_pillars["adjustments"],
                               "fresh_direct_grading": {"grade": fg, "confidence": fc},
                               "maturity_grading": {"grade": candidate.grade, "confidence": candidate.confidence}})
    assert buffer_results[0]["fresh_direct_grading"]["grade"] == "B+"
    assert buffer_results[0]["maturity_grading"]["grade"] == "B"
    assert buffer_results[1]["maturity_grading"]["confidence"] > settings.ut_exhaustion_confidence_cap

    process = function("engine/signal_processor.py", "process_best_signal", "SignalProcessor")
    veto_call = next(n for n in ast.walk(process) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "directional_fut_veto")
    route_call = next(n for n in ast.walk(process) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute) and n.func.attr == "_resolve_instrument_type")
    # Verify neither caller's 5m value is reassigned anywhere in the production body.
    unbound = [n.id for n in ast.walk(process) if isinstance(n, ast.Name)
               and isinstance(n.ctx, ast.Store) and n.id in {"regime", "intel_score", "intel_result"}]
    assert not unbound, unbound
    assert next(k.value.id for k in veto_call.keywords if k.arg == "regime") == "regime"
    assert next(k.value.id for k in route_call.keywords if k.arg == "regime") == "regime"
    assert next(k.value.id for k in route_call.keywords if k.arg == "intel_score") == "intel_score"

    timeframe_results = []
    for caller_regime, own_regime in (("TRENDING_DOWN", "TRENDING_UP"), ("TRENDING_UP", "TRENDING_DOWN")):
        _, own_score, own_label = context("15min", {}, -.8, caller_regime,
                                         {"15min": {"intel_result": {}, "intel_score": .8, "regime": own_regime}})
        best_sig = signal(adx=30.)
        best_sig.timeframe = "15min"
        best_sig.graded_regime, best_sig.graded_intel_score = own_label, own_score
        local = dict(env, settings=settings, inst_type="FUT", best_sig=best_sig,
                     regime=caller_regime, adx_value=30., fast_thrust_override=False,
                     is_reversion_scalp=False)
        # Execute the actual production call expression, not a rewritten equivalent.
        observed_veto = eval(compile(ast.Expression(copy.deepcopy(veto_call)), "live_futures_veto", "eval"), local)
        expected_veto = veto(settings, inst_type="FUT", signal_type="BUY", regime=own_label, adx_value=30.)
        timeframe_results.append({"candidate_timeframe": "15min", "signal": "BUY",
                                  "caller_5m_regime": caller_regime, "graded_15m_regime": own_label,
                                  "production_veto": observed_veto, "own_timeframe_veto": expected_veto,
                                  "final_candidate_metadata": stamp(best_sig, caller_regime, -.8)})
    assert timeframe_results[0]["production_veto"] and not timeframe_results[0]["own_timeframe_veto"]
    assert not timeframe_results[1]["production_veto"] and timeframe_results[1]["own_timeframe_veto"]
    report = {"scope": "Current-source isolated mechanism probes; no live execution or profitability assertion",
              "reachability": {
                  "buffer": "New CANDLE_CLOSE candidates skip stabilization/regrade (scanner.py:34010-34015); bug applies to INTRACANDLE buffered candidates.",
                  "timeframe_veto": "FUT veto applies only to FUT, enabled regime veto, no thrust/reversion exemption. AUTO can reroute vetoed FUT to OPT. Other later gates may still reject a wrongly unvetoed candidate.",
                  "snapshot": "Stub settings isolate mechanisms; this artifact does not inspect or assert current running application configuration."
              },
              "settings": vars(settings), "buffer_nonlinear_adjustment": buffer_results,
              "own_timeframe_pre_candidate_veto": timeframe_results,
              "source_evidence": {"veto_call_line": veto_call.lineno, "routing_call_line": route_call.lineno,
                                  "caller_context_reassignments": unbound,
                                  "regrade_line": function("scanner.py", "_regrade_matured_candidate", "Scanner").lineno}}
    destination = Path(__file__).with_suffix(".json")
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
