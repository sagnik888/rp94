"""The headline P&L must say which lane produced it.

2026-08-31, 16:46. The process was restarted after the close, so create_system refused
REAL mode -- "REAL startup refused by safe mode: outside REAL arming window
(09:00-15:30 IST)" -- and came up HISTORICAL, which is correct. It then replayed the
session and the dashboard header rendered the replay total as:

    DAILY P&L: Rs 6,065.1     TRADES: 10     W/L: 7/3     WR: 70.0%

The live result for that session was -Rs 1,106.50 across six exits. The replay rows are
identifiable -- their exit reasons are HISTORICAL / TARGET HIT / SL HIT, which the live
lane never emits, and their exit timestamps are blank -- but nothing on the header said
so, and the total was drawn in the same green as live money.

A losing day reading as a winning one is worse than any single bad trade, because every
judgement made from that screen is then made on a number that is not the money.

get_summary() is not at fault: it totals whatever trades the manager holds, which is
right for the mode it is in. The fault was that the lane was never carried to the thing
that renders the total. It is now decided in get_dashboard_payload, where is_historical
is already known, rather than inferred in the browser -- so no consumer of the payload
can render the headline unlabelled.
"""
import pytest

from trading.trade_manager import TradeManager


@pytest.fixture
def manager(monkeypatch):
    """A TradeManager shell: only the lane stamping is under test here."""
    import threading

    tm = TradeManager.__new__(TradeManager)
    # Exactly what get_dashboard_payload touches: _lock, closed_trades, open_trades,
    # get_summary, _build_session_analytics, _filter_concurrency_compliant_closed,
    # _valid_closed_chronology, _parse_time.
    tm._lock = threading.RLock()
    tm.closed_trades = []
    tm.open_trades = {}
    monkeypatch.setattr(
        TradeManager, "_filter_concurrency_compliant_closed", lambda self, rows: rows
    )
    monkeypatch.setattr(
        TradeManager, "_valid_closed_chronology", lambda self, *a, **k: True
    )
    monkeypatch.setattr(
        TradeManager, "get_summary", lambda self, **kwargs: {"daily_pnl": 6065.1}
    )
    monkeypatch.setattr(
        TradeManager, "_build_session_analytics", lambda self, *a, **k: {}
    )
    return tm


def _summary(manager, monkeypatch, *, is_historical):
    monkeypatch.setattr(
        "trading.trade_manager.build_dashboard_rows", lambda *a, **k: []
    )
    payload = manager.get_dashboard_payload(is_historical=is_historical)
    return payload["summary"]


def test_a_replay_total_is_labelled_as_a_backtest(manager, monkeypatch):
    """The exact 2026-08-31 situation: HISTORICAL mode, a positive replay total."""
    summary = _summary(manager, monkeypatch, is_historical=True)

    assert summary["is_replay"] is True
    assert summary["lane"] == "REPLAY"
    assert summary["pnl_label"] == "BACKTEST P&L", (
        "a replay total is still headlined as DAILY P&L; this is how +Rs 6,065 was read "
        "as the result of a session that actually lost Rs 1,106.50"
    )


def test_a_live_total_is_labelled_as_the_day(manager, monkeypatch):
    summary = _summary(manager, monkeypatch, is_historical=False)

    assert summary["is_replay"] is False
    assert summary["lane"] == "LIVE"
    assert summary["pnl_label"] == "DAILY P&L"


def test_the_lane_never_changes_the_number(manager, monkeypatch):
    """Labelling only. The total must be identical in both lanes."""
    replay = _summary(manager, monkeypatch, is_historical=True)
    live = _summary(manager, monkeypatch, is_historical=False)

    assert replay["daily_pnl"] == live["daily_pnl"] == 6065.1


def test_the_header_reads_the_server_flag_not_its_own_guess():
    """The browser must not re-derive the lane.

    trade-table.js already had `state.latestData.mode === 'HISTORICAL'` fifty lines below
    the P&L render and did not use it there. A second, client-side derivation of the same
    fact is how the two drift apart, so the renderer reads summary.is_replay.

    This is a source check and cannot observe the running page; it guards the wiring, not
    the pixels.
    """
    from pathlib import Path

    script = Path("dashboard/static/js/trade-table.js").read_text(encoding="utf-8")
    start = script.index("const pnlEl = document.getElementById('dailyPnl')")
    window = script[max(0, start - 900) : start + 900]

    assert "summary.is_replay" in window, (
        "the headline P&L no longer reads the server's lane flag"
    )
    assert "summary.pnl_label" in window
    assert "dailyPnlLabel" in window, "the header label is not being re-rendered"

    markup = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    assert 'id="dailyPnlLabel"' in markup, (
        "the header label has no id, so it cannot be switched to BACKTEST P&L"
    )


def test_a_replay_total_is_not_drawn_as_profit_or_loss():
    """Green means money. A rehearsal must not borrow that colour."""
    from pathlib import Path

    script = Path("dashboard/static/js/trade-table.js").read_text(encoding="utf-8")
    start = script.index("const pnlEl = document.getElementById('dailyPnl')")
    window = script[start : start + 700]

    assert "replay-value" in window, "a replay total still renders in the live green/red"
    css = Path("dashboard/static/css/style.css").read_text(encoding="utf-8")
    assert ".h-summary-value.replay-value" in css, "replay styling is not defined"
