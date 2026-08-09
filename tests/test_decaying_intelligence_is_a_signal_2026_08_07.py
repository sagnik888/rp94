"""The stored intelligence history was write-only.

SnapshotStore records a snapshot per scan for the whole session — PCR, OI, greeks, order
flow — and the ONLY reader anywhere is a chart overlay in scanner_chart_mixin. No live
decision ever asked it anything.

Every pillar the loss guard reads is a point-in-time value. None of them says whether the
case for a trade has been strengthening or falling apart over the last quarter hour. A
trade whose intelligence has been sliding steadily is deteriorating even while price sits
still — and that is exactly the trade worth cutting before the stop does it.

"Cannot say" must never read as "deteriorating". Too few snapshots, an empty store, no
timestamps: all return None and add nothing, or every trade in the opening minutes would
be penalised for the store not being full yet.

DEFAULT OFF: it makes exits stricter.
"""
import time
from types import SimpleNamespace

import pytest

from intelligence.snapshot_store import MarketIntelSnapshotStore as SnapshotStore
from scanner import Scanner


def _store(scores, step=60.0):
    """A store holding `scores` at one-minute spacing, oldest first."""
    s = SnapshotStore()
    base = time.time() - step * len(scores)
    for i, score in enumerate(scores):
        s._raw["NIFTY"].append(
            {"instrument": "NIFTY", "snapshot_ts": int(base + i * step), "intel_score": score}
        )
    return s


# ── reading the direction ────────────────────────────────────────────────────

def test_a_decaying_score_reports_a_negative_slope():
    slope = _store([70.0, 65.0, 60.0, 55.0, 50.0]).score_trend("NIFTY")
    assert slope is not None and slope < 0


def test_an_improving_score_reports_a_positive_slope():
    assert _store([50.0, 55.0, 60.0, 65.0, 70.0]).score_trend("NIFTY") > 0


def test_a_flat_score_reports_no_movement():
    assert _store([60.0] * 5).score_trend("NIFTY") == pytest.approx(0.0)


def test_the_slope_is_per_minute():
    """70 to 50 over four minutes is -5 a minute."""
    assert _store([70.0, 65.0, 60.0, 55.0, 50.0]).score_trend("NIFTY") == pytest.approx(-5.0)


def test_too_few_points_is_unknown_not_flat():
    assert _store([70.0, 50.0]).score_trend("NIFTY", min_points=4) is None


def test_an_empty_store_is_unknown():
    assert SnapshotStore().score_trend("NIFTY") is None


def test_an_unknown_instrument_is_unknown():
    assert _store([70.0, 65.0, 60.0, 55.0]).score_trend("BANKNIFTY") is None


def test_points_outside_the_window_are_ignored():
    """An hour-old collapse is not what is happening now."""
    s = _store([10.0, 12.0, 60.0, 61.0, 62.0, 63.0], step=600.0)
    assert s.score_trend("NIFTY", lookback_seconds=1800.0) >= 0


def test_zero_scores_are_not_treated_as_a_collapse():
    """A missing reading records as 0 and must not look like the score fell to nothing."""
    s = _store([60.0, 0.0, 0.0, 61.0, 62.0, 63.0])
    assert s.score_trend("NIFTY") > 0


# ── the loss guard consumes it ───────────────────────────────────────────────

def _scanner(store):
    s = Scanner.__new__(Scanner)
    s.intel_snapshots = store
    return s


def _settings(**kw):
    base = dict(
        loss_guard_intel_decay_enabled=True,
        loss_guard_intel_decay_window_seconds=900.0,
        loss_guard_intel_decay_min_slope=-1.0,
        loss_guard_intel_decay_severity=1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_decay_adds_severity():
    reasons = []
    got = _scanner(_store([70.0, 65.0, 60.0, 55.0, 50.0]))._decaying_intel_points(
        "NIFTY", reasons, _settings()
    )
    assert got == 1
    assert reasons and "decaying" in reasons[0]


def test_it_is_off_by_default():
    reasons = []
    got = _scanner(_store([70.0, 65.0, 60.0, 55.0, 50.0]))._decaying_intel_points(
        "NIFTY", reasons, _settings(loss_guard_intel_decay_enabled=False)
    )
    assert got == 0 and reasons == []


def test_improving_intelligence_adds_nothing():
    reasons = []
    assert _scanner(_store([50.0, 55.0, 60.0, 65.0, 70.0]))._decaying_intel_points(
        "NIFTY", reasons, _settings()
    ) == 0


def test_a_gentle_slide_below_the_threshold_is_ignored():
    """-0.25/min over 15 minutes is noise, not deterioration."""
    reasons = []
    assert _scanner(_store([61.0, 60.75, 60.5, 60.25, 60.0]))._decaying_intel_points(
        "NIFTY", reasons, _settings(loss_guard_intel_decay_min_slope=-1.0)
    ) == 0


def test_an_empty_store_adds_nothing():
    """The opening minutes of a session must not penalise every trade."""
    reasons = []
    assert _scanner(SnapshotStore())._decaying_intel_points("NIFTY", reasons, _settings()) == 0


def test_a_store_without_the_accessor_is_tolerated():
    reasons = []
    assert _scanner(SimpleNamespace())._decaying_intel_points("NIFTY", reasons, _settings()) == 0


def test_a_raising_store_never_breaks_the_loss_guard():
    class _Boom:
        def score_trend(self, *a, **k):
            raise RuntimeError("history unavailable")

    reasons = []
    assert _scanner(_Boom())._decaying_intel_points("NIFTY", reasons, _settings()) == 0


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_loss_guard_actually_calls_it():
    import inspect

    src = inspect.getsource(Scanner._loss_guard_severity)
    assert "_decaying_intel_points" in src, (
        "the decay signal is computed by nothing — the same write-only pattern this "
        "requirement exists to fix"
    )
    assert "price_points +=" in src


def test_the_snapshot_records_the_score_it_needs():
    import inspect

    from intelligence.snapshot_store import MarketIntelSnapshotStore as S

    assert '"intel_score"' in inspect.getsource(S._build_snapshot), (
        "snapshots no longer carry the aggregate score, so no trend can be computed"
    )


# ── the writer, 2026-08-09 ───────────────────────────────────────────────────
# Every test above pokes `_raw` directly, which is why a broken WRITER stayed
# invisible: push_snapshot read intel_result["score"], and the aggregator publishes
# the score inside results["aggregate"] -- there is no top-level "score" key at all.
# So every stored snapshot held 0.0, score_trend never assembled two points, and the
# decay guard answered "cannot say" for the life of the process.


def _agg(score):
    """An aggregator-shaped payload: the score lives under "aggregate"."""
    return {"aggregate": {"score": score, "score_norm": score / 100.0}, "regime": {}}


def test_push_snapshot_actually_records_the_aggregate_score():
    s = SnapshotStore()
    s.push_snapshot("NIFTY", None, intel_result=_agg(62.5), timestamp=time.time())
    assert s._raw["NIFTY"][-1]["intel_score"] == pytest.approx(62.5), (
        "the writer is reading a key the aggregator never publishes"
    )


def test_a_top_level_score_key_is_not_what_the_aggregator_publishes():
    """The premise. If this ever becomes true, the old read would have been fine."""
    import inspect

    from intelligence.intelligence_aggregator import IntelligenceAggregator

    src = inspect.getsource(IntelligenceAggregator.analyze)
    assert 'results["score"]' not in src and "results['score']" not in src


def test_a_slope_is_measurable_end_to_end_through_push_snapshot():
    s = SnapshotStore()
    base = time.time() - 300.0
    for i, sc in enumerate([70.0, 65.0, 60.0, 55.0, 50.0]):
        s.push_snapshot("NIFTY", None, intel_result=_agg(sc), timestamp=base + i * 60.0)
    assert s.score_trend("NIFTY") == pytest.approx(-5.0)


# ── bidirectional: a decaying BEARISH case must be visible too ───────────────

def test_a_decaying_bearish_case_is_not_discarded():
    """`score > 0` dropped every bearish row, so a bought PE whose case was
    evaporating produced no slope at all."""
    slope = _store([-70.0, -65.0, -60.0, -55.0, -50.0]).score_trend("NIFTY")
    assert slope == pytest.approx(5.0), (
        "a bearish reading is still filtered out, so PE trades get no decay signal"
    )


def test_a_strengthening_bearish_case_reports_a_negative_slope():
    assert _store([-50.0, -55.0, -60.0, -65.0, -70.0]).score_trend("NIFTY") == pytest.approx(-5.0)


def test_a_neutral_zero_reading_is_a_reading_not_a_gap():
    assert _store([0.0, -2.0, -4.0, -6.0]).score_trend("NIFTY") == pytest.approx(-2.0)


def test_a_sign_flip_is_tracked_across_zero():
    """Bullish conviction collapsing through neutral into bearish."""
    assert _store([20.0, 10.0, 0.0, -10.0, -20.0]).score_trend("NIFTY") == pytest.approx(-10.0)
