"""UT/EMA broker ownership and its single native alignment add."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import config.settings as cs
from trading.trade_manager import IST, TradeManager
from tests.test_scale_in_2026_08_06 import _manager, _trade, _Settings


@pytest.fixture
def settings(monkeypatch):
    value = _Settings(scale_in_enabled=False, scale_in_max_adds_per_trade=0,
                      scale_in_max_total_lots=1, scale_in_min_profit_r=100.,
                      entry_risk_budget_enabled=True,
                      forbid_concurrent_same_contract_real=True,
                      same_contract_requires_ledger_evidence=True)
    monkeypatch.setattr(cs, "get_settings", lambda: value)
    return value


def native_trade(**values):
    trade = _trade(**values)
    trade.source_lane = trade.exit_policy_owner = "UTEMA"
    trade.source_engine = "ut_ema"
    trade.quality_verdict = {"checks": {"utema_state": {"alignment_added": False}}}
    return trade


def touch(**overrides):
    return dict(bid=199.5, ask=200., age_seconds=.1, usable=True, source="test", **overrides)


def open_manager(trade=None, mode="HISTORICAL", broker=None):
    manager = _manager(trade or native_trade(), mode=mode, broker=broker)
    manager._trade_counter = 0
    manager.max_positions = 1
    manager.orphan_positions = {}
    manager.nudge_position_sync = lambda: None
    manager.options_sl_pct = 15.
    manager._option_contract_liquidity_admission = lambda **kw: (False, "other strategy liquidity floor")
    manager._entry_risk_admission = lambda **kw: (False, "other strategy projected risk floor")
    return manager


def open_native(manager, **overrides):
    args = dict(instrument="NIFTY", timeframe="15min", direction="LONG", price=200.,
                trailing_stop=170., lots=1, lot_size=75, grade="NATIVE", target=210., rr_ratio=1/3,
                trading_symbol="NIFTY2680524500CE", symbol_token="1", inst_type="OPT",
                source_engine="ut_ema", source_lane="UTEMA", exit_policy_owner="UTEMA",
                execution_touch_quote=touch(), entry_time=datetime.now(IST),
                quality_verdict={"checks": {"utema_state": {"alignment_added": False}}})
    args.update(overrides)
    return manager.open_trade(**args)


def test_native_alignment_ignores_other_strategy_enable_profit_and_size_limits(settings):
    trade = native_trade(lots=2, current_price=190.)
    manager = _manager(trade)
    manager._entry_risk_admission = lambda **kw: (_ for _ in ()).throw(AssertionError("generic risk called"))
    ok, reason = manager.scale_in_trade(trade.id, 1, 190., native_alignment=True)
    assert ok, reason
    assert trade.lots == 3 and trade.scale_in_count == 1
    assert trade.entry_price == pytest.approx((400. + 190.) / 3)
    assert trade.utema_state["alignment_added"] is True
    assert trade.quality_verdict["checks"]["utema_state"]["alignment_added"] is True
    assert manager.scale_in_trade(trade.id, 1, 190., native_alignment=True)[0] is False


@pytest.mark.parametrize("add_lots", [None, 0, 2, -1])
def test_native_alignment_is_exactly_one_extra_lot(settings, add_lots):
    trade = native_trade()
    assert _manager(trade).scale_in_trade(trade.id, add_lots, 200., native_alignment=True)[0] is False


def test_non_native_trade_cannot_claim_alignment_exemption(settings):
    trade = _trade()
    assert _manager(trade).scale_in_trade(trade.id, 1, 240., native_alignment=True)[0] is False
    assert _manager(trade).scale_in_trade(trade.id, 1, 240.) == (False, "scale-in disabled")


@pytest.mark.parametrize("owned,confirmed", [(None, 150), (0, 150), (150, 0)])
def test_live_alignment_requires_own_confirmed_fill(settings, owned, confirmed):
    trade = native_trade()
    trade.broker_quantity = confirmed
    manager = _manager(trade, mode="REAL", broker=SimpleNamespace())
    manager.scanner.auto_mode = True
    manager._ledger_owned_quantity = lambda tid: owned
    manager._request_broker_scale_in = lambda *a: pytest.fail("An unconfirmed add was submitted")
    assert manager.scale_in_trade(trade.id, 1, 200., native_alignment=True)[0] is False


def test_live_alignment_submits_only_one_owned_lot_under_auto(settings):
    trade = native_trade()
    manager = _manager(trade, mode="REAL", broker=SimpleNamespace())
    manager.scanner.auto_mode = True
    manager._ledger_owned_quantity = lambda tid: 150
    submitted = []
    manager._request_broker_scale_in = lambda *a: submitted.append(a)
    ok, reason = manager.scale_in_trade(trade.id, 1, 200., native_alignment=True)
    assert ok, reason
    assert len(submitted) == 1 and submitted[0][1] == 1
    assert manager.scale_in_trade(trade.id, 1, 200., native_alignment=True)[0] is False


@pytest.mark.parametrize("flag,value", [("pending_exit", True), ("pending_execution", True),
                                       ("execution_status", "RECOVERY_REQUIRED"),
                                       ("_scale_in_inflight", True)])
def test_native_alignment_retains_lifecycle_controls(settings, flag, value):
    trade = native_trade()
    setattr(trade, flag, value)
    assert _manager(trade).scale_in_trade(trade.id, 1, 200., native_alignment=True)[0] is False


def test_alignment_weighted_entry_retains_fifteen_percent_premium_ceiling(settings):
    trade = native_trade(lots=2)
    old_index_stop = trade.spot_stop
    ok, reason = _manager(trade).scale_in_trade(trade.id, 1, 350., native_alignment=True)
    assert ok, reason
    assert trade.entry_price == pytest.approx(250.)
    assert trade.current_stop >= trade.entry_price * .85
    assert trade.current_stop == trade.premium_stop
    assert trade.premium_entry == trade.entry_price
    assert trade.spot_stop == old_index_stop


def test_native_same_contract_peers_use_ledger_without_changing_master_flag(settings):
    trade = native_trade()
    manager = _manager(trade, mode="REAL")
    manager._ledger_owned_quantity = lambda tid: 150
    assert manager._same_contract_entry_blocked(trade.trading_symbol, native_owner="UTEMA") == (False, "")
    assert manager._same_contract_entry_blocked(trade.trading_symbol)[0] is True
    assert settings.forbid_concurrent_same_contract_real is True


@pytest.mark.parametrize("quantity", [None, 0])
def test_native_same_contract_never_guesses_ownership(settings, quantity):
    trade = native_trade()
    manager = _manager(trade, mode="REAL")
    manager._ledger_owned_quantity = lambda tid: quantity
    settings.same_contract_requires_ledger_evidence = False
    blocked, reason = manager._same_contract_entry_blocked(trade.trading_symbol, native_owner="UTEMA")
    assert blocked is (quantity is None)
    if quantity is None:
        assert "ledger" in reason


def test_native_same_contract_exemption_does_not_extend_to_other_owners(settings):
    trade = _trade()
    trade.source_lane = trade.exit_policy_owner = "S1"
    manager = _manager(trade, mode="REAL")
    manager._ledger_owned_quantity = lambda tid: 150
    assert manager._same_contract_entry_blocked(trade.trading_symbol, native_owner="UTEMA")[0] is True


def test_native_open_uses_its_own_geometry_not_position_risk_or_liquidity_floors(settings):
    manager = open_manager()
    assert manager.can_open_trade()[0] is False
    trade = open_native(manager)
    assert trade is not None and trade.timeframe == "15min"
    assert trade.exit_policy_owner == trade.source_lane == "UTEMA"
    assert trade.utema_state["alignment_added"] is False
    assert trade.is_recovery is False
    assert trade.current_stop == pytest.approx(170.)


def test_native_open_keeps_actual_broker_funds_refusal(settings):
    checked = []
    broker = SimpleNamespace(cached_entry_margin_refusal=lambda **kw: checked.append(kw) or {
        "ok": False, "reason": "insufficient funds"})
    manager = open_manager(mode="REAL", broker=broker)
    manager.open_trades = {}
    assert open_native(manager) is None
    assert len(checked) == 1 and checked[0]["qty"] == 75


@pytest.mark.parametrize("overrides", [dict(price=float("nan")), dict(price=0.),
                                       dict(trailing_stop=-1.), dict(trailing_stop=210.),
                                       dict(target=190.)])
def test_native_open_rejects_invalid_price_geometry(settings, overrides):
    manager = open_manager()
    assert open_native(manager, **overrides) is None


@pytest.mark.parametrize("quote", [{}, {"usable": True, "bid": 200., "ask": 199., "age_seconds": .1},
                                  {"usable": True, "bid": 199., "ask": 200., "age_seconds": 10.}])
def test_native_real_entry_still_requires_fresh_executable_book(settings, quote):
    manager = open_manager(mode="REAL")
    manager.open_trades = {}
    assert open_native(manager, execution_touch_quote=quote) is None


def test_native_actual_fill_preserves_selected_rr_without_generic_floor(settings):
    trade = native_trade()
    trade.target = 210.
    manager = _manager(trade)
    manager.fresh_entry_policy_guard = lambda t: (False, "other strategy floor")
    ok, reason = manager._actual_fill_entry_policy_verdict(trade)
    assert ok, reason
    assert trade.current_stop == 170.
    trade._entry_policy_actual_fill_verified = False
    assert manager._actual_fill_entry_policy_verdict(trade)[0] is False


def test_native_premium_risk_cap_comes_from_its_frozen_state(settings):
    trade = native_trade()
    trade.quality_verdict["checks"]["utema_state"]["premium_risk_pct"] = 4.
    manager = _manager(trade)
    manager.options_sl_pct = 1.
    manager._cap_option_entry_stop(trade)
    assert trade.current_stop == 192.


def test_native_alignment_reuses_order_fill_ledger_and_full_quantity_protection(settings):
    trade = native_trade(lots=2)
    trade.target = 300.
    orders, fills, protection, margins = [], [], [], []
    def place_order(symbol, token, qty, side, product_type, price=0., **kw):
        orders.append((symbol, qty, side, price, kw))
        return "NATIVE-ADD"
    broker = SimpleNamespace(place_order=place_order, get_fill=lambda oid: {
        "state": "FILLED", "quantity": 75, "average_price": 240.})
    manager = _manager(trade, mode="REAL", broker=broker)
    manager.scanner.auto_mode = True
    manager._tag_kwargs = lambda *a, **kw: {}
    manager._entry_margin_verdict = lambda *a: margins.append(a) or None
    manager._record_fill = lambda t, oid, qty, leg: fills.append((oid, qty, leg))
    manager.ensure_broker_protection = lambda t, qty: protection.append(qty) or True
    manager._release_margin_reservations = lambda *a: None
    manager._option_contract_liquidity_admission = lambda **kw: (False, "other strategy floor")
    manager._run_scale_in(trade, 1, 75, 240., "UTEMA_ALIGNMENT")
    assert len(orders) == 1 and orders[0][1:4] == (75, "BUY", 240.)
    assert len(margins) == 1
    assert fills == [("NATIVE-ADD", 75, "ADD")]
    assert protection == [225]
    assert trade.entry_price == pytest.approx((400. + 240.) / 3)
    assert trade.broker_quantity == 225 and trade.lots == 3
    assert trade.current_stop >= trade.entry_price * .85


@pytest.mark.parametrize("mode,auto", [("HISTORICAL", True), ("REAL", False)])
def test_queued_native_add_rechecks_live_auto_authority(settings, mode, auto):
    trade = native_trade()
    broker = SimpleNamespace(place_order=lambda *a, **kw: pytest.fail("unauthorized live add"))
    manager = _manager(trade, mode=mode, broker=broker)
    manager.scanner.auto_mode = auto
    manager._entry_margin_verdict = lambda *a: pytest.fail("unauthorized add reached margin")
    manager._run_scale_in(trade, 1, 75, 200., "UTEMA_ALIGNMENT")
    assert trade.lots == 2 and trade.scale_in_count == 0


def test_native_add_rechecks_auto_after_margin_preflight(settings):
    trade = native_trade()
    broker = SimpleNamespace(place_order=lambda *a, **kw: pytest.fail("Auto-off order submitted"))
    manager = _manager(trade, mode="REAL", broker=broker)
    manager.scanner.auto_mode = True
    manager._tag_kwargs = lambda *a, **kw: {}
    released = []
    def margin(*args):
        manager.scanner.auto_mode = False
        return {"ok": True, "reservation_id": "native-reservation"}
    manager._entry_margin_verdict = margin
    manager._release_margin_reservations = lambda reservations: released.extend(reservations)
    manager._run_scale_in(trade, 1, 75, 200., "UTEMA_ALIGNMENT")
    assert released == ["native-reservation"]
    assert trade.lots == 2 and trade.scale_in_count == 0


def test_native_alignment_queue_refusal_is_retryable_without_booking(settings, monkeypatch):
    from engine.async_queue import execution_queue
    trade = native_trade()
    manager = _manager(trade, mode="REAL", broker=SimpleNamespace())
    manager.scanner.auto_mode = True
    manager._ledger_owned_quantity = lambda tid: 150
    monkeypatch.setattr(execution_queue, "submit", lambda *a, **kw: False)
    ok, reason = manager.scale_in_trade(trade.id, 1, 200., native_alignment=True)
    assert ok is False and "queue" in reason
    assert trade.lots == 2 and trade.scale_in_count == 0
    assert getattr(trade, "_scale_in_inflight", False) is False


def test_native_actual_fill_reprices_selected_rr_from_fill_and_tick_stop(settings):
    trade = native_trade(entry_price=203.37)
    trade.target = 230.  # Target from the earlier signal quote is obsolete.
    trade.rr_ratio = 1.5
    trade.spot_target, trade.underlying_target = 24096., 24096.
    trade.utema_state = {"premium_risk_pct": 7.3, "rr": 3.25}
    manager = _manager(trade)
    manager.options_sl_pct = .1
    manager.fresh_entry_policy_guard = lambda t: pytest.fail("generic RR policy")
    assert manager._actual_fill_entry_policy_verdict(trade) == (True, "")
    assert trade.current_stop == pytest.approx(188.55)
    assert (trade.entry_price - trade.current_stop) / trade.entry_price <= .073
    assert trade.target == pytest.approx(203.37 + (203.37 - 188.55) * 3.25)
    assert trade.premium_target == trade.target
    assert trade.entry_policy_actual_rr == trade.rr_ratio == 3.25
    assert trade.spot_target == trade.underlying_target == 24096.


def test_native_favorable_fill_keeps_tighter_protection_and_selected_rr(settings):
    trade = native_trade(entry_price=190.)
    trade.target = 260.
    trade.utema_state = {"premium_risk_pct": 15., "rr": 2.5}
    assert _manager(trade)._actual_fill_entry_policy_verdict(trade) == (True, "")
    assert trade.current_stop == 170.  # Never relax an already selected trigger.
    assert trade.target == pytest.approx(240.)
    assert (trade.target-trade.entry_price)/(trade.entry_price-trade.current_stop) == 2.5


def test_native_alignment_target_uses_actual_partial_fill_average(settings):
    trade = native_trade(lots=2, entry_price=200.)
    trade.target, trade.spot_target = 250., 24096.
    trade.utema_state = {"premium_risk_pct": 8., "rr": 3.}
    trade._entry_option_stop_cap_pct = 1.  # Another strategy's stale cap cannot replace native risk.
    manager = _manager(trade)
    manager._book_scale_in(trade, 1, 240., "UTEMA_ALIGNMENT", filled_quantity=30)
    assert trade.entry_price == pytest.approx((200.*150 + 240.*30)/180)
    assert trade.broker_quantity == 180
    assert trade.current_stop == pytest.approx(190.15)
    assert trade.target == pytest.approx(trade.entry_price + (trade.entry_price-190.15)*3.)
    assert trade.premium_entry == trade.entry_price
    assert trade.premium_stop == trade.current_stop
    assert trade.premium_target == trade.target
    assert trade.spot_target == 24096.
    assert trade.utema_state["alignment_added"] is True
    assert manager._actual_fill_entry_policy_verdict(trade, phase="scale_in_actual_fill") == (True, "")
    assert trade.current_stop == pytest.approx(190.15)


def test_native_averaging_down_does_not_relax_stop_or_freeze_a_new_risk_percentage(settings):
    trade = native_trade(lots=2, entry_price=200.)
    trade.utema_state = {"premium_risk_pct": 15., "rr": 2.5}
    manager = _manager(trade)
    manager._book_scale_in(trade, 1, 190., "UTEMA_ALIGNMENT")
    assert trade.current_stop == 170.
    assert trade._entry_option_stop_cap_pct == 15.
    assert trade.target == pytest.approx(trade.entry_price + (trade.entry_price-170.)*2.5)


def test_native_profit_protected_alignment_keeps_stop_and_existing_profitable_target(settings):
    trade = native_trade(lots=2, entry_price=200.)
    trade.current_stop = trade.trailing_stop = 215.
    trade.target = 280.
    trade.utema_state = {"premium_risk_pct": 15., "rr": 2.5}
    manager = _manager(trade)
    manager._book_scale_in(trade, 1, 190., "UTEMA_ALIGNMENT")
    assert trade.current_stop == 215.
    assert trade.target == trade.premium_target == 280.
    assert manager._actual_fill_entry_policy_verdict(trade, phase="scale_in_actual_fill") == (True, "")
    assert manager._actual_fill_entry_policy_verdict(trade)[0] is False


@pytest.mark.parametrize("rr", [0., -1., float("nan"), float("inf"), "bad"])
def test_native_actual_fill_rejects_unusable_selected_rr(settings, rr):
    trade = native_trade()
    trade.target = 250.
    trade.utema_state = {"premium_risk_pct": 15., "rr": rr}
    allowed, reason = _manager(trade)._actual_fill_entry_policy_verdict(trade)
    assert allowed is False
    assert "selected option RR" in reason
