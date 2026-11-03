"""Run the full isolated pytest suite with durable progress and source hashes."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
PREFIX = ROOT / "reports/full_system_24h_complete_20261001"
SOURCES = [p for directory in ("engine", "trading", "data", "config", "intelligence", "dashboard")
           for p in (ROOT / directory).rglob("*.py")] + [ROOT / "scanner.py", ROOT / "main.py"]
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
manifest = {
    "pid": os.getpid(), "started_epoch": time.time(), "status": "running",
    "source_hashes": {str(p.relative_to(ROOT)): digest(p) for p in SOURCES},
    "protected_hashes": {str(p.relative_to(ROOT)): digest(p)
                         for p in (ROOT / ".env", ROOT / "data_store/morning_refresh_summary.json") if p.exists()},
    "command": "pytest -q --tb=no -m 'integration or not integration' --durations=15",
}
manifest_path = Path(str(PREFIX) + ".manifest.json")
def save():
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
save()

import pytest

class DurableProgress:
    def pytest_collection_finish(self, session):
        manifest["collected"] = len(session.items)
        Path(str(PREFIX) + ".collection.json").write_text(
            json.dumps([item.nodeid for item in session.items], indent=2), encoding="utf-8")
        save()
    def pytest_runtest_logreport(self, report):
        if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
            with Path(str(PREFIX) + ".progress.jsonl").open("a", encoding="utf-8") as progress:
                progress.write(json.dumps({"nodeid": report.nodeid, "outcome": report.outcome,
                                           "duration": report.duration}) + "\n")

exit_code = int(pytest.main(["-q", "--tb=no", "-m", "integration or not integration", "--durations=15",
                            "--junitxml=" + str(PREFIX) + ".xml"], plugins=[DurableProgress()]))
manifest.update(status="finished", exit_code=exit_code, finished_epoch=time.time(),
    source_unchanged=all((ROOT/p).exists() and digest(ROOT/p)==h for p,h in manifest["source_hashes"].items()),
    protected_unchanged=all((ROOT/p).exists() and digest(ROOT/p)==h for p,h in manifest["protected_hashes"].items()))
save()
print(json.dumps({k:v for k,v in manifest.items() if not k.endswith("hashes")}))
raise SystemExit(exit_code)
