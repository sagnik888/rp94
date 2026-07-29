"""Regression: update_candles must not shadow the module-level pandas import.

A function-level `import pandas as pd` inside CandleBuilder.update_candles made `pd` a
LOCAL name for the whole function. It sat inside `if not ref or ref <= 0:`, so whenever
a valid reference price existed (i.e. normal operation after the first tick) the import
never ran and the later `pd.isna(ref)` / `pd.concat(...)` calls raised
"cannot access local variable 'pd'". That failed every candle update, and therefore
every historical fetch: "Fetch failed for NIFTY 5min: cannot access local variable 'pd'".
"""
import ast
from pathlib import Path

import pandas as pd

from data.candle_builder import CandleBuilder


def _frame(start, n, base):
    return pd.DataFrame(
        {"open": [base] * n, "high": [base + 1] * n, "low": [base - 1] * n,
         "close": [base + 0.5] * n, "volume": [10] * n},
        index=pd.date_range(start, periods=n, freq="1min"),
    )


def test_update_candles_works_when_reference_price_is_known():
    """The exact failing path: a valid ref skips the branch that held the import."""
    cb = CandleBuilder(max_candles=100)
    cb._latest_prices["NIFTY"] = 100.0
    cb.update_candles("NIFTY", _frame("2026-07-29 09:15", 2, 100), "1min")
    assert len(cb._candles_1min["NIFTY"]) == 2


def test_repeated_updates_concat_without_unbound_local():
    cb = CandleBuilder(max_candles=100)
    cb.update_candles("NIFTY", _frame("2026-07-29 09:15", 2, 100), "1min")
    cb._latest_prices["NIFTY"] = 100.0
    cb.update_candles("NIFTY", _frame("2026-07-29 09:20", 2, 100), "1min")
    assert len(cb._candles_1min["NIFTY"]) == 4


def test_no_function_level_pandas_import_shadows_module_scope():
    """Structural guard so the shadowing cannot be reintroduced."""
    tree = ast.parse(Path(CandleBuilder.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import):
                for alias in inner.names:
                    if alias.name == "pandas":
                        offenders.append(f"{node.name}:{inner.lineno}")
    assert offenders == [], f"function-level pandas import shadows module scope: {offenders}"
