"""A signal the repaint guard discarded was re-admitted six seconds later.

Live log, 2026-08-10:

    12:55:51  [STABILIZATION] Rejected MIDCPNIFTY 5min LONG: UT1 arrow vanished after 41.9s
    12:55:57  Live Gate: missed-signal re-entry admitting MIDCPNIFTY 5min LONG (A, 82%);
              trend_regime_intel support, age 355s
    12:55:58  MANUAL SIGNAL: LONG MIDCPNIFTY @ 174.5

The stabilization gate threw the signal out because the indicator WITHDREW its arrow.
Six seconds later the missed-signal re-entry path picked the same signal back up, at 355
seconds old, and opened the position that produced both of that day's MIDCPNIFTY rows.

The interesting part is that the guard was already there. `_missed_reentry_blocked_reason`
lists "REPAINT" among its hard blocks — but it matches on the reason TEXT of the original
soft rejection, which was written long before the arrow vanished and says nothing about
repainting. So the block was real and simply never consulted about this event.

The discard branch already withdraws the signal from `_last_signal_identity`,
`_candidate_process_cache` and `_raw_utbot_activity_keys`. The re-entry seed pool was
missing from that list. It is withdrawn at the source now, which is where the fact lives
— rather than by bolting another token onto a text match.

Scope note: this withdraws THAT signal, keyed on its own history key. A genuinely new
arrow on the same instrument is unaffected, and so is the same instrument on another
timeframe.
"""
import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

from scanner import Scanner

TS = datetime(2026, 8, 10, 12, 50, 2)
INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def _scanner(pool=None):
    s = Scanner.__new__(Scanner)
    s._diag_inc = lambda *a, **k: None
    s._candidate_history_key = lambda c: (
        f"{c.instrument}|{c.timeframe}|{c.direction}|{c.signal_timestamp}"
    )
    if pool is not None:
        s._missed_reentry_candidates = pool
    return s


def _cand(index="MIDCPNIFTY", tf="5min", direction="LONG", ts=TS):
    return SimpleNamespace(instrument=index, timeframe=tf, direction=direction,
                           signal_timestamp=ts)


# ── the withdrawal itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("index", INDICES)
def test_a_discarded_arrow_leaves_the_reentry_pool(index):
    c = _cand(index)
    s = _scanner({index: [c]})
    assert s._drop_missed_reentry_seed(c, "arrow vanished") is True
    assert s._missed_reentry_candidates[index] == []


def test_only_that_signal_is_withdrawn():
    """A 15min arrow is a different signal and must survive its 5min sibling's discard."""
    five, fifteen = _cand(tf="5min"), _cand(tf="15min")
    s = _scanner({"MIDCPNIFTY": [five, fifteen]})
    s._drop_missed_reentry_seed(five, "arrow vanished")
    assert s._missed_reentry_candidates["MIDCPNIFTY"] == [fifteen]


def test_a_later_arrow_on_the_same_chart_is_untouched():
    """The guard is about THIS withdrawn claim, not about the instrument."""
    old = _cand(ts=TS)
    fresh = _cand(ts=datetime(2026, 8, 10, 13, 5, 0))
    s = _scanner({"MIDCPNIFTY": [old, fresh]})
    s._drop_missed_reentry_seed(old, "arrow vanished")
    assert s._missed_reentry_candidates["MIDCPNIFTY"] == [fresh]


def test_the_opposite_direction_is_untouched():
    long_c, short_c = _cand(direction="LONG"), _cand(direction="SHORT")
    s = _scanner({"MIDCPNIFTY": [long_c, short_c]})
    s._drop_missed_reentry_seed(long_c, "arrow vanished")
    assert s._missed_reentry_candidates["MIDCPNIFTY"] == [short_c]


# ── it cannot misfire ────────────────────────────────────────────────────────

def test_withdrawing_twice_is_a_no_op():
    c = _cand()
    s = _scanner({"MIDCPNIFTY": [c]})
    assert s._drop_missed_reentry_seed(c) is True
    assert s._drop_missed_reentry_seed(c) is False


def test_no_pool_at_all_is_safe():
    assert _scanner()._drop_missed_reentry_seed(_cand()) is False


def test_an_empty_pool_is_safe():
    assert _scanner({})._drop_missed_reentry_seed(_cand()) is False


def test_a_missing_candidate_is_safe():
    assert _scanner({"MIDCPNIFTY": [_cand()]})._drop_missed_reentry_seed(None) is False


def test_a_raising_history_key_does_not_propagate():
    s = _scanner({"MIDCPNIFTY": [_cand()]})

    def _boom(_c):
        raise RuntimeError("no key")

    s._candidate_history_key = _boom
    assert s._drop_missed_reentry_seed(_cand()) is False


# ── the wiring at the discard site ───────────────────────────────────────────

def test_the_stabilization_discard_withdraws_the_seed():
    src = inspect.getsource(Scanner._coordinate_and_execute)
    marker = "stabilization_repaint_gate"
    assert marker in src, "the repaint discard branch moved"
    window = src[src.index(marker): src.index(marker) + 900]
    assert "_drop_missed_reentry_seed" in window, (
        "a discarded arrow is still left in the re-entry pool, so the missed-signal "
        "path can re-admit it seconds later"
    )


def test_both_discard_reasons_are_distinguished():
    src = inspect.getsource(Scanner._coordinate_and_execute)
    window = src[src.index("stabilization_repaint_gate"):]
    window = window[:window.index("keys_to_remove.append(key)")]
    assert "flipped to the opposite side" in window
    assert "vanished during stabilization" in window


def test_the_existing_hard_block_still_lists_repaint():
    """The premise for why this had to be fixed at the source: the text-matching block
    exists and is correct, it just never sees this event."""
    src = inspect.getsource(Scanner._missed_reentry_blocked_reason)
    assert '"REPAINT"' in src


def test_the_discard_still_cleans_its_other_caches():
    """The seed withdrawal is an ADDITION to that cleanup, not a replacement."""
    src = inspect.getsource(Scanner._coordinate_and_execute)
    window = src[src.index("stabilization_repaint_gate"):]
    window = window[:window.index("keys_to_remove.append(key)")]
    assert "_forget_signal_display_rows" in window
