"""The session archiver must not report its own deliberate refusal as a failure.

From the live log, 2026-08-31 10:32:

    WARNING | Kite historical_data failed: InputException: invalid token      (x5)
    WARNING | Session archive failed for 2026-08-27: Command '[...
              archive_session_data.py, 2026-08-27, --quiet]' returned non-zero exit
              status 2.

Neither line is a fault. scripts/archive_session_data.py deliberately refuses heavy
archive IO during market hours -- "[skip] market hours - refusing heavy archive IO (use
--force to override)" -- and main's _archive_loop retried it every five minutes, logging
the refusal as a failure each time. The session reports show 73 occurrences on
2026-08-24 and 80 on 2026-08-25.

The "invalid token" burst is the same event one step earlier: _archive_loop back-fills
contract history IN-PROCESS before spawning the child, and re-fetching an EXPIRED option
contract is what Kite rejects. That phrase means the INSTRUMENT token, not the broker
session -- reads and orders were both healthy throughout.

The fix is to defer the whole attempt while the market is open rather than to soften the
child's exit code. That matters: exit 2 for a skipped day is a deliberate fail-closed
guard pinned by tests/test_postmarket_archive_fail_closed_2026_08_22.py, and inverting it
would let a genuinely skipped day be recorded as archived. The day is deliberately NOT
added to `attempted`, so it still archives after the close exactly as before.
"""
import inspect

import main
import scripts.archive_session_data as archive


def _archive_loop_source():
    source = inspect.getsource(main)
    assert "def _archive_loop" in source, "the archive loop was renamed"
    body = source.split("def _archive_loop")[1]
    return body.split("threading.Thread(target=_archive_loop")[0]


def test_the_loop_checks_market_hours_before_doing_any_work():
    """The guard must sit ahead of BOTH the back-fill and the child process."""
    body = _archive_loop_source()

    assert "market_hours_now" in body, (
        "the archive loop no longer consults market_hours_now; a market-hours attempt "
        "will back-fill expired contracts and then log the child's refusal as a failure"
    )

    guard = body.index("market_hours_now")
    backfill = body.index("backfill_contract_history")
    child = body.index("subprocess.run")

    assert guard < backfill, (
        "the market-hours check runs AFTER the contract back-fill, so the expired-"
        "contract refetch (the 'invalid token' burst) still happens every five minutes"
    )
    assert guard < child, "the market-hours check runs after the child is spawned"


def test_a_deferred_day_is_not_marked_attempted():
    """Deferring must not consume the day; it has to archive after the close."""
    body = _archive_loop_source()

    guard = body.index("market_hours_now")
    continue_after_guard = body.index("continue", guard)
    marked = body.index("attempted.add(session_day)")

    assert continue_after_guard < marked, (
        "the market-hours branch falls through to attempted.add(session_day), so a day "
        "deferred during market hours would never be archived after the close"
    )


def test_the_deferral_is_not_logged_as_a_failure():
    """It is expected behaviour, so it must not read as an error to the operator."""
    body = _archive_loop_source()
    guard = body.index("market_hours_now")
    window = body[guard : body.index("backfill_contract_history")]

    assert "logger.warning" not in window and "logger.error" not in window, (
        "the market-hours deferral is logged at warning/error level; this fired 80 times "
        "in a single session and trained the operator to ignore the archiver"
    )
    assert "deferred" in window.lower()


def test_the_guard_cannot_itself_block_archiving():
    """If the import or the check raises, archiving must proceed, not stop."""
    body = _archive_loop_source()
    guard = body.index("market_hours_now")
    window = body[max(0, guard - 400) : guard + 400]

    assert "except Exception" in window, (
        "the market-hours guard has no failure path; an import error would stop the "
        "archiver entirely rather than fall back to attempting the archive"
    )


def test_the_child_still_fails_closed_on_a_skipped_day():
    """The guard this fix must NOT weaken, restated here so the pairing is visible."""
    import pytest

    original = archive.archive_day
    original_argv = archive.sys.argv
    try:
        archive.sys.argv = ["archive_session_data.py", "1999-01-01", "--quiet"]
        archive.archive_day = lambda *a, **k: {"date": "1999-01-01", "skipped": "no_events"}
        with pytest.raises(SystemExit) as exc:
            archive.main()
        assert exc.value.code == 2, (
            "a skipped day must still exit non-zero; softening this would let an "
            "unarchived session be recorded as complete"
        )
    finally:
        archive.archive_day = original
        archive.sys.argv = original_argv


def test_market_hours_now_is_still_the_shared_predicate():
    """Both sides must agree on what 'market hours' means."""
    assert callable(getattr(archive, "market_hours_now", None))
    assert isinstance(archive.market_hours_now(), bool)
