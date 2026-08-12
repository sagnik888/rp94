"""An intelligence score of 0.0 is a reading. Absent intelligence is not.

_candidate_signed_intelligence returns 0.0 for a balanced book AND for every
failure path — missing cache, wrong timeframe, malformed payload, exception. The
15-minute choppy-scalp guard then demands `signed_intel >= 0.20` for a BUY, so
"we have no reading" and "the book is neutral" both read as "not aligned" and
both refuse the entry.

2026-08-12 14:36 and 14:55, during the day's largest move:

    blocked MIDCPNIFTY 15min LONG; 15min choppy scalp guard:
        signed intelligence -0.00 not aligned for BUY
    blocked MIDCPNIFTY 15min LONG; 15min choppy scalp guard:
        signed intelligence -0.01 not aligned for BUY

restore_point_51 reports availability separately from the score
(_fresh_entry_intelligence_metrics -> (available, signed, adx, reason)) and reads
the EXACT timeframe with no fallback. That contract is implemented here on this
build's own primitives rather than chain-porting its cache readers.

Separately: the guard fast-tracks market-wide reversals through an 8-second
stabilization buffer and then vetoes them as isolated chop scalps. Those entries
already clear B+ grade, 60% confidence and an independently confirmed peer
(_market_wide_candidate_quality), which is strictly more evidence than a single
instrument's intel alignment.
"""
import time
from types import SimpleNamespace

import pytest

from scanner import Scanner


def _intel(score=0.55, adx=27.0):
    """score_norm is the canonical -1..+1 field; bare `score` is legacy-scaled and
    aggregate_intelligence_score divides it (engine/intelligence_score.py)."""
    return {"aggregate": {"score_norm": score}, "regime": {"adx": adx}}


def _sc(cache=None, age=1.0):
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._intel_cache = cache if cache is not None else {}
    sc._cached_results = {}
    sc._intel_age_seconds = lambda *_a, **_k: age
    return sc


def _cand(instrument="MIDCPNIFTY", timeframe="15min", **kw):
    base = dict(
        instrument=instrument, timeframe=timeframe, inst_type="OPT",
        direction="LONG", grade="B+", confidence=0.66,
        intelligence_score=0.0, adx_value=0.0,
        power_move=False, volume_spike_entry=False,
        late_session_reclaim_entry=False, market_wide_reversal_entry=False,
        market_ltp_shock_entry=False, one_way_cascade_entry=False,
        quad_union_entry=False, raw_union_entry=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── availability is reported separately from the score ───────────────────────

def test_a_real_zero_reading_is_available():
    """A balanced book is evidence. It must not read as missing data."""
    sc = _sc({"MIDCPNIFTY_15min": _intel(score=0.0)})

    available, signed, _adx, source = sc._entry_intelligence_reading(_cand(), "15min")

    assert available is True
    assert signed == pytest.approx(0.0)
    assert source == "fresh_exact_intelligence"


def test_absent_intelligence_is_unavailable_not_zero():
    sc = _sc({"NIFTY_5min": _intel()})  # cache exists, wrong instrument/timeframe

    available, signed, _adx, reason = sc._entry_intelligence_reading(_cand(), "15min")

    assert available is False
    assert signed == 0.0
    assert "unavailable" in reason


def test_it_reads_the_exact_timeframe_and_never_falls_back():
    """A 5-minute reading is not evidence about a 15-minute entry."""
    sc = _sc({"MIDCPNIFTY_5min": _intel(score=0.9)})

    available, _signed, _adx, _reason = sc._entry_intelligence_reading(_cand(), "15min")

    assert available is False


def test_stale_intelligence_is_unavailable():
    sc = _sc({"MIDCPNIFTY_15min": _intel()}, age=1200.0)

    available, _signed, _adx, reason = sc._entry_intelligence_reading(_cand(), "15min")

    assert available is False
    assert "stale" in reason


def test_unknown_age_is_not_treated_as_stale():
    """None means "cannot tell", which is not evidence of staleness."""
    sc = _sc({"MIDCPNIFTY_15min": _intel()})
    sc._intel_age_seconds = lambda *_a, **_k: None

    available, _signed, _adx, _reason = sc._entry_intelligence_reading(_cand(), "15min")

    assert available is True


def test_a_real_reading_carries_its_score_and_adx():
    sc = _sc({"MIDCPNIFTY_15min": _intel(score=0.62, adx=31.0)})

    available, signed, adx, _reason = sc._entry_intelligence_reading(_cand(), "15min")

    assert available is True
    assert signed == pytest.approx(0.62, abs=0.05)
    assert adx == pytest.approx(31.0)


def test_a_scanner_without_a_cache_keeps_the_candidate_snapshot():
    """Bare Scanner.__new__ test doubles own no cache; restore_point_51 preserves
    their explicit candidate values rather than failing closed."""
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"

    available, signed, adx, source = sc._entry_intelligence_reading(
        _cand(intelligence_score=0.44, adx_value=26.0), "15min"
    )

    assert available is True
    assert signed == pytest.approx(0.44, abs=0.05)
    assert adx == pytest.approx(26.0)
    assert "snapshot" in source


def test_a_legacy_score_payload_is_read_on_its_own_scale():
    """Archived payloads predating score_norm carry a legacy-scaled `score`. The
    canonical reader divides it; normalize_intelligence_score would have guessed
    the scale from magnitude (test_intel_score_unit_is_explicit bans that)."""
    from engine.intelligence_score import LEGACY_SCORE_SCALE

    sc = _sc({"MIDCPNIFTY_15min": {"aggregate": {"score": 60.0}, "regime": {"adx": 20.0}}})

    available, signed, _adx, _reason = sc._entry_intelligence_reading(_cand(), "15min")

    assert available is True
    assert signed == pytest.approx(60.0 / LEGACY_SCORE_SCALE)


@pytest.mark.parametrize("payload", [None, {}, "junk", {"aggregate": "bad"}])
def test_malformed_payloads_never_raise(payload):
    sc = _sc({"MIDCPNIFTY_15min": payload})

    result = sc._entry_intelligence_reading(_cand(), "15min")

    assert isinstance(result, tuple) and len(result) == 4


# ── the guard that consumed the reading ──────────────────────────────────────

def _guard_scanner(cache):
    sc = _sc(cache)
    sc._candidate_15m_trend_agrees = lambda _c: False
    sc._candle_thrust_metrics = lambda *_a, **_k: {"volume_ratio": 2.0}
    sc._candidate_volume_ratio_for_burst = lambda _c: 2.0
    sc._micro_timeframe_direction_confirmed = lambda *_a, **_k: (
        True, {"1min": {"volume_ratio": 2.0}, "5min": {"volume_ratio": 2.0}}
    )
    sc._set_scalp_lock = lambda *_a, **_k: None
    sc._tag_candidate_reason = lambda *_a, **_k: None
    return sc


def test_the_guard_says_unavailable_rather_than_not_aligned():
    sc = _guard_scanner({"NIFTY_5min": _intel()})

    ok, reason = sc._fifteen_min_choppy_scalp_context(_cand(), "RANGING")

    assert ok is False
    assert "unavailable" in reason
    assert "not aligned" not in reason


def test_the_guard_still_refuses_a_genuinely_opposed_book():
    sc = _guard_scanner({"MIDCPNIFTY_15min": _intel(score=-0.60)})

    ok, reason = sc._fifteen_min_choppy_scalp_context(_cand(), "RANGING")

    assert ok is False
    assert "not aligned" in reason


def test_the_guard_admits_an_aligned_book():
    sc = _guard_scanner({"MIDCPNIFTY_15min": _intel(score=0.60)})

    ok, _reason = sc._fifteen_min_choppy_scalp_context(_cand(), "RANGING")

    assert ok is True


@pytest.mark.parametrize(
    "flag",
    ["market_wide_reversal_entry", "market_ltp_shock_entry", "one_way_cascade_entry"],
)
def test_a_synchronised_multi_index_reversal_is_not_an_isolated_chop_scalp(flag):
    """These are fast-tracked through an 8s buffer BECAUSE the whole market turned,
    then were vetoed for lacking single-instrument intel alignment. Same class as
    the power_move / volume_spike exemptions already here."""
    sc = _guard_scanner({"NIFTY_5min": _intel()})

    ok, reason = sc._fifteen_min_choppy_scalp_context(_cand(**{flag: True}), "RANGING")

    assert ok is True, reason


def test_the_existing_exemptions_are_untouched():
    sc = _guard_scanner({"NIFTY_5min": _intel()})

    for flag in ("power_move", "volume_spike_entry", "late_session_reclaim_entry"):
        ok, _reason = sc._fifteen_min_choppy_scalp_context(_cand(**{flag: True}), "RANGING")
        assert ok is True, flag


def test_a_calm_regime_never_reaches_the_guard():
    sc = _guard_scanner({})

    ok, _reason = sc._fifteen_min_choppy_scalp_context(_cand(), "TRENDING_UP")

    assert ok is True
