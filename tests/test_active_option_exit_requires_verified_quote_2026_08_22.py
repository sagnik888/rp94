import threading
from datetime import datetime
from types import SimpleNamespace

from scanner import Scanner
from trading.trade_manager import Trade, TradeManager


def _option_trade():
    return Trade(
        id="T1",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime(2026, 8, 21, 10, 0),
        trailing_stop=90.0,
        current_stop=90.0,
        lots=1,
        lot_size=75,
        grade="A",
        inst_type="OPT",
        trading_symbol="NIFTY26AUG24200CE",
        entry_spot=24200.0,
        instrument_multiplier=0.5,
        current_price=100.0,
        peak_pnl=250.0,
    )


def test_unverified_synthetic_mark_cannot_close_or_ratchet_an_option():
    trade = _option_trade()
    manager = TradeManager.__new__(TradeManager)
    manager.open_trades = {trade.id: trade}
    manager.closed_trades = []
    manager._last_mark_to_market_save = 0.0
    manager._dirty = threading.Event()

    # Synthetic mark = 100 + (24000 - 24200)*0.5 = 0.05 floor, far below the
    # stop. It is display continuity only, never an executable stop trigger.
    manager.update_trade(
        trade.id,
        current_spot=24000.0,
        new_trailing_stop=110.0,
        current_time=datetime(2026, 8, 21, 10, 1),
        real_premium=None,
        quote_verified=False,
    )

    assert trade.id in manager.open_trades
    assert manager.closed_trades == []
    assert trade.current_stop == 90.0
    assert trade.peak_pnl == 250.0
    assert trade.quote_verified is False


def test_scanner_holds_elective_exit_when_active_option_quote_is_unverified():
    trade = _option_trade()
    trade.quote_verified = False
    closed = []
    logs = []
    scanner = Scanner.__new__(Scanner)
    scanner.trades = SimpleNamespace(open_trades={trade.id: trade})
    scanner._reset_daily_counters = lambda: None
    scanner.log_event = lambda message, level: logs.append((message, level))
    scanner._diag_inc = lambda *args: None
    scanner._spread_aware_exit_decision = lambda *args: (_ for _ in ()).throw(
        AssertionError("spread/close path must not run")
    )
    scanner._close_and_record_internal = lambda *args: closed.append(args)

    scanner._close_and_record(trade.id, 40.0, "SMART_PROFIT_LOCK")

    assert closed == []
    assert logs and "synthetic display marks cannot execute exits" in logs[0][0]


def test_protective_forced_or_operator_exits_bypass_missing_option_quote():
    assert Scanner._option_exit_allows_unverified_mark("FORCE_EOD_KILL") is True
    assert Scanner._option_exit_allows_unverified_mark("RECOVERY_FLATTEN") is True
    assert Scanner._option_exit_allows_unverified_mark("MANUAL_EXIT") is True
    assert Scanner._option_exit_allows_unverified_mark("TRAILING_STOP") is True
    assert Scanner._option_exit_allows_unverified_mark("STOP_HIT") is True
    assert Scanner._option_exit_allows_unverified_mark("SMART_PROFIT_LOCK") is False
