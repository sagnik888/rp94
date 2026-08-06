"""A signal whose source bar has closed does not belong in an anti-repaint buffer.

The buffer exists to let a FORMING bar's arrow settle. A final bar's arrow cannot repaint,
so the wait buys nothing — and it cost 30 seconds before market intelligence and the
filters even saw the candidate. On NIFTY 24500 with an ATM premium of 217.50 and lot 75,
30 seconds of post-close drift is real money per lot, plus outright missed fills.

Worse, the buffered closed-candle signal was still run through the repaint gate, which
re-reads the CURRENT latest signal from the indicator cache. Once that cache rolls to the
next bar the old identity is gone, so a fully-confirmed closed-bar signal was discarded as
"UT1 arrow vanished" — a rejection that cannot logically apply to a bar already final.

The system already computed the answer: `entered_intracandle` is set for every candidate a
few lines above the buffer, and nothing in the buffer read it.
"""
import ast
import inspect
import textwrap

import pytest

from scanner import Scanner


def _buffer_source():
    src = inspect.getsource(Scanner._coordinate_and_execute)
    marker = "Live Anti-Repaint Stabilization Buffer"
    assert marker in src, "the stabilization buffer block moved or was renamed"
    return src[src.index(marker):]


def test_the_buffer_consults_entered_intracandle():
    """It was computed for every candidate and read by nothing in this block."""
    block = _buffer_source()
    assert "entered_intracandle" in block, (
        "the stabilization buffer still ignores whether the source candle has closed, so "
        "a confirmed closed-bar signal waits the full stabilization window"
    )


def test_a_closed_candle_candidate_is_matured_not_buffered():
    """Parse the branch: it must append to matured_candidates, not _pending_live_signals."""
    block = _buffer_source()
    start = block.index("entered_intracandle")
    window = block[start:start + 900]
    assert "matured_candidates.append" in window, (
        "a closed-candle signal is not routed straight to the matured list"
    )
    assert "continue" in window, "the closed-candle branch falls through into buffering"


def test_the_branch_is_the_negative_case():
    """`not entered_intracandle` — a FORMING bar must still be buffered.

    Parsed, not grepped. A first draft matched the first textual occurrence of the flag
    name, which was the explanatory COMMENT above the branch, so it proved nothing about
    the code and failed against a correct implementation.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner._coordinate_and_execute)))
    guards = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(n, ast.Constant) and n.value == "entered_intracandle"
            for n in ast.walk(node.test)
        )
    ]
    assert guards, "no branch actually tests entered_intracandle"
    negated = [
        g for g in guards
        if isinstance(g.test, ast.UnaryOp) and isinstance(g.test.op, ast.Not)
    ]
    assert negated, (
        "the closed-candle branch is not negated — as written it would skip the buffer "
        "for FORMING bars and buffer the closed ones, exactly backwards"
    )


def test_a_forming_candle_is_still_buffered():
    """The anti-repaint protection must survive intact for its actual purpose."""
    block = _buffer_source()
    assert "_pending_live_signals[key] = {" in block, (
        "forming-candle signals are no longer buffered at all — the repaint protection "
        "this block exists for has been removed"
    )
    assert "[STABILIZATION] Buffering" in block


def test_the_skip_is_counted_so_it_can_be_seen_in_diagnostics():
    block = _buffer_source()
    assert "closed_candle_direct" in block


def test_the_decision_is_logged_once_per_signal_not_per_scan():
    """log_signal_decision_once keys on instrument|tf|timestamp."""
    block = _buffer_source()
    idx = block.index("closed_candle_direct")
    window = block[idx - 400:idx + 700]
    assert "log_signal_decision_once" in window


# ── the flag this leans on still means what it should ────────────────────────

def test_source_is_intrabar_is_false_once_the_bar_has_closed():
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    s = Scanner.__new__(Scanner)
    sig_ts = datetime(2026, 8, 7, 10, 30)
    c = SimpleNamespace(timeframe="5min", signal_timestamp=sig_ts, action="ENTRY")

    # 2 seconds after the bar closed at 10:35 -> final
    assert s._candidate_source_is_intrabar(c, sig_ts + timedelta(minutes=5, seconds=2)) is False
    # 2 minutes in -> still forming
    assert s._candidate_source_is_intrabar(c, sig_ts + timedelta(minutes=2)) is True


def test_a_still_forming_bar_keeps_its_protection():
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    s = Scanner.__new__(Scanner)
    sig_ts = datetime(2026, 8, 7, 10, 30)
    c = SimpleNamespace(timeframe="15min", signal_timestamp=sig_ts, action="ENTRY")
    assert s._candidate_source_is_intrabar(c, sig_ts + timedelta(minutes=9)) is True
