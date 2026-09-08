import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
POOL_VARIABLES = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def test_numeric_thread_budget_is_applied_before_application_imports():
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    budget = source.index("for _thread_env in (")
    assert budget < source.index("import uvicorn")
    assert budget < source.index("from scanner import Scanner")


def test_numeric_thread_budget_is_configurable_and_reaches_every_pool():
    environment = os.environ.copy()
    environment["UT1_NUMERIC_THREADS"] = "2"
    for name in POOL_VARIABLES:
        environment.pop(name, None)
    code = (
        "import json, os, main; "
        f"print(json.dumps({{name: os.environ.get(name) for name in {POOL_VARIABLES!r}}}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    values = json.loads(result.stdout.strip().splitlines()[-1])

    assert values == {name: "2" for name in POOL_VARIABLES}
