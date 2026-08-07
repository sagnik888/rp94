"""A logger named "once" that fired 1,050 times in one session.

RegimeDetector._log_er_promotion_once dedupes on (key, bar, regime) held in a
per-INSTANCE set. But Scanner._detect_regime_warmed constructs a throwaway detector on
every single call, by design — it needs empty smoothing history to stay replay
deterministic. So the dedup set was empty on every call and the guard never held.

Measured in logs/ut1.log for the 2026-08-07 session: 1,050 promotion lines, of which a
single BANKNIFTY_60min -> TRENDING_DOWN repeated 314 times and MIDCPNIFTY_60min ->
TRENDING_UP 301 times. 8.5% of the whole log was this one message.

Cosmetic in isolation, but this is the log used to diagnose live incidents, and it is
the file that carried the volume evidence in this same session.
"""
import pandas as pd
import pytest
from loguru import logger

from engine.market_regime import MarketRegime
from intelligence.regime_detector import RegimeDetector


@pytest.fixture
def captured():
    lines = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    RegimeDetector._ER_PROMOTION_LOG_KEYS.clear()
    yield lines
    logger.remove(sink)
    RegimeDetector._ER_PROMOTION_LOG_KEYS.clear()


def _frame(last="2026-08-07 10:15"):
    idx = pd.date_range(end=pd.Timestamp(last), periods=5, freq="1min")
    return pd.DataFrame({"close": [1.0] * 5}, index=idx)


def _promote(detector, frame, key="BANKNIFTY_60min", regime=MarketRegime.TRENDING_DOWN):
    detector._log_er_promotion_once(
        key=key, df=frame, regime=regime, adx_val=19.8, er_value=0.70,
        reason="Kaufman ER promotion",
    )


def _promotions(lines):
    return [ln for ln in lines if "Regime ER promotion" in ln]


def test_one_detector_logs_a_repeated_promotion_once(captured):
    d, frame = RegimeDetector(), _frame()
    for _ in range(10):
        _promote(d, frame)
    assert len(_promotions(captured)) == 1


def test_throwaway_detectors_still_log_only_once(captured):
    """The actual live shape: a NEW detector per call, as _detect_regime_warmed does."""
    frame = _frame()
    for _ in range(20):
        _promote(RegimeDetector(), frame)
    assert len(_promotions(captured)) == 1, (
        "a fresh detector per call resets the dedup set — this is the 314x repeat"
    )


def test_a_new_bar_is_reported_again(captured):
    """Dedup must not silence a genuinely new promotion."""
    for stamp in ("2026-08-07 10:15", "2026-08-07 11:15", "2026-08-07 12:15"):
        _promote(RegimeDetector(), _frame(stamp))
    assert len(_promotions(captured)) == 3


def test_a_different_regime_on_the_same_bar_is_reported(captured):
    frame = _frame()
    _promote(RegimeDetector(), frame, regime=MarketRegime.TRENDING_DOWN)
    _promote(RegimeDetector(), frame, regime=MarketRegime.TRENDING_UP)
    assert len(_promotions(captured)) == 2


def test_a_different_instrument_is_reported(captured):
    frame = _frame()
    _promote(RegimeDetector(), frame, key="BANKNIFTY_60min")
    _promote(RegimeDetector(), frame, key="NIFTY_60min")
    assert len(_promotions(captured)) == 2


# ── the reset paths still work ───────────────────────────────────────────────

def test_a_full_reset_allows_the_line_again(captured):
    """Replays must start from empty memory."""
    d, frame = RegimeDetector(), _frame()
    _promote(d, frame)
    d.reset_history()
    _promote(d, frame)
    assert len(_promotions(captured)) == 2


def test_a_scoped_reset_only_clears_that_instrument(captured):
    frame = _frame()
    d = RegimeDetector()
    _promote(d, frame, key="BANKNIFTY_60min")
    _promote(d, frame, key="NIFTY_60min")
    d.reset_history("BANKNIFTY")
    _promote(d, frame, key="BANKNIFTY_60min")   # cleared -> logs again
    _promote(d, frame, key="NIFTY_60min")       # retained -> stays quiet
    assert len(_promotions(captured)) == 3


def test_the_cap_does_not_evict_at_random(captured):
    """A set has no order, so keeping "the last 1024" kept an arbitrary 1024."""
    d = RegimeDetector()
    for i in range(RegimeDetector._ER_PROMOTION_LOG_CAP + 50):
        _promote(d, _frame(), key=f"IDX{i}_60min")
    assert len(RegimeDetector._ER_PROMOTION_LOG_KEYS) <= RegimeDetector._ER_PROMOTION_LOG_CAP


def test_the_cache_is_shared_not_per_instance():
    """The property the fix turns on."""
    assert "_ER_PROMOTION_LOG_KEYS" in vars(RegimeDetector)
    assert RegimeDetector().__dict__.get("_ER_PROMOTION_LOG_KEYS") is None, (
        "an instance attribute would shadow the shared cache and restore the bug"
    )


def test_a_broken_frame_never_raises(captured):
    _promote(RegimeDetector(), None)
    _promote(RegimeDetector(), pd.DataFrame())
