"""Offline proof that native RR policies vary and execution preserves their geometry."""
from types import SimpleNamespace as NS

import pytest

from config.settings import Settings
from engine import signal_quality_policy
from scanner import Scanner
from tests.test_combined_filter_inheritance_2026_10_05 import OWNERS, _run
from tests.test_ut_smart_live_path_2026_09_26 import live


@pytest.mark.parametrize("mode,native", OWNERS)
def test_native_combined_targets_match_measured_contract_and_index_rr(live, monkeypatch, mode, native):
    candidates, _ = _run(live, monkeypatch, mode, native, True, "normal")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.inst_type == "OPT"
    premium_rr = (candidate.target - candidate.price) / (candidate.price - candidate.initial_stop)
    index_rr = abs(candidate.spot_target - candidate.entry_spot) / abs(candidate.entry_spot - candidate.spot_stop)
    assert premium_rr == pytest.approx(candidate.rr)
    assert index_rr == pytest.approx(candidate.rr)
    if native == "ut_smart":
        assert candidate.rr == 3.0  # UTS uses this as a nominal display target, not an exit.
    elif native == "s1":
        assert candidate.rr in {0.7, 1.0}
    else:
        assert 1.5 <= candidate.rr <= 2.5  # Native short-timeframe ceiling.


def test_quad_quality_and_timeframe_produce_distinct_initial_rr(monkeypatch):
    settings = Settings(_env_file=None)
    monkeypatch.setattr(signal_quality_policy, "get_settings", lambda: settings)
    low = signal_quality_policy.dynamic_rr("B", .40, 0.0, timeframe="15min", direction="LONG")
    middle = signal_quality_policy.dynamic_rr("A", .70, .30, timeframe="15min", direction="LONG")
    high = signal_quality_policy.dynamic_rr("A+", .95, 1.0, timeframe="15min", direction="LONG")
    short = signal_quality_policy.dynamic_rr("A+", .95, 1.0, timeframe="5min", direction="LONG")
    assert (low, middle, high, short) == (1.5, 2.98, 4.76, 2.5)
    assert low < middle < high


@pytest.mark.parametrize("source,lane,rr", [
    ("quad_prime", "QP", 2.95), ("ut_smart", "UTS", 3.0),
    ("s1", "S1", 1.0), ("s1", "S1", .7),
])
@pytest.mark.parametrize("direction,option_type", [("LONG", "CE"), ("SHORT", "PE")])
def test_live_ask_repricing_preserves_native_rr_and_buy_option_stop(monkeypatch, source, lane, rr, direction, option_type):
    import scanner as scanner_module

    # Legacy S1 floor (0.5): the 0.7 weak target is preserved, not lifted to the base target.
    settings = Settings(_env_file=None, s1_lane_rr_floor_at_target=False)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)
    monkeypatch.setattr(Scanner, "_option_premium_stop_distance_for_candidate", lambda *a: 18.0)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    candidate = NS(instrument="NIFTY", inst_type="OPT", direction=direction, option_type=option_type,
                   source_engine=source, source_lane=lane, price=100.0, stop=90.0,
                   initial_stop=90.0, target=100.0 + 10.0 * rr, rr=rr)
    entry, stop, target = scanner._execution_levels(candidate, execution_touch_quote={
        "usable": True, "bid": 119.0, "ask": 120.0, "age_seconds": .1,
    })
    assert entry == 120.0
    assert stop == 108.0
    assert stop < entry < target
    assert (target - entry) / (entry - stop) == pytest.approx(rr)
