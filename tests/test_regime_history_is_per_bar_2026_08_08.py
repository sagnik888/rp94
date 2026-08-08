"""Regime smoothing and strong-ADX hysteresis were complete no-ops in live trading.

`detect()` appended one entry to `_regime_history[key]` on every CALL, then majority-voted
the last `regime_smoothing_window` (5) entries. Both `_smooth_regime` and
`_recent_trend_regime` read that same list, and nothing deduped by bar identity.

Live, detect does not run per bar — it runs on the intelligence TTL. That is 30 seconds
against a 1-second scan loop, so roughly 10 calls per 5-minute bar and 30 per 15-minute
bar. A 5-entry "5 bar" window therefore spanned ~150 seconds: half of ONE 5-minute bar.
The majority vote was smoothing a bar against itself, and the hysteresis had no prior bar
to hold against. Both were no-ops.

That matters because the label they exist to stabilise is the one every money decision
reads: reversal exits, the trending-vs-tight trailing branch, the futures veto, and
grading. A single-bar flip that smoothing was installed to absorb reached those gates raw.

History is now keyed on bar identity — the same `str(df.index[-1])` the promotion logger
already uses. Re-evaluating the same bar REVISES its vote rather than casting another.
"""
import pandas as pd
import pytest

from intelligence.regime_detector import RegimeDetector


def _frame(last="2026-08-05 10:15", bars=80, trend=1.0, bar=0):
    """`bar` advances the LAST timestamp — that is what identifies a bar.

    The first draft varied `bars` instead, which only moved the START of the window and
    left every frame ending on the same timestamp. The fix correctly deduped them and the
    test read that as a failure. Bar identity is the last index, not the length.
    """
    end = pd.Timestamp(last) + pd.Timedelta(minutes=5 * bar)
    idx = pd.date_range(end=end, periods=bars, freq="5min")
    close = pd.Series(range(bars), dtype=float) * trend + 24000.0
    return pd.DataFrame(
        {
            "open": close.values, "high": close.values + 5.0,
            "low": close.values - 5.0, "close": close.values,
            "volume": [1000.0] * bars,
        },
        index=idx,
    )


KEY = "NIFTY_5min"


def _history(det):
    return det._regime_history.get(KEY, [])


# ── one entry per bar ────────────────────────────────────────────────────────

def test_re_evaluating_the_same_bar_does_not_add_a_vote():
    """The live shape: ~10 detect calls land on one 5-minute bar."""
    det = RegimeDetector()
    frame = _frame()
    for _ in range(10):
        det.detect(frame, "NIFTY", "5min")
    assert len(_history(det)) == 1, (
        f"10 calls on one bar produced {len(_history(det))} votes — the smoothing window "
        f"is measured in calls, not bars"
    )


def test_each_new_bar_adds_exactly_one_vote():
    det = RegimeDetector()
    for i in range(6):
        det.detect(_frame(bar=i), "NIFTY", "5min")
    assert len(_history(det)) == 6


def test_a_revised_opinion_on_the_same_bar_replaces_it():
    """Intra-bar the read can change; the LAST word for that bar is what counts.

    The first draft asserted `a != b or b == a`, which is a tautology and proved
    nothing. This compares against what a FRESH detector says for the same frame.
    """
    up, down = _frame(trend=1.0), _frame(trend=-1.0)
    expected = RegimeDetector()
    expected.detect(down, "NIFTY", "5min")
    down_regime = expected._regime_history[KEY][-1]

    det = RegimeDetector()
    det.detect(up, "NIFTY", "5min")
    up_regime = _history(det)[-1]
    det.detect(down, "NIFTY", "5min")

    assert len(_history(det)) == 1, "the revision was appended as a second vote"
    assert _history(det)[-1] == down_regime, "the bar kept its stale first opinion"
    if up_regime != down_regime:
        assert _history(det)[-1] != up_regime


def test_the_window_now_spans_real_bars():
    """Five entries must represent five distinct bars, not 150 seconds."""
    det = RegimeDetector()
    for i in range(5):
        det.detect(_frame(bar=i), "NIFTY", "5min")
    for _ in range(20):
        det.detect(_frame(bar=4), "NIFTY", "5min")
    assert len(_history(det)) == 5


def test_history_is_still_capped():
    det = RegimeDetector()
    for i in range(60):
        det.detect(_frame(bar=i), "NIFTY", "5min")
    assert len(_history(det)) <= 50


def test_separate_timeframes_keep_separate_histories():
    det = RegimeDetector()
    det.detect(_frame(), "NIFTY", "5min")
    det.detect(_frame(), "NIFTY", "15min")
    assert len(det._regime_history.get("NIFTY_5min", [])) == 1
    assert len(det._regime_history.get("NIFTY_15min", [])) == 1


# ── the warmup replay must still build real history ──────────────────────────

def test_replaying_distinct_bars_still_warms_the_smoother():
    """_detect_regime_warmed feeds progressively longer frames; each is a new bar."""
    det = RegimeDetector()
    full = _frame(bars=90)
    for offset in range(4, -1, -1):
        det.detect(full.iloc[: len(full) - offset], "NIFTY", "5min")
    assert len(_history(det)) == 5, "the warmup replay no longer builds smoothing history"


# ── the reset paths keep both structures in step ─────────────────────────────

def test_a_full_reset_clears_the_bar_markers_too():
    det = RegimeDetector()
    det.detect(_frame(), "NIFTY", "5min")
    det.reset_history()
    assert det._regime_history_bar == {}
    det.detect(_frame(), "NIFTY", "5min")
    assert len(_history(det)) == 1, "a stale bar marker survived the reset"


def test_a_scoped_reset_clears_only_that_instrument():
    det = RegimeDetector()
    det.detect(_frame(), "NIFTY", "5min")
    det.detect(_frame(), "BANKNIFTY", "5min")
    det.reset_history("NIFTY")
    assert "NIFTY_5min" not in det._regime_history_bar
    assert "BANKNIFTY_5min" in det._regime_history_bar


def test_an_unusable_index_does_not_raise():
    det = RegimeDetector()
    frame = _frame()
    frame.index = range(len(frame))  # not a DatetimeIndex
    det.detect(frame, "NIFTY", "5min")
