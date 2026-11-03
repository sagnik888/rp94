"""Run the full pytest suite in deterministic, collection-balanced shards."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Wall-clock assertions are invalid under deliberate multi-process contention.
# Keep these files in the full run, but execute them after parallel shards finish.
SERIAL_TEST_FILES = {
    "tests/test_pipeline_speed_e2e.py",
    "tests/test_fyers_history_cannot_stall_the_loop_2026_08_31.py",
    "tests/test_angelone_latency_futures_volume_2026_09_08.py",
}


def _collect() -> dict[str, int]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode not in (0, 5):
        raise RuntimeError(result.stdout + result.stderr)
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        node = line.strip()
        if not node.startswith("tests/") and not node.startswith("tests\\"):
            continue
        path = node.split("::", 1)[0].replace("\\", "/")
        counts[path] = counts.get(path, 0) + 1
    return counts


def _balanced(counts: dict[str, int], shard_count: int) -> list[list[str]]:
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0] * shard_count
    for path, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        target = min(range(shard_count), key=lambda idx: (totals[idx], idx))
        shards[target].append(path)
        totals[target] += count
    return [sorted(group) for group in shards]


def _run(index: int, paths: list[str], output_dir: Path) -> dict[str, object]:
    started = time.monotonic()
    log_path = output_dir / f"shard-{index + 1:02d}.log"
    command = [sys.executable, "-m", "pytest", "-q", *paths]
    with log_path.open("w", encoding="utf-8") as stream:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return {
        "shard": index + 1,
        "exit_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "files": len(paths),
        "log": str(log_path.relative_to(ROOT)).replace("\\", "/"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=8)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--output", default="reports/test-shards")
    args = parser.parse_args()
    shard_count = max(1, args.shards)
    output_dir = ROOT / args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = _collect()
    parallel_counts = {
        path: count for path, count in counts.items() if path not in SERIAL_TEST_FILES
    }
    serial_paths = sorted(path for path in counts if path in SERIAL_TEST_FILES)
    shards = _balanced(parallel_counts, shard_count)
    groups = [*shards, serial_paths] if serial_paths else shards
    manifest = {
        "collected_tests": sum(counts.values()),
        "test_files": len(counts),
        "shards": [
            {
                "shard": idx + 1,
                "estimated_tests": sum(counts[path] for path in paths),
                "files": paths,
                "execution": "serial" if serial_paths and idx == len(shards) else "parallel",
            }
            for idx, paths in enumerate(groups)
        ],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.parallel, shard_count))) as pool:
        futures = {
            pool.submit(_run, idx, paths, output_dir): idx
            for idx, paths in enumerate(shards)
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"shard {result['shard']}/{len(groups)}: exit={result['exit_code']} "
                f"duration={result['duration_seconds']}s log={result['log']}",
                flush=True,
            )
    if serial_paths:
        result = _run(len(shards), serial_paths, output_dir)
        results.append(result)
        print(
            f"shard {result['shard']}/{len(groups)}: exit={result['exit_code']} "
            f"duration={result['duration_seconds']}s log={result['log']}",
            flush=True,
        )
    results.sort(key=lambda item: int(item["shard"]))
    summary = {**manifest, "results": results, "passed": all(r["exit_code"] == 0 for r in results)}
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
