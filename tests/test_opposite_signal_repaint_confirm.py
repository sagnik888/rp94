from datetime import datetime
from types import SimpleNamespace

from scanner import Scanner


def _sig(signal_type="SELL", ts=None):
    return SimpleNamespace(
        signal_type=signal_type,
        timestamp=ts or datetime(2026, 6, 22, 10, 0, 0),
        price=100.0,
        trailing_stop=95.0,
    )


def _scanner(mode="REAL", open_trades=None):
    s = Scanner.__new__(Scanner)
    s.mode = mode
    s.trades = SimpleNamespace(open_trades=open_trades or {})
    s._pending_opposite_exit = {}
    s._active_session_entry_candidates = lambda instrument, timeframe=None, as_of=None: []
    s.log_event = lambda *a, **k: None
    return s


def _age_pending(s, seconds):
    for k in list(s._pending_opposite_exit):
        s._pending_opposite_exit[k] -= seconds


def test_live_opposite_exit_waits_for_repaint_confirm_then_confirms():
    trade = SimpleNamespace(instrument="NIFTY", timeframe="15min", direction="LONG")  # CE long
    s = _scanner(open_trades={"t1": trade})
    sig = _sig("SELL")  # opposite to a LONG CE

    # First sighting and any sighting inside the window must NOT exit yet.
    assert s._opposite_exit_confirm_ready("NIFTY", sig, "15min") is False
    assert s._opposite_exit_confirm_ready("NIFTY", sig, "15min") is False
    # 25s is still inside the 30–60s operator confirm band — must keep holding.
    _age_pending(s, 25.0)
    assert s._opposite_exit_confirm_ready("NIFTY", sig, "15min") is False

    # Once the flip survives the clamped buffer (>=30s) it confirms and may exit.
    _age_pending(s, 10.0)
    assert s._opposite_exit_confirm_ready("NIFTY", sig, "15min") is True


def test_confirm_window_is_identical_for_5min_and_15min():
    for tf in ("5min", "15min"):
        trade = SimpleNamespace(instrument="NIFTY", timeframe=tf, direction="LONG")
        s = _scanner(open_trades={"t1": trade})
        sig = _sig("SELL")
        # Same behavior regardless of timeframe: hold first, confirm after the
        # clamped 30–60s window (repaint_recheck_seconds floors at 30s).
        assert s._opposite_exit_confirm_ready("NIFTY", sig, tf) is False
        _age_pending(s, 35.0)
        assert s._opposite_exit_confirm_ready("NIFTY", sig, tf) is True


def test_historical_opposite_exit_is_immediate_for_parity():
    trade = SimpleNamespace(instrument="NIFTY", timeframe="15min", direction="LONG")
    s = _scanner(mode="HISTORICAL", open_trades={"t1": trade})
    assert s._opposite_exit_confirm_ready("NIFTY", _sig("SELL"), "15min") is True


def test_no_opposite_exposure_never_blocks():
    s = _scanner(open_trades={})  # nothing to protect
    assert s._opposite_exit_confirm_ready("NIFTY", _sig("SELL"), "15min") is True


def test_same_direction_signal_is_not_opposite():
    trade = SimpleNamespace(instrument="NIFTY", timeframe="15min", direction="LONG")
    s = _scanner(open_trades={"t1": trade})
    # A BUY signal is NOT opposite to a LONG -> no exposure to debounce.
    assert s._opposite_exit_confirm_ready("NIFTY", _sig("BUY"), "15min") is True


def test_stale_timer_is_pruned_when_position_is_flat():
    # An opposite flip starts a confirm timer while a trade is open...
    trade = SimpleNamespace(instrument="NIFTY", timeframe="15min", direction="LONG")
    s = _scanner(open_trades={"t1": trade})
    sig = _sig("SELL", ts=datetime(2026, 6, 22, 10, 0, 0))
    assert s._opposite_exit_confirm_ready("NIFTY", sig, "15min") is False
    _age_pending(s, 60.0)

    # ...the trade then exits via a different path (e.g. stop). Now flat: the
    # stale timer must be pruned so it can't fast-track a future position's exit.
    s.trades.open_trades = {}
    assert s._opposite_exit_confirm_ready("NIFTY", sig, "15min") is True
    assert s._pending_opposite_exit == {}

    # A new position opens and the same identity recurs -> must start a FRESH
    # window, not confirm instantly off the pruned timer.
    s.trades.open_trades = {"t2": SimpleNamespace(instrument="NIFTY", timeframe="15min", direction="LONG")}
    assert s._opposite_exit_confirm_ready("NIFTY", sig, "15min") is False


def test_repaint_to_new_candle_signal_resets_the_timer():
    trade = SimpleNamespace(instrument="NIFTY", timeframe="15min", direction="LONG")
    s = _scanner(open_trades={"t1": trade})
    first = _sig("SELL", ts=datetime(2026, 6, 22, 10, 0, 0))
    assert s._opposite_exit_confirm_ready("NIFTY", first, "15min") is False
    _age_pending(s, 25.0)

    # A different candle's opposite flip arrives: the stale timer is dropped and a
    # fresh confirmation window starts, so it must not confirm instantly.
    second = _sig("SELL", ts=datetime(2026, 6, 22, 10, 15, 0))
    assert s._opposite_exit_confirm_ready("NIFTY", second, "15min") is False
    assert all("10:00:00" not in k for k in s._pending_opposite_exit)
