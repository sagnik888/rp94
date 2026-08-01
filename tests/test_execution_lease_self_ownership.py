"""A process must not block itself out of its own broker account.

Windows byte-range locks (msvcrt.locking) are per-HANDLE, not per-process. A second
AccountExecutionLease object for the same account inside ONE process therefore cannot
take the lock its sibling holds, and the refusal surfaced as

    entry order blocked: REAL broker account is owned by another UT1 process

while naming this process's own pid. Any path that rebuilds SmartApiBroker while an
earlier one still holds the lease — scanner.py's dynamic reconnect, an AngelOne re-auth
— would then refuse every one of its own orders.

The safety invariant that must NEVER regress: a genuinely different process still gets
refused. Self-ownership is only claimed when the live holder's metadata names this exact
pid AND host, which can only be us — the lock is held, so the pid cannot have been
recycled, and every acquire() rewrites the metadata.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from trading.account_execution_lease import AccountExecutionLease, lease_owner_description


def _lease(path: Path, instance: str = "UT1:8001") -> AccountExecutionLease:
    lease = AccountExecutionLease("TESTACCT1", instance)
    lease.path = path
    return lease


def test_second_lease_object_in_the_same_process_is_not_locked_out(tmp_path):
    path = tmp_path / "acct.lock"
    first = _lease(path)
    second = _lease(path)
    try:
        assert first.acquire() is True
        assert second.acquire() is True, "the process locked itself out of its own account"
        assert second.held is True
    finally:
        first.release()
        second.release()


def test_a_different_process_is_still_refused(tmp_path):
    """The whole point of the lease: two UT1 processes must never net one account."""
    path = tmp_path / "acct.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
                import sys, time
                from pathlib import Path
                sys.path.insert(0, {os.getcwd()!r})
                from trading.account_execution_lease import AccountExecutionLease
                lease = AccountExecutionLease("TESTACCT1", "UT1:other")
                lease.path = Path({str(path)!r})
                assert lease.acquire()
                print("held", flush=True)
                time.sleep(30)
            """),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        mine = _lease(path)
        assert mine.acquire() is False, "two processes just claimed one broker account"
        assert mine.held is False
        assert "UT1:other" in lease_owner_description(mine.owner_metadata)
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_release_clears_self_ownership(tmp_path):
    path = tmp_path / "acct.lock"
    first = _lease(path)
    second = _lease(path)
    try:
        assert first.acquire() is True
        assert second.acquire() is True
        second.release()
        assert second.held is False
    finally:
        first.release()
        second.release()


def test_owner_description_does_not_invent_a_phantom_owner():
    """An unreadable lock file must not be reported as a confident second instance."""
    assert "owner unknown" in lease_owner_description({})
    assert "owner unknown" in lease_owner_description(None)
    described = lease_owner_description(
        {"instance_id": "UT1:8001", "pid": 4242, "host": "box", "acquired_at": "2026-07-31T09:00:00+00:00"}
    )
    assert "pid 4242" in described and "box" in described
