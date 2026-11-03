"""Regression proof for contract touches hidden inside one 100 ms monitor window.

Kite can deliver 100 -> 112 -> 101 before Scanner's next pass.  The ordinary quote
cache retains only 101, so a 110 target (or a stop crossed on the inverse path) is lost.
The raw path must stay ordered, bounded, contract-specific, and must be evaluated
against the stop that was active while those ticks arrived rather than a stop tightened
later in the monitor pass.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
import threading
import time

import pytest

from data.market_data import MarketDataProvider
from engine.exit_cycle import ExitCycleService
from scanner import Scanner
from trading.trade_manager import IST, TradeManager


def _provider():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._contract_tick_history_size = 8
    return provider


def test_contract_tick_history_preserves_touch_then_rebound_in_order():
    provider = _provider()
    base = 1_780_000_000.0

    provider._record_contract_tick("123", 100.0, base)
    provider._record_contract_tick("123", 112.0, base + 0.020)
    provider._record_contract_tick("123", 101.0, base + 0.040)

    ticks, cursor, overflowed = provider.get_contract_ticks_since("123")

    assert [tick[1] for tick in ticks] == [100.0, 112.0, 101.0]
    assert [tick[0] for tick in ticks] == [1, 2, 3]
    assert cursor == 3
    assert overflowed is False


def test_contract_tick_history_is_per_token_and_incremental():
    provider = _provider()
    base = 1_780_000_000.0
    provider._record_contract_tick("A", 100.0, base)
    provider._record_contract_tick("B", 200.0, base + 0.010)
    provider._record_contract_tick("A", 101.0, base + 0.020)

    first, cursor, _ = provider.get_contract_ticks_since("A")
    provider._record_contract_tick("A", 102.0, base + 0.030)
    second, next_cursor, _ = provider.get_contract_ticks_since("A", cursor)

    assert [tick[1] for tick in first] == [100.0, 101.0]
    assert [tick[1] for tick in second] == [102.0]
    assert next_cursor == 3


def test_contract_tick_history_reports_bounded_buffer_overflow():
    provider = _provider()
    provider._contract_tick_history_size = 16
    for price in range(100, 118):
        provider._record_contract_tick("123", float(price), 1_780_000_000.0 + price)

    ticks, cursor, overflowed = provider.get_contract_ticks_since("123", 1)

    assert [tick[1] for tick in ticks] == [float(price) for price in range(102, 118)]
    assert cursor == 18
    assert overflowed is True


def test_open_position_retains_long_burst_while_candidate_stays_bounded():
    provider = _provider()
    provider._contract_tick_history_size = 16
    provider._active_contract_tick_history_size = 8192
    provider.set_active_contract_tick_tokens({"LIVE"})
    for index in range(5000):
        provider._record_contract_tick("LIVE", 100.0 + index / 100.0, 1_780_000_000.0 + index / 1000.0)
        provider._record_contract_tick("CANDIDATE", 100.0, 1_780_000_000.0 + index / 1000.0)
    live, live_cursor, live_overflow = provider.get_contract_ticks_since("LIVE", 1)
    candidate, _, candidate_overflow = provider.get_contract_ticks_since("CANDIDATE", 1)
    assert len(live) == 4999 and live_cursor == 5000 and live_overflow is False
    assert len(candidate) == 16 and candidate_overflow is True


def test_active_position_tick_history_is_not_evicted_before_candidates():
    provider = _provider()
    provider._contract_tick_history_token_limit = 8
    provider.set_active_contract_tick_tokens({"LIVE"})
    base = 1_780_000_000.0
    provider._record_contract_tick("LIVE", 100.0, base)
    for index in range(8):
        provider._record_contract_tick(f"C{index}", 100.0, base + index + 1)
    ticks, _, _ = provider.get_contract_ticks_since("LIVE")
    assert len(ticks) == 1


def test_exit_worker_prioritizes_only_open_position_contracts(monkeypatch):
    selected = []
    data = SimpleNamespace(set_active_contract_tick_tokens=lambda tokens: selected.append(set(tokens)))
    trades = SimpleNamespace(open_trades={
        "open": SimpleNamespace(symbol_token="LIVE", status="OPEN"),
        "closed": SimpleNamespace(symbol_token="OLD", status="CLOSED"),
    })
    scanner = SimpleNamespace(data=data, trades=trades)
    service = ExitCycleService(scanner)
    monkeypatch.setattr(service, "update_active_trades", lambda **_kw: None)
    service.run_tick()
    assert selected == [{"LIVE"}]


def test_concurrent_socket_writers_keep_sequence_order_and_bounded_memory():
    provider = _provider()
    provider._contract_tick_history_size = 2048
    barrier = threading.Barrier(5)

    def write_batch(batch):
        barrier.wait()
        for offset in range(500):
            provider._record_contract_tick(
                "123", 100.0 + batch + offset / 1000.0, time.time()
            )

    writers = [threading.Thread(target=write_batch, args=(batch,)) for batch in range(4)]
    for writer in writers:
        writer.start()
    barrier.wait()
    for writer in writers:
        writer.join(timeout=2.0)

    ticks, cursor, overflowed = provider.get_contract_ticks_since("123")
    sequences = [tick[0] for tick in ticks]
    assert all(not writer.is_alive() for writer in writers)
    assert len(ticks) == 2000
    assert sequences == list(range(1, 2001))
    assert cursor == 2000
    assert overflowed is False


def _manager_with_trade(*, inst_type="OPT", direction="LONG", stop=90.0, target=110.0):
    manager = TradeManager(broker=None, max_positions=5)
    manager.mode = "HISTORICAL"
    manager.open_trades.clear()
    trade = manager.open_trade(
        instrument="NIFTY",
        timeframe="1min",
        direction=direction,
        price=100.0,
        trailing_stop=stop,
        lots=1,
        lot_size=25,
        grade="A",
        confidence=0.8,
        target=target,
        inst_type=inst_type,
        option_type="CE" if inst_type == "OPT" else "",
        instrument_multiplier=0.5 if inst_type == "OPT" else 1.0,
        entry_spot=25_000.0,
        is_ghost=True,
        initial_stop=stop,
    )
    return manager, trade


def test_option_target_touch_survives_rebound_before_monitor_pass():
    manager, trade = _manager_with_trade()
    now = datetime(2026, 9, 6, 10, 0)

    manager.update_trade(
        trade.id,
        current_spot=25_002.0,
        real_premium=101.0,
        quote_verified=True,
        current_time=now + timedelta(milliseconds=100),
        observed_contract_ticks=[
            (100.0, now),
            (112.0, now + timedelta(milliseconds=20)),
            (101.0, now + timedelta(milliseconds=40)),
        ],
    )

    assert trade.id not in manager.open_trades
    closed = next(row for row in manager.closed_trades if row.id == trade.id)
    assert closed.exit_reason == "TARGET_HIT"
    assert closed.exit_price == pytest.approx(112.0)


def test_short_future_stop_touch_survives_rebound_before_monitor_pass():
    manager, trade = _manager_with_trade(
        inst_type="FUT", direction="SHORT", stop=105.0, target=90.0
    )
    now = datetime(2026, 9, 6, 10, 0)

    manager.update_trade(
        trade.id,
        current_spot=100.0,
        real_fut_price=100.0,
        current_time=now + timedelta(milliseconds=100),
        observed_contract_ticks=[
            (100.0, now),
            (106.0, now + timedelta(milliseconds=20)),
            (100.0, now + timedelta(milliseconds=40)),
        ],
    )

    assert trade.id not in manager.open_trades
    closed = next(row for row in manager.closed_trades if row.id == trade.id)
    assert closed.exit_reason == "TRAILING_STOP"
    assert closed.exit_price == pytest.approx(106.0)


def test_old_tick_cannot_hit_stop_tightened_after_that_tick():
    manager, trade = _manager_with_trade(stop=90.0, target=0.0)
    now = datetime(2026, 9, 6, 10, 0)

    manager.update_trade(
        trade.id,
        current_spot=25_002.0,
        real_premium=101.0,
        quote_verified=True,
        new_trailing_stop=98.0,
        current_time=now + timedelta(milliseconds=100),
        observed_contract_ticks=[
            (100.0, now),
            (95.0, now + timedelta(milliseconds=20)),
            (101.0, now + timedelta(milliseconds=40)),
        ],
    )

    assert trade.id in manager.open_trades
    assert trade.current_stop == pytest.approx(98.0)


def test_unverified_path_cannot_trigger_option_exit():
    manager, trade = _manager_with_trade()
    now = datetime(2026, 9, 6, 10, 0)

    manager.update_trade(
        trade.id,
        current_spot=25_002.0,
        real_premium=None,
        quote_verified=False,
        current_time=now + timedelta(milliseconds=100),
        observed_contract_ticks=[(112.0, now + timedelta(milliseconds=20))],
    )

    assert trade.id in manager.open_trades


def test_runner_ignores_retired_hard_target_but_keeps_stop_touch():
    manager, trade = _manager_with_trade(target=110.0)
    trade.runner_mode = True
    now = datetime(2026, 9, 6, 10, 0)

    manager.update_trade(
        trade.id,
        current_spot=25_002.0,
        real_premium=101.0,
        quote_verified=True,
        current_time=now + timedelta(milliseconds=100),
        observed_contract_ticks=[(112.0, now + timedelta(milliseconds=20))],
    )

    assert trade.id in manager.open_trades


def test_fresh_option_websocket_tick_beats_two_second_fallback_cache():
    scanner = Scanner.__new__(Scanner)
    scanner._premium_cache = {"123": {"price": 100.0, "time": time.time()}}
    scanner.market_info = {}
    scanner._option_premium_quote_plausible = lambda _trade, _price: True
    scanner.data = SimpleNamespace(
        subscribe_tokens=lambda *_args, **_kwargs: None,
        get_cached_contract_ltp=lambda *_args, **_kwargs: 112.0,
    )
    trade = SimpleNamespace(
        symbol_token="123",
        trading_symbol="NIFTY26SEP25000CE",
        instrument="NIFTY",
        entry_price=100.0,
        inst_type="OPT",
    )

    assert scanner._get_live_premium_cached(trade, allow_blocking=False) == 112.0


def test_fresh_future_websocket_tick_beats_two_second_fallback_cache():
    scanner = Scanner.__new__(Scanner)
    scanner._candidate_ltp_cache = {
        "FUT:456": {"price": 25_000.0, "time": time.time()}
    }
    scanner.data = SimpleNamespace(
        _ws_cache={"456": {"ltp": 25_120.0, "time": time.time()}},
    )
    trade = SimpleNamespace(
        symbol_token="456",
        trading_symbol="NIFTY26SEPFUT",
    )

    assert scanner._get_live_fut_ltp_cached(trade, allow_blocking=False) == 25_120.0


def test_scanner_consumes_only_post_entry_ticks_and_only_once():
    provider = _provider()
    base = time.time()
    provider._record_contract_tick("123", 999.0, base)
    trade = SimpleNamespace(
        symbol_token="123",
        trading_symbol="NIFTY26SEP25000CE",
        inst_type="OPT",
        entry_time=datetime.fromtimestamp(base + 0.010, IST),
    )
    provider._record_contract_tick("123", 112.0, base + 0.020)
    provider._record_contract_tick("123", 101.0, base + 0.040)
    scanner = Scanner.__new__(Scanner)
    scanner.data = provider
    scanner._option_premium_quote_plausible = lambda _trade, _price: True

    first = scanner._trade_contract_tick_path(trade)
    uncommitted_retry = scanner._trade_contract_tick_path(trade)
    scanner._commit_trade_contract_tick_path(trade)
    second = scanner._trade_contract_tick_path(trade)

    assert [price for price, _when in first] == [112.0, 101.0]
    assert uncommitted_retry == first
    assert second == []


class _StubTicker:
    MODE_FULL = "full"

    def __init__(self, _api_key, _access_token):
        self.on_ticks = self.on_connect = self.on_close = None
        self.on_error = self.on_reconnect = self.on_noreconnect = None
        self.on_order_update = None

    def connect(self, threaded=True):
        return threaded

    def subscribe(self, _tokens):
        return None

    def set_mode(self, _mode, _tokens):
        return None


def test_real_kite_callback_records_derivative_path_without_calling_trade_logic(monkeypatch):
    kiteconnect = pytest.importorskip("kiteconnect")
    monkeypatch.setattr(kiteconnect, "KiteTicker", _StubTicker, raising=False)
    provider = _provider()
    provider.start_streams = True
    provider.kite = SimpleNamespace(
        _auth=SimpleNamespace(api_key="k", kite=SimpleNamespace(access_token="t"))
    )
    provider._close_ws_socket = lambda *_args, **_kwargs: None
    provider._active_kite_tokens = {123}
    provider._kite_index_names = {}
    provider._kite_ws_connected = False
    provider._ws_cache = {}
    provider._index_quote_lock = threading.RLock()
    provider._last_kite_ws_tick = 0.0
    provider._last_ws_tick = 0.0
    provider._last_ltp_time = 0.0

    assert provider.start_kite_ticker() is True
    provider.kws.on_ticks(
        provider.kws,
        [
            {"instrument_token": 123, "last_price": 112.0},
            {"instrument_token": 123, "last_price": 101.0},
        ],
    )

    ticks, _cursor, _overflowed = provider.get_contract_ticks_since("123")
    assert [tick[1] for tick in ticks] == [112.0, 101.0]
