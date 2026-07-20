"""Regression for audit fixes R3 (heavy-trend lag), R4 (cascade buckets), R5 (dashboard schema)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config.settings import get_settings
from engine.signal_processor import TradeCandidate


def _scanner():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc.inst_pref = "AUTO"
    sc.mode = "HISTORICAL"
    sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    sc.trades = SimpleNamespace(open_trades={})
    sc._cached_results = {}
    sc._latest_trade_candidates = {}
    sc._pending_live_signals = {}
    sc._heavy_trend_votes = {}
    sc._setting_enabled = lambda v, d=True: bool(v) if isinstance(v, bool) else d
    sc.log_event = lambda *a, **k: None
    return sc


def _cand(inst, direction="LONG", tf="15min", cascade=False, conf=0.9):
    return TradeCandidate(
        instrument=inst,
        direction=direction,
        price=100.0,
        stop=90.0,
        target=120.0,
        lots=1,
        lot_size=25,
        grade="A",
        confidence=conf,
        timeframe=tf,
        inst_type="OPT",
        option_type="CE",
        atm_strike=25000.0,
        multiplier=0.5,
        trading_symbol=f"{inst}CE",
        symbol_token="1",
        rr=1.5,
        score=conf,
        reasons=[],
        action="ENTRY",
        status="TRADE SIGNAL",
        one_way_cascade_entry=cascade,
        signal_timestamp=datetime(2026, 7, 9, 10, 0, 0),
    )


# ── R3: heavy-trend no lag-1 ─────────────────────────────────────────

def test_r3_same_wave_vote_registry_aligns_without_open_trades():
    sc = _scanner()
    # No open trades, empty cache — only wave votes
    sc._register_heavy_trend_vote("BANKNIFTY", "LONG", "15min")
    assert sc._heavy_trend_lot_scale_eligible("NIFTY", "LONG", "15min") is True


def test_r3_ingest_wave_candidates_counts_peers():
    sc = _scanner()
    now = datetime.now()
    wave = [
        _cand("NIFTY", "LONG"),
        _cand("SENSEX", "LONG"),
        _cand("MIDCPNIFTY", "SHORT"),  # opposite — not counted for LONG
    ]
    for c in wave:
        c.signal_timestamp = now
    sc._ingest_wave_heavy_trend_votes(wave)
    # NIFTY + SENSEX LONG = 2
    assert sc._count_aligned_index_trend("LONG", "15min", include_self="NIFTY") >= 2
    assert sc._heavy_trend_lot_scale_eligible("BANKNIFTY", "LONG", "15min") is True


def test_r3_stale_votes_expire():
    sc = _scanner()
    old = datetime.now() - timedelta(seconds=900)
    sc._heavy_trend_votes = {
        "BANKNIFTY": {"direction": "LONG", "timeframe": "15min", "ts": old},
    }
    # Self only after expiry of peer
    assert sc._heavy_trend_lot_scale_eligible("NIFTY", "LONG", "15min") is False


# ── R4: cascade bucket / global caps ─────────────────────────────────

def test_r4_cascade_raises_core_bucket_cap():
    sc = _scanner()
    normal = sc._active_bucket_cap("CORE", _cand("NIFTY", cascade=False))
    casc = sc._active_bucket_cap("CORE", _cand("NIFTY", cascade=True))
    assert casc >= 12
    assert casc > normal or casc == max(normal, 12)


def test_r4_cascade_raises_midcap_bucket_cap():
    sc = _scanner()
    normal = sc._active_bucket_cap("MIDCAP", _cand("MIDCPNIFTY", cascade=False))
    casc = sc._active_bucket_cap("MIDCAP", _cand("MIDCPNIFTY", cascade=True))
    assert casc >= 3
    assert casc >= normal


def test_r4_trade_manager_cascade_wave_capacity():
    from trading.trade_manager import TradeManager
    import threading

    tm = TradeManager.__new__(TradeManager)
    tm.max_positions = 12
    tm.open_trades = {}
    tm.orphan_positions = {}
    tm._lock = threading.RLock()
    tm._cascade_wave_capacity = False

    assert tm._effective_max_positions() == 12
    tm.arm_cascade_wave_capacity(True)
    assert tm._effective_max_positions() >= 18
    # Fill 12 ghosts not needed — just verify can open under raised cap
    for i in range(12):
        tm.open_trades[f"t{i}"] = SimpleNamespace(is_ghost=False, one_way_cascade_entry=True)
    ok, _ = tm.can_open_trade()
    assert ok is True  # 12 < 18


# ── R5: settings surface ─────────────────────────────────────────────

def test_r5_settings_defaults_present():
    s = get_settings()
    assert s.heavy_trend_vote_max_age_seconds >= 30
    assert s.cascade_core_bucket_max_positions >= 9
    assert s.cascade_midcap_bucket_max_positions >= 2
    assert s.cascade_max_concurrent_positions >= 12
    assert s.cascade_midcap_solo_max_tf == 2
    assert s.dual_clone_reduction_enabled is True


def test_r5_server_advanced_fields_include_new_knobs():
    from dashboard import server as srv

    fields = srv._ADVANCED_CONFIG_FIELDS
    for key in (
        "heavy_trend_lot_scale_enabled",
        "cascade_multi_tf_stack_enabled",
        "cascade_midcap_solo_max_tf",
        "cascade_core_bucket_max_positions",
        "cascade_max_concurrent_positions",
        "multi_signal_mixed_book_enabled",
        "dual_clone_reduction_enabled",
        "cascade_lot_multiplier_opt",
    ):
        assert key in fields, key
    for key in (
        "heavy_trend_lot_scale_enabled",
        "cascade_multi_tf_stack_enabled",
        "multi_signal_mixed_book_enabled",
        "dual_clone_reduction_enabled",
    ):
        assert key in srv._ADVANCED_BOOLEAN_CONFIG_FIELDS, key
