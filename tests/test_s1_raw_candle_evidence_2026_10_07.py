"""S1 chart candles must not erase market-candle evidence consumed by grading."""

import pytest

from engine.s1_engine import S1Params, get_s1_signals
from engine.signal_manager import SignalManager
from tests.test_s1_engine_2026_09_23 import _frame


@pytest.mark.parametrize("kind", ["standard", "heikin_ashi"])
def test_s1_carries_raw_signal_bar_through_grading_without_changing_chart_prices(kind):
    frame = _frame(11)
    signals, _, _ = get_s1_signals(frame, "NIFTY", "5min", S1Params(candle_type=kind))
    assert signals
    manager = SignalManager()
    for signal in signals:
        row = frame.iloc[signal.bar_index]
        expected = {key: float(row[key]) for key in ("open", "high", "low", "close")}
        assert signal.raw_candle == expected
        assert signal.price == expected["close"]
        manager._grade_signal(signal, 0.0, 0.0, "RANGING")
        assert signal.grading_inputs["signal"]["raw_candle"] == expected
    if kind == "heikin_ashi":
        assert any(signal.s1_chart_signal_close != signal.raw_candle["close"] for signal in signals)
    before = signals[0].raw_candle.copy()
    frame.iloc[signals[0].bar_index, frame.columns.get_loc("close")] += 100
    assert signals[0].raw_candle == before
