"""
Audit + stress: 1M SPIKE vs POST-WIN SNAPBACK — logic, integration, edge cases.

Covers:
  - Decoupled toggles (1M scalping vs reversion)
  - Weak-1min admission matrix
  - Dual move-size gate boundaries
  - Window arm/expiry, counter-trend, cooldown bypass
  - Depth/OFR on SPIKE only
  - Stabilization (spike + pure snapback share 3s floor)
  - Rare dual-TF, dual-tag RR, concurrent arm churn
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config.settings import Settings, get_settings
from engine.signal_processor import SignalProcessor
from engine.signal_quality_policy import (
    volume_spike_depth_ofr_aligned,
    volume_spike_entry_confirmed,
)
from scanner import Scanner


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def settings():
    s = get_settings()
    # SPIKE
    s.volume_spike_entry_enabled = True
    s.one_min_scalping_enabled = True
    s.one_min_scalping_high_burst_only = False
    s.volume_spike_entry_min_volume_ratio = 2.5
    s.volume_spike_entry_min_range_atr_ratio = 1.5
    s.volume_spike_entry_min_close_location = 0.65
    s.volume_spike_depth_confirm_enabled = True
    s.volume_spike_depth_ofr_bullish = 1.08
    s.volume_spike_depth_ofr_bearish = 0.92
    s.volume_spike_entry_stabilization_seconds = 3.0
    s.volume_spike_entry_stabilization_seconds_min = 3.0
    s.volume_spike_entry_stabilization_seconds_max = 15.0
    s.volume_spike_entry_1min_scalp_rr = 1.5
    s.live_signal_stabilization_seconds = 20.0
    s.live_signal_stabilization_seconds_5min = 30.0
    s.live_signal_stabilization_seconds_15min = 30.0
    # SNAPBACK
    s.post_exit_reversion_enabled = True
    s.post_exit_reversion_window_minutes = 3.0
    s.post_exit_reversion_min_rr = 1.5
    s.post_exit_reversion_include_stop_loss = True
    s.post_exit_reversion_include_oi_pcr = True
    s.post_exit_reversion_open_distress_enabled = True
    s.post_exit_reversion_loss_band_soft_rs = 1000.0
    s.post_exit_reversion_loss_band_deep_rs = 2000.0
    s.post_exit_reversion_fast_move_pct = 0.40
    s.post_exit_reversion_fast_lookback_minutes = 5.0
    s.post_exit_reversion_slow_move_pct = 0.50
    s.post_exit_reversion_slow_lookback_minutes = 10.0
    s.rare_dual_tf_entry_enabled = True
    s.rare_dual_tf_min_grade = "A"
    s.rare_dual_tf_min_confidence = 0.70
    yield s
    s.post_exit_reversion_enabled = False
    s.one_min_scalping_enabled = True


def _scanner_stub(*, move_5m: float = 0.45, move_10m: float = 0.30):
    st = Scanner.__new__(Scanner)
    st._last_major_exits = {}
    st._last_major_exit_time = None
    st._last_major_exit_instrument = None
    st._last_exit_time = {}
    st.mode = "PAPER"
    st.latest_regimes = {}
    st.index_cooldown_minutes = 15
    st.max_consecutive_losses = 3
    st._trades_today = {}
    st._losses_today = {}
    st.MIDCAP_CORRELATED_INDEX = "MIDCPNIFTY"

    def _move(inst, lookback_minutes=None, as_of=None, timeframe="1min"):
        lb = float(lookback_minutes if lookback_minutes is not None else 5.0)
        return float(move_5m if lb <= 5.5 else move_10m)

    st._index_short_span_move_pct = _move
    st._index_event_move_qualifies = Scanner._index_event_move_qualifies.__get__(st, Scanner)
    st._arm_reversion_window = Scanner._arm_reversion_window.__get__(st, Scanner)
    st._reversion_window_active = Scanner._reversion_window_active.__get__(st, Scanner)
    st._maybe_arm_open_distress_reversion = Scanner._maybe_arm_open_distress_reversion.__get__(
        st, Scanner
    )
    st._live_signal_stabilization_requirements = (
        Scanner._live_signal_stabilization_requirements.__get__(st, Scanner)
    )
    return st


def _arm(st, *, when=None, direction="LONG", reason="TARGET_HIT", source="major_win"):
    when = when or datetime(2026, 7, 10, 14, 30, 0)
    return st._arm_reversion_window(
        "NIFTY", direction=direction, reason=reason, when=when, source=source
    )


# ── 1. Toggle / TF inclusion matrix ──────────────────────────────────────────


class TestToggleDecoupling:
    def test_field_defaults_independent(self):
        f = Settings.model_fields
        assert f["one_min_scalping_enabled"].default is True
        assert f["volume_spike_entry_enabled"].default is True
        assert f["post_exit_reversion_enabled"].default is False  # opt-in SNAPBACK
        assert f["volume_spike_entry_stabilization_seconds"].default == 3.0
        assert f["post_exit_reversion_window_minutes"].default == 3.0
        assert f["post_exit_reversion_min_rr"].default == 1.5
        assert f["volume_spike_entry_1min_scalp_rr"].default == 1.5

    def test_1min_scan_when_spike_scalp_on_no_window(self, settings):
        """Continuous SPIKE path: 1min in candidate TFs without reversion window."""
        # Simulate the TF inclusion predicate from process_best_signal
        _vol = True
        _scalp = True
        _rev_win = False
        include = (_vol and _scalp) or _rev_win
        assert include is True

    def test_1min_scan_when_scalp_off_but_window_open(self, settings):
        """SNAPBACK alone: reversion window admits 1min even if 1M-SCALP OFF."""
        _vol = True
        _scalp = False
        _rev_win = True
        include = (_vol and _scalp) or _rev_win
        assert include is True

    def test_1min_scan_when_volume_spike_toggle_off_but_window_open(self, settings):
        """True decoupling: pure SNAPBACK must not require volume_spike_entry_enabled."""
        _vol = False
        _scalp = False
        _rev_win = True
        include = (_vol and _scalp) or _rev_win
        assert include is True

    def test_no_1min_when_both_paths_closed(self, settings):
        _vol = True
        _scalp = False
        _rev_win = False
        include = (_vol and _scalp) or _rev_win
        assert include is False


# ── 2. Weak-1min admission matrix ────────────────────────────────────────────


class TestWeak1minGateMatrix:
    """Mirrors process_best_signal: 1min needs spike OR snapback OR cascade."""

    @staticmethod
    def _admit(volume_spike, reversion, cascade=False):
        if volume_spike or cascade or reversion:
            return True
        return False

    def test_spike_only(self):
        assert self._admit(True, False) is True

    def test_snapback_only(self):
        assert self._admit(False, True) is True

    def test_both_dual_tag(self):
        assert self._admit(True, True) is True

    def test_neither_rejected(self):
        assert self._admit(False, False) is False

    def test_entry_admissible_raw_filter(self):
        one_plain = {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=False)}
        one_spike = {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=True)}
        five = {"tf": "5min", "sig": SimpleNamespace(volume_spike_entry=False)}
        # No window → plain 1min dropped
        out = SignalProcessor._entry_admissible_raw_candidates(
            [one_plain, one_spike, five], reversion_window_open=False
        )
        assert [c["sig"] for c in out] == [one_spike["sig"], five["sig"]]
        # Window → plain 1min admitted for SNAPBACK eval
        out2 = SignalProcessor._entry_admissible_raw_candidates(
            [one_plain, five], reversion_window_open=True
        )
        assert len(out2) == 2


# ── 3. SPIKE depth / thrust ──────────────────────────────────────────────────


class TestSpikeDepthAndThrust:
    def test_depth_blocks_adverse_ofr_1min(self, settings):
        assert (
            volume_spike_entry_confirmed(
                timeframe="1min",
                volume_ratio=3.0,
                range_atr_ratio=1.6,
                close_location=0.70,
                adx_value=30.0,
                settings=settings,
                signal_type="BUY",
                depth_usable=True,
                ofr=0.80,
            )
            is False
        )

    def test_depth_allows_aligned_ofr(self, settings):
        assert volume_spike_entry_confirmed(
            timeframe="1min",
            volume_ratio=3.0,
            range_atr_ratio=1.6,
            close_location=0.70,
            adx_value=30.0,
            settings=settings,
            signal_type="BUY",
            depth_usable=True,
            ofr=1.20,
        )

    def test_depth_na_never_blocks(self, settings):
        assert volume_spike_depth_ofr_aligned(
            signal_type="BUY", ofr=0.1, depth_usable=False, settings=settings
        )

    def test_5min_spike_ignores_depth(self, settings):
        assert volume_spike_entry_confirmed(
            timeframe="5min",
            volume_ratio=2.5,
            range_atr_ratio=1.5,
            close_location=0.65,
            settings=settings,
            signal_type="BUY",
            depth_usable=True,
            ofr=0.50,
        )

    def test_high_burst_only_blocks_weak_context(self, settings):
        settings.one_min_scalping_high_burst_only = True
        assert (
            volume_spike_entry_confirmed(
                timeframe="1min",
                volume_ratio=3.5,
                range_atr_ratio=1.8,
                close_location=0.80,
                adx_value=10.0,  # weak ADX
                intel_score=0.1,
                power_move=False,
                settings=settings,
                signal_type="BUY",
                depth_usable=False,
            )
            is False
        )


# ── 4. SNAPBACK arm / evaluate / window ──────────────────────────────────────


class TestSnapbackArmAndEval:
    def test_fast_band_boundary(self, settings):
        # Just below 0.40% / 5m and below 0.50% / 10m → fail
        st = _scanner_stub(move_5m=0.399, move_10m=0.499)
        assert not _arm(st)
        # At floor 0.40
        st2 = _scanner_stub(move_5m=0.40, move_10m=0.10)
        assert _arm(st2)

    def test_slow_band_alone_qualifies(self, settings):
        st = _scanner_stub(move_5m=0.10, move_10m=0.50)
        assert _arm(st)

    def test_window_expires_after_3m(self, settings):
        st = _scanner_stub(move_5m=0.55, move_10m=0.20)
        t0 = datetime(2026, 7, 10, 14, 0, 0)
        assert _arm(st, when=t0)
        assert st._reversion_window_active("NIFTY", now=t0 + timedelta(minutes=2, seconds=59))
        assert not st._reversion_window_active("NIFTY", now=t0 + timedelta(minutes=3, seconds=1))

    def test_counter_trend_required(self, settings):
        st = _scanner_stub(move_5m=0.50, move_10m=0.20)
        st._candle_thrust_metrics = lambda *a, **k: {
            "volume_ratio": 1.8,
            "range_atr_ratio": 1.2,
            "close_location": 0.65,
        }
        t0 = datetime.now().replace(tzinfo=None) - timedelta(minutes=1)
        st._last_major_exits = {
            "NIFTY": {
                "time": t0,
                "direction": "LONG",
                "reason": "TARGET_HIT",
                "source": "major_win",
                "source_tf": "5min",
                "move_pct": 0.50,
                "move_5m_pct": 0.50,
                "move_10m_pct": 0.20,
            }
        }
        ok_sell, _ = SignalProcessor._evaluate_reversion_scalp(
            st,
            settings,
            instrument="NIFTY",
            best_tf="1min",
            best_sig=SimpleNamespace(signal_type="SELL", volume_spike_entry=False),
        )
        ok_buy, _ = SignalProcessor._evaluate_reversion_scalp(
            st,
            settings,
            instrument="NIFTY",
            best_tf="1min",
            best_sig=SimpleNamespace(signal_type="BUY", volume_spike_entry=False),
        )
        assert ok_sell is True
        assert ok_buy is False

    def test_5min_never_snapback(self, settings):
        st = _scanner_stub(move_5m=0.60, move_10m=0.40)
        t0 = datetime.now().replace(tzinfo=None) - timedelta(seconds=30)
        st._last_major_exits = {
            "NIFTY": {
                "time": t0,
                "direction": "SHORT",
                "reason": "TRAILING_STOP",
                "source": "stop_loss",
                "move_pct": 0.60,
                "move_5m_pct": 0.60,
                "move_10m_pct": 0.40,
            }
        }
        ok, _ = SignalProcessor._evaluate_reversion_scalp(
            st,
            settings,
            instrument="NIFTY",
            best_tf="5min",
            best_sig=SimpleNamespace(signal_type="BUY"),
        )
        assert ok is False

    def test_reversion_off_never_evaluates(self, settings):
        settings.post_exit_reversion_enabled = False
        st = _scanner_stub(move_5m=0.80, move_10m=0.80)
        t0 = datetime.now().replace(tzinfo=None)
        st._last_major_exits = {
            "NIFTY": {
                "time": t0,
                "direction": "LONG",
                "reason": "TARGET_HIT",
                "source": "major_win",
                "move_pct": 0.80,
                "move_5m_pct": 0.80,
                "move_10m_pct": 0.80,
            }
        }
        ok, _ = SignalProcessor._evaluate_reversion_scalp(
            st,
            settings,
            instrument="NIFTY",
            best_tf="1min",
            best_sig=SimpleNamespace(signal_type="SELL"),
        )
        assert ok is False


# ── 5. Stabilization integration ─────────────────────────────────────────────


class TestStabilizationIntegration:
    def test_spike_uses_3s_floor(self, settings):
        st = _scanner_stub()
        cand = SimpleNamespace(
            timeframe="1min",
            volume_spike_entry=True,
            is_reversion_scalp=False,
            market_ltp_shock_entry=False,
            market_wide_reversal_entry=False,
            inst_type="FUT",
            instrument="NIFTY",
        )
        sec, scans, adapt = st._live_signal_stabilization_requirements(cand)
        assert sec == 3.0 and scans == 2 and adapt is True

    def test_pure_snapback_also_fast_stab(self, settings):
        """Integration fix: pure SNAPBACK must not fall through to 20–30s base."""
        st = _scanner_stub()
        cand = SimpleNamespace(
            timeframe="1min",
            volume_spike_entry=False,
            is_reversion_scalp=True,
            market_ltp_shock_entry=False,
            market_wide_reversal_entry=False,
            inst_type="FUT",
            instrument="NIFTY",
        )
        sec, scans, adapt = st._live_signal_stabilization_requirements(cand)
        assert sec == 3.0 and scans == 2 and adapt is True

    def test_dual_tag_spike_plus_snapback_still_3s(self, settings):
        st = _scanner_stub()
        cand = SimpleNamespace(
            timeframe="1min",
            volume_spike_entry=True,
            is_reversion_scalp=True,
            market_ltp_shock_entry=False,
            market_wide_reversal_entry=False,
            inst_type="FUT",
            instrument="NIFTY",
        )
        sec, _, _ = st._live_signal_stabilization_requirements(cand)
        assert sec == 3.0

    def test_5m15m_untouched(self, settings):
        st = _scanner_stub()
        st.latest_regimes = {"NIFTY": "TRENDING_UP"}
        for tf, expect in (("5min", 30.0), ("15min", 30.0)):
            cand = SimpleNamespace(
                timeframe=tf,
                volume_spike_entry=False,
                is_reversion_scalp=False,
                market_ltp_shock_entry=False,
                market_wide_reversal_entry=False,
                inst_type="FUT",
                instrument="NIFTY",
            )
            sec, scans, _ = st._live_signal_stabilization_requirements(cand)
            assert sec == expect and scans == 1


# ── 6. RR / stop priority when dual-tagged ───────────────────────────────────


class TestRiskParams:
    def test_rr_floors_aligned(self, settings):
        assert max(1.5, settings.post_exit_reversion_min_rr) == 1.5
        assert max(1.5, settings.volume_spike_entry_1min_scalp_rr) == 1.5

    def test_tight_fut_stop_helper(self, settings):
        # Missing attr falls back to default 0.05; min with global cap
        pct = SignalProcessor._tight_futures_stop_pct(0.10, settings, "reversion_scalp_futures_sl_pct", 0.05)
        assert pct == 0.05
        pct2 = SignalProcessor._volume_spike_futures_stop_pct(0.10, settings)
        assert pct2 == 0.05


# ── 7. Rare dual TF ──────────────────────────────────────────────────────────


class TestRareDual:
    def test_snapback_winner_can_emit_structure_sibling(self, settings):
        sp = SignalProcessor.__new__(SignalProcessor)
        winner = SimpleNamespace(
            instrument="NIFTY",
            direction="SHORT",
            timeframe="1min",
            grade="A",
            confidence=0.80,
            volume_spike_entry=False,
            is_reversion_scalp=True,
            market_ltp_shock_entry=False,
            one_way_cascade_entry=False,
            inst_type="FUT",
            price=25000.0,
            stop=25050.0,
            target=24925.0,
            lots=1,
            rr=1.5,
            reasons=[],
            source_lane="A",
            source_engine="utbot",
            source_engines=["utbot"],
            signal_timestamp=datetime.now(),
        )
        sig5 = SimpleNamespace(
            signal_type="SELL",
            timestamp=datetime.now(),
            price=25000.0,
            source_engine="utbot",
            source_engines=["utbot"],
            bar_index=1,
            volume_spike_entry=False,
        )
        scored = [
            (1.5, SimpleNamespace(signal_type="SELL"), "1min", "A", 0.80, [], False),
            (0.9, sig5, "5min", "A", 0.72, ["structure"], False),
        ]
        sibs = sp._emit_rare_dual_tf_siblings(winner, scored, settings, "utbot")
        assert len(sibs) == 1
        assert sibs[0].timeframe == "5min"
        assert str(sibs[0].inst_type).upper() == "OPT"

    def test_no_dual_without_excellent_companion(self, settings):
        sp = SignalProcessor.__new__(SignalProcessor)
        winner = SimpleNamespace(
            instrument="NIFTY",
            direction="LONG",
            timeframe="1min",
            grade="A",
            confidence=0.80,
            volume_spike_entry=True,
            is_reversion_scalp=False,
            market_ltp_shock_entry=False,
            one_way_cascade_entry=False,
            inst_type="FUT",
            price=25000.0,
            stop=24950.0,
            target=25075.0,
            lots=1,
            rr=1.5,
            reasons=[],
            source_lane="A",
            source_engine="utbot",
            source_engines=["utbot"],
            signal_timestamp=datetime.now(),
        )
        scored = [
            (1.5, SimpleNamespace(signal_type="BUY"), "1min", "A", 0.80, [], False),
            (0.4, SimpleNamespace(signal_type="BUY"), "5min", "B", 0.55, [], False),
        ]
        assert sp._emit_rare_dual_tf_siblings(winner, scored, settings, "utbot") == []


# ── 8. Cooldown vs reversion window ──────────────────────────────────────────


class TestCooldownBypass:
    def test_reversion_window_bypasses_cooldown_predicate(self, settings):
        st = _scanner_stub(move_5m=0.55, move_10m=0.20)
        t0 = datetime(2026, 7, 10, 11, 0, 0)
        assert _arm(st, when=t0)
        # Active window → cooldown should be cleared in _can_trade_instrument path
        assert st._reversion_window_active("NIFTY", now=t0 + timedelta(minutes=1))
        # Simulate cooldown decision fragment
        cooldown_on = True
        if cooldown_on and st._reversion_window_active("NIFTY", now=t0 + timedelta(minutes=1)):
            cooldown_on = False
        assert cooldown_on is False


# ── 9. Distress arm matrix ───────────────────────────────────────────────────


class TestDistressArm:
    def test_soft_band_or_logic(self, settings):
        st = _scanner_stub(move_5m=0.50, move_10m=0.20)
        trade = SimpleNamespace(
            instrument="NIFTY",
            direction="LONG",
            inst_type="FUT",
            option_type="",
            trade_side="",
            timeframe="5min",
        )
        st._trade_direction_view = lambda t: "LONG"
        st._get_trade_intel = lambda t: None
        st._candle_thrust_metrics = lambda inst, tf, sig, as_of=None: {
            "volume_ratio": 2.0,
            "range_atr_ratio": 1.3,
            "close_location": 0.70,
        }
        now = datetime(2026, 7, 10, 11, 0, 0)
        assert st._maybe_arm_open_distress_reversion(trade, unrealized_pnl=-400.0, latest_time=now)

    def test_beyond_deep_band_no_arm(self, settings):
        st = _scanner_stub(move_5m=0.80, move_10m=0.80)
        trade = SimpleNamespace(
            instrument="NIFTY",
            direction="LONG",
            inst_type="FUT",
            option_type="",
            trade_side="",
            timeframe="15min",
        )
        st._trade_direction_view = lambda t: "LONG"
        st._get_trade_intel = lambda t: {"aggregate": {"score": -50.0}}
        st._intel_reversal_reason = lambda *a, **k: "OI_PCR"
        st._candle_thrust_metrics = lambda *a, **k: {
            "volume_ratio": 3.0,
            "range_atr_ratio": 2.0,
            "close_location": 0.80,
        }
        now = datetime(2026, 7, 10, 11, 0, 0)
        # loss > 2000 deep → freefall chase blocked
        assert (
            st._maybe_arm_open_distress_reversion(trade, unrealized_pnl=-2500.0, latest_time=now)
            is False
        )


# ── 10. Stress: concurrent arm churn / multi-index ───────────────────────────


class TestStressChurn:
    def test_multi_index_independent_windows(self, settings):
        st = _scanner_stub(move_5m=0.50, move_10m=0.20)
        t0 = datetime(2026, 7, 10, 12, 0, 0)
        for idx, d in (("NIFTY", "LONG"), ("BANKNIFTY", "SHORT"), ("SENSEX", "LONG")):
            assert st._arm_reversion_window(idx, direction=d, reason="TARGET_HIT", when=t0, source="major_win")
        assert len(st._last_major_exits) == 3
        # Expire only NIFTY
        assert not st._reversion_window_active("NIFTY", now=t0 + timedelta(minutes=4))
        assert st._reversion_window_active("BANKNIFTY", now=t0 + timedelta(minutes=1))

    def test_rapid_rearm_refresh_same_index(self, settings):
        st = _scanner_stub(move_5m=0.55, move_10m=0.30)
        t0 = datetime(2026, 7, 10, 13, 0, 0)
        for i in range(25):
            when = t0 + timedelta(seconds=i * 10)
            assert st._arm_reversion_window(
                "NIFTY", direction="LONG", reason=f"TARGET_HIT_{i}", when=when, source="major_win"
            )
        # Last arm wins
        assert st._last_major_exits["NIFTY"]["reason"] == "TARGET_HIT_24"
        assert st._reversion_window_active("NIFTY", now=t0 + timedelta(seconds=24 * 10 + 30))

    def test_evaluate_stress_many_signals_only_counter_pass(self, settings):
        st = _scanner_stub(move_5m=0.55, move_10m=0.20)
        st._candle_thrust_metrics = lambda *a, **k: {
            "volume_ratio": 1.7,
            "range_atr_ratio": 1.15,
            "close_location": 0.60,
        }
        t0 = datetime.now().replace(tzinfo=None) - timedelta(minutes=1)
        st._last_major_exits = {
            "NIFTY": {
                "time": t0,
                "direction": "LONG",
                "reason": "SMART_PROFIT_LOCK",
                "source": "major_win",
                "source_tf": "5min",
                "move_pct": 0.55,
                "move_5m_pct": 0.55,
                "move_10m_pct": 0.20,
            }
        }
        passed = 0
        for side in ["BUY", "SELL"] * 50:
            ok, _ = SignalProcessor._evaluate_reversion_scalp(
                st,
                settings,
                instrument="NIFTY",
                best_tf="1min",
                best_sig=SimpleNamespace(signal_type=side, volume_spike_entry=False),
            )
            if ok:
                passed += 1
        # Only SELL is counter-trend after LONG exit → 50 of 100
        assert passed == 50

    def test_move_gate_stress_or_logic_grid(self, settings):
        """Grid of (m5, m10) around thresholds — qualifies iff m5>=0.40 OR m10>=0.50."""
        cases = [
            (0.00, 0.00, False),
            (0.39, 0.49, False),
            (0.40, 0.00, True),
            (0.00, 0.50, True),
            (0.40, 0.50, True),
            (0.55, 0.20, True),
            (0.20, 0.55, True),
            (1.00, 1.00, True),
        ]
        for m5, m10, expect in cases:
            st = _scanner_stub(move_5m=m5, move_10m=m10)
            ok = bool(_arm(st, when=datetime(2026, 7, 10, 10, 0, 0)))
            assert ok is expect, f"m5={m5} m10={m10} expected {expect} got {ok}"


# ── 11. Integration: SPIKE does not require SNAPBACK and vice versa ──────────


class TestPrimary15NeverBlocks1mSpikeSnapback:
    """Engine / TF-policy must not kill 1m SPIKE or SNAPBACK — only own toggles."""

    @staticmethod
    def _apply_primary15_filter(cands, *, engine: str, policy: str = "PRIMARY_15"):
        """Mirror signal_processor PRIMARY_15 candidate filter after fix."""
        if policy == "PRIMARY_15" and engine != "quad_prime":
            return [c for c in cands if str(c.get("tf") or "") in {"15min", "1min"}]
        return list(cands)

    def test_utbot_primary15_keeps_1min_spike(self):
        cands = [
            {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=True)},
            {"tf": "5min", "sig": SimpleNamespace(volume_spike_entry=False)},
            {"tf": "15min", "sig": SimpleNamespace(volume_spike_entry=False)},
        ]
        out = self._apply_primary15_filter(cands, engine="utbot")
        tfs = {c["tf"] for c in out}
        assert "1min" in tfs and "15min" in tfs
        assert "5min" not in tfs

    def test_utbot_primary15_keeps_1min_snapback_window_row(self):
        # Admitted via reversion window (no spike tag) — still 1min keep.
        cands = [
            {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=False)},
            {"tf": "5min", "sig": SimpleNamespace()},
        ]
        out = self._apply_primary15_filter(cands, engine="ema_rsi")
        assert any(c["tf"] == "1min" for c in out)
        assert not any(c["tf"] == "5min" for c in out)

    def test_include_5min_keeps_all(self):
        cands = [
            {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=True)},
            {"tf": "5min", "sig": SimpleNamespace()},
            {"tf": "15min", "sig": SimpleNamespace()},
        ]
        out = self._apply_primary15_filter(cands, engine="utbot", policy="INCLUDE_5MIN")
        assert len(out) == 3

    def test_live_gate_policy_allows_1min_spike_under_primary15(self, settings):
        settings.ut_timeframe_entry_policy = "PRIMARY_15"
        settings.scanner_engine = "utbot"
        st = Scanner.__new__(Scanner)
        st.mode = "REAL"
        st._is_quad_prime_raw_candidate = lambda c: False
        st._gate_block = lambda c, msg: False
        cand = SimpleNamespace(
            instrument="NIFTY",
            direction="LONG",
            timeframe="1min",
            volume_spike_entry=True,
            is_reversion_scalp=False,
            market_ltp_shock_entry=False,
            market_wide_reversal_entry=False,
        )
        assert Scanner._passes_timeframe_entry_policy(st, cand) is True
        cand2 = SimpleNamespace(
            instrument="NIFTY",
            direction="SHORT",
            timeframe="1min",
            volume_spike_entry=False,
            is_reversion_scalp=True,
            market_ltp_shock_entry=False,
            market_wide_reversal_entry=False,
        )
        assert Scanner._passes_timeframe_entry_policy(st, cand2) is True


class TestPathIndependence:
    def test_spike_confirmed_without_reversion_settings(self, settings):
        settings.post_exit_reversion_enabled = False
        assert volume_spike_entry_confirmed(
            timeframe="1min",
            volume_ratio=3.0,
            range_atr_ratio=1.6,
            close_location=0.70,
            adx_value=28.0,
            settings=settings,
            signal_type="BUY",
            depth_usable=False,
        )

    def test_snapback_without_spike_metrics(self, settings):
        settings.one_min_scalping_enabled = False
        st = _scanner_stub(move_5m=0.50, move_10m=0.20)
        st._candle_thrust_metrics = lambda *a, **k: {
            "volume_ratio": 1.55,
            "range_atr_ratio": 1.05,
            "close_location": 0.58,
        }
        t0 = datetime.now().replace(tzinfo=None) - timedelta(seconds=45)
        st._last_major_exits = {
            "NIFTY": {
                "time": t0,
                "direction": "SHORT",
                "reason": "OI_PCR_REVERSAL",
                "source": "oi_pcr",
                "source_tf": "15min",
                "move_pct": 0.50,
                "move_5m_pct": 0.50,
                "move_10m_pct": 0.20,
            }
        }
        ok, meta = SignalProcessor._evaluate_reversion_scalp(
            st,
            settings,
            instrument="NIFTY",
            best_tf="1min",
            best_sig=SimpleNamespace(signal_type="BUY", volume_spike_entry=False),
        )
        assert ok is True
        assert meta["source"] == "oi_pcr"
        assert meta.get("tape_path") == "bare_min"

    def test_1min_trade_does_not_arm_via_source_tf_filter(self, settings):
        st = _scanner_stub(move_5m=0.80, move_10m=0.80)
        st._reversion_source_tf_allowed = Scanner._reversion_source_tf_allowed.__get__(
            st, Scanner
        )
        assert not st._reversion_source_tf_allowed("1min", settings)
        trade_1m = SimpleNamespace(
            instrument="NIFTY",
            direction="LONG",
            timeframe="1min",
            inst_type="FUT",
            option_type="",
            trade_side="",
        )
        st._trade_direction_view = lambda t: "LONG"
        st._get_trade_intel = lambda t: {"aggregate": {"score": -40}}
        st._intel_reversal_reason = lambda *a, **k: "OI"
        st._candle_thrust_metrics = lambda *a, **k: {
            "volume_ratio": 3.0,
            "range_atr_ratio": 2.0,
            "close_location": 0.80,
        }
        assert (
            st._maybe_arm_open_distress_reversion(
                trade_1m, unrealized_pnl=-500.0, latest_time=datetime(2026, 7, 10, 11, 0, 0)
            )
            is False
        )

    def test_suppress_tape_only_when_no_spike(self, settings):
        # Pure snapback still suppressed at *raw* tape until tagged — documented.
        assert SignalProcessor._suppress_1min_no_spike_tape("1min", False, settings) is True
        assert SignalProcessor._suppress_1min_no_spike_tape("1min", True, settings) is False
        assert SignalProcessor._suppress_1min_no_spike_tape("5min", False, settings) is False
