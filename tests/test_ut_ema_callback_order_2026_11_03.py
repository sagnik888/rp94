"""Chart mutation must not reverse the native observation queue."""
import threading
import pandas as pd

from engine.ut_ema_stream import UTEmaTickService, install_tick_service
from tests.test_ut_ema_stream_2026_10_29 import host


def test_slow_older_candle_callback_does_not_queue_behind_newer_tick(tmp_path, monkeypatch):
    scanner, _ = host()
    entered, release = threading.Event(), threading.Event()
    at = pd.Timestamp("2026-10-29 09:25:01")
    def candles(instrument, price, volume, stamp, source):
        if stamp == at:
            entered.set()
            assert release.wait(2)
    scanner.data.on_tick = candles
    scanner.candles._price_plausible = lambda *args: True
    scanner.data.is_market_open = lambda *args: True
    # Keep the real submit path and inspect the queue before its consumer runs.
    monkeypatch.setattr(UTEmaTickService, "_run", lambda self: None)
    service = install_tick_service(scanner, checkpoint_dir=tmp_path)
    older = threading.Thread(target=scanner.data.on_tick, args=("NIFTY", 101., 0, at, ""))
    older.start()
    try:
        assert entered.wait(1)
        scanner.data.on_tick("NIFTY", 102., 0, at+pd.Timedelta(seconds=1), "")
    finally:
        release.set()
        older.join(2)
    assert [item[2] for item in service._queue] == [at, at+pd.Timedelta(seconds=1)]
    assert [item[1] for item in service._queue] == [101., 102.]


def test_rejected_price_never_enters_native_queue(tmp_path, monkeypatch):
    scanner, _ = host()
    called = []
    scanner.data.on_tick = lambda *args: called.append(args)
    scanner.candles._price_plausible = lambda *args: False
    service = install_tick_service(scanner, checkpoint_dir=tmp_path)
    scanner.data.on_tick("NIFTY", 900000., 0, pd.Timestamp("2026-10-29 09:25"), "")
    assert len(called) == 1
    assert not service._queue


def test_backup_volume_cannot_advance_native_price_clock_past_delayed_owner(tmp_path, monkeypatch):
    from tests.test_three_broker_priority_2026_10_23 import three, cached
    scanner, adapter = host()
    source = three(tmp_path)
    scanner.data = source
    candles = []
    source.on_tick = lambda *args: candles.append(args)
    source._tick_callback_takes_source = None
    source._tick_callback_ref = None
    monkeypatch.setattr(UTEmaTickService, "_run", lambda self: None)
    service = install_tick_service(scanner, checkpoint_dir=tmp_path / "stream")
    at = pd.Timestamp("2026-10-29 09:25", tz="Asia/Kolkata").timestamp()
    source._ws_cache["100"] = cached(100., "kite_ws", at)
    assert source._emit_arbitrated_index_tick("NIFTY", 100., 0., "", "kite_ws", at, at)
    # A volume-only backup packet carries the owner's cached price. Its newer
    # receipt must not overtake the next owner packet delayed inside a batch.
    assert source._emit_arbitrated_index_tick("NIFTY", 999., 10., "fyers_index", "fyers_ws", at + 2, at + 2)
    source._ws_cache["100"] = cached(101., "kite_ws", at + 1)
    assert source._emit_arbitrated_index_tick("NIFTY", 101., 0., "", "kite_ws", at + 1, at + 1)
    for observation in list(service._queue):
        service.process_observation(*observation)
    assert adapter._native.prices == [100., 101.]
    assert service.health["NIFTY_5min"]["status"] == "READY"
    assert len(candles) == 3
    assert candles[1][1:3] == (100., 10.)
    assert candles[1][-1] == "fyers_index"


def test_angel_and_fyers_owner_ticks_still_reach_native_after_failover(tmp_path, monkeypatch):
    from tests.test_three_broker_priority_2026_10_23 import three, cached
    from data.market_data import FYERS_INDEX_SYMBOLS
    scanner, adapter = host()
    source = three(tmp_path)
    scanner.data = source
    candles = []
    source.on_tick = lambda *args: candles.append(args)
    source._tick_callback_takes_source = None
    source._tick_callback_ref = None
    monkeypatch.setattr(UTEmaTickService, "_run", lambda self: None)
    service = install_tick_service(scanner, checkpoint_dir=tmp_path / "stream")
    at = pd.Timestamp("2026-10-29 09:25", tz="Asia/Kolkata").timestamp()
    source._ws_cache["angel:NIFTY"] = cached(101., "angel_ws", at)
    assert not source._emit_arbitrated_index_tick("NIFTY", 101., 0., "", "angel_ws", at, at)
    assert source._emit_arbitrated_index_tick("NIFTY", 101., 0., "", "angel_ws", at + .3, at + .3)
    assert source._emit_arbitrated_index_tick("NIFTY", 999., 10., "fyers_index", "fyers_ws", at + .4, at + .4)
    source._ws_cache["angel:NIFTY"] = cached(101., "angel_ws", at - 100)
    source._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = cached(102., "fyers_ws", at + 1)
    assert not source._emit_arbitrated_index_tick("NIFTY", 102., 20., "fyers_index", "fyers_ws", at + 1, at + 1)
    assert source._emit_arbitrated_index_tick("NIFTY", 102., 21., "fyers_index", "fyers_ws", at + 1.3, at + 1.3)
    for observation in list(service._queue):
        service.process_observation(*observation)
    assert adapter._native.prices == [101., 102.]
    assert len(candles) == 3
    assert candles[-1][2] == 21.


def test_genuinely_late_observation_still_cannot_rewind_owner(tmp_path):
    scanner, adapter = host()
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    at = pd.Timestamp("2026-10-29 09:25:02")
    service.process_observation("NIFTY", 101., at, 1)
    service.process_observation("NIFTY", 99., at-pd.Timedelta(seconds=1), 2)
    assert adapter._native.prices == [101.]
    health = service.health["NIFTY_5min"]
    assert health["status"] == "STALE_TICK"
    assert health["last_stale_delay_seconds"] == 1.
    assert health["stale_ticks_ignored"] == 1
    assert health["ignored_stale_at"] == (at-pd.Timedelta(seconds=1)).isoformat()


def test_preopen_reference_is_excluded_but_session_gaps_still_block(tmp_path):
    scanner, _ = host()
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    stamps = pd.date_range("2026-10-29 09:00", periods=5, freq="15min")
    frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100.), index=stamps)
    filtered = service._session_history(frame)
    assert filtered.index.equals(stamps[1:])
    assert len(frame) == 5
    assert not service._history_gap(filtered, "15min", pd.Timestamp("2026-10-29 10:15"))
    missing = filtered.drop(pd.Timestamp("2026-10-29 09:45"))
    assert "missing" in service._history_gap(missing, "15min", pd.Timestamp("2026-10-29 10:15"))
    shifted = filtered.copy()
    shifted.index = shifted.index+pd.Timedelta(minutes=1)
    assert "grid" in service._history_gap(service._session_history(shifted), "15min", pd.Timestamp("2026-10-29 10:15"))


def test_special_session_open_is_not_discarded(tmp_path):
    scanner, _ = host()
    scanner.data.get_index_session_bounds = lambda at: (
        pd.Timestamp(at).normalize()+pd.Timedelta(hours=9),
        pd.Timestamp(at).normalize()+pd.Timedelta(hours=10))
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    frame = pd.DataFrame(dict(close=[100.,101.]), index=pd.to_datetime(["2026-10-29 09:00", "2026-10-29 09:15"]))
    pd.testing.assert_frame_equal(service._session_history(frame), frame)


def test_unknown_special_hours_are_preserved_for_validation(tmp_path, monkeypatch):
    scanner, _ = host()
    monkeypatch.setattr("data.market_calendar.load_nse_special_trading_days", lambda: {"2026-10-29"})
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    frame = pd.DataFrame(dict(close=[100.,101.]), index=pd.to_datetime(["2026-10-29 18:00", "2026-10-29 18:15"]))
    pd.testing.assert_frame_equal(service._session_history(frame), frame)


def test_missing_closed_bars_require_every_official_minute(tmp_path):
    scanner, _ = host()
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    stamps = pd.date_range("2026-10-29 09:15", periods=45, freq="min")
    base = pd.DataFrame(dict(open=100., high=102., low=99., close=101.), index=stamps)
    history = base.iloc[[0,30]].copy()
    original = history.copy()
    scanner.candles.get_candles = lambda *args: base
    recovered = service._bootstrap_history("NIFTY", "15min", history, pd.Timestamp("2026-10-29 10:00"))
    assert recovered.loc["2026-10-29 09:30"].tolist() == [100.,102.,99.,101.]
    assert not service._history_gap(recovered, "15min", pd.Timestamp("2026-10-29 10:00"))
    pd.testing.assert_frame_equal(history, original)
    scanner.candles.get_candles = lambda *args: base.drop(pd.Timestamp("2026-10-29 09:35"))
    incomplete = service._bootstrap_history("NIFTY", "15min", history, pd.Timestamp("2026-10-29 10:00"))
    assert pd.Timestamp("2026-10-29 09:30") not in incomplete.index
    assert "missing" in service._history_gap(incomplete, "15min", pd.Timestamp("2026-10-29 10:00"))
    scanner.candles.get_candles = lambda *args: base
    future = service._bootstrap_history("NIFTY", "15min", history, pd.Timestamp("2026-10-29 09:44"))
    assert pd.Timestamp("2026-10-29 09:30") not in future.index
