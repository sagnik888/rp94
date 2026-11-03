"""Caught-up trade monitors must not scan the retained contract history."""

from collections import deque
import threading

import pytest

from data.market_data import MarketDataProvider


class CountedHistory(deque):
    def __iter__(self):
        self.iterations += 1
        for tick in super().__iter__():
            self.visits += 1
            yield tick

    def __reversed__(self):
        self.reverse_iterations += 1
        for tick in super().__reversed__():
            self.visits += 1
            yield tick


def provider_with_history():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    history = CountedHistory(
        ((index, 100.0 + index / 1000.0, 1_780_000_000.0 + index, 0.0)
         for index in range(1, 32769)),
        maxlen=32768,
    )
    history.iterations = 0
    history.reverse_iterations = 0
    history.visits = 0
    provider._contract_tick_history_lock = threading.Lock()
    provider._contract_tick_history = {"A": history}
    return provider, history


@pytest.mark.parametrize("cursor", [32768, 32780])
@pytest.mark.parametrize("not_before", [0.0, 1_800_000_000.0])
def test_caught_up_cursor_does_not_walk_full_retained_history(cursor, not_before):
    provider, history = provider_with_history()

    assert provider.get_contract_ticks_since("A", cursor, not_before) == (
        [], 32768, False,
    )
    assert history.iterations == 0
    assert history.reverse_iterations == 0
    assert history.visits == 0


@pytest.mark.parametrize("not_before", [0.0, 1_800_000_000.0])
def test_one_unread_tick_visits_only_the_unread_tail_and_cursor_boundary(not_before):
    provider, history = provider_with_history()

    ticks, newest, overflowed = provider.get_contract_ticks_since("A", 32767, not_before)

    assert [tick[0] for tick in ticks] == ([32768] if not_before == 0.0 else [])
    assert newest == 32768 and overflowed is False
    assert history.visits <= 2


def test_unread_tail_keeps_order_time_filter_and_copy_isolation():
    provider, history = provider_with_history()

    ticks, newest, overflowed = provider.get_contract_ticks_since(
        "A", 32764, not_before=1_780_032_766.0,
    )

    assert [tick[0] for tick in ticks] == [32766, 32767, 32768]
    assert newest == 32768 and overflowed is False
    ticks.clear()
    assert len(history) == 32768


def test_initial_cursor_still_reads_tail_and_overflow_is_reported():
    provider, _ = provider_with_history()
    provider._contract_tick_history["A"] = deque(
        [(20, 95.0, 100.0, 0.0), (21, 105.0, 101.0, 0.0)], maxlen=16,
    )

    initial, newest, overflowed = provider.get_contract_ticks_since("A", 0)
    assert [tick[1] for tick in initial] == [95.0, 105.0]
    assert newest == 21 and overflowed is False
    assert provider.get_contract_ticks_since("A", 1) == (initial, 21, True)
    assert provider.get_contract_ticks_since("A", 21) == ([], 21, False)
    assert provider.get_contract_ticks_since("B", 21) == ([], 21, False)


@pytest.mark.parametrize("after_sequence,overflowed", [(0, False), (1, True), (20, False)])
def test_nonmonotonic_receipt_times_do_not_hide_older_eligible_unread_ticks(after_sequence, overflowed):
    provider, _ = provider_with_history()
    retained = [
        (20, 100.0, 700.0, 0.0),
        (21, 95.0, 100.0, 0.0),
        (22, 110.0, 800.0, 0.0),
        (23, 101.0, 100.0, 0.0),
    ]
    provider._contract_tick_history["A"] = deque(retained, maxlen=16)

    actual = provider.get_contract_ticks_since("A", after_sequence, not_before=500.0)
    expected = [tick for tick in retained if tick[0] > after_sequence and tick[2] >= 500.0]

    assert actual == (expected, 23, overflowed)
