"""The launcher console must show the log, and must not show toast noise.

Start_UT1.bat redirects the python process's STDERR to a file so a MemoryError
traceback or a Windows OOM kill outlives the console window. Loguru's console
sink was `sys.stderr`, so that redirect swallowed the entire log: after
"[INFO] Starting Backend Server (main.py)" the console went silent for the rest
of the session while the system scanned normally.

The only thing that ever reached the console was this, over and over:

    (<ToastDismissalReason.TIMED_OUT: 2>,)

win11toast defaults on_click/on_dismissed/on_failed to the builtin print(), so
every desktop toast that merely timed out wrote its dismissal reason to STDOUT.

Both ends are tested here: the log has to arrive on STDOUT, and the toast
callbacks must not be print.
"""
import queue
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_a_log_line_reaches_stdout_not_the_stderr_redirect():
    """Import main the way startup does, then log; the console must see it."""
    probe = (
        "import main\n"
        "from loguru import logger\n"
        "logger.info('CONSOLE-SINK-PROBE')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=300,
        errors="replace",
    )
    assert result.returncode == 0, result.stderr[-2000:]
    # STDOUT is the console the operator watches. STDERR is the file.
    assert "CONSOLE-SINK-PROBE" in result.stdout, (
        "log line never reached stdout; the launcher console stays blank.\n"
        f"stderr tail: {result.stderr[-2000:]}"
    )
    assert "CONSOLE-SINK-PROBE" not in result.stderr


def test_toast_callbacks_are_not_the_builtin_print():
    """A timed-out toast must log, not print into the launcher console."""
    import notifications

    captured = {}

    def _fake_toast(*args, **kwargs):
        captured.update(kwargs)

    fake = types.ModuleType("win11toast")
    fake.toast = _fake_toast

    # The worker is a `while True` with no exit, so the thread it runs on cannot be
    # joined. Give it a PRIVATE queue: it then parks on that instead of the
    # module-level one, where a leaked consumer would race every later test in the
    # same pytest process for notifications it was never meant to see.
    original_module = sys.modules.get("win11toast")
    original_queue = notifications._notification_queue
    private_queue: "queue.Queue" = queue.Queue()
    sys.modules["win11toast"] = fake
    notifications._notification_queue = private_queue
    try:
        worker = threading.Thread(
            target=notifications._notification_worker, daemon=True
        )
        worker.start()
        private_queue.put(("hello", "info"))
        deadline = time.time() + 10
        while not captured and time.time() < deadline:
            time.sleep(0.05)
    finally:
        notifications._notification_queue = original_queue
        if original_module is None:
            sys.modules.pop("win11toast", None)
        else:
            sys.modules["win11toast"] = original_module

    assert notifications._notification_queue is original_queue

    assert captured, "the notification worker never dispatched the toast"
    for hook in ("on_click", "on_dismissed", "on_failed"):
        assert hook in captured, f"{hook} left at win11toast's print() default"
        assert captured[hook] is not print, f"{hook} still prints to the console"
