"""Regression: the AngelOne log cleaner must not spam WinError 32 hourly.

The SDK logger keeps yesterday's app.log handle open until restart, so
shutil.rmtree on that folder raises PermissionError/OSError (WinError 32).
The cleaner must guard per-folder, warn once, remember the locked folder,
and keep cleaning other folders — not abort the whole sweep every hour.
"""
import inspect

import main


def test_cleaner_loop_has_per_folder_lock_guard():
    src = inspect.getsource(main.start_angelone_log_cleaner)
    # per-folder guard exists
    assert "except (PermissionError, OSError)" in src
    # locked folders are remembered and skipped (no hourly re-noise)
    assert "locked_skip" in src
    assert "locked_skip.add(folder_path)" in src
    # the guard wraps the rmtree inside the folder loop, not around the whole sweep
    assert src.index("for folder_path in glob.glob") < src.index("except (PermissionError, OSError)")


def test_cleaner_warns_instead_of_error_for_locked_folder():
    src = inspect.getsource(main.start_angelone_log_cleaner)
    guard_block = src.split("except (PermissionError, OSError)", 1)[1]
    assert "logger.warning" in guard_block.split("time.sleep", 1)[0]
