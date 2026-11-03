"""Isolated repair benchmark; reads source only and never imports the trading app.

Original measured output (1000 calls, four healthy books of 150 rows):
before: 675.791 ms; after: 86.357 ms.
Host helpers model explicit lifecycle IDs, side identity and minute conversion.
This is a method microbenchmark, not an end-to-end scanner measurement.
Run from the repository root: python reports/benchmark_session_repair_read_20261102.py
"""
import ast
import copy
from datetime import datetime
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace as NS


module = ast.parse(Path("scanner.py").read_text(encoding="utf-8"))
cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Scanner")
method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
              and node.name == "_repair_same_side_superseded_session_rows")


def load(node):
    namespace = {"logger": NS(info=lambda *args: None)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 "<isolated_repair_method>", "exec"), namespace)
    return namespace[method.name]


after = load(copy.deepcopy(method))
old = copy.deepcopy(method)
outer = next(node for node in old.body if isinstance(node, ast.For))
outer.body = [node for node in outer.body
              if not (isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "superseded_exits"
                              for target in node.targets))
              and not (isinstance(node, ast.If) and ast.unparse(node.test) == "not superseded_exits")]
inner = next(node for node in outer.body if isinstance(node, ast.For)
             and ast.unparse(node.iter) == "superseded_exits")
original = ast.parse('''for key, row in list(book.items()):
    if str(getattr(row, "action", "ENTRY") or "ENTRY").upper() != "EXIT":
        continue
    if str(getattr(row, "exit_reason", "") or "").upper() != "SUPERSEDED_SIGNAL":
        continue
''').body[0]
inner.iter = original.iter
inner.body = original.body + inner.body
before = load(old)
stamp = datetime(2026, 11, 2, 10, 0)
store = {name: {str(index): NS(lifecycle_id=str(index), instrument=name, timeframe="5min",
                             direction="LONG", inst_type="OPT", option_type="CE",
                             signal_timestamp=stamp, action="ENTRY") for index in range(150)}
         for name in ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]}
host = NS(_session_trade_candidates=store,
          _candidate_lifecycle_key=lambda row: row.lifecycle_id,
          _same_side_refresh_identity=lambda row: (
              row.instrument, row.timeframe, row.direction, row.inst_type, row.option_type),
          _candidate_minute_bucket=lambda value: value.replace(second=0, microsecond=0))
for label, fn in [("before", before), ("after", after)]:
    start = perf_counter()
    for _ in range(1000):
        assert fn(host) is False
    elapsed = perf_counter() - start
    print(f"{label}: 1000 healthy repairs,4x150 rows: {elapsed*1000:.3f}ms ({elapsed:.6f}ms/read)")
