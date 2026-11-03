"""A stable identity for the code that actually produced a decision row.

A 2026-09-15 refusal could not be attributed to any build: the only marker on a decision
row was ``strategy.revision``, a constant string from settings, so a row emitted by an
edited working tree and a row emitted by a released commit were indistinguishable. Commit
SHA alone does not close that, because this workspace runs with a dirty tree for days at a
time and the interesting differences live in the uncommitted hunks.

The fingerprint is therefore the commit SHA *and* a digest over the contents of the
modules that decide admission. Two processes agree if and only if the decision code is
byte-identical, whether or not anything was committed.

Computed once per process and cached: the digest reads a few megabytes, which is fine at
start-up and would not be fine on every rejection.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Only the modules that can change an admission decision. Adding a file here changes
# every future fingerprint, so keep it to code whose edit genuinely re-grades a signal.
FINGERPRINT_SOURCES = (
    "engine/quality_verdict.py",
    "engine/signal_quality_policy.py",
    "engine/signal_processor.py",
    "engine/entry_data_quality.py",
    "intelligence/price_action.py",
    "intelligence/regime_detector.py",
    "config/settings.py",
    "scanner.py",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CACHE: dict = {}
_LOCK = threading.Lock()

# Tracked source whose uncommitted hunks can change what a running process decides.
# Tests and the frozen replay copies under reports/ are excluded: editing a test does
# not change the build that is trading, and must not look as if it did.
DIFF_PATHSPECS = ("*.py", ":(exclude)tests/*", ":(exclude)reports/*")
_GIT_TIMEOUT_SECONDS = 2.0
_UNKNOWN = "unknown"
_IST = timezone(timedelta(hours=5, minutes=30))
# Naive IST, the rtracker clock convention. Taken when this module is first imported,
# which the scanner does at start-up (engine.signal_processor imports it at top level),
# so it is the process start to within import latency.
_PROCESS_STARTED_AT = datetime.now(_IST).replace(tzinfo=None).isoformat()
_STAMP_CACHE: dict = {}
# Separate from _LOCK: computing the stamp calls build_fingerprint, which takes _LOCK.
_STAMP_LOCK = threading.Lock()


def _git_head_sha(root: Path) -> str:
    """The checked-out commit, read from .git directly.

    A subprocess would be more correct about worktrees and packed refs, but this runs at
    import time inside a trading process; a hang here is worse than an empty string.
    """
    try:
        head_file = root / ".git" / "HEAD"
        if not head_file.is_file():
            return ""
        head = head_file.read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            ref_file = root / ".git" / ref
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8", errors="replace").strip()[:12]
            packed = root / ".git" / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("#") or " " not in line:
                        continue
                    sha, name = line.split(" ", 1)
                    if name.strip() == ref:
                        return sha.strip()[:12]
            return ""
        return head[:12]
    except Exception:
        return ""


def _source_digest(root: Path) -> str:
    """12 hex chars over the contents of the decision modules.

    A file that cannot be read is folded in by name with a MISSING marker rather than
    skipped, so a truncated deployment does not silently produce a matching digest.
    """
    digest = hashlib.sha256()
    for rel in FINGERPRINT_SOURCES:
        digest.update(rel.encode("utf-8"))
        try:
            digest.update(hashlib.sha256((root / rel).read_bytes()).digest())
        except Exception:
            digest.update(b"MISSING")
    return digest.hexdigest()[:12]


def build_fingerprint(root: Path | None = None, *, refresh: bool = False) -> str:
    """``<commit>+<digest>``, or just ``<digest>`` outside a git checkout.

    ``refresh`` is for tests; production callers take the cached value.
    """
    base = Path(root) if root is not None else _REPO_ROOT
    key = str(base)
    if not refresh:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
    with _LOCK:
        if refresh or key not in _CACHE:
            sha = _git_head_sha(base)
            digest = _source_digest(base)
            _CACHE[key] = f"{sha}+{digest}" if sha else digest
    return _CACHE[key]


def build_identity(settings=None, scanner=None, root: Path | None = None) -> dict:
    """The full provenance triple stamped onto a decision row.

    Rejections previously returned before the accepted-candidate stamp ran, so every
    refused row carried an empty revision and an empty data_provenance while every
    admitted row carried both. That asymmetry is what made refusals unattributable.
    """
    mode = str(getattr(scanner, "mode", "") or "").upper()
    return {
        "strategy_revision": str(getattr(settings, "strategy_revision", "") or ""),
        "strategy_direction_mode": str(
            getattr(settings, "strategy_direction_mode", "") or ""
        ),
        "data_provenance": "LIVE" if mode == "REAL" else "BACKTEST",
        "build_fingerprint": build_fingerprint(root),
    }


def _run_git(root: Path, *args: str) -> bytes | None:
    """stdout of one git command, or None on any failure, non-zero exit or timeout."""
    kwargs = {}
    if sys.platform == "win32":
        # No console window flashing up from inside the trading process.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            # --no-optional-locks: never take .git/index.lock under a concurrent git.
            ["git", "-C", str(root), "--no-optional-locks", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            **kwargs,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _untracked_digest_parts(base: Path, listing: bytes) -> list[bytes]:
    """``path NUL sha256(bytes)`` per untracked source file, in sorted path order.

    ``git diff HEAD`` never shows untracked files, and several live runtime modules sit
    untracked in this workspace; without their contents an edit to one of them alone
    left the stamp unchanged. An unreadable file is folded in by name with a MISSING
    marker, as in ``_source_digest``.
    """
    parts = []
    for raw in sorted(p for p in listing.split(b"\0") if p):
        try:
            content = hashlib.sha256(
                (base / raw.decode("utf-8", "surrogateescape")).read_bytes()
            ).digest()
        except Exception:
            content = b"MISSING"
        parts.append(raw + b"\0" + content)
    return parts


def _compute_build_stamp(base: Path) -> dict:
    sha_out = _run_git(base, "rev-parse", "HEAD")
    sha = sha_out.decode("ascii", "replace").strip()[:12] if sha_out else ""
    dirty = None
    diff_hash = _UNKNOWN
    if sha:
        diff = _run_git(
            base, "diff", "HEAD", "--no-color", "--no-ext-diff", "--", *DIFF_PATHSPECS
        )
        untracked = _run_git(
            base, "ls-files", "--others", "--exclude-standard", "-z", "--", *DIFF_PATHSPECS
        )
        if diff is not None and untracked is not None:
            parts = _untracked_digest_parts(base, untracked)
            dirty = bool(diff.strip() or parts)
            digest = hashlib.sha256(diff)
            for part in parts:
                digest.update(part)
            # With no untracked source this equals sha256(diff), the pre-existing value.
            diff_hash = digest.hexdigest()[:12]
        elif diff is not None and diff.strip():
            # The listing failed, so the hash is unknown, but tracked hunks alone
            # already prove the tree is dirty. An empty diff proves nothing.
            dirty = True
    return {
        "git_sha": sha or _UNKNOWN,
        "git_dirty": dirty,
        "diff_hash": diff_hash,
        "fingerprint": build_fingerprint(base),
        "process_started_at": _PROCESS_STARTED_AT,
        "pid": os.getpid(),
    }


def process_build_stamp(root: Path | None = None, *, refresh: bool = False) -> dict:
    """Which build this process is running, for the ``build`` block of decision rows.

    On 2026-09-16 three restarts loaded three different builds off one commit, and the
    rows could not say which build wrote them. ``git_dirty`` plus ``diff_hash`` (over
    the tracked-source diff against HEAD and the contents of untracked, non-ignored
    source outside tests/ and reports/) separate those builds; ``fingerprint`` is the
    decision-module content digest above; ``process_started_at`` and ``pid`` separate
    restarts of an identical build.

    Computed ONCE per process: the first call shells out to git (bounded by a short
    timeout, ``"unknown"`` / ``None`` when git is missing or fails) and every later call
    returns a copy of the cached dict. ``refresh`` is for tests.
    """
    base = Path(root) if root is not None else _REPO_ROOT
    key = str(base)
    if not refresh:
        cached = _STAMP_CACHE.get(key)
        if cached is not None:
            return dict(cached)
    with _STAMP_LOCK:
        if refresh or key not in _STAMP_CACHE:
            if refresh:
                build_fingerprint(base, refresh=True)
            _STAMP_CACHE[key] = _compute_build_stamp(base)
        return dict(_STAMP_CACHE[key])


__all__ = [
    "DIFF_PATHSPECS",
    "FINGERPRINT_SOURCES",
    "build_fingerprint",
    "build_identity",
    "process_build_stamp",
]
