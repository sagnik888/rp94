"""Same-bar union arrows keep independent virgin/repaint identities."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from engine.alt_scanners import AltSignalEngine
from engine.signal_processor import SignalProcessor
from scanner import Scanner


TS = datetime(2026, 8, 21, 11, 10)
KEY = "NIFTY_5min"


def _signal(signal_type: str, source: str = "utbot"):
    return SimpleNamespace(
        timestamp=TS,
        signal_type=signal_type,
        bar_index=23,
        source_engine=source,
    )


def _candidate(direction: str):
    return SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction=direction,
        source_signal_type="BUY" if direction == "LONG" else "SELL",
    )


def test_union_merge_preserves_opposite_arrows_on_the_same_bar():
    merged = AltSignalEngine._merge_union_signals(
        [_signal("SELL", "ema"), _signal("BUY", "utbot")]
    )

    assert {signal.signal_type for signal in merged} == {"BUY", "SELL"}
    assert len(merged) == 2


def test_each_same_bar_direction_is_virgin_once_not_once_per_scan():
    scanner = SimpleNamespace(
        _last_signal_time={KEY: TS - timedelta(microseconds=1)},
        _last_signal_identity={},
    )
    first_scan_lock = scanner._last_signal_time[KEY]

    for signal_type in ("SELL", "BUY"):
        identity = f"{TS.isoformat()}|{signal_type}"
        assert SignalProcessor._live_signal_is_new(
            TS,
            signal_type,
            first_scan_lock,
            scanner._last_signal_identity.get(KEY),
        )
        SignalProcessor._remember_live_signal_identity(
            scanner, KEY, TS, identity
        )

    assert scanner._last_signal_identity[KEY] == {
        f"{TS.isoformat()}|SELL",
        f"{TS.isoformat()}|BUY",
    }

    second_scan_lock = scanner._last_signal_time[KEY]
    for signal_type in ("SELL", "BUY"):
        assert not SignalProcessor._live_signal_is_new(
            TS,
            signal_type,
            second_scan_lock,
            scanner._last_signal_identity[KEY],
        )


def test_withdrawing_one_identity_does_not_reopen_its_opposite_peer():
    buy_identity = f"{TS.isoformat()}|BUY"
    sell_identity = f"{TS.isoformat()}|SELL"
    scanner = SimpleNamespace(
        _last_signal_time={KEY: TS},
        _last_signal_identity={KEY: {buy_identity, sell_identity}},
    )

    assert SignalProcessor._forget_live_signal_identity(
        scanner, KEY, TS, buy_identity
    )
    assert scanner._last_signal_identity[KEY] == {sell_identity}
    assert scanner._last_signal_time[KEY] == TS
    assert SignalProcessor._live_signal_is_new(
        TS, "BUY", TS, scanner._last_signal_identity[KEY]
    )
    assert not SignalProcessor._live_signal_is_new(
        TS, "SELL", TS, scanner._last_signal_identity[KEY]
    )


@pytest.mark.parametrize(
    ("signals", "direction"),
    [
        ([_signal("SELL", "ema"), _signal("BUY", "utbot")], "LONG"),
        ([_signal("BUY", "utbot"), _signal("SELL", "ema")], "SHORT"),
    ],
)
def test_repaint_searches_all_same_timestamp_arrows(signals, direction):
    scanner = Scanner.__new__(Scanner)
    scanner._cached_results = {KEY: {"signals": signals}}

    assert scanner._repaint_signal_status(_candidate(direction), TS) == "PRESENT"


def test_repaint_still_reports_flip_when_only_the_opposite_arrow_remains():
    scanner = Scanner.__new__(Scanner)
    scanner._cached_results = {KEY: {"signals": [_signal("SELL", "ema")]}}

    assert scanner._repaint_signal_status(_candidate("LONG"), TS) == "FLIPPED"
