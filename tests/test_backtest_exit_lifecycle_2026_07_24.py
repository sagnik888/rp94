"""Self-tests for the tick-lifecycle exit backtester (scripts/backtest_exit_lifecycle.py).

Validate the exit simulators on synthetic premium paths so the tool that gates
money decisions is itself trustworthy.
"""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from scanner import Scanner

_SPEC = importlib.util.spec_from_file_location(
    "bt_exit", Path(__file__).resolve().parents[1] / "scripts" / "backtest_exit_lifecycle.py"
)
bt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bt)


def _bars(seq):
    # seq: list of (high, low). close = low for simplicity.
    return pd.DataFrame(
        [{"timestamp": pd.Timestamp(2026, 7, 24, 9, 20) + pd.Timedelta(minutes=i),
          "open": h, "high": h, "low": lo, "close": lo, "volume": 1}
         for i, (h, lo) in enumerate(seq)]
    )


def _trade(entry=100.0, stop=90.0, target=115.0, regime="TRENDING_UP", adx=30.0):
    return {"entry": entry, "stop": stop, "target": target, "raw_regime": regime,
            "adx": adx, "direction": "LONG", "option_type": "CE"}


def test_straight_to_stop_both_exit_at_stop():
    bars = _bars([(101, 99), (95, 90), (92, 85)])  # drifts down into stop
    assert bt.sim_old(_trade(), bars, 65)[1] == "STOP"
    assert bt.sim_runner(_trade(), bars, Scanner.__new__(Scanner), 65)[1] == "STOP"


def test_big_move_runner_rides_past_giveback():
    # Clean strong up-move well past 1R (risk=10): peak 100->140 then fades to 128.
    bars = _bars([(105, 104), (120, 118), (140, 138), (132, 128)])
    px, why = bt.sim_runner(_trade(), bars, Scanner.__new__(Scanner), 65)
    assert why == "RUNNER_LOCK"
    # locks ~90% of the 40-pt peak gain -> exit well above entry
    assert px > 130


def test_below_0p4r_crossover_does_not_ride():
    # Peak +3 (0.3R < the 0.4R crossover) then reverses to the stop: the runner must NOT
    # engage — below the crossover the trade keeps the current guards, same as OLD.
    # entry 100 / stop 90 / risk 10.
    path = [(103, 102), (100, 99), (95, 94), (91, 88)]
    _, old_why = bt.sim_old(_trade(), _bars(path), 65)
    _, run_why = bt.sim_runner(_trade(), _bars(path), Scanner.__new__(Scanner), 65)
    assert run_why != "RUNNER_LOCK"
    assert run_why == old_why


def test_at_crossover_in_trend_hands_off_to_runner():
    # Peak +6 (0.6R: above the 0.4R crossover) in a strong trend then fades: the runner
    # engages and the 85-90% lock governs (protects more than the 50% give-back).
    path = [(104, 103), (106, 105), (103, 101), (99, 97)]
    _, run_why = bt.sim_runner(_trade(), _bars(path), Scanner.__new__(Scanner), 65)
    assert run_why == "RUNNER_LOCK"


def test_matching_and_type_helpers():
    assert bt._instrument_of("BANKNIFTY28JUL2657500CE_1min.csv") == "BANKNIFTY"
    assert bt._type_of("BANKNIFTY28JUL2657500CE_1min.csv") == "CE"
    assert bt._type_of("NIFTY31JUL2624000PE_1min.csv") == "PE"
