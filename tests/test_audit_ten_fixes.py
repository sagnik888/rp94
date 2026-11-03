"""Regression suite: fixed-lot sizing + symmetric loss-guard (post operator policy)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from engine.position_sizing import (
    lots_from_risk_budget,
    resolve_trade_lots,
    size_lots_for_trade,
)
from engine.risk_manager import RiskManager
from trading.trade_manager import TradeManager


# ─── Fixed-lot policy (operator default) ─────────────────────────────────────


def test_default_sizing_honors_user_lots_without_hardcap():
    s = Settings(
        capital_opt=500_000.0,
        risk_opt_pct=8.0,
        max_trade_loss_abs=12_000.0,
        auto_risk_sizing_enabled=False,
        risk_based_lots_enabled=False,
    )
    # Premium 400, stop 340 (60 pts), lot 15 → would be risk-capped under old policy
    result = resolve_trade_lots(
        settings=s,
        inst_type="OPT",
        requested_lots=20,
        lot_size=15,
        entry_price=400.0,
        stop_price=340.0,
    )
    assert result.lots == 20
    assert result.mode == "fixed"


def test_default_fut_sizing_hard_capped_at_one_lot():
    """Operator rule (2026-07-10): futures are ALWAYS 1 lot by default."""
    s = Settings(auto_risk_sizing_enabled=False, max_trade_loss_abs=500.0)
    result = resolve_trade_lots(
        settings=s,
        inst_type="FUT",
        requested_lots=10,
        lot_size=25,
        entry_price=25_000.0,
        stop_price=24_975.0,
    )
    assert result.lots == 1
    assert result.mode == "fixed"


def test_fut_sizing_honors_user_lots_when_cap_raised():
    """Raising fut_lots_hard_cap restores user-lot sizing (no hidden rupee cap)."""
    s = Settings(
        auto_risk_sizing_enabled=False,
        max_trade_loss_abs=500.0,
        fut_lots_hard_cap=10,
    )
    result = resolve_trade_lots(
        settings=s,
        inst_type="FUT",
        requested_lots=10,
        lot_size=25,
        entry_price=25_000.0,
        stop_price=24_975.0,  # one lot risk 625 > old cap 500
    )
    assert result.lots == 10
    assert result.mode == "fixed"


def test_cascade_multiplies_configured_lots():
    s = Settings(
        auto_risk_sizing_enabled=False,
        risk_based_lots_enabled=False,
        cascade_lot_scale_enabled=True,
        cascade_lot_multiplier=2.0,
        cascade_lot_multiplier_fut=1.0,
        cascade_lot_multiplier_opt=3.0,
        fut_lots_hard_cap=2,  # raise cap so the FUT-flat-multiplier spec is visible
    )
    fut = resolve_trade_lots(
        settings=s,
        inst_type="FUT",
        requested_lots=2,
        lot_size=25,
        entry_price=25_000.0,
        stop_price=24_975.0,
        cascade_scale=True,
    )
    # FUT flat: 2 × 1.0 = 2
    assert fut.lots == 2
    assert fut.mode == "fixed"

    opt = resolve_trade_lots(
        settings=s,
        inst_type="OPT",
        requested_lots=2,
        lot_size=25,
        entry_price=100.0,
        stop_price=85.0,
        cascade_scale=True,
    )
    # OPT-only expand: 2 × 3.0 = 6
    assert opt.lots == 6
    assert opt.mode == "cascade"
    assert opt.lots > fut.lots


def test_cascade_opt_scales_more_than_fut_same_base():
    """Cascade expands OPT only; FUT multiplier is 1.0 (flat)."""
    from engine.loss_guard_policy import cascade_multiplier_for_inst

    s = Settings(
        cascade_lot_multiplier_fut=1.0,
        cascade_lot_multiplier_opt=3.0,
    )
    assert cascade_multiplier_for_inst(s, "FUT") == pytest.approx(1.0)
    assert cascade_multiplier_for_inst(s, "OPT") > cascade_multiplier_for_inst(s, "FUT")


def test_r1_high_conviction_alone_does_not_scale_lots():
    """R1: A+/power_move style high_conviction must NOT multiply lots."""
    s = Settings(
        auto_risk_sizing_enabled=False,
        cascade_lot_scale_enabled=True,
        cascade_lot_multiplier=2.0,
        fut_lots_hard_cap=2,  # visible base so a hidden multiply would show as 4
    )
    result = resolve_trade_lots(
        settings=s,
        inst_type="FUT",
        requested_lots=2,
        lot_size=25,
        entry_price=25_000.0,
        stop_price=24_975.0,
        cascade_scale=False,
        high_conviction=True,
    )
    assert result.lots == 2
    assert result.mode == "fixed"


def test_heavy_trend_opt_scales_2x_only_fut_flat():
    """Non-cascade heavy trend (2/4 align): OPT ×2 max; FUT 1×."""
    s = Settings(
        auto_risk_sizing_enabled=False,
        risk_based_lots_enabled=False,
        heavy_trend_lot_scale_enabled=True,
        heavy_trend_lot_multiplier_opt=2.0,
        heavy_trend_lot_multiplier_fut=1.0,
        fut_lots_hard_cap=2,  # raise cap so FUT-flat (no ×2) stays observable
    )
    opt = resolve_trade_lots(
        settings=s,
        inst_type="OPT",
        requested_lots=2,
        lot_size=25,
        entry_price=100.0,
        stop_price=85.0,
        heavy_trend_scale=True,
    )
    assert opt.lots == 4
    assert opt.mode == "heavy_trend"

    fut = resolve_trade_lots(
        settings=s,
        inst_type="FUT",
        requested_lots=2,
        lot_size=25,
        entry_price=25_000.0,
        stop_price=24_975.0,
        heavy_trend_scale=True,
    )
    assert fut.lots == 2
    assert fut.mode == "fixed"


def test_cascade_wins_over_heavy_trend_when_both():
    s = Settings(
        auto_risk_sizing_enabled=False,
        risk_based_lots_enabled=False,
        cascade_lot_scale_enabled=True,
        cascade_lot_multiplier_opt=3.0,
        cascade_lot_multiplier_opt_min=2.0,
        heavy_trend_lot_multiplier_opt=2.0,
    )
    opt = resolve_trade_lots(
        settings=s,
        inst_type="OPT",
        requested_lots=2,
        lot_size=25,
        entry_price=100.0,
        stop_price=85.0,
        cascade_scale=True,
        heavy_trend_scale=True,
    )
    assert opt.lots == 6  # cascade 3× wins
    assert opt.mode == "cascade"


def test_cascade_opt_mult_clamped_between_2_and_3():
    from engine.loss_guard_policy import cascade_multiplier_for_inst

    # Hard ceiling 3× even if settings try higher
    s_hi = Settings(
        cascade_lot_multiplier_opt=5.0,
        cascade_lot_multiplier_opt_min=2.0,
    )
    assert cascade_multiplier_for_inst(s_hi, "OPT") == pytest.approx(3.0)
    s2 = Settings(
        cascade_lot_multiplier_opt=3.0,
        cascade_lot_multiplier_opt_min=2.0,
    )
    assert cascade_multiplier_for_inst(s2, "OPT") == pytest.approx(3.0)
    s3 = Settings(
        cascade_lot_multiplier_opt=2.0,
        cascade_lot_multiplier_opt_min=2.0,
    )
    assert cascade_multiplier_for_inst(s3, "OPT") == pytest.approx(2.0)


def test_rest_always_1x_without_cascade_or_heavy_trend():
    s = Settings(
        auto_risk_sizing_enabled=False,
        risk_based_lots_enabled=False,
        cascade_lot_scale_enabled=True,
        fut_lots_hard_cap=3,  # cap above request so 1× (no multiply) is observable
    )
    for inst in ("OPT", "FUT"):
        r = resolve_trade_lots(
            settings=s,
            inst_type=inst,
            requested_lots=3,
            lot_size=25,
            entry_price=100.0 if inst == "OPT" else 25_000.0,
            stop_price=85.0 if inst == "OPT" else 24_975.0,
            cascade_scale=False,
            heavy_trend_scale=False,
            high_conviction=True,
        )
        assert r.lots == 3
        assert r.mode == "fixed"


def test_r7_cascade_max_lots_cap():
    s = Settings(
        auto_risk_sizing_enabled=False,
        risk_based_lots_enabled=False,
        cascade_lot_scale_enabled=True,
        cascade_lot_multiplier=3.0,
        cascade_lot_max_lots=5,
    )
    result = resolve_trade_lots(
        settings=s,
        inst_type="OPT",
        requested_lots=20,
        lot_size=15,
        entry_price=400.0,
        stop_price=340.0,
        cascade_scale=True,
    )
    assert result.lots == 5


def test_auto_risk_sizing_optional_still_works_when_enabled():
    s = Settings(
        capital_fut=1_000_000.0,
        risk_fut_pct=1.0,
        max_trade_loss_abs=5_000.0,
        auto_risk_sizing_enabled=True,
        risk_per_trade_daily_fraction=1.0,
        fut_lots_hard_cap=10,  # exercise the risk-budget clamp, not the FUT cap
    )
    result = size_lots_for_trade(
        settings=s,
        inst_type="FUT",
        requested_lots=10,
        lot_size=25,
        entry_price=25_000.0,
        stop_price=24_975.0,
    )
    assert result.lots == 8  # floor(5000/625)


def test_risk_configuration_summary_fixed_mode():
    s = Settings(auto_risk_sizing_enabled=False)
    summary = s.risk_configuration_summary()
    assert summary["ok"] is True
    assert summary["sizing_mode"] == "fixed_lots"
    assert summary["auto_risk_sizing_enabled"] is False


# ─── TARGET_HIT + EARLY_LOSS_CUT ─────────────────────────────────────────────


def test_update_trade_closes_on_target_hit_long_fut():
    tm = TradeManager(broker=None, max_positions=5)
    tm.mode = "HISTORICAL"
    tm.open_trades.clear()
    trade = tm.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=95.0,
        lots=1,
        lot_size=10,
        grade="A",
        target=110.0,
        inst_type="FUT",
        is_ghost=True,
        initial_stop=95.0,
    )
    tid = trade.id
    tm.update_trade(tid, current_spot=111.0, real_fut_price=111.0)
    assert tid not in tm.open_trades
    closed = next(t for t in tm.closed_trades if t.id == tid)
    assert closed.exit_reason == "TARGET_HIT"


def test_early_loss_cut_fut_at_r_multiple():
    tm = TradeManager(broker=None, max_positions=5)
    tm.mode = "HISTORICAL"
    tm.open_trades.clear()
    trade = tm.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=95.0,  # 5 pts risk
        lots=1,
        lot_size=10,
        grade="A",
        target=110.0,
        inst_type="FUT",
        is_ghost=True,
        initial_stop=95.0,
    )
    tid = trade.id
    # At 0.8R loss: entry 100, risk 5 → cut at 100 - 4 = 96
    # Price 95.9 should trigger EARLY_LOSS_CUT (loss 4.1 > 4.0)
    tm.update_trade(tid, current_spot=95.9, real_fut_price=95.9, new_trailing_stop=95.0)
    assert tid not in tm.open_trades
    closed = next(t for t in tm.closed_trades if t.id == tid)
    assert closed.exit_reason == "EARLY_LOSS_CUT"


def test_early_loss_cut_opt_symmetric():
    tm = TradeManager(broker=None, max_positions=5)
    tm.mode = "HISTORICAL"
    tm.open_trades.clear()
    trade = tm.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=85.0,  # 15 pts premium risk
        lots=1,
        lot_size=25,
        grade="A",
        target=130.0,
        inst_type="OPT",
        option_type="CE",
        instrument_multiplier=0.5,
        entry_spot=25_000.0,
        is_ghost=True,
        initial_stop=85.0,
    )
    tid = trade.id
    # 0.8R of 15 = 12 → cut when premium <= 88
    tm.update_trade(
        tid,
        current_spot=24_900.0,
        real_premium=87.0,
        new_trailing_stop=85.0,
    )
    assert tid not in tm.open_trades
    closed = next(t for t in tm.closed_trades if t.id == tid)
    assert closed.exit_reason == "EARLY_LOSS_CUT"


def test_option_trail_premium_space_ratchet():
    tm = TradeManager(broker=None, max_positions=5)
    tm.mode = "HISTORICAL"
    tm.open_trades.clear()
    trade = tm.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="SHORT",
        price=100.0,
        trailing_stop=80.0,
        lots=1,
        lot_size=25,
        grade="A",
        target=0.0,
        inst_type="OPT",
        option_type="PE",
        instrument_multiplier=-0.5,
        entry_spot=25_000.0,
        is_ghost=True,
        initial_stop=80.0,
        confidence=0.8,  # Ordinary setup; an omitted 0.0 score requires the 5% cap.
    )
    tid = trade.id
    tm.update_trade(
        tid,
        current_spot=24_900.0,
        real_premium=110.0,
        new_trailing_stop=90.0,
    )
    assert tid in tm.open_trades
    assert trade.current_stop == pytest.approx(90.0)


# ─── RiskManager fail-closed ─────────────────────────────────────────────────


def test_risk_manager_fail_closed_on_bad_closed_trade():
    class _BadTime:
        def astimezone(self, _tz):
            raise ValueError("bad tz")

    class _Trades:
        def __init__(self):
            self.closed_trades = [
                SimpleNamespace(entry_time=_BadTime(), inst_type="OPT", pnl=-100.0)
            ]
            self.open_trades = {}
            self.closed = []

        def close_trade(self, trade_id, price, reason):
            self.closed.append((trade_id, price, reason))

        def emergency_flatten_trade(self, trade, reason):
            pass

    trades = _Trades()
    rm = RiskManager(trades, 100_000.0)
    rm.update_settings(
        max_daily_loss_pct=1.0,
        capital_total=100_000.0,
        capital_fut=50_000.0,
        capital_opt=50_000.0,
        risk_fut_pct=2.0,
        risk_opt_pct=2.0,
        max_trade_loss_abs=5_000.0,
        circuit_breaker_slippage_bps=0.0,
    )
    assert rm.check_circuit_breaker() is True
    assert rm.daily_loss_breached is True


# ─── Read API token ──────────────────────────────────────────────────────────


def test_token_required_paths_defined():
    from dashboard import server as dash

    assert "/api/state" in dash._TOKEN_REQUIRED_API_PATHS
    assert "/api/trades" in dash._TOKEN_REQUIRED_API_PATHS
    assert "/api/status" not in dash._TOKEN_REQUIRED_API_PATHS


def test_shared_resolve_importable():
    from engine import signal_processor as sp

    assert hasattr(sp, "resolve_trade_lots") or hasattr(sp, "size_lots_for_trade")


# ─── Intel-accelerated loss guard + pullback classification ──────────────────


def test_early_loss_cut_accepts_lower_override_for_big_reversal():
    """TM path: intel-accelerated cut_r_override fires before base 0.8R."""
    tm = TradeManager(broker=None, max_positions=5)
    tm.mode = "HISTORICAL"
    tm.open_trades.clear()
    trade = tm.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=100.0,
        trailing_stop=90.0,  # 10 pts risk
        lots=1,
        lot_size=10,
        grade="A",
        target=120.0,
        inst_type="FUT",
        is_ghost=True,
        initial_stop=90.0,
    )
    tid = trade.id
    # 0.4R loss = price 96.0 — below 0.8R base, but override 0.35 triggers
    assert TradeManager._early_loss_cut_hit(trade, cut_r_override=0.35) is False  # not updated yet
    trade.current_price = 96.0
    assert TradeManager._early_loss_cut_hit(trade, cut_r_override=0.35) is True
    assert TradeManager._early_loss_cut_hit(trade, cut_r_override=0.80) is False


def test_loss_guard_holds_small_pullback_until_base_r(monkeypatch):
    from scanner import Scanner
    from config.settings import get_settings

    scanner = Scanner.__new__(Scanner)
    scanner._setting_enabled = lambda v, d=True: bool(v) if isinstance(v, bool) else d
    settings = get_settings()
    monkeypatch.setattr(settings, "loss_guard_intel_accelerated_enabled", True)
    monkeypatch.setattr(settings, "fut_early_loss_cut_enabled", True)
    monkeypatch.setattr(settings, "fut_loss_cut_r", 0.8)
    monkeypatch.setattr(settings, "loss_guard_intel_min_r", 0.35)
    monkeypatch.setattr(settings, "loss_guard_soft_intel_r", 0.55)

    trade = SimpleNamespace(
        inst_type="FUT",
        direction="LONG",
        instrument="NIFTY",
        timeframe="5min",
        option_type="",
    )

    def fake_severity(*a, **k):
        return 1, ["price small_pullback"], {"pullback_class": "small_pullback"}

    scanner._loss_guard_severity = fake_severity
    # 0.5R loss should NOT cut when class is small_pullback (needs 0.8R)
    should, detail, cut_r = Scanner._loss_guard_decision(
        scanner,
        trade,
        current_pnl_rs=-500.0,
        initial_risk_rs=1000.0,
        latest_time=None,
        regime="TRENDING_UP",
        current_price=100.0,
    )
    assert should is False
    assert cut_r == pytest.approx(0.8)
    assert "pullback" in detail.lower() or "held" in detail.lower()


def test_loss_guard_accelerates_on_big_reversal_severity(monkeypatch):
    from scanner import Scanner
    from config.settings import get_settings

    scanner = Scanner.__new__(Scanner)
    scanner._setting_enabled = lambda v, d=True: True
    settings = get_settings()
    monkeypatch.setattr(settings, "loss_guard_intel_accelerated_enabled", True)
    monkeypatch.setattr(settings, "fut_early_loss_cut_enabled", True)
    monkeypatch.setattr(settings, "fut_loss_cut_r", 0.8)
    monkeypatch.setattr(settings, "loss_guard_intel_min_r", 0.35)
    monkeypatch.setattr(settings, "loss_guard_soft_intel_r", 0.55)
    monkeypatch.setattr(settings, "loss_guard_big_reversal_severity", 5)

    trade = SimpleNamespace(
        inst_type="FUT",
        direction="LONG",
        instrument="NIFTY",
        timeframe="5min",
        option_type="",
    )

    def fake_severity(*a, **k):
        return 6, ["price big_reversal", "OI BEARISH", "PCR BEARISH"], {
            "pullback_class": "big_reversal"
        }

    scanner._loss_guard_severity = fake_severity
    # 0.40R loss should cut when severity is high (thr 0.35R)
    should, detail, cut_r = Scanner._loss_guard_decision(
        scanner,
        trade,
        current_pnl_rs=-400.0,
        initial_risk_rs=1000.0,
        latest_time=None,
        regime="RANGING",
        current_price=100.0,
    )
    assert should is True
    assert cut_r == pytest.approx(0.35)
    assert "BIG_REVERSE" in detail or "EARLY_LOSS_CUT" in detail


def test_pullback_class_labels_exist_in_reversal_context():
    """Source contract: reversal context must emit pullback_class for live deduction."""
    from pathlib import Path

    src = Path("scanner.py").read_text(encoding="utf-8")
    assert 'pullback_class = "big_reversal"' in src
    assert 'pullback_class = "small_pullback"' in src
    assert "def _loss_guard_decision" in src
    assert "def _loss_guard_severity" in src
    assert "rejection_wick" in src
    assert "grind_reverse" in src
    assert "is_losing" in src
    assert "premium melt" in src or "premium_melt" in src
    assert "intel_stale" in src or "intel stale" in src


# ─── Six gap fixes ───────────────────────────────────────────────────────────


def test_oi_pcr_two_votes_when_losing():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    intel = {
        "pcr": {"signal": "BEARISH", "primary_pcr": 0.7},
        "oi": {"signal": "BEARISH"},
        "order_flow": {"signal": "NEUTRAL", "ratio": 1.0},
    }
    # Winning / default: needs 3 votes → only 2 → None
    assert sc._intel_reversal_reason("LONG", intel, is_losing=False) is None
    # Losing: 2 votes enough
    reason = sc._intel_reversal_reason("LONG", intel, is_losing=True)
    assert reason is not None
    assert "OI_PCR_REVERSAL" in reason
    assert "2/" in reason or "votes=2" in reason


def test_oi_pcr_three_votes_still_fires_when_winning():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    intel = {
        "pcr": {"signal": "BEARISH", "primary_pcr": 0.6},
        "oi": {"signal": "BEARISH"},
        "order_flow": {"signal": "BEARISH", "ratio": 0.7},
    }
    reason = sc._intel_reversal_reason("LONG", intel, is_losing=False)
    assert reason is not None
    assert "votes=3" in reason


def test_intel_age_seconds_from_embedded_timestamp():
    from scanner import Scanner
    from datetime import datetime, timedelta
    import pytz

    IST = pytz.timezone("Asia/Kolkata")
    sc = Scanner.__new__(Scanner)
    sc._last_intel_fetch = {}
    now = datetime.now(IST).replace(tzinfo=None)
    intel = {"as_of": (now - timedelta(seconds=120)).isoformat()}
    age = sc._intel_age_seconds(intel, "NIFTY", now)
    assert age is not None
    assert 100 <= age <= 140


def test_intel_age_from_last_fetch_map():
    from scanner import Scanner
    import time as _time

    sc = Scanner.__new__(Scanner)
    sc._last_intel_fetch = {"NIFTY": _time.time() - 45.0}
    age = sc._intel_age_seconds({}, "NIFTY", None)
    assert age is not None
    assert 40 <= age <= 55


def test_loss_guard_severity_premium_melt_opt(monkeypatch):
    from scanner import Scanner
    from config.settings import get_settings

    sc = Scanner.__new__(Scanner)
    sc._setting_enabled = lambda v, d=True: True
    settings = get_settings()
    monkeypatch.setattr(settings, "loss_guard_opt_premium_melt_pct", 8.0)
    monkeypatch.setattr(settings, "loss_guard_opt_premium_melt_severity", 2)

    trade = SimpleNamespace(
        inst_type="OPT",
        direction="LONG",
        option_type="CE",
        instrument="NIFTY",
        timeframe="5min",
        entry_price=100.0,
        current_price=90.0,
    )

    def fake_ctx(*a, **k):
        return {
            "has_context": True,
            "pullback_class": "small_pullback",
            "fast_reversal": False,
            "trend_supportive": True,
            "one_min_emergency": False,
            "rejection_wick": False,
            "grind_reverse": False,
        }

    sc._profit_guard_reversal_context = fake_ctx
    sc._get_trade_intel = lambda t: {}
    sc._trade_like_market_intelligence_context = lambda t: {}
    sc._intel_age_seconds = lambda *a, **k: 10.0
    sc._intel_reversal_reason = lambda *a, **k: None
    sc._trade_like_signed_intelligence = lambda *a, **k: 0.0
    sc._view_aligned_intel_score = lambda *a, **k: 0.0
    sc._intel_exit_blocked_by_trend = lambda *a, **k: False

    # 10% premium melt from 100 → 90
    sev, reasons, meta = Scanner._loss_guard_severity(
        sc,
        trade,
        current_price=90.0,
        latest_time=None,
        regime="RANGING",
        current_pnl_rs=-250.0,
    )
    assert sev >= 2
    assert any("premium melt" in r.lower() for r in reasons)
    assert meta.get("pullback_class") == "premium_melt"


def test_loss_guard_stale_intel_does_not_full_boost(monkeypatch):
    from scanner import Scanner
    from config.settings import get_settings

    sc = Scanner.__new__(Scanner)
    sc._setting_enabled = lambda v, d=True: True
    settings = get_settings()
    monkeypatch.setattr(settings, "loss_guard_intel_max_age_seconds", 30.0)

    trade = SimpleNamespace(
        inst_type="FUT",
        direction="LONG",
        option_type="",
        instrument="NIFTY",
        timeframe="5min",
        entry_price=100.0,
        current_price=99.0,
    )
    sc._profit_guard_reversal_context = lambda *a, **k: {
        "has_context": True,
        "pullback_class": "small_pullback",
        "fast_reversal": False,
        "trend_supportive": True,
        "one_min_emergency": False,
        "rejection_wick": False,
        "grind_reverse": False,
    }
    sc._get_trade_intel = lambda t: {"oi": {"signal": "BEARISH"}, "as_of": "x"}
    sc._trade_like_market_intelligence_context = lambda t: {}
    sc._intel_age_seconds = lambda *a, **k: 200.0  # stale
    sc._intel_reversal_reason = lambda *a, **k: "OI_PCR_REVERSAL bearish votes=2"
    sc._intel_exit_blocked_by_trend = lambda *a, **k: False
    sc._trade_like_signed_intelligence = lambda *a, **k: -0.8
    sc._view_aligned_intel_score = lambda v, s: s
    sc._intel_signal_opposes_view = lambda sig, view: True

    sev, reasons, meta = Scanner._loss_guard_severity(
        sc,
        trade,
        current_price=99.0,
        latest_time=None,
        regime="RANGING",
        current_pnl_rs=-100.0,
    )
    assert meta.get("intel_stale") is True
    assert any("stale" in r.lower() for r in reasons)
    # Stale OI reverse adds 1 not 3 — total should be lower than fresh path
    assert sev < 8


def test_rejection_wick_and_grind_in_source_and_severity_keys():
    from scanner import Scanner

    # Contract: severity consumes wick/grind flags from price context
    import inspect

    src = inspect.getsource(Scanner._loss_guard_severity)
    assert "rejection_wick" in src
    assert "grind_reverse" in src
    assert "premium melt" in src or "premium_melt" in src
    assert "intel_stale" in src


def test_r4_severity_cap_limits_stacking():
    from engine.loss_guard_policy import cap_loss_guard_severity, cut_r_from_severity

    capped = cap_loss_guard_severity(
        99, price_points=9, intel_points=9, max_total=8, max_price=5, max_intel=5
    )
    assert capped == 8
    r, label = cut_r_from_severity(8, base_r=0.8, min_r=0.35, soft_r=0.55, big_thr=5, soft_thr=3)
    assert r == pytest.approx(0.35)
    assert label == "BIG_REVERSE"
    r2, lab2 = cut_r_from_severity(
        1, base_r=0.8, min_r=0.35, soft_r=0.55, pullback_class="small_pullback"
    )
    assert r2 == pytest.approx(0.8)
    assert lab2 == "pullback_hold"


def test_r8_has_no_collection_time_baseline_quarantine():
    import tests.conftest as conftest

    assert not hasattr(conftest, "_QUARANTINED")
    assert not hasattr(conftest, "pytest_collection_modifyitems")


def test_r2_tm_uses_published_cut_r_override():
    """TM backstop honors scanner-published loss_guard_cut_r (R2)."""
    from trading.trade_manager import TradeManager

    trade = SimpleNamespace(
        inst_type="FUT",
        direction="LONG",
        entry_price=100.0,
        current_price=96.0,  # 0.4R of 10pt risk
        initial_stop=90.0,
        trailing_stop=90.0,
        lots=1,
        lot_size=10,
        loss_guard_cut_r=0.35,
    )
    assert TradeManager._early_loss_cut_hit(trade, cut_r_override=trade.loss_guard_cut_r) is True
    assert TradeManager._early_loss_cut_hit(trade, cut_r_override=0.8) is False
