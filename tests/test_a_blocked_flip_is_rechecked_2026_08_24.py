"""A flip blocked by an open opposite trade must stay recheck-eligible.

This engine is an ATR flip scanner: a BUY arrow is also the SELL's exit. When a live-book
trade is open in the opposite direction, `process_best_signal` refuses the reverse entry.
There are two such refusals and they behave differently:

  engine/signal_processor.py:5046   "REJECTED - Opposite Signal Wait"
      built with _build_rejected_from_raw, which arms
      scanner._live_signal_recheck_eligible (signal_processor.py:4896-4908) when the
      status is recheckable. The NEXT cycle re-evaluates the signal, and once the old
      position is gone the reversal fires. Its entry in _recheckable_rejection_status
      spells out why: "otherwise the dedup lock orphans it forever".

  engine/signal_processor.py:5231   "REJECTED - Existing Opposite Trade"   <-- the bug
      the quad_prime branch, and quad_prime is the SHIPPED engine (.env
      SCANNER_ENGINE='quad_prime'). It calls the bare _emit_signal_decision, which only
      logs and increments a diagnostic counter, then `return []`. No recheck is armed.
      The dedup lock was already advanced when the raw signal was collected
      (signal_processor.py:4830), so the signal is presented exactly once and dropped.

Measured in data_store/reports/missed_rejected_signals_*.csv: 56 flips dropped this way
on four sessions — 2026-07-09 (24), 2026-07-31 (16), 2026-08-19 (13), 2026-07-10 (3).
On 2026-07-09 a single NIFTY trade (T0003_NIFTY_07090302) refused SELL flips at 10:35,
10:47, 10:49, 11:40, 11:47, 11:51 and onward, and no reversal was ever taken.

The fix is to make the quad_prime refusal behave like its sibling: same recheckable
status handling, same _build_rejected_from_raw. It does NOT force an entry — the guard
still refuses while the trade is open. It only stops the signal being thrown away, so the
cycle after the position closes can act on it.

Scope: the SAME-direction guard ("REJECTED - Existing Active Trade",
signal_processor.py:5305) is deliberately left non-recheckable. Re-entering a direction
you are already in is not a flip.
"""
from engine.signal_processor import SignalProcessor


def test_the_flip_refusal_is_recheckable():
    assert SignalProcessor._recheckable_rejection_status(
        "REJECTED - Existing Opposite Trade"
    ) is True


def test_its_sibling_path_is_still_recheckable():
    """The one that already worked must not regress."""
    assert SignalProcessor._recheckable_rejection_status(
        "REJECTED - Opposite Signal Wait"
    ) is True


def test_the_same_direction_guard_stays_non_recheckable():
    """Not a flip: re-entering a direction already held is a duplicate, not a reversal."""
    assert SignalProcessor._recheckable_rejection_status(
        "REJECTED - Existing Active Trade"
    ) is False


def test_an_unrelated_status_is_unaffected():
    assert SignalProcessor._recheckable_rejection_status("REJECTED - Something Else") is False


def test_the_quad_prime_guard_arms_the_recheck():
    """A recheckable status is inert unless the emitter actually arms the recheck.

    _emit_signal_decision only logs; the arming lives in _build_rejected_from_raw. This
    pins that the quad_prime opposite-trade branch uses the arming one, which is the
    whole difference between the two paths.
    """
    import inspect

    src = inspect.getsource(SignalProcessor.process_best_signal)
    marker = "REJECTED - Existing Opposite Trade"
    assert marker in src, "the quad_prime opposite guard moved; re-point this test"

    at = src.index(marker)
    before = src[:at]
    # Whichever emitter opens the call this status belongs to is the last one named
    # before it. Distance-independent, so reformatting the argument dict cannot flip it.
    arming = before.rfind("_build_rejected_from_raw(")
    bare = before.rfind("_emit_signal_decision(")

    assert arming > bare, (
        "the opposite-trade guard still emits via the bare _emit_signal_decision, which "
        "arms no recheck, so the flip signal is dropped instead of re-evaluated next cycle"
    )
