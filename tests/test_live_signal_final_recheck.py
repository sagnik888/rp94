from datetime import datetime
from types import SimpleNamespace

from engine.signal_processor import SignalProcessor


def test_preclose_gate_rejection_is_rechecked_before_candle_completion():
    assert SignalProcessor._recheckable_rejection_status(
        "REJECTED - 5Min Pre-Close Gate"
    ) is True
    assert SignalProcessor._recheckable_rejection_status(
        "REJECTED - 5Min Early Morning Gate"
    ) is True
    assert SignalProcessor._recheckable_rejection_status(
        "REJECTED - Weak 1min"
    ) is True
    assert SignalProcessor._recheckable_rejection_status(
        "REJECTED - Late Cutoff"
    ) is False


def test_opposite_signal_wait_is_recheckable_so_reversal_is_not_orphaned():
    # An opposite signal exits the existing trade THIS cycle but is blocked from
    # entering in the same cycle. It MUST stay recheck-eligible so the next cycle
    # re-evaluates it (existing trade now gone) and the reversal entry can fire.
    # Without this the dedup lock advances past the signal and the reversal is
    # orphaned forever (observed live: SENSEX 5min SELL never re-entered after exit).
    assert SignalProcessor._recheckable_rejection_status(
        "REJECTED - Opposite Signal Wait"
    ) is True


def test_volume_spike_stop_respects_later_tighter_global_setting():
    settings = SimpleNamespace(volume_spike_entry_futures_sl_pct=0.05)

    assert SignalProcessor._volume_spike_futures_stop_pct(0.15, settings) == 0.05
    assert SignalProcessor._volume_spike_futures_stop_pct(0.02, settings) == 0.02


def test_rejected_forming_signal_rechecks_at_controlled_cadence():
    signal_ts = datetime(2026, 6, 22, 10, 45)

    assert SignalProcessor._live_signal_recheck_due(
        signal_ts,
        "15min",
        datetime(2026, 6, 22, 10, 54, 48),
        True,
        False,
        datetime(2026, 6, 22, 10, 54, 46),
        5.0,
    ) is False
    assert SignalProcessor._live_signal_recheck_due(
        signal_ts,
        "15min",
        datetime(2026, 6, 22, 10, 54, 51),
        True,
        False,
        datetime(2026, 6, 22, 10, 54, 46),
        5.0,
    ) is True


def test_rejected_signal_gets_one_final_candle_close_recheck():
    signal_ts = datetime(2026, 6, 22, 10, 45)

    assert SignalProcessor._live_signal_recheck_due(
        signal_ts,
        "15min",
        datetime(2026, 6, 22, 11, 0),
        True,
        False,
        datetime(2026, 6, 22, 10, 59, 55),
        5.0,
    ) is True


def test_pending_signal_is_not_double_processed():
    assert SignalProcessor._live_signal_recheck_due(
        datetime(2026, 6, 22, 10, 45),
        "15min",
        datetime(2026, 6, 22, 10, 55),
        True,
        True,
        datetime(2026, 6, 22, 10, 54, 50),
        5.0,
    ) is False


def test_wide_futures_stop_has_explicit_rejection_reason():
    reason = SignalProcessor._futures_stop_distance_rejection(
        natural_stop_distance=28.79,
        spot=14610.75,
        futures_sl_pct=0.1,
    )

    assert reason is not None
    assert "28.79 pts" in reason
    assert "21.92 pts safety limit" in reason


def test_futures_stop_inside_safety_envelope_is_allowed():
    assert SignalProcessor._futures_stop_distance_rejection(
        natural_stop_distance=21.90,
        spot=14610.75,
        futures_sl_pct=0.1,
    ) is None


def test_futures_stop_rejection_uses_regime_selected_tolerance():
    ranging_rejection = SignalProcessor._futures_stop_distance_rejection(
        natural_stop_distance=45.0,
        spot=20000.0,
        futures_sl_pct=0.15,
        safety_multiplier=2.0,
        regime="RANGING",
    )
    trending_rejection = SignalProcessor._futures_stop_distance_rejection(
        natural_stop_distance=45.0,
        spot=20000.0,
        futures_sl_pct=0.15,
        safety_multiplier=2.0,
        regime="TRENDING_UP",
    )

    assert ranging_rejection is not None
    assert "RANGING tolerance 0.100%" in ranging_rejection
    assert trending_rejection is None


def test_futures_regime_cap_never_widens_a_tighter_natural_stop():
    assert SignalProcessor._futures_stop_distance(
        natural_stop_distance=12.0,
        reference_price=20000.0,
        futures_sl_pct=0.15,
        regime="TRENDING_UP",
    ) == 12.0
    assert SignalProcessor._futures_stop_distance(
        natural_stop_distance=45.0,
        reference_price=20000.0,
        futures_sl_pct=0.15,
        regime="RANGING",
    ) == 20.0
