from scanner import Scanner


def test_notify_trade_event_resets_throttle_and_pokes_callback():
    s = Scanner.__new__(Scanner)
    s._last_broadcast_time = 12345.0
    calls = []
    s.on_trade_event = lambda: calls.append(True)

    s._notify_trade_event()

    # Throttle dropped so the next broadcast is not delayed, and the panel poked.
    assert s._last_broadcast_time == 0.0
    assert calls == [True]


def test_notify_trade_event_is_safe_without_callback():
    s = Scanner.__new__(Scanner)
    s._last_broadcast_time = 5.0
    s.on_trade_event = None
    # Must not raise when no dashboard notifier is wired (e.g. headless/backtest).
    s._notify_trade_event()
    assert s._last_broadcast_time == 0.0


def test_notify_trade_event_swallows_callback_errors():
    s = Scanner.__new__(Scanner)
    s._last_broadcast_time = 9.0

    def boom():
        raise RuntimeError("loop gone")

    s.on_trade_event = boom
    s._notify_trade_event()  # must not propagate
    assert s._last_broadcast_time == 0.0
