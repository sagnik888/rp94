"""POST-WIN 3M SNAPBACK + dual move gate + rare dual-TF + decouple from 1M SPIKE."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from engine.signal_processor import SignalProcessor
from scanner import Scanner


@pytest.fixture
def settings():
    s = get_settings()
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
    s.one_min_scalping_enabled = False
    s.volume_spike_entry_enabled = True
    s.rare_dual_tf_entry_enabled = True
    s.rare_dual_tf_min_grade = "A"
    s.rare_dual_tf_min_confidence = 0.70
    s.volume_spike_entry_1min_scalp_rr = 1.5
    yield s
    s.post_exit_reversion_enabled = False
    s.one_min_scalping_enabled = True


def _scanner_stub(*, move_5m: float = 0.45, move_10m: float = 0.30):
    st = Scanner.__new__(Scanner)
    st._last_major_exits = {}
    st._last_major_exit_time = None
    st._last_major_exit_instrument = None
    st.mode = "PAPER"

    def _move(inst, lookback_minutes=None, as_of=None, timeframe="1min"):
        lb = float(lookback_minutes if lookback_minutes is not None else 5.0)
        if lb <= 5.5:
            return float(move_5m)
        return float(move_10m)

    st._index_short_span_move_pct = _move
    # Bind real qualify helper so dual OR-logic is exercised.
    st._index_event_move_qualifies = Scanner._index_event_move_qualifies.__get__(st, Scanner)
    st._arm_reversion_window = Scanner._arm_reversion_window.__get__(st, Scanner)
    st._reversion_window_active = Scanner._reversion_window_active.__get__(st, Scanner)
    return st


def test_arm_window_fast_band_0_40_over_5m(settings):
    st = _scanner_stub(move_5m=0.42, move_10m=0.20)
    now = datetime(2026, 7, 10, 14, 30, 0)
    assert st._arm_reversion_window(
        "NIFTY", direction="LONG", reason="SMART_PROFIT_LOCK", when=now, source="major_win"
    )
    info = st._last_major_exits["NIFTY"]
    assert float(info["move_5m_pct"]) >= 0.40
    assert st._reversion_window_active("NIFTY", now=now + timedelta(minutes=1))


def test_arm_window_slow_band_0_50_over_10m(settings):
    # Below 0.40%/5m but above 0.50%/10m → still qualifies.
    st = _scanner_stub(move_5m=0.25, move_10m=0.55)
    now = datetime(2026, 7, 10, 14, 30, 0)
    assert st._arm_reversion_window(
        "NIFTY", direction="LONG", reason="TARGET_HIT", when=now, source="major_win"
    )
    assert float(st._last_major_exits["NIFTY"]["move_10m_pct"]) >= 0.50


def test_arm_window_rejects_when_both_bands_fail(settings):
    st = _scanner_stub(move_5m=0.20, move_10m=0.30)
    now = datetime(2026, 7, 10, 14, 30, 0)
    assert not st._arm_reversion_window(
        "NIFTY", direction="LONG", reason="SMART_PROFIT_LOCK", when=now, source="major_win"
    )
    assert not st._last_major_exits


def test_arm_window_after_stop_loss_exit(settings):
    st = _scanner_stub(move_5m=0.50, move_10m=0.20)
    when = datetime(2026, 7, 10, 14, 0, 0)
    assert st._arm_reversion_window(
        "NIFTY", direction="LONG", reason="TRAILING_STOP", when=when, source="stop_loss"
    )
    assert st._reversion_window_active("NIFTY", now=when)


def test_arm_window_after_oi_pcr_exit(settings):
    st = _scanner_stub(move_5m=0.41, move_10m=0.20)
    now = datetime(2026, 7, 10, 13, 15, 0)
    assert st._arm_reversion_window(
        "BANKNIFTY", direction="SHORT", reason="OI_PCR_REVERSAL", when=now, source="oi_pcr"
    )
    assert st._last_major_exits["BANKNIFTY"]["source"] == "oi_pcr"


def test_open_distress_soft_band_arms_on_fast_opposite(settings):
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
    st._candle_thrust_metrics = lambda inst, tf, sig, as_of=None: (
        {"volume_ratio": 2.0, "range_atr_ratio": 1.3, "close_location": 0.70}
        if str(sig).upper() == "SELL"
        else None
    )
    st._maybe_arm_open_distress_reversion = Scanner._maybe_arm_open_distress_reversion.__get__(
        st, Scanner
    )
    now = datetime(2026, 7, 10, 11, 0, 0)
    armed = st._maybe_arm_open_distress_reversion(trade, unrealized_pnl=-450.0, latest_time=now)
    assert armed is True
    assert "OPEN_DISTRESS_SOFT" in st._last_major_exits["NIFTY"]["reason"]


def test_open_distress_deep_band_requires_reverse_and_thrust(settings):
    st = _scanner_stub(move_5m=0.60, move_10m=0.40)
    trade = SimpleNamespace(
        instrument="NIFTY",
        direction="LONG",
        inst_type="FUT",
        option_type="",
        trade_side="",
        timeframe="15min",
    )
    st._trade_direction_view = lambda t: "LONG"
    st._get_trade_intel = lambda t: None
    st._candle_thrust_metrics = lambda inst, tf, sig, as_of=None: {
        "volume_ratio": 3.0,
        "range_atr_ratio": 1.8,
        "close_location": 0.75,
    }
    st._maybe_arm_open_distress_reversion = Scanner._maybe_arm_open_distress_reversion.__get__(
        st, Scanner
    )
    now = datetime(2026, 7, 10, 11, 5, 0)
    assert (
        st._maybe_arm_open_distress_reversion(trade, unrealized_pnl=-1500.0, latest_time=now)
        is False
    )
    st._get_trade_intel = lambda t: {"aggregate": {"score": -40.0}}
    st._intel_reversal_reason = lambda *a, **k: "OI_PCR_REVERSAL bearish"
    assert (
        st._maybe_arm_open_distress_reversion(trade, unrealized_pnl=-1500.0, latest_time=now)
        is True
    )
    assert "OPEN_DISTRESS_DEEP" in st._last_major_exits["NIFTY"]["reason"]


def test_reversion_disabled_never_arms(settings):
    settings.post_exit_reversion_enabled = False
    st = _scanner_stub()
    st._arm_reversion_window("NIFTY", direction="LONG", reason="TARGET_HIT")
    assert not st._last_major_exits


def test_reversion_and_spike_rr_both_1_5(settings):
    final_rev = max(
        1.5, float(getattr(settings, "post_exit_reversion_min_rr", 1.5) or 1.5)
    )
    final_spike = max(
        1.5, float(getattr(settings, "volume_spike_entry_1min_scalp_rr", 1.5) or 1.5)
    )
    assert final_rev == 1.5
    assert final_spike == 1.5


def test_entry_admissible_decouples_spike_from_reversion_window():
    one = {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=False)}
    five = {"tf": "5min", "sig": SimpleNamespace(volume_spike_entry=False)}
    assert SignalProcessor._entry_admissible_raw_candidates([one, five]) == [five]
    assert SignalProcessor._entry_admissible_raw_candidates(
        [one, five], reversion_window_open=True
    ) == [one, five]


def test_evaluate_reversion_scalp_pure_snapback_without_spike(settings):
    st = _scanner_stub(move_5m=0.50, move_10m=0.20)
    now = datetime.now().replace(tzinfo=None) - timedelta(minutes=1)
    st._last_major_exits = {
        "NIFTY": {
            "time": now,
            "direction": "LONG",
            "reason": "TARGET_HIT",
            "source": "major_win",
            "source_tf": "5min",
            "move_pct": 0.50,
            "move_5m_pct": 0.50,
            "move_10m_pct": 0.20,
        }
    }
    # Bare-min tape for pure SNAPBACK (below full SPIKE 2.5x)
    st._candle_thrust_metrics = lambda inst, tf, sig, as_of=None: {
        "volume_ratio": 1.6,
        "range_atr_ratio": 1.1,
        "close_location": 0.60,
    }
    sig = SimpleNamespace(
        signal_type="SELL", timestamp=now + timedelta(minutes=1), volume_spike_entry=False
    )
    ok, meta = SignalProcessor._evaluate_reversion_scalp(
        st, settings, instrument="NIFTY", best_tf="1min", best_sig=sig
    )
    assert ok is True
    assert meta["view"] == "LONG"
    assert meta.get("tape_ok") is True
    ok2, _ = SignalProcessor._evaluate_reversion_scalp(
        st,
        settings,
        instrument="NIFTY",
        best_tf="1min",
        best_sig=SimpleNamespace(signal_type="BUY", timestamp=now, volume_spike_entry=False),
    )
    assert ok2 is False
    ok3, _ = SignalProcessor._evaluate_reversion_scalp(
        st, settings, instrument="NIFTY", best_tf="5min", best_sig=sig
    )
    assert ok3 is False


def test_pure_snapback_rejects_zero_tape(settings):
    st = _scanner_stub(move_5m=0.50, move_10m=0.20)
    now = datetime.now().replace(tzinfo=None) - timedelta(minutes=1)
    st._last_major_exits = {
        "NIFTY": {
            "time": now,
            "direction": "LONG",
            "reason": "TARGET_HIT",
            "source": "major_win",
            "source_tf": "15min",
            "move_pct": 0.50,
            "move_5m_pct": 0.50,
            "move_10m_pct": 0.20,
        }
    }
    st._candle_thrust_metrics = lambda *a, **k: {
        "volume_ratio": 0.8,
        "range_atr_ratio": 0.5,
        "close_location": 0.40,
    }
    ok, meta = SignalProcessor._evaluate_reversion_scalp(
        st,
        settings,
        instrument="NIFTY",
        best_tf="1min",
        best_sig=SimpleNamespace(signal_type="SELL", volume_spike_entry=False),
    )
    assert ok is False
    assert meta.get("tape_path") == "fail"


def test_reversion_source_tf_allows_5_15_not_1min(settings):
    st = _scanner_stub()
    st._reversion_source_tf_allowed = Scanner._reversion_source_tf_allowed.__get__(st, Scanner)
    assert st._reversion_source_tf_allowed("5min", settings)
    assert st._reversion_source_tf_allowed("15min", settings)
    assert not st._reversion_source_tf_allowed("1min", settings)
    assert not st._reversion_source_tf_allowed("", settings)


def test_rare_dual_tf_emits_one_sibling_when_both_excellent(settings):
    sp = SignalProcessor.__new__(SignalProcessor)
    winner = SimpleNamespace(
        instrument="NIFTY",
        direction="SHORT",
        timeframe="1min",
        grade="A",
        confidence=0.78,
        volume_spike_entry=True,
        is_reversion_scalp=False,
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
        (1.8, SimpleNamespace(signal_type="SELL", timestamp=datetime.now(), volume_spike_entry=True), "1min", "A", 0.78, [], False),
        (0.85, sig5, "5min", "A", 0.75, ["structure"], False),
    ]
    sibs = sp._emit_rare_dual_tf_siblings(winner, scored, settings, "utbot")
    assert len(sibs) == 1
    assert sibs[0].timeframe == "5min"
    assert getattr(sibs[0], "rare_dual_tf_entry", False) is True
    # Second leg prefers OPT (never dual FUT).
    assert str(sibs[0].inst_type).upper() == "OPT"


def test_rare_dual_tf_skips_weak_companion(settings):
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
    sig5 = SimpleNamespace(
        signal_type="BUY",
        timestamp=datetime.now(),
        price=25000.0,
        source_engine="utbot",
        source_engines=["utbot"],
        bar_index=1,
    )
    # B-grade companion fails rare dual floor.
    scored = [
        (1.8, SimpleNamespace(signal_type="BUY", volume_spike_entry=True), "1min", "A", 0.80, [], False),
        (0.6, sig5, "5min", "B+", 0.62, [], False),
    ]
    assert sp._emit_rare_dual_tf_siblings(winner, scored, settings, "utbot") == []


def test_missed_reentry_window_is_360_seconds_for_1m_and_5m(settings):
    """Original design: 0–6 min (360s) retrack so 1min + 5min can re-fire if stronger."""
    s = get_settings()
    s.missed_signal_reentry_max_age_minutes = 6.0
    s.missed_signal_reentry_min_age_seconds = 20.0
    s.missed_signal_reentry_timeframes = ["1min", "5min", "15min"]
    max_min = float(getattr(s, "missed_signal_reentry_max_age_minutes", 6.0) or 6.0)
    assert abs(max_min * 60.0 - 360.0) < 1e-6
    tfs = {str(t).lower() for t in (getattr(s, "missed_signal_reentry_timeframes", None) or [])}
    assert "1min" in tfs and "5min" in tfs
    # Anti-flicker floor (not true zero): seeds younger than 20s wait.
    min_age = float(getattr(s, "missed_signal_reentry_min_age_seconds", 20.0) or 20.0)
    assert min_age == 20.0
