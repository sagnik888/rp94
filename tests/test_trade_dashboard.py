from datetime import datetime

from trading.trade_dashboard import build_dashboard_rows
from trading.trade_manager import IST, Trade, TradeManager


def _manager():
    manager = TradeManager.__new__(TradeManager)
    manager.options_sl_pct = 15.0
    manager.orphan_positions = {}
    return manager


def _trade(trade_id="t1", **updates):
    values = {
        "id": trade_id,
        "instrument": "NIFTY",
        "timeframe": "15min",
        "direction": "LONG",
        "entry_price": 100.0,
        "entry_time": IST.localize(datetime(2026, 6, 12, 10, 0)),
        "trailing_stop": 90.0,
        "current_stop": 90.0,
        "lots": 1,
        "lot_size": 75,
        "grade": "A",
        "target": 120.0,
        "confidence": 0.82,
    }
    values.update(updates)
    return Trade(**values)


def test_trade_rows_preserve_timestamps_and_closed_price_rules():
    trade = _trade(
        status="CLOSED",
        exit_price=112.0,
        current_price=112.0,
        exit_time=IST.localize(datetime(2026, 6, 12, 11, 15)),
        exit_reason="TARGET",
    )

    row = build_dashboard_rows(_manager(), [], [trade], False)[0]

    assert row["entry_time"] == "10:00:00"
    assert row["entry_date"] == "12 Jun 26"
    assert row["exit_time"] == "11:15:00"
    assert row["exit_timestamp"].endswith("+05:30")
    assert row["current_price"] == 0.0
    assert row["exit_price"] == 112.0


def test_live_option_spot_levels_are_converted_to_premium_levels():
    trade = _trade(
        inst_type="OPT",
        atm_strike=23000,
        option_type="CE",
        entry_spot=23000.0,
        current_stop=22950.0,
        target=23100.0,
        instrument_multiplier=0.5,
    )

    row = build_dashboard_rows(_manager(), [trade], [], False)[0]

    assert row["instrument"] == "NIFTY 23000 CE"
    assert row["trailing_stop"] == 85.0
    assert row["target"] == 150.0


def test_rows_are_sorted_capped_and_include_live_orphans():
    manager = _manager()
    manager.orphan_positions = {
        "NIFTY26JUN23000CE": {
            "netqty": "-75",
            "sellavgprice": 95.0,
            "ltp": 90.0,
            "unrealised": 375.0,
        }
    }
    older = _trade("old", entry_time=datetime(2026, 6, 11, 10, 0))
    newer = _trade("new", entry_time=datetime(2026, 6, 12, 10, 0))

    rows = build_dashboard_rows(manager, [older, newer], [], True, limit=1)

    assert [row["id"] for row in rows] == ["new", "ORPHAN|NIFTY26JUN23000CE"]
    assert rows[1]["direction"] == "SHORT"
    assert rows[1]["inst_type"] == "OPT"
