"""A graded A+ entry may lean on fresh completed Fyers cash bars; fast routes may not.

2026-09-17: Fyers returned ``volume=0`` for all four indices on both its websocket and
its quote endpoint, so a forming-minute cash reading existed only when a history poll
landed inside the same minute.  The A+ data gate required that reading and blocked
BANKNIFTY 5min SHORT at 11:00 (+94 index points in 15 minutes) while every completed
1-minute cash bar was present.

The same morning showed why "completed bars" must mean TODAY's bars: at 09:16 the last
ten rows of the continuous candle store were mostly the previous session, and the A+
SHORT the gate refused there went 155 points against.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from engine.entry_data_quality import MIN_SESSION_CASH_BARS, evaluate_entry_data_quality
from scanner import Scanner


def _chain():
    return {"intelligence_usable": True, "stale": False, "fallback": False}


def _completed_only(session_bars=MIN_SESSION_CASH_BARS):
    return {
        "volume_available": False,
        "volume_reliable": False,
        "volume_comparable": True,
        "completed_cash_volume_fresh": True,
        "completed_cash_session_bars": session_bars,
    }


def test_completed_session_bars_satisfy_the_graded_a_plus_volume_check():
    verdict = evaluate_entry_data_quality(
        live=True,
        special_entry=True,
        one_minute_volume=_completed_only(),
        chain_quality=_chain(),
        accept_completed_cash_bars=True,
    )

    assert verdict["allowed"]
    assert verdict["reason_code"] == "READY"
    assert verdict["checks"]["one_minute_cash_volume_basis"] == "completed_bars"


def test_default_caller_still_requires_the_forming_minute():
    verdict = evaluate_entry_data_quality(
        live=True,
        special_entry=True,
        one_minute_volume=_completed_only(),
        chain_quality=_chain(),
    )

    assert not verdict["allowed"]
    assert verdict["reason_code"] == "ONE_MINUTE_VOLUME_UNAVAILABLE"


def test_previous_session_bars_cannot_stand_in_at_the_open():
    verdict = evaluate_entry_data_quality(
        live=True,
        special_entry=True,
        one_minute_volume=_completed_only(session_bars=1),
        chain_quality=_chain(),
        accept_completed_cash_bars=True,
    )

    assert not verdict["allowed"]
    assert verdict["reason_code"] == "ONE_MINUTE_VOLUME_UNAVAILABLE"


def test_stale_completed_bars_do_not_count():
    context = _completed_only()
    context["completed_cash_volume_fresh"] = False
    verdict = evaluate_entry_data_quality(
        live=True,
        special_entry=True,
        one_minute_volume=context,
        chain_quality=_chain(),
        accept_completed_cash_bars=True,
    )

    assert not verdict["allowed"]
    assert verdict["reason_code"] == "ONE_MINUTE_VOLUME_UNAVAILABLE"


def _scanner_with(context):
    return SimpleNamespace(
        mode="REAL",
        _live_entry_data_blocks={},
        candles=SimpleNamespace(get_candles=lambda *_: None),
        _prediction_volume_context=lambda *_: dict(context),
    )


def _candidate(**overrides):
    values = dict(
        instrument="BANKNIFTY",
        grade="A+",
        market_ltp_shock_entry=False,
        volume_spike_entry=False,
        chain_quality=_chain(),
        inst_type="FUT",
        strike_oi_available=False,
        strike_volume_available=False,
        strike_liquidity_available=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_scanner_admits_graded_a_plus_on_completed_bars():
    verdict = Scanner._live_entry_data_quality(
        _scanner_with(_completed_only()), _candidate()
    )

    assert verdict["allowed"], verdict
    assert verdict["checks"]["one_minute_cash_volume_basis"] == "completed_bars"


def test_scanner_keeps_shock_and_spike_routes_on_the_forming_minute():
    for flag in ("market_ltp_shock_entry", "volume_spike_entry"):
        verdict = Scanner._live_entry_data_quality(
            _scanner_with(_completed_only()), _candidate(**{flag: True})
        )
        assert not verdict["allowed"], flag
        assert verdict["reason_code"] == "ONE_MINUTE_VOLUME_UNAVAILABLE", flag


def _evidence_scanner():
    return Scanner.__new__(Scanner)


def test_session_cash_bars_ignore_the_previous_session():
    current_minute = datetime.now().replace(second=0, microsecond=0)
    completed = current_minute - timedelta(minutes=1)
    yesterday = [completed - timedelta(days=1, minutes=8 - i) for i in range(8)]
    index = pd.DatetimeIndex(yesterday + [completed, current_minute])
    frame = pd.DataFrame(
        {
            "open": range(10),
            "high": range(1, 11),
            "low": range(10),
            "close": range(1, 11),
            "volume": [1000.0 + i for i in range(9)] + [0.0],
            "volume_basis": ["fyers_index"] * 9 + [""],
            "source": ["fyers_history"] * 9 + ["live_tick"],
        },
        index=index,
    )

    evidence = Scanner._recent_cash_volume_evidence(_evidence_scanner(), "NIFTY", frame)

    assert evidence["positive_cash_bars"] == 9
    assert evidence["session_cash_bars"] == 1


def test_session_cash_bars_count_a_full_morning():
    current_minute = datetime.now().replace(second=0, microsecond=0)
    index = pd.date_range(end=current_minute, periods=10, freq="1min")
    frame = pd.DataFrame(
        {
            "open": range(10),
            "high": range(1, 11),
            "low": range(10),
            "close": range(1, 11),
            "volume": [1000.0 + i for i in range(9)] + [0.0],
            "volume_basis": ["fyers_index"] * 9 + [""],
            "source": ["fyers_history"] * 9 + ["live_tick"],
        },
        index=index,
    )

    evidence = Scanner._recent_cash_volume_evidence(_evidence_scanner(), "NIFTY", frame)

    # Crossing midnight while the test runs would legitimately split the window.
    if index[0].date() == index[-2].date():
        assert evidence["session_cash_bars"] == 9
    assert evidence["completed_cash_volume_fresh"] is True
