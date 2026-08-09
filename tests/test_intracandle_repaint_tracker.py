from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from scanner import Scanner
from trading.trade_manager import IST


@pytest.mark.asyncio
async def test_broker_repaint_tracker_checks_signal_at_candle_close(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(scanner_module.asyncio, "sleep", no_wait)
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        signal_timestamp=datetime.now(IST) - timedelta(minutes=6),
    )
    trade = SimpleNamespace(
        id="T1",
        status="OPEN",
        entry_price=100.0,
        current_price=101.0,
    )
    closed = []
    dummy = SimpleNamespace(
        # Tri-state now: the tracker asks WHY the arrow is missing, so "cannot tell"
        # (an empty scan cache) can no longer masquerade as "vanished".
        _repaint_signal_status=lambda _candidate, _timestamp: "PRESENT",
        candles=SimpleNamespace(get_latest_price=lambda _instrument: 101.0),
        _close_and_record=lambda *args, **_kwargs: closed.append(args),
        log_event=lambda *_args: None,
    )

    await Scanner._track_repaint_until_candle_close(dummy, trade, candidate)

    assert trade.repaint_guard_checked is True
    assert closed == []


@pytest.mark.asyncio
async def test_broker_repaint_tracker_aborts_at_final_check_if_signal_vanished(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(scanner_module.asyncio, "sleep", no_wait)
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        signal_timestamp=datetime.now(IST) - timedelta(minutes=6),
    )
    trade = SimpleNamespace(
        id="T2",
        status="OPEN",
        entry_price=100.0,
        current_price=99.0,
    )
    closed = []
    dummy = SimpleNamespace(
        _repaint_signal_status=lambda _candidate, _timestamp: "VANISHED",
        candles=SimpleNamespace(get_latest_price=lambda _instrument: 99.0),
        _close_and_record=lambda *args, **_kwargs: closed.append(args),
        log_event=lambda *_args: None,
    )

    await Scanner._track_repaint_until_candle_close(dummy, trade, candidate)

    assert closed == [("T2", 99.0, "REPAINT_ABORT")]
