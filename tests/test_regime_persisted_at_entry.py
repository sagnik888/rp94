"""Regression: regime-at-entry must reach the Trade and survive persistence.

The candidate is tagged with market_regime at grading time, but historically
none of the scanner's open_trade() call sites forwarded it — every DB row
stored ''/UNKNOWN, making per-regime P&L attribution (e.g. '15min in chop')
impossible to measure. These tests pin the forwarding and the round-trip.
"""
import re


def test_all_scanner_open_trade_sites_forward_market_regime():
    src = open("scanner.py", encoding="utf-8").read()
    sites = [m.start() for m in re.finditer(r"\.open_trade\(", src)]
    assert len(sites) >= 3, f"expected >=3 open_trade call sites, found {len(sites)}"
    for pos in sites:
        window = src[pos : pos + 2500]
        call = window[: window.find("exec_type=") + 200]
        assert "market_regime=" in call, (
            "open_trade call site missing market_regime= near: "
            + src[max(0, pos - 120) : pos + 80].splitlines()[-1]
        )


def test_trade_round_trips_market_regime():
    from datetime import datetime
    from trading.trade_manager import IST, Trade

    trade = Trade(
        id="T1", instrument="NIFTY", timeframe="15min", direction="LONG",
        entry_price=100.0, entry_time=datetime.now(IST), trailing_stop=99.0,
        current_stop=99.0, lots=1, lot_size=75, grade="A",
        market_regime="RANGING",
    )
    assert trade.market_regime == "RANGING"
    payload = trade.to_dict()
    assert payload.get("market_regime") == "RANGING"
    restored = Trade.from_dict(payload)
    assert restored.market_regime == "RANGING"
