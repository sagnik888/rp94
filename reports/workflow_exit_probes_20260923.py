"""Read-only, stdlib-only probes of the CURRENT working-tree exit decisions.

No application import, settings persistence, database, broker or network access.
Extracts current methods/blocks with AST and substitutes market inputs. This proves
decision wiring, not occurrence in any recorded trade or a profitable counterfactual.
Run: python reports/workflow_exit_probes_20260923.py
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
PATHS = ("scanner.py", "trading/trade_manager.py", "config/settings.py")
SOURCES = {p: (ROOT / p).read_text(encoding="utf-8-sig") for p in PATHS}
TREES = {p: ast.parse(s) for p, s in SOURCES.items()}
IST = timezone(timedelta(hours=5, minutes=30))
REFERENCES = {}


def method_node(path, class_name, name):
    cls = next(n for n in TREES[path].body if isinstance(n, ast.ClassDef) and n.name == class_name)
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    REFERENCES[name] = f"{path}:{fn.lineno}"
    return fn


def compile_body(body, namespace):
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + copy.deepcopy(body), type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<extracted-current-source>", "exec"), namespace)


def install(path, original_class, name, target_class, namespace, static=False):
    fn = copy.deepcopy(method_node(path, original_class, name))
    fn.decorator_list = []
    compile_body([fn], namespace)
    value = namespace[name]
    setattr(target_class, name, staticmethod(value) if static else value)


def default_settings():
    cls = next(n for n in TREES["config/settings.py"].body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    values = {}
    for n in cls.body:
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            try:
                values[n.target.id] = ast.literal_eval(n.value)
            except (ValueError, TypeError):
                pass
    return NS(**values)


SETTINGS = default_settings()


class ProbeScanner:
    # Market/context inputs, not replacements for the decision functions under test.
    mode = "REAL"
    auto_mode = True
    _setting_enabled = staticmethod(lambda value, default=True: default if value is None else bool(value))
    _round_trip_cost_rs = lambda self, trade: 80.0
    _eod_protect_active = lambda self, now: False
    _profit_lock_lot_count = staticmethod(lambda trade, qty: 1)
    _get_trade_regime = lambda self, trade, default="": trade.market_regime
    _trade_like_age_seconds = lambda self, trade, now: 30.0
    _option_breathing_distance = lambda self, *a, **kw: 2.0
    _trade_like_market_intelligence_context = lambda self, trade: {}
    _regime_conviction_vector_from_context = lambda self, *a: {}
    _trade_direction_view = lambda self, *a, **kw: "LONG"
    _direction_signal_for_view = lambda self, view: "BUY"
    _intraday_phase_from_session_context = lambda self, ctx: ("", 0.0)
    _trade_like_signed_intelligence = lambda self, *a: 0.0
    _view_aligned_intel_score = lambda self, view, score: score
    _live_direction_context = lambda self, *a: {}
    _regime_opposes_direction = lambda self, *a: False
    _armed_broker_stop_crossed = lambda self, *a: False
    _profit_guard_reversal_context = lambda self, *a, **kw: {"has_context": False}
    _reset_daily_counters = lambda self: None
    log_event = lambda self, *a, **kw: None


class ProbeManager:
    mode = "REAL"


NS_GLOBAL = {
    "Scanner": ProbeScanner,
    "get_settings": lambda: SETTINGS,
    "IST": IST,
    "datetime": datetime,
    "is_risky_regime": lambda regime: regime in {"RANGING", "UNKNOWN", "VOLATILE"},
    "is_trending_regime": lambda regime: regime in {"TRENDING_UP", "TRENDING_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN"},
    "regime_aligns": lambda regime, side: regime.endswith("_UP") if side == "BUY" else regime.endswith("_DOWN"),
    "logger": NS(warning=lambda *a: None, info=lambda *a: None),
}
for name in (
    "_loss_guard_decision", "_profit_lock_min_peak", "_profit_lock_arm_base_rs",
    "_profit_protect_cost_floor_rs", "_profit_lock_arm_cap_rs", "_profit_lock_floor",
    "_profit_lock_ratio_floor", "_profit_lock_ratio_ceiling", "_first_rung_regime_retention",
    "_apply_option_stop_breathing_room", "_choppy_profit_lock_bypass_peak_threshold",
    "_should_defer_trend_option_profit_lock", "_spread_aware_exit_decision", "_close_and_record",
):
    install("scanner.py", "Scanner", name, ProbeScanner, NS_GLOBAL)
install("scanner.py", "Scanner", "_position_cost_rs", ProbeScanner, NS_GLOBAL, static=True)
for name in ("_early_loss_cut_min_hold_met", "_early_loss_cut_hit"):
    install("trading/trade_manager.py", "TradeManager", name, ProbeManager, NS_GLOBAL, static=name.endswith("_hit"))


def probe_option_profit_lock():
    """Actual floor, breathing gate, trend deferral and close-wrapper decisions."""
    sc = ProbeScanner()
    now = datetime(2026, 9, 18, 12, 0)
    trade = NS(instrument="NIFTY", timeframe="15min", inst_type="OPT", direction="LONG",
               entry_price=100.0, current_price=116.0, entry_spot=25000.0,
               instrument_multiplier=0.5, lots=1, lot_size=65, quantity=65,
               initial_stop=90.0, market_regime="RANGING", peak_pnl=1040.0,
               scalp_lock_mode=True, grade="A+", quote_verified=True,
               execution_status="FILLED", signal_timestamp=now - timedelta(seconds=30))
    sc.candles = NS(get_latest_price=lambda instrument: 25000.0)
    sc.trades = NS(open_trades={"probe": trade})
    captured = []
    sc._close_and_record_internal = lambda *args: captured.append(args)
    # The real wrapper executes; only the final broker/persistence boundary is intercepted.
    sc.data = None
    arm = sc._profit_lock_min_peak(True, 650.0, "15min", now, trade_like=trade)
    stop, reason, ratio = sc._profit_lock_floor(100.0, "LONG", 65, 1040.0, True,
                                             initial_risk_rs=650.0, timeframe="15min", now=now, trade_like=trade)
    bypass_arm, bypass_context = sc._choppy_profit_lock_bypass_peak_threshold(
        trade, regime_text="RANGING", latest_time=now, entry_price=100.0, qty=65, peak_pnl=1040.0)
    # Exact live-loop slice from converting the floor to the `continue` after close.
    loop = method_node("scanner.py", "Scanner", "_update_active_trades")
    body = next(n for n in loop.body if isinstance(n, ast.For)).body
    start = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "lock_stop_spot" for t in n.targets))
    end = next(i for i in range(start, len(body)) if isinstance(body[i], ast.If) and ast.unparse(body[i].test) == "lock_reason")
    env = dict(NS_GLOBAL, self=sc, trade=trade, tid="probe", entry_val=100.0, qty=65,
               price=25000.0, trade_price=25000.0, opt_val=116.0, new_stop=24980.0,
               pre_profit_lock_stop=24980.0, lock_stop=stop, lock_reason=reason,
               lock_ratio=ratio, latest_time=now, regime="RANGING", current_pnl_rs=1040.0,
               prem_to_spot=lambda p: 25000.0 + (p-100.0)/0.5,
               spot_to_prem=lambda p: 100.0 + (p-25000.0)*0.5)
    wrapper = ast.For(target=ast.Name(id="_one_pass", ctx=ast.Store()), iter=ast.Tuple(elts=[ast.Constant(1)], ctx=ast.Load()), body=copy.deepcopy(body[start:end+1]), orelse=[])
    compile_body([wrapper], env)
    assert captured and captured[0][2] == "SMART_PROFIT_LOCK"
    assert trade.current_price > stop
    return dict(timeframe=trade.timeframe, trade_age_seconds=30, verified_option_mark=116.0,
                actual_peak_pnl_rs=1040.0, initial_risk_rs=650.0, lock_arm_rs=arm,
                premium_lock_stop=stop, retention=ratio, breathing_bypass_arm_rs=bypass_arm,
                breathing_bypass_context=bypass_context, breathing_clamped=env["profit_lock_breathing_clamped"],
                trend_deferral=env["profit_lock_deferred"], current_index_spot=25000.0,
                compared_index_stop=env["new_stop"], premium_stop_actually_crossed=False,
                resulting_close_reason=captured[0][2], requested_close_price=captured[0][1],
                scope="Current live-loop profit-lock region and close gate; unrelated earlier exits assumed idle.")


def probe_loss_owner_disagreement():
    sc, tm = ProbeScanner(), ProbeManager()
    conf = types.ModuleType("config.settings")
    conf.get_settings = lambda: SETTINGS
    old_module = sys.modules.get("config.settings")
    sys.modules["config.settings"] = conf
    output = {}
    try:
        for label, peak, risk, pnl in (("cost_floor", 0.0, 80.0, -75.0), ("green_trade_deferral", 900.0, 1000.0, -900.0)):
            now = datetime.now(IST)
            t = NS(inst_type="OPT", direction="LONG", entry_price=100.0, current_price=100+pnl/65,
                   initial_stop=100-risk/65, lots=1, lot_size=65, quantity=65, peak_pnl=peak,
                   timeframe="15min", scalp_lock_mode=False, market_regime="RANGING",
                   entry_time=now-timedelta(seconds=30), current_stop=100-risk/65)
            verdict = sc._loss_guard_decision(t, current_pnl_rs=pnl, initial_risk_rs=risk, latest_time=now)
            cut = tm._early_loss_cut_min_hold_met(t) and tm._early_loss_cut_hit(t)
            assert verdict[0] is False and cut is True
            output[label] = dict(scanner_decision=verdict, trade_manager_min_hold_passed=tm._early_loss_cut_min_hold_met(t),
                                 trade_manager_backstop_cuts=cut, hard_stop_crossed=t.current_price<=t.current_stop,
                                 initial_risk_rs=risk, gross_pnl_rs=pnl, round_trip_cost_rs=80.0,
                                 limitation=("Narrow-risk/legacy/recovered position fixture; does not prove current entry policy admits this risk geometry."
                                             if label == "cost_floor" else "Local rule disagreement; preceding profit exits may mask it in the full loop."))
    finally:
        if old_module is None:
            sys.modules.pop("config.settings", None)
        else:
            sys.modules["config.settings"] = old_module
    return output


def main():
    result = {
        "scope": "Extracted CURRENT source with source-default policy values and explicit synthetic market inputs; no live mutations.",
        "source_sha256": {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in PATHS},
        "option_profit_lock_wrong_price_space": probe_option_profit_lock(),
        "early_loss_cut_two_owners": probe_loss_owner_disagreement(),
        "source_references": REFERENCES,
    }
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
