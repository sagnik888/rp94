"""Combined scanner admission must retain native contracts and shared risk limits."""
import copy
import threading
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from config.settings import Settings
from engine.scanner_modes import COMBINED_MODES
from tests.test_multi_signal_mixed_book import _cand, _scanner
from trading.trade_manager import TradeManager


@pytest.fixture
def policy(monkeypatch):
    settings = Settings(_env_file=None, scanner_engine="quad_prime",
        multi_signal_mixed_book_enabled=True, multi_signal_min_instruments=3,
        multi_signal_min_timeframes=2, multi_signal_min_engines=2,
        multi_signal_max_fut=1, multi_signal_max_opt_instruments=1,
        entry_risk_budget_enabled=True, capital_total=200000,
        capital_fut=100000, capital_opt=100000, risk_fut_pct=2,
        risk_opt_pct=1, max_daily_loss_pct=10, max_trade_loss_abs=0,
        slippage_fut_bps=0, cascade_max_concurrent_positions=0)
    monkeypatch.setattr("config.settings._settings", settings)
    return settings


def _native_contract(candidate):
    return tuple(getattr(candidate, name, None) for name in (
        "instrument", "timeframe", "direction", "inst_type", "trading_symbol",
        "symbol_token", "price", "stop", "target", "initial_stop", "lots",
        "lot_size", "source_lane", "source_engine"))


@pytest.mark.parametrize("mode", COMBINED_MODES)
@pytest.mark.parametrize("case", ["futures", "existing_future", "options"])
def test_combined_wave_retains_native_contracts_but_not_segment_cap_bypass(policy, mode, case):
    scanner = _scanner()
    native = COMBINED_MODES[mode]
    inst_type = "OPT" if case == "options" else "FUT"
    candidates = [_cand(instrument, inst_type=inst_type,
        source_engine="s1" if inst_type == "OPT" and index == 1 else native,
        source_lane="S1" if inst_type == "OPT" and index == 1 else ("QP" if native == "quad_prime" else "UTS"),
        signal_timestamp=datetime(2026, 10, 5, 10), initial_stop=90 if inst_type == "OPT" else 24975)
        for index, instrument in enumerate(("NIFTY", "BANKNIFTY", "SENSEX"))]
    if case == "existing_future":
        scanner.trades.open_trades = {"existing": NS(instrument="NIFTY", inst_type="FUT", is_ghost=False)}
    assert scanner._multi_signal_mixed_book_trigger(candidates)

    # The same configured cap is enforced by the existing standalone scanner.
    policy.scanner_engine = native
    native_output = scanner._apply_multi_signal_mixed_book(copy.deepcopy(candidates))
    future_slots = 0 if case == "existing_future" else 1
    assert sum(c.inst_type == "FUT" for c in native_output) <= future_slots
    assert sum(c.inst_type == "OPT" for c in native_output) <= 1

    policy.scanner_engine = mode
    originals = {id(c): _native_contract(c) for c in candidates}
    result = scanner._apply_multi_signal_mixed_book(candidates)
    assert sum(c.inst_type == "FUT" for c in result) <= future_slots
    assert sum(c.inst_type == "OPT" for c in result) <= 1
    if case != "existing_future":
        assert result
    assert all(id(c) in originals and _native_contract(c) == originals[id(c)] for c in result)


def _manager():
    manager = TradeManager.__new__(TradeManager)
    manager._lock = threading.RLock()
    manager.max_positions = 2
    manager.closed_trades = []
    manager.orphan_positions = {}
    manager.open_trades = {
        "quad": NS(inst_type="OPT", direction="LONG", entry_price=200, current_stop=190,
            lots=1, lot_size=65, source_lane="QP", source_engine="quad_prime", is_ghost=False),
        "s1": NS(inst_type="OPT", direction="SHORT", entry_price=200, current_stop=190,
            lots=1, lot_size=65, source_lane="S1", source_engine="s1", is_ghost=False)}
    return manager


@pytest.mark.parametrize("mode", ["quad_prime", "ut_smart", "s1", *COMBINED_MODES])
def test_all_native_and_combined_modes_share_global_position_cap(policy, mode):
    policy.scanner_engine = mode
    allowed, reason = _manager().can_open_trade(is_recovery=False)
    assert not allowed
    assert reason == "Max positions (2) reached"


@pytest.mark.parametrize("mode", ["quad_prime", "ut_smart", "s1", *COMBINED_MODES])
def test_all_native_and_combined_modes_count_other_owners_stop_risk(policy, mode):
    policy.scanner_engine = mode
    manager = _manager()
    manager.open_trades.pop("quad")
    allowed, reason = manager._entry_risk_admission(inst_type="OPT", direction="LONG",
        price=200, trailing_stop=190, initial_stop=190, lots=1, lot_size=65,
        instrument_multiplier=.5, is_recovery=False)
    assert not allowed
    assert "OPT projected stop risk Rs.1300 exceeds Rs.1000" in reason


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_disabled_mixed_book_keeps_native_independent_candidates(policy, mode):
    policy.scanner_engine = mode
    policy.multi_signal_mixed_book_enabled = False
    scanner = _scanner()
    native = COMBINED_MODES[mode]
    candidates = [_cand(inst, inst_type="FUT", source_engine=native,
        source_lane="QP" if native == "quad_prime" else "UTS",
        signal_timestamp=datetime(2026, 10, 5, 10))
        for inst in ("NIFTY", "BANKNIFTY", "SENSEX")]
    assert scanner._apply_multi_signal_mixed_book(candidates) == candidates


@pytest.mark.parametrize("mode", COMBINED_MODES)
@pytest.mark.parametrize("preference,inst_type,limit", [
    ("AUTO", "FUT", 1), ("AUTO", "OPT", 1), ("FUT", "FUT", 2), ("OPT", "OPT", 2)])
def test_combined_admission_honors_existing_preference_slot_arithmetic(policy, mode, preference, inst_type, limit):
    policy.scanner_engine = mode
    scanner = _scanner()
    scanner.inst_pref = preference
    native = COMBINED_MODES[mode]
    candidates = [_cand(inst, inst_type=inst_type, source_engine=native,
        source_lane="QP" if native == "quad_prime" else "UTS",
        signal_timestamp=datetime(2026, 10, 5, 10))
        for inst in ("NIFTY", "BANKNIFTY", "SENSEX")]
    contracts = {id(c): _native_contract(c) for c in candidates}
    result = scanner._apply_multi_signal_mixed_book(candidates)
    assert len(result) == limit
    assert all(_native_contract(c) == contracts[id(c)] for c in result)


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_caps_never_drop_exit_candidates_or_charge_them_to_entry_slots(policy, mode):
    policy.scanner_engine = mode
    scanner = _scanner()
    native = COMBINED_MODES[mode]
    entries = [_cand(inst, inst_type="FUT", source_engine=native,
        source_lane="QP" if native == "quad_prime" else "UTS",
        signal_timestamp=datetime(2026, 10, 5, 10))
        for inst in ("NIFTY", "BANKNIFTY", "SENSEX")]
    exits = [_cand(inst, action="EXIT", inst_type="FUT", source_engine=native,
        source_lane="QP" if native == "quad_prime" else "UTS",
        signal_timestamp=datetime(2026, 10, 5, 10)) for inst in ("NIFTY", "SENSEX")]
    result = scanner._apply_multi_signal_mixed_book([*exits, *entries])
    assert all(c in result for c in exits)
    assert sum(c.action == "ENTRY" and c.inst_type == "FUT" for c in result) == 1


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_nontriggering_combined_wave_does_not_acquire_new_segment_caps(policy, mode):
    policy.scanner_engine = mode
    scanner = _scanner()
    candidates = [_cand("NIFTY", inst_type="OPT", source_engine="s1", source_lane="S1"),
        _cand("NIFTY", inst_type="OPT", source_engine=COMBINED_MODES[mode],
            source_lane="QP" if mode == "quad_prime_s1" else "UTS")]
    assert not scanner._multi_signal_mixed_book_trigger(candidates)
    assert scanner._apply_multi_signal_mixed_book(candidates) == candidates


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_ghost_future_never_consumes_combined_live_future_slot(policy, mode):
    policy.scanner_engine = mode
    scanner = _scanner()
    scanner.trades.open_trades = {"ghost": NS(instrument="NIFTY", inst_type="FUT", is_ghost=True)}
    candidates = [_cand(inst, inst_type="FUT", source_engine=COMBINED_MODES[mode],
        source_lane="QP" if mode == "quad_prime_s1" else "UTS",
        signal_timestamp=datetime(2026, 10, 5, 10))
        for inst in ("NIFTY", "BANKNIFTY", "SENSEX")]
    result = scanner._apply_multi_signal_mixed_book(candidates)
    assert len(result) == 1 and result[0].inst_type == "FUT"


@pytest.mark.parametrize("mode", COMBINED_MODES)
def test_combined_futures_only_keeps_one_future_per_index_without_conversion(policy, mode):
    policy.scanner_engine = mode
    policy.multi_signal_max_fut = 2
    policy.multi_signal_max_opt_instruments = 2
    scanner = _scanner()
    scanner.inst_pref = "FUT"
    candidates = [_cand(inst, inst_type="FUT", timeframe=tf,
        source_engine=COMBINED_MODES[mode], source_lane="QP" if mode == "quad_prime_s1" else "UTS",
        signal_timestamp=datetime(2026, 10, 5, 10))
        for inst, tf in (("NIFTY", "1min"), ("NIFTY", "5min"), ("BANKNIFTY", "5min"), ("SENSEX", "5min"))]
    result = scanner._apply_multi_signal_mixed_book(candidates)
    assert len(result) == 3
    assert len({c.instrument for c in result}) == 3
    assert all(c.inst_type == "FUT" for c in result)
