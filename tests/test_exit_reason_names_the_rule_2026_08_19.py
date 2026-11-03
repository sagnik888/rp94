"""An exit must be logged under the rule that actually fired.

2026-08-18, live session. The most frequent exit reason in the log was
PEAK_GIVEBACK_CAP, on 5 of 12 trades, and every one of them closed NEGATIVE:

    SENSEX    15min  peak Rs.277  ->  -Rs.190
    BANKNIFTY  5min  peak Rs.158  ->  -Rs.294
    BANKNIFTY  5min  peak Rs.198  ->  -Rs.143
    MIDCPNIFTY 15min peak Rs.180  ->  -Rs.158
    SENSEX     5min  peak Rs.236  ->  -Rs.135

A give-back cap books a REDUCED GAIN; it cannot book a loss. The explanation is
that the cap never ran: PEAK_GIVEBACK_CAP_ENABLED was 'False' in .env all day
(the 05:33 backup still had 'True', so it was turned off before the open).

_peak_giveback_cap_exit hosts TWO rules. The breakeven stop is checked first and
returns on its own terms; the disabled cap then returns False immediately after.
So the rule that fired on all five was the BREAKEVEN STOP — arm Rs.150, floor
Rs.0 — and every peak above was between Rs.158 and Rs.277, i.e. just over that
arm, with the exit at or below zero. That is the breakeven condition exactly.

Both call sites then logged the hardcoded string "PEAK_GIVEBACK_CAP".

The cost of this is not the fill, it is the feedback loop. config/settings.py:751
records, as the stated basis for a design decision, that "PEAK_GIVEBACK_CAP had
never fired in any log" — while it is the dominant exit label in the live log.
Both observations are honest; the name simply does not identify the rule. Any
tuning of either rule from this log is tuning the wrong one.

This test fixes the NAME only. No threshold, no ordering and no fill changes.
"""
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from scanner import Scanner
from tests.source_views import scanner_runtime_source


def _sc():
    sc = Scanner.__new__(Scanner)
    sc.mode = "LIVE"
    return sc


def _t(**kw):
    base = dict(instrument="NIFTY", inst_type="OPT", timeframe="5min")
    base.update(kw)
    return SimpleNamespace(**base)


def _verdict(peak, now_pnl):
    return Scanner._peak_giveback_cap_exit(
        _sc(), _t(), current_pnl_rs=now_pnl, peak_pnl_rs=peak
    )


# ── the name must follow the rule ────────────────────────────────────────────

def test_the_breakeven_stop_is_not_called_the_giveback_cap(monkeypatch):
    """The 2026-08-18 case: cap OFF, breakeven ON, a proven trade back at zero."""
    settings = get_settings()
    monkeypatch.setattr(settings, "peak_giveback_cap_enabled", False)
    monkeypatch.setattr(settings, "breakeven_stop_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_arm_rs", 150.0)
    monkeypatch.setattr(settings, "breakeven_stop_floor_rs", 0.0)

    v = _verdict(peak=277.0, now_pnl=-190.0)
    assert v[0] is True
    assert v.rule == "BREAKEVEN_STOP"
    assert v.rule != "PEAK_GIVEBACK_CAP"


def test_the_giveback_cap_still_names_itself(monkeypatch):
    """When the cap is the rule that fires, the name is unchanged."""
    settings = get_settings()
    monkeypatch.setattr(settings, "peak_giveback_cap_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_enabled", False)

    v = _verdict(peak=800.0, now_pnl=500.0)
    assert v[0] is True
    assert v.rule == "PEAK_GIVEBACK_CAP"


def test_a_quiet_verdict_names_no_rule(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "peak_giveback_cap_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_enabled", True)

    v = _verdict(peak=40.0, now_pnl=35.0)
    assert v[0] is False
    assert v.rule == ""


@pytest.mark.parametrize(
    "peak,final",
    [(277.0, -190.0), (157.5, -294.5), (198.0, -143.0), (180.0, -158.0), (236.0, -135.0)],
)
def test_every_2026_08_18_firing_was_the_breakeven_stop(monkeypatch, peak, final):
    """All five recorded 'PEAK_GIVEBACK_CAP' exits, replayed under that day's config."""
    settings = get_settings()
    monkeypatch.setattr(settings, "peak_giveback_cap_enabled", False)
    monkeypatch.setattr(settings, "breakeven_stop_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_arm_rs", 150.0)
    monkeypatch.setattr(settings, "breakeven_stop_floor_rs", 0.0)

    v = _verdict(peak=peak, now_pnl=final)
    assert v[0] is True, "the recorded exit must reproduce"
    assert v.rule == "BREAKEVEN_STOP"


# ── backward compatibility: everything that already unpacks this keeps working ──

def test_the_verdict_is_still_a_two_tuple(monkeypatch):
    """37 existing tests and the backtest probe in engine/signal_processor.py do
    `fired, why = ...`. Attaching the rule must not become a third element."""
    settings = get_settings()
    monkeypatch.setattr(settings, "peak_giveback_cap_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_enabled", False)

    v = _verdict(peak=800.0, now_pnl=500.0)
    assert len(v) == 2
    fired, why = v                      # must not raise
    assert fired is True
    assert isinstance(why, str) and why


# ── wiring: the live monitors must use the verdict's rule, not a literal ──────

def test_neither_live_call_site_hardcodes_the_exit_name():
    """Both monitor paths previously passed the literal "PEAK_GIVEBACK_CAP" into
    create_exit / _close_and_record regardless of which rule fired."""
    import inspect

    src = scanner_runtime_source()
    sites = [i for i in range(len(src)) if src.startswith("self._peak_giveback_cap_exit(", i)]
    sites += [i for i in range(len(src)) if src.startswith("scanner._peak_giveback_cap_exit(", i)]
    assert len(sites) == 2, f"expected 2 call sites, found {len(sites)}"

    for start in sites:
        window = src[start:start + 1600]
        assert '"PEAK_GIVEBACK_CAP"' not in window, (
            "call site still hardcodes the exit reason instead of using the "
            "rule the verdict reports"
        )
        assert "cap_rule" in window, "call site does not read the rule off the verdict"


def test_the_breakeven_stop_keeps_the_caps_stop_out_treatment():
    """Pinned deliberately, and NOT changed here.

    PEAK_GIVEBACK_CAP is excluded from STOP_OUT_EXIT_REASONS because a cap exit is
    frequently profitable, so counting it would wrongly suppress re-entry. Renaming
    the breakeven firings must not silently move them into that set — that would be
    a behaviour change smuggled in under a logging fix.

    Whether BREAKEVEN_STOP *should* suppress re-entry is a real question (it closes
    a trade that failed), but it is a separate decision with its own evidence.
    """
    assert "PEAK_GIVEBACK_CAP" not in Scanner.STOP_OUT_EXIT_REASONS
    assert "BREAKEVEN_STOP" not in Scanner.STOP_OUT_EXIT_REASONS
