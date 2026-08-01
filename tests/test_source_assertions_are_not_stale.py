"""Meta-test: source-text assertions must not silently stop guarding anything.

Many tests here assert on SOURCE TEXT rather than behaviour — `assert "foo" in
inspect.getsource(...)`. That style is brittle in a specific way: it keeps passing
while the thing it guards is renamed or removed underneath it, and nobody notices
until the guarded behaviour has already broken in production.

That is not hypothetical. test_execution_lease_acquired_on_real_2026_07_31 asserted the
dynamic broker got an account_id by string-matching `SmartApiBroker(`. After the Kite
migration that path built a broker from a permanently-None api with the lease keyed on
a retired setting — producing a broker that could not trade AND had no execution lease,
failing open on the one guard that must never fail open.

This test does not forbid the pattern; rewriting ~200 assertions would be its own
project. It fails when such an assertion starts referring to text that exists nowhere
in the live source, which is the point at which it has stopped guarding anything.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = (
    "venv", "codex-pytest", "__pycache__", "worktrees", ".git",
    "static_backup", "scratch", "archive", ".benchmarks",
)

# Assertions that legitimately check text the TEST builds (report bodies, log lines,
# rendered output) rather than repo source. Keyed by file, since these modules verify
# generated artefacts end to end.
GENERATED_OUTPUT_MODULES = {
    "test_live_session_tracker_scheduler.py",
    "test_engine_lab.py",
}


def _live_source_blob() -> str:
    chunks = []
    for pattern in ("**/*.py", "**/*.js", "**/*.html", "**/*.bat", "**/*.ps1"):
        for path in ROOT.glob(pattern):
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            if any(part in rel for part in SKIP_PARTS) or rel.startswith("tests/"):
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
    return "\n".join(chunks)


def _source_text_assertions():
    pattern = re.compile(
        r"""\s*assert\s+(["'])(.+?)\1\s+in\s+(body|src|source|code|js|html|text)\b"""
    )
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name in GENERATED_OUTPUT_MODULES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "getsource" not in text and "read_text(" not in text:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            match = pattern.match(line)
            if not match:
                continue
            needle = match.group(2)
            # Very short needles match incidentally; they prove nothing either way.
            if len(needle) >= 6:
                yield path.name, line_no, needle


def test_no_source_assertion_guards_text_that_no_longer_exists():
    blob = _live_source_blob()
    assert len(blob) > 100_000, "source corpus looks wrong; the check would be vacuous"

    stale = [
        f"{name}:{line_no} guards {needle!r}, which appears nowhere in live source"
        for name, line_no, needle in _source_text_assertions()
        if needle.replace('\\"', '"') not in blob
    ]
    assert not stale, (
        "These assertions no longer guard anything — the code they describe was renamed "
        "or removed, and they will keep passing regardless:\n  " + "\n  ".join(stale)
    )


def test_the_sweep_actually_inspects_something():
    """Guards the guard: a broken regex here would make the check pass vacuously."""
    found = list(_source_text_assertions())
    assert len(found) > 50, f"expected many source-text assertions, found {len(found)}"
