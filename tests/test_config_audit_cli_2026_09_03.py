from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_config_audit_documented_direct_invocation_imports_project_modules(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(root / "scripts" / "config_audit.py"),
            "--env",
            str(tmp_path / "missing.env"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "config audit" in result.stdout
    assert "ordered pairs: all intact" in result.stdout
