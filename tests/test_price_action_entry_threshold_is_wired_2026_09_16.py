"""The entry price-action veto reads a setting, and that setting still cuts at -0.40.

`price_action_adverse_threshold` (default -0.50) is read only by the exit-side
loss-recovery checks in scanner.py. The ENTRY veto cut at a literal -0.40 written inside
`PriceActionAnalyzer.analyze`, so an operator changing the setting moved the exits and
not the entries. Wiring the two together at one number would have moved one of them: at
-0.50 the entry veto releases 145 of 564 recorded ADVERSE refusals (25.7%).

These tests pin that the entry boundary is now a real parameter, that its default leaves
every historical verdict where it was, and that the exit-side setting is untouched.
"""
import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from intelligence.price_action import PriceActionAnalyzer


def _rising_df(bars: int = 80) -> pd.DataFrame:
    """A steadily rising tape. A LONG chasing it scores clearly negative (-0.20):
    late Aroon trend caution plus buying the top of the range."""
    idx = pd.date_range("2026-09-15 09:15", periods=bars, freq="5min")
    base = np.linspace(100.0, 130.0, bars)
    wobble = np.sin(np.linspace(0, 9, bars)) * 0.4
    close = base + wobble
    return pd.DataFrame(
        {
            "open": close - 0.15,
            "high": close + 0.45,
            "low": close - 0.45,
            "close": close,
            "volume": np.full(bars, 1000.0),
        },
        index=idx,
    )


@pytest.fixture(scope="module")
def chase_the_top():
    result = PriceActionAnalyzer().analyze(_rising_df(), "BUY")
    assert result is not None, "fixture tape produced no price-action context"
    return result


def test_a_negative_score_is_produced_to_reason_about(chase_the_top):
    assert chase_the_top["score"] < 0.0


def test_the_boundary_that_decided_is_recorded(chase_the_top):
    assert chase_the_top["adverse_threshold"] == pytest.approx(-0.40)


def test_the_default_boundary_is_still_minus_zero_four(chase_the_top):
    """The hardcoded value this replaced. Any drift here re-grades history."""
    explicit = PriceActionAnalyzer().analyze(_rising_df(), "BUY", adverse_threshold=-0.40)
    assert explicit["verdict"] == chase_the_top["verdict"]
    assert explicit["score"] == pytest.approx(chase_the_top["score"])


def test_the_threshold_actually_moves_the_verdict():
    """Proves the parameter is live rather than decorative."""
    df = _rising_df()
    strict = PriceActionAnalyzer().analyze(df, "BUY", adverse_threshold=-0.01)
    permissive = PriceActionAnalyzer().analyze(df, "BUY", adverse_threshold=-0.99)

    assert strict["score"] == pytest.approx(permissive["score"])
    assert strict["verdict"] == "ADVERSE"
    assert permissive["verdict"] != "ADVERSE"
    assert strict["adverse_threshold"] == pytest.approx(-0.01)
    assert permissive["adverse_threshold"] == pytest.approx(-0.99)


def test_a_non_negative_score_is_never_adverse_however_strict_the_boundary():
    """The threshold may only tighten the losing side; it cannot condemn a flat read."""
    df = _rising_df()
    for threshold in (-0.01, -0.40, -0.99):
        result = PriceActionAnalyzer().analyze(df, "SELL", adverse_threshold=threshold)
        assert result["score"] >= 0.0
        assert result["verdict"] != "ADVERSE"


def test_entry_and_exit_thresholds_are_separate_settings():
    """One number for both would move the entry veto or the exit checks."""
    settings = Settings()
    assert settings.price_action_entry_adverse_threshold == pytest.approx(-0.40)
    assert settings.price_action_adverse_threshold == pytest.approx(-0.50)


def test_entry_context_passes_the_setting_through(monkeypatch):
    """The call site must hand the setting down, not fall back to the default."""
    from engine import signal_processor

    seen = {}
    real_analyze = PriceActionAnalyzer.analyze

    def spy(self, df, signal_type, **kwargs):
        seen.update(kwargs)
        return real_analyze(self, df, signal_type, **kwargs)

    monkeypatch.setattr(PriceActionAnalyzer, "analyze", spy)

    settings = Settings()
    settings.price_action_module_enabled = True
    settings.price_action_entry_adverse_threshold = -0.33

    class _Sig:
        signal_type = "BUY"

    class _Candles:
        def get_candles(self, instrument, timeframe):
            return _rising_df()

    class _Scanner:
        candles = _Candles()

    context = signal_processor.price_action_context(
        _Scanner(), "NIFTY", "5min", _Sig(), settings
    )

    assert context is not None, "price_action_context returned nothing to assert on"
    assert seen.get("adverse_threshold") == pytest.approx(-0.33)
    assert context["adverse_threshold"] == pytest.approx(-0.33)
