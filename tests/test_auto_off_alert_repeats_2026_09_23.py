"""REAL with Auto Mode OFF must keep saying so while it is costing entries.

2026-09-22 fell one-sided from the open (NIFTY -122.8, -0.52%, low late). The gates did
their job: nine signals were ACCEPTED, four of them SHORTs during the fall at 11:00, 11:35,
12:44 and 13:01. Every one was recorded "Accepted and displayed for manual execution" with
executed=0, and the session ended with zero broker orders.

The watchdog already knew. It raised "broker mode is REAL during market hours but Auto Mode
is OFF - signals will NOT execute" once, at 09:15:45, and `_alert` then latched it for the
rest of the process. Nothing spoke again while the accepted signals piled up.
"""
import main


def test_a_newly_accepted_signal_reraises_immediately():
    assert main._auto_off_realert_due(
        accepted_unexecuted=1, last_count=0,
        last_realert_monotonic=5.0, now_monotonic=6.0, every_seconds=900.0,
    )


def test_a_quiet_stretch_still_heartbeats():
    assert main._auto_off_realert_due(
        accepted_unexecuted=0, last_count=0,
        last_realert_monotonic=100.0, now_monotonic=100.0 + 901.0, every_seconds=900.0,
    )


def test_it_stays_quiet_between_signals_inside_the_window():
    assert not main._auto_off_realert_due(
        accepted_unexecuted=2, last_count=2,
        last_realert_monotonic=100.0, now_monotonic=160.0, every_seconds=900.0,
    )


def test_the_first_pass_does_not_double_fire():
    """_alert itself raises the first one; the re-raise must not duplicate it."""
    assert not main._auto_off_realert_due(
        accepted_unexecuted=0, last_count=0,
        last_realert_monotonic=0.0, now_monotonic=10_000.0, every_seconds=900.0,
    )


def test_the_heartbeat_floor_cannot_be_configured_below_a_minute():
    assert not main._auto_off_realert_due(
        accepted_unexecuted=0, last_count=0,
        last_realert_monotonic=10.0, now_monotonic=40.0, every_seconds=5.0,
    )
    assert main._auto_off_realert_due(
        accepted_unexecuted=0, last_count=0,
        last_realert_monotonic=10.0, now_monotonic=71.0, every_seconds=5.0,
    )


def test_the_heartbeat_is_off_by_default():
    """19 notifications in 4.5 hours of a deliberately manual session, none about a signal."""
    import inspect

    src = inspect.getsource(main)
    assert 'os.getenv("RUNTIME_WATCHDOG_AUTO_OFF_REALERT_SECONDS", "0")' in src
    assert not main._auto_off_realert_due(
        accepted_unexecuted=0, last_count=0,
        last_realert_monotonic=100.0, now_monotonic=100.0 + 86_400.0, every_seconds=0.0,
    )


def test_a_new_unplaced_signal_still_alerts_with_the_heartbeat_off():
    assert main._auto_off_realert_due(
        accepted_unexecuted=1, last_count=0,
        last_realert_monotonic=100.0, now_monotonic=101.0, every_seconds=0.0,
    )


# ── the count that goes into the message ─────────────────────────────────────


class _Candidate:
    def __init__(self, accepted, order_id=None):
        self.accepted_by_gate = accepted
        self.broker_order_id = order_id


def test_only_accepted_and_unplaced_signals_are_counted():
    scanner = type("_S", (), {})()
    scanner._session_trade_candidates = {
        "NIFTY": {"a": _Candidate(True), "b": _Candidate(True, "260922190001"), "c": _Candidate(False)},
        "BANKNIFTY": {"d": _Candidate(True), "e": _Candidate(True)},
    }

    assert main._accepted_unexecuted_count(scanner) == 3


def test_the_count_never_breaks_the_watchdog():
    class _Exploding:
        @property
        def _session_trade_candidates(self):
            raise RuntimeError("ledger mid-write")

    assert main._accepted_unexecuted_count(_Exploding()) == 0
    assert main._accepted_unexecuted_count(object()) == 0
