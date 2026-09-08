"""Session mood adapts symmetrically after large positive or negative opens."""
from __future__ import annotations

import pandas as pd
import pytest

from intelligence.session_context import SessionContextAnalyzer


def _frame(index, closes, volumes):
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(open_price, close) + 0.08 for open_price, close in zip(opens, closes)],
            "low": [min(open_price, close) - 0.08 for open_price, close in zip(opens, closes)],
            "close": closes,
            "volume": volumes,
        },
        index=index,
    )


def _session_after_gap(gap_pct: float):
    previous_close = 100.0
    prior = _frame(
        pd.date_range("2026-09-04 14:00", periods=12, freq="5min"),
        [previous_close] * 12,
        [1000] * 12,
    )
    opened = previous_close * (1.0 + gap_pct / 100.0)
    opening_extension = 1.0 if gap_pct > 0 else -1.0
    first = [opened + opening_extension * index * 0.05 for index in range(18)]
    reversal_end = 99.7 if gap_pct > 0 else 100.3
    step = (reversal_end - first[-1]) / 42.0
    reversal = [first[-1] + (index + 1) * step for index in range(42)]
    closes = first + reversal
    current = _frame(
        pd.date_range("2026-09-05 09:15", periods=60, freq="5min"),
        closes,
        [2200] * 18 + [2600] * 42,
    )
    return SessionContextAnalyzer().analyze(
        pd.concat([prior, current]),
        instrument="NIFTY",
        spot_price=closes[-1],
        previous_close=previous_close,
    )


@pytest.mark.parametrize("gap_pct", [-0.4, -1.0, -2.0, -3.0])
def test_negative_open_followed_by_slow_recovery_flips_effective_phase_bullish(gap_pct):
    result = _session_after_gap(gap_pct)
    assert result["gap_pct"] == pytest.approx(gap_pct)
    assert result["effective_day_type"] == "TREND_DAY_UP"
    assert result["effective_bias"] == "BULLISH"
    assert result["phase_confidence"] >= 0.55


@pytest.mark.parametrize("gap_pct", [0.4, 1.0, 2.0, 3.0])
def test_positive_open_followed_by_slow_fade_flips_effective_phase_bearish(gap_pct):
    result = _session_after_gap(gap_pct)
    assert result["gap_pct"] == pytest.approx(gap_pct)
    assert result["effective_day_type"] == "TREND_DAY_DOWN"
    assert result["effective_bias"] == "BEARISH"
    assert result["phase_confidence"] >= 0.55
