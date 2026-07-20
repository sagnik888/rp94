"""Fixes from the 2026-07-09 FUT-vs-OPT backtest audit.

Fix 1 — sweep-summary hygiene: the all-engines historical sweep replays the
same flip once per engine stream (84x2 + 28x3 + 5x4 duplicates = Rs.315k
phantom P&L in the 673-row run), and FUT_SHADOW companion rows were blended
into the headline FUT P&L. get_summary() must collapse exact duplicates and
segregate the shadow book; both are no-ops on a live session.

Fix 2 — FUT replay exit parity: shadow FUT trades computed a target but never
simulated it — they rode to the option trade's exit (zero TARGET_HIT among 334
FUT rows). The shadow builder now walks index candles with the same intrabar
SL/target tie-break the option replay gets.
"""
from datetime import datetime

import pandas as pd

from engine.signal_processor import SignalProcessor
from trading.trade_manager import IST, Trade, TradeManager


def _trade(tid, pnl, inst_type="FUT", symbol="BANKNIFTY-FUT", entry=None, **kw):
    return Trade(
        id=tid, instrument="BANKNIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, entry_time=entry or IST.localize(datetime(2026, 7, 8, 13, 35)),
        trailing_stop=99.0, current_stop=99.0, lots=1, lot_size=35, grade="A",
        inst_type=inst_type, trading_symbol=symbol, pnl=pnl, status="CLOSED", **kw,
    )


# ── Fix 1 ────────────────────────────────────────────────────────────────────

def test_summary_collapses_engine_replay_duplicates():
    tm = TradeManager(broker=None, max_positions=5)
    # same flip replayed by 3 engine streams -> identical (inst, tf, dir, pnl, ts)
    dupes = [_trade(f"H_BN_5m_{i}", 20995.0) for i in range(3)]
    unique = [_trade("H_BN_5m_other", 5000.0, entry=IST.localize(datetime(2026, 7, 8, 14, 0)))]
    s = tm.get_summary(filtered_trades=dupes + unique, open_trades=[])
    assert s["engine_replay"]["excess_rows"] == 2
    assert s["engine_replay"]["excess_pnl"] == 2 * 20995
    assert s["engine_replay"]["unique_rows"] == 2
    assert s["daily_pnl"] == 20995 + 5000            # deduped headline
    assert s["total_trades"] == 2


def test_summary_segregates_futures_shadow_book():
    tm = TradeManager(broker=None, max_positions=5)
    real_opt = _trade("H_OPT", 1000.0, inst_type="OPT", symbol="BANKNIFTY58000CE")
    shadow = _trade("H_OPT_FUT", 24184.0, inst_type="FUT", symbol="FUT_SHADOW")
    s = tm.get_summary(filtered_trades=[real_opt, shadow], open_trades=[])
    assert s["futures_shadow"]["rows"] == 1
    assert s["futures_shadow"]["pnl"] == 24184
    assert s["daily_pnl"] == 1000                    # shadow excluded from headline
    assert s["fut_pnl"] == 0                         # shadow not in FUT headline
    assert s["opt_pnl"] == 1000


def test_summary_is_noop_on_normal_live_trades():
    tm = TradeManager(broker=None, max_positions=5)
    t1 = _trade("T1", 500.0, entry=IST.localize(datetime(2026, 7, 9, 10, 0)))
    t2 = _trade("T2", -200.0, inst_type="OPT", symbol="NIFTY24400CE",
                entry=IST.localize(datetime(2026, 7, 9, 11, 0)))
    s = tm.get_summary(filtered_trades=[t1, t2], open_trades=[])
    assert s["engine_replay"]["excess_rows"] == 0
    assert s["futures_shadow"]["rows"] == 0
    assert s["total_trades"] == 2


# ── Fix 2 ────────────────────────────────────────────────────────────────────

def _window(rows):
    idx = pd.date_range("2026-07-08 13:35", periods=len(rows), freq="5min")
    return pd.DataFrame(rows, index=idx)


def test_futures_sim_hits_target_before_ride_to_end():
    # LONG entry 100, stop 95, target 110; second bar tags the target.
    w = _window([
        {"open": 100, "high": 104, "low": 99, "close": 103},
        {"open": 103, "high": 111, "low": 102, "close": 108},
    ])
    price, reason, ts = SignalProcessor._simulate_futures_exit_path(w, 100.0, 95.0, 110.0, "LONG")
    assert reason == "TARGET_HIT" and price == 110.0 and ts == w.index[1]


def test_futures_sim_stop_first_and_tie_break():
    # SHORT entry 100, stop 105, target 90; bar sweeps both -> open closer to stop.
    w = _window([{"open": 104, "high": 106, "low": 89, "close": 95}])
    price, reason, _ = SignalProcessor._simulate_futures_exit_path(w, 100.0, 105.0, 90.0, "SHORT")
    assert reason == "SL HIT" and price == 105.0
    # neither level touched -> caller keeps its default
    w2 = _window([{"open": 100, "high": 101, "low": 99, "close": 100.5}])
    price2, reason2, ts2 = SignalProcessor._simulate_futures_exit_path(w2, 100.0, 95.0, 110.0, "LONG")
    assert reason2 == "" and price2 is None and ts2 is None


def test_shadow_builder_uses_simulation_source_contract():
    import inspect
    src = inspect.getsource(SignalProcessor._build_historical_futures_shadow_trade)
    assert "_simulate_futures_exit_path" in src
    assert "scanner.candles.get_candles" in src


def test_primary_fut_replay_gets_target_parity_source_contract():
    """The PRIMARY FUT replay path (not just shadows) must run the parity
    walker — this dataset's 334 FUT rows were primary-routed and had zero
    TARGET_HIT before the fix."""
    import inspect
    src = inspect.getsource(SignalProcessor.process_best_signal)
    fut_block = src[src.find('if sim_inst == "FUT":', src.find("between_candles")):]
    assert "_simulate_futures_exit_path" in fut_block[:2200]
    assert "fut_target_hit" in src
    # target-hit must be reflected in the exit reason assembly
    assert '"TARGET_HIT" if fut_target_hit' in src
