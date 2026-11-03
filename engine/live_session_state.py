"""What the operator had set for today's live session, kept across a restart.

2026-09-25 user: "in live market after a restart the system must remember the settings
from before for the current live session - if it was running in Auto it starts with Auto
on, if Auto was off it stays off". Every other dashboard setting is already saved to .env;
Auto Mode deliberately is not (a fresh process must never inherit order authority before
its feeds and warmup are proved), and the 09:00 REAL switch is runtime-only. This file
remembers both for the current trading day only. Auto is never armed from here directly:
the scanner restores it through the same readiness-gated arm as any other Auto request.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

PATH = Path("data_store") / "live_session_state.json"
_LOCK = threading.Lock()


def load(session_day: str) -> Dict[str, Any]:
    """Today's saved state, or {} when there is none for this trading day."""
    try:
        data = json.loads(Path(PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or str(data.get("session_day") or "") != str(session_day):
        return {}
    return data


def update(session_day: str, **fields: Any) -> Dict[str, Any]:
    """Merge fields into today's state (a new day starts empty) and write it atomically."""
    with _LOCK:
        data = load(session_day)
        data.update(fields)
        data["session_day"] = str(session_day)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path = Path(PATH)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
        return data
