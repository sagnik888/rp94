"""Two defects found by comparing against the v6 build, verified present here.

A. The dashboard Execute button placed a GHOST trade. execute_manual_signal called
   open_trade(..., is_ghost=True) and then reported "ORDER PENDING"/"submitted", so no
   broker order was ever sent — while the toast Execute (make_executor) passed
   is_ghost=False and was real. Two manual surfaces, opposite behaviour, identical UI.

B. Closed ghost P&L contaminated the real daily circuit breaker. The closed-trade loop
   summed every row while the open-trade loop excluded ghosts, so virtual profits could
   mask real losses (breaker fails to fire) and virtual losses could halt real trading.

The two compounded: ghosts created by A closed, then drove the breaker via B.
"""
import inspect
from types import SimpleNamespace

from engine.risk_manager import RiskManager
from scanner import Scanner


# ── A: dashboard Execute must be a real order ────────────────────────────────

def test_dashboard_execute_is_not_a_ghost():
    # Strip comments: the fix carries an explanatory comment that mentions the old
    # is_ghost=True value, which a naive substring check would match.
    code = "\n".join(
        line.split("#", 1)[0]
        for line in inspect.getsource(Scanner.execute_manual_signal).splitlines()
    )
    assert "is_ghost=False" in code, "dashboard Execute must place a REAL broker order"
    assert "is_ghost=True" not in code, "dashboard Execute must not create a virtual trade"


def test_dashboard_execute_still_reports_submission():
    """It reported success before; that must remain true now that it is real."""
    src = inspect.getsource(Scanner.execute_manual_signal)
    assert "ORDER PENDING" in src
    assert '"status": "submitted"' in src or "'status': 'submitted'" in src


# ── B: virtual rows must not drive the real breaker ──────────────────────────

def _trade(**over):
    base = dict(is_ghost=False, id="T1", trading_symbol="NIFTY31JUL24000CE",
                grade="A", data_provenance="LIVE")
    base.update(over)
    return SimpleNamespace(**base)


def test_ghost_trade_is_excluded_from_risk():
    assert RiskManager._is_non_live_trade(_trade(is_ghost=True)) is True


def test_historical_and_eod_rows_are_excluded():
    assert RiskManager._is_non_live_trade(_trade(id="H_123")) is True
    assert RiskManager._is_non_live_trade(_trade(id="EOD_9")) is True


def test_futures_shadow_rows_are_excluded():
    assert RiskManager._is_non_live_trade(_trade(trading_symbol="FUT_SHADOW")) is True
    assert RiskManager._is_non_live_trade(_trade(grade="A FUT SHADOW")) is True


def test_replay_and_simulation_provenance_excluded():
    for prov in ("HISTORICAL", "BACKTEST", "REPLAY", "SIM", "SIMULATION", "PAPER_HIST"):
        assert RiskManager._is_non_live_trade(_trade(data_provenance=prov)) is True


def test_real_live_trade_is_counted():
    assert RiskManager._is_non_live_trade(_trade()) is False


def test_closed_trade_loop_filters_non_live_rows():
    """The contaminated loop must now skip virtual rows, like the open-trade loop."""
    src = inspect.getsource(RiskManager.check_circuit_breaker)
    closed_section = src.split("Closed Trades for today", 1)[1].split(
        "# 2. Open Trades", 1
    )[0]
    assert "_is_non_live_trade" in closed_section


# ── ported v6 rate limiter (fixes the volume outage chain) ───────────────────

def test_history_limiter_is_available_and_bounded():
    from trading.api_rate_limiter import (
        SharedApiRateLimiter, get_shared_history_limiter,
    )

    limiter = get_shared_history_limiter("TESTCLIENT")
    snap = limiter.snapshot()
    assert snap["per_minute_limit"] > 0 and snap["per_hour_limit"] > 0
    # AngelOne caps order book / positions / trade book at 1 request per second.
    assert SharedApiRateLimiter.ENDPOINT_MIN_INTERVALS["order_book"] == 1.0
    assert SharedApiRateLimiter.ENDPOINT_MIN_INTERVALS["positions"] == 1.0


def test_existing_callers_remain_compatible():
    """begin_priority_call(timeout=...) must still work without an endpoint."""
    from trading.api_rate_limiter import get_shared_api_limiter

    class FakeApi:
        pass

    limiter = get_shared_api_limiter(FakeApi())
    assert limiter.begin_priority_call(timeout=1.0) is True
    limiter.end_priority_call()


def test_angel_history_call_is_rate_gated():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("data/market_data.py").read_text(encoding="utf-8")
    assert "get_shared_history_limiter" in src
    assert "history_limiter.try_begin" in src, "getCandleData must be paced"
