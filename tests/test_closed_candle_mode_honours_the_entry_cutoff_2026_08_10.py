"""CANDLE_CLOSE mode never applied the per-candidate entry cutoff.

Every per-candidate `_entry_cutoff_allows_candidate` call lives inside the stabilization
block guarded by `if self.mode != "HISTORICAL" and not is_warmup and intrabar_enabled:`.
In CANDLE_CLOSE that block is skipped wholesale, leaving one wave-level `any()` that is
(a) across all candidates rather than per candidate and (b) only reached once the clock is
already past ut_5min_no_entry_after. So a bar that closed inside the window but is
processed after it entered unchecked:

    14:45  15min bar prints a BUY; closed-candle mode defers it to the close
    15:00  bar closes
    15:03  slow refresh emits it — still inside the post-close grace, so the ghost
           guard passes, and after_no_entry is False because the BAR stamp is 14:45
    -> a fresh position opens ~15:03, past the no-entry policy and ~21 minutes before
       the 15:25 hard square-off

LATENT TODAY: the shipped config runs INTRACANDLE (live_intrabar_signal_entries=True), so
the live path already rejects this candidate. It is one dashboard save away from live —
configure() calls settings.set_signal_fire_mode — which is why it is worth closing.

The check is confined to the closed-candle branch. Applying it that early in INTRACANDLE
would reject candidates the stabilization buffer is still entitled to mature, which is a
live-path behaviour change this deliberately does not make.
"""
import inspect
import re

import pytest

from scanner import Scanner
from engine.execution_service import ExecutionService


def _timing_loop_source():
    """The branch under test, sliced from marker to marker rather than by line window.

    Fixed char/line windows in this repo have silently drifted as the file grew and then
    asserted against the wrong code.
    """
    src = inspect.getsource(ExecutionService.coordinate_and_execute)
    start = src.index("timing_eligible = []")
    end = src.index("Live Anti-Repaint Stabilization Buffer")
    # Comments are stripped: the explanatory comment on this branch names the very
    # function the assertions search for, so a raw slice matches prose instead of code.
    body = "\n".join(
        ln for ln in src[start:end].splitlines() if not ln.strip().startswith("#")
    )
    assert body.strip(), "timing-loop slice came back empty"
    return body


def test_the_closed_candle_branch_applies_the_cutoff():
    """Asserts the whole guard expression, not merely that the call appears.

    A first draft asserted only that the name was present, which `if False and
    not self._entry_cutoff_allows_candidate(...)` satisfies — the dead-code trap this
    repo has already been caught by twice.
    """
    body = _timing_loop_source()
    assert "not intrabar_enabled and not scanner._entry_cutoff_allows_candidate(" in body, (
        "CANDLE_CLOSE still admits a candidate whose candle closed past the policy"
    )


def test_the_cutoff_there_is_gated_to_closed_candle_mode():
    """It must NOT fire in INTRACANDLE, where the buffer owns the decision."""
    body = _timing_loop_source()
    idx = body.index("_entry_cutoff_allows_candidate")
    guard = body[max(0, idx - 200): idx]
    assert "not intrabar_enabled" in guard, (
        "the cutoff was applied in both fire modes; INTRACANDLE behaviour changed"
    )


def test_the_intracandle_call_sites_are_still_present():
    """The three buffer-path checks must survive untouched."""
    src = inspect.getsource(ExecutionService.coordinate_and_execute)
    assert src.count("_entry_cutoff_allows_candidate") >= 4, (
        "a pre-existing cutoff call site was lost"
    )


def test_the_stabilization_block_is_still_intracandle_only():
    """The premise. If this block ever runs in CANDLE_CLOSE the new check is redundant."""
    src = inspect.getsource(ExecutionService.coordinate_and_execute)
    assert 'not is_warmup and intrabar_enabled:' in src


def test_the_fire_mode_is_reachable_at_runtime():
    """Why a latent bug was worth closing: an operator can flip this from the dashboard."""
    from config.settings import Settings

    assert hasattr(Settings, "set_signal_fire_mode")
    assert "set_signal_fire_mode" in inspect.getsource(Scanner.configure)


def test_the_shipped_mode_is_intracandle():
    """UT Smart re-enabled the forming-candle path on 2026-09-26."""
    from config.settings import Settings

    # The user's .env may legitimately select candle-close mode.
    defaults = {name: Settings.model_fields[name].default for name in
                ('ut_signal_mode', 'live_intrabar_signal_entries')}
    assert Settings(_env_file=None, **defaults).get_signal_fire_mode() == "INTRACANDLE"


# ── the predicate itself still behaves ───────────────────────────────────────

def test_the_cutoff_predicate_is_unchanged():
    sig = inspect.signature(Scanner._entry_cutoff_allows_candidate)
    assert list(sig.parameters) == ["self", "candidate", "session_now"]
