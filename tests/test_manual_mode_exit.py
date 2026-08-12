import asyncio
from types import SimpleNamespace
from datetime import datetime, timedelta

import pandas as pd
import pytest

import scanner as scanner_module
from scanner import Scanner
from engine.signal_processor import SignalProcessor, TradeCandidate


def test_manual_exit_notification_uses_trade_quantity_property(monkeypatch):
    captured = {}

    class NotificationManager:
        def send_trade_notification(self, **kwargs):
            captured.update(kwargs)

    trade = SimpleNamespace(
        instrument="NIFTY",
        direction="LONG",
        entry_price=100.0,
        target=110.0,
        trailing_stop=95.0,
        rr_ratio=2.0,
        quantity=50,
        execution_status="OPEN",
    )
    scanner = Scanner.__new__(Scanner)
    scanner.auto_mode = False
    scanner.trades = SimpleNamespace(open_trades={"trade-1": trade})
    scanner._reset_daily_counters = lambda: None

    monkeypatch.setattr(
        scanner_module,
        "get_notification_manager",
        lambda loop: NotificationManager(),
    )

    async def dispatch():
        scanner._close_and_record("trade-1", 105.0, "TARGET_HIT")

    asyncio.run(dispatch())

    assert captured["signal_type"] == "EXIT"
    assert captured["profit"] == 250.0
    assert callable(captured["on_execute"])


def _option_candidate(**overrides):
    values = {
        "inst_type": "OPT",
        "direction": "SHORT",
        "price": 802.65,
        "current_price": 785.08,
        "exit_price": 0.0,
        "stop": 722.39,
        "target": 1099.0,
        "lots": 1,
        "lot_size": 30,
        "multiplier": -0.5,
        "rr": 3.7,
        "score": 0.0,
        "pnl": 0.0,
        "action": "ENTRY",
        "exit_reason": "",
        "reasons": [],
        "runner_mode": False,
        "signal_timestamp": None,
        "timeframe": "5min",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_bought_pe_uses_long_premium_direction_for_profit_lock():
    scanner = Scanner.__new__(Scanner)
    candidate = _option_candidate()

    price_direction = scanner._candidate_price_direction(candidate)
    lock_stop, reason, _ = scanner._profit_lock_floor(
        candidate.price,
        price_direction,
        candidate.lots * candidate.lot_size,
        peak_pnl=1200.0,
    )

    assert price_direction == "LONG"
    assert reason == "SMART_PROFIT_LOCK"
    assert lock_stop > candidate.price
    assert scanner._candidate_gross_pnl(candidate, lock_stop) > 0


def test_smart_profit_lock_tiered_retention():
    scanner = Scanner.__new__(Scanner)

    # peak 1200 is in the Rs.1,000-2,000 tier of the operator's breathing schedule
    # (35% -> 30%): 0.65 + 0.05*(200/1000) = 0.66, i.e. 34% breathing.
    lock_stop, reason, ratio = scanner._profit_lock_floor(
        entry_price=100.0,
        direction="LONG",
        qty=10,
        peak_pnl=1200.0,
    )

    assert reason == "SMART_PROFIT_LOCK"
    # Clamped into the operator's 20-25% breathing band; the raw tier here was 0.72.
    assert ratio == 0.66
    assert lock_stop == pytest.approx(179.2)  # 100 + (1200*0.66)/10


def test_smart_profit_lock_tier_boundaries():
    scanner = Scanner.__new__(Scanner)

    def ratio(peak):
        return scanner._profit_lock_floor(100.0, "LONG", 10, peak)[2]

    # The operator's breathing schedule (2026-08-08). It replaced a flat 20-25% band that
    # was far too tight at the small end: on an index option a Rs.600 peak is a handful of
    # premium points, and a 20-25% leash closed trades on ordinary bid/ask breathing.
    #   <= Rs.1,000  50%      Rs.1,000-2,000  35%->30%
    #   Rs.2,000-3,000  25%->20%            Rs.3,000+  15%->10%
    # The arm moved to Rs.250 on the non-scalp path (2026-08-11: over 12 live sessions
    # the median trade peaked at Rs.274 while the median non-scalp arm computed to
    # Rs.868, so only 4 of 59 rows ever reached their own lock). These calls pass no
    # initial risk, so the base is the whole arm.
    assert ratio(200.0) == 0.0            # below the Rs.250 arm -> not armed, no lock
    assert ratio(400.0) == 0.50           # armed, and inside the first rung
    assert ratio(500.0) == 0.50           # 50% breathing
    assert ratio(999.0) == 0.50
    assert ratio(1000.0) == 0.50          # 'up to Rs.1,000' is inclusive -> 50%
    assert ratio(1001.0) == pytest.approx(0.65, abs=1e-3)  # 35% starts just above
    assert ratio(1500.0) == 0.675
    assert ratio(2000.0) == 0.75          # 25% breathing
    assert ratio(2500.0) == 0.775
    assert ratio(3000.0) == 0.85          # 15% breathing
    # The major-win rung used to run to 1.00 here. A ratio of exactly 1.00 puts the
    # protective floor ON the peak price, and price must leave the peak for a later tick
    # to exist — so the floor is breached by the first one and the trailing lock becomes
    # a fixed take-profit at Rs.5000. test_option_trend_major_win_cap_keeps_nifty_pe_
    # runner_breathing below already patched this for trend options via a 0.95 cap; the
    # ceiling now applies to everything.
    assert ratio(4000.0) == 0.875         # 12.5% breathing, mid-ramp
    assert ratio(5000.0) == 0.90          # 10% breathing, the tight end
    assert ratio(50000.0) == 0.90         # and it stays a lock, however large the win


def test_option_trend_major_win_cap_keeps_nifty_pe_runner_breathing():
    scanner = Scanner.__new__(Scanner)
    settings = scanner_module.get_settings()
    old_cap = getattr(settings, "option_major_win_trend_lock_ratio_cap", 0.95)
    settings.option_major_win_trend_lock_ratio_cap = 0.95
    trade = _option_candidate(
        price=167.05,
        lot_size=65,
        grade="A",
        timeframe="5min",
        runner_mode=True,
    )

    try:
        lock_stop, reason, ratio = scanner._profit_lock_floor(
            entry_price=trade.price,
            direction="LONG",
            qty=trade.lots * trade.lot_size,
            peak_pnl=4858.75,
            timeframe=trade.timeframe,
            trade_like=trade,
        )
    finally:
        settings.option_major_win_trend_lock_ratio_cap = old_cap

    assert reason == "MAJOR_WIN_GUARD"
    assert ratio == 0.8965  # trend cap 0.95 no longer binds below the 0.90 ceiling
    # Clamped to the 0.80 band ceiling instead of the old 0.95 trend-option cap.
    assert lock_stop == pytest.approx(234.063, abs=0.01)
    assert 241.27 > lock_stop


def test_scalp_profit_lock_uses_initial_risk_floor():
    from config.settings import get_settings

    scanner = Scanner.__new__(Scanner)
    settings = get_settings()
    original = getattr(settings, "scalp_profit_lock_min_r_multiple", 0.50)
    settings.scalp_profit_lock_min_r_multiple = 0.50

    try:
        lock_stop, reason, ratio = scanner._profit_lock_floor(
            entry_price=100.0,
            direction="LONG",
            qty=10,
            peak_pnl=1200.0,
            scalp_mode=True,
            initial_risk_rs=3000.0,
        )
        assert (lock_stop, reason, ratio) == (0.0, "", 0.0)

        lock_stop, reason, ratio = scanner._profit_lock_floor(
            entry_price=100.0,
            direction="LONG",
            qty=10,
            peak_pnl=1600.0,
            scalp_mode=True,
            initial_risk_rs=3000.0,
        )
        assert reason == "SMART_PROFIT_LOCK"
        assert ratio > 0
        assert lock_stop > 100.0
    finally:
        settings.scalp_profit_lock_min_r_multiple = original


def test_5min_option_scalp_lock_uses_scalp_r_floor():
    settings = scanner_module.get_settings()
    old_profit = getattr(settings, "profit_lock_min_r_multiple", 1.0)
    old_scalp = getattr(settings, "scalp_profit_lock_min_r_multiple", 0.50)
    settings.profit_lock_min_r_multiple = 1.0
    settings.scalp_profit_lock_min_r_multiple = 0.50
    scanner = Scanner.__new__(Scanner)

    try:
        lock_stop, reason, ratio = scanner._profit_lock_floor(
            entry_price=100.0,
            direction="LONG",
            qty=10,
            peak_pnl=1600.0,
            scalp_mode=True,
            initial_risk_rs=3000.0,
            timeframe="5min",
        )
        assert reason == "SMART_PROFIT_LOCK"
        assert ratio > 0
        assert lock_stop > 100.0

        lock_stop, reason, ratio = scanner._profit_lock_floor(
            entry_price=100.0,
            direction="LONG",
            qty=10,
            peak_pnl=1600.0,
            scalp_mode=True,
            initial_risk_rs=3000.0,
            timeframe="1min",
        )
        assert reason == "SMART_PROFIT_LOCK"
        assert ratio > 0
        assert lock_stop > 100.0
    finally:
        settings.profit_lock_min_r_multiple = old_profit
        settings.scalp_profit_lock_min_r_multiple = old_scalp


def test_missing_initial_stop_derives_configured_option_risk_before_locking():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    candidate = _option_candidate(
        direction="LONG",
        price=1197.00,
        stop=1205.55,
        initial_stop=0.0,
        lot_size=25,
        timeframe="5min",
        peak_pnl=700.0,
        scalp_lock_mode=True,
    )

    risk = scanner._trade_like_initial_risk_rs(candidate, qty=25, inst_mult=1.0)

    assert risk == pytest.approx(1197.00 * 0.15 * 25)
    assert candidate.initial_stop == pytest.approx(1197.00 * 0.85)

    lock_stop, reason, ratio = scanner._profit_lock_floor(
        entry_price=candidate.price,
        direction="LONG",
        qty=25,
        peak_pnl=700.0,
        scalp_mode=True,
        initial_risk_rs=risk,
        timeframe="5min",
    )
    assert (lock_stop, reason, ratio) == (0.0, "", 0.0)


def test_exit_rows_inherit_raw_entry_audit_metadata():
    scanner = Scanner.__new__(Scanner)
    source_ts = datetime(2026, 7, 1, 9, 45)
    entry = SimpleNamespace(
        signal_timestamp=source_ts,
        entry_timestamp=datetime(2026, 7, 1, 9, 46),
        source_engine="utbot+ema",
        source_engines=["utbot", "ema"],
        quad_union_entry=True,
        raw_union_entry=True,
        scalp_lock_mode=False,
        market_regime="TRENDING_UP",
        intelligence_score=0.72,
        location_decision="ALLOW",
        adx_value=31.0,
        initial_stop=660.83,
    )
    exit_row = SimpleNamespace(signal_timestamp=datetime(2026, 7, 1, 9, 50))

    scanner._inherit_exit_entry_metadata(exit_row, entry)

    assert exit_row.signal_timestamp == source_ts
    assert exit_row.entry_timestamp == entry.entry_timestamp
    assert exit_row.source_engine == "utbot+ema"
    assert exit_row.source_engines == ["utbot", "ema"]
    assert exit_row.raw_union_entry is True
    assert exit_row.scalp_lock_mode is False
    assert exit_row.market_regime == "TRENDING_UP"
    assert exit_row.intelligence_score == 0.72
    assert exit_row.location_decision == "ALLOW"
    assert exit_row.adx_value == 31.0
    assert exit_row.initial_stop == 660.83


def test_active_session_entries_match_exits_by_lifecycle_not_timeframe_direction():
    from engine.signal_processor import TradeCandidate

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._ensure_session_candidate_day = lambda: None
    scanner._candidate_matches_live_session_day = lambda candidate: True
    scanner._candidate_chronology_valid = lambda candidate: True
    scanner._normalize_loaded_exit_candidate = lambda candidate: None
    scanner._persist_session_trade_candidates = lambda: None

    def row(ts, *, action="ENTRY", exit_ts=None, strike=76700):
        candidate = TradeCandidate(
            instrument="SENSEX",
            direction="LONG",
            price=777.45,
            stop=660.83,
            target=964.04,
            lots=1,
            lot_size=20,
            grade="B",
            confidence=0.49,
            timeframe="5min",
            inst_type="OPT",
            option_type="CE",
            atm_strike=strike,
            multiplier=0.56,
            trading_symbol=f"SENSEX{strike}CE",
            symbol_token=str(strike),
            signal_timestamp=ts,
            action=action,
            source_engine="utbot",
            source_engines=["utbot"],
            quad_union_entry=True,
            raw_union_entry=True,
        )
        if exit_ts is not None:
            setattr(candidate, "exit_timestamp", exit_ts)
            setattr(candidate, "source_signal_timestamp", ts)
            candidate.exit_reason = "SUPERSEDED_SIGNAL"
        return candidate

    old_entry_ts = datetime(2026, 7, 1, 10, 50)
    new_entry_ts = datetime(2026, 7, 1, 11, 0)
    supersede_ts = datetime(2026, 7, 1, 11, 0)
    old_entry = row(old_entry_ts, strike=76700)
    old_exit = row(old_entry_ts, action="EXIT", exit_ts=supersede_ts, strike=76700)
    new_entry = row(new_entry_ts, strike=76700)

    scanner._session_trade_candidates = {
        "SENSEX": {
            scanner._candidate_history_key(old_entry): old_entry,
            scanner._candidate_history_key(old_exit): old_exit,
            scanner._candidate_history_key(new_entry): new_entry,
        }
    }

    active = scanner._active_session_entry_candidates(
        "SENSEX",
        as_of=datetime(2026, 7, 1, 11, 45),
    )

    assert [candidate.signal_timestamp for candidate in active] == [new_entry_ts]


def _supersede_scanner():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.config = {"indices": {"BANKNIFTY": {}}}
    scanner._session_trade_candidates = {}
    scanner._latest_exit_candidates = {}
    scanner._last_opposite_exit_signal_time = {}
    scanner._ensure_session_candidate_day = lambda: None
    scanner._normalize_loaded_exit_candidate = lambda candidate: False
    scanner._persist_session_trade_candidates = lambda: None
    scanner._refresh_trade_candidates = lambda candidates, cfg: None
    scanner._get_live_premium_cached = lambda candidate: getattr(candidate, "current_price", 0.0)
    scanner._candidate_exit_resolution = lambda previous, px, reason, text: (px, reason, text)
    scanner._candidate_net_pnl = lambda previous, px: round((px - previous.price) * previous.lot_size, 2)
    scanner._inherit_exit_entry_metadata = lambda exit_candidate, previous: None
    scanner.log_event = lambda *args, **kwargs: None
    return scanner


def _supersede_candidate(direction: str, signal_ts: datetime, *, option_type: str):
    return TradeCandidate(
        instrument="BANKNIFTY",
        direction=direction,
        price=1175.0,
        stop=1011.0,
        target=1905.0,
        lots=1,
        lot_size=30,
        grade="A+",
        confidence=0.95,
        timeframe="5min",
        inst_type="OPT",
        option_type=option_type,
        atm_strike=57900,
        multiplier=1.0 if direction == "LONG" else -1.0,
        trading_symbol=f"BANKNIFTY57900{option_type}",
        symbol_token="57900",
        rr=4.0,
        score=0.95,
        signal_timestamp=signal_ts,
        current_price=1175.0,
        action="ENTRY",
        reasons=[],
    )


def test_manual_dashboard_signal_row_exposes_exit_for_matching_open_trade():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False
    signal_ts = datetime(2026, 7, 6, 11, 33)
    candidate = _supersede_candidate("LONG", signal_ts, option_type="CE")
    candidate.accepted_by_gate = True
    trade = SimpleNamespace(
        id="T0001_BANKNIFTY",
        status="OPEN",
        exec_type="M",
        instrument="BANKNIFTY",
        timeframe="5min",
        direction="LONG",
        trading_symbol="BANKNIFTY57900CE",
        option_type="CE",
        atm_strike=57900,
        signal_time=signal_ts,
        entry_time=signal_ts + timedelta(seconds=20),
        current_price=1201.5,
        entry_price=1175.0,
        pending_exit=False,
        execution_status="OPEN",
    )
    scanner.trades = SimpleNamespace(open_trades={trade.id: trade})

    state = scanner._manual_signal_exit_state(candidate)

    assert state["manual_entry_executed"] is True
    assert state["manual_exit_trade_id"] == trade.id
    assert state["manual_exit_price"] == 1201.5
    assert state["manual_exit_enabled"] is True


def test_manual_dashboard_signal_match_respects_lifecycle_when_signal_time_exists():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = False
    signal_ts = datetime(2026, 7, 6, 11, 33)
    candidate = _supersede_candidate("LONG", signal_ts, option_type="CE")
    trade = SimpleNamespace(
        id="T0002_BANKNIFTY",
        status="OPEN",
        exec_type="M",
        instrument="BANKNIFTY",
        timeframe="5min",
        direction="LONG",
        trading_symbol="BANKNIFTY57900CE",
        option_type="CE",
        atm_strike=57900,
        signal_time=signal_ts + timedelta(minutes=5),
        entry_time=signal_ts + timedelta(minutes=5, seconds=15),
        current_price=1201.5,
        entry_price=1175.0,
        pending_exit=False,
        execution_status="OPEN",
    )
    scanner.trades = SimpleNamespace(open_trades={trade.id: trade})

    assert scanner._manual_signal_exit_state(candidate) == {}


def test_dashboard_close_trade_uses_trade_manager_and_publishes_refresh():
    scanner = Scanner.__new__(Scanner)
    published = {"count": 0, "notified": 0, "closed": None}
    trade = SimpleNamespace(
        id="T0003_BANKNIFTY",
        status="OPEN",
        current_price=1212.25,
        entry_price=1175.0,
        pending_exit=False,
        execution_status="OPEN",
    )

    class Trades:
        open_trades = {trade.id: trade}

        def close_trade(self, trade_id, price, reason):
            published["closed"] = (trade_id, price, reason)
            return True

    async def publish():
        published["count"] += 1

    scanner.trades = Trades()
    scanner.log_event = lambda *args, **kwargs: None
    scanner._publish_trade_payload_now = publish
    scanner._notify_trade_event = lambda: published.__setitem__("notified", published["notified"] + 1)

    result = asyncio.run(scanner.close_dashboard_trade(trade.id, 0.0))

    assert result["status"] == "submitted"
    assert result["exit_price"] == 1212.25
    assert published["closed"] == (trade.id, 1212.25, "MANUAL")
    assert published["count"] == 1
    assert published["notified"] == 1


def test_same_direction_refresh_does_not_create_superseded_exit():
    scanner = _supersede_scanner()
    old_ts = datetime(2026, 7, 1, 12, 30)
    new_ts = datetime(2026, 7, 1, 13, 10)
    old_entry = _supersede_candidate("LONG", old_ts, option_type="CE")
    old_entry.price = 1189.9
    old_entry.current_price = 1175.0
    old_entry.entry_timestamp = datetime(2026, 7, 1, 12, 30, 59)
    old_entry.accepted_by_gate = True
    new_entry = _supersede_candidate("LONG", new_ts, option_type="CE")

    scanner._session_trade_candidates = {
        "BANKNIFTY": {scanner._candidate_history_key(old_entry): old_entry}
    }

    scanner._close_superseded_session_entries([new_entry])

    rows = list(scanner._session_trade_candidates["BANKNIFTY"].values())
    assert all(getattr(row, "action", "ENTRY") != "EXIT" for row in rows)
    assert all(getattr(row, "exit_reason", "") != "SUPERSEDED_SIGNAL" for row in rows)
    assert new_entry.signal_timestamp == old_ts
    assert new_entry.entry_timestamp == old_entry.entry_timestamp
    assert new_entry.price == old_entry.price


def test_same_direction_refresh_is_absorbed_before_new_entry_path():
    scanner = _supersede_scanner()
    old_ts = datetime(2026, 7, 1, 12, 30)
    new_ts = datetime(2026, 7, 1, 13, 10)
    old_entry = _supersede_candidate("LONG", old_ts, option_type="CE")
    old_entry.price = 1189.9
    old_entry.entry_timestamp = datetime(2026, 7, 1, 12, 30, 59)
    old_entry.accepted_by_gate = True
    new_entry = _supersede_candidate("LONG", new_ts, option_type="CE")

    scanner._session_trade_candidates = {
        "BANKNIFTY": {scanner._candidate_history_key(old_entry): old_entry}
    }

    assert scanner._absorb_same_direction_session_refresh(new_entry) is True
    assert new_entry.signal_timestamp == old_ts
    assert new_entry.entry_timestamp == old_entry.entry_timestamp
    assert new_entry.price == old_entry.price
    rows = list(scanner._session_trade_candidates["BANKNIFTY"].values())
    assert len(rows) == 1
    assert all(getattr(row, "action", "ENTRY") != "EXIT" for row in rows)


def test_opposite_direction_signal_still_supersedes_active_entry():
    scanner = _supersede_scanner()
    old_ts = datetime(2026, 7, 1, 12, 30)
    new_ts = datetime(2026, 7, 1, 13, 10)
    old_entry = _supersede_candidate("LONG", old_ts, option_type="CE")
    new_entry = _supersede_candidate("SHORT", new_ts, option_type="PE")

    scanner._session_trade_candidates = {
        "BANKNIFTY": {scanner._candidate_history_key(old_entry): old_entry}
    }

    scanner._close_superseded_session_entries([new_entry])

    rows = list(scanner._session_trade_candidates["BANKNIFTY"].values())
    exits = [row for row in rows if getattr(row, "action", "ENTRY") == "EXIT"]
    assert len(exits) == 1
    assert exits[0].exit_reason == "SUPERSEDED_SIGNAL"
    assert exits[0].exit_timestamp == new_ts


def test_legacy_same_direction_superseded_rows_are_scrubbed_from_session_ledger():
    scanner = _supersede_scanner()
    old_ts = datetime(2026, 7, 1, 12, 30)
    refresh_ts = datetime(2026, 7, 1, 13, 10)
    final_exit_ts = datetime(2026, 7, 1, 13, 17)
    old_entry = _supersede_candidate("LONG", old_ts, option_type="CE")
    old_entry.accepted_by_gate = True
    bad_exit = _supersede_candidate("LONG", old_ts, option_type="CE")
    bad_exit.action = "EXIT"
    bad_exit.exit_reason = "SUPERSEDED_SIGNAL"
    bad_exit.exit_timestamp = refresh_ts
    bad_exit.source_signal_timestamp = old_ts
    refresh_entry = _supersede_candidate("LONG", refresh_ts, option_type="CE")
    refresh_entry.accepted_by_gate = True
    final_exit = _supersede_candidate("LONG", refresh_ts, option_type="CE")
    final_exit.action = "EXIT"
    final_exit.exit_reason = "STOP_HIT"
    final_exit.exit_timestamp = final_exit_ts
    final_exit.source_signal_timestamp = refresh_ts

    scanner._session_trade_candidates = {
        "BANKNIFTY": {
            scanner._candidate_history_key(old_entry): old_entry,
            scanner._candidate_history_key(bad_exit): bad_exit,
            scanner._candidate_history_key(refresh_entry): refresh_entry,
            scanner._candidate_history_key(final_exit): final_exit,
        }
    }

    rows = scanner._get_session_trade_candidates("BANKNIFTY")

    assert all(getattr(row, "exit_reason", "") != "SUPERSEDED_SIGNAL" for row in rows)
    assert all(getattr(row, "signal_timestamp", None) != old_ts for row in rows)
    assert any(getattr(row, "signal_timestamp", None) == refresh_ts for row in rows)
    assert any(getattr(row, "exit_reason", "") == "STOP_HIT" for row in rows)


def test_soft_intel_profit_protect_tightens_5min_to_85_percent():
    scanner = Scanner.__new__(Scanner)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": True,
        "trend_supportive": False,
        "trend_votes": 0,
        "reason": "fast adverse 1m reversal",
    }
    trade = SimpleNamespace(
        instrument="BANKNIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
    )
    intel = {
        "aggregate": {"score": -20.0, "signal": "HOLD"},
        "pcr": {"signal": "NEUTRAL"},
        "oi": {"signal": "NEUTRAL"},
        "order_flow": {"signal": "NEUTRAL", "ratio": 1.0},
    }

    stop, ratio, reason = scanner._soft_intel_profit_protect_floor(
        trade,
        "LONG",
        qty=10,
        inst_mult=1.0,
        current_price=110.0,
        current_pnl=800.0,
        peak_pnl=1000.0,
        intel_result=intel,
        latest_time=datetime(2026, 6, 16, 13, 30),
    )

    assert ratio == 0.85
    assert stop == 185.0
    assert "soft bearish intel" in reason


def test_soft_intel_profit_protect_15min_uses_5min_confirmation():
    scanner = Scanner.__new__(Scanner)
    seen = {}

    def context(_trade, _price, _latest_time, timeframe="1min"):
        seen["timeframe"] = timeframe
        return {
            "has_context": True,
            "fast_reversal": False,
            "trend_supportive": False,
            "trend_votes": 1,
            "reason": "5m trend no longer supportive",
        }

    scanner._profit_guard_reversal_context = context
    trade = SimpleNamespace(
        instrument="BANKNIFTY",
        timeframe="15min",
        direction="SHORT",
        price=200.0,
    )
    intel = {
        "aggregate": {"score": 22.0, "signal": "HOLD"},
        "pcr": {"signal": "NEUTRAL"},
        "oi": {"signal": "NEUTRAL"},
        "order_flow": {"signal": "NEUTRAL", "ratio": 1.0},
    }

    stop, ratio, _ = scanner._soft_intel_profit_protect_floor(
        trade,
        "SHORT",
        qty=10,
        inst_mult=1.0,
        current_price=190.0,
        current_pnl=850.0,
        peak_pnl=1000.0,
        intel_result=intel,
        latest_time=datetime(2026, 6, 16, 13, 30),
    )

    assert seen["timeframe"] == "5min"
    assert ratio == 0.80
    assert stop == 120.0


def test_timeframe_intel_cache_reuses_5min_result():
    scanner = Scanner.__new__(Scanner)
    scanner.config = {"indices": {"NIFTY": {"strike_interval": 50}}}
    scanner._intel_cache = {}
    scanner._intel_cache_time = {}
    scanner._last_intel_fetch = {}
    scanner._chain_cache = {}
    scanner._timeframe_regimes = {}
    scanner.latest_regimes = {}
    scanner.mode = "REAL"
    scanner._live_analysis_window = lambda df, _tf: df
    rows = 35
    candles = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(rows)],
            "high": [102.0 + i for i in range(rows)],
            "low": [99.0 + i for i in range(rows)],
            "close": [101.0 + i for i in range(rows)],
            "volume": [1000 + i for i in range(rows)],
        },
        index=pd.date_range("2026-06-16 09:15", periods=rows, freq="5min"),
    )
    scanner.candles = SimpleNamespace(
        get_candles=lambda _instrument, _timeframe: candles,
        get_latest_price=lambda _instrument: 135.0,
    )
    scanner._get_option_chain_cached = lambda *_args, **_kwargs: (None, {})
    scanner._get_depth_context = lambda *_args, **_kwargs: {}
    scanner.expiry = SimpleNamespace(get_dte=lambda *_args: 7)
    calls = []

    def analyze(**kwargs):
        calls.append(kwargs)
        return {
            "aggregate": {"score": 0.0, "timeframe": kwargs["timeframe"]},
            "regime": {"regime": "TRENDING_UP", "ready_for_grading": True, "candle_count": rows},
        }

    scanner.intel = SimpleNamespace(analyze=analyze)

    first = scanner._get_timeframe_intel("NIFTY", "5min")
    second = scanner._get_timeframe_intel("NIFTY", "5min")

    assert first is second
    assert len(calls) == 1
    assert calls[0]["timeframe"] == "5min"


def test_timeframe_intel_cache_keeps_15min_separate_from_5min():
    scanner = Scanner.__new__(Scanner)
    scanner.config = {"indices": {"NIFTY": {"strike_interval": 50}}}
    scanner._intel_cache = {}
    scanner._intel_cache_time = {}
    scanner._last_intel_fetch = {}
    scanner._chain_cache = {}
    scanner._timeframe_regimes = {}
    scanner.latest_regimes = {}
    scanner.mode = "REAL"
    scanner._live_analysis_window = lambda df, _tf: df

    def frame(freq, start):
        rows = 35
        return pd.DataFrame(
            {
                "open": [start + i for i in range(rows)],
                "high": [start + i + 2 for i in range(rows)],
                "low": [start + i - 1 for i in range(rows)],
                "close": [start + i + 1 for i in range(rows)],
                "volume": [1000 + i for i in range(rows)],
            },
            index=pd.date_range("2026-06-16 09:15", periods=rows, freq=freq),
        )

    frames = {"1min": frame("1min", 90.0), "5min": frame("5min", 100.0), "15min": frame("15min", 200.0)}
    scanner.candles = SimpleNamespace(
        get_candles=lambda _instrument, timeframe: frames[timeframe],
        get_latest_price=lambda _instrument: 235.0,
    )
    scanner._get_option_chain_cached = lambda *_args, **_kwargs: (None, {})
    scanner._get_depth_context = lambda *_args, **_kwargs: {}
    scanner.expiry = SimpleNamespace(get_dte=lambda *_args: 7)
    calls = []

    def analyze(**kwargs):
        calls.append(kwargs)
        regime = "VOLATILE" if kwargs["timeframe"] == "5min" else "TRENDING_UP"
        return {
            "aggregate": {"score": 0.0, "timeframe": kwargs["timeframe"]},
            "regime": {"regime": regime, "ready_for_grading": True, "candle_count": 35},
        }

    scanner.intel = SimpleNamespace(analyze=analyze)

    five = scanner._get_timeframe_intel("NIFTY", "5min")
    fifteen = scanner._get_timeframe_intel("NIFTY", "15min")
    fifteen_again = scanner._get_timeframe_intel("NIFTY", "15min")

    assert five["aggregate"]["timeframe"] == "5min"
    assert fifteen["aggregate"]["timeframe"] == "15min"
    assert fifteen_again is fifteen
    assert [call["timeframe"] for call in calls] == ["5min", "15min"]
    assert scanner.latest_regimes["NIFTY"] == "VOLATILE"
    assert scanner._timeframe_regimes["NIFTY_15min"] == "TRENDING_UP"


def test_trade_regime_prefers_trade_timeframe_over_legacy_5min():
    scanner = Scanner.__new__(Scanner)
    scanner.latest_regimes = {"NIFTY": "VOLATILE"}
    scanner._timeframe_regimes = {"NIFTY_15min": "TRENDING_DOWN", "NIFTY_5min": "VOLATILE"}

    trade = SimpleNamespace(instrument="NIFTY", timeframe="15min")

    assert scanner._get_trade_regime(trade) == "TRENDING_DOWN"


def test_option_breathing_room_blocks_premature_profit_side_stop():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    entry_time = datetime(2026, 7, 1, 11, 45, 55)
    candidate = _option_candidate(
        direction="LONG",
        price=1197.00,
        stop=1205.55,
        initial_stop=0.0,
        lot_size=25,
        timeframe="5min",
        signal_timestamp=entry_time,
        peak_pnl=700.0,
    )
    proposed_stop = candidate.price + 26.0

    adjusted_stop, breathing, clamped = scanner._apply_option_stop_breathing_room(
        candidate,
        proposed_stop,
        latest_time=entry_time + timedelta(minutes=1),
        regime="VOLATILE",
    )

    assert clamped is True
    assert breathing > 0
    assert adjusted_stop < candidate.price
    assert candidate.initial_stop == pytest.approx(1197.00 * 0.85)


def test_option_breathing_room_allows_profit_stop_after_proved_move():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    entry_time = datetime(2026, 7, 1, 11, 45, 55)
    candidate = _option_candidate(
        direction="LONG",
        price=100.0,
        stop=85.0,
        initial_stop=85.0,
        lot_size=25,
        timeframe="5min",
        signal_timestamp=entry_time,
        peak_pnl=1500.0,
    )
    proposed_stop = 120.0

    adjusted_stop, _, clamped = scanner._apply_option_stop_breathing_room(
        candidate,
        proposed_stop,
        latest_time=entry_time + timedelta(minutes=6),
        regime="TRENDING_UP",
    )

    assert adjusted_stop == proposed_stop
    assert clamped is False
    assert scanner._candidate_net_pnl(candidate, adjusted_stop) > 0


def test_option_repaint_flicker_defers_until_close_inside_breathing_room():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 7.0
    settings = scanner_module.get_settings()
    old_defer = getattr(settings, "option_repaint_abort_defer_until_close", True)
    old_mult = getattr(settings, "option_repaint_abort_severe_breathing_mult", 1.25)
    settings.option_repaint_abort_defer_until_close = True
    settings.option_repaint_abort_severe_breathing_mult = 1.25
    try:
        signal_time = datetime(2026, 6, 29, 11, 0)
        candidate = _option_candidate(
            direction="SHORT",
            price=490.30,
            current_price=466.50,
            stop=455.98,
            initial_stop=455.98,
            lot_size=20,
            timeframe="15min",
            signal_timestamp=signal_time,
        )

        defer, reason = scanner._should_defer_option_repaint_abort(
            candidate,
            466.50,
            signal_time + timedelta(minutes=1, seconds=29),
            is_candle_closed=False,
            regime="VOLATILE",
        )

        assert defer is True
        assert "deferred until candle close" in reason
    finally:
        settings.option_repaint_abort_defer_until_close = old_defer
        settings.option_repaint_abort_severe_breathing_mult = old_mult


def test_option_repaint_abort_not_deferred_after_severe_premium_loss():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 7.0
    signal_time = datetime(2026, 6, 29, 11, 0)
    candidate = _option_candidate(
        direction="SHORT",
        price=490.30,
        current_price=430.00,
        stop=455.98,
        initial_stop=455.98,
        lot_size=20,
        timeframe="15min",
        signal_timestamp=signal_time,
    )

    defer, reason = scanner._should_defer_option_repaint_abort(
        candidate,
        430.00,
        signal_time + timedelta(minutes=1, seconds=29),
        is_candle_closed=False,
        regime="VOLATILE",
    )

    assert defer is False
    assert "severe repaint threshold" in reason


def test_option_repaint_abort_not_deferred_after_persistent_vanish():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 7.0
    settings = scanner_module.get_settings()
    old_defer = getattr(settings, "option_repaint_abort_defer_until_close", True)
    old_mult = getattr(settings, "option_repaint_abort_severe_breathing_mult", 1.25)
    old_checks = getattr(settings, "option_repaint_abort_max_deferred_checks", 2)
    old_seconds = getattr(settings, "option_repaint_abort_max_deferred_seconds", 55.0)
    settings.option_repaint_abort_defer_until_close = True
    settings.option_repaint_abort_severe_breathing_mult = 2.5
    settings.option_repaint_abort_max_deferred_checks = 2
    settings.option_repaint_abort_max_deferred_seconds = 55.0
    try:
        signal_time = datetime(2026, 6, 29, 10, 45)
        candidate = _option_candidate(
            direction="SHORT",
            price=190.55,
            current_price=185.05,
            stop=170.30,
            initial_stop=170.30,
            lot_size=50,
            timeframe="15min",
            signal_timestamp=signal_time,
        )
        candidate._repaint_absent_checks = 3
        candidate._repaint_first_absent_at = signal_time + timedelta(minutes=9)

        defer, reason = scanner._should_defer_option_repaint_abort(
            candidate,
            185.05,
            signal_time + timedelta(minutes=9, seconds=41),
            is_candle_closed=False,
            regime="BREAKOUT_DOWN",
        )

        assert defer is False
        assert "persistent repaint" in reason
    finally:
        settings.option_repaint_abort_defer_until_close = old_defer
        settings.option_repaint_abort_severe_breathing_mult = old_mult
        settings.option_repaint_abort_max_deferred_checks = old_checks
        settings.option_repaint_abort_max_deferred_seconds = old_seconds


def test_intel_exit_defers_small_pullback_with_supportive_price_action():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 10.0
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 3,
        "reason": "same-trend shallow pullback",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24050.0)
    candidate = _option_candidate(
        direction="LONG",
        price=500.0,
        current_price=485.0,
        stop=450.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )
    candidate.inst_type = "OPT"

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        485.0,
        datetime(2026, 6, 29, 11, 20),
        "OI_PCR_REVERSAL bearish votes=3 PCR=1.20 OFR=0.70",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -30.0}},
    )

    assert defer is True
    assert "configured normal-noise room" in reason


def test_intel_exit_allows_minimum_loss_after_premium_noise_room_breaks():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 10.0
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 3,
        "reason": "same-trend shallow pullback",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24050.0)
    candidate = _option_candidate(
        direction="LONG",
        price=500.0,
        current_price=420.0,
        stop=450.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )
    candidate.inst_type = "OPT"

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        420.0,
        datetime(2026, 6, 29, 11, 20),
        "OI_PCR_REVERSAL bearish votes=3 PCR=1.20 OFR=0.70",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -30.0}},
    )

    assert defer is False
    assert reason == ""


def test_intel_exit_defers_index_move_inside_point_one_percent_ltp_noise():
    scanner = Scanner.__new__(Scanner)
    scanner.futures_sl_pct = 0.2
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 3,
        "reason": "same-trend index noise",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 23978.0)
    candidate = _option_candidate(
        inst_type="FUT",
        direction="LONG",
        price=24000.0,
        current_price=23978.0,
        stop=23960.0,
        multiplier=1.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        23978.0,
        datetime(2026, 6, 29, 11, 20),
        "Intelligence flip score -50.0",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -50.0}},
    )

    assert defer is True
    assert "normal_noise" in reason


def test_intel_exit_defers_winner_inside_profit_lock_tier_room():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 10.0
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 3,
        "reason": "same-trend shallow pullback",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24050.0)
    candidate = _option_candidate(
        direction="LONG",
        price=500.0,
        current_price=548.0,
        stop=450.0,
        target=650.0,
        peak_pnl=1800.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )
    candidate.inst_type = "OPT"

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        548.0,
        datetime(2026, 6, 29, 11, 20),
        "OI_PCR_REVERSAL bearish votes=3 PCR=1.20 OFR=0.70",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -50.0}},
    )

    assert defer is True
    assert "protected profit room" in reason


def test_intel_exit_allows_confirmed_fast_reversal():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 10.0
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": True,
        "trend_supportive": False,
        "trend_votes": 0,
        "reason": "fast adverse reversal",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24050.0)
    candidate = _option_candidate(
        direction="LONG",
        price=500.0,
        current_price=485.0,
        stop=450.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )
    candidate.inst_type = "OPT"

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        485.0,
        datetime(2026, 6, 29, 11, 20),
        "OI_PCR_REVERSAL bearish votes=3 PCR=1.20 OFR=0.70",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -70.0}},
    )

    assert defer is False
    assert reason == ""


def test_intel_exit_allows_severe_premium_damage_even_without_context():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 10.0
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": False,
        "reason": "no context",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24050.0)
    candidate = _option_candidate(
        direction="LONG",
        price=500.0,
        current_price=420.0,
        stop=450.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )
    candidate.inst_type = "OPT"

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        420.0,
        datetime(2026, 6, 29, 11, 20),
        "Intelligence flip score -75.0",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -75.0}},
    )

    assert defer is False
    assert reason == ""


def test_intel_exit_cuts_recovered_loss_near_entry_when_context_weakens():
    scanner = Scanner.__new__(Scanner)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": False,
        "trend_votes": 0,
        "reason": "trend no longer supportive",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24050.0)
    candidate = _option_candidate(
        direction="LONG",
        price=500.0,
        current_price=495.0,
        stop=450.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        495.0,
        datetime(2026, 6, 29, 11, 20),
        "OI_PCR_REVERSAL bearish votes=1 PCR=1.10 OFR=0.80",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -30.0}},
    )

    assert defer is False
    assert reason == ""


def test_intel_exit_protects_small_profit_after_cost_on_strong_reversal():
    scanner = Scanner.__new__(Scanner)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": True,
        "trend_supportive": False,
        "trend_votes": 0,
        "reason": "fast adverse reversal",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24050.0)
    candidate = _option_candidate(
        direction="LONG",
        price=500.0,
        current_price=503.0,
        stop=450.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        503.0,
        datetime(2026, 6, 29, 11, 20),
        "OI_PCR_REVERSAL bearish votes=3 PCR=1.20 OFR=0.70",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -70.0}},
    )

    assert defer is False
    assert reason == ""


def test_intel_exit_enforces_profit_guard_tier_floor():
    scanner = Scanner.__new__(Scanner)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 3,
        "reason": "same-trend pullback",
    }
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24050.0)
    candidate = _option_candidate(
        direction="LONG",
        price=500.0,
        current_price=528.0,
        stop=450.0,
        peak_pnl=1800.0,
        timeframe="15min",
        signal_timestamp=datetime(2026, 6, 29, 11, 0),
    )

    defer, reason = scanner._should_defer_option_intel_exit(
        candidate,
        528.0,
        datetime(2026, 6, 29, 11, 20),
        "OI_PCR_REVERSAL bearish votes=1 PCR=1.10 OFR=0.80",
        regime="VOLATILE",
        intel_result={"aggregate": {"score": -20.0}},
    )

    assert defer is False
    assert reason == ""


def test_option_breathing_room_uses_initial_risk_not_tiny_fixed_stop():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    entry_time = datetime(2026, 6, 15, 14, 36, 46)
    candidate = _option_candidate(
        direction="LONG",
        price=224.60,
        stop=216.60,
        initial_stop=190.91,
        timeframe="15min",
        signal_timestamp=entry_time,
        peak_pnl=123.50,
    )

    adjusted_stop, breathing, clamped = scanner._apply_option_stop_breathing_room(
        candidate,
        216.60,
        latest_time=entry_time + timedelta(minutes=6),
        regime="RANGING",
    )

    assert breathing >= 25.0
    assert clamped is True
    assert adjusted_stop <= 200.0


def test_choppy_scalp_smart_profit_lock_is_not_diluted_by_breathing_room():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    candidate = _option_candidate(
        direction="SHORT",
        inst_type="OPT",
        option_type="PE",
        price=707.05,
        stop=678.77,
        initial_stop=678.77,
        timeframe="5min",
        signal_timestamp=datetime(2026, 7, 8, 12, 5),
        peak_pnl=1554.0,
        scalp_lock_mode=True,
        market_regime="RANGING",
    )

    lock_stop, reason, ratio = scanner._profit_lock_floor(
        entry_price=candidate.price,
        direction="LONG",
        qty=30,
        peak_pnl=candidate.peak_pnl,
        scalp_mode=True,
        initial_risk_rs=abs(candidate.price - candidate.initial_stop) * 30,
        timeframe="5min",
        now=datetime(2026, 7, 8, 12, 12),
        trade_like=candidate,
    )
    adjusted_stop, breathing, clamped = scanner._apply_option_stop_breathing_room(
        candidate,
        lock_stop,
        latest_time=datetime(2026, 7, 8, 12, 12),
        regime="RANGING",
        profit_lock_reason=reason,
    )

    assert reason == "SMART_PROFIT_LOCK"
    # Rs.1,554 peak sits in the 35%->30% tier: 32.2% breathing.
    assert ratio == pytest.approx(0.6777)
    assert lock_stop == pytest.approx(742.15, abs=0.02)
    assert breathing > 0
    assert clamped is False
    assert adjusted_stop == pytest.approx(lock_stop)


def test_choppy_scalp_lock_needs_warning_pressure_below_base_peak():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    candidate = _option_candidate(
        direction="LONG",
        inst_type="OPT",
        option_type="CE",
        price=707.05,
        current_price=730.00,
        stop=678.77,
        initial_stop=678.77,
        timeframe="5min",
        signal_timestamp=datetime(2026, 7, 8, 12, 5),
        peak_pnl=900.0,
        scalp_lock_mode=True,
        market_regime="RANGING",
    )

    adjusted_stop, breathing, clamped = scanner._apply_option_stop_breathing_room(
        candidate,
        740.00,
        latest_time=datetime(2026, 7, 8, 12, 7),
        regime="RANGING",
        profit_lock_reason="SMART_PROFIT_LOCK",
    )

    assert breathing > 0
    assert clamped is True
    assert adjusted_stop < candidate.price


def test_quiet_adverse_ce_scalp_lock_bypasses_breathing_at_urgent_peak():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    candidate = _option_candidate(
        direction="LONG",
        inst_type="OPT",
        option_type="CE",
        price=100.0,
        current_price=116.0,
        stop=90.0,
        initial_stop=90.0,
        timeframe="5min",
        signal_timestamp=datetime(2026, 7, 8, 12, 5),
        peak_pnl=650.0,
        scalp_lock_mode=True,
        market_regime="RANGING",
        intelligence_score=-0.80,
        market_intelligence_context={
            "session_context": {"day_type": "RANGE_DAY", "confidence": 0.80, "range_vs_10day": 0.40},
            "aggregate": {"signal": "SELL", "score": -80.0},
        },
    )

    adjusted_stop, _, clamped = scanner._apply_option_stop_breathing_room(
        candidate,
        110.0,
        latest_time=datetime(2026, 7, 8, 12, 7),
        regime="RANGING",
        profit_lock_reason="SMART_PROFIT_LOCK",
    )

    assert clamped is False
    assert adjusted_stop == pytest.approx(110.0)


def test_pe_adaptive_lock_treats_bullish_intel_as_adverse_and_bearish_as_supportive():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    base_kwargs = dict(
        direction="SHORT",
        inst_type="OPT",
        option_type="PE",
        price=100.0,
        current_price=116.0,
        stop=90.0,
        initial_stop=90.0,
        timeframe="5min",
        signal_timestamp=datetime(2026, 7, 8, 12, 5),
        peak_pnl=650.0,
        scalp_lock_mode=True,
        market_regime="RANGING",
        market_intelligence_context={
            "session_context": {"day_type": "RANGE_DAY", "confidence": 0.80, "range_vs_10day": 0.40},
        },
    )

    supportive = _option_candidate(**base_kwargs, intelligence_score=-0.80)
    adjusted_supportive, _, clamped_supportive = scanner._apply_option_stop_breathing_room(
        supportive,
        110.0,
        latest_time=datetime(2026, 7, 8, 12, 7),
        regime="RANGING",
        profit_lock_reason="SMART_PROFIT_LOCK",
    )

    adverse = _option_candidate(**base_kwargs, intelligence_score=0.80)
    adjusted_adverse, _, clamped_adverse = scanner._apply_option_stop_breathing_room(
        adverse,
        110.0,
        latest_time=datetime(2026, 7, 8, 12, 7),
        regime="RANGING",
        profit_lock_reason="SMART_PROFIT_LOCK",
    )

    assert clamped_supportive is True
    assert adjusted_supportive < supportive.price
    assert clamped_adverse is False
    assert adjusted_adverse == pytest.approx(110.0)


def test_repaint_abort_reason_survives_when_price_is_below_stop():
    scanner = Scanner.__new__(Scanner)
    candidate = _option_candidate(direction="LONG", price=224.60, stop=216.60)

    exit_px, reason, text = scanner._candidate_exit_resolution(
        candidate,
        215.50,
        "REPAINT_ABORT",
        "Signal repainted intra-candle",
    )

    assert exit_px == 215.50
    assert reason == "REPAINT_ABORT"
    assert text == "Signal repainted intra-candle"


def _scanner_for_repaint_override_test(candidate, override_ctx):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.mtf = SimpleNamespace(engines={})
    scanner.candles = SimpleNamespace(get_candles=lambda *_args, **_kwargs: None)
    scanner._latest_exit_candidates = {}
    scanner._diag_counts = {}
    scanner._captured_exits = []
    scanner._events = []

    scanner._loaded_session_is_current_day = lambda: True
    scanner._active_session_entry_candidates = lambda _instrument: [candidate]
    scanner._ensure_trade_like_initial_stop = lambda c: float(getattr(c, "initial_stop", c.price - 30.0))
    scanner._get_trade_regime = lambda _c: "RANGING"
    scanner._get_trade_intel = lambda _c: None
    scanner._candidate_price_direction = lambda _c: "LONG"
    scanner._candidate_price_from_spot_level = lambda _c, spot: float(spot)
    scanner._candidate_trailing_stop_is_valid = lambda *_args, **_kwargs: False
    scanner._apply_option_stop_breathing_room = lambda _c, stop, **_kwargs: (stop, 0.0, False)
    scanner._candidate_gross_pnl = lambda _c, _price: -300.0
    scanner._candidate_net_pnl = lambda _c, _price: -402.5
    scanner._candidate_candle_extreme_price = lambda *_args, **_kwargs: 0.0
    scanner._trade_like_initial_risk_rs = lambda *_args, **_kwargs: 1200.0
    scanner._profit_lock_floor = lambda *_args, **_kwargs: (0.0, "", 0.0)
    scanner._profit_lock_min_peak = lambda *_args, **_kwargs: 999999.0
    scanner._peak_giveback_or_reversal_exit = lambda *_args, **_kwargs: (False, "", "")
    scanner._stagnation_exit_decision = lambda *_args, **_kwargs: (False, "")
    scanner._candidate_stop_hit = lambda *_args, **_kwargs: False
    scanner._candidate_target_hit = lambda *_args, **_kwargs: False
    scanner._live_signal_candle_has_closed = lambda *_args, **_kwargs: False
    scanner._repaint_signal_status = lambda *_args, **_kwargs: "VANISHED"
    scanner._post_entry_repaint_override_context = lambda *_args, **_kwargs: override_ctx
    scanner._repaint_abort_held_for_profit = lambda *_args, **_kwargs: False
    scanner._should_defer_option_repaint_abort = lambda *_args, **_kwargs: (False, "")
    scanner._mark_post_repaint_recheck = lambda *_args, **_kwargs: None
    scanner._inherit_exit_entry_metadata = lambda *_args, **_kwargs: None
    scanner._persist_session_trade_candidates = lambda: None
    scanner._remember_trade_candidates = lambda exits: scanner._captured_exits.extend(exits)
    scanner.log_event = lambda *args, **_kwargs: scanner._events.append(args)
    scanner._notify = lambda *_args, **_kwargs: None

    def diag_inc(section, key):
        scanner._diag_counts[(section, key)] = scanner._diag_counts.get((section, key), 0) + 1

    scanner._diag_inc = diag_inc
    return scanner


def _repaint_override_candidate():
    signal_ts = datetime.now(scanner_module.IST).replace(tzinfo=None) - timedelta(minutes=2)
    candidate = _option_candidate(
        instrument="BANKNIFTY",
        direction="SHORT",
        price=804.05,
        current_price=793.30,
        stop=755.81,
        target=885.57,
        grade="B+",
        confidence=0.592,
        option_type="PE",
        atm_strike=58100,
        trading_symbol="BANKNIFTY28JUL2658100PE",
        symbol_token="61892",
        signal_timestamp=signal_ts,
        quote_verified=True,
        stabilization_momentum_override=True,
        initial_stop=771.88,
        spot_stop=0.0,
        spot_target=0.0,
        source_engines=["supertrend", "consensus"],
    )
    candidate._next_repaint_check = datetime.now(scanner_module.IST).replace(tzinfo=None) - timedelta(seconds=1)
    return candidate


def test_post_entry_repaint_guard_holds_stabilization_override_trade():
    candidate = _repaint_override_candidate()
    scanner = _scanner_for_repaint_override_test(
        candidate,
        {"reason": "lower-high breakdown through recent swing low"},
    )

    scanner._check_session_candidates_breaches_locked("BANKNIFTY")

    assert scanner._captured_exits == []
    assert scanner._diag_counts[("repaint_guard", "momentum_override_held")] == 1
    assert scanner._diag_counts[("repaint_guard", "passed")] == 1


def test_post_entry_repaint_guard_labels_failed_override_exit():
    candidate = _repaint_override_candidate()
    scanner = _scanner_for_repaint_override_test(candidate, None)

    scanner._check_session_candidates_breaches_locked("BANKNIFTY")

    assert len(scanner._captured_exits) == 1
    exit_candidate = scanner._captured_exits[0]
    assert exit_candidate.exit_reason == "MOMENTUM_OVERRIDE_FAILED"
    assert any("Momentum override failed after entry" in reason for reason in exit_candidate.reasons)


def test_session_smart_lock_falls_back_to_giveback_exit_when_floor_unavailable():
    """A 5m option winner must not round-trip just because option breathing room
    cleared the computed smart-lock floor. Exit at the current mark once the peak
    has armed smart protection and the trade has collapsed below the keep floor."""
    candidate = _repaint_override_candidate()
    candidate.peak_pnl = 1554.0
    candidate.current_price = 702.60
    candidate.price = 707.05
    candidate.initial_stop = 678.77
    candidate.scalp_lock_mode = True

    scanner = _scanner_for_repaint_override_test(candidate, None)
    scanner._candidate_gross_pnl = lambda _c, _price: -213.5
    scanner._candidate_net_pnl = lambda _c, _price: -293.5
    scanner._trade_like_initial_risk_rs = lambda *_args, **_kwargs: 848.4
    scanner._profit_lock_min_peak = lambda *_args, **_kwargs: 848.4
    scanner._profit_lock_floor = lambda *_args, **_kwargs: (0.0, "", 0.0)

    scanner._check_session_candidates_breaches_locked("BANKNIFTY")

    assert len(scanner._captured_exits) == 1
    exit_candidate = scanner._captured_exits[0]
    assert exit_candidate.exit_reason == "PROFIT_GIVEBACK_GUARD"
    assert exit_candidate.exit_price == pytest.approx(702.60)
    assert any("Smart lock floor unavailable after peak Rs.1554" in reason for reason in exit_candidate.reasons)


def test_session_smart_lock_keep_floor_does_not_cut_a_winner_at_its_peak():
    """The Rs.450 keep-floor is COLLAPSE protection, not a profit cap.

    2026-08-12 live: the non-scalp lock arms at Rs.250
    (profit_lock_nonscalp_min_peak_rs) while the keep-floor is Rs.450, so
    `current < floor` was true the instant the lock armed. NIFTY 24400PE was
    market-exited 18s after entry AT ITS PEAK (peak Rs.250 == current Rs.250)
    for Rs.170 net; the same signal in restore_point_51 ran to Rs.3,124. Every
    large win has to cross the Rs.250-450 band, so nothing may cut inside it.
    """
    candidate = _repaint_override_candidate()
    candidate.instrument = "NIFTY"
    candidate.option_type = "PE"
    candidate.timeframe = "5min"
    candidate.lots = 1
    candidate.lot_size = 75
    candidate.price = 121.80
    candidate.current_price = 125.65
    candidate.initial_stop = 112.06
    candidate.stop = 112.06
    candidate.peak_pnl = 250.0
    candidate.scalp_lock_mode = False
    candidate._next_repaint_check = None

    scanner = _scanner_for_repaint_override_test(candidate, None)
    scanner._repaint_signal_status = lambda *_args, **_kwargs: "INTACT"
    # At the peak: current == peak, no give-back whatsoever.
    scanner._candidate_gross_pnl = lambda _c, _price: 250.0
    scanner._candidate_net_pnl = lambda _c, _price: 170.0
    scanner._trade_like_initial_risk_rs = lambda *_args, **_kwargs: 730.5
    scanner._profit_lock_min_peak = lambda *_args, **_kwargs: 250.0
    # Lock floor below the round trip cost -> "Smart lock floor unavailable".
    scanner._profit_lock_floor = lambda *_args, **_kwargs: (0.0, "", 0.0)

    scanner._check_session_candidates_breaches_locked("NIFTY")

    assert scanner._captured_exits == []


def test_smart_lock_floor_collapse_requires_a_peak_above_the_floor():
    scanner = Scanner.__new__(Scanner)

    # Armed at Rs.250 and sitting at its peak: never a collapse.
    assert scanner._smart_lock_floor_collapse(250.0, 250.0) is False
    # Peak never cleared the floor, so the floor cannot have been given back.
    assert scanner._smart_lock_floor_collapse(449.0, 10.0) is False
    # Earned above the floor and gave it back: this is what the floor is for.
    assert scanner._smart_lock_floor_collapse(1554.0, -213.5) is True
    assert scanner._smart_lock_floor_collapse(900.0, 449.0) is True
    # Above the floor and holding it: still protected by the retention ladder.
    assert scanner._smart_lock_floor_collapse(900.0, 451.0) is False


def test_smart_lock_backstop_never_fires_before_the_ladder_it_backs_up():
    scanner = Scanner.__new__(Scanner)

    # Whatever retention the ladder chose IS the backstop level, in both modes.
    for scalp in (False, True):
        assert scanner._smart_lock_backstop_ratio(0.50, scalp) == pytest.approx(0.50)
        assert scanner._smart_lock_backstop_ratio(0.38, scalp) == pytest.approx(0.38)
        assert scanner._smart_lock_backstop_ratio(0.90, scalp) == pytest.approx(0.90)
    # No ladder floor -> the constant, and only floor-collapse can exit there anyway.
    assert scanner._smart_lock_backstop_ratio(0.0, False) == pytest.approx(0.65)
    assert scanner._smart_lock_backstop_ratio(0.0, True) == pytest.approx(0.70)


def _faded_winner_scanner(current_gross):
    """Peak Rs.1000, ladder retaining 50% (floor Rs.500 = premium 128.47)."""
    candidate = _repaint_override_candidate()
    candidate.instrument = "NIFTY"
    candidate.option_type = "PE"
    candidate.timeframe = "5min"
    candidate.lots = 1
    candidate.lot_size = 75
    candidate.price = 121.80
    candidate.current_price = 121.80 + (current_gross / 75.0)
    candidate.initial_stop = 112.06
    candidate.stop = 112.06
    candidate.peak_pnl = 1000.0
    candidate.scalp_lock_mode = False
    candidate._next_repaint_check = None

    scanner = _scanner_for_repaint_override_test(candidate, None)
    scanner._repaint_signal_status = lambda *_args, **_kwargs: "INTACT"
    scanner._candidate_gross_pnl = lambda _c, _price: float(current_gross)
    scanner._candidate_net_pnl = lambda _c, _price: 400.0
    scanner._trade_like_initial_risk_rs = lambda *_args, **_kwargs: 730.5
    scanner._profit_lock_min_peak = lambda *_args, **_kwargs: 250.0
    scanner._profit_lock_floor = lambda *_args, **_kwargs: (128.47, "SMART_PROFIT_LOCK", 0.50)
    return scanner


def test_faded_winner_above_the_ladder_floor_is_not_cut_at_the_ladder_floor():
    """Peak Rs.1000, now Rs.600, ladder trailing at Rs.500.

    The flat 0.65 gate opened at Rs.650 and booked the exit at the ladder's Rs.500
    floor — a price the trade had not reached — while auto mode's 0.45 gate held
    the same trade. The backstop may not pre-empt the trail it backs up.
    """
    scanner = _faded_winner_scanner(600.0)

    scanner._check_session_candidates_breaches_locked("NIFTY")

    assert scanner._captured_exits == []


def test_faded_winner_below_the_ladder_floor_still_exits_on_the_lock():
    scanner = _faded_winner_scanner(400.0)

    scanner._check_session_candidates_breaches_locked("NIFTY")

    assert len(scanner._captured_exits) == 1
    assert scanner._captured_exits[0].exit_reason == "SMART_PROFIT_LOCK"


def test_a_flat_trade_is_not_losing_just_because_it_owes_its_own_costs():
    scanner = Scanner.__new__(Scanner)
    trade = _option_candidate(inst_type="OPT")  # opt_cost 80 round trip

    # Sitting exactly at entry: net is -80 purely because of brokerage and taxes.
    assert scanner._position_is_underwater(trade, -80.0) is False
    assert scanner._position_is_underwater(trade, 0.0) is False
    assert scanner._position_is_underwater(trade, 250.0) is False
    # Actually moved against the position.
    assert scanner._position_is_underwater(trade, -81.0) is True
    assert scanner._position_is_underwater(trade, -400.0) is True


def _midcpnifty_intel_scanner(net_pnl):
    """MIDCPNIFTY 14975CE, seconds after entry, first option chain of the day.

    PCR contrarian BEARISH + order flow 0.17 = exactly 2 bearish votes, which is
    the relaxed loss-guard bar and one short of the 3-vote winner bar.
    """
    candidate = _repaint_override_candidate()
    candidate.instrument = "MIDCPNIFTY"
    candidate.option_type = "CE"
    candidate.direction = "LONG"
    candidate.timeframe = "15min"
    candidate.lots = 1
    candidate.lot_size = 120
    candidate.price = 161.45
    candidate.current_price = 161.45
    candidate.initial_stop = 148.53
    candidate.stop = 148.53
    candidate.peak_pnl = 0.0
    candidate._next_repaint_check = None

    scanner = _scanner_for_repaint_override_test(candidate, None)
    scanner._repaint_signal_status = lambda *_args, **_kwargs: "INTACT"
    scanner._candidate_gross_pnl = lambda _c, _price: float(net_pnl) + 80.0
    scanner._candidate_net_pnl = lambda _c, _price: float(net_pnl)
    scanner._get_trade_intel = lambda _c: {
        "pcr": {"contrarian_signal": "BEARISH", "primary_pcr": 0.89},
        "oi": {"signal": "NEUTRAL"},
        "order_flow": {"ratio": 0.17},
        "aggregate": {"score": 0.0},
    }
    scanner._intel_exit_blocked_by_trend = lambda *_args, **_kwargs: False
    scanner._should_defer_option_intel_exit = lambda *_args, **_kwargs: (False, "")
    return scanner


def test_two_vote_intel_reversal_cannot_cut_a_trade_that_is_only_down_its_costs():
    """2026-08-12 09:15:27: MIDCPNIFTY 14975CE entered 09:15:03 with no option
    chain yet (PCR=0 at 09:15:10). The session's first chain fetch 23s later voted
    2/2 bearish and closed it flat for -Rs.80. It only got the 2-vote bar because
    net P&L at entry is minus the round trip cost, which every trade owes."""
    scanner = _midcpnifty_intel_scanner(-80.0)

    scanner._check_session_candidates_breaches_locked("MIDCPNIFTY")

    assert scanner._captured_exits == []


def test_two_vote_intel_reversal_still_cuts_a_genuinely_losing_trade():
    scanner = _midcpnifty_intel_scanner(-600.0)
    # Entered with a clean book, so the 2 votes now are a genuine turn — this
    # isolates the vote bar from the entry-vs-now comparison tested below.
    scanner._active_session_entry_candidates("MIDCPNIFTY")[0].entry_intel = {
        "adverse_votes": 0
    }

    scanner._check_session_candidates_breaches_locked("MIDCPNIFTY")

    assert len(scanner._captured_exits) == 1
    assert scanner._captured_exits[0].exit_reason == "OI_PCR_REVERSAL"


def _three_vote_bearish_scanner(entry_intel=None, net_pnl=-600.0):
    """Genuinely losing, so the 2-vote bar applies and 3 votes clear it outright.

    Only the entry-vs-now comparison can hold this trade.
    """
    scanner = _midcpnifty_intel_scanner(net_pnl)
    candidate = scanner._active_session_entry_candidates("MIDCPNIFTY")[0]
    if entry_intel is not None:
        candidate.entry_intel = entry_intel
    scanner._get_trade_intel = lambda _c: {
        "pcr": {"contrarian_signal": "BEARISH", "primary_pcr": 0.89},
        "oi": {"signal": "BEARISH"},
        "order_flow": {"ratio": 0.17},
        "aggregate": {"score": 0.0},
    }
    return scanner, candidate


def test_intel_exit_cannot_fire_on_the_first_reading_an_intel_less_entry_ever_saw():
    """MIDCPNIFTY entered 09:15:03 with no option chain. There is nothing for the
    09:15:26 reading to have reversed FROM, so it becomes the reference instead of
    the trigger — and the reference is latched so the next tick cannot re-fire it."""
    scanner, candidate = _three_vote_bearish_scanner()

    scanner._check_session_candidates_breaches_locked("MIDCPNIFTY")
    assert scanner._captured_exits == []
    assert getattr(candidate, "_intel_reversal_baseline", None) == 3

    # The very next pass sees the same reading: still not a reversal.
    scanner._check_session_candidates_breaches_locked("MIDCPNIFTY")
    assert scanner._captured_exits == []


def test_intel_that_was_already_adverse_at_entry_is_not_a_reversal():
    """Entered with the book already leaning against it. The entry gate had its
    chance; the same reading later is the accepted condition, not a turn."""
    scanner, _candidate = _three_vote_bearish_scanner(entry_intel={"adverse_votes": 3})

    scanner._check_session_candidates_breaches_locked("MIDCPNIFTY")

    assert scanner._captured_exits == []


def test_intel_that_actually_turned_against_the_entry_still_exits():
    scanner, _candidate = _three_vote_bearish_scanner(entry_intel={"adverse_votes": 1})

    scanner._check_session_candidates_breaches_locked("MIDCPNIFTY")

    assert len(scanner._captured_exits) == 1
    assert scanner._captured_exits[0].exit_reason == "OI_PCR_REVERSAL"


def test_option_stop_hit_exit_uses_observed_premium_not_stop_line():
    scanner = Scanner.__new__(Scanner)
    candidate = _option_candidate(direction="SHORT", price=806.85, stop=758.44)
    candidate.inst_type = "OPT"
    candidate.option_type = "PE"

    exit_px, reason, text = scanner._candidate_exit_resolution(
        candidate,
        305.05,
        "",
        "",
    )

    assert exit_px == 305.05
    assert reason == "STOP_HIT"
    assert text == "Stoploss hit"


def test_legacy_repaint_stop_hit_payload_loads_as_repaint_abort():
    scanner = Scanner.__new__(Scanner)

    candidate = scanner._candidate_from_payload(
        {
            "instrument": "NIFTY",
            "direction": "LONG",
            "price": 224.60,
            "stop": 216.60,
            "target": 257.80,
            "inst_type": "OPT",
            "action": "EXIT",
            "exit_reason": "STOP_HIT",
            "reasons": ["Signal repainted intra-candle (1m check) at 14:42:52"],
        }
    )

    assert candidate.exit_reason == "REPAINT_ABORT"


def test_option_live_stop_distance_respects_configured_sl_pct():
    scanner = SimpleNamespace(options_sl_pct=15.0)

    assert round(SignalProcessor._option_premium_stop_distance(224.60, scanner, "TRENDING_UP"), 2) == 33.69
    assert round(SignalProcessor._option_premium_stop_distance(224.60, SimpleNamespace(options_sl_pct=10.0), "RANGING"), 2) == 33.69


def test_option_stop_timeframe_atr_aware():
    """The ATR-derived premium stop is timeframe-bounded: 5min tighter, 15min wider,
    both capped by options_sl_pct; disabled/flag-off falls back to the flat percentage."""
    from config.settings import get_settings

    settings = get_settings()
    scanner = SimpleNamespace(options_sl_pct=15.0)
    price = 1200.0

    # Large ATR (12.5% of premium) diverges at the band edges: 5min caps at 10%,
    # 15min caps at 15% (but ceiling is options_sl_pct=15% so 12.5% passes through).
    big_atr = 150.0
    s5 = SignalProcessor._option_premium_stop_distance(price, scanner, "TRENDING_UP", timeframe="5min", atr_premium_stop=big_atr, settings=settings)
    s15 = SignalProcessor._option_premium_stop_distance(price, scanner, "TRENDING_UP", timeframe="15min", atr_premium_stop=big_atr, settings=settings)
    assert round(s5, 2) == 120.0   # 10% cap
    assert round(s15, 2) == 150.0  # 12.5% ATR within 15% cap
    assert s5 < s15

    # Tiny ATR floors to the per-timeframe minimum (5min 4%, 15min 6%).
    tiny_atr = 5.0
    t5 = SignalProcessor._option_premium_stop_distance(price, scanner, "TRENDING_UP", timeframe="5min", atr_premium_stop=tiny_atr, settings=settings)
    t15 = SignalProcessor._option_premium_stop_distance(price, scanner, "TRENDING_UP", timeframe="15min", atr_premium_stop=tiny_atr, settings=settings)
    assert round(t5, 2) == 48.0    # 4% floor
    assert round(t15, 2) == 72.0   # 6% floor

    # No ATR supplied → legacy flat percentage (backward compatible).
    assert round(SignalProcessor._option_premium_stop_distance(price, scanner, "TRENDING_UP"), 2) == 180.0

    # Feature flag off → flat percentage even when ATR is supplied.
    settings.option_stop_tf_atr_aware = False
    try:
        off = SignalProcessor._option_premium_stop_distance(price, scanner, "TRENDING_UP", timeframe="5min", atr_premium_stop=big_atr, settings=settings)
        assert round(off, 2) == 180.0
    finally:
        settings.option_stop_tf_atr_aware = True


# MIDCPNIFTY 14525CE that went +₹900 -> -₹1,088: risk = |304-285.76|*120 ≈ ₹2,189 (1R).
_MIDCAP_RISK = abs(304.0 - 285.76) * 120


def test_sub_1r_giveback_guard_protects_special_dead_zone_winner():
    """Fix 1 (special-case): a scalp/late or near-EOD trade whose sub-1R peak fades
    is closed at the current still-positive price so it cannot round-trip to a loss."""
    sc = Scanner.__new__(Scanner)
    sc.latest_regimes = {}
    # scalp_mode=True, matching production: scanner.py computes this as
    # `scalp_mode = bool(getattr(previous, "scalp_lock_mode", False))` before passing it
    # in. Passing the non-scalp arm for a scalp trade was harmless while both branches
    # shared a Rs.500 base; once the non-scalp base dropped to Rs.250 it silently moved
    # this scenario out of the dead zone it is meant to exercise.
    smp = sc._profit_lock_min_peak(True, _MIDCAP_RISK, "5min")  # ~1094, dead-zone ceiling

    # SPECIAL context (scalp-lock / late entry): peak ₹900 fades to ₹400 (<=50%) -> cut.
    scalp_trade = SimpleNamespace(instrument="MIDCPNIFTY 14525CE", scalp_lock_mode=True)
    should, reason, _ = sc._peak_giveback_or_reversal_exit(
        scalp_trade, current_pnl_rs=400.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=smp, latest_time=None, regime="RANGING",
        inst_type="OPT", option_type="CE", direction="LONG",
    )
    assert should is True
    assert reason == "PROFIT_GIVEBACK_GUARD"

    # If the same armed winner round-trips slightly below breakeven between scans,
    # the guard still exits instead of waiting for the full hard stop.
    crossed_zero, crossed_reason, crossed_ctx = sc._peak_giveback_or_reversal_exit(
        scalp_trade, current_pnl_rs=-213.5, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=smp, latest_time=None, regime="RANGING",
        inst_type="OPT", option_type="CE", direction="LONG",
    )
    assert crossed_zero is True
    assert crossed_reason == "PROFIT_GIVEBACK_GUARD"
    assert "faded to Rs.-214" in crossed_ctx

    # A scalp winner still holding most of its peak is NOT cut.
    hold, _, _ = sc._peak_giveback_or_reversal_exit(
        scalp_trade, current_pnl_rs=800.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=smp, latest_time=None, regime="RANGING",
        inst_type="OPT", option_type="CE", direction="LONG",
    )
    assert hold is False


def test_normal_sub_1r_fade_is_now_owned_by_the_profit_lock():
    """A normal trade fading a sub-1R peak used to be left running on trend days, and
    caught by the give-back guard only on chop days.

    2026-08-11 measured what that cost: across 12 live sessions the book reached
    Rs.42,444 of peak profit and banked -Rs.992, with 45 trades going green then closing
    red. So the non-scalp lock now arms at 0.20R (see profit_lock_min_r_multiple) and
    this fade is protected on EVERY day type.

    The protection did not disappear from the give-back guard, it MOVED: with the arm at
    Rs.437.8 a Rs.900 peak is above it, so SMART_PROFIT_LOCK owns the trade and the
    give-back guard correctly declines a position that is no longer in its dead zone.
    Asserted both ways below, because "the guard says False" alone would also describe
    an unprotected trade.
    """
    sc = Scanner.__new__(Scanner)
    sc.latest_regimes = {}
    normal = SimpleNamespace(instrument="MIDCPNIFTY 14525CE")  # no scalp flag, mid-day

    for day_type, confidence in (("TREND_DAY_UP", 0.8), ("UNDECIDED", 0.4)):
        sc._cached_timeframe_intel = lambda *a, _d=day_type, _c=confidence, **k: {
            "session_context": {"day_type": _d, "confidence": _c}
        }
        smp = sc._profit_lock_min_peak(False, _MIDCAP_RISK, "5min")
        assert smp == pytest.approx(437.76), "the non-scalp arm moved unexpectedly"

        # Out of the give-back guard's dead zone, because the lock now covers it.
        should, _, _ = sc._peak_giveback_or_reversal_exit(
            normal, current_pnl_rs=400.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
            smart_min_peak=smp, latest_time=None, regime="RANGING",
            inst_type="OPT", option_type="CE", direction="LONG",
        )
        assert should is False, f"{day_type}: give-back guard fired inside the lock's range"

        # ...and the lock genuinely fires: Rs.900 peak, 50% rung, floor Rs.450 > Rs.400.
        _floor, _label, ratio = sc._profit_lock_floor(
            304.0, "LONG", 120, 900.0, scalp_mode=False,
            initial_risk_rs=_MIDCAP_RISK, timeframe="5min", trade_like=normal,
        )
        assert ratio == 0.50, f"{day_type}: lock did not arm on a Rs.900 peak"
        assert 400.0 < ratio * 900.0, f"{day_type}: trade left unprotected by both layers"


def test_unconfirmed_repaint_window_winner_is_still_protected():
    """2026-07-02: an intracandle entry whose repaint check had not yet passed peaked
    Rs.990 and faded negative before the repaint abort fired. During that structurally
    unconfirmed window the give-back guard armed even without scalp/EOD context.

    Since 2026-08-11 the non-scalp profit lock arms at 0.20R/Rs.250, below the give-back
    family's 0.40R/Rs.500, so the LOCK reaches this trade first. The protection asserted
    here is the same protection; only the layer providing it changed."""
    sc = Scanner.__new__(Scanner)
    sc.latest_regimes = {}
    sc._cached_timeframe_intel = lambda *a, **k: {"session_context": {"day_type": "TREND_DAY_UP", "confidence": 0.8}}
    smp = sc._profit_lock_min_peak(False, _MIDCAP_RISK, "5min")

    unconfirmed = SimpleNamespace(
        instrument="MIDCPNIFTY 14525CE",
        entered_intracandle=True,  # repaint check pending (no repaint_guard_checked)
    )
    # 2026-08-11: on the NON-SCALP path the give-back family no longer has a band to
    # cover here. Its arm is max(Rs.500, 0.40R) = Rs.875.5 while the lock now arms at
    # max(Rs.250, 0.20R) = Rs.437.8, so the lock is always first and the dead zone these
    # guards were written to patch is empty. That is a tightening, not a hole: the lock's
    # first rung retains 50%, the same fraction profit_giveback_exit_ratio exits at, and
    # every rung above it retains more.
    gb_arm = sc._profit_giveback_arm(_MIDCAP_RISK)
    assert smp < gb_arm, (
        "the give-back guard arms before the lock again — the dead zone is back"
    )

    should, reason, _ = sc._peak_giveback_or_reversal_exit(
        unconfirmed, current_pnl_rs=400.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=smp, latest_time=None, regime="RANGING",
        inst_type="OPT", option_type="CE", direction="LONG",
    )
    assert should is False, "give-back guard fired inside the profit lock's range"
    _f, _l, ratio = sc._profit_lock_floor(
        304.0, "LONG", 120, 900.0, scalp_mode=False,
        initial_risk_rs=_MIDCAP_RISK, timeframe="5min", trade_like=unconfirmed,
    )
    assert ratio == 0.50 and 400.0 < ratio * 900.0, "unconfirmed winner left unprotected"

    # Once the repaint guard confirms the candle the trade is a normal tracked position
    # again, and the give-back family stays out of the lock's range either way.
    confirmed = SimpleNamespace(
        instrument="MIDCPNIFTY 14525CE",
        entered_intracandle=True,
        repaint_guard_checked=True,
    )
    hold, _, _ = sc._peak_giveback_or_reversal_exit(
        confirmed, current_pnl_rs=400.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=smp, latest_time=None, regime="RANGING",
        inst_type="OPT", option_type="CE", direction="LONG",
    )
    assert hold is False


def test_repaint_window_giveback_guard_respects_setting_gate():
    from config.settings import get_settings

    sc = Scanner.__new__(Scanner)
    sc.latest_regimes = {}
    sc._cached_timeframe_intel = lambda *a, **k: {"session_context": {"day_type": "TREND_DAY_UP", "confidence": 0.8}}
    smp = sc._profit_lock_min_peak(False, _MIDCAP_RISK, "5min")
    unconfirmed = SimpleNamespace(
        instrument="MIDCPNIFTY 14525CE",
        entered_intracandle=True,
    )
    settings = get_settings()
    settings.repaint_window_giveback_guard_enabled = False
    try:
        should, _, _ = sc._peak_giveback_or_reversal_exit(
            unconfirmed, current_pnl_rs=400.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
            smart_min_peak=smp, latest_time=None, regime="RANGING",
            inst_type="OPT", option_type="CE", direction="LONG",
        )
        assert should is False
    finally:
        settings.repaint_window_giveback_guard_enabled = True


def test_cross_index_reversal_takes_profit_post_entry():
    """Point 2: while a long trade is live and fading, a broad cross-index reversal
    (>= 2 peer indices turned against it) takes the profit even though the trade's
    own index regime hasn't flipped."""
    sc = Scanner.__new__(Scanner)
    sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    trade = SimpleNamespace(instrument="MIDCPNIFTY 14525CE")

    # Own index still ranging, but 3 peers have turned down against the long view.
    sc.latest_regimes = {
        "MIDCPNIFTY": "RANGING",
        "NIFTY": "TRENDING_DOWN",
        "BANKNIFTY": "TRENDING_DOWN",
        "SENSEX": "BREAKOUT_DOWN",
    }
    should, reason, _ = sc._peak_giveback_or_reversal_exit(
        trade, current_pnl_rs=700.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=99999.0, latest_time=None, regime="RANGING",
        inst_type="OPT", option_type="CE", direction="LONG",
    )
    assert should is True
    assert reason == "CROSS_INDEX_REVERSAL_EXIT"

    # Peers NOT broadly opposing (1 down, 1 up, 1 ranging) -> not cut on cross-index.
    sc.latest_regimes = {
        "MIDCPNIFTY": "RANGING",
        "NIFTY": "TRENDING_UP",
        "BANKNIFTY": "RANGING",
        "SENSEX": "TRENDING_DOWN",
    }
    hold, _, _ = sc._peak_giveback_or_reversal_exit(
        trade, current_pnl_rs=700.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=99999.0, latest_time=None, regime="RANGING",
        inst_type="OPT", option_type="CE", direction="LONG",
    )
    assert hold is False


def test_eod_proximity_collapses_profit_lock_arm():
    """Fix 2: within the square-off window the 1R arm collapses so protection engages on
    a modest peak instead of waiting for a run that can't happen.

    It collapses to the ROUND-TRIP COST, not to eod_protect_min_peak_rs (Rs.1), since
    2026-08-11: at Rs.1 any blip armed a protective exit, and PROFIT_GIVEBACK_GUARD duly
    banked a Rs.36 peak on BANKNIFTY 57400PE for a net -Rs.44 against Rs.80 of charges.
    Below the round trip a protective exit cannot come out ahead, so that is the floor.
    """
    from config.settings import get_settings

    sc = Scanner.__new__(Scanner)
    normal = sc._profit_lock_min_peak(False, _MIDCAP_RISK, "5min")
    near_eod = sc._profit_lock_min_peak(False, _MIDCAP_RISK, "5min", datetime(2026, 7, 1, 15, 10))
    assert normal > near_eod, "the EOD window no longer collapses the arm"
    assert near_eod == pytest.approx(float(getattr(get_settings(), "opt_cost", 80.0)))
    assert near_eod > 36.0, "a peak smaller than the spread still arms a protective exit"


def test_loss_recovery_watch_holds_former_winner_inside_breathing_with_support():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    scanner.latest_regimes = {}
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 58000.0)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 2,
        "reason": "5m support still holding",
    }
    trade = _option_candidate(
        instrument="BANKNIFTY",
        direction="SHORT",
        option_type="PE",
        price=100.0,
        current_price=96.0,
        stop=85.0,
        initial_stop=85.0,
        lot_size=30,
        peak_pnl=800.0,
        market_regime="RANGING",
        intelligence_score=-0.20,
        price_action_verdict="NEUTRAL",
        price_action_score=0.10,
    )

    action, reason = scanner._loss_recovery_watch_decision(
        trade,
        current_price=96.0,
        current_pnl_rs=-120.0,
        peak_pnl=800.0,
        initial_risk_rs=450.0,
        latest_time=datetime(2026, 7, 8, 12, 20),
        regime="RANGING",
        proposed_exit_reason="STOP_HIT",
    )

    assert action == "HOLD"
    assert "deferred" in reason
    assert trade._loss_recovery_best_pnl == pytest.approx(-120.0)
    assert trade._loss_recovery_floor_pnl == pytest.approx(-370.0)


def test_loss_recovery_watch_exits_when_reduced_loss_trail_breaks():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    scanner.latest_regimes = {}
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 58000.0)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 2,
        "reason": "5m support still holding",
    }
    trade = _option_candidate(
        instrument="BANKNIFTY",
        direction="SHORT",
        option_type="PE",
        price=100.0,
        current_price=96.0,
        stop=85.0,
        initial_stop=85.0,
        lot_size=30,
        peak_pnl=800.0,
        market_regime="RANGING",
        intelligence_score=-0.20,
    )

    first_action, _ = scanner._loss_recovery_watch_decision(
        trade,
        current_price=96.0,
        current_pnl_rs=-120.0,
        peak_pnl=800.0,
        initial_risk_rs=900.0,
        latest_time=datetime(2026, 7, 8, 12, 20),
        regime="RANGING",
        proposed_exit_reason="STOP_HIT",
    )
    second_action, second_reason = scanner._loss_recovery_watch_decision(
        trade,
        current_price=87.0,
        current_pnl_rs=-390.0,
        peak_pnl=800.0,
        initial_risk_rs=900.0,
        latest_time=datetime(2026, 7, 8, 12, 22),
        regime="RANGING",
        proposed_exit_reason="STOP_HIT",
    )

    assert first_action == "HOLD"
    assert second_action == "EXIT"
    assert "reduced-loss trail hit" in second_reason


def test_loss_recovery_watch_exits_when_support_breaks():
    scanner = Scanner.__new__(Scanner)
    scanner.options_sl_pct = 15.0
    scanner.latest_regimes = {}
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 58000.0)
    contexts = [
        {
            "has_context": True,
            "fast_reversal": False,
            "trend_supportive": True,
            "trend_votes": 2,
            "reason": "5m support still holding",
        },
        {
            "has_context": True,
            "fast_reversal": True,
            "trend_supportive": False,
            "trend_votes": 0,
            "reason": "1m+5m support broke",
        },
    ]
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: contexts.pop(0)
    trade = _option_candidate(
        instrument="BANKNIFTY",
        direction="SHORT",
        option_type="PE",
        price=100.0,
        current_price=96.0,
        stop=85.0,
        initial_stop=85.0,
        lot_size=30,
        peak_pnl=800.0,
        market_regime="RANGING",
        intelligence_score=-0.20,
    )

    assert scanner._loss_recovery_watch_decision(
        trade,
        current_price=96.0,
        current_pnl_rs=-120.0,
        peak_pnl=800.0,
        initial_risk_rs=900.0,
        latest_time=datetime(2026, 7, 8, 12, 20),
        regime="RANGING",
        proposed_exit_reason="STOP_HIT",
    )[0] == "HOLD"
    action, reason = scanner._loss_recovery_watch_decision(
        trade,
        current_price=96.0,
        current_pnl_rs=-120.0,
        peak_pnl=800.0,
        initial_risk_rs=900.0,
        latest_time=datetime(2026, 7, 8, 12, 21),
        regime="RANGING",
        proposed_exit_reason="STOP_HIT",
    )

    assert action == "EXIT"
    assert "fast adverse reversal" in reason


def test_peak_threat_exit_takes_near_peak_winner_on_adverse_evidence():
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 58400.0)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": True,
        "trend_supportive": False,
        "trend_votes": 0,
        "reason": "1m/5m adverse expansion",
    }
    scanner._live_direction_context = lambda *_args, **_kwargs: {
        "adverse": True,
        "supportive": False,
        "live_regime": "TRENDING_UP",
        "cross_opp": 2,
        "cross_peers": 3,
    }
    trade = _option_candidate(
        instrument="BANKNIFTY",
        direction="SHORT",
        option_type="PE",
        price=707.05,
        current_price=757.05,
        lot_size=30,
        peak_pnl=1554.0,
        intelligence_score=0.70,
        market_intelligence_context={
            "aggregate": {"signal": "BUY", "score": 70.0},
            "order_flow": {"signal": "BULLISH", "ratio": 1.40},
            "volume": {"signal": "BULLISH_VOLUME", "volume_ratio": 0.90},
            "oi": {"signal": "BULLISH"},
        },
    )

    def _observe(at):
        return scanner._peak_threat_exit_decision(
            trade,
            current_price=757.05,
            current_pnl_rs=1500.0,
            peak_pnl=1554.0,
            initial_risk_rs=848.4,
            latest_time=at,
            regime="RANGING",
            inst_type="OPT",
            option_type="PE",
            direction="SHORT",
        )

    # A winner needs the same two observations the loss side already required
    # (peak_threat_exit_confirm_seconds); one tick no longer cuts it.
    first_exit, _first_reason, _first_ctx = _observe(datetime(2026, 7, 8, 12, 14))
    assert first_exit is False
    should_exit, reason, ctx = _observe(datetime(2026, 7, 8, 12, 14, 10))

    assert should_exit is True
    assert reason == "PEAK_THREAT_EXIT"
    assert "retained 97%" in ctx
    assert "OI BULLISH opposes SHORT" in ctx


def test_peak_threat_exit_ignores_supportive_near_peak_pullback():
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 58400.0)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "trend_votes": 3,
        "reason": "same-trend pullback",
    }
    scanner._live_direction_context = lambda *_args, **_kwargs: {
        "adverse": False,
        "supportive": True,
        "live_regime": "BREAKOUT_DOWN",
        "cross_opp": 0,
        "cross_peers": 3,
    }
    trade = _option_candidate(
        instrument="BANKNIFTY",
        direction="SHORT",
        option_type="PE",
        price=707.05,
        current_price=752.05,
        lot_size=30,
        peak_pnl=1554.0,
        intelligence_score=-0.75,
        market_intelligence_context={
            "aggregate": {"signal": "SELL", "score": -75.0},
            "order_flow": {"signal": "BEARISH", "ratio": 0.65},
            "volume": {"signal": "BEARISH_VOLUME", "volume_ratio": 1.40},
            "oi": {"signal": "BEARISH"},
        },
    )

    should_exit, reason, ctx = scanner._peak_threat_exit_decision(
        trade,
        current_price=752.05,
        current_pnl_rs=1350.0,
        peak_pnl=1554.0,
        initial_risk_rs=848.4,
        latest_time=datetime(2026, 7, 8, 12, 14),
        regime="BREAKOUT_DOWN",
        inst_type="OPT",
        option_type="PE",
        direction="SHORT",
    )

    assert should_exit is False
    assert reason == ""
    assert "score" in ctx


def test_peak_threat_exit_is_bidirectional_for_ce_and_pe():
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 24500.0)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": True,
        "trend_supportive": False,
        "trend_votes": 0,
        "reason": "opposite impulse",
    }
    scanner._live_direction_context = lambda *_args, **_kwargs: {
        "adverse": False,
        "supportive": False,
        "live_regime": "RANGING",
        "cross_opp": 0,
        "cross_peers": 3,
    }
    ce = _option_candidate(
        instrument="NIFTY",
        direction="LONG",
        option_type="CE",
        price=100.0,
        current_price=132.0,
        lot_size=50,
        intelligence_score=-0.70,
        market_intelligence_context={"aggregate": {"signal": "SELL"}, "oi": {"signal": "BEARISH"}},
    )
    pe = _option_candidate(
        instrument="NIFTY",
        direction="SHORT",
        option_type="PE",
        price=100.0,
        current_price=132.0,
        lot_size=50,
        intelligence_score=0.70,
        market_intelligence_context={"aggregate": {"signal": "BUY"}, "oi": {"signal": "BULLISH"}},
    )

    def _twice(row, option_type, direction):
        kwargs = dict(
            current_price=132.0,
            current_pnl_rs=1600.0,
            peak_pnl=1650.0,
            initial_risk_rs=900.0,
            regime="RANGING",
            inst_type="OPT",
            option_type=option_type,
            direction=direction,
        )
        scanner._peak_threat_exit_decision(
            row, latest_time=datetime(2026, 7, 8, 12, 14), **kwargs
        )
        return scanner._peak_threat_exit_decision(
            row, latest_time=datetime(2026, 7, 8, 12, 14, 10), **kwargs
        )

    # Two observations each — a winner is no longer cut on one tick.
    ce_exit = _twice(ce, "CE", "LONG")
    pe_exit = _twice(pe, "PE", "SHORT")

    assert ce_exit[0] is True and ce_exit[1] == "PEAK_THREAT_EXIT"
    assert pe_exit[0] is True and pe_exit[1] == "PEAK_THREAT_EXIT"


def test_loss_threat_exit_cuts_small_loser_on_strong_adverse_evidence():
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(get_latest_price=lambda _instrument: 58400.0)
    scanner._profit_guard_reversal_context = lambda *_args, **_kwargs: {
        "has_context": True,
        "fast_reversal": True,
        "trend_supportive": False,
        "trend_votes": 0,
        "reason": "adverse expansion",
    }
    scanner._live_direction_context = lambda *_args, **_kwargs: {
        "adverse": True,
        "supportive": False,
        "live_regime": "TRENDING_UP",
        "cross_opp": 2,
        "cross_peers": 3,
    }
    trade = _option_candidate(
        instrument="BANKNIFTY",
        direction="SHORT",
        option_type="PE",
        price=707.05,
        current_price=700.05,
        lot_size=30,
        intelligence_score=0.70,
        market_intelligence_context={
            "aggregate": {"signal": "BUY"},
            "order_flow": {"signal": "BULLISH", "ratio": 1.40},
            "oi": {"signal": "BULLISH"},
        },
    )

    def _look(at):
        return scanner._peak_threat_exit_decision(
            trade,
            current_price=700.05,
            current_pnl_rs=-210.0,
            peak_pnl=300.0,
            initial_risk_rs=848.4,
            latest_time=at,
            regime="RANGING",
            inst_type="OPT",
            option_type="PE",
            direction="SHORT",
        )

    # CHANGED 2026-08-10: the loss arm now requires the same evidence twice, at least
    # peak_threat_exit_loss_confirm_seconds apart on the MARKET clock, with price among
    # the domains. This trade has price (fast reversal, 0/3 trend votes), live_market and
    # fresh_intel, so it still cuts — it just has to be seen holding.
    first_exit, _first_reason, first_ctx = _look(datetime(2026, 7, 8, 12, 14))
    assert first_exit is False
    assert "confirmation 1/2" in first_ctx

    should_exit, reason, ctx = _look(datetime(2026, 7, 8, 12, 14, 4))

    assert should_exit is True
    assert reason == "LOSS_THREAT_EXIT"
    assert "adverse Rs.210" in ctx
    assert "price" in ctx


def test_eod_loss_minimize_exits_near_breakeven_loser():
    """Final-runway losers should be closed when the tape gives a small-loss exit."""
    from config.settings import get_settings

    settings = get_settings()
    old_values = (
        settings.ut_force_exit_time,
        settings.eod_loss_minimize_enabled,
        settings.eod_loss_minimize_window_minutes,
        settings.eod_loss_minimize_min_age_minutes,
        settings.eod_loss_minimize_near_breakeven_rs,
    )
    settings.ut_force_exit_time = "15:22"
    settings.eod_loss_minimize_enabled = True
    settings.eod_loss_minimize_window_minutes = 20.0
    settings.eod_loss_minimize_min_age_minutes = 3.0
    settings.eod_loss_minimize_near_breakeven_rs = 250.0
    try:
        sc = Scanner.__new__(Scanner)
        trade = SimpleNamespace(
            instrument="BANKNIFTY",
            entry_time=datetime(2026, 7, 1, 14, 58),
        )

        should, reason = sc._eod_loss_minimize_exit(
            trade,
            current_pnl_rs=-180.0,
            initial_risk_rs=2500.0,
            latest_time=datetime(2026, 7, 1, 15, 8),
        )

        assert should is True
        assert "near breakeven" in reason
    finally:
        (
            settings.ut_force_exit_time,
            settings.eod_loss_minimize_enabled,
            settings.eod_loss_minimize_window_minutes,
            settings.eod_loss_minimize_min_age_minutes,
            settings.eod_loss_minimize_near_breakeven_rs,
        ) = old_values


def test_eod_loss_minimize_exits_when_best_loss_worsens():
    """Once the EOD window sees a reduced-loss mark, do not let it slip away."""
    from config.settings import get_settings

    settings = get_settings()
    old_values = (
        settings.ut_force_exit_time,
        settings.eod_loss_minimize_enabled,
        settings.eod_loss_minimize_window_minutes,
        settings.eod_loss_minimize_min_age_minutes,
        settings.eod_loss_minimize_giveback_rs,
        settings.eod_loss_minimize_giveback_r_multiple,
    )
    settings.ut_force_exit_time = "15:22"
    settings.eod_loss_minimize_enabled = True
    settings.eod_loss_minimize_window_minutes = 20.0
    settings.eod_loss_minimize_min_age_minutes = 3.0
    settings.eod_loss_minimize_giveback_rs = 250.0
    settings.eod_loss_minimize_giveback_r_multiple = 0.12
    try:
        sc = Scanner.__new__(Scanner)
        trade = SimpleNamespace(
            instrument="NIFTY",
            entry_time=datetime(2026, 7, 1, 14, 55),
            _eod_loss_best_pnl=-420.0,
        )

        should, reason = sc._eod_loss_minimize_exit(
            trade,
            current_pnl_rs=-760.0,
            initial_risk_rs=2000.0,
            latest_time=datetime(2026, 7, 1, 15, 7),
        )

        assert should is True
        assert "worsened" in reason
    finally:
        (
            settings.ut_force_exit_time,
            settings.eod_loss_minimize_enabled,
            settings.eod_loss_minimize_window_minutes,
            settings.eod_loss_minimize_min_age_minutes,
            settings.eod_loss_minimize_giveback_rs,
            settings.eod_loss_minimize_giveback_r_multiple,
        ) = old_values


def test_eod_loss_minimize_can_expand_hard_runway_to_ten_minutes():
    from config.settings import get_settings

    settings = get_settings()
    old_values = (
        settings.ut_force_exit_time,
        settings.eod_loss_minimize_enabled,
        settings.eod_loss_minimize_window_minutes,
        settings.eod_loss_minimize_hard_exit_minutes,
        settings.eod_loss_minimize_max_hard_exit_minutes,
        settings.eod_loss_minimize_material_loss_r,
        settings.eod_loss_minimize_near_breakeven_rs,
    )
    settings.ut_force_exit_time = "15:22"
    settings.eod_loss_minimize_enabled = True
    settings.eod_loss_minimize_window_minutes = 20.0
    settings.eod_loss_minimize_hard_exit_minutes = 5.0
    settings.eod_loss_minimize_max_hard_exit_minutes = 10.0
    settings.eod_loss_minimize_material_loss_r = 0.45
    settings.eod_loss_minimize_near_breakeven_rs = 250.0
    try:
        sc = Scanner.__new__(Scanner)
        sc.latest_regimes = {"BANKNIFTY": "TRENDING_UP"}
        sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        trade = SimpleNamespace(
            instrument="BANKNIFTY",
            inst_type="FUT",
            direction="SHORT",
            entry_time=datetime(2026, 7, 1, 14, 55),
        )

        should, reason = sc._eod_loss_minimize_exit(
            trade,
            current_pnl_rs=-1300.0,
            initial_risk_rs=2500.0,
            latest_time=datetime(2026, 7, 1, 15, 12),
            regime="TRENDING_UP",
        )

        assert should is True
        assert "10.0m" in reason
        assert "material loss" in reason
    finally:
        (
            settings.ut_force_exit_time,
            settings.eod_loss_minimize_enabled,
            settings.eod_loss_minimize_window_minutes,
            settings.eod_loss_minimize_hard_exit_minutes,
            settings.eod_loss_minimize_max_hard_exit_minutes,
            settings.eod_loss_minimize_material_loss_r,
            settings.eod_loss_minimize_near_breakeven_rs,
        ) = old_values


def test_eod_loss_minimize_supportive_small_loss_waits_at_ten_minutes():
    from config.settings import get_settings

    settings = get_settings()
    old_values = (
        settings.ut_force_exit_time,
        settings.eod_loss_minimize_hard_exit_minutes,
        settings.eod_loss_minimize_max_hard_exit_minutes,
        settings.eod_loss_minimize_material_loss_r,
        settings.eod_loss_minimize_near_breakeven_rs,
    )
    settings.ut_force_exit_time = "15:22"
    settings.eod_loss_minimize_hard_exit_minutes = 5.0
    settings.eod_loss_minimize_max_hard_exit_minutes = 10.0
    settings.eod_loss_minimize_material_loss_r = 0.45
    settings.eod_loss_minimize_near_breakeven_rs = 250.0
    try:
        sc = Scanner.__new__(Scanner)
        sc.latest_regimes = {"BANKNIFTY": "TRENDING_DOWN"}
        sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        trade = SimpleNamespace(
            instrument="BANKNIFTY",
            inst_type="FUT",
            direction="SHORT",
            entry_time=datetime(2026, 7, 1, 14, 55),
        )

        should, reason = sc._eod_loss_minimize_exit(
            trade,
            current_pnl_rs=-600.0,
            initial_risk_rs=2500.0,
            latest_time=datetime(2026, 7, 1, 15, 12),
            regime="TRENDING_DOWN",
        )

        assert should is False
        assert "EOD loss watch" in reason
    finally:
        (
            settings.ut_force_exit_time,
            settings.eod_loss_minimize_hard_exit_minutes,
            settings.eod_loss_minimize_max_hard_exit_minutes,
            settings.eod_loss_minimize_material_loss_r,
            settings.eod_loss_minimize_near_breakeven_rs,
        ) = old_values


def test_eod_loss_minimize_leaves_mid_session_loser_running():
    from config.settings import get_settings

    settings = get_settings()
    old_force_exit = settings.ut_force_exit_time
    settings.ut_force_exit_time = "15:22"
    try:
        sc = Scanner.__new__(Scanner)
        trade = SimpleNamespace(
            instrument="SENSEX",
            entry_time=datetime(2026, 7, 1, 12, 0),
        )

        should, reason = sc._eod_loss_minimize_exit(
            trade,
            current_pnl_rs=-180.0,
            initial_risk_rs=2500.0,
            latest_time=datetime(2026, 7, 1, 12, 10),
        )

        assert should is False
        assert "outside EOD loss window" in reason
    finally:
        settings.ut_force_exit_time = old_force_exit


def test_eod_profit_giveback_tightens_when_late_context_adverse():
    from config.settings import get_settings

    settings = get_settings()
    old_values = (
        settings.ut_force_exit_time,
        settings.eod_profit_adaptive_peak_enabled,
        settings.eod_profit_adverse_exit_ratio,
        settings.profit_giveback_exit_ratio,
    )
    settings.ut_force_exit_time = "15:22"
    settings.eod_profit_adaptive_peak_enabled = True
    settings.eod_profit_adverse_exit_ratio = 0.75
    settings.profit_giveback_exit_ratio = 0.50
    try:
        sc = Scanner.__new__(Scanner)
        sc.latest_regimes = {"NIFTY": "TRENDING_DOWN"}
        sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        trade = SimpleNamespace(instrument="NIFTY", inst_type="OPT", option_type="CE")

        should, reason, ctx = sc._peak_giveback_or_reversal_exit(
            trade,
            current_pnl_rs=700.0,
            peak_pnl=1000.0,
            initial_risk_rs=2000.0,
            smart_min_peak=99999.0,
            latest_time=datetime(2026, 7, 1, 15, 12),
            regime="TRENDING_DOWN",
            inst_type="OPT",
            option_type="CE",
            direction="LONG",
        )

        assert should is True
        assert reason == "PROFIT_GIVEBACK_GUARD"
        assert "keep >= 75%" in ctx
    finally:
        (
            settings.ut_force_exit_time,
            settings.eod_profit_adaptive_peak_enabled,
            settings.eod_profit_adverse_exit_ratio,
            settings.profit_giveback_exit_ratio,
        ) = old_values


def test_eod_profit_giveback_relaxes_when_late_context_supportive():
    from config.settings import get_settings

    settings = get_settings()
    old_values = (
        settings.ut_force_exit_time,
        settings.eod_profit_adaptive_peak_enabled,
        settings.eod_profit_supportive_exit_ratio,
        settings.profit_giveback_exit_ratio,
    )
    settings.ut_force_exit_time = "15:22"
    settings.eod_profit_adaptive_peak_enabled = True
    settings.eod_profit_supportive_exit_ratio = 0.45
    settings.profit_giveback_exit_ratio = 0.50
    try:
        sc = Scanner.__new__(Scanner)
        sc.latest_regimes = {"NIFTY": "TRENDING_UP"}
        sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
        trade = SimpleNamespace(instrument="NIFTY", inst_type="OPT", option_type="CE")

        should, _, _ = sc._peak_giveback_or_reversal_exit(
            trade,
            current_pnl_rs=700.0,
            peak_pnl=1000.0,
            initial_risk_rs=2000.0,
            smart_min_peak=99999.0,
            latest_time=datetime(2026, 7, 1, 15, 12),
            regime="TRENDING_UP",
            inst_type="OPT",
            option_type="CE",
            direction="LONG",
        )

        assert should is False
    finally:
        (
            settings.ut_force_exit_time,
            settings.eod_profit_adaptive_peak_enabled,
            settings.eod_profit_supportive_exit_ratio,
            settings.profit_giveback_exit_ratio,
        ) = old_values


def test_trade_direction_view_derives_bidirectional_option_context():
    assert Scanner._trade_direction_view(SimpleNamespace(inst_type="OPT", option_symbol="NIFTY14JUL2624500CE")) == "LONG"
    assert Scanner._trade_direction_view(SimpleNamespace(inst_type="OPT", option_symbol="NIFTY14JUL2624500PE")) == "SHORT"
    assert Scanner._trade_direction_view(SimpleNamespace(trade_side="BUY CE")) == "LONG"
    assert Scanner._trade_direction_view(SimpleNamespace(trade_side="BUY PE")) == "SHORT"
    assert Scanner._trade_direction_view(SimpleNamespace(underlying_direction="LONG")) == "LONG"
    assert Scanner._trade_direction_view(SimpleNamespace(underlying_direction="SHORT")) == "SHORT"


def test_live_direction_context_unknown_direction_stays_neutral():
    sc = Scanner.__new__(Scanner)
    sc.latest_regimes = {"NIFTY": "TRENDING_UP"}
    sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    trade = SimpleNamespace(instrument="NIFTY")

    ctx = sc._live_direction_context(trade, "", "TRENDING_UP")

    assert ctx["aligned"] is False
    assert ctx["opposed"] is False
    assert ctx["supportive"] is False
    assert ctx["adverse"] is False


def test_live_regime_reversal_takes_profit():
    """Fix 3: a profitable open trade whose live regime flips against it and whose
    profit is fading is taken off — but only when the regime truly opposes."""
    sc = Scanner.__new__(Scanner)
    sc.latest_regimes = {"MIDCPNIFTY": "TRENDING_DOWN"}
    trade = SimpleNamespace(instrument="MIDCPNIFTY 14525CE")

    # Long CE, regime flipped down, profit fading off peak -> take profit.
    should, reason, _ = sc._peak_giveback_or_reversal_exit(
        trade, current_pnl_rs=700.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=99999.0, latest_time=None, regime="TRENDING_DOWN",
        inst_type="OPT", option_type="CE", direction="LONG",
    )
    assert should is True
    assert reason == "LIVE_REVERSAL_EXIT"

    # A PE in the same downtrend is aligned/winning — must NOT be cut.
    hold, _, _ = sc._peak_giveback_or_reversal_exit(
        trade, current_pnl_rs=700.0, peak_pnl=900.0, initial_risk_rs=_MIDCAP_RISK,
        smart_min_peak=99999.0, latest_time=None, regime="TRENDING_DOWN",
        inst_type="OPT", option_type="PE", direction="SHORT",
    )
    assert hold is False


def test_late_entry_marked_protect_first_profit():
    """Fix 4: an entry in the final minutes is forced scalp-lock (protect first profit)."""
    sc = Scanner.__new__(Scanner)
    late = SimpleNamespace(signal_timestamp=datetime(2026, 7, 1, 14, 59, 23))
    midday = SimpleNamespace(signal_timestamp=datetime(2026, 7, 1, 10, 0))
    assert sc._late_entry_scalp_active(late) is True
    assert sc._late_entry_scalp_active(midday) is False

    setattr(late, "_late_entry_scalp", True)
    sc._set_scalp_lock(late, False)  # normal decision is non-scalp, but late forces scalp
    assert late.scalp_lock_mode is True

    setattr(midday, "_late_entry_scalp", False)
    sc._set_scalp_lock(midday, False)
    assert midday.scalp_lock_mode is False


def test_pe_trailing_stop_validation_uses_premium_price_direction():
    candidate = _option_candidate()

    assert Scanner._candidate_trailing_stop_is_valid(candidate, 810.0, 820.0)
    assert not Scanner._candidate_trailing_stop_is_valid(candidate, 830.0, 820.0)


def test_persisted_loss_cannot_remain_labelled_smart_profit_lock():
    scanner = Scanner.__new__(Scanner)
    scanner.futures_sl_pct = 0.2
    scanner.options_sl_pct = 10.0
    candidate = _option_candidate(
        action="EXIT",
        exit_reason="SMART_PROFIT_LOCK",
        exit_price=785.08,
        pnl=-607.10,
    )

    changed = scanner._normalize_loaded_exit_candidate(candidate)

    assert changed is True
    assert candidate.exit_reason == "LEGACY_LOCK_ERROR"
    assert candidate.exit_price == 785.08
    assert candidate.pnl < 0


def test_persisted_quote_invalid_is_not_reclassified_as_stop_hit():
    scanner = Scanner.__new__(Scanner)
    scanner.futures_sl_pct = 0.2
    scanner.options_sl_pct = 7.0
    candidate = _option_candidate(
        direction="SHORT",
        action="EXIT",
        price=806.85,
        stop=758.44,
        exit_reason="QUOTE_INVALID",
        exit_price=305.0,
        current_price=305.0,
        lot_size=120,
        pnl=0.0,
    )
    candidate.inst_type = "OPT"
    candidate.option_type = "PE"

    scanner._normalize_loaded_exit_candidate(candidate)

    assert candidate.exit_reason == "QUOTE_INVALID"
    assert candidate.exit_price == 305.0
    assert candidate.current_price == 305.0
    assert candidate.pnl == 0.0


def test_exit_before_entry_session_row_is_quarantined():
    scanner = Scanner.__new__(Scanner)
    entry_ts = datetime(2026, 6, 15, 12, 17)
    candidate = _option_candidate(
        instrument="SENSEX",
        action="EXIT",
        signal_timestamp=entry_ts,
        entry_timestamp=entry_ts,
        exit_timestamp=entry_ts - timedelta(seconds=36),
        pnl=1075.0,
    )
    scanner.mode = "REAL"
    scanner._process_started_at = None
    scanner._session_trade_candidates = {"SENSEX": {"bad": candidate}}
    scanner._ensure_session_candidate_day = lambda: None

    rows = scanner._get_session_trade_candidates("SENSEX")

    assert rows == []
    assert candidate.invalid_chronology is True
    assert candidate.status == "QUARANTINED - INVALID CHRONOLOGY"


def test_opposite_signal_exits_broker_backed_open_trade_once():
    scanner = Scanner.__new__(Scanner)
    signal_ts = datetime(2026, 6, 15, 12, 30)
    signal = SimpleNamespace(signal_type="SELL", timestamp=signal_ts, price=23950.0)
    trade = SimpleNamespace(
        id="T-OPEN",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="FUT",
        current_price=23955.0,
    )
    calls = []
    open_trades = {"T-OPEN": trade}
    scanner.trades = SimpleNamespace(open_trades=open_trades)
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 23948.0)
    scanner._get_live_premium_cached = lambda trade: 0.0
    def close_and_record(*args, **kwargs):
        calls.append((args, kwargs))
        open_trades.pop(args[0], None)

    scanner._close_and_record = close_and_record
    scanner._last_opposite_exit_signal_time = {}
    scanner._latest_trade_candidates = {}
    scanner._active_session_entry_candidates = lambda *args, **kwargs: []
    # Live mode now repaint-confirms the opposite flip before exiting.
    scanner.mode = "REAL"
    scanner._pending_opposite_exit = {}
    scanner.log_event = lambda *a, **k: None

    # First sighting holds (pending repaint-confirm) — no exit yet.
    assert scanner._handle_opposite_signal_exit("NIFTY", signal, "5min") is False
    assert calls == []

    # Survive the stabilization buffer, then it confirms and exits exactly once.
    for key in list(scanner._pending_opposite_exit):
        scanner._pending_opposite_exit[key] -= 60.0
    assert scanner._handle_opposite_signal_exit("NIFTY", signal, "5min") is True
    assert scanner._handle_opposite_signal_exit("NIFTY", signal, "5min") is False
    assert calls == [
        (
            ("T-OPEN", 23948.0, "OPPOSITE_SIGNAL"),
            {"exit_time": signal_ts},
        )
    ]
