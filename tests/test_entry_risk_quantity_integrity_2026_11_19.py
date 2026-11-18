"""Pure risk-policy regressions; no live broker access or order submission."""
import pytest

from engine.entry_risk_policy import evaluate_entry_risk, projected_stop_risk_rs


@pytest.mark.parametrize("candidate", [
    {"inst_type": "OPT", "price": 100, "stop": 90, "lot_size": 65},
    {"inst_type": "OPT", "price": 100, "stop": 90, "lots": 0, "lot_size": 65},
    {"inst_type": "OPT", "price": 100, "stop": 90, "lots": -2, "lot_size": 65},
    {"inst_type": "OPT", "price": 100, "stop": 90, "lots": 1, "lot_size": 0},
    {"inst_type": "OPT", "price": 100, "stop": 90, "lots": 1, "lot_size": 65.5},
    {"inst_type": "OPT", "price": 100, "stop": 90, "lots": float("nan"), "lot_size": 65},
    {"inst_type": "OPT", "price": 100, "stop": 90, "lots": float("inf"), "lot_size": 65},
    {"inst_type": "OPT", "price": float("inf"), "stop": 90, "lots": 1, "lot_size": 65},
])
def test_invalid_risk_quantities_and_prices_are_unmeasurable(candidate):
    assert projected_stop_risk_rs(candidate) is None


def test_valid_option_two_lots_retains_exact_risk():
    item = {"inst_type": "OPT", "price": 100, "stop": 90,
            "lots": 2, "lot_size": 65}
    assert projected_stop_risk_rs(item) == pytest.approx(1300)


def test_restored_futures_position_uses_actual_multiplier():
    item = {"inst_type": "FUT", "direction": "LONG", "price": 24000,
            "stop": 23900, "lots": 1, "lot_size": 65,
            "instrument_multiplier": 2.0}
    assert projected_stop_risk_rs(item) == pytest.approx(13000)


def test_explicit_invalid_futures_multiplier_fails_closed():
    item = {"inst_type": "FUT", "direction": "LONG", "price": 24000,
            "stop": 23900, "lots": 1, "lot_size": 65, "multiplier": 0}
    assert projected_stop_risk_rs(item) is None


@pytest.mark.parametrize("slippage", [float("nan"), float("inf"), -1.0, "bad"])
def test_invalid_slippage_is_unmeasurable(slippage):
    item = {"inst_type": "OPT", "price": 100, "stop": 90,
            "lots": 1, "lot_size": 65}
    assert projected_stop_risk_rs(item, slippage_bps=slippage) is None


def test_unmeasurable_open_risk_refuses_new_entry():
    valid = {"inst_type": "OPT", "price": 100, "stop": 90,
             "lots": 1, "lot_size": 65}
    damaged = dict(valid, lot_size=0)
    decision = evaluate_entry_risk(valid, [damaged], fut_limit_rs=50000,
                                   opt_limit_rs=50000, global_limit_rs=100000)
    assert not decision.allowed
    assert "open position risk" in decision.reason


def test_unmeasurable_candidate_refuses_new_entry():
    invalid = {"inst_type": "OPT", "price": 100, "stop": 90,
               "lots": 0, "lot_size": 65}
    decision = evaluate_entry_risk(invalid, [], fut_limit_rs=50000,
                                   opt_limit_rs=50000, global_limit_rs=100000)
    assert not decision.allowed
    assert "candidate stop risk unavailable" in decision.reason
