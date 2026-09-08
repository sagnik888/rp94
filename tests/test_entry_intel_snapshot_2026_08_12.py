"""The engine's intel never reached the trade record, so it could never be evaluated.

Measured over 239 accepted trades:

    volume.volume_ratio        recorded on   2%   (5 rows)
    grading.* pillars          recorded on  33%
    PCR / OI / order_flow      no field existed at all
    grading.pillars.vix_penalty non-zero on 3 of 76

intelligence_history.json does hold these series per minute, but as a rolling
5,000-point window: only 76 of 240 trades were still joinable to it. The evidence
expires before anyone uses it.

OBSERVATION ONLY. A composite entry GATE over these features was built, measured and
REJECTED before shipping:

    thresholds learned on the first 120 trades, applied to the next 120
      TRAIN  K>=3   55.6% win rate,  Rs.752/trade   (baseline 47.5% / Rs.466)
      TEST   K>=3   16.7% win rate, -Rs.336/trade   (baseline 32.5% / -Rs.72)

and the adaptive rolling-median variant beat its own baseline win rate in NO
configuration. The separation was curve-fitting: the medians came from the same rows
being scored. This file therefore tests a recorder, not a filter — and one assertion
below pins that nothing consumes it for a decision.
"""
from types import SimpleNamespace

import pytest

from intelligence.rtracker import _entry_intel_block
from scanner import Scanner

INTEL = {
    "volume": {"buy_sell_ratio": 1.23, "volume_ratio": 2.4},
    "oi": {"signal": "BEARISH",
           "cumulative_analysis": {"total_call_oi": 1.2e8, "total_put_oi": 1.6e8}},
    "pcr": {"pcr": 1.337},
    "order_flow": {"ratio": 0.84},
    "aggregate": {"score": -3.2},
    "breadth_context": {"aligned_count": 3, "bias": "BEARISH"},
    "session_context": {"day_type": "TREND_DAY_DOWN", "confidence": 0.81,
                        "close_above_vwap_pct": 0.34},
    "greeks": {"iv_percentile": 48.0,
               "call": {"delta": 0.4845, "gamma": 0.002207, "theta": -20.1, "vega": 7.2, "iv": 10.0},
               "put": {"delta": -0.51, "gamma": 0.0021, "theta": -19.0, "vega": 7.1, "iv": 10.5}},
}


def _sc(intel=INTEL, vix=13.4):
    sc = Scanner.__new__(Scanner)
    sc._cached_timeframe_intel = lambda *a, **k: intel
    sc._premium_cache = {"INDIAVIX": {"price": vix, "time": 0}} if vix else {}
    sc._vix_percentile = lambda v, t: 41.2
    return sc


def _cand(option_type="PE"):
    return SimpleNamespace(instrument="NIFTY", timeframe="5min", option_type=option_type)


def _snap(**kw):
    return Scanner._entry_intel_snapshot(_sc(**kw), _cand())


# ── it captures what was previously lost ─────────────────────────────────────

@pytest.mark.parametrize("field,expected", [
    ("volume_ratio", 2.4),          # was recorded on 2% of accepted rows
    ("pcr", 1.337),                 # had no field at all
    ("order_ratio", 0.84),          # had no field at all
    ("total_call_oi", 1.2e8),       # had no field at all
    ("total_put_oi", 1.6e8),        # had no field at all
    ("iv_percentile", 48.0),
    ("breadth_aligned", 3.0),
    ("close_above_vwap_pct", 0.34),
    ("aggregate_score", -3.2),
    ("vol_buy_sell_ratio", 1.23),
])
def test_the_previously_unrecorded_fields_are_captured(field, expected):
    assert _snap()[field] == pytest.approx(expected)


@pytest.mark.parametrize("field,expected", [
    ("oi_signal", "BEARISH"), ("breadth_bias", "BEARISH"), ("day_type", "TREND_DAY_DOWN"),
])
def test_the_categorical_context_is_captured(field, expected):
    assert _snap()[field] == expected


def test_the_regime_context_is_captured_not_just_the_label():
    """The exit row keeps hard_label and adx and drops everything that says HOW the
    market was behaving — measured across 240 traded rows:

        ACCEPTED   atr_pct 99%   bb_width 99%   kaufman_er 99%   confidence 99%
        EXIT       atr_pct  0%   bb_width  0%   kaufman_er  0%   confidence  0%

    kaufman_er is the one measure separating a one-way grind from chop of the same SIZE,
    and it was present on ZERO traded rows — so "did the regime turn mid-trade?" and
    "does directional efficiency predict outcome?" were both unanswerable.
    """
    intel = dict(INTEL)
    intel["regime"] = {"regime": "RANGING", "confidence": 0.71, "kaufman_er": 0.34,
                       "adx": 18.2, "atr_pct": 0.31, "bb_width": 0.9}
    s = Scanner._entry_intel_snapshot(_sc(intel), _cand())
    assert s["regime_label"] == "RANGING"
    assert s["kaufman_er"] == pytest.approx(0.34)
    assert s["adx"] == pytest.approx(18.2)
    assert s["atr_pct"] == pytest.approx(0.31)
    assert s["bb_width"] == pytest.approx(0.9)
    assert s["regime_confidence"] == pytest.approx(0.71)


def test_a_directional_grind_labelled_ranging_is_now_visible():
    """The exact case that could not be seen: RANGING by label, but ER 0.34 says the
    move is one-way. 32.1% of RANGING events carry ER >= 0.30, and RANGING's median ER
    (0.207) is HIGHER than TRENDING_UP's (0.194) — the labels do not separate on
    directionality at all."""
    intel = dict(INTEL)
    intel["regime"] = {"regime": "RANGING", "kaufman_er": 0.34, "adx": 18.0}
    s = Scanner._entry_intel_snapshot(_sc(intel), _cand())
    assert s["regime_label"] == "RANGING" and s["kaufman_er"] > 0.30


def test_vix_and_its_percentile_are_captured():
    """VIX is fetched live and percentile-ranked, but reached the record only as a
    grading penalty that was non-zero on 3 of 76 rows."""
    s = _snap()
    assert s["india_vix"] == pytest.approx(13.4)
    assert s["vix_percentile"] == pytest.approx(41.2)


# ── the greeks leg must match the contract ───────────────────────────────────

def test_a_pe_candidate_records_the_put_leg():
    """Recording the call delta against a bought PE would invert the sign of the one
    feature that measured a real separation (delta above median: 37.8% vs 17.9%)."""
    assert Scanner._entry_intel_snapshot(_sc(), _cand("PE"))["delta"] == pytest.approx(-0.51)


def test_a_ce_candidate_records_the_call_leg():
    assert Scanner._entry_intel_snapshot(_sc(), _cand("CE"))["delta"] == pytest.approx(0.4845)


def test_a_missing_leg_falls_back_rather_than_dropping_the_greeks():
    intel = dict(INTEL)
    intel["greeks"] = {"iv_percentile": 30.0, "call": {"delta": 0.6}}
    s = Scanner._entry_intel_snapshot(_sc(intel), _cand("PE"))
    assert s["iv_percentile"] == pytest.approx(30.0)
    assert s["delta"] == pytest.approx(0.6)


# ── it can never break an entry ──────────────────────────────────────────────

def test_intel_that_raises_yields_an_empty_snapshot():
    sc = Scanner.__new__(Scanner)

    def boom(*a, **k):
        raise RuntimeError("intel down")

    sc._cached_timeframe_intel = boom
    assert Scanner._entry_intel_snapshot(sc, _cand()) == {}


@pytest.mark.parametrize("intel", [None, {}, [], "junk", {"volume": "not-a-dict"}])
def test_malformed_intel_never_raises(intel):
    sc = Scanner.__new__(Scanner)
    sc._cached_timeframe_intel = lambda *a, **k: intel
    sc._premium_cache = {}
    assert isinstance(Scanner._entry_intel_snapshot(sc, _cand()), dict)


def test_missing_vix_drops_the_field_rather_than_recording_zero():
    """A zero VIX would read as a real measurement — this codebase's recurring defect."""
    s = _snap(vix=None)
    assert "india_vix" not in s and "vix_percentile" not in s


def test_nan_and_infinity_are_dropped():
    intel = dict(INTEL)
    intel["pcr"] = {"pcr": float("nan")}
    intel["order_flow"] = {"ratio": float("inf")}
    s = Scanner._entry_intel_snapshot(_sc(intel), _cand())
    assert "pcr" not in s and "order_ratio" not in s


# ── the rtracker block ───────────────────────────────────────────────────────

def test_the_block_is_json_safe():
    import json

    json.dumps(_entry_intel_block(_snap()))


@pytest.mark.parametrize("value", [None, "junk", 42, [], {"a": object()}])
def test_a_malformed_block_costs_the_field_not_the_record(value):
    assert isinstance(_entry_intel_block(value), dict)


def test_the_block_is_bounded():
    """A runaway payload must not bloat every event in the log."""
    big = {f"k{i}": float(i) for i in range(500)}
    assert len(_entry_intel_block(big)) <= 40


def test_long_strings_are_truncated():
    assert len(_entry_intel_block({"day_type": "X" * 500})["day_type"]) <= 32


def test_the_event_carries_the_block():
    import inspect

    import intelligence.rtracker as rt

    assert '"entry_intel": _entry_intel_block(' in inspect.getsource(rt)


# ── wiring ───────────────────────────────────────────────────────────────────

def test_every_acceptance_route_takes_the_snapshot():
    """_record_accepted_entry is the one point all three acceptance routes reach."""
    import inspect

    src = inspect.getsource(Scanner._record_accepted_entry)
    assert "_entry_intel_snapshot" in src
    assert "entry_intel" in src


def test_the_snapshot_is_taken_once():
    """Re-snapping on a later pass would record conditions that postdate the decision."""
    import inspect

    src = inspect.getsource(Scanner._record_accepted_entry)
    idx = src.index("_entry_intel_snapshot")
    assert 'getattr(candidate, "entry_intel", None)' in src[:idx]


def test_the_exit_row_inherits_it():
    """The outcome and the conditions that preceded it must land in the same record."""
    entry = SimpleNamespace(entry_intel={"pcr": 1.2})
    exit_row = SimpleNamespace()
    Scanner._inherit_exit_entry_metadata(Scanner.__new__(Scanner), exit_row, entry)
    assert exit_row.entry_intel == {"pcr": 1.2}


def test_the_inherited_snapshot_is_a_copy():
    entry = SimpleNamespace(entry_intel={"pcr": 1.2})
    exit_row = SimpleNamespace()
    Scanner._inherit_exit_entry_metadata(Scanner.__new__(Scanner), exit_row, entry)
    entry.entry_intel["pcr"] = 9.9
    assert exit_row.entry_intel["pcr"] == 1.2


def test_exit_inherits_runner_and_lifecycle_provenance():
    entry = SimpleNamespace(
        runner_mode=True,
        runner_activation_timestamp="2026-08-25T10:00:00",
        runner_activation_reason="TARGET 1 HIT",
        runner_stop=123.45,
        stop_provenance="runner",
        lifecycle_id="L-1",
        trade_id="T-1",
    )
    exit_row = SimpleNamespace()
    Scanner._inherit_exit_entry_metadata(Scanner.__new__(Scanner), exit_row, entry)

    assert exit_row.runner_mode is True
    assert exit_row.runner_activation_timestamp == "2026-08-25T10:00:00"
    assert exit_row.runner_activation_reason == "TARGET 1 HIT"
    assert exit_row.runner_stop == 123.45
    assert exit_row.runner_stop_at_exit == 123.45
    assert exit_row.stop_provenance == "runner"
    assert exit_row.lifecycle_id == "L-1"
    assert exit_row.trade_id == "T-1"


def test_exit_inherits_a_copy_of_entry_admission_evidence():
    quality = {
        "allowed": True,
        "policy_version": "quality-verdict-test",
        "checks": {"adx": 27.0, "trend_15m_agrees": True},
    }
    entry = SimpleNamespace(
        quality_verdict=quality,
        accepted_by_gate=True,
        watch_only=False,
        trend_15m_agrees=True,
    )
    exit_row = SimpleNamespace()

    Scanner._inherit_exit_entry_metadata(Scanner.__new__(Scanner), exit_row, entry)

    assert exit_row.accepted_by_gate is True
    assert exit_row.watch_only is False
    assert exit_row.trend_15m_agrees is True
    assert exit_row.quality_verdict == quality
    quality["checks"]["adx"] = 1.0
    assert exit_row.quality_verdict["checks"]["adx"] == 27.0


def test_the_toggle_switches_it_off():
    from config.settings import Settings, get_settings

    s = get_settings()
    saved = getattr(s, "entry_intel_snapshot_enabled")
    try:
        setattr(s, "entry_intel_snapshot_enabled", False)
        import inspect

        src = inspect.getsource(Scanner._record_accepted_entry)
        assert "entry_intel_snapshot_enabled" in src
    finally:
        setattr(s, "entry_intel_snapshot_enabled", saved)
    assert Settings.model_fields["entry_intel_snapshot_enabled"].default is True


# ── it is an instrument, not a filter ────────────────────────────────────────

def test_the_only_consumer_is_the_reversal_reference():
    """The composite gate over these features inverted out-of-sample (16.7% vs a 32.5%
    baseline), so the snapshot may never be used to PREDICT an outcome.

    Exactly one consumer is allowed, and only of `adverse_votes`:
    _intel_reversal_is_new_evidence reads it as the reference an OI_PCR_REVERSAL
    exit is measured against. That makes no predictive claim about the features —
    the same three pillars already decide that exit, and the reference can only
    ever SUPPRESS a firing (it requires the adverse count to have risen since
    entry), never cause one. Any other read is the gate that inverted, and the
    out-of-sample evidence has to be revisited before it is allowed.
    """
    import inspect
    import re

    src = inspect.getsource(Scanner)
    # \b so this matches the entry_intel FIELD and not longer identifiers that
    # merely start with it (e.g. _entry_intelligence_reading, which reads the live
    # intel cache and never touches the snapshot).
    field = re.compile(r"\bentry_intel\b")
    reads = [
        ln for ln in src.splitlines()
        if field.search(ln)
        and "_entry_intel_snapshot" not in ln
        and not ln.strip().startswith("#")
    ]
    for line in reads:
        assert (
            "setattr" in line
            or "getattr" in line
            or "entry_intel_snapshot_enabled" in line
            or "adverse_votes" in line
        ), f"entry_intel is being read for a decision: {line.strip()}"


def test_the_live_trade_receives_the_snapshot_too():
    """Auto mode manages the Trade, not the candidate, and Trade declares no
    entry_intel field — so without the copy the exit guards read None on every
    automated trade and fall back to their first-reading latch."""
    sc = Scanner.__new__(Scanner)
    sc._reset_daily_counters = lambda: None
    sc._counted_entry_keys = {"t1"}  # short-circuits after the snapshot block
    cand = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", option_type="CE",
        direction="LONG", entry_intel={"pcr": 1.2, "adverse_votes": 1},
    )
    trade = SimpleNamespace(id="t1")

    Scanner._record_accepted_entry(sc, cand, trade)

    assert trade.entry_intel == {"pcr": 1.2, "adverse_votes": 1}
    cand.entry_intel["pcr"] = 9.9
    assert trade.entry_intel["pcr"] == 1.2


def test_the_reversal_reference_can_only_suppress_an_exit():
    """The guarantee the allowance above depends on: it returns a bool that gates an
    already-decided reversal_reason, so no code path turns it into an exit."""
    import inspect

    src = inspect.getsource(Scanner._intel_reversal_is_new_evidence)
    assert "create_exit" not in src and "_close_and_record" not in src
