"""UT_5MIN_LOSS_COOLDOWN_MINUTES='45' was configured and enforced nowhere.

Repo-wide, the only readers of ut_5min_loss_cooldown_minutes were two broadcast/config
payloads — scanner.get_broadcast_config and scanner_frictionless.py, the latter imported
by nothing. No cooldown computation consumed it. The real post-loss block was the flat
index_cooldown_minutes (3.0 live), which is additionally regime-scoped, so past minute 3
on trend tape a same-index re-entry was allowed with no trend gate at all — while the
dashboard showed a 45-minute post-loss pause.

SHIPS DISABLED. Enforcing 45 minutes is a trading-policy change, not a defect fix: at
four indices and a ~360-minute entry window it caps each index near 8 entries, and the
per-index consecutive-loss halt (3 in a row) already exists. So the gate is built,
tested and inert until an operator opts in, and the setting stops implying a protection
that is not there.

It stacks on the flat cooldown and is deliberately NOT bypassed by the stop-out re-entry
allowance or the post-exit reversion window — those exist to re-enter a trend that
stopped out early, which is exactly what a post-loss cooldown is meant to interrupt.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

from scanner import Scanner

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _pinned_settings():
    """get_settings() returns a shared singleton that other modules mutate; pin and
    restore the two fields under test so these pass alone and in a full run."""
    from config.settings import get_settings

    s = get_settings()
    saved = (
        getattr(s, "ut_5min_loss_cooldown_enabled", False),
        getattr(s, "ut_5min_loss_cooldown_minutes", 45),
    )
    yield s
    s.ut_5min_loss_cooldown_enabled, s.ut_5min_loss_cooldown_minutes = saved


def _sc(*, streak=1, minutes_ago=10.0, instrument="NIFTY"):
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._losses_today = {instrument: streak}
    sc._last_exit_time = {
        instrument: datetime.now(IST).replace(tzinfo=None)
        - timedelta(minutes=minutes_ago)
    }
    return sc


def _run(sc, instrument="NIFTY"):
    return Scanner._post_loss_cooldown_block(sc, instrument, None)


# ── off by default ───────────────────────────────────────────────────────────

def test_it_is_inert_in_the_shipped_config(_pinned_settings):
    from config.settings import get_settings

    assert bool(getattr(get_settings(), "ut_5min_loss_cooldown_enabled", False)) is False
    assert _run(_sc(minutes_ago=1.0))[0] is False


# ── enabled: the gate ────────────────────────────────────────────────────────

def _on(s, minutes=45):
    s.ut_5min_loss_cooldown_enabled = True
    s.ut_5min_loss_cooldown_minutes = minutes


def test_a_recent_loss_holds_the_index_out(_pinned_settings):
    _on(_pinned_settings)
    blocked, reason = _run(_sc(minutes_ago=10.0))
    assert blocked is True
    assert "35m remaining" in reason


def test_the_index_is_released_once_the_window_passes(_pinned_settings):
    _on(_pinned_settings)
    assert _run(_sc(minutes_ago=46.0))[0] is False


def test_exactly_at_the_boundary_it_is_released(_pinned_settings):
    _on(_pinned_settings)
    assert _run(_sc(minutes_ago=45.0))[0] is False


def test_a_winning_last_outcome_does_not_hold_the_index(_pinned_settings):
    """The per-index streak resets to 0 on any non-loss, so this is what 'the most
    recent outcome was a loss' means."""
    _on(_pinned_settings)
    assert _run(_sc(streak=0, minutes_ago=1.0))[0] is False


def test_it_is_per_index(_pinned_settings):
    _on(_pinned_settings)
    sc = _sc(instrument="NIFTY", minutes_ago=1.0)
    assert _run(sc, "NIFTY")[0] is True
    assert _run(sc, "BANKNIFTY")[0] is False, "one index's loss froze another"


def test_a_longer_streak_still_reports_the_right_remainder(_pinned_settings):
    _on(_pinned_settings, minutes=20)
    blocked, reason = _run(_sc(streak=3, minutes_ago=5.0))
    assert blocked is True
    assert "15m remaining" in reason and "3 loss(es)" in reason


# ── it must not blow up on missing or odd state ──────────────────────────────

def test_no_recorded_exit_does_not_block(_pinned_settings):
    _on(_pinned_settings)
    sc = _sc()
    sc._last_exit_time = {}
    assert _run(sc)[0] is False


def test_a_zero_minute_setting_disables_it(_pinned_settings):
    _on(_pinned_settings, minutes=0)
    assert _run(_sc(minutes_ago=0.5))[0] is False


def test_a_junk_setting_does_not_block(_pinned_settings):
    _on(_pinned_settings)
    _pinned_settings.ut_5min_loss_cooldown_minutes = "not a number"
    assert _run(_sc(minutes_ago=0.5))[0] is False


def test_an_empty_instrument_does_not_block(_pinned_settings):
    _on(_pinned_settings)
    assert Scanner._post_loss_cooldown_block(_sc(), "", None)[0] is False


def test_a_tz_aware_exit_time_is_handled(_pinned_settings):
    _on(_pinned_settings)
    sc = _sc()
    sc._last_exit_time = {"NIFTY": datetime.now(IST) - timedelta(minutes=5)}
    assert _run(sc)[0] is True


def test_a_scanner_with_no_streak_state_does_not_block(_pinned_settings):
    _on(_pinned_settings)
    sc = _sc()
    del sc._losses_today
    assert _run(sc)[0] is False


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_eligibility_gate_consults_it():
    import inspect

    src = inspect.getsource(Scanner._can_trade_instrument)
    assert "_post_loss_cooldown_block" in src, (
        "the gate exists but nothing asks it"
    )


def test_it_is_not_bypassed_by_the_stop_out_reentry_allowance():
    """The bypass above it re-enters a trend that stopped out early — precisely what a
    post-loss cooldown interrupts. It must sit outside that branch."""
    import inspect

    src = inspect.getsource(Scanner._can_trade_instrument)
    assert src.index("_stop_out_reentry_allowed") < src.index("_post_loss_cooldown_block")


def test_the_settings_resolve():
    from config.settings import get_settings

    s = get_settings()
    assert hasattr(s, "ut_5min_loss_cooldown_enabled")
    assert int(getattr(s, "ut_5min_loss_cooldown_minutes")) > 0


# ── a latent IndexError found next door while wiring this ────────────────────

@pytest.mark.parametrize("blank", ["", "   ", None])
def test_recording_a_blank_instrument_does_not_raise(blank):
    """_record_consecutive_loss_outcome guards `if not instrument` — but the line above
    did str(x or "").split()[0], and "".split() is [], so it raised before reaching the
    guard. This runs on the exit-recording path."""
    sc = Scanner.__new__(Scanner)
    sc._losses_today = {}
    assert Scanner._record_consecutive_loss_outcome(sc, blank, -100.0) == 0


def test_recording_a_real_instrument_still_counts():
    sc = Scanner.__new__(Scanner)
    sc._losses_today = {}
    assert Scanner._record_consecutive_loss_outcome(sc, "NIFTY", -100.0) == 1
    assert Scanner._record_consecutive_loss_outcome(sc, "NIFTY", -50.0) == 2
    assert Scanner._record_consecutive_loss_outcome(sc, "NIFTY", 200.0) == 0
