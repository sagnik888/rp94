"""P&L and TRADES must not freeze when some signals ran in Auto and others did not.

2026-09-17, REAL mode. Auto Mode was OFF for most of the session (nine restarts, three
manual switch-offs), so accepted signals became SIGNAL_ONLY marks. The one broker fill
(NIFTY 09:18) switched the dashboard headline to the broker ledger, and from then on the
seven manual signals (+Rs.816 in marks) never reached P&L or TRADES: the header read
"TRADES 2, P&L -205.5" all afternoon while signals kept firing.

It also counted a MIDCPNIFTY entry the margin pre-check REFUSED as a closed losing
trade with -Rs.80 of charges that were never paid. TradeManager.get_summary already
excluded refused entries; the dashboard rebuilt its headline from rows and did not.
"""
from types import SimpleNamespace

from engine.scanner_dashboard import ScannerTradePayloadBuilder
from engine.trade_row_adapter import normalize_dashboard_accounting_row


DAY = "2026-09-17"


def _broker_row(trade_id, pnl, *, status="CLOSED", execution_status="CLOSED", order_id="260917190096110"):
    return {
        "id": trade_id, "instrument": "NIFTY 23200 CE", "timeframe": "15min", "direction": "LONG",
        "inst_type": "OPT", "trading_symbol": "NIFTY2692223200CE", "status": status,
        "execution_status": execution_status, "broker_order_id": order_id,
        "entry_date": DAY, "exit_date": DAY, "entry_time": "09:18:50", "exit_time": "09:20:05",
        "entry_timestamp": f"{DAY}T09:18:50", "exit_timestamp": f"{DAY}T09:20:05",
        "pnl": pnl, "charges": 80.0, "lifecycle_id": trade_id, "exit_reason": "BREAKEVEN_STOP",
    }


def _signal_row(life, action, pnl, entered, exited=""):
    return {
        "id": f"SESSION|{life}", "lifecycle_id": life, "action": action, "inst_type": "OPT",
        "instrument": "BANKNIFTY", "timeframe": "5min", "direction": "SHORT",
        "pnl": pnl, "exit_reason": "SMART_PROFIT_LOCK" if exited else "",
        "entry_timestamp": f"{DAY}T{entered}", "exit_timestamp": f"{DAY}T{exited}" if exited else "",
        "accepted_by_gate": True,
    }


def _scanner(closed, session_rows):
    return SimpleNamespace(
        mode="REAL", backtest_days=1, scan_count=1, simulation_id=1, inst_pref="AUTO",
        active_indices=["NIFTY"], capital_total=1_000_000.0,
        _session_trade_candidates={"NIFTY": {}},
        trades=SimpleNamespace(get_dashboard_payload=lambda **kwargs: {
            "open": [], "closed": [dict(row) for row in closed], "signals": [], "summary": {}, "analytics": {},
        }),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: DAY,
        _loaded_session_is_current_day=lambda: True,
        _get_session_trade_candidates=lambda instrument: session_rows,
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: dict(candidate),
    )


FILLED = _broker_row("T0014_NIFTY_09170918", -125.5)
REFUSED = _broker_row("T0015_MIDCPNIFTY_09170919", -80.0, status="CLOSED",
                      execution_status="REJECTED", order_id="")
SIGNALS = [
    _signal_row("bank", "EXIT", 752.15, "13:27:26", "13:28:34"),
    _signal_row("nifty", "EXIT", 655.65, "13:27:29", "13:28:45"),
    _signal_row("sensex", "EXIT", -328.0, "14:14:48", "14:16:34"),
]


def test_a_refused_entry_is_listed_but_not_counted_as_a_trade():
    payload = ScannerTradePayloadBuilder(_scanner([FILLED, REFUSED], [])).build()

    summary = payload["summary"]
    assert summary["total_trades"] == 1
    assert summary["daily_pnl"] == -125.5
    assert summary["losses"] == 1
    assert summary["rejected_entries"] == 1
    listed = {row["id"]: row for row in payload["closed"]}
    assert listed["T0015_MIDCPNIFTY_09170919"]["pnl"] == 0.0
    assert listed["T0015_MIDCPNIFTY_09170919"]["accounting_excluded"] == "REJECTED"


def test_manual_signals_are_published_beside_the_broker_headline():
    payload = ScannerTradePayloadBuilder(_scanner([FILLED, REFUSED], SIGNALS)).build()

    summary = payload["summary"]
    # Money headline is the broker ledger only.
    assert summary["pnl_is_money"] is True
    assert summary["total_trades"] == 1
    assert summary["daily_pnl"] == -125.5
    # The manual lane is no longer dropped, and never mixed into money.
    lane = summary["signal_lane"]
    assert lane["pnl_is_money"] is False
    assert lane["trades"] == 3
    assert lane["pnl"] == 1079.8
    assert (lane["wins"], lane["losses"]) == (2, 1)


def test_refused_entries_alone_do_not_hide_the_signal_lane():
    payload = ScannerTradePayloadBuilder(_scanner([REFUSED], SIGNALS)).build()

    summary = payload["summary"]
    assert summary["pnl_is_money"] is False
    assert summary["total_trades"] == 3
    assert summary["daily_pnl"] == 1079.8


def test_signal_lane_absent_without_manual_signals():
    payload = ScannerTradePayloadBuilder(_scanner([FILLED], [])).build()

    assert "signal_lane" not in payload["summary"]


def test_normalizing_a_refused_row_zeroes_only_display_accounting():
    row = normalize_dashboard_accounting_row(dict(REFUSED))

    assert row["raw_pnl"] == -80.0
    assert (row["pnl"], row["accounting_pnl"], row["charges"]) == (0.0, 0.0, 0.0)
    assert normalize_dashboard_accounting_row(dict(FILLED))["pnl"] == -125.5
