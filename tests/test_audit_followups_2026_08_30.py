"""The three items left open by the 2026-08-30 A-Z certification.

Each one is a failure that is INVISIBLE in production -- nothing errors, nothing is
logged, and the dashboard shows something plausible. That is what makes them worth
pinning rather than merely fixing.

1. The 5-minute lane fails closed when the 15-minute state is merely UNAVAILABLE. A cold
   start, an engine that failed to build, or a lookup that raised all produce
   position 0, which never equals +/-1, so trend_15m_agrees is False -- and in REAL mode
   that flag alone closes the lane (swept: 0 of 5,775 combinations pass with it False, in
   all seven regimes). 5min carries 181 of 252 recorded entries. No decision row was
   written, so it read as "no 5min signal today" rather than "the 15m state is missing".

2. The Signal Forensics panel served a fixture as a real lifecycle.
   signal_lifecycle_events held exactly ONE row across 24 sessions -- lifecycle_id
   "LFC-TEST-12345", grade A+, pnl +500 -- and both writers had zero call sites, so no
   real signal had ever reached the surface. Because the endpoint falls back to the most
   recent available date and the fixture was the only date present, it was the DEFAULT
   view.

3. LOW_GAIN_PROTECT carried a disjunct that could never decide, and sits in opposite
   relative order to SMART_PROFIT_LOCK in the two exit ladders.
"""
import inspect
import re
from types import SimpleNamespace

import pytest

from scanner import Scanner
from tests.source_views import scanner_runtime_source


# ── 1. a missing 15m state must be distinguishable, and audible ──────────────

def _scanner_with_engines(engines):
    sc = Scanner.__new__(Scanner)
    sc.mtf = SimpleNamespace(engines=engines)
    sc._logged = []
    sc.log_signal_decision_once = lambda k, m, t="trade": sc._logged.append((k, m))
    return sc


def _candidate(direction="LONG", instrument="NIFTY"):
    return SimpleNamespace(instrument=instrument, direction=direction, timeframe="5min")


def test_a_missing_15m_engine_is_reported_not_silently_refused():
    """The regression: this closed 72% of the book with nothing written down."""
    sc = _scanner_with_engines({})
    candidate = _candidate()

    agrees = sc._candidate_15m_trend_agrees(candidate)

    assert agrees is False, "admission must be unchanged -- it still fails closed"
    assert candidate.trend_15m_state == "unavailable"
    assert sc._logged, "the absence was not reported"
    assert "unavailable" in sc._logged[0][1]


def test_a_genuinely_opposed_15m_trend_is_not_reported_as_an_outage():
    """position 0 or an opposing position is a real disagreement, not an absence.
    Crying outage on every ordinary disagreement would bury the real signal."""
    sc = _scanner_with_engines(
        {"NIFTY_15min": SimpleNamespace(get_state=lambda key: SimpleNamespace(position=-1))}
    )
    candidate = _candidate(direction="LONG")

    assert sc._candidate_15m_trend_agrees(candidate) is False
    assert candidate.trend_15m_state == "disagrees"
    assert sc._logged == [], "an ordinary disagreement was logged as an outage"


def test_an_agreeing_15m_trend_still_admits():
    sc = _scanner_with_engines(
        {"NIFTY_15min": SimpleNamespace(get_state=lambda key: SimpleNamespace(position=1))}
    )
    candidate = _candidate(direction="LONG")

    assert sc._candidate_15m_trend_agrees(candidate) is True
    assert candidate.trend_15m_state == "agrees"
    assert sc._logged == []


def test_a_raising_lookup_refuses_closed_but_says_so():
    """Was a bare `except Exception: return False` -- any failure became a permanent,
    unlogged refusal."""
    def boom(_key):
        raise RuntimeError("engine exploded")

    sc = _scanner_with_engines({"NIFTY_15min": SimpleNamespace(get_state=boom)})
    candidate = _candidate()

    assert sc._candidate_15m_trend_agrees(candidate) is False
    assert candidate.trend_15m_state == "unavailable"


def test_the_instrument_key_is_normalised_like_its_sibling_site():
    """scanner.py:1277 splits this field defensively; this site did not, and a suffix
    would have missed mtf.engines and closed the lane for that index."""
    sc = _scanner_with_engines(
        {"NIFTY_15min": SimpleNamespace(get_state=lambda key: SimpleNamespace(position=1))}
    )
    candidate = _candidate(instrument="NIFTY 25AUG")

    assert sc._candidate_15m_trend_agrees(candidate) is True


# ── 2. the forensics panel must never present a fixture as real ──────────────

def test_a_fixture_row_is_never_served_as_a_real_lifecycle():
    from dashboard.server import _is_synthetic_forensic_row

    assert _is_synthetic_forensic_row(
        {"lifecycle_id": "LFC-TEST-12345", "signal_id": "SIG-TEST-999"}
    ) is True


def test_a_real_lifecycle_is_not_mistaken_for_a_fixture():
    from dashboard.server import _is_synthetic_forensic_row

    assert _is_synthetic_forensic_row(
        {"lifecycle_id": "NIFTY|5min|LONG|2026-08-27T10:15:00", "signal_id": "a3f9c2"}
    ) is False


def test_the_panel_is_allowed_to_be_honestly_empty():
    """Showing nothing beats showing an invented trade."""
    from dashboard.server import _drop_synthetic_forensic_rows

    result = _drop_synthetic_forensic_rows(
        {"items": [{"lifecycle_id": "LFC-TEST-12345"}]}
    )

    assert result["items"] == []
    assert result["synthetic_rows_hidden"] == 1


def test_the_flight_recorder_has_a_caller():
    """Both writers were fully built and never invoked. A recorder with no caller is
    indistinguishable from a broken one, and the panel cannot tell you which."""
    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager)
    definitions = len(re.findall(r"def _record_trade_forensics\b", src))
    calls = len(re.findall(r"self\._record_trade_forensics\(", src))

    assert definitions == 1
    assert calls >= 1, "the flight recorder still has no production caller"


# ── 3. the low-gain rule and the two ladders ─────────────────────────────────

def test_the_unreachable_low_gain_disjunct_is_gone():
    """`or current_gain_pct < 2.0` under a `peak_gain_pct >= 10.0` gate can never
    decide: peak*0.40 is never below 4.0, so {current<2.0} is a strict subset of
    {current<4.0}. Verified exhaustively over 39.8M (peak, current) pairs -- zero cases
    where it fired alone."""
    src = scanner_runtime_source()
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )

    assert "current_gain_pct < 2.0" not in code


def test_the_low_gain_rule_still_fires_where_it_always_did():
    """Removing the dead disjunct must be behaviour-identical."""
    def old(peak, current):
        return peak >= 10.0 and (current < peak * 0.40 or current < 2.0)

    def new(peak, current):
        return peak >= 10.0 and current < peak * 0.40

    for peak in (9.0, 10.0, 12.5, 40.0, 100.0):
        for current in (-20.0, -1.0, 0.0, 1.9, 2.0, 3.9, 4.0, 5.0, 39.0, 41.0):
            assert old(peak, current) == new(peak, current), (peak, current)


def _ladder(method):
    """The exit reasons a ladder emits, in order. Literal names only -- the live lane
    also emits some via a variable (cap_rule), which this deliberately does not chase."""
    src = inspect.getsource(method)
    # The S1 lane (2026-09-23/24; since 2026-09-25 also UT SMART EXIT, via is_lane) is its
    # own exit set placed ahead of the ladder in both loops and ends where the ladder
    # starts; this test pins the other engines' ladder. Since 2026-10-07 the gate reads the
    # frozen exit owner (is_managed_lane) instead of the admission switch (is_lane).
    start = src.find("if s1_lane_policy.is_managed_lane(")
    if start < 0:
        start = src.find("if s1_lane_policy.is_lane(")
    if start >= 0:
        end = src.find("fast_exit, fast_exit_reason = self._fifteen_min_one_min_support_exit_decision(", start)
        if end < 0:
            end = src.find("fast_exit, fast_exit_reason = scanner._fifteen_min_one_min_support_exit_decision(", start)
        assert end > start, "S1 lane block no longer sits directly before the ladder"
        src = src[:start] + src[end:]
    out = []
    pattern = r'(?:create_exit\(\s*[^,]+,\s*[^,]+,\s*|_close_and_record\(\s*[^,]+,\s*[^,]+,\s*)"([A-Z_]+)"'
    for match in re.finditer(pattern, src):
        name = match.group(1)
        if not out or out[-1] != name:
            out.append(name)
    return out


def test_the_two_ladders_disagree_and_that_is_pinned():
    """NOT asserting they match -- making them match is a live-money behaviour change
    that no available data can justify, because session_signals records only the
    session lane. This pins the divergence so it is a known property rather than an
    accident, and so the next change to either order has to be deliberate.

    The specific inversion: LOW_GAIN_PROTECT runs BEFORE SMART_PROFIT_LOCK in the
    session lane and AFTER it in the live one, so the same trade can be attributed and
    priced differently depending on which lane closed it.
    """
    session = _ladder(Scanner._check_session_candidates_breaches_locked)
    from engine.exit_cycle import ExitCycleService

    live = _ladder(ExitCycleService.update_active_trades)

    assert session.index("LOW_GAIN_PROTECT") < session.index("SMART_PROFIT_LOCK")
    assert live.index("SMART_PROFIT_LOCK") < live.index("LOW_GAIN_PROTECT")

    # Both still open on the same four and close on stagnation.
    assert session[:4] == live[:4] == [
        "OI_PCR_REVERSAL", "INTEL_FLIP", "RECOVERY_WATCH_EXIT", "EARLY_LOSS_CUT",
    ]
    assert session[-1] == live[-1] == "STAGNATION_EXIT"
