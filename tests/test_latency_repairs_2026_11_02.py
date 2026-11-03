"""Decision-preserving regressions for the measured November latency paths."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace as NS

import pandas as pd
import pytest


def test_archived_expiry_reuses_identical_resolution(tmp_path, monkeypatch):
    from engine.expiry_manager import ArchivedExpiryContext, ExpiryManager
    (tmp_path/"instruments.json").write_text(json.dumps([
        dict(name="NIFTY", symbol="NIFTY26O2022000CE", expiry="20OCT2026"),
        dict(name="NIFTY", symbol="NIFTY26O2722000CE", expiry="27OCT2026")]))
    resolver = ArchivedExpiryContext(tmp_path)
    original = ExpiryManager._select_option_expiry_for_date
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(ExpiryManager, "_select_option_expiry_for_date", counted)
    first = resolver.resolve("NIFTY", "2026-10-20 10:00")
    assert first["available"]
    expected = copy.deepcopy(first)
    first["source"] = "changed_by_consumer"
    assert resolver.resolve("NIFTY", "2026-10-20 10:00") == expected
    assert len(calls) == 1


def test_identical_replay_does_not_dirty_ledger_twice():
    from tests.test_ut_ema_replay_2026_10_29 import harness
    from engine.ut_ema_replay import replay_candidates
    processor, mtf, settings = harness()
    scanner = processor.scanner
    dirty = []
    scanner.trades._mark_dirty = lambda: dirty.append(True)
    args = (processor, "NIFTY", mtf, {"strike_interval": 50}, 1, 65, settings)
    replay_candidates(*args)
    assert scanner.trades.closed_trades
    expected = scanner.trades.closed_trades[0].to_dict()
    dirty.clear()
    replay_candidates(*args)
    assert scanner.trades.closed_trades[0].to_dict() == expected
    assert dirty == []


def test_replay_owned_trade_never_enters_wall_clock_exit_monitor():
    from engine.exit_cycle import ExitCycleService
    trade = NS(id="H_UTEMA_test", is_live=False, source_engine="ut_ema", source_lane="UTEMA",
               exit_policy_owner="UTEMA", data_provenance="BACKTEST_OBSERVED_OPTION_MINUTES")
    scanner = NS(mode="HISTORICAL", trades=NS(open_trades={trade.id: trade}), _reset_daily_counters=lambda: None)
    scanner._trade_contract_tick_path = lambda t: pytest.fail("Replay row entered wall-clock monitor")
    ExitCycleService(scanner).update_active_trades(allow_blocking=False)


def test_trade_persistence_releases_book_lock_during_database_wait(monkeypatch):
    from trading.trade_manager import TradeManager
    manager = TradeManager.__new__(TradeManager)
    manager._lock = threading.RLock()
    manager.open_trades, manager.closed_trades = {}, []
    entered, release = threading.Event(), threading.Event()
    def save(rows):
        entered.set()
        assert release.wait(3.)
    monkeypatch.setattr("trading.trade_manager.db.save_trades", save)
    writer = threading.Thread(target=manager.save_state)
    writer.start()
    try:
        assert entered.wait(2.)
        acquired = manager._lock.acquire(timeout=.1)
        if acquired:
            manager._lock.release()
        assert acquired, "Database I/O holds the trade book lock"
    finally:
        release.set()
        writer.join(3.)


def test_expiry_cache_respects_gamma_clock_settings_calendar_and_archive(tmp_path, monkeypatch):
    from config.settings import get_settings
    from engine.expiry_manager import ArchivedExpiryContext, ExpiryManager
    settings = get_settings()
    for key, value in dict(expiry_day_trade_current_week=True, expiry_day_gamma_cutoff="13:00",
            expiry_day_late_window=True, expiry_day_late_window_start="14:00", expiry_day_late_window_end="15:15",
            expiry_day_after_cutoff_mode="rollover", option_rollover_working_days=1).items():
        monkeypatch.setattr(settings, key, value)
    path = tmp_path/"instruments.json"
    rows = [dict(name="NIFTY", symbol="NIFTY26O2022000CE", expiry="20OCT2026"),
            dict(name="NIFTY", symbol="NIFTY26O2722000CE", expiry="27OCT2026")]
    path.write_text(json.dumps(rows))
    resolver = ArchivedExpiryContext(tmp_path)
    assert resolver.resolve("NIFTY", "2026-10-20 10:00")["contract_expiry"] == "2026-10-20"
    assert resolver.resolve("NIFTY", "2026-10-20 13:01")["contract_expiry"] == "2026-10-27"
    assert resolver.resolve("NIFTY", "2026-10-20 14:01")["contract_expiry"] == "2026-10-20"
    monkeypatch.setattr(settings, "expiry_day_trade_current_week", False)
    assert resolver.resolve("NIFTY", "2026-10-20 10:00")["contract_expiry"] == "2026-10-27"
    monkeypatch.setattr(ExpiryManager, "_holiday_cache", {"2026-10-21"})
    monkeypatch.setattr(ExpiryManager, "_special_session_cache", set())
    expected = ArchivedExpiryContext(tmp_path).resolve("NIFTY", "2026-10-20 10:00")
    assert resolver.resolve("NIFTY", "2026-10-20 10:00") == expected
    rows[1]["expiry"] = "28OCT2026"
    path.write_text(json.dumps(rows)+" ")
    assert resolver.resolve("NIFTY", "2026-10-20 10:00")["contract_expiry"] == "2026-10-28"


@pytest.mark.parametrize("mode,live,provenance", [
    ("REAL", False, "BACKTEST_OBSERVED_OPTION_MINUTES"),
    ("HISTORICAL", True, "BACKTEST_OBSERVED_OPTION_MINUTES"),
    ("HISTORICAL", False, "MANUAL")])
def test_real_live_and_manual_trade_monitors_remain_active(mode, live, provenance):
    from engine.exit_cycle import ExitCycleService
    trade = NS(id="H_UTEMA_test", is_live=live, source_engine="ut_ema", source_lane="UTEMA",
               exit_policy_owner="UTEMA", data_provenance=provenance)
    scanner = NS(mode=mode, trades=NS(open_trades={trade.id: trade}), _reset_daily_counters=lambda: None)
    def observed(trade):
        raise RuntimeError("monitor_active")
    scanner._trade_contract_tick_path = observed
    with pytest.raises(RuntimeError, match="monitor_active"):
        ExitCycleService(scanner).update_active_trades(allow_blocking=False)


def test_archived_option_lookup_reuses_prices_and_invalidates_sources(tmp_path, monkeypatch):
    from engine.signal_processor import SignalProcessor
    from engine.option_history_store import OptionHistoryStore
    from tests.test_ut_ema_replay_2026_10_29 import options
    monkeypatch.chdir(tmp_path)
    candles = tmp_path/"data_store"/"candles"
    candles.mkdir(parents=True)
    path = candles/"NIFTY26O291000CE_1min.csv"
    bars = options()
    bars.to_csv(path)
    master = tmp_path/"data_store"/"instruments.json"
    master.write_text(json.dumps([dict(symbol="NIFTY26O291000CE", expiry="29OCT2026")]))
    processor = SignalProcessor(NS())
    processor._option_history_store = store = OptionHistoryStore(bundle_path=tmp_path/"none.parquet")
    original = store.load
    reads = []
    def counted(path):
        reads.append(True)
        return original(path)
    monkeypatch.setattr(store, "load", counted)
    def lookup():
        return processor._load_cached_historical_option_contract("NIFTY", 1000, "CE", "1min",
            pd.Timestamp("2026-10-20").date(), now_dt=pd.Timestamp("2026-10-20 12:00"))
    first, symbol = lookup()
    assert symbol == "NIFTY26O291000CE"
    first.iloc[0, 0] = -1.
    assert lookup()[0].iloc[0, 0] == 100.
    assert len(reads) == 1
    bars.iloc[0] = [102., 102.5, 101.5, 102.]
    bars.to_csv(path)
    assert lookup()[0].iloc[0, 0] == 102.
    assert len(reads) == 2
    master.write_text(json.dumps([dict(symbol="NIFTY26O291000CE", expiry="05NOV2026")])+" ")
    assert lookup()[0].attrs["contract_expiry"] == "2026-11-05"
    path.unlink()
    assert lookup()[0].empty


@pytest.mark.parametrize("changed", ["prices", "native", "lots", "rr", "delta", "horizon", "calendar"])
def test_simulation_memo_invalidates_every_decision_input(monkeypatch, changed):
    import engine.ut_ema_replay as replay
    from tests.test_ut_ema_replay_2026_10_29 import native, options
    calls = []
    original = replay.simulate_record
    def counted(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(replay, "simulate_record", counted)
    processor = NS()
    record, bars = native(), options()
    args = [record, bars, 1, 2.5, 1., "2026-10-20 10:05"]
    first = replay._simulate_cached(processor, *args)
    expected = copy.deepcopy(first)
    first["exit_trace"].append({"mutated": True})
    assert replay._simulate_cached(processor, *args) == expected
    assert len(calls) == 1
    if changed == "prices": bars.iloc[0, 3] += .1
    elif changed == "native": record["exit_reason"] = "UT T2"
    elif changed == "lots": args[2] = 2
    elif changed == "rr": args[3] = 3.
    elif changed == "delta": args[4] = .8
    elif changed == "horizon": args[5] = "2026-10-20 10:06"
    else: monkeypatch.setattr("data.market_calendar.load_nse_holidays", lambda: {"2026-10-21"})
    actual = replay._simulate_cached(processor, *args)
    assert len(calls) == 2
    assert actual == original(*args)


def test_broadcast_encodes_once_for_clients_sharing_snapshot(monkeypatch):
    import dashboard.server as server
    class Socket:
        def __init__(self): self.messages = []
        async def send_text(self, text): self.messages.append(json.loads(text))
    clients = [Socket(), Socket()]
    monkeypatch.setattr(server, "active_connections", set(clients))
    monkeypatch.setattr(server.dashboard_state, "last_sent_states", {})
    original = server._frame_bytes
    encodes = []
    def counted(value):
        encodes.append(value["type"])
        return original(value)
    monkeypatch.setattr(server, "_frame_bytes", counted)
    async def exercise():
        await server._deliver_broadcast({"mode": "REAL", "scan_count": 1})
        assert encodes == ["full_update"]
        encodes.clear()
        await server._deliver_broadcast({"mode": "REAL", "scan_count": 2})
        assert encodes == ["full_update", "delta_update"]
    asyncio.run(exercise())
    assert clients[0].messages == clients[1].messages


def test_broadcast_serializes_trade_row_only_once(monkeypatch):
    import dashboard.server as server
    monkeypatch.setattr(server, "active_connections", set())
    original = server.make_serializable
    visits = []
    def counted(value):
        if isinstance(value, dict) and value.get("id") == "one-row":
            visits.append(True)
        return original(value)
    monkeypatch.setattr(server, "make_serializable", counted)
    asyncio.run(server._deliver_broadcast({"mode": "REAL", "trades": {
        "open": [{"id": "one-row", "price": float("nan")}], "closed": [], "signals": [], "summary": {}}}))
    assert visits == [True]
    assert server._latest_full_trades_payload["open"][0]["price"] == 0
