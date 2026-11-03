"""Create a reproducible, non-destructive scanner baseline manifest.

The manifest identifies the exact dirty-tree inputs used by an evaluation without
staging, committing, copying secrets, or pretending that HEAD identifies local work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
INCLUDED_ROOTS = ("config", "data", "engine", "intelligence", "scripts", "tests", "trading")
INCLUDED_FILES = ("main.py", "scanner.py", "pytest.ini", "requirements.txt")
EXCLUDED_PARTS = {".git", ".pytest_cache", "__pycache__", ".codex-test-runtime", ".venv", "venv"}
SAFE_ENV_KEYS = (
    "SCANNER_ENGINE",
    "AUTO_MODE",
    "UT_TIMEFRAME_ENTRY_POLICY",
    "UT_SIGNAL_MODE",
)


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_files() -> Iterable[Path]:
    for name in INCLUDED_FILES:
        path = ROOT / name
        if path.is_file():
            yield path
    for root_name in INCLUDED_ROOTS:
        root = ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not EXCLUDED_PARTS.intersection(path.parts):
                if path.suffix.lower() in {".py", ".ini", ".json", ".yaml", ".yml"}:
                    yield path


def _safe_env() -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    dotenv: dict[str, str] = {}
    env_path = ROOT / ".env"
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in SAFE_ENV_KEYS:
                dotenv[key.strip()] = value.strip().strip("'\"")
    for key in SAFE_ENV_KEYS:
        values[key] = os.environ.get(key, dotenv.get(key))
    return values


def build_manifest() -> dict:
    status_lines = [line for line in _git("status", "--short").splitlines() if line]
    files = sorted(set(_source_files()))
    hashes = {
        path.relative_to(ROOT).as_posix(): {"sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in files
    }
    tracked_modified = sum(1 for line in status_lines if not line.startswith("??"))
    untracked = sum(1 for line in status_lines if line.startswith("??"))
    source_fingerprint = hashlib.sha256(
        "\n".join(f"{name}:{item['sha256']}" for name, item in hashes.items()).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "head": _git("rev-parse", "HEAD"),
            "tracked_modified_paths": tracked_modified,
            "untracked_status_entries": untracked,
            "clean": not status_lines,
        },
        "runtime_selectors": _safe_env(),
        "source_fingerprint": source_fingerprint,
        "files": hashes,
        "notes": [
            "Manifest is evidence, not a backup; preserve the referenced worktree separately.",
            "No secret-bearing environment values are included.",
            "Test results belong in a separate immutable evaluation report keyed by source_fingerprint.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_manifest()
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "source_fingerprint": payload["source_fingerprint"],
        "file_count": len(payload["files"]),
        "repository": payload["repository"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
