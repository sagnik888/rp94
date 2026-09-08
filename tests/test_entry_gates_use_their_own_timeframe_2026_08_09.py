"""Admission/loosening cannot borrow a different timeframe's regime.

The instrument-only regime cache is the 5-minute label. It remains a conservative
fallback for exit/risk management, but canonical admission treats a missing 15-minute
label as UNKNOWN and scalp-lock release requires exact-timeframe ownership.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from scanner import Scanner

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def _scanner(tf_regimes=None, latest=None):
    s = Scanner.__new__(Scanner)
    s._timeframe_regimes = dict(tf_regimes or {})
    s.latest_regimes = dict(latest or {})
    return s


def _cand(instrument="NIFTY", timeframe="15min"):
    return SimpleNamespace(instrument=instrument, timeframe=timeframe)


# ── the borrowed label is identified as borrowed ─────────────────────────────

@pytest.mark.parametrize("index", INDICES)
def test_a_15min_candidate_with_no_own_label_is_flagged_as_borrowing(index):
    s = _scanner(latest={index: "TRENDING_UP"})
    assert Scanner._get_trade_regime(s, _cand(index, "15min")) == "TRENDING_UP"
    assert Scanner._trade_regime_is_own_timeframe(s, _cand(index, "15min")) is False, (
        "the 5-minute label is being treated as this candidate's own"
    )


@pytest.mark.parametrize("index", INDICES)
def test_an_own_timeframe_label_is_preferred_over_the_5min_one(index):
    s = _scanner(tf_regimes={f"{index}_15min": "RANGING"}, latest={index: "TRENDING_UP"})
    assert Scanner._get_trade_regime(s, _cand(index, "15min")) == "RANGING"
    assert Scanner._trade_regime_is_own_timeframe(s, _cand(index, "15min")) is True


def test_a_5min_candidate_owns_the_instrument_keyed_label():
    s = _scanner(latest={"NIFTY": "TRENDING_UP"})
    assert Scanner._trade_regime_is_own_timeframe(s, _cand("NIFTY", "5min")) is True


# ── the gates read their own timeframe ───────────────────────────────────────

GATES = [
    "_market_location_entry_gate",
    "_candidate_quality_context",
    "_live_signal_stabilization_requirements",
]


@pytest.mark.parametrize("fn_name", GATES)
def test_the_gate_no_longer_reads_the_instrument_keyed_5min_label(fn_name):
    src = inspect.getsource(getattr(Scanner, fn_name))
    tree = ast.parse(textwrap.dedent(src))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # latest_regimes.get(candidate.instrument, ...) in any spelling
        if isinstance(f, ast.Attribute) and f.attr == "get" and "latest_regimes" in ast.dump(f.value):
            arg = ast.dump(node.args[0]) if node.args else ""
            if "timeframe" not in arg and "_timeframe_intel_key" not in arg:
                bad.append(node.lineno)
    assert not bad, (
        f"{fn_name} still grades on the instrument-keyed 5-minute regime at offsets {bad}"
    )


@pytest.mark.parametrize("fn_name", GATES)
def test_the_gate_uses_the_own_timeframe_resolver(fn_name):
    assert "_get_trade_regime(" in inspect.getsource(getattr(Scanner, fn_name))


def test_live_gate_delegates_regime_resolution_to_the_quality_context():
    assert "_candidate_quality_context(" in inspect.getsource(
        Scanner._passes_live_trade_ready_gate
    )


# ── privileges refuse a borrowed label ───────────────────────────────────────

def _calls_in(node):
    """Names of every function/method called anywhere inside an AST node."""
    return {
        (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
    }


def test_the_retired_b_grade_bypass_cannot_return():
    """B/B+ admission belongs to the canonical evidence verdict, not this preference."""
    source = inspect.getsource(Scanner._passes_candidate_grade_preference)
    assert "_b_grade_entry_allowed" not in source
    assert "live_b_grade_min_confidence" not in source
    assert "canonical quality policy owns B+ eligibility" in source


def test_the_scalp_lock_release_requires_an_own_timeframe_regime():
    """Releasing the lock LOOSENS the tightest profit lock. AST, not text."""
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(Scanner._market_location_entry_gate)))
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "trend_pullback_entry" for t in n.targets)
    ]
    assert assigns, "trend_pullback_entry assignment not found"
    assert any("_trade_regime_is_own_timeframe" in _calls_in(a.value) for a in assigns), (
        "the scalp lock can still be released on a borrowed 5-minute label"
    )


def test_the_restricting_gates_are_not_gated_on_own_timeframe():
    """Tightening on a borrowed label is explicitly documented as fine. Requiring
    own-timeframe there would DISABLE a veto when the label is missing -- the unsafe
    direction. This test exists so nobody 'completes' the fix by adding it."""
    for fn_name in ("_passes_live_trade_ready_gate", "_live_signal_stabilization_requirements"):
        src = inspect.getsource(getattr(Scanner, fn_name))
        assert "_trade_regime_is_own_timeframe" not in src, (
            f"{fn_name} is a restriction; gating it on own-timeframe removes protection"
        )


# ── the premise ──────────────────────────────────────────────────────────────

def test_the_rule_this_enforces_is_still_written_down():
    doc = inspect.getdoc(Scanner._trade_regime_is_own_timeframe) or ""
    assert "must NOT be used to grant a privilege" in doc
    assert "83%" in doc


def test_latest_regimes_is_still_the_5min_label():
    """If this ever becomes per-timeframe, the whole fix is unnecessary."""
    src = inspect.getsource(Scanner.__init__)
    line = [ln for ln in src.splitlines() if "self.latest_regimes" in ln and ":" in ln]
    assert line and "5min" in line[0], line


def test_runner_mode_still_enforces_it_too():
    """The one seam that already had the rule must not have lost it."""
    src = inspect.getsource(Scanner)
    assert src.count("_trade_regime_is_own_timeframe(") >= 3
