from datetime import datetime
from types import SimpleNamespace

from engine.scanner_dashboard import ScannerTradePayloadBuilder


def _scanner_with_rows(rows, session_rows=None):
    session_rows = list(session_rows or [])
    return SimpleNamespace(
        mode="HISTORICAL",
        backtest_days=30,
        scan_count=1,
        simulation_id=7,
        _historical_completed_simulation_id=7,
        _calculation_lock=False,
        is_calculating=False,
        _historical_rebuild_pending=False,
        _recalculation_status={"status": "idle"},
        inst_pref="AUTO",
        active_indices=["NIFTY"],
        _session_trade_candidates={"NIFTY": {}} if session_rows else {},
        _latest_filtered_candidates={},
        trades=SimpleNamespace(
            get_dashboard_payload=lambda **kwargs: {
                "open": [],
                "closed": list(rows),
                "signals": [],
                "summary": {},
                "analytics": {},
            }
        ),
        _setting_enabled=lambda value, default: default,
        _get_current_session_day=lambda: "2026-09-06",
        historical_session_cutoff_date=lambda: datetime(2026, 9, 4).date(),
        _get_session_trade_candidates=lambda instrument: list(session_rows),
        _normalize_loaded_exit_candidate=lambda candidate: False,
        _serialize_trade_candidate=lambda candidate: (
            dict(candidate) if isinstance(candidate, dict) else dict(candidate.__dict__)
        ),
    )


def _row(*, trade_id, provenance, pnl, exit_price, exit_reason):
    return {
        "id": trade_id,
        "lifecycle_id": trade_id,
        "instrument": "NIFTY 24000 CE",
        "timeframe": "15min",
        "direction": "LONG",
        "inst_type": "OPT",
        "trading_symbol": "NIFTY2690824000CE",
        "entry_price": 100.0,
        "exit_price": exit_price,
        "entry_timestamp": datetime(2026, 9, 3, 10, 0).timestamp(),
        "exit_timestamp": datetime(2026, 9, 3, 10, 30).timestamp(),
        "status": "CLOSED",
        "data_provenance": provenance,
        "exit_reason": exit_reason,
        "pnl": pnl,
        "lots": 1,
        "lot_size": 65,
    }


def test_backtest_headline_excludes_carried_live_and_session_ledger_rows():
    replay = _row(
        trade_id="H_NIFTY_15min_UTBOT_20260903100000",
        provenance="BACKTEST",
        pnl=5000.0,
        exit_price=178.15384615384616,
        exit_reason="HISTORICAL",
    )
    carried_live = _row(
        trade_id="SESSION|NIFTY|15min|ENTRY|LONG|2026-09-03T10:00:00",
        provenance="LIVE",
        pnl=-900.0,
        exit_price=87.38461538461539,
        exit_reason="LOSS_THREAT_EXIT",
    )

    payload = ScannerTradePayloadBuilder(
        _scanner_with_rows([carried_live, replay])
    ).build()

    assert payload["summary"]["pnl_label"] == "BACKTEST P&L"
    assert payload["summary"]["total_trades"] == 1
    assert payload["summary"]["daily_pnl"] == 5000.0
    assert [row["id"] for row in payload["closed"]] == [replay["id"]]
    assert payload["analytics"]["by_exit_reason"] == {
        "HISTORICAL": {"count": 1, "pnl": 5000.0, "wins": 1, "losses": 0, "win_rate": 100.0}
    }
    assert payload["meta"]["excluded_session_ledger_rows"] == 1
    assert payload["meta"]["excluded_session_ledger_pnl"] == -900.0


def test_historical_reconstruction_cannot_reinject_a_carried_session_candidate():
    replay = _row(
        trade_id="H_NIFTY_15min_UTBOT_20260903100000",
        provenance="BACKTEST",
        pnl=5000.0,
        exit_price=178.15384615384616,
        exit_reason="HISTORICAL",
    )
    carried_session = {
        **_row(
            trade_id="SESSION|NIFTY|15min|ENTRY|LONG|2026-09-03T10:00:00",
            provenance="LIVE",
            pnl=-900.0,
            exit_price=87.38461538461539,
            exit_reason="LOSS_THREAT_EXIT",
        ),
        "action": "ENTRY",
        "price": 100.0,
        "current_price": 87.38461538461539,
        "timestamp": "2026-09-03T10:00:00+05:30",
    }

    payload = ScannerTradePayloadBuilder(
        _scanner_with_rows([replay], session_rows=[carried_session])
    ).build()

    assert payload["summary"]["total_trades"] == 1
    assert payload["summary"]["daily_pnl"] == 5000.0
    assert [row["id"] for row in payload["closed"]] == [replay["id"]]
    assert payload["meta"]["excluded_session_ledger_rows"] == 0
    assert payload["meta"]["excluded_session_candidate_rows"] == 1
    assert payload["meta"]["excluded_session_candidate_pnl"] == -900.0
