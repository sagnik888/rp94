"""Regression tests for mid-session diagnostics rehydrate + rupee loss ladder.

Covers fixes from live-session forensics (2026-07-09):
  * rehydrate exit_reasons / rejects after process restart
  * concurrency blocks count into diagnostics rejects
  * rupee ladder: freefall specials OR slow bleed (not naked arm threshold)
  * OPT/FUT product arms
  * former-winner recovery breathing -500..-700
  * early peak protect fully rolled back
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config.settings import get_settings
from engine.signal_processor import TradeCandidate
from scanner import Scanner
from trading.trade_manager import IST


def _scanner() -> Scanner:
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._diagnostics = {
        "rejects": {},
        "exit_reasons": {},
        "latency": {"last_ms": 0, "high_latency_events": 0},
    }
    sc._diagnostic_unique_events = set()
    sc._session_trade_candidates = {}
    sc._diagnostics_rehydrated_day = ""
    # staticmethod: bind as plain function (not instance method)
    sc._setting_enabled = lambda value, default=True: Scanner._setting_enabled(value, default)
    sc._strict_setting_enabled = lambda value, default=False: Scanner._strict_setting_enabled(
        value, default
    )
    # Bind methods that don't need full init
    sc._diag_inc = Scanner._diag_inc.__get__(sc, Scanner)
    sc._diag_inc_unique = Scanner._diag_inc_unique.__get__(sc, Scanner)
    sc._record_live_reject = Scanner._record_live_reject.__get__(sc, Scanner)
    sc._rehydrate_live_diagnostics_from_session = (
        Scanner._rehydrate_live_diagnostics_from_session.__get__(sc, Scanner)
    )
    sc._rupee_loss_band_guard_decision = Scanner._rupee_loss_band_guard_decision.__get__(
        sc, Scanner
    )
    sc._rupee_loss_band_freefall_flags = Scanner._rupee_loss_band_freefall_flags.__get__(
        sc, Scanner
    )
    sc._rupee_loss_band_track_slow_bleed = Scanner._rupee_loss_band_track_slow_bleed.__get__(
        sc, Scanner
    )
    sc._loss_recovery_watch_decision = Scanner._loss_recovery_watch_decision.__get__(
        sc, Scanner
    )
    sc._trade_direction_view = lambda *a, **k: Scanner._trade_direction_view(*a, **k)
    sc._regime_opposes_direction = lambda *a, **k: Scanner._regime_opposes_direction(*a, **k)
    sc._eod_protect_active = lambda *_a, **_k: False
    sc.candles = SimpleNamespace(get_latest_price=lambda *_a, **_k: 24000.0)
    sc.log_event = lambda *a, **k: None
    return sc


def _opt_candidate(**kwargs):
    now = datetime.now(IST).replace(tzinfo=None)
    base = dict(
        instrument="BANKNIFTY",
        direction="LONG",
        price=1000.0,
        stop=850.0,
        target=1300.0,
        lots=1,
        lot_size=30,
        grade="A",
        confidence=0.82,
        timeframe="15min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=57200.0,
        multiplier=1.0,
        trading_symbol="BANKNIFTY28JUL2657200CE",
        symbol_token="1",
        rr=3.0,
        score=0.8,
        reasons=[],
        spot_stop=0.0,
        spot_target=0.0,
        signal_timestamp=now - timedelta(minutes=10),
        current_price=980.0,
        pnl=-500.0,
        status="ENTRY",
        action="ENTRY",
    )
    base.update(kwargs)
    c = TradeCandidate(**base)
    setattr(c, "entry_time", base["signal_timestamp"])
    setattr(c, "peak_pnl", float(kwargs.get("peak_pnl", 600.0)))
    setattr(c, "market_regime", "TRENDING_UP")
    return c


# ── Diagnostics rehydrate ─────────────────────────────────────────


def test_early_peak_protect_fully_removed():
    assert not hasattr(Scanner, "_early_peak_protect_decision")
    assert not hasattr(Scanner, "_early_peak_protect_stop_price")
    s = get_settings()
    assert not hasattr(s, "early_peak_protect_enabled")


def test_rehydrate_exit_reasons_from_session_ledger():
    sc = _scanner()
    day = "2026-07-09"
    sc.mode = "REAL"
    sc._session_trade_candidates = {
        "BANKNIFTY": {
            "a": SimpleNamespace(action="EXIT", exit_reason="LOSS_THREAT_EXIT", is_exit=True),
            "b": SimpleNamespace(action="EXIT", exit_reason="PEAK_THREAT_EXIT", is_exit=True),
            "c": SimpleNamespace(action="ENTRY", exit_reason="", is_exit=False),
            "d": SimpleNamespace(action="EXIT", exit_reason="LOSS_THREAT_EXIT", is_exit=True),
        }
    }
    # No signal_decisions dependency
    import data.sqlite_db as sqlite_mod

    original = sqlite_mod.db.load_signal_decisions
    sqlite_mod.db.load_signal_decisions = lambda _day: [
        {"status": "REJECTED", "reason": "Regime Veto: counter trend"},
        {"status": "REJECTED", "reason": "Regime Veto: counter trend"},
        {"status": "ACCEPTED", "reason": "ok"},
        {"status": "MISSED", "reason": "Concurrency: duplicate"},
    ]
    try:
        sc._rehydrate_live_diagnostics_from_session(day)
    finally:
        sqlite_mod.db.load_signal_decisions = original

    assert sc._diagnostics["exit_reasons"]["LOSS_THREAT_EXIT"] == 2
    assert sc._diagnostics["exit_reasons"]["PEAK_THREAT_EXIT"] == 1
    assert sc._diagnostics["rejects"]["Regime Veto: counter trend"] == 2
    assert sc._diagnostics["rejects"]["Concurrency: duplicate"] == 1
    assert sc._diagnostics_rehydrated_day == day

    # Second call same day must not double-count
    sc._diagnostics["exit_reasons"]["LOSS_THREAT_EXIT"] = 99
    sc._rehydrate_live_diagnostics_from_session(day)
    assert sc._diagnostics["exit_reasons"]["LOSS_THREAT_EXIT"] == 99


def test_record_live_reject_unique_and_plain():
    sc = _scanner()
    sc._record_live_reject("Concurrency: dup", identity="BN|15min|LONG|ts1|dup")
    sc._record_live_reject("Concurrency: dup", identity="BN|15min|LONG|ts1|dup")  # unique
    sc._record_live_reject("Concurrency: other", identity="BN|15min|LONG|ts2|other")
    sc._record_live_reject("Plain reject")  # non-unique path
    sc._record_live_reject("Plain reject")
    assert sc._diagnostics["rejects"]["Concurrency: dup"] == 1
    assert sc._diagnostics["rejects"]["Concurrency: other"] == 1
    assert sc._diagnostics["rejects"]["Plain reject"] == 2


# ── Rupee ladder product arms ─────────────────────────────────────


def test_ladder_below_arm_does_not_fire():
    sc = _scanner()
    sc._loss_guard_severity = lambda *a, **k: (0, [], {})
    sc._profit_guard_reversal_context = lambda *a, **k: {"has_context": False}
    sc._live_direction_context = lambda *a, **k: {}
    sc._trade_like_market_intelligence_context = lambda *a, **k: {}
    sc._trade_like_signed_intelligence = lambda *a, **k: 0.0
    sc._view_aligned_intel_score = lambda *a, **k: 0.0
    sc._repaint_close_check_in_window = lambda *a, **k: False
    sc._live_utbot_signal_still_present = lambda *a, **k: True

    c = _opt_candidate()
    now = datetime.now(IST).replace(tzinfo=None)
    fire, reason, detail = sc._rupee_loss_band_guard_decision(
        c, current_pnl_rs=-900.0, latest_time=now, regime="TRENDING_UP", current_price=970.0
    )
    assert fire is False
    assert reason == ""
    assert "ladder start" in detail


def test_ladder_at_arm_without_freefall_or_bleed_holds():
    """Naked -1000 is NOT an auto exit."""
    sc = _scanner()
    sc._loss_guard_severity = lambda *a, **k: (1, [], {"pullback_class": "noise"})
    sc._profit_guard_reversal_context = lambda *a, **k: {
        "has_context": True,
        "fast_reversal": False,
        "one_min_emergency": False,
        "trend_supportive": True,
        "trend_votes": 2,
        "reason": "ok",
    }
    sc._live_direction_context = lambda *a, **k: {"adverse": False, "live_regime": "TRENDING_UP"}
    sc._trade_like_market_intelligence_context = lambda *a, **k: {}
    sc._trade_like_signed_intelligence = lambda *a, **k: 0.0
    sc._view_aligned_intel_score = lambda *a, **k: 0.1
    sc._repaint_close_check_in_window = lambda *a, **k: False
    sc._live_utbot_signal_still_present = lambda *a, **k: True

    c = _opt_candidate()
    now = datetime.now(IST).replace(tzinfo=None)
    # First touch of arm — no 4 min bleed yet
    fire, reason, detail = sc._rupee_loss_band_guard_decision(
        c, current_pnl_rs=-1050.0, latest_time=now, regime="TRENDING_UP", current_price=960.0
    )
    assert fire is False
    assert "hold" in detail
    assert reason == ""


def test_ladder_freefall_fires_with_flags_and_severity():
    sc = _scanner()
    sc._loss_guard_severity = lambda *a, **k: (5, ["reversal dump"], {"pullback_class": "reversal"})
    sc._profit_guard_reversal_context = lambda *a, **k: {
        "has_context": True,
        "fast_reversal": True,
        "one_min_emergency": True,
        "trend_supportive": False,
        "trend_votes": 0,
        "reason": "fast dump",
    }
    sc._live_direction_context = lambda *a, **k: {
        "adverse": True,
        "live_regime": "TRENDING_DOWN",
        "supportive": False,
    }
    sc._trade_like_market_intelligence_context = lambda *a, **k: {}
    sc._trade_like_signed_intelligence = lambda *a, **k: -0.8
    sc._view_aligned_intel_score = lambda *a, **k: -0.8
    sc._repaint_close_check_in_window = lambda *a, **k: False
    sc._live_utbot_signal_still_present = lambda *a, **k: True

    c = _opt_candidate()
    setattr(c, "price_action_verdict", "ADVERSE")
    now = datetime.now(IST).replace(tzinfo=None)
    fire, reason, detail = sc._rupee_loss_band_guard_decision(
        c, current_pnl_rs=-1100.0, latest_time=now, regime="TRENDING_DOWN", current_price=950.0
    )
    assert fire is True
    assert reason == "RUPEE_LOSS_BAND_EXIT"
    assert "FREEFALL" in detail
    assert "OPT" in detail


def test_ladder_slow_bleed_fires_after_time_and_worsen():
    sc = _scanner()
    sc._loss_guard_severity = lambda *a, **k: (1, [], {"pullback_class": "noise"})
    sc._profit_guard_reversal_context = lambda *a, **k: {
        "has_context": True,
        "fast_reversal": False,
        "one_min_emergency": False,
        "trend_supportive": True,
        "trend_votes": 2,
        "reason": "soft",
    }
    sc._live_direction_context = lambda *a, **k: {"adverse": False, "live_regime": "TRENDING_UP"}
    sc._trade_like_market_intelligence_context = lambda *a, **k: {}
    sc._trade_like_signed_intelligence = lambda *a, **k: 0.0
    sc._view_aligned_intel_score = lambda *a, **k: 0.0
    sc._repaint_close_check_in_window = lambda *a, **k: False
    sc._live_utbot_signal_still_present = lambda *a, **k: True

    c = _opt_candidate()
    t0 = datetime.now(IST).replace(tzinfo=None) - timedelta(minutes=5)
    t1 = datetime.now(IST).replace(tzinfo=None)

    # Arm at first deep loss
    fire0, _, detail0 = sc._rupee_loss_band_guard_decision(
        c, current_pnl_rs=-1050.0, latest_time=t0, regime="TRENDING_UP", current_price=960.0
    )
    assert fire0 is False

    # 5 minutes later, worse by >150, no recovery
    fire1, reason1, detail1 = sc._rupee_loss_band_guard_decision(
        c, current_pnl_rs=-1300.0, latest_time=t1, regime="TRENDING_UP", current_price=940.0
    )
    assert fire1 is True
    assert reason1 == "RUPEE_LOSS_BAND_EXIT"
    assert "SLOW_BLEED" in detail1


def test_ladder_catastrophe_force_without_freefall():
    sc = _scanner()
    sc._loss_guard_severity = lambda *a, **k: (0, [], {})
    sc._profit_guard_reversal_context = lambda *a, **k: {"has_context": False}
    sc._live_direction_context = lambda *a, **k: {}
    sc._trade_like_market_intelligence_context = lambda *a, **k: {}
    sc._trade_like_signed_intelligence = lambda *a, **k: 0.0
    sc._view_aligned_intel_score = lambda *a, **k: 0.0
    sc._repaint_close_check_in_window = lambda *a, **k: False
    sc._live_utbot_signal_still_present = lambda *a, **k: True

    c = _opt_candidate()
    now = datetime.now(IST).replace(tzinfo=None)
    fire, reason, detail = sc._rupee_loss_band_guard_decision(
        c, current_pnl_rs=-3200.0, latest_time=now, regime="UNKNOWN", current_price=800.0
    )
    assert fire is True
    assert reason == "RUPEE_LOSS_BAND_EXIT"
    assert "CATASTROPHE" in detail


def test_ladder_fut_arm_higher_than_opt():
    sc = _scanner()
    sc._loss_guard_severity = lambda *a, **k: (0, [], {})
    sc._profit_guard_reversal_context = lambda *a, **k: {"has_context": False}
    sc._live_direction_context = lambda *a, **k: {}
    sc._trade_like_market_intelligence_context = lambda *a, **k: {}
    sc._trade_like_signed_intelligence = lambda *a, **k: 0.0
    sc._view_aligned_intel_score = lambda *a, **k: 0.0
    sc._repaint_close_check_in_window = lambda *a, **k: False
    sc._live_utbot_signal_still_present = lambda *a, **k: True

    fut = _opt_candidate(inst_type="FUT", option_type="", trading_symbol="BANKNIFTY28JUL26FUT")
    now = datetime.now(IST).replace(tzinfo=None)
    # -1200 is past OPT arm but under FUT arm 1500
    fire, _, detail = sc._rupee_loss_band_guard_decision(
        fut, current_pnl_rs=-1200.0, latest_time=now, regime="TRENDING_UP", current_price=57000.0
    )
    assert fire is False
    assert "ladder start" in detail
    assert "FUT" in detail


# ── Recovery breathing ────────────────────────────────────────────


def test_recovery_breathing_holds_former_winner_in_soft_red():
    sc = _scanner()
    c = _opt_candidate()
    setattr(c, "peak_pnl", 800.0)
    setattr(c, "price_action_verdict", "NEUTRAL")
    setattr(c, "price_action_score", 0.0)

    sc._profit_guard_reversal_context = lambda *a, **k: {
        "has_context": True,
        "fast_reversal": False,
        "one_min_emergency": False,
        "trend_supportive": True,
        "trend_votes": 2,
        "reason": "trend ok, momentum soft",
    }
    sc._live_direction_context = lambda *a, **k: {
        "adverse": False,
        "supportive": True,
        "live_regime": "TRENDING_UP",
    }
    sc._trade_like_market_intelligence_context = lambda *a, **k: {}
    sc._trade_like_signed_intelligence = lambda *a, **k: 0.2
    sc._view_aligned_intel_score = lambda *a, **k: 0.2
    sc._option_breathing_distance = lambda *a, **k: 50.0

    now = datetime.now(IST).replace(tzinfo=None)
    # -600 within -500..-700 breathing for former winner
    action, msg = sc._loss_recovery_watch_decision(
        c,
        current_price=980.0,
        current_pnl_rs=-600.0,
        peak_pnl=800.0,
        initial_risk_rs=2700.0,
        latest_time=now,
        regime="TRENDING_UP",
        proposed_exit_reason="EARLY_LOSS_CUT",
    )
    assert action == "HOLD", msg


def test_recovery_exits_beyond_700_breathing_cap():
    sc = _scanner()
    c = _opt_candidate()
    setattr(c, "peak_pnl", 800.0)
    setattr(c, "price_action_verdict", "NEUTRAL")
    setattr(c, "_loss_recovery_watch_started_at", datetime.now(IST).replace(tzinfo=None))

    sc._profit_guard_reversal_context = lambda *a, **k: {
        "has_context": True,
        "fast_reversal": False,
        "one_min_emergency": False,
        "trend_supportive": True,
        "trend_votes": 2,
        "reason": "trend ok",
    }
    sc._live_direction_context = lambda *a, **k: {"adverse": False, "live_regime": "TRENDING_UP"}
    sc._trade_like_market_intelligence_context = lambda *a, **k: {}
    sc._trade_like_signed_intelligence = lambda *a, **k: 0.2
    sc._view_aligned_intel_score = lambda *a, **k: 0.2
    sc._option_breathing_distance = lambda *a, **k: 50.0

    now = datetime.now(IST).replace(tzinfo=None)
    action, msg = sc._loss_recovery_watch_decision(
        c,
        current_price=960.0,
        current_pnl_rs=-850.0,
        peak_pnl=800.0,
        initial_risk_rs=2700.0,
        latest_time=now,
        regime="TRENDING_UP",
        proposed_exit_reason="EARLY_LOSS_CUT",
    )
    assert action == "EXIT", msg
    assert "breathing cap" in msg or "beyond" in msg


def test_settings_product_arms_and_slow_bleed_defaults():
    s = get_settings()
    assert s.rupee_loss_band_enabled is True
    assert s.rupee_loss_band_opt_only is False
    assert s.rupee_loss_band_start_rs_opt == 1000.0
    assert s.rupee_loss_band_start_rs_fut == 1500.0
    assert s.rupee_loss_band_freefall_enabled is True
    assert s.rupee_loss_band_slow_bleed_enabled is True
    assert s.rupee_loss_band_min_freefall_flags >= 2
    assert s.loss_recovery_watch_max_loss_rs == 700.0
    assert s.loss_recovery_watch_soft_loss_rs == 500.0


def test_pipeline_order_contains_ladder_after_loss_guard():
    """Static audit: session + live paths call ladder near loss guard."""
    from pathlib import Path

    src = Path("scanner.py").read_text(encoding="utf-8")
    # Both management paths must invoke ladder
    assert src.count("_rupee_loss_band_guard_decision(") >= 3  # def + 2 call sites
    # Session path: loss guard then ladder (by source order)
    sess = src.find("def _check_session_candidates_breaches_locked")
    assert sess > 0
    chunk = src[sess : sess + 25000]
    i_loss = chunk.find("_loss_guard_decision(")
    i_band = chunk.find("_rupee_loss_band_guard_decision(")
    i_threat = chunk.find("_peak_threat_exit_decision(")
    assert 0 < i_loss < i_band < i_threat, (i_loss, i_band, i_threat)

    # Live path: same relative order near trade management
    # Find second occurrence region after session function ends roughly
    live_idx = src.find("# Early loss-guard (symmetric FUT/OPT)")
    if live_idx < 0:
        live_idx = src.find("Early Loss Cut for")
    assert live_idx > 0
    live_chunk = src[live_idx : live_idx + 8000]
    assert "_rupee_loss_band_guard_decision(" in live_chunk
    assert "_peak_threat_exit_decision(" in live_chunk
