"""Parity checks for optimizations identified by the live restart profile."""
import copy
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest
from kiteconnect import KiteConnect

from data.kite_history_client import HistoryKiteConnect
from engine.alt_scanners import AltSignalEngine


@pytest.mark.parametrize("stamp", ["2026-11-04T09:15:00+0530", "2026-11-04T09:15:00+05:30",
                                   "2026-11-04T03:45:00Z", "November 4, 2026 09:15:00"])
@pytest.mark.parametrize("oi", [False, True])
def test_history_formatter_matches_sdk(stamp, oi):
    data = {"candles": [[stamp, 100., 102., 99., 101., 456] + ([789] if oi else [])]}
    original = copy.deepcopy(data)
    assert HistoryKiteConnect._format_historical(None, data) == KiteConnect._format_historical(None, data)
    assert data == original


def test_history_formatter_rejects_malformed_candle_like_sdk():
    for formatter in [HistoryKiteConnect._format_historical, KiteConnect._format_historical]:
        with pytest.raises(IndexError):
            formatter(None, {"candles": [["2026-11-04T09:15:00+0530", 1]]})


@pytest.mark.parametrize("evidence", [{}, {"broker_order_id": "broker-1"},
    {"broker_quantity": 50}, {"entry_filled_at": "2026-11-03T10:00:00"},
    {"protective_stop_order_id": "stop-1"}, {"data_provenance": "LIVE"}])
def test_replay_recovery_requires_explicit_provenance_and_no_broker_evidence(evidence):
    from trading.trade_manager import TradeManager
    row = {"id": "H_UTEMA_test", "data_provenance": "BACKTEST_OBSERVED_OPTION_MINUTES"}
    row.update(evidence)
    assert TradeManager._unfilled_replay_record(row) == (not evidence)


def test_replay_load_retains_audit_record_without_creating_active_position(monkeypatch):
    import threading
    import trading.trade_manager as module
    manager = module.TradeManager.__new__(module.TradeManager)
    manager.open_trades, manager.closed_trades, manager._trade_counter = {}, [], 0
    manager._lock = threading.RLock()
    row = {"id": "H_UTEMA_test", "data_provenance": "BACKTEST_OBSERVED_OPTION_MINUTES"}
    monkeypatch.setattr(module.db, "load_open_trades", lambda: [row])
    monkeypatch.setattr(module.db, "load_closed_trades", lambda: [])
    monkeypatch.setattr(manager, "_drop_delisted_open_records", lambda rows: rows)
    manager.load_state()
    assert manager.open_trades == {}
    assert manager.closed_trades == []
    assert manager._restored_replay_open_rows == {row["id"]: row}


@pytest.mark.parametrize("mode", ["quad_prime", "s1"])
def test_numeric_frame_optimization_preserves_engine_results(mode, monkeypatch):
    rng = np.random.default_rng(41)
    idx = pd.date_range("2026-11-03 09:15", periods=300, freq="min")
    c = 23000 + np.cumsum(rng.normal(0, 5, len(idx)))
    frame = pd.DataFrame(dict(open=c-1, high=c+6, low=c-7, close=c, volume=rng.integers(10, 100, len(idx))), index=idx)
    frame["price_source"] = pd.Series("kite", index=idx, dtype="string[pyarrow]")
    frame["volume_basis"] = pd.Series("constituent_shares", index=idx, dtype="string[pyarrow]")
    before = frame.copy(deep=True)
    optimized = AltSignalEngine(signal_source=mode, signal_mode="confirmed")
    original = AltSignalEngine(signal_source=mode, signal_mode="confirmed")
    monkeypatch.setattr(original, "_analysis_frame", lambda value: value)
    kw = dict(instrument="NIFTY", timeframe="1min", df_5min=frame, df_15min=frame)
    actual, expected = optimized.process(frame, **kw), original.process(frame, **kw)
    assert not actual.get("error"), actual.get("error")
    assert [asdict(s) for s in actual["signals"]] == [asdict(s) for s in expected["signals"]]
    for name in ("trailing_stop", "atr", "adx", "positions"):
        if name in expected:
            np.testing.assert_allclose(np.asarray(actual[name], dtype=float),
                                       np.asarray(expected[name], dtype=float), equal_nan=True)
    pd.testing.assert_frame_equal(frame, before)
