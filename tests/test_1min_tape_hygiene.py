"""1min tape hygiene: only log entry-worthy 1min thrust signals.

A 1min signal can ONLY enter via the volume-spike first filter: normally 2.5x
volume, or the stronger-context 2.0x floor when ADX/intel/power context agrees.
No-spike 1min rejections/detections are pure tape noise. `_suppress_1min_no_spike_tape`
decides whether to keep such a signal off the live tape.
"""

from types import SimpleNamespace

from engine.signal_processor import SignalProcessor

_S = lambda **kw: SimpleNamespace(live_1min_log_only_volume_spike=kw.get("enabled", True))


def test_suppresses_no_spike_1min():
    assert SignalProcessor._suppress_1min_no_spike_tape("1min", False, _S()) is True


def test_keeps_spike_cleared_1min():
    assert SignalProcessor._suppress_1min_no_spike_tape("1min", True, _S()) is False


def test_never_suppresses_5min_or_15min():
    assert SignalProcessor._suppress_1min_no_spike_tape("5min", False, _S()) is False
    assert SignalProcessor._suppress_1min_no_spike_tape("15min", False, _S()) is False


def test_respects_disable_toggle():
    assert SignalProcessor._suppress_1min_no_spike_tape("1min", False, _S(enabled=False)) is False


def test_defaults_on_when_setting_absent():
    # No attribute -> getattr default True -> suppress.
    assert SignalProcessor._suppress_1min_no_spike_tape("1min", False, SimpleNamespace()) is True


def test_non_spike_1min_cannot_compete_for_entry_selection():
    one_min = {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=False)}
    five_min = {"tf": "5min", "sig": SimpleNamespace(volume_spike_entry=False)}
    fifteen_min = {"tf": "15min", "sig": SimpleNamespace(volume_spike_entry=False)}

    assert SignalProcessor._entry_admissible_raw_candidates([one_min, five_min, fifteen_min]) == [
        five_min,
        fifteen_min,
    ]


def test_spike_1min_can_compete_for_entry_selection():
    one_min = {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=True)}
    five_min = {"tf": "5min", "sig": SimpleNamespace(volume_spike_entry=False)}

    assert SignalProcessor._entry_admissible_raw_candidates([one_min, five_min]) == [
        one_min,
        five_min,
    ]


def test_reversion_window_admits_non_spike_1min():
    """POST-WIN SNAPBACK path: 1min rows compete while reversion window is open
    even without volume_spike_entry (1M SPIKE may be OFF)."""
    one_min = {"tf": "1min", "sig": SimpleNamespace(volume_spike_entry=False)}
    five_min = {"tf": "5min", "sig": SimpleNamespace(volume_spike_entry=False)}

    assert SignalProcessor._entry_admissible_raw_candidates(
        [one_min, five_min],
        reversion_window_open=True,
    ) == [one_min, five_min]
    assert SignalProcessor._entry_admissible_raw_candidates(
        [one_min, five_min],
        reversion_window_open=False,
    ) == [five_min]


def test_tf_select_suppresses_plain_1min_diagnostic():
    sig = SimpleNamespace(volume_spike_entry=False, power_move=False)

    assert SignalProcessor._should_log_tf_select_diagnostic(
        "1min",
        sig,
        [(0.8, sig, "1min", "A", 0.8, [], False), (0.75, sig, "5min", "A", 0.75, [], False)],
        _S(),
        trade_involved=False,
    ) is False


def test_tf_select_logs_1min_volume_burst():
    sig = SimpleNamespace(volume_spike_entry=True, power_move=False)

    assert SignalProcessor._should_log_tf_select_diagnostic(
        "1min",
        sig,
        [(1.8, sig, "1min", "A", 0.8, [], False)],
        _S(),
        trade_involved=False,
    ) is True


def test_tf_select_logs_when_trade_involved_or_non_1min():
    sig = SimpleNamespace(volume_spike_entry=False, power_move=False)

    assert SignalProcessor._should_log_tf_select_diagnostic(
        "1min",
        sig,
        [(0.8, sig, "1min", "A", 0.8, [], False)],
        _S(),
        trade_involved=True,
    ) is True
    assert SignalProcessor._should_log_tf_select_diagnostic(
        "5min",
        sig,
        [(0.8, sig, "5min", "A", 0.8, [], False), (0.79, sig, "15min", "A", 0.79, [], False)],
        _S(),
        trade_involved=False,
    ) is True
