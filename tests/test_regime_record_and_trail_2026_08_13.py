"""Two regime defects: a fabricated measurement, and two guards fighting.

1. rtracker recorded regime confidence 0.0 when it had none.

    "hard_label": str(regime_ctx.get("regime") or candidate.market_regime or "")
    "confidence": _safe_float(regime_ctx.get("confidence", 0.0))

The label falls back to the candidate; the confidence does not. So a row whose
regime context is absent records the RIGHT label beside a confidence of zero that
nothing measured. On 2026-08-12 the four worst trades (-Rs.1,298) read
`TRENDING_UP, adx 47-54, confidence 0.0` — but RegimeDetector cannot emit 0: its
floor is 40 and a trending label computes min(95, adx+30), i.e. 77-84 for those
readings. The zero was invented at the recording layer.

That matters more than a cosmetic: every threshold in this system is argued from
this archive, and an analysis of it will read "traded on zero confidence" where
the truth is "confidence was never written down". Absent is not zero — the same
rule the entry-intel snapshot already follows for VIX.

2. VOLATILE was handed the tightest trail and the loosest floor at once.

_first_rung_regime_retention carries a measured table over 47 sessions:

    TRENDING_UP 8%  RANGING 35%  BREAKOUT_DOWN 43%  BREAKOUT_UP 47%  VOLATILE 49%

and loosens the profit-lock floor to 0.38 for the three highest-heat regimes,
VOLATILE among them, because "the MEDIAN winner gives back 49%". Meanwhile
uses_tight_trailing put VOLATILE on a stop pinned to the PREVIOUS 1-MINUTE
CANDLE's extreme. The 1-minute trail binds first every time, so the measured
loosening never reached the regime it was measured for. The tight lane keeps
RANGING (35%) and UNKNOWN, where it is defensible.
"""
import json
from types import SimpleNamespace

import pytest

from engine.market_regime import is_risky_regime, uses_tight_trailing
from intelligence.rtracker import RegimeTracker


# ── 1. an unmeasured regime metric is not a zero ─────────────────────────────

def _record(tmp_path, candidate):
    rt = RegimeTracker(root_dir=tmp_path, enabled=True)
    rt.record_candidate(candidate, decision="REJECTED", reason="x")
    rows = []
    for path in (tmp_path / "events").glob("*.jsonl"):
        rows.extend(json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
    return rows[0]["regime"]


def _cand(**kw):
    base = dict(
        instrument="NIFTY", timeframe="5min", direction="LONG",
        signal_timestamp="2026-08-12T13:25:00", market_regime="TRENDING_UP",
        adx_value=48.1, confidence=0.76, grade="A",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_an_unmeasured_confidence_is_absent_not_zero(tmp_path):
    """The exact 2026-08-12 shape: real label, no regime context."""
    regime = _record(tmp_path, _cand())

    assert regime["hard_label"] == "TRENDING_UP"
    assert regime.get("confidence") is None, "0.0 would read as a measurement"


def test_a_measured_confidence_is_recorded(tmp_path):
    cand = _cand()
    cand.market_intelligence_context = {"regime": {"regime": "TRENDING_UP", "confidence": 78.0}}

    regime = _record(tmp_path, cand)

    assert regime["confidence"] == pytest.approx(78.0)


def test_a_measured_zero_is_still_recorded(tmp_path):
    """A real 0.0 must survive; only the UNMEASURED case becomes absent."""
    cand = _cand()
    cand.market_intelligence_context = {"regime": {"regime": "RANGING", "confidence": 0.0}}

    regime = _record(tmp_path, cand)

    assert regime["confidence"] == pytest.approx(0.0)


@pytest.mark.parametrize("field", ["kaufman_er", "atr_pct", "bb_width"])
def test_the_other_unmeasured_regime_metrics_are_absent_too(tmp_path, field):
    regime = _record(tmp_path, _cand())

    assert regime.get(field) is None


def test_adx_keeps_its_candidate_fallback(tmp_path):
    """adx already falls back to the candidate; that stays."""
    regime = _record(tmp_path, _cand())

    assert regime["adx"] == pytest.approx(48.1)


# ── 2. the highest-heat regime stops getting the tightest trail ──────────────

def test_volatile_no_longer_takes_the_one_minute_trail():
    """49% measured heat is the loosest-needing regime, not the tightest."""
    assert uses_tight_trailing("VOLATILE") is False


@pytest.mark.parametrize("regime", ["RANGING", "UNKNOWN"])
def test_the_tight_lane_keeps_the_regimes_it_was_built_for(regime):
    assert uses_tight_trailing(regime) is True


@pytest.mark.parametrize("regime", ["TRENDING_UP", "TRENDING_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN"])
def test_trending_and_breakout_are_still_loose(regime):
    assert uses_tight_trailing(regime) is False


def test_volatile_is_still_treated_as_risky_everywhere_else():
    """Only the TRAIL changes. VOLATILE keeps its risky-regime entry treatment."""
    assert is_risky_regime("VOLATILE") is True


def test_the_trail_lane_now_agrees_with_the_measured_heat_table():
    """The three highest-heat regimes all get room; the mid and unknown do not."""
    loosened_by_retention = {"VOLATILE", "BREAKOUT_UP", "BREAKOUT_DOWN"}

    for regime in loosened_by_retention:
        assert uses_tight_trailing(regime) is False, (
            f"{regime} is loosened by _first_rung_regime_retention but pinned to the "
            "1-minute candle by uses_tight_trailing"
        )


# ── the same fabricated zero, in three more blocks ───────────────────────────
# Measured live on 2026-08-13 at the same moment, for the same quantity:
#
#   volume profiler (log):        NIFTY 0.108x  BANKNIFTY 0.198x  SENSEX 0.043x
#   rtracker volume.volume_ratio: 0.0   on all 158 rows that session
#   decision_intel.volume_ratio:  1.0
#
# Volume was being measured the whole time. The archive recorded neither the
# value nor its absence, and 0.0 is a legitimate reading for buy/sell ratio and
# aggregate score — so nothing downstream can tell a real zero from a missing one.
# This is the block the next 20 sessions of tuning data lands in.

def test_an_unmeasured_volume_ratio_is_absent_not_zero(tmp_path):
    regime_free = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="LONG",
        signal_timestamp="2026-08-13T13:00:00",
    )
    rt = RegimeTracker(root_dir=tmp_path, enabled=True)
    rt.record_candidate(regime_free, decision="REJECTED", reason="x")
    row = json.loads(next((tmp_path / "events").glob("*.jsonl")).read_text(encoding="utf-8").strip())

    assert row["volume"].get("volume_ratio") is None
    assert row["volume"].get("buy_sell_ratio") is None
    assert row["volume"].get("current_volume") is None


def test_a_measured_volume_ratio_survives(tmp_path):
    cand = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="LONG",
        signal_timestamp="2026-08-13T13:00:00",
        market_intelligence_context={"volume": {"volume_ratio": 0.108, "buy_sell_ratio": 1.23}},
    )
    rt = RegimeTracker(root_dir=tmp_path, enabled=True)
    rt.record_candidate(cand, decision="REJECTED", reason="x")
    row = json.loads(next((tmp_path / "events").glob("*.jsonl")).read_text(encoding="utf-8").strip())

    assert row["volume"]["volume_ratio"] == pytest.approx(0.108)
    assert row["volume"]["buy_sell_ratio"] == pytest.approx(1.23)


def test_a_genuinely_zero_reading_still_records(tmp_path):
    """0.0 is a real buy/sell ratio. Only the UNMEASURED case becomes absent."""
    cand = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="LONG",
        signal_timestamp="2026-08-13T13:00:00",
        market_intelligence_context={"volume": {"buy_sell_ratio": 0.0}},
    )
    rt = RegimeTracker(root_dir=tmp_path, enabled=True)
    rt.record_candidate(cand, decision="REJECTED", reason="x")
    row = json.loads(next((tmp_path / "events").glob("*.jsonl")).read_text(encoding="utf-8").strip())

    assert row["volume"]["buy_sell_ratio"] == pytest.approx(0.0)


def test_an_unmeasured_aggregate_and_breadth_are_absent(tmp_path):
    cand = SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="LONG",
        signal_timestamp="2026-08-13T13:00:00",
    )
    rt = RegimeTracker(root_dir=tmp_path, enabled=True)
    rt.record_candidate(cand, decision="REJECTED", reason="x")
    row = json.loads(next((tmp_path / "events").glob("*.jsonl")).read_text(encoding="utf-8").strip())

    assert row["aggregate"].get("score_norm") is None
    assert row["breadth"].get("aligned_count") is None
