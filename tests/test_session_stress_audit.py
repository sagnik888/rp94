"""Stress audit for session fixes: mixed book, cascade multi-TF, lot scale, concurrency.

Covers policies agreed in the last chat cycle:
  1. Mixed book: 1 FUT (1 lot) equity rank + rest OPT; MIDCP FUT-blocked
  2. Core cascade stack equal for NIFTY/BN/SENSEX (1m+5m+15m + dual)
  3. MIDCAP: solo best-2 of 1/5/15; dual 2 main + 1 dual (2+1)
  4. Lots: cascade OPT 2–3×, heavy-trend OPT 2×, else 1×; FUT always 1×
  5. Concurrency: normal 2; dual 2+1; cascade core/midcap caps
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config.settings import Settings, get_settings
from engine.loss_guard_policy import (
    cascade_multiplier_for_inst,
    heavy_trend_multiplier_for_inst,
)
from engine.position_sizing import resolve_trade_lots
from engine.signal_processor import SignalProcessor, TradeCandidate


# ── helpers ──────────────────────────────────────────────────────────

def _cand(
    instrument,
    direction="LONG",
    timeframe="5min",
    conf=0.8,
    grade="A",
    inst_type="FUT",
    **kwargs,
):
    c = TradeCandidate(
        instrument=instrument,
        direction=direction,
        price=100.0 if inst_type == "OPT" else 25000.0,
        stop=90.0 if inst_type == "OPT" else 24975.0,
        target=120.0 if inst_type == "OPT" else 25050.0,
        lots=2,
        lot_size=25,
        grade=grade,
        confidence=conf,
        timeframe=timeframe,
        inst_type=inst_type,
        option_type="CE" if direction == "LONG" and inst_type == "OPT" else "",
        atm_strike=25000.0 if inst_type == "OPT" else 0.0,
        multiplier=0.5 if inst_type == "OPT" else 1.0,
        trading_symbol=f"{instrument}-FUT" if inst_type == "FUT" else f"{instrument}25000CE",
        symbol_token="1",
        rr=1.5,
        score=conf,
        reasons=[],
        action="ENTRY",
        status="TRADE SIGNAL",
    )
    for k, v in kwargs.items():
        setattr(c, k, v)
    return c


def _scanner():
    from scanner import Scanner

    sc = Scanner.__new__(Scanner)
    sc.inst_pref = "AUTO"
    sc.mode = "HISTORICAL"
    sc.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    # Operator default base = 1 OPT lot → cascade 1×3 = 3 lots (not 6)
    sc.user_lots = {i: 1 for i in sc.active_indices}
    sc.user_lots_fut = {i: 1 for i in sc.active_indices}
    sc.options_sl_pct = 15.0
    sc.futures_sl_pct = 0.1
    sc.latest_regimes = {i: "TRENDING_UP" for i in sc.active_indices}
    sc.config = {
        "indices": {
            i: {"strike_interval": 50, "lot_size": 25} for i in sc.active_indices
        }
    }
    sc.market_info = {
        i: {"current_fut": f"{i}-I", "current_fut_token": f"t{i}"}
        for i in sc.active_indices
    }
    sc.candles = SimpleNamespace(get_latest_price=lambda name: 25000.0)
    sc.data = MagicMock()
    sc.data.get_option_token.side_effect = lambda inst, strike, ot: {
        "symbol": f"{inst}{int(strike)}{ot}",
        "token": f"tok-{inst}-{strike}-{ot}",
    }
    sc.data.get_ltp.return_value = 150.0
    sc.expiry = SimpleNamespace(get_dte=lambda *a, **k: 3)
    sc.signal_processor = SimpleNamespace(
        _option_signed_delta=lambda *a, **k: 0.5 if a[2] == "CE" else -0.5
    )
    sc.trades = SimpleNamespace(open_trades={})
    sc._cached_results = {}
    sc._latest_trade_candidates = {}
    sc._setting_enabled = lambda v, d=True: bool(v) if isinstance(v, bool) else d
    sc.log_event = lambda *a, **k: None
    sc._source_lane = Scanner._source_lane
    return sc


def _full_cascade_wave(base_conf=0.80):
    """All 4 indices × 3 TFs cascade, plus dual B on 5/15 for core + midcap."""
    cands = []
    specs = [
        ("NIFTY", 0.15),
        ("BANKNIFTY", 0.10),
        ("SENSEX", 0.05),
        ("MIDCPNIFTY", 0.0),
    ]
    for inst, boost in specs:
        for i, tf in enumerate(["1min", "5min", "15min"]):
            cands.append(
                _cand(
                    inst,
                    timeframe=tf,
                    conf=base_conf + boost + i * 0.01,
                    grade="A+" if tf == "15min" else "A",
                    one_way_cascade_entry=True,
                    source_lane="A",
                )
            )
        for tf, conf_extra in (("5min", 0.02), ("15min", 0.03)):
            cands.append(
                _cand(
                    inst,
                    timeframe=tf,
                    conf=base_conf + boost + conf_extra,
                    one_way_cascade_entry=True,
                    source_lane="B",
                )
            )
    return cands


# ── LOT SCALE STRESS ─────────────────────────────────────────────────

class TestLotScalePolicy:
    def test_matrix_cascade_heavy_fixed(self):
        s = Settings(
            auto_risk_sizing_enabled=False,
            cascade_lot_scale_enabled=True,
            cascade_lot_multiplier_opt=3.0,
            cascade_lot_multiplier_opt_min=2.0,
            cascade_lot_multiplier_fut=1.0,
            heavy_trend_lot_scale_enabled=True,
            heavy_trend_lot_multiplier_opt=2.0,
            fut_lots_hard_cap=3,  # raised so FUT flat-multiplier rows stay visible
        )
        # Operator default (2026-07-10): FUT hard-capped at 1 lot out of the box.
        r_default = resolve_trade_lots(
            settings=Settings(auto_risk_sizing_enabled=False),
            inst_type="FUT",
            requested_lots=3,
            lot_size=25,
            entry_price=25000.0,
            stop_price=24975.0,
        )
        assert r_default.lots == 1
        assert r_default.mode == "fixed"
        cases = [
            # (inst, cascade, heavy, base, expected_lots, expected_mode)
            ("OPT", True, False, 2, 6, "cascade"),
            ("OPT", True, True, 2, 6, "cascade"),  # cascade wins
            ("OPT", False, True, 2, 4, "heavy_trend"),
            ("OPT", False, False, 2, 2, "fixed"),
            ("FUT", True, False, 2, 2, "fixed"),
            ("FUT", False, True, 2, 2, "fixed"),
            ("FUT", False, False, 3, 3, "fixed"),
            ("OPT", False, False, 5, 5, "fixed"),
        ]
        for inst, casc, heavy, base, exp_lots, exp_mode in cases:
            r = resolve_trade_lots(
                settings=s,
                inst_type=inst,
                requested_lots=base,
                lot_size=25,
                entry_price=100.0 if inst == "OPT" else 25000.0,
                stop_price=85.0 if inst == "OPT" else 24975.0,
                cascade_scale=casc,
                heavy_trend_scale=heavy,
                high_conviction=True,  # must not multiply alone
            )
            assert r.lots == exp_lots, f"{inst} casc={casc} heavy={heavy}"
            assert r.mode == exp_mode, f"{inst} casc={casc} heavy={heavy} mode"

    def test_opt_cascade_clamped_2_to_3(self):
        assert cascade_multiplier_for_inst(
            Settings(cascade_lot_multiplier_opt=5.0, cascade_lot_multiplier_opt_min=2.0),
            "OPT",
        ) == pytest.approx(3.0)
        assert cascade_multiplier_for_inst(
            Settings(cascade_lot_multiplier_opt=2.0, cascade_lot_multiplier_opt_min=2.0),
            "OPT",
        ) == pytest.approx(2.0)
        assert heavy_trend_multiplier_for_inst(
            Settings(heavy_trend_lot_multiplier_opt=5.0), "OPT"
        ) == pytest.approx(2.0)
        assert heavy_trend_multiplier_for_inst(Settings(), "FUT") == pytest.approx(1.0)

    def test_base_1_lot_cascade_opt_still_scales(self):
        s = Settings(
            auto_risk_sizing_enabled=False,
            cascade_lot_multiplier_opt=3.0,
            cascade_lot_multiplier_opt_min=2.0,
        )
        r = resolve_trade_lots(
            settings=s, inst_type="OPT", requested_lots=1, lot_size=25,
            entry_price=100.0, stop_price=85.0, cascade_scale=True,
        )
        assert r.lots == 3  # 1×3


# ── MIXED BOOK + CASCADE WAVE STRESS ─────────────────────────────────

class TestMixedBookWave:
    def test_full_cascade_wave_invariants(self):
        sc = _scanner()
        out = sc._apply_multi_signal_mixed_book(_full_cascade_wave())

        fut = [c for c in out if c.inst_type == "FUT"]
        opt = [c for c in out if c.inst_type == "OPT"]
        by_inst = {}
        for c in out:
            by_inst.setdefault(c.instrument, []).append(c)

        # Exactly 1 FUT, 1 lot, never MIDCP
        assert len(fut) == 1
        assert fut[0].lots == 1
        assert fut[0].instrument != "MIDCPNIFTY"
        assert fut[0].instrument in {"NIFTY", "BANKNIFTY", "SENSEX"}

        # Core: primary 1m+5m+15m; simultaneous dual same-TF clones reduced away
        for core in ("NIFTY", "BANKNIFTY", "SENSEX"):
            rows = by_inst.get(core, [])
            assert len(rows) >= 3, f"{core} expected multi-TF, got {len(rows)}"
            tfs = {c.timeframe for c in rows}
            assert {"1min", "5min", "15min"}.issubset(tfs) or len(tfs) >= 3
            # No duplicate TF after clone reduction (same-wave dual dropped)
            tf_list = [c.timeframe for c in rows]
            assert len(tf_list) == len(set(tf_list)), f"{core} dup TF: {tf_list}"

        # MIDCAP: simultaneous dual same-TF clones reduced → solo best-2 (or 2+1 if dual survives)
        mid = by_inst.get("MIDCPNIFTY", [])
        assert 2 <= len(mid) <= 3, f"MIDCAP expected 2–3, got {len(mid)}: {mid}"
        assert all(c.inst_type == "OPT" for c in mid)
        # Same-wave dual same TF within 1m → clone reduction drops dual → 2 main
        dual_mid = [c for c in mid if getattr(c, "source_lane", "") == "B"]
        main_mid = [c for c in mid if getattr(c, "source_lane", "") != "B"]
        assert len(main_mid) >= 2
        # Simultaneous dual clones should not double same TF
        tfs = [c.timeframe for c in mid]
        assert len(tfs) == len(set(tfs)), f"duplicate TF after clone reduction: {tfs}"

        # All non-FUT are OPT
        assert all(c.inst_type == "OPT" for c in out if c is not fut[0])

        # OPT cascade: base 1 × mult 3 = 3 lots (single scale; not 6×)
        for c in opt:
            assert c.lots == 3, f"{c.instrument} {c.timeframe} lots={c.lots}"

    def test_bn_wins_fut_when_highest_rank(self):
        sc = _scanner()
        cands = [
            _cand("NIFTY", conf=0.70, one_way_cascade_entry=True, timeframe="15min"),
            _cand("BANKNIFTY", conf=0.99, grade="A+", one_way_cascade_entry=True, timeframe="15min"),
            _cand("SENSEX", conf=0.80, one_way_cascade_entry=True, timeframe="15min"),
            _cand("MIDCPNIFTY", conf=0.75, one_way_cascade_entry=True, timeframe="15min"),
        ]
        out = sc._apply_multi_signal_mixed_book(cands)
        fut = [c for c in out if c.inst_type == "FUT"]
        assert len(fut) == 1
        assert fut[0].instrument == "BANKNIFTY"

    def test_existing_open_fut_forces_all_opt(self):
        sc = _scanner()
        sc.trades.open_trades = {
            "x": SimpleNamespace(inst_type="FUT", is_ghost=False, instrument="NIFTY")
        }
        out = sc._apply_multi_signal_mixed_book(
            [
                _cand("NIFTY", conf=0.9, one_way_cascade_entry=True),
                _cand("BANKNIFTY", conf=0.85, one_way_cascade_entry=True),
                _cand("SENSEX", conf=0.8, one_way_cascade_entry=True),
            ]
        )
        assert all(c.inst_type == "OPT" for c in out)

    def test_non_cascade_collapses_one_per_index(self):
        sc = _scanner()
        cands = []
        for inst in ("NIFTY", "BANKNIFTY", "SENSEX"):
            cands.append(_cand(inst, timeframe="5min", conf=0.7))
            cands.append(_cand(inst, timeframe="15min", conf=0.9, grade="A+"))
        out = sc._apply_multi_signal_mixed_book(cands)
        for inst in ("NIFTY", "BANKNIFTY", "SENSEX"):
            rows = [c for c in out if c.instrument == inst]
            assert len(rows) == 1
            assert rows[0].timeframe == "15min"

    def test_pref_locked_forces_mixed_book_to_pref(self):
        sc = _scanner()
        sc.inst_pref = "OPT"
        cands = [_cand("NIFTY"), _cand("BANKNIFTY"), _cand("SENSEX")]
        assert sc._multi_signal_mixed_book_trigger(cands) is True
        out = sc._apply_multi_signal_mixed_book(cands)
        assert len(out) > 0
        assert all(str(getattr(c, "inst_type", "") or "").upper() == "OPT" for c in out)


# ── MIDCAP STACK STRESS ──────────────────────────────────────────────

class TestMidcapCascadeStack:
    def test_solo_best_two_drops_weakest(self):
        sc = _scanner()
        rows = [
            _cand("MIDCPNIFTY", timeframe="1min", conf=0.50, source_lane="A"),
            _cand("MIDCPNIFTY", timeframe="5min", conf=0.90, source_lane="A"),
            _cand("MIDCPNIFTY", timeframe="15min", conf=0.80, source_lane="A"),
        ]
        picked = sc._pick_cascade_tf_stack_for_midcap(rows)
        assert len(picked) == 2
        assert {c.timeframe for c in picked} == {"5min", "15min"}

    def test_dual_2_plus_1(self):
        from datetime import datetime, timedelta

        sc = _scanner()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        t90 = t0 + timedelta(seconds=90)
        rows = [
            _cand("MIDCPNIFTY", timeframe="1min", conf=0.70, source_lane="A", signal_timestamp=t0),
            _cand("MIDCPNIFTY", timeframe="5min", conf=0.85, source_lane="A", signal_timestamp=t0),
            _cand("MIDCPNIFTY", timeframe="15min", conf=0.95, source_lane="A", signal_timestamp=t0),
            _cand("MIDCPNIFTY", timeframe="5min", conf=0.88, source_lane="B", signal_timestamp=t90),
            _cand("MIDCPNIFTY", timeframe="15min", conf=0.92, source_lane="B", signal_timestamp=t90),
        ]
        picked = sc._pick_cascade_tf_stack_for_midcap(rows)
        assert len(picked) == 3
        main = [c for c in picked if getattr(c, "source_lane", "") != "B"]
        dual = [c for c in picked if getattr(c, "source_lane", "") == "B"]
        assert len(main) == 2
        assert len(dual) == 1
        assert dual[0].timeframe == "15min"

    def test_midcap_simultaneous_dual_same_tf_clone_reduced(self):
        sc = _scanner()
        rows = [
            _cand("MIDCPNIFTY", timeframe="5min", conf=0.85, source_lane="A"),
            _cand("MIDCPNIFTY", timeframe="15min", conf=0.95, source_lane="A"),
            _cand("MIDCPNIFTY", timeframe="15min", conf=0.92, source_lane="B"),
        ]
        picked = sc._pick_cascade_tf_stack_for_midcap(rows)
        assert len(picked) == 2
        assert len([c for c in picked if c.timeframe == "15min"]) == 1

    def test_midcap_cap_is_3_on_cascade(self):
        sc = _scanner()
        cand = _cand("MIDCPNIFTY", one_way_cascade_entry=True)
        cap, basis = sc._active_index_trade_cap(cand, 0)
        assert cap == 3  # max(solo2, dual 2+1)
        assert "midcap" in basis.lower()

    def test_core_cap_is_5_on_cascade(self):
        sc = _scanner()
        cand = _cand("NIFTY", one_way_cascade_entry=True)
        cap, basis = sc._active_index_trade_cap(cand, 0)
        assert cap == 5  # 3+2 headroom (simultaneous dual clones still reduced at select)
        assert "cascade" in basis.lower()

    def test_lot_base1_cascade_is_2_to_3_not_six(self):
        s = Settings(
            auto_risk_sizing_enabled=False,
            cascade_lot_multiplier_opt=3.0,
            cascade_lot_multiplier_opt_min=2.0,
            cascade_lot_multiplier_fut=1.0,
        )
        r3 = resolve_trade_lots(
            settings=s, inst_type="OPT", requested_lots=1, lot_size=25,
            entry_price=100.0, stop_price=85.0, cascade_scale=True,
        )
        assert r3.lots == 3
        assert r3.mode == "cascade"
        s2 = Settings(
            auto_risk_sizing_enabled=False,
            cascade_lot_multiplier_opt=2.0,
            cascade_lot_multiplier_opt_min=2.0,
        )
        r2 = resolve_trade_lots(
            settings=s2, inst_type="OPT", requested_lots=1, lot_size=25,
            entry_price=100.0, stop_price=85.0, cascade_scale=True,
        )
        assert r2.lots == 2


# ── CONCURRENCY STRESS ───────────────────────────────────────────────

class TestConcurrencyCaps:
    def test_normal_cap_two(self):
        sc = _scanner()
        cap, basis = sc._active_index_trade_cap(
            _cand("NIFTY", one_way_cascade_entry=False), 0
        )
        assert cap == 2
        assert basis == "normal"

    def test_cascade_cross_tf_allows_same_book(self):
        sc = _scanner()
        sc.trades.open_trades = {
            "t1": SimpleNamespace(
                instrument="NIFTY", timeframe="15min", inst_type="OPT",
                grade="A", confidence=0.9, direction="LONG", status="OPEN",
                source_lane="A", is_ghost=False,
            )
        }
        sc._active_session_entry_candidates = lambda *a, **k: []
        sc._is_quad_prime_raw_candidate = lambda *a, **k: False
        sc._dual_lane_concurrency_decision = lambda *a, **k: (None, "")
        cand = _cand(
            "NIFTY", timeframe="5min", inst_type="OPT",
            one_way_cascade_entry=True, source_lane="A",
        )
        assert sc._prepare_candidate_for_concurrency(cand) is True

    def test_cascade_same_tf_same_lane_blocks(self):
        sc = _scanner()
        sc.trades.open_trades = {
            "t1": SimpleNamespace(
                instrument="NIFTY", timeframe="5min", inst_type="OPT",
                grade="A", confidence=0.9, direction="LONG", status="OPEN",
                source_lane="A", is_ghost=False,
            )
        }
        sc._active_session_entry_candidates = lambda *a, **k: []
        sc._is_quad_prime_raw_candidate = lambda *a, **k: False
        sc._dual_lane_concurrency_decision = lambda *a, **k: (None, "")
        cand = _cand(
            "NIFTY", timeframe="5min", inst_type="OPT",
            one_way_cascade_entry=True, source_lane="A",
        )
        assert sc._prepare_candidate_for_concurrency(cand) is False

    def test_fill_cascade_cap_then_block(self):
        sc = _scanner()
        # 5 open TFs on NIFTY cascade
        sc.trades.open_trades = {
            f"t{i}": SimpleNamespace(
                instrument="NIFTY", timeframe=tf, inst_type="OPT",
                grade="A", confidence=0.9, direction="LONG", status="OPEN",
                source_lane=lane, is_ghost=False,
            )
            for i, (tf, lane) in enumerate(
                [("1min", "A"), ("5min", "A"), ("15min", "A"), ("5min", "B"), ("15min", "B")]
            )
        }
        sc._active_session_entry_candidates = lambda *a, **k: []
        sc._is_quad_prime_raw_candidate = lambda *a, **k: False
        sc._dual_lane_concurrency_decision = lambda *a, **k: (None, "")
        cand = _cand(
            "NIFTY", timeframe="1min", inst_type="OPT",
            one_way_cascade_entry=True, source_lane="A",
        )
        # cap 5 already filled
        assert sc._prepare_candidate_for_concurrency(cand) is False


# ── HEAVY TREND ALIGNMENT ────────────────────────────────────────────

class TestHeavyTrendAlign:
    def test_two_of_four_eligible(self):
        sc = _scanner()
        sc._cached_results = {
            "BANKNIFTY_15min": {"signals": [SimpleNamespace(signal_type="BUY")]}
        }
        assert sc._heavy_trend_lot_scale_eligible("NIFTY", "LONG", "15min") is True

    def test_solo_not_eligible(self):
        sc = _scanner()
        sc._cached_results = {}
        assert sc._heavy_trend_lot_scale_eligible("NIFTY", "LONG", "15min") is False

    def test_opposite_direction_not_aligned(self):
        sc = _scanner()
        sc._cached_results = {
            "BANKNIFTY_15min": {"signals": [SimpleNamespace(signal_type="SELL")]}
        }
        assert sc._heavy_trend_lot_scale_eligible("NIFTY", "LONG", "15min") is False


# ── SIBLING EMIT / WAVE SELECT ───────────────────────────────────────

class TestCascadeWaveSelect:
    def test_core_wave_primary_three_simultaneous_dual_clones_dropped(self):
        """Same-wave dual same TF → clone reduction; primary 3 kept."""
        from datetime import datetime

        settings = get_settings()
        t0 = datetime(2026, 7, 9, 10, 0, 0)
        sig = SimpleNamespace(
            signal_type="BUY", price=1.0, source_engine="utbot",
            source_engines=["utbot"], source_lane="A", timestamp=t0, bar_index=1,
        )
        sig_b = SimpleNamespace(
            signal_type="BUY", price=1.0, source_engine="apex_hybrid",
            source_engines=["apex_hybrid"], source_lane="B", timestamp=t0, bar_index=2,
        )
        scored = [
            (0.9, sig, "15min", "A+", 0.9, [], False),
            (0.85, sig, "5min", "A", 0.85, [], False),
            (0.8, sig, "1min", "A", 0.8, [], False),
            (0.88, sig_b, "5min", "A", 0.88, [], False),
            (0.87, sig_b, "15min", "A", 0.87, [], False),
        ]
        wave = SignalProcessor._select_cascade_wave_scored_rows(
            scored, settings, "adaptive_dual", instrument="NIFTY"
        )
        assert len(wave) == 3  # dual clones dropped

    def test_midcap_solo_wave_two(self):
        settings = get_settings()
        sig = SimpleNamespace(
            signal_type="BUY", price=1.0, source_engine="utbot",
            source_engines=["utbot"], source_lane="A", timestamp=None, bar_index=1,
        )
        scored = [
            (0.9, sig, "15min", "A+", 0.9, [], False),
            (0.85, sig, "5min", "A", 0.85, [], False),
            (0.8, sig, "1min", "A", 0.8, [], False),
        ]
        wave = SignalProcessor._select_cascade_wave_scored_rows(
            scored, settings, "utbot", instrument="MIDCPNIFTY"
        )
        assert len(wave) == 2

    def test_midcap_dual_wave_three_when_staggered(self):
        from datetime import datetime, timedelta

        settings = get_settings()
        t0 = datetime(2026, 7, 9, 10, 0, 0)
        t90 = t0 + timedelta(seconds=90)
        sig = SimpleNamespace(
            signal_type="BUY", price=1.0, source_engine="utbot",
            source_engines=["utbot"], source_lane="A", timestamp=t0, bar_index=1,
        )
        sig_b = SimpleNamespace(
            signal_type="BUY", price=1.0, source_engine="apex_hybrid",
            source_engines=["apex_hybrid"], source_lane="B", timestamp=t90, bar_index=2,
        )
        scored = [
            (0.9, sig, "15min", "A+", 0.9, [], False),
            (0.85, sig, "5min", "A", 0.85, [], False),
            (0.8, sig, "1min", "A", 0.8, [], False),
            (0.88, sig_b, "5min", "A", 0.88, [], False),
            (0.87, sig_b, "15min", "A", 0.87, [], False),
        ]
        wave = SignalProcessor._select_cascade_wave_scored_rows(
            scored, settings, "adaptive_dual", instrument="MIDCPNIFTY"
        )
        assert len(wave) == 3  # 2 main + 1 dual (staggered >1m)


# ── SETTINGS SURFACE ─────────────────────────────────────────────────

class TestSettingsSurface:
    def test_critical_defaults(self):
        s = get_settings()
        assert s.auto_risk_sizing_enabled is False
        assert s.cascade_lot_multiplier_fut == pytest.approx(1.0)
        assert s.cascade_lot_multiplier_opt == pytest.approx(3.0)
        assert s.cascade_lot_multiplier_opt_min == pytest.approx(2.0)
        assert s.heavy_trend_lot_multiplier_opt == pytest.approx(2.0)
        assert s.cascade_multi_tf_stack_enabled is True
        assert s.cascade_midcap_solo_max_tf == 2
        assert s.cascade_midcap_dual_main_max == 2
        assert s.cascade_midcap_dual_addon_max == 1
        assert "MIDCPNIFTY" in s.auto_futures_blocked_indices
        assert set(s.cascade_multi_tf_core_indices) == {"NIFTY", "BANKNIFTY", "SENSEX"}
        assert s.max_concurrent_trades_per_index == 2
        assert s.dual_mode_max_per_index == 3
        assert s.multi_signal_mixed_book_enabled is True
        assert s.multi_signal_max_fut == 1
        assert s.multi_signal_fut_lots == 1
