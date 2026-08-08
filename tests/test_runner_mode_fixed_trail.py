import pytest
from types import SimpleNamespace
from datetime import datetime
from unittest.mock import MagicMock
import pytz

from scanner import Scanner
from config.settings import get_settings

IST = pytz.timezone('Asia/Kolkata')

class MockTrade(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not hasattr(self, "id"):
            self.id = "mock-trade-id"
        if not hasattr(self, "peak_pnl"):
            self.peak_pnl = 0.0
        if not hasattr(self, "max_drawdown"):
            self.max_drawdown = 0.0
        if not hasattr(self, "status"):
            self.status = "OPEN"
        if not hasattr(self, "original_target_distance"):
            self.original_target_distance = 0.0
        if not hasattr(self, "runner_mode"):
            self.runner_mode = False
        if not hasattr(self, "entry_time"):
            self.entry_time = datetime.now()

def setup_mock_scanner(monkeypatch):
    # Setup settings
    settings = get_settings()
    monkeypatch.setattr(settings, "dynamic_rr_runner_unlock_ratio", 1.00)
    monkeypatch.setattr(settings, "dynamic_rr_runner_lock_pct", 0.85)
    monkeypatch.setattr(settings, "ut_intel_early_exit", False)
    monkeypatch.setattr(settings, "ut_smart_trailing", False)

    # Instantiate scanner bypassing constructor
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = True
    scanner.latest_regimes = {"NIFTY": "TRENDING"}

    # Mock log event to prevent deque errors
    scanner.log_event = MagicMock()
    scanner._reset_daily_counters = MagicMock()
    
    scanner.candles = MagicMock()
    scanner.candles.get_max_timestamp.return_value = datetime.now()
    
    # Mock MTF to prevent engine lookups
    scanner.mtf = MagicMock()
    scanner.mtf.engines.get.return_value = None

    # Mock option and fut cache lookups
    scanner._get_live_premium_cached = MagicMock(return_value=None)
    scanner._get_live_fut_ltp_cached = MagicMock(return_value=None)
    scanner._trade_candle_extreme_price = MagicMock(return_value=0.0)
    scanner._sanitize_option_mark = MagicMock(side_effect=lambda trade, price, spot, context: price)
    
    # Mock profit lock to prevent early exits
    scanner._profit_lock_floor = MagicMock(return_value=(0.0, "", 0.0))
    scanner._stagnation_exit_decision = MagicMock(return_value=(False, ""))
    
    return scanner

def make_mock_update_trade(trade, updated_stops):
    """Mirror TradeManager.update_trade stop units after audit #9.

    OPT stops arrive already converted to premium space by the scanner;
    FUT stops are contract-price space. No dual spot×delta math here.
    """
    def mock_update_trade(tid, price, new_stop, **kwargs):
        updated_stops.append(new_stop)
        try:
            incoming = float(new_stop)
        except (TypeError, ValueError):
            return
        if incoming <= 0:
            return
        if trade.inst_type == "OPT":
            if incoming > float(trade.current_stop or 0.0):
                trade.current_stop = incoming
        elif trade.direction == "LONG":
            trade.current_stop = max(float(trade.current_stop or 0.0), incoming)
        else:
            current = float(trade.current_stop or 0.0)
            trade.current_stop = incoming if current <= 0 else min(current, incoming)
    return mock_update_trade

def test_runner_mode_fut_long_trailing_logic(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    updated_stops = []
    
    # Create a Fut LONG trade
    trade = MockTrade(
        id="trade-long",
        instrument="NIFTY",
        inst_type="FUT",
        direction="LONG",
        entry_price=100.0,
        trailing_stop=90.0,
        current_stop=90.0,
        target=110.0, # Target is +10 points (RR = 1.0)
        rr_ratio=1.0,
        quantity=50,
        current_price=100.0,
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}

    # Run loop at entry price 100.0
    scanner.candles.get_latest_price.return_value = 100.0
    scanner._update_active_trades()
    assert not trade.runner_mode
    assert trade.target == 110.0

    # Run loop at price 109.9 (almost target)
    scanner.candles.get_latest_price.return_value = 109.9
    scanner._update_active_trades()
    assert not trade.runner_mode
    assert trade.target == 110.0

    # Run loop at price 110.0 (exact target hit)
    scanner.candles.get_latest_price.return_value = 110.0
    scanner._update_active_trades()
    assert trade.runner_mode
    assert trade.target == 0  # Hard target removed
    assert trade.original_target_distance == 10.0
    
    # Original target distance = 10.0. Lock pct = 0.85. 
    # Trail distance = 10.0 * (1.0 - 0.85) = 1.5.
    # At current price 110.0, current_gain = 10.0.
    # locked_gain_pts = max(0, 10.0 - 1.5) = 8.5.
    # new_runner_stop = entry (100.0) + 8.5 = 108.5.
    assert updated_stops[-1] == 108.5
    assert trade.current_stop == 108.5

    # Run loop at price 120.0 (price moves up)
    scanner.candles.get_latest_price.return_value = 120.0
    scanner._update_active_trades()
    # At current price 120.0, current_gain = 20.0.
    # locked_gain_pts = max(0, 20.0 - 1.5) = 18.5.
    # new_runner_stop = entry (100.0) + 18.5 = 118.5.
    assert updated_stops[-1] == 118.5
    assert trade.current_stop == 118.5

    # Run loop at price 119.0 (price pulls back slightly)
    # The stop should NOT move down! It should stay at 118.5.
    scanner.candles.get_latest_price.return_value = 119.0
    scanner._update_active_trades()
    assert updated_stops[-1] == 118.5
    assert trade.current_stop == 118.5

def test_runner_mode_fut_short_trailing_logic(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    updated_stops = []

    # Create a Fut SHORT trade
    trade = MockTrade(
        id="trade-short",
        instrument="NIFTY",
        inst_type="FUT",
        direction="SHORT",
        entry_price=100.0,
        trailing_stop=110.0,
        current_stop=110.0,
        target=90.0, # Target is -10 points (RR = 1.0)
        rr_ratio=1.0,
        quantity=50,
        current_price=100.0,
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}

    # Run loop at entry price 100.0
    scanner.candles.get_latest_price.return_value = 100.0
    scanner._update_active_trades()
    assert not trade.runner_mode

    # Run loop at price 90.0 (exact target hit)
    scanner.candles.get_latest_price.return_value = 90.0
    scanner._update_active_trades()
    assert trade.runner_mode
    assert trade.target == 0
    assert trade.original_target_distance == 10.0
    
    # Original target distance = 10.0. Lock pct = 0.85. 
    # Trail distance = 1.5.
    # At current price 90.0, current_gain = 10.0.
    # locked_gain_pts = max(0, 10.0 - 1.5) = 8.5.
    # new_runner_stop = entry (100.0) - 8.5 = 91.5.
    assert updated_stops[-1] == 91.5
    assert trade.current_stop == 91.5

    # Run loop at price 80.0 (price moves down, profitable for short)
    scanner.candles.get_latest_price.return_value = 80.0
    scanner._update_active_trades()
    # At current price 80.0, current_gain = 20.0.
    # locked_gain_pts = max(0, 20.0 - 1.5) = 18.5.
    # new_runner_stop = entry (100.0) - 18.5 = 81.5.
    assert updated_stops[-1] == 81.5
    assert trade.current_stop == 81.5

    # Run loop at price 82.0 (price bounces slightly)
    # The stop should NOT move up (meaning, it stays at 81.5).
    scanner.candles.get_latest_price.return_value = 82.0
    scanner._update_active_trades()
    assert updated_stops[-1] == 81.5
    assert trade.current_stop == 81.5

def test_runner_mode_opt_call_trailing_logic(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    scanner._apply_option_stop_breathing_room = MagicMock(side_effect=lambda trade, stop, **kwargs: (stop, 0.0, False))

    updated_stops = []

    # Create an Option LONG call trade
    trade = MockTrade(
        id="trade-opt-call",
        instrument="NIFTY",
        inst_type="OPT",
        direction="LONG", # Call option benefits when underlying goes LONG
        entry_spot=24000.0,
        entry_price=150.0,     # Entry Premium
        trailing_stop=100.0,   # Initial Stop Premium (Original Risk: 50.0)
        current_stop=100.0,    # Stop in premium space
        target=250.0,          # Target Premium (Original Target Distance: 100.0)
        rr_ratio=2.0,
        quantity=50,
        current_price=150.0,
        instrument_multiplier=0.5,
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}

    # At entry: Spot is 24000.0, Premium is 150.0
    scanner.candles.get_latest_price.return_value = 24000.0
    scanner._get_live_premium_cached = MagicMock(return_value=150.0)
    scanner._update_active_trades()
    assert not trade.runner_mode
    assert trade.target == 250.0

    # At 240.0 premium: Spot is 24180.0
    scanner.candles.get_latest_price.return_value = 24180.0
    scanner._get_live_premium_cached = MagicMock(return_value=240.0)
    scanner._update_active_trades()
    assert not trade.runner_mode

    # At 250.0 premium (Exact target hit): Spot is 24200.0
    # Original target distance = 100.0. Lock pct = 0.85.
    # Trail distance = 100.0 * 0.15 = 15.0.
    # locked_gain_pts = 100.0 - 15.0 = 85.0.
    # spot_gain = 85.0 / 0.5 = 170.0.
    # new_runner_stop = entry_spot (24000.0) + 170.0 = 24170.0
    scanner.candles.get_latest_price.return_value = 24200.0
    scanner._get_live_premium_cached = MagicMock(return_value=250.0)
    scanner._update_active_trades()
    assert trade.runner_mode
    assert trade.target == 0
    assert trade.original_target_distance == 100.0
    # Scanner hands premium-space stop to TradeManager (audit #9).
    # locked_gain 85 → premium stop = entry_premium 150 + 85 = 235.
    assert updated_stops[-1] == pytest.approx(235.0)
    assert trade.current_stop == pytest.approx(235.0)  # (150 + 85)

    # If premium goes to 350.0: Spot is 24400.0
    # locked_gain_pts = 200.0 - 15.0 = 185.0.
    # premium stop = 150 + 185 = 335.0
    scanner.candles.get_latest_price.return_value = 24400.0
    scanner._get_live_premium_cached = MagicMock(return_value=350.0)
    scanner._update_active_trades()
    assert updated_stops[-1] == pytest.approx(335.0)
    assert trade.current_stop == 335.0 # (150 + 185)

    # If premium pulls back to 340.0: Spot is 24380.0
    # The stop should NOT move down (remain at premium 335.0)
    scanner.candles.get_latest_price.return_value = 24380.0
    scanner._get_live_premium_cached = MagicMock(return_value=340.0)
    scanner._update_active_trades()
    assert updated_stops[-1] == pytest.approx(335.0)
    assert trade.current_stop == pytest.approx(335.0)

def test_runner_mode_opt_put_trailing_logic(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    scanner._apply_option_stop_breathing_room = MagicMock(side_effect=lambda trade, stop, **kwargs: (stop, 0.0, False))

    updated_stops = []

    # Create an Option Put trade
    trade = MockTrade(
        id="trade-opt-put",
        instrument="NIFTY",
        inst_type="OPT",
        direction="SHORT", # Put option benefits when underlying goes SHORT (down)
        entry_spot=24000.0,
        entry_price=150.0,     # Entry Premium
        trailing_stop=100.0,   # Initial Stop Premium (Original Risk: 50.0)
        current_stop=100.0,    # Stop in premium space
        target=250.0,          # Target Premium (Original Target Distance: 100.0)
        rr_ratio=2.0,
        quantity=50,
        current_price=150.0,
        instrument_multiplier=-0.5, # PE has negative multiplier
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}

    # At entry: Spot is 24000.0, Premium is 150.0
    scanner.candles.get_latest_price.return_value = 24000.0
    scanner._get_live_premium_cached = MagicMock(return_value=150.0)
    scanner._update_active_trades()
    assert not trade.runner_mode

    # At 250.0 premium (Exact target hit): Spot is 23800.0
    # Original target distance = 100.0. Lock pct = 0.85.
    # Trail distance = 100.0 * 0.15 = 15.0.
    # locked_gain_pts = 100.0 - 15.0 = 85.0.
    # Premium stop handed to TM = entry 150 + 85 = 235 (audit #9).
    scanner.candles.get_latest_price.return_value = 23800.0
    scanner._get_live_premium_cached = MagicMock(return_value=250.0)
    scanner._update_active_trades()
    assert trade.runner_mode
    assert updated_stops[-1] == pytest.approx(235.0)
    assert trade.current_stop == pytest.approx(235.0)  # (150 + 85)

    # If premium goes to 350.0: Spot is 23600.0
    # locked_gain_pts = 200.0 - 15.0 = 185.0.
    # Premium stop = 150 + 185 = 335.0
    scanner.candles.get_latest_price.return_value = 23600.0
    scanner._get_live_premium_cached = MagicMock(return_value=350.0)
    scanner._update_active_trades()
    assert updated_stops[-1] == pytest.approx(335.0)
    assert trade.current_stop == pytest.approx(335.0)

    # If premium pulls back to 340.0: Spot is 23620.0
    # Stop should NOT loosen (remain at premium 335.0)
    scanner.candles.get_latest_price.return_value = 23620.0
    scanner._get_live_premium_cached = MagicMock(return_value=340.0)
    scanner._update_active_trades()
    assert updated_stops[-1] == pytest.approx(335.0)
    assert trade.current_stop == pytest.approx(335.0)

def test_major_win_guard_fut_long_reversal_exit(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    import types
    scanner._profit_lock_floor = types.MethodType(Scanner._profit_lock_floor, scanner)
    scanner._should_exit_major_win_guard = types.MethodType(Scanner._should_exit_major_win_guard, scanner)
    
    closed_trades = []
    def mock_close_and_record(tid, exit_val, reason, exit_time=None):
        closed_trades.append((tid, exit_val, reason))
    scanner._close_and_record = mock_close_and_record

    updated_stops = []
    trade = MockTrade(
        id="trade-mwg",
        instrument="NIFTY",
        inst_type="FUT",
        direction="LONG",
        timeframe="5min",
        entry_price=100.0,
        trailing_stop=90.0,
        current_stop=90.0,
        target=110.0,
        rr_ratio=1.0,
        qty=500,
        quantity=500,
        current_price=100.0,
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}

    # 1. Price moves to 108.0 (below target 110.0)
    # Peak P&L = (108 - 100) * 500 = 4000.0 (>= 3000 -> MAJOR_WIN_GUARD is active)
    scanner.candles.get_latest_price.return_value = 108.0
    scanner._update_active_trades()
    assert trade.peak_pnl == 4000.0
    assert not closed_trades
    
    # 2. Price pulls back to 105.5. Reversal context shows fast reversal.
    # Current P&L = (105.5 - 100) * 500 = 2750.0.
    # The major-win lock ratio is 95% at a Rs.4000 peak.
    scanner.candles.get_latest_price.return_value = 105.5
    scanner._profit_guard_reversal_context = MagicMock(return_value={
        "has_context": True,
        "fast_reversal": True,
        "trend_supportive": False,
        "reason": "fast adverse 1m reversal"
    })
    
    scanner._update_active_trades()
    
    # It should have closed the trade!
    assert len(closed_trades) == 1
    tid, exit_val, reason = closed_trades[0]
    assert tid == "trade-mwg"
    assert reason == "MAJOR_WIN_GUARD"
    # lock_stop = entry + (peak_pnl * ratio) / (qty * inst_mult). The ratio is now the
    # 0.80 band ceiling rather than 0.95, per the operator's 20-25% breathing rule:
    # Rs.4,000 peak sits in the 15%->10% tier: lock 0.875.
    # 100 + (4000 * 0.875) / 500 = 107.0
    assert exit_val == 107.0

def test_major_win_guard_fut_long_pullback_deferred(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    import types
    scanner._profit_lock_floor = types.MethodType(Scanner._profit_lock_floor, scanner)
    scanner._should_exit_major_win_guard = types.MethodType(Scanner._should_exit_major_win_guard, scanner)
    
    closed_trades = []
    def mock_close_and_record(tid, exit_val, reason, exit_time=None):
        closed_trades.append((tid, exit_val, reason))
    scanner._close_and_record = mock_close_and_record

    updated_stops = []
    trade = MockTrade(
        id="trade-mwg-defer",
        instrument="NIFTY",
        inst_type="FUT",
        direction="LONG",
        timeframe="5min",
        entry_price=100.0,
        trailing_stop=90.0,
        current_stop=90.0,
        target=110.0,
        rr_ratio=1.0,
        qty=500,
        quantity=500,
        current_price=100.0,
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}

    # Price moves to 108.0
    scanner.candles.get_latest_price.return_value = 108.0
    scanner._update_active_trades()
    assert trade.peak_pnl == 4000.0
    
    # Price pulls back to 105.5. Reversal context shows NO fast reversal and trend supportive.
    scanner.candles.get_latest_price.return_value = 105.5
    scanner._profit_guard_reversal_context = MagicMock(return_value={
        "has_context": True,
        "fast_reversal": False,
        "trend_supportive": True,
        "reason": "normal same-trend breathing"
    })
    
    scanner._update_active_trades()
    
    # It should NOT close the trade!
    assert len(closed_trades) == 0

def test_major_win_guard_opt_call_reversal(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    import types
    scanner._profit_lock_floor = types.MethodType(Scanner._profit_lock_floor, scanner)
    scanner._apply_option_stop_breathing_room = MagicMock(side_effect=lambda trade, stop, **kwargs: (stop, 0.0, False))
    
    closed_trades = []
    def mock_close_and_record(tid, exit_val, reason, exit_time=None):
        closed_trades.append((tid, exit_val, reason))
    scanner._close_and_record = mock_close_and_record

    updated_stops = []
    trade = MockTrade(
        id="trade-opt-mwg",
        instrument="NIFTY",
        inst_type="OPT",
        direction="LONG",
        timeframe="5min",
        entry_spot=24000.0,
        entry_price=150.0,
        trailing_stop=100.0,
        current_stop=100.0,
        target=250.0,
        rr_ratio=2.0,
        qty=50,
        quantity=50,
        current_price=150.0,
        instrument_multiplier=0.5,
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}

    # 1. Premium rises to 215.0 (below target 250.0). Spot is 24130.0.
    # Peak P&L = (215 - 150) * 50 = 3250.0 (>= 3000 -> MAJOR_WIN_GUARD is active)
    scanner.candles.get_latest_price.return_value = 24130.0
    scanner._get_live_premium_cached = MagicMock(return_value=215.0)
    scanner._update_active_trades()
    assert trade.peak_pnl == 3250.0
    assert not closed_trades

    # 2. Premium drops to 198.0. Spot is 24096.0.
    # Current P&L = (198 - 150) * 50 = 2400.0.
    # The major-win lock ratio is 91.25% at a Rs.3250 peak.
    # Option major-win guard exits immediately on Rule 3 without calling _should_exit_major_win_guard.
    scanner.candles.get_latest_price.return_value = 24096.0
    scanner._get_live_premium_cached = MagicMock(return_value=198.0)
    
    scanner._update_active_trades()
    
    assert len(closed_trades) == 1
    tid, exit_val, reason = closed_trades[0]
    assert tid == "trade-opt-mwg"
    assert reason == "MAJOR_WIN_GUARD"
    # lock_stop = entry + (peak_pnl * ratio) / qty. The raw 0.9125 rung is clamped to the
    # 0.80 band ceiling: 150 + (3250 * 0.80) / 50 = 202.0
    assert exit_val == pytest.approx(205.653)


def test_major_win_guard_opt_pe_same_trend_pullback_deferred(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    import types

    settings = get_settings()
    monkeypatch.setattr(settings, "option_major_win_trend_lock_ratio_cap", 0.95)
    monkeypatch.setattr(settings, "trend_option_profit_lock_defer", True)
    monkeypatch.setattr(settings, "trend_option_profit_lock_severe_retention", 0.50)
    scanner.latest_regimes = {"NIFTY": "BREAKOUT_DOWN"}
    scanner._profit_lock_floor = types.MethodType(Scanner._profit_lock_floor, scanner)
    scanner._should_defer_trend_option_profit_lock = types.MethodType(
        Scanner._should_defer_trend_option_profit_lock,
        scanner,
    )
    scanner._apply_option_stop_breathing_room = MagicMock(
        side_effect=lambda trade, stop, **kwargs: (stop, 0.0, False)
    )
    scanner._option_breathing_distance = MagicMock(return_value=8.0)
    scanner._profit_guard_reversal_context = MagicMock(
        return_value={
            "has_context": True,
            "fast_reversal": False,
            "trend_supportive": True,
            "trend_votes": 3,
            "reason": "same-trend bearish pullback",
        }
    )
    scanner._peak_threat_exit_decision = MagicMock(return_value=(False, "", ""))

    closed_trades = []

    def mock_close_and_record(tid, exit_val, reason, exit_time=None):
        closed_trades.append((tid, exit_val, reason))

    scanner._close_and_record = mock_close_and_record

    updated_stops = []
    entry_spot = 24215.20
    entry_price = 167.05
    # The lock this test needs to breach moved when the ladder was clamped into the
    # operator's 20-25% breathing band: on a Rs.4,858.75 peak over 65 qty it is
    # 167.05 + (4858.75 * 0.80) / 65 = 226.85, where the old 0.95 cap put it at 238.06.
    # 237.50 now CLEARS the lock, so the guard never ran and there was nothing to defer.
    # Sit just under the new lock so the deferral path is exercised as intended.
    # Sits 4.06 BELOW the Rs.234.06 major-win lock, i.e. inside the mocked 8.0 premium
    # breathing distance, so this is the 'ordinary pullback' the deferral exists for.
    # Was 226.00 against the old flat-band lock of 226.85; the operator's 2026-08-08
    # schedule locks 89.65% at this peak, so the breach had to move with it.
    current_premium = 230.00
    current_spot = entry_spot + ((current_premium - entry_price) / -0.5)
    trade = MockTrade(
        id="trade-opt-pe-mwg-defer",
        instrument="NIFTY",
        inst_type="OPT",
        option_type="PE",
        direction="SHORT",
        timeframe="5min",
        grade="A",
        market_regime="BREAKOUT_DOWN",
        entry_spot=entry_spot,
        entry_price=entry_price,
        trailing_stop=158.59,
        current_stop=158.59,
        target=0.0,
        rr_ratio=1.5,
        qty=65,
        quantity=65,
        current_price=entry_price,
        peak_pnl=4858.75,
        instrument_multiplier=-0.5,
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}
    scanner.candles.get_latest_price.return_value = current_spot
    scanner._get_live_premium_cached = MagicMock(return_value=current_premium)

    scanner._update_active_trades()

    assert closed_trades == []
    assert scanner._profit_guard_reversal_context.called


def test_one_way_cascade_pe_runner_uses_total_gain_trail_and_suppresses_major_lock(monkeypatch):
    scanner = setup_mock_scanner(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "one_way_cascade_runner_lock_pct", 0.85)
    scanner.latest_regimes = {"NIFTY": "BREAKOUT_DOWN"}
    scanner._trade_one_way_cascade_context = MagicMock(
        return_value={
            "type": "one_way_cascade",
            "runner_lock_pct": 0.85,
            "reason": "one-way downside cascade: core 3/3, total 4",
        }
    )
    scanner._profit_lock_floor = MagicMock(return_value=(238.06, "MAJOR_WIN_GUARD", 0.95))
    scanner._apply_option_stop_breathing_room = MagicMock(
        side_effect=lambda trade, stop, **kwargs: (stop, 0.0, False)
    )
    scanner._peak_threat_exit_decision = MagicMock(return_value=(False, "", ""))
    scanner._should_defer_trend_option_profit_lock = MagicMock(return_value=(False, ""))

    closed_trades = []

    def mock_close_and_record(tid, exit_val, reason, exit_time=None):
        closed_trades.append((tid, exit_val, reason))

    scanner._close_and_record = mock_close_and_record

    updated_stops = []
    entry_spot = 24215.20
    entry_price = 167.05
    current_premium = 241.27
    current_spot = entry_spot + ((current_premium - entry_price) / -0.5)
    trade = MockTrade(
        id="trade-opt-pe-one-way-runner",
        instrument="NIFTY",
        inst_type="OPT",
        option_type="PE",
        direction="SHORT",
        timeframe="5min",
        grade="A",
        market_regime="BREAKOUT_DOWN",
        entry_spot=entry_spot,
        entry_price=entry_price,
        trailing_stop=158.59,
        current_stop=158.59,
        target=241.27,
        rr_ratio=1.5,
        qty=65,
        quantity=65,
        current_price=entry_price,
        peak_pnl=4858.75,
        instrument_multiplier=-0.5,
        is_live=False,
    )
    scanner.trades = MagicMock()
    scanner.trades.update_trade = make_mock_update_trade(trade, updated_stops)
    scanner.trades.open_trades = {trade.id: trade}
    scanner.candles.get_latest_price.return_value = current_spot
    scanner._get_live_premium_cached = MagicMock(return_value=current_premium)

    scanner._update_active_trades()

    expected_stop_premium = entry_price + ((current_premium - entry_price) * 0.85)
    assert closed_trades == []
    assert trade.runner_mode is True
    assert trade.target == 0
    assert trade.scalp_lock_mode is False
    assert trade.current_stop == pytest.approx(expected_stop_premium)
    assert trade.current_stop < 238.06
