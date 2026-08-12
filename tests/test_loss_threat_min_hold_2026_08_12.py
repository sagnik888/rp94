"""The 60-second minimum hold before a loss-threat exit was bypassed on nearly every trade.

Measured across 19 sessions: 54 of 98 LOSS_THREAT_EXITs (55%) fired INSIDE the 60s
window the setting is supposed to enforce — 22 within 10 seconds, 16 within 5 — together
-Rs.11,869.

Two defects, one behind the other.

1. `latest_time` is the CANDLE clock. An intracandle entry is stamped AFTER the bar it
   fired inside, so a fill at 15:09:36 against the 15:05:00 bar computes -276 seconds.
   `_trade_like_age_seconds` treated any negative age as unknowable and returned None.
   INTRACANDLE is the default fire mode, so this was the normal case, not an edge one.

2. The single caller read None as "no restriction" and let the exit through.

The helper's refusal is CORRECT and is left alone — an inconsistent clock must not
manufacture an age (test_the_age_helper_is_direction_safe, 2026-08-10), and a row with no
entry stamp must not be blocked outright or the exit disarms entirely
(test_an_unknowable_age_does_not_block_the_guard). Both of those still pass unmodified.
The fix belongs at the call site: what the hold needs is how long the POSITION has been
open, which the wall clock knows. Live only — in a replay "now" is meaningless, so
HISTORICAL keeps the candle-derived answer.

Why it matters: 34 of 96 winners (35%) went UNDERWATER before they turned, and they
earned Rs.48,041 — 46% of all winning P&L. Their dips ran to Rs.196 (median), Rs.388
(p75), Rs.604 (max), while LOSS_THREAT_EXIT consumes a median 15% of the stop (~Rs.146)
when it fires. Without the hold, the exit's trigger sits below the routine dip of a trade
that goes on to win.

The hard stop is untouched, so maximum loss per trade is unchanged. This only delays the
DISCRETIONARY cut until the position has printed something of its own.
"""
import datetime as dt
import inspect
from types import SimpleNamespace

import pytest
import pytz

from scanner import Scanner

IST = pytz.timezone("Asia/Kolkata")
AGE = Scanner._trade_like_age_seconds
ENTRY = dt.datetime(2026, 8, 11, 15, 9, 36)


def _t(**kw):
    return SimpleNamespace(**kw)


# ── the helper's contract is UNCHANGED ───────────────────────────────────────

def test_the_helper_still_refuses_a_backwards_clock():
    """Direction-safety from 2026-08-10: an inconsistent clock must not invent an age."""
    assert AGE(_t(entry_timestamp=ENTRY), ENTRY - dt.timedelta(seconds=90)) is None


def test_the_helper_still_refuses_a_missing_clock_or_stamp():
    assert AGE(_t(entry_timestamp=ENTRY), None) is None
    assert AGE(_t(), dt.datetime(2026, 8, 11, 15, 12, 0)) is None


def test_the_helper_still_measures_a_forward_clock():
    assert AGE(_t(entry_timestamp=ENTRY), ENTRY + dt.timedelta(seconds=144)) == pytest.approx(144.0)


@pytest.mark.parametrize("field", ["entry_time", "entry_timestamp", "signal_time"])
def test_every_entry_stamp_field_is_read(field):
    assert AGE(_t(**{field: ENTRY}), ENTRY + dt.timedelta(seconds=144)) == pytest.approx(144.0)


# ── the call site resolves the intracandle case ──────────────────────────────

def test_the_guard_falls_back_to_the_wall_clock_when_live():
    """The intracandle artifact: candle clock behind the entry, helper returns None, and
    the hold silently stopped applying."""
    src = inspect.getsource(Scanner)
    idx = src.index("if held is not None and held < min_hold:") if "if held is None or held < min_hold:" in src         else src.index("if held is None and str(getattr(self,")
    window = src[idx:idx + 400]
    assert "datetime.now(IST)" in window, "the call site has no live age fallback"


def test_the_fallback_is_live_only():
    """In a replay latest_time is legitimately in the past and 'now' would report weeks."""
    src = inspect.getsource(Scanner)
    idx = src.index("if held is None and str(getattr(self,")
    assert 'HISTORICAL' in src[idx:idx + 200]


# ── the caller no longer reads absence as permission ─────────────────────────

def test_a_still_unknown_age_does_not_block():
    """Preserving the 2026-08-10 rule: a row with no entry stamp must not disarm the
    exit. The fallback narrows WHEN age is unknown; it does not change what unknown means."""
    src = inspect.getsource(Scanner)
    assert "if held is not None and held < min_hold:" in src


def test_the_guard_clears_the_confirmation_when_it_blocks():
    """Otherwise a part-built confirmation survives the block and the next eligible
    scan fires immediately on stale evidence."""
    src = inspect.getsource(Scanner)
    idx = src.index("if held is not None and held < min_hold:")
    assert "_clear_loss_threat_confirmation" in src[idx:idx + 300]


def test_the_refusal_message_reports_the_age():
    src = inspect.getsource(Scanner)
    idx = src.index("if held is not None and held < min_hold:")
    window = src[idx:idx + 500]
    assert "held {held:.0f}s" in window


# ── scope: nothing else is loosened ──────────────────────────────────────────

def test_only_the_discretionary_loss_exit_is_delayed():
    """The hard stop, EARLY_LOSS_CUT and every profit-side exit are untouched, so the
    maximum loss per trade is exactly what it was."""
    src = inspect.getsource(Scanner)
    idx = src.index("if held is not None and held < min_hold:")
    # Searched BACKWARDS from the guard, not over a fixed slice — a comment added above
    # it pushed the anchor out of a 600-char window on the first run.
    assert 0 < src.rfind("peak_threat_exit_min_hold_seconds", 0, idx) < idx


def test_the_minimum_hold_is_still_configurable():
    from config.settings import Settings

    assert Settings.model_fields["peak_threat_exit_min_hold_seconds"].default == 60.0


def test_a_zero_minimum_restores_the_old_behaviour():
    """min_hold <= 0 skips the block entirely, so the setting remains a true off switch."""
    src = inspect.getsource(Scanner)
    idx = src.index("if held is not None and held < min_hold:")
    assert 0 < src.rfind("if min_hold > 0:", 0, idx) < idx


# ── the evidence requirements it protects are still in place ─────────────────

def test_the_loss_exit_still_demands_price_evidence_and_corroboration():
    """This exit was already evidence-based — score, >=2 domains with price mandatory,
    and the same signature confirmed twice. The hold is the missing fourth leg, not a
    replacement for the other three."""
    src = inspect.getsource(Scanner)
    assert "peak_threat_exit_loss_min_score" in src
    assert '"price" in evidence_domains' in src
    assert "peak_threat_exit_loss_confirm_seconds" in src


def test_the_cost_floor_still_applies():
    """A loss smaller than the round trip cannot be worth paying a round trip to avoid."""
    src = inspect.getsource(Scanner)
    assert "_round_trip_cost_rs" in src
