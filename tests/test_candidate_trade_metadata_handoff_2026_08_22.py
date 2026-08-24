"""Entry attribution must survive Candidate -> Trade without becoming exit context.

The scanner stamps provenance, price-action, strategy revision and a canonical signed
intelligence score on each accepted candidate.  All three execution routes used to
drop that metadata at ``open_trade()``, so persisted trades could not be attributed.
Simply copying the score into the old generic field was also unsafe: the exit helper
read that frozen entry value before the fresh context supplied by its caller.
"""

import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

import scanner as scanner_module
from data.sqlite_db import TRADE_PERSISTED_COLUMNS, db
from scanner import Scanner
from trading.trade_manager import IST, Trade, TradeManager


ENTRY_METADATA_KWARGS = {
    "data_provenance",
    "price_action_verdict",
    "entry_intelligence_score",
    "strategy_revision",
    "strategy_direction_mode",
}


def _trade(**overrides) -> Trade:
    values = {
        "id": "T0001_NIFTY_08221000",
        "instrument": "NIFTY",
        "timeframe": "5min",
        "direction": "LONG",
        "entry_price": 125.0,
        "entry_time": IST.localize(datetime(2026, 8, 22, 10, 0)),
        "trailing_stop": 110.0,
        "current_stop": 110.0,
        "lots": 1,
        "lot_size": 65,
        "grade": "A",
    }
    values.update(overrides)
    return Trade(**values)


def _historical_manager() -> TradeManager:
    """Minimal safe manager: it cannot submit a broker order."""
    manager = TradeManager.__new__(TradeManager)
    manager._lock = threading.RLock()
    manager.open_trades = {}
    manager.closed_trades = []
    manager._trade_counter = 0
    manager.mode = "HISTORICAL"
    manager.broker = None
    manager.ui_logger = None
    manager.can_open_trade = lambda is_recovery=False: (True, "")
    manager._same_contract_entry_blocked = lambda *args, **kwargs: (False, "")
    manager._entry_mark_admission = lambda **kwargs: (True, "")
    manager._entry_risk_admission = lambda **kwargs: (True, "")
    manager.nudge_position_sync = lambda: None
    manager._mark_dirty = lambda: None
    manager._live_orders_permitted = lambda: False
    return manager


def test_all_three_scanner_entry_routes_forward_candidate_attribution():
    tree = ast.parse(Path(scanner_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "open_trade"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "trades"
    ]

    assert len(calls) == 3, "A new scanner execution route needs the same metadata handoff"
    for call in calls:
        forwarded = {keyword.arg for keyword in call.keywords if keyword.arg}
        assert ENTRY_METADATA_KWARGS <= forwarded


def test_open_trade_copies_candidate_attribution_into_the_trade():
    manager = _historical_manager()

    trade = manager.open_trade(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        price=125.0,
        trailing_stop=110.0,
        lots=1,
        lot_size=65,
        grade="A",
        data_provenance="LIVE",
        price_action_verdict="FAVORABLE",
        entry_intelligence_score=-0.37,
        strategy_revision="rev-2026-08-22",
        strategy_direction_mode="WITH_TREND",
        is_ghost=True,
    )

    assert trade is not None
    assert trade.data_provenance == "LIVE"
    assert trade.price_action_verdict == "FAVORABLE"
    assert trade.entry_intelligence_score == pytest.approx(-0.37)
    # Older telemetry readers still see the same entry snapshot.
    assert trade.intelligence_score == pytest.approx(-0.37)
    assert trade.strategy_revision == "rev-2026-08-22"
    assert trade.strategy_direction_mode == "WITH_TREND"


@pytest.mark.parametrize(
    "fresh_aggregate, expected",
    [
        ({"score_norm": -0.24}, -0.24),
        ({"score_norm": 0.0}, 0.0),
        ({"score": 73.0}, 0.73),
    ],
)
def test_fresh_exit_context_wins_over_the_frozen_entry_score(fresh_aggregate, expected):
    scanner = Scanner.__new__(Scanner)
    trade = SimpleNamespace(
        entry_intelligence_score=0.91,
        intelligence_score=0.91,
    )

    result = scanner._trade_like_signed_intelligence(
        trade,
        {"aggregate": fresh_aggregate},
    )

    assert result == pytest.approx(expected)


def test_resolved_live_context_wins_when_the_caller_does_not_pass_one():
    scanner = Scanner.__new__(Scanner)
    scanner._trade_like_market_intelligence_context = lambda trade: {
        "aggregate": {"score_norm": -0.42}
    }
    trade = SimpleNamespace(
        entry_intelligence_score=0.91,
        intelligence_score=0.91,
    )

    assert scanner._trade_like_signed_intelligence(trade) == pytest.approx(-0.42)


def test_entry_score_round_trips_and_old_rows_get_a_backward_default():
    trade = _trade(
        entry_intelligence_score=-0.37,
        intelligence_score=-0.37,
    )
    payload = trade.to_dict()

    assert payload["entry_intelligence_score"] == pytest.approx(-0.37)
    assert Trade.from_dict(payload).entry_intelligence_score == pytest.approx(-0.37)

    legacy_payload = dict(payload)
    legacy_payload.pop("entry_intelligence_score")
    assert Trade.from_dict(legacy_payload).entry_intelligence_score == pytest.approx(-0.37)


def test_entry_score_is_in_the_sqlite_contract_and_migrated_schema():
    assert "entry_intelligence_score" in TRADE_PERSISTED_COLUMNS
    columns = {
        str(row["name"])
        for row in db.conn.execute("PRAGMA table_info(trades)").fetchall()
    }
    assert "entry_intelligence_score" in columns
