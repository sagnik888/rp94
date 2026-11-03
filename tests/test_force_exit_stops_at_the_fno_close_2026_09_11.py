"""The force exit must stop sending orders once the F&O segment has closed.

check_session_end had a lower bound (ut_force_exit_time) and a calendar gate, but no
upper bound. From the force-exit time onward it re-ran on every ~1.5s scan cycle for as
long as a position stayed open, and the scanner does not idle while trades are open. A
position that survived the square-off (broker reject, RMS refusal, IP block) was
therefore re-sent to close_trade / emergency_flatten_trade every cycle for the rest of
the evening, after the exchange had stopped accepting F&O orders. A rejected exit
clears pending_exit, so each cycle sent a fresh order that could only be rejected.

Since SEBI's Closing Auction Session (2026-08-03) the equity derivatives segment trades
until 15:40 (SEBI circular 16-Jan-2026 para 4.2.3; NSE/FAOP/74467), so 15:30-15:40 is
still a valid exit window and must keep working. After 15:40 the position is real and
still needs closing, so this escalates by name, once per day, instead of going quiet.
"""
import threading
from datetime import datetime

from trading.trade_manager import TradeManager

SYMBOL = "NIFTY2691523400CE"


class _Broker:
    def __init__(self):
        self.exits = []

    def close_order(self, symbol, token, qty, side, product_type="CARRYFORWARD", **kw):
        self.exits.append((symbol, qty, side))
        return "EXIT-1"


class _Provider:
    def is_market_holiday(self, dt=None):
        return False

    def is_market_special_session(self, dt=None):
        return False


class _S:
    ut_force_exit_time = "15:15"
    fno_market_close_time = "15:40"
    force_exit_flattens_orphans = False


class _FlattenOrphans(_S):
    force_exit_flattens_orphans = True


def _trade(execution_status="OPEN"):
    t = type("T", (), {})()
    t.id = "T1"
    t.trading_symbol = SYMBOL
    t.symbol_token = "1"
    t.status = "OPEN"
    t.execution_status = execution_status
    t.current_price = 130.0
    t.protective_order_id = ""
    return t


def _manager(mode="REAL", execution_status="OPEN", orphans=None):
    m = TradeManager.__new__(TradeManager)
    m.broker = _Broker()
    m.mode = mode
    m.product_type = "CARRYFORWARD"
    m._lock = threading.RLock()
    m.open_trades = {"T1": _trade(execution_status)} if execution_status else {}
    m.closed_trades = []
    m.orphan_positions = dict(orphans or {})
    m.data = _Provider()
    m.save_state = lambda: None
    m.logged = []
    m._log_ui = lambda msg, level="info": m.logged.append(msg)
    m.closed_ids = []
    m.flattened_ids = []
    m.close_trade = lambda tid, px, reason, exit_time=None: m.closed_ids.append(tid)
    m.emergency_flatten_trade = lambda trade, reason: m.flattened_ids.append(trade.id)
    return m


def _run(manager, when, settings=None):
    import config.settings as cs

    original = cs.get_settings
    cs.get_settings = lambda: (settings or _S())
    try:
        manager.check_session_end(when)
    finally:
        cs.get_settings = original


# 2026-09-10 is a Thursday, a normal trading day.
AT_1515 = datetime(2026, 9, 10, 15, 15)
AT_1539 = datetime(2026, 9, 10, 15, 39, 59)
AT_1540 = datetime(2026, 9, 10, 15, 40)
AT_1745 = datetime(2026, 9, 10, 17, 45)


def test_the_force_exit_still_fires_at_the_force_exit_time():
    m = _manager()
    _run(m, AT_1515)
    assert m.closed_ids == ["T1"]


def test_the_force_exit_still_fires_until_the_fno_close():
    """15:30-15:40 is a valid F&O exit window since CAS; it must not be cut short."""
    m = _manager()
    _run(m, AT_1539)
    assert m.closed_ids == ["T1"], "a retry inside the 15:30-15:40 F&O window was suppressed"


def test_no_exit_is_sent_at_the_fno_close():
    m = _manager()
    _run(m, AT_1540)
    assert m.closed_ids == [], "sent an exit at the F&O close"


def test_no_exit_is_sent_in_the_evening():
    """The live defect: an exit per scan cycle for the rest of the evening."""
    m = _manager()
    for _ in range(5):
        _run(m, AT_1745)
    assert m.closed_ids == [], f"sent {len(m.closed_ids)} exits after the F&O close"
    assert m.broker.exits == []


def test_a_recovery_row_is_not_flattened_after_the_close():
    m = _manager(execution_status="RECOVERY_REQUIRED")
    _run(m, AT_1745)
    assert m.flattened_ids == [], "emergency flatten fired into a closed segment"


def test_an_orphan_is_not_swept_after_the_close():
    m = _manager(execution_status=None, orphans={SYMBOL: {"netqty": 75, "symboltoken": "1"}})
    _run(m, AT_1745, _FlattenOrphans())
    assert m.broker.exits == [], "orphan square-off sent into a closed segment"
    assert any(SYMBOL in msg for msg in m.logged), "orphan carried past the close in silence"


def test_the_stuck_position_is_escalated_by_name_once_per_day():
    m = _manager()
    for _ in range(6):
        _run(m, AT_1745)
    assert len(m.logged) == 1, f"escalated {len(m.logged)} times in one evening"
    assert SYMBOL in m.logged[0]
    assert "15:40" in m.logged[0]


def test_historical_mode_still_closes_locally_after_the_close():
    """No order is sent outside REAL mode, so there is nothing to suppress."""
    m = _manager(mode="HISTORICAL")
    _run(m, AT_1745)
    assert m.closed_ids == ["T1"]


def test_the_close_time_defaults_to_the_post_cas_fno_close():
    from config.settings import Settings

    assert Settings.model_fields["fno_market_close_time"].default == "15:40"


# The scanner calls check_expiry_gamma_time_exit immediately before check_session_end,
# every cycle, with the same missing upper bound. Guarding only check_session_end left an
# expiry-day position stuck past 15:40 re-sent through this path instead.
class _Expiry(_S):
    expiry_day_trade_current_week = True
    expiry_day_gamma_time_exit = "13:30"
    expiry_day_late_window_time_exit = "15:10"
    expiry_day_gamma_cutoff = "13:00"


def _expiry_manager(monkeypatch, mode="REAL"):
    import engine.expiry_manager as em

    monkeypatch.setattr(em.expiry_manager, "is_expiry_day", lambda *a, **k: True)
    m = _manager(mode=mode)
    trade = m.open_trades["T1"]
    trade.inst_type = "OPT"
    trade.instrument = "NIFTY"
    trade.entry_time = datetime(2026, 9, 10, 10, 5)
    return m


def _run_gamma(manager, when):
    import config.settings as cs

    original = cs.get_settings
    cs.get_settings = lambda: _Expiry()
    try:
        manager.check_expiry_gamma_time_exit(when)
    finally:
        cs.get_settings = original


def test_the_expiry_gamma_exit_still_fires_on_time(monkeypatch):
    m = _expiry_manager(monkeypatch)
    _run_gamma(m, datetime(2026, 9, 10, 13, 30))
    assert m.closed_ids == ["T1"]


def test_the_expiry_gamma_exit_sends_nothing_after_the_fno_close(monkeypatch):
    m = _expiry_manager(monkeypatch)
    for _ in range(5):
        _run_gamma(m, AT_1745)
    assert m.closed_ids == [], f"expiry gamma path sent {len(m.closed_ids)} exits after the close"


def test_the_expiry_gamma_exit_still_closes_locally_in_historical_mode(monkeypatch):
    m = _expiry_manager(monkeypatch, mode="HISTORICAL")
    _run_gamma(m, AT_1745)
    assert m.closed_ids == ["T1"]
