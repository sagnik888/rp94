"""Regression cases for the five consistency defects in the 2026-10-06 guard audit.

reports/guard_audit_20261006/GUARD_AUDIT.md confirmed each defect with an offline probe.
Every test here runs production code (the real Scanner / TradeManager / RiskManager
methods, or the production exit block extracted from the manual ledger) with controlled
inputs, and pairs the repaired behaviour with a positive control so a guard that simply
stopped firing would fail too.

1. Manual HOLD: a trend deferral or breathing clamp must not be overruled by the
   retention backstop in the same pass, and a deferred lock restores the stop.
2. Manual candidates (``price``) and broker trades (``entry_price``) size the same.
3. The separate major-win rung compares per-lot with per-lot.
4. The queued Auto guard rechecks the daily-loss breaker before broker submission.
5. The enabled entry-risk budget refuses entry when it cannot be evaluated.
"""
from __future__ import annotations

import ast
import inspect
import sys
import threading
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from engine.risk_manager import RiskManager
from engine.signal_processor import TradeCandidate
from scanner import Scanner
from trading.trade_manager import IST, Trade, TradeManager

ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides):
    base = dict(
        major_win_guard_flat_fallback_rs=3000.0,
        major_win_guard_cost_pct_by_index={"NIFTY": 11.5},
        major_win_guard_futures_r_multiple=1.5,
        profit_lock_abs_floor_rs=450.0,
        profit_lock_cost_pct=5.0,
        profit_lock_min_cost_multiple=2.0,
        profit_lock_per_lot_basis=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def controlled_settings(monkeypatch):
    holder = {"value": _settings()}
    monkeypatch.setattr(scanner_module, "get_settings", lambda: holder["value"])
    return holder


def _host():
    host = Scanner.__new__(Scanner)
    host._round_trip_cost_rs = lambda _row: 100.0
    return host


def _candidate(price=200.0, lots=1, lot_size=100, instrument="NIFTY"):
    return TradeCandidate(
        instrument=instrument, direction="LONG", price=price, stop=price * 0.85,
        target=price * 1.3, lots=lots, lot_size=lot_size, grade="A", confidence=70.0,
        timeframe="5min", inst_type="OPT", option_type="CE", atm_strike=25000.0,
        multiplier=1.0, trading_symbol="NIFTYTESTCE", symbol_token="TOKEN",
    )


def _trade(entry_price=200.0, lots=1, lot_size=100, instrument="NIFTY", inst_type="OPT"):
    return Trade(
        id="T-AUDIT", instrument=instrument, timeframe="5min", direction="LONG",
        entry_price=entry_price, entry_time=datetime.now(IST),
        trailing_stop=entry_price * 0.85, current_stop=entry_price * 0.85,
        lots=lots, lot_size=lot_size, grade="A", inst_type=inst_type,
    )


# ── 2. one sizing basis for candidate, broker trade and replay rows ─────────────────

def _sizing(host, row):
    return dict(
        cost=Scanner._position_cost_rs(row),
        arm_base=host._profit_lock_arm_base_rs(row, 250.0, 2.5),
        keep_floor=host._profit_lock_floor_rs(row),
        major_arm=host._major_win_guard_arm_rs(row),
    )


def test_manual_candidate_and_broker_trade_size_the_same_contract_identically(controlled_settings):
    host = _host()
    manual = _sizing(host, _candidate())
    broker = _sizing(host, _trade())
    # Replay rows carry both fields (engine/signal_processor.py historical option sim).
    replay_row = SimpleNamespace(
        instrument="NIFTY", inst_type="OPT", lots=1, lot_size=100,
        price=200.0, entry_price=200.0,
    )
    replay = _sizing(host, replay_row)

    assert manual == broker == replay
    # The audit's broker column: premium cost, not the flat fallbacks.
    assert broker == pytest.approx(
        dict(cost=20_000.0, arm_base=500.0, keep_floor=1_000.0, major_arm=2_300.0)
    )


def test_unpriced_rows_still_take_the_flat_fallbacks(controlled_settings):
    host = _host()
    unpriced = SimpleNamespace(instrument="NIFTY", inst_type="OPT", lots=1, lot_size=100)
    assert _sizing(host, unpriced) == pytest.approx(
        dict(cost=0.0, arm_base=250.0, keep_floor=450.0, major_arm=3_000.0)
    )


def test_a_zero_price_falls_through_to_entry_price():
    row = SimpleNamespace(price=0.0, entry_price=200.0)
    assert Scanner._trade_like_entry_price(row) == pytest.approx(200.0)
    assert Scanner._trade_like_entry_price(SimpleNamespace(price=150.0)) == pytest.approx(150.0)
    assert Scanner._trade_like_entry_price(SimpleNamespace(entry_price=180.0)) == pytest.approx(180.0)
    assert Scanner._trade_like_entry_price(SimpleNamespace(price="junk")) == 0.0
    assert Scanner._trade_like_entry_price(None) == 0.0


def test_loss_guard_premium_melt_reads_the_shared_entry_adapter():
    source = inspect.getsource(Scanner._loss_guard_severity)
    assert 'getattr(trade_like, "entry_price", 0.0)' not in source
    assert "_trade_like_entry_price(trade_like)" in source


# ── 3. major-win rung: per-lot peak against a per-lot arm ───────────────────────────

def _position(lots, price=200.0, lot_size=100, **extra):
    return SimpleNamespace(
        instrument="NIFTY", inst_type="OPT", lots=lots, lot_size=lot_size,
        quantity=lots * lot_size, entry_price=price, price=price, **extra,
    )


def test_adding_a_lot_does_not_switch_the_major_win_rung_off(controlled_settings):
    host = _host()
    for lots in (1, 2, 3):
        row = _position(lots)
        total_peak = 2_400.0 * lots
        ladder_peak = Scanner._profit_ladder_pnl(host, row, total_peak, row.quantity)
        arm = host._major_win_guard_ladder_arm_rs(row, row.quantity)
        assert ladder_peak == pytest.approx(2_400.0)
        assert arm == pytest.approx(2_300.0), f"{lots} lots"
        assert ladder_peak >= arm


def test_major_win_rung_still_needs_a_major_peak_at_every_size(controlled_settings):
    host = _host()
    for lots in (1, 2, 3):
        row = _position(lots)
        ladder_peak = Scanner._profit_ladder_pnl(host, row, 2_200.0 * lots, row.quantity)
        assert ladder_peak < host._major_win_guard_ladder_arm_rs(row, row.quantity)


def test_whole_position_basis_is_unchanged_when_per_lot_is_off(controlled_settings):
    controlled_settings["value"] = _settings(profit_lock_per_lot_basis=False)
    host = _host()
    row = _position(2)
    assert host._major_win_guard_ladder_arm_rs(row, row.quantity) == pytest.approx(4_600.0)
    assert host._major_win_guard_arm_rs(row) == pytest.approx(4_600.0)


def test_flat_fallback_rung_is_not_divided_by_lots(controlled_settings):
    host = _host()
    unknown_index = SimpleNamespace(
        instrument="UNKNOWN_IDX", inst_type="OPT", lots=3, lot_size=100,
        quantity=300, entry_price=200.0,
    )
    assert host._major_win_guard_ladder_arm_rs(unknown_index, 300) == pytest.approx(3_000.0)


def test_futures_rung_is_per_lot_with_the_flat_floor(controlled_settings):
    host = _host()
    future = SimpleNamespace(
        instrument="NIFTY", inst_type="FUT", lots=2, lot_size=25, quantity=50,
        entry_price=25_000.0, frozen_initial_risk_rs=6_000.0,
    )
    # Whole position: max(3000, 6000 * 1.5) = 9000 -> per lot 4500.
    assert host._major_win_guard_arm_rs(future) == pytest.approx(9_000.0)
    assert host._major_win_guard_ladder_arm_rs(future, 50) == pytest.approx(4_500.0)
    small = SimpleNamespace(**{**vars(future), "frozen_initial_risk_rs": 2_500.0})
    # Whole 3750 -> per lot 1875, floored at the 3000 rung.
    assert host._major_win_guard_ladder_arm_rs(small, 50) == pytest.approx(3_000.0)


def _first_major_win_if(tree_or_node):
    for node in ast.walk(tree_or_node):
        if isinstance(node, ast.If) and any(
            isinstance(n, ast.Attribute) and n.attr.startswith("_major_win_guard_")
            for n in ast.walk(node.test)
        ):
            return node
    raise AssertionError("major-win branch not found")


def test_manual_live_and_replay_major_win_branches_agree_across_lot_counts(controlled_settings):
    """Evaluate the production branch conditions themselves, as the audit probe did."""
    scanner_tree = ast.parse((ROOT / "scanner.py").read_text(encoding="utf-8-sig"))
    scanner_cls = next(n for n in scanner_tree.body if isinstance(n, ast.ClassDef) and n.name == "Scanner")
    manual_method = next(
        n for n in scanner_cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_check_session_candidates_breaches_locked"
    )
    manual_if = _first_major_win_if(manual_method)
    live_if = _first_major_win_if(ast.parse((ROOT / "engine" / "exit_cycle.py").read_text(encoding="utf-8-sig")))
    replay_if = _first_major_win_if(ast.parse((ROOT / "engine" / "signal_processor.py").read_text(encoding="utf-8-sig")))
    live_retention = next(n for n in live_if.body if isinstance(n, ast.If))

    def evaluate(condition, env):
        return bool(eval(compile(ast.Expression(condition), "<production branch>", "eval"), env))

    host = _host()
    outcomes = []
    for lots in (1, 2):
        row = _position(lots, peak_pnl=2_400.0 * lots)
        current = row.peak_pnl * 0.70
        env = dict(
            vars(scanner_module), scanner=host, self=host, probe=host, _Scanner=Scanner,
            trade=row, previous=row, adaptive_active=False, qty=row.quantity,
            previous_peak=row.peak_pnl, peak=row.peak_pnl, current_pnl=current,
            current_pnl_rs=current,
            ladder_peak=Scanner._profit_ladder_pnl(host, row, row.peak_pnl, row.quantity),
        )
        outcomes.append((
            evaluate(manual_if.test, env),
            evaluate(live_if.test, env) and evaluate(live_retention.test, env),
            evaluate(replay_if.test, env),
        ))
    assert outcomes == [(True, True, True), (True, True, True)]


# ── 1. manual HOLD precedence ───────────────────────────────────────────────────────

def _manual_lock_through_backstop():
    """The manual ledger's production block from the lock ratchet to the backstop."""
    tree = ast.parse((ROOT / "scanner.py").read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Scanner")
    method = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_check_session_candidates_breaches_locked"
    )
    loop = next(
        n for n in ast.walk(method)
        if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and n.target.id == "previous"
    )

    def assigns(node, name):
        return isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        )

    start = next(i for i, n in enumerate(loop.body) if assigns(n, "option_profit_lock"))
    end = next(
        i for i, n in enumerate(loop.body)
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Tuple)
        and any(isinstance(t, ast.Name) and t.id == "gb_exit" for t in n.targets[0].elts)
    )
    body = ast.Module(body=[ast.For(
        target=ast.Name(id="previous", ctx=ast.Store()),
        iter=ast.List(elts=[ast.Name(id="row", ctx=ast.Load())], ctx=ast.Load()),
        body=loop.body[start:end], orelse=[],
    )], type_ignores=[])
    return compile(ast.fix_missing_locations(body), "<manual lock-through-backstop>", "exec")


def _run_manual_pass(*, defer, clamp, lock_stop=110.125, current_price=109.6, stop=85.0):
    row = SimpleNamespace(
        instrument="NIFTY", inst_type="OPT", timeframe="5min", price=100.0, stop=stop,
        lots=1, lot_size=100, scalp_lock_mode=False,
    )
    events = []
    host = Scanner.__new__(Scanner)
    host.log_event = lambda message, level: events.append(("log", message))
    host._candidate_net_pnl = lambda r, price: (float(price) - r.price) * 100.0
    host._candidate_gross_pnl = host._candidate_net_pnl
    host._apply_option_stop_breathing_room = lambda r, proposed, **kw: (
        (108.0, 2.0, True) if clamp else (proposed, 0.0, False)
    )
    host._should_defer_trend_option_profit_lock = lambda *a, **kw: (
        (True, "controlled valid trend hold") if defer else (False, "trend not supportive")
    )
    host._profit_lock_min_peak = lambda *a, **kw: 250.0
    host._profit_lock_floor_rs = lambda r: 450.0
    # Isolate the retention/HOLD interaction from the separate major-win branch.
    host._major_win_guard_ladder_arm_rs = lambda *a, **kw: 100_000.0
    current_pnl = (current_price - row.price) * 100.0
    env = dict(
        vars(scanner_module), self=host, row=row, price_direction="LONG",
        lock_stop=lock_stop, lock_reason="SMART_PROFIT_LOCK", lock_ratio=0.675,
        latest_time=datetime(2026, 10, 6, 12, 0), regime="TRENDING_UP", changed=False,
        previous_peak=1_500.0, current_price=current_price, current_pnl_rs=current_pnl,
        qty=100, initial_risk_prev=1_500.0,
        create_exit=lambda r, price, reason, detail: events.append(("exit", reason, price)),
        handle_recovery_watch=lambda reason: False,
    )
    exec(_manual_lock_through_backstop(), env)
    return row, [e for e in events if e[0] == "exit"], events


def test_a_valid_trend_hold_is_not_overruled_and_restores_the_stop():
    row, exits, events = _run_manual_pass(defer=True, clamp=False)
    assert exits == [], "the retention backstop overruled the trend HOLD in the same pass"
    assert row.stop == pytest.approx(85.0), "the deferred lock left its tightened stop behind"
    assert any("Deferred SMART_PROFIT_LOCK" in e[1] for e in events if e[0] == "log")


def test_a_breathing_clamp_is_not_overruled_and_keeps_its_protected_stop():
    row, exits, _events = _run_manual_pass(defer=False, clamp=True)
    assert exits == []
    assert row.stop == pytest.approx(108.0), "the protected breathing stop must stay armed"


def test_control_without_a_hold_the_crossed_lock_still_exits():
    row, exits, _events = _run_manual_pass(defer=False, clamp=False)
    assert exits == [("exit", "SMART_PROFIT_LOCK", 109.6)]
    assert row.stop == pytest.approx(110.125)


def test_control_without_a_hold_the_retention_backstop_still_exits():
    # Lock stop 105 is not crossed at 109.6, but Rs.960 of a Rs.1,500 peak is below the
    # 67.5% retention, so only the backstop can close it.
    row, exits, _events = _run_manual_pass(defer=False, clamp=False, lock_stop=105.0)
    assert exits == [("exit", "SMART_PROFIT_LOCK", 109.6)]


def test_a_hold_never_loosens_a_stop_committed_on_an_earlier_pass():
    # The row already carries a committed stop above this pass's lock proposal.
    row, exits, _events = _run_manual_pass(defer=True, clamp=False, stop=111.0, current_price=111.5)
    assert row.stop == pytest.approx(111.0)
    assert exits == []


def test_manual_backstop_uses_the_same_hold_contract_as_the_broker_path():
    manual = inspect.getsource(Scanner._check_session_candidates_breaches_locked)
    live = (ROOT / "engine" / "exit_cycle.py").read_text(encoding="utf-8-sig")
    for source in (manual, live):
        assert "_smart_profit_retention_eligible(" in source
        assert "trend_deferred=profit_lock_deferred" in source
        assert "breathing_clamped=profit_lock_breathing_clamped" in source


# ── 4. queued Auto entries recheck the daily-loss breaker ───────────────────────────

def _auto_host(mode="REAL", risk_manager="latched"):
    host = Scanner.__new__(Scanner)
    host.mode = mode
    host.auto_mode = True
    host._auto_execution_resource_blockers = lambda: []
    if risk_manager == "latched":
        rm = RiskManager(None, 1_000_000.0)
        rm.daily_loss_breached = True
        host.risk_manager = rm
    elif risk_manager == "opt_latched":
        rm = RiskManager(None, 1_000_000.0)
        rm.opt_loss_breached = True
        host.risk_manager = rm
    elif risk_manager == "clear":
        host.risk_manager = RiskManager(None, 1_000_000.0)
    elif risk_manager is not None:
        host.risk_manager = risk_manager
    return host


@pytest.mark.parametrize("mode", ["REAL", "PAPER"])
def test_queued_auto_entry_is_refused_after_the_breaker_latches(mode):
    allowed, reason = Scanner._queued_auto_entry_submission_guard(
        _auto_host(mode), SimpleNamespace(inst_type="OPT")
    )
    assert allowed is False
    assert "circuit breaker" in reason


def test_segment_breaker_binds_only_its_segment():
    host = _auto_host(risk_manager="opt_latched")
    assert Scanner._queued_auto_entry_submission_guard(host, SimpleNamespace(inst_type="OPT"))[0] is False
    assert Scanner._queued_auto_entry_submission_guard(host, SimpleNamespace(inst_type="FUT")) == (True, "")


def test_control_clear_breaker_admits_and_other_refusals_still_apply():
    host = _auto_host(risk_manager="clear")
    trade = SimpleNamespace(inst_type="OPT")
    assert Scanner._queued_auto_entry_submission_guard(host, trade) == (True, "")
    host.auto_mode = False
    assert Scanner._queued_auto_entry_submission_guard(host, trade)[0] is False
    host.auto_mode = True
    host._auto_execution_resource_blockers = lambda: ["resource refusal"]
    assert Scanner._queued_auto_entry_submission_guard(host, trade) == (False, "resource refusal")


def test_breaker_check_fails_closed_in_real_mode():
    def boom(_inst_type):
        raise RuntimeError("lock poisoned")

    raising = _auto_host(risk_manager=SimpleNamespace(can_trade=boom))
    allowed, reason = Scanner._queued_auto_entry_submission_guard(raising, SimpleNamespace(inst_type="OPT"))
    assert allowed is False and "fail-closed" in reason
    missing = _auto_host(risk_manager=None)
    allowed, reason = Scanner._queued_auto_entry_submission_guard(missing, SimpleNamespace(inst_type="OPT"))
    assert allowed is False and "unavailable" in reason
    paper_missing = _auto_host(mode="PAPER", risk_manager=None)
    assert Scanner._queued_auto_entry_submission_guard(paper_missing, SimpleNamespace(inst_type="OPT")) == (True, "")


class _Broker:
    def __init__(self):
        self.place_calls = []
        self.reservations = []
        self.released = []

    def preflight_entry_margin(self, **kwargs):
        if not kwargs.get("reserve", True):
            return {"ok": True, "reservation_id": ""}
        reservation_id = f"R{len(self.reservations) + 1}"
        self.reservations.append(reservation_id)
        return {"ok": True, "reservation_id": reservation_id}

    def release_entry_margin_reservation(self, reservation_id):
        self.released.append(str(reservation_id))

    def place_order(self, *args, **kwargs):
        self.place_calls.append(kwargs.get("symbol") or (args[0] if args else ""))
        return "ENTRY-1"

    def get_positions(self):
        return {"data": []}

    def get_order_status(self, _order_id):
        return {"status": "OPEN"}


def _manager(broker, guard):
    manager = TradeManager.__new__(TradeManager)
    manager.broker = broker
    manager.ui_logger = None
    manager.on_notification = None
    manager.entry_submission_guard = guard
    manager.max_positions = 4
    manager.session_end = manager._parse_time("15:25")
    manager.product_type = "CARRYFORWARD"
    manager.open_trades = {}
    manager.closed_trades = []
    manager._trade_counter = 0
    manager._lock = threading.RLock()
    manager._protection_lock = threading.RLock()
    manager._state_file = ""
    manager._last_mark_to_market_save = 0.0
    manager._last_protective_sync = {}
    manager._last_protective_status_poll = {}
    manager._last_recovery_alert = {}
    manager.orphan_positions = {}
    manager.futures_sl_pct = 0.3
    manager.options_sl_pct = 15.0
    manager.mode = "REAL"
    manager.is_warmup = False
    manager.save_state = lambda: None
    return manager


@pytest.fixture
def synchronous_queue(monkeypatch):
    monkeypatch.setattr("trading.order_retry.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "engine.async_queue.execution_queue.submit",
        lambda _name, func, *args, **kwargs: func(*args, **kwargs),
    )


def test_open_trade_sends_no_order_once_the_breaker_latched_while_queued(synchronous_queue):
    """Wiring, not just the helper: the scanner guard as TradeManager receives it."""
    host = _auto_host(risk_manager="clear")
    broker = _Broker()
    manager = _manager(broker, guard=host._queued_auto_entry_submission_guard)
    host.risk_manager.daily_loss_breached = True  # latched after admission, before submit

    trade = manager.open_trade(
        instrument="NIFTY", timeframe="5min", direction="LONG", price=100.0,
        trailing_stop=95.0, lots=1, lot_size=10, grade="A", target=110.0,
        trading_symbol="NIFTYTESTFUT", symbol_token="TOKEN-NIFTY", inst_type="FUT",
        exec_type="A", entry_time=IST.localize(datetime(2026, 10, 7, 10, 0)),
    )

    assert broker.place_calls == []
    assert broker.reservations == broker.released
    assert trade.exit_reason == "ENTRY_SUBMISSION_GUARD"
    assert "circuit breaker" in trade.execution_error


def test_breaker_never_blocks_closing_existing_exposure():
    """Exits do not pass through the entry submission guard."""
    source = inspect.getsource(TradeManager.close_trade)
    assert "entry_submission_guard" not in source
    assert "_entry_submission_guard_verdict" not in source


# ── 5. enabled entry-risk budget fails closed ───────────────────────────────────────

def _risk_admission(monkeypatch, *, enabled=True, evaluator=None, import_fails=False, is_recovery=False):
    import config.settings as cs

    settings = SimpleNamespace(
        entry_risk_budget_enabled=enabled, capital_fut=490_000.0, capital_opt=500_000.0,
        capital_total=1_000_005.0, risk_fut_pct=1.5, risk_opt_pct=9.0,
        max_daily_loss_pct=3.0, max_trade_loss_abs=0.0, slippage_fut_bps=0.0,
    )
    monkeypatch.setattr(cs, "get_settings", lambda: settings)
    if import_fails:
        broken = types.ModuleType("engine.entry_risk_policy")  # no evaluate_entry_risk
        monkeypatch.setitem(sys.modules, "engine.entry_risk_policy", broken)
    elif evaluator is not None:
        import engine.entry_risk_policy as policy

        monkeypatch.setattr(policy, "evaluate_entry_risk", evaluator)
    manager = TradeManager.__new__(TradeManager)
    manager.open_trades = {}
    manager.closed_trades = []
    manager._lock = threading.RLock()
    return manager._entry_risk_admission(
        inst_type="OPT", direction="LONG", price=100.0, trailing_stop=90.0,
        initial_stop=90.0, lots=1, lot_size=50, instrument_multiplier=0.5,
        is_recovery=is_recovery,
    )


def _raises(*_a, **_kw):
    raise RuntimeError("isolated evaluator failure")


def test_enabled_risk_budget_refuses_when_evaluation_raises(monkeypatch):
    allowed, reason = _risk_admission(monkeypatch, evaluator=_raises)
    assert allowed is False
    assert "could not be evaluated" in reason and "isolated evaluator failure" in reason


def test_enabled_risk_budget_refuses_when_its_policy_cannot_be_imported(monkeypatch):
    allowed, reason = _risk_admission(monkeypatch, import_fails=True)
    assert allowed is False
    assert "unavailable" in reason


def test_control_enabled_budget_still_admits_a_fitting_entry(monkeypatch):
    allowed, _reason = _risk_admission(monkeypatch)
    assert allowed is True


def test_disabled_budget_and_recovery_are_unaffected_by_a_broken_evaluator(monkeypatch):
    assert _risk_admission(monkeypatch, enabled=False, evaluator=_raises) == (True, "")
    assert _risk_admission(monkeypatch, evaluator=_raises, is_recovery=True) == (True, "")
