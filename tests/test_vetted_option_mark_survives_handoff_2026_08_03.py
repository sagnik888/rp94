"""A rejected option quote must not come back through the side door.

Scanner._active_option_decision_mark exists to decide whether a contract LTP is usable:
when the quote is older than option_internal_mark_quote_age_seconds (3.0s) and differs
materially from the delta-implied mark, it REJECTS the LTP and substitutes a synthetic
one. The scanner honours that for its own stop check.

But the value handed to TradeManager.update_trade was the RAW premium, and TradeManager
overwrites trade.current_price with it. Its only guard there is an upper plausibility
bound (~spot*0.12, meant to catch an index price leaking into a premium field), NOT a
staleness test — so a quote the scanner had explicitly refused could still drive
EARLY_LOSS_CUT and TRAILING_STOP, closing a live position at a price the system had
already judged untrustworthy.

Both directions cost money: a stale low stops out a winner, a stale high suppresses a
stop that should have fired.

tests/test_option_mark_fallback.py asserts the REJECTION. Nothing asserted what
TradeManager receives, which is exactly why this survived.
"""
import inspect

from scanner import Scanner
from engine.exit_cycle import ExitCycleService


def _update_active_trades_source():
    return inspect.getsource(ExitCycleService.update_active_trades)


def test_the_vetted_mark_is_what_reaches_trade_manager():
    """The raw LTP must not be forwarded once the vetting has replaced it."""
    src = _update_active_trades_source()
    assert "real_premium = float(mark)" in src, (
        "update_trade is still handed the raw premium; the vetted mark is computed, "
        "used for the scanner's own stop check, and then discarded at the handoff"
    )


def test_the_reassignment_happens_before_the_handoff():
    """Order matters: assigning after the call would change nothing."""
    src = _update_active_trades_source()
    assign = src.index("real_premium = float(mark)")
    handoff = src.index("real_premium=real_premium")
    assert assign < handoff, "the vetted mark is assigned after it is already passed"


def test_the_no_quote_case_is_left_alone():
    """Guarded on `is not None`, so a missing LTP keeps TradeManager's own fallback.

    When there is no usable quote at all, TradeManager computes a delta-implied premium
    of its own. Redirecting that case too would swap in the scanner's synthetic, which
    also applies theta decay — a real behaviour change beyond the defect, deliberately
    not made here.
    """
    src = _update_active_trades_source()
    marker = "if real_premium is not None:"
    assert marker in src
    assert src.index(marker) < src.index("real_premium = float(mark)")


def test_the_scanner_still_uses_the_same_mark_for_its_own_stop_check():
    """The two must agree; disagreement between them is the whole bug class."""
    src = _update_active_trades_source()
    assert "trade.current_price = float(mark)" in src
    assert "auto_stop_trigger" in src
