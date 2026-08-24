"""RTracker outcome units are explicit and legacy paths replay on one basis."""

from datetime import datetime
from types import SimpleNamespace

from intelligence.rtracker import (
    PNL_BASIS_GROSS,
    PNL_BASIS_NET,
    RegimeTracker,
    normalize_replay_pnl_event,
)


def _exit_candidate(**overrides):
    values = {
        "instrument": "NIFTY",
        "timeframe": "5min",
        "direction": "LONG",
        "signal_timestamp": datetime(2026, 8, 21, 10, 0),
        "entry_timestamp": datetime(2026, 8, 21, 10, 0),
        "exit_timestamp": datetime(2026, 8, 21, 10, 5),
        "action": "EXIT",
        "status": "EXIT SIGNAL",
        "price": 100.0,
        "current_price": 110.0,
        "exit_price": 110.0,
        "pnl": 570.0,
        "pnl_path": [(0.0, 0.0), (20.0, 650.0)],
        "inst_type": "OPT",
        "option_type": "CE",
        "lots": 1,
        "lot_size": 65,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _event(candidate):
    tracker = RegimeTracker.__new__(RegimeTracker)
    return tracker._event_from_candidate(
        candidate,
        decision="EXIT",
        reason="TARGET_HIT",
        source="unit",
        extra=None,
    )


def test_new_exit_event_declares_pnl_bases_and_exact_round_trip_cost():
    event = _event(_exit_candidate())

    assert event["pnl_basis"] == PNL_BASIS_NET
    assert event["pnl_path_basis"] == PNL_BASIS_GROSS
    assert event["round_trip_cost"] == 80.0

    replay = normalize_replay_pnl_event(event)
    assert replay is not None
    assert replay["pnl"] == 570.0
    assert replay["pnl_path_gross"] == [[0.0, 0.0], [20.0, 650.0]]
    assert replay["pnl_path_net"] == [[0.0, -80.0], [20.0, 570.0]]


def test_legacy_exit_infers_cost_before_replaying_the_gross_path():
    event = _event(_exit_candidate())
    event.pop("pnl_basis")
    event.pop("pnl_path_basis")
    event.pop("round_trip_cost")

    replay = normalize_replay_pnl_event(event)
    assert replay is not None
    assert replay["round_trip_cost"] == 80.0
    assert replay["pnl"] == 570.0
    assert replay["pnl_path_net"][-1][1] == 570.0


def test_an_explicit_net_path_is_converted_back_to_guard_decision_units():
    event = _event(
        _exit_candidate(
            pnl_path=[(0.0, -80.0), (20.0, 570.0)],
            pnl_path_basis=PNL_BASIS_NET,
            round_trip_cost=80.0,
        )
    )

    replay = normalize_replay_pnl_event(event)
    assert replay is not None
    assert replay["pnl_path_net"] == [[0.0, -80.0], [20.0, 570.0]]
    assert replay["pnl_path_gross"] == [[0.0, 0.0], [20.0, 650.0]]


def test_legacy_event_without_enough_contract_data_is_skipped_not_mixed():
    event = {
        "schema": "rtracker.v1",
        "decision": "EXIT",
        "pnl": -100.0,
        "pnl_path": [[0.0, 0.0], [10.0, -20.0]],
        "contract": {"inst_type": "OPT"},
    }

    assert normalize_replay_pnl_event(event) is None
