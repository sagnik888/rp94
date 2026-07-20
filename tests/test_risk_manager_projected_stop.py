from datetime import datetime
from types import SimpleNamespace

from engine.risk_manager import RiskManager


class _Trades:
    def __init__(self, open_trades=None):
        self.closed_trades = []
        self.open_trades = open_trades or {}
        self.closed = []

    def close_trade(self, trade_id, price, reason):
        self.closed.append((trade_id, price, reason))


def _open_trade(**overrides):
    values = {
        "entry_time": datetime.now(),
        "inst_type": "OPT",
        "entry_price": 100.0,
        "current_price": 100.0,
        "unrealized_pnl": 0.0,
        "lots": 20,
        "lot_size": 50,
        "instrument_multiplier": 1.0,
        "trailing_stop": None,
        "direction": "LONG",
        "is_ghost": False,
        "execution_status": "OPEN",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_projected_stop_risk_uses_tighter_live_stop_and_trade_cap():
    manager = RiskManager(_Trades(), 100000.0)
    manager.update_settings(options_sl_pct=15.0, max_trade_loss_abs=5000.0)

    assert manager._projected_stop_risk(_open_trade(lots=1, lot_size=100, trailing_stop=95.0)) == 500.0
    assert manager._projected_stop_risk(_open_trade(lots=10, lot_size=100, trailing_stop=None)) == 5000.0


def test_global_breaker_halts_on_projected_stop_loss_before_mark_to_market_loss():
    trade = _open_trade()
    trades = _Trades({"opt-1": trade})
    manager = RiskManager(trades, 100000.0)
    manager.update_settings(
        max_daily_loss_pct=10.0,
        capital_fut=50000.0,
        capital_opt=50000.0,
        capital_total=100000.0,
        options_sl_pct=15.0,
        max_trade_loss_abs=50000.0,
        circuit_breaker_slippage_bps=0.0,
    )

    assert manager.check_circuit_breaker() is True
    assert manager.daily_loss_breached is True
    assert trades.closed == [("opt-1", 100.0, "GLOBAL_DAILY_LOSS_LIMIT")]
