"""Cross-process ownership for one REAL broker account.

AngelOne positions are account-netted, so two UT1 processes cannot safely
reconcile or protect the same account independently. This lease uses an OS
file lock keyed only by the broker account id; app folders and dashboard ports
therefore contend for the same execution right.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import socket
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class AccountExecutionLease:
    """Non-blocking, process-scoped exclusive lease for a broker account."""

    def __init__(self, account_id: str, instance_id: str = "") -> None:
        normalized = str(account_id or "").strip().upper()
        if not normalized:
            raise ValueError("account_id is required for a REAL execution lease")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
        root = Path(tempfile.gettempdir()) / "ut1-account-execution"
        self.path = root / f"angelone-{digest}.lock"
        self.account_fingerprint = digest
        self.instance_id = str(instance_id or "UT1").strip() or "UT1"
        self._handle = None
        self._guard = threading.RLock()
        self._owner_metadata: Dict[str, Any] = {}
        atexit.register(self.release)

    @property
    def held(self) -> bool:
        with self._guard:
            return self._handle is not None and not self._handle.closed

    @property
    def owner_metadata(self) -> Dict[str, Any]:
        with self._guard:
            return dict(self._owner_metadata)

    def _read_metadata(self, handle) -> Dict[str, Any]:
        try:
            handle.seek(1)
            raw = handle.read().decode("utf-8", errors="ignore").strip()
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    @staticmethod
    def _lock(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self) -> bool:
        with self._guard:
            if self.held:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b", buffering=0)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            try:
                self._lock(handle)
            except (OSError, BlockingIOError):
                self._owner_metadata = self._read_metadata(handle)
                handle.close()
                return False

            metadata = {
                "instance_id": self.instance_id,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "account_fingerprint": self.account_fingerprint,
            }
            handle.seek(1)
            handle.truncate()
            handle.write(json.dumps(metadata, sort_keys=True).encode("utf-8"))
            handle.flush()
            self._handle = handle
            self._owner_metadata = metadata
            return True

    def release(self) -> None:
        with self._guard:
            handle = self._handle
            self._handle = None
            if handle is None or handle.closed:
                return
            try:
                self._unlock(handle)
            finally:
                handle.close()


def lease_owner_description(metadata: Optional[Dict[str, Any]]) -> str:
    metadata = metadata or {}
    instance = str(metadata.get("instance_id") or "another UT1 process")
    pid = metadata.get("pid")
    return f"{instance} (pid {pid})" if pid else instance
