"""Fast manifest writer for restore points by skipping excluded directories during walk."""

import os
import hashlib
import json
from datetime import datetime
from pathlib import Path

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".benchmarks",
    "venv",
    "logs",
    "runtime_logs",
    "archive",
    "data_store",
    "ut1-index-final3",
}

EXCLUDE_NAMES = {
    "audit_results.json",
    "RESTORE_POINT_24_MANIFEST.json",
    "RESTORE_POINT_25_MANIFEST.json",
    "RESTORE_POINT_26_MANIFEST.json",
    "RESTORE_POINT_26_MANIFEST_SELF.json",
    "RESTORE_POINT_27_MANIFEST.json",
    "RESTORE_POINT_28_MANIFEST.json",
    "fyers_token.json",
    "market_data.py_fixed_func.py",
    "scanner_frictionless.py",
    "scanner_old.py",
    "ws_out.json",
}

INCLUDE_SUFFIXES = {
    ".py",
    ".js",
    ".css",
    ".html",
    ".json",
    ".md",
    ".txt",
    ".ini",
    ".bat",
    ".ps1",
    ".gitignore",
}

def iter_source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter dirnames in-place to prune the walk traversal
        dirnames[:] = [
            d for d in dirnames 
            if d not in EXCLUDE_DIRS 
            and not d.startswith("restore_point_")
        ]
        
        for fname in filenames:
            if fname in EXCLUDE_NAMES:
                continue
            if fname.startswith("RESTORE_POINT_") and fname.endswith("_MANIFEST.json"):
                continue
            path = Path(dirpath) / fname
            if path.suffix.lower() in INCLUDE_SUFFIXES or fname == ".gitignore":
                yield path

def build_manifest(root: Path) -> dict:
    files = []
    aggregate = hashlib.sha256()
    all_paths = sorted(list(iter_source_files(root)), key=lambda p: p.relative_to(root).as_posix())
    
    for path in all_paths:
        rel = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        aggregate.update(rel.encode("utf-8"))
        aggregate.update(digest.encode("ascii"))
        files.append({"path": rel, "sha256": digest, "bytes": path.stat().st_size})
    return {
        "generated_at": datetime.now().isoformat(),
        "root": str(root),
        "file_count": len(files),
        "source_tree_sha256": aggregate.hexdigest(),
        "files": files,
    }

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    manifest = build_manifest(Path(args.root).resolve())
    Path(args.out).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("file_count", "source_tree_sha256")}, indent=2))

if __name__ == "__main__":
    main()
