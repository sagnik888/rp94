"""A flipped arrow is evidence AGAINST the trade, not an absence of evidence.

_live_utbot_signal_still_present returned a plain bool for two very different failures:

  VANISHED — nothing prints at that timestamp any more; the indicator withdrew its claim.
  FLIPPED  — a signal is still there and it now prints the OPPOSITE side.

Both fell into the same branch, so both reached the stabilization momentum override. That
override asks "does context still support this side", and its most-used branch by far
(regime_flip: 653 of 1327 re-admissions) asks only whether a slow regime label agrees. A
regime label LAGS the bar that just flipped, so a BUY got re-admitted at the exact moment
UT1 switched to SELL. At the post-entry site the same call HELD a live position open
against the printed reversal.

Three consequences covered here:
  1. the override refuses a flip outright;
  2. the pending buffer evicts a stale opposite arrow instead of racing it;
  3. the concurrency guard, which never looked at direction at all, refuses to hold both
     sides of one index at once.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner

TS = datetime(2026, 8, 5, 11, 5)


def _sig(signal_type, ts=TS):
    return SimpleNamespace(signal_type=signal_type, timestamp=ts, price=24500.0)


def _candidate(direction="LONG", source="BUY", tf="5min", instrument="NIFTY"):
    return SimpleNamespace(
        instrument=instrument,
        timeframe=tf,
        direction=direction,
        source_signal_type=source,
        signal_timestamp=TS,
        confidence=0.82,
        reasons=[],
        inst_type="OPT",
        action="ENTRY",
    )


RELIEF = {"type": "regime_flip", "reason": "regime flip aligns with BUY (STRONG_UPTREND)"}


def _scanner(signals, instrument="NIFTY", tf="5min"):
    s = Scanner.__new__(Scanner)
    s._cached_results = {f"{instrument}_{tf}": {"signals": list(signals)}}
    s.latest_regimes = {instrument: "STRONG_UPTREND"}
    s._pending_live_signals = {}
    s._diag_inc = lambda *a, **k: None
    s.log_event = lambda *a, **k: None
    s._rtracker_record_candidate = lambda *a, **k: None
    s._forget_signal_display_rows = lambda *a, **k: None
    # The branch chain below the flip check has its own coverage; stub it to a stand-in
    # so these tests speak only to whether a FLIP is allowed to reach it at all.
    s._late_session_reclaim_context = lambda *a, **k: None
    s._one_way_cascade_context = lambda *a, **k: None
    s._time_in_window = lambda *a, **k: False
    s._entry_reversal_relief_context = lambda *a, **k: dict(RELIEF)
    return s


# ── 1. the status is reported, not just the boolean ──────────────────────────

def test_a_matching_arrow_reads_present():
    s = _scanner([_sig("BUY")])
    assert s._repaint_signal_status(_candidate(), TS) == "PRESENT"
    assert s._live_utbot_signal_still_present(_candidate(), TS) is True


def test_a_withdrawn_arrow_reads_vanished():
    s = _scanner([_sig("BUY", TS - timedelta(minutes=15))])
    assert s._repaint_signal_status(_candidate(), TS) == "VANISHED"
    assert s._live_utbot_signal_still_present(_candidate(), TS) is False


def test_an_arrow_that_turned_around_reads_flipped():
    s = _scanner([_sig("SELL")])
    assert s._repaint_signal_status(_candidate(), TS) == "FLIPPED"
    assert s._live_utbot_signal_still_present(_candidate(), TS) is False


def test_no_cached_scan_reads_unknown_not_vanished():
    """An empty cache is 'cannot tell', which is not the same claim as 'withdrawn'."""
    s = _scanner([])
    assert s._repaint_signal_status(_candidate(), TS) == "UNKNOWN"


def test_no_timestamp_reads_unknown():
    s = _scanner([_sig("BUY")])
    assert s._repaint_signal_status(_candidate(), None) == "UNKNOWN"


def test_the_boolean_wrapper_is_unchanged_for_every_case():
    """Other call sites still consume the bool; only PRESENT may be True."""
    for signals, expected in (
        ([_sig("BUY")], True),
        ([_sig("SELL")], False),
        ([], False),
        ([_sig("BUY", TS - timedelta(minutes=15))], False),
    ):
        assert _scanner(signals)._live_utbot_signal_still_present(_candidate(), TS) is expected


# ── 2. the override refuses a flip ───────────────────────────────────────────

def test_the_override_refuses_a_flipped_arrow():
    """The live defect: regime still says UPTREND while the bar now prints SELL."""
    s = _scanner([_sig("SELL")])
    assert s._stabilization_repaint_override_context(_candidate(), TS) is None, (
        "re-admitted a BUY on a bar whose arrow had turned SELL, on the strength of a "
        "lagging regime label"
    )


def test_the_override_still_rescues_a_vanished_arrow():
    """The override must keep doing the job it exists for."""
    s = _scanner([_sig("BUY", TS - timedelta(minutes=15))])
    ctx = s._stabilization_repaint_override_context(_candidate(), TS)
    assert ctx, "a genuinely flickered arrow lost its momentum override"
    assert ctx.get("type") == "regime_flip"


def test_a_flipped_arrow_is_refused_even_with_a_perfectly_aligned_regime():
    s = _scanner([_sig("SELL")])
    s.latest_regimes = {"NIFTY": "STRONG_UPTREND"}
    assert s._stabilization_repaint_override_context(_candidate(), TS) is None


def test_the_post_entry_override_refuses_a_flip_too():
    """Worse than a bad entry: this HOLDS an open position against a printed reversal."""
    s = _scanner([_sig("SELL")])
    c = _candidate()
    c.stabilization_momentum_override = True
    assert s._post_entry_repaint_override_context(c, TS) is None


def test_the_post_entry_override_still_holds_through_a_flicker():
    s = _scanner([_sig("BUY", TS - timedelta(minutes=15))])
    c = _candidate()
    c.stabilization_momentum_override = True
    assert s._post_entry_repaint_override_context(c, TS) is not None


def test_a_flipped_short_is_refused_as_well():
    """A SELL never reaches the relief branch, so give it the cascade context it CAN use."""
    s = _scanner([_sig("BUY")])
    s.latest_regimes = {"NIFTY": "STRONG_DOWNTREND"}
    s._one_way_cascade_context = lambda *a, **k: {"type": "one_way_cascade", "reason": "stub"}
    c = _candidate("SHORT", "SELL")
    assert s._stabilization_repaint_override_context(c, TS) is None
    # ...and the same cascade context still rescues a SHORT whose arrow merely vanished.
    gone = _scanner([_sig("SELL", TS - timedelta(minutes=15))])
    gone._one_way_cascade_context = lambda *a, **k: {"type": "one_way_cascade", "reason": "stub"}
    assert gone._stabilization_repaint_override_context(c, TS) is not None


# ── 3. the buffer evicts a superseded opposite arrow ─────────────────────────

def _buffer(s, candidate, sig_ts):
    key = f"{candidate.instrument}_{candidate.timeframe}_{candidate.direction}"
    s._pending_live_signals[key] = {
        "timestamp": TS, "candidate": candidate, "sig_timestamp": sig_ts, "stable_scans": 0,
    }
    return key


def test_a_newer_opposite_arrow_evicts_the_stale_buffered_one():
    s = _scanner([_sig("SELL")])
    long_key = _buffer(s, _candidate("LONG", "BUY"), TS)
    fresh = _candidate("SHORT", "SELL")
    s._evict_superseded_opposite_buffer(fresh, TS + timedelta(minutes=5))
    assert long_key not in s._pending_live_signals, (
        "a stale LONG stayed buffered next to a fresh SHORT on the same chart — both "
        "could mature into a long CE and a long PE nobody armed"
    )


def test_an_older_incoming_arrow_does_not_evict_a_newer_buffered_one():
    s = _scanner([_sig("SELL")])
    short_key = _buffer(s, _candidate("SHORT", "SELL"), TS + timedelta(minutes=5))
    s._evict_superseded_opposite_buffer(_candidate("LONG", "BUY"), TS)
    assert short_key in s._pending_live_signals, "the newer arrow was thrown away"


def test_the_same_direction_buffer_is_never_touched():
    s = _scanner([_sig("BUY")])
    key = _buffer(s, _candidate("LONG", "BUY", tf="1min"), TS)
    s._evict_superseded_opposite_buffer(_candidate("LONG", "BUY"), TS + timedelta(minutes=1))
    assert key in s._pending_live_signals


def test_another_timeframe_is_not_evicted():
    """1min and 5min disagreeing is ordinary; only the SAME chart supersedes."""
    s = _scanner([_sig("BUY")])
    key = _buffer(s, _candidate("SHORT", "SELL", tf="15min"), TS)
    s._evict_superseded_opposite_buffer(_candidate("LONG", "BUY", tf="5min"), TS)
    assert key in s._pending_live_signals


def test_eviction_is_silent_when_nothing_is_buffered():
    s = _scanner([_sig("BUY")])
    s._evict_superseded_opposite_buffer(_candidate(), TS)
    assert s._pending_live_signals == {}


# ── 4. the concurrency guard finally looks at direction ──────────────────────

def _open(direction="SHORT", tf="15min", instrument="NIFTY", **kw):
    kw.setdefault("is_ghost", False)
    return SimpleNamespace(
        instrument=instrument, timeframe=tf, direction=direction, inst_type="OPT", **kw
    )


def _guard_scanner():
    return Scanner.__new__(Scanner)


def test_an_opposite_direction_stack_is_refused():
    s = _guard_scanner()
    reason = s._opposite_direction_stack_block(_candidate("LONG"), [_open("SHORT")])
    assert reason, (
        "allowed a long CE alongside a long PE on one index — two premiums, two "
        "spreads and theta against both"
    )
    assert "straddle" in reason


def test_the_same_direction_is_allowed():
    s = _guard_scanner()
    assert s._opposite_direction_stack_block(_candidate("LONG"), [_open("LONG")]) == ""


def test_an_empty_book_is_allowed():
    """A reversal AFTER the first trade exits must pass untouched."""
    s = _guard_scanner()
    assert s._opposite_direction_stack_block(_candidate("LONG"), []) == ""


def test_a_deliberately_armed_hedge_leg_is_exempt():
    s = _guard_scanner()
    c = _candidate("SHORT")
    c.pullback_hedge_leg = True
    assert s._opposite_direction_stack_block(c, [_open("LONG")]) == ""


def test_an_existing_hedge_leg_does_not_block_the_next_entry():
    """The hedge is the opposite side BY DESIGN; it must not wall off the book."""
    s = _guard_scanner()
    assert s._opposite_direction_stack_block(
        _candidate("LONG"), [_open("SHORT", pullback_hedge_leg=True)]
    ) == ""


def test_a_ghost_row_does_not_block():
    s = _guard_scanner()
    assert s._opposite_direction_stack_block(
        _candidate("LONG"), [_open("SHORT", is_ghost=True)]
    ) == ""


def test_the_block_can_be_turned_off(monkeypatch):
    """Deliberately running both sides stays possible for whoever wants it."""
    s = _guard_scanner()
    real = scanner_module.get_settings()
    monkeypatch.setattr(
        scanner_module, "get_settings",
        lambda: SimpleNamespace(
            **{**vars(real), "block_opposite_direction_stack": False}
        ),
    )
    assert s._opposite_direction_stack_block(_candidate("LONG"), [_open("SHORT")]) == ""


def test_the_guard_itself_blocks_the_opposite_stack():
    """Drive _prepare_candidate_for_concurrency, not just the helper.

    ut_concurrency_guard is OFF in the live config, so the guard short-circuits to True
    right after the per-index cap. The direction check has to land BEFORE that or it is
    wired to nothing.
    """
    s = Scanner.__new__(Scanner)
    s.trades = SimpleNamespace(open_trades={"t1": _open("SHORT", tf="15min")})
    s._active_session_entry_candidates = lambda *a, **k: []
    s._active_index_trade_cap = lambda c, n: (10, "test")
    s._dual_lane_concurrency_decision = lambda c, e, **k: (None, "")
    s._is_quad_prime_raw_candidate = lambda c: False
    s._record_live_reject = lambda *a, **k: None
    s._source_lane = lambda item: "A"

    c = _candidate("LONG")
    c.one_way_cascade_entry = False
    c.missed_signal_reentry = False
    assert s._prepare_candidate_for_concurrency(c) is False, (
        "the direction check never ran: ut_concurrency_guard=False returns True before "
        "reaching it"
    )


def test_the_guard_still_admits_a_same_direction_stack():
    s = Scanner.__new__(Scanner)
    s.trades = SimpleNamespace(open_trades={"t1": _open("LONG", tf="15min")})
    s._active_session_entry_candidates = lambda *a, **k: []
    s._active_index_trade_cap = lambda c, n: (10, "test")
    s._dual_lane_concurrency_decision = lambda c, e, **k: (None, "")
    s._is_quad_prime_raw_candidate = lambda c: False
    s._record_live_reject = lambda *a, **k: None
    s._source_lane = lambda item: "A"

    c = _candidate("LONG")
    c.one_way_cascade_entry = False
    c.missed_signal_reentry = False
    assert s._prepare_candidate_for_concurrency(c) is True


def test_a_rare_burst_reversal_is_exempt():
    """The strongest-evidence path in the system must not be walled off.

    A rare burst already demands A+ grade, ~95%+ confidence, high ADX, a volume burst
    AND all-index regime agreement — it is the system saying the whole market turned.
    Refusing that entry because two stale legs have not stopped out yet would forfeit
    the best trade of the move to protect the worst.
    """
    s = _guard_scanner()
    c = _candidate("SHORT")
    c.rare_burst_entry = True
    assert s._opposite_direction_stack_block(c, [_open("LONG"), _open("LONG", tf="5min")]) == ""
