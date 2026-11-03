"""A power move must be measured on the signal's own bar, not on a forming one.

`_detect_power_move` takes `df.tail(lookback + 1).iloc[-1]` as the candle to judge. With
no `as_of` that is, live, the FORMING bar -- so the volume surge, range expansion and
close location that define a power move were read off an unfinished candle that could
still reverse, and a power move could appear and vanish inside one bar.

Its sibling thrust lane never had this: `_detect_volume_spike_entry` is called with
`as_of=sig.timestamp`, and the HISTORICAL reconstruction of `_detect_power_move` passes
`as_of` too. Only the live power-move call omitted it, so live and replay could disagree
about the very same bar -- which is exactly the kind of gap that makes a backtest
unfalsifiable.
"""
import inspect
import re

import pytest


LIVE_CALL = re.compile(
    r"power_move = scanner\._detect_power_move\((.*?)\n        \)", re.S
)


def _signal_processor_source():
    import engine.signal_processor as sp
    return inspect.getsource(sp)


def test_the_live_power_move_call_pins_a_bar():
    """Read from the imported module, so a stale .py on disk cannot pass this."""
    match = LIVE_CALL.search(_signal_processor_source())
    assert match, "live _detect_power_move call not found"
    assert "as_of=" in match.group(1)


def test_it_pins_the_signal_timestamp_specifically():
    match = LIVE_CALL.search(_signal_processor_source())
    args = match.group(1)
    assert 'as_of=getattr(best_sig, "timestamp", None)' in args


def test_the_detector_accepts_as_of_at_all():
    from scanner import Scanner
    params = inspect.signature(Scanner._detect_power_move).parameters
    assert "as_of" in params
    assert params["as_of"].default is None


def test_the_sibling_thrust_lane_still_pins_its_bar():
    """The precedent this change follows. If this ever regresses, the two lanes
    disagree again and this file's premise is gone."""
    source = _signal_processor_source()
    match = re.search(
        r"scanner\._detect_volume_spike_entry\((.*?)\n                        \)", source, re.S
    )
    assert match, "live _detect_volume_spike_entry call not found"
    assert "as_of=sig.timestamp" in match.group(1)


def test_an_absent_timestamp_degrades_to_the_previous_behaviour():
    """A signal with no timestamp must not raise; as_of=None is the old path."""
    from scanner import Scanner

    class _Bare:
        pass

    assert getattr(_Bare(), "timestamp", None) is None
    # _detect_power_move guards as_of with `if as_of is not None`, so None is inert.
    source = inspect.getsource(Scanner._detect_power_move)
    assert "if as_of is not None:" in source


def test_as_of_slices_the_frame_before_the_metrics_are_read():
    """The slice must happen before `recent`/`last` are taken, or pinning does nothing."""
    source = inspect.getsource(__import__("scanner").Scanner._detect_power_move)
    slice_at = source.find("_entry_candles_at")
    last_at = source.find("last = recent.iloc[-1]")
    assert slice_at != -1 and last_at != -1
    assert slice_at < last_at
