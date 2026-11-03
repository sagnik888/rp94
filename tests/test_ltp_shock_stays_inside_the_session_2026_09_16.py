"""The LTP-shock lane must measure the move inside one session, not across the gap.

2026-09-15, 09:17: SENSEX and NIFTY were admitted LONG as CONFIRMED_MARKET_SHIFT with
"market LTP rally: core 3/3, total 4, avg |move|=0.21% in <=5m". The indices had opened at
their high and were already falling; both trades were cut within a minute
(-Rs.336 and -Rs.258.75) and their 15/30/60-minute shadow marks were -1,753 to -3,382.

The 0.21% was the overnight gap. `_evaluate_market_ltp_shock` walks the 1-minute frame
positionally (`frame["close"].iloc[-(lb + 1)]` for a 3-5 bar lookback) and the candle store
is continuous across sessions: in data_store/candles/NIFTY_1min.csv the row before
2026-09-15 09:15 is 2026-09-11 15:29. At 09:15-09:19 the reference bar is therefore the
previous session's close. The prior-volume baseline crossed the same boundary, and when it
summed to zero the volume test was skipped while `volume_ratio` was still recorded as 1.0.

These tests pin the session boundary and the honest unavailable ratio. They do not change
any threshold value; the strong-tier move floor still decides what counts as a shock.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from scanner import Scanner

IST_FMT = "%Y-%m-%d %H:%M"


def _frame(rows):
    """rows: list of (timestamp string, close, volume)."""
    index = pd.to_datetime([r[0] for r in rows])
    closes = [float(r[1]) for r in rows]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.0005 for c in closes],
            "low": [c * 0.9995 for c in closes],
            "close": closes,
            "volume": [float(r[2]) for r in rows],
        },
        index=index,
    )


def _scanner(frames):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner._setting_enabled = Scanner._setting_enabled
    scanner.candles = type(
        "Candles",
        (),
        {
            "get_candles": staticmethod(lambda inst, tf: frames.get(inst)),
            "get_latest_price": staticmethod(lambda inst: 0.0),
        },
    )()
    return scanner


def _gap_open_frames():
    """Prior session closes lower, today gaps up 0.5% and then goes nowhere."""
    frames = {}
    for inst, base in (("NIFTY", 23400.0), ("BANKNIFTY", 56300.0), ("SENSEX", 74800.0), ("MIDCPNIFTY", 14300.0)):
        prior = [(f"2026-09-11 15:2{m}", base, 1_000_000.0) for m in range(5, 10)]
        gapped = base * 1.005
        today = [
            ("2026-09-15 09:15", gapped, 3_000_000.0),
            ("2026-09-15 09:16", gapped * 0.9999, 3_000_000.0),
            ("2026-09-15 09:17", gapped * 0.9998, 3_000_000.0),
        ]
        frames[inst] = _frame(prior + today)
    return frames


def test_the_overnight_gap_is_not_a_shock():
    scanner = _scanner(_gap_open_frames())

    shock = Scanner._evaluate_market_ltp_shock(
        scanner,
        "NIFTY",
        "BUY",
        datetime.strptime("2026-09-15 09:17", IST_FMT),
        timeframe="5min",
        setup_confidence=0.96,
        setup_adx=30.0,
        setup_intel_score=0.6,
        trend_15m_agrees=True,
    )

    assert shock is None, (
        "the opening minutes have fewer in-session bars than the lookback, so the only "
        f"move available is the overnight gap: {shock}"
    )


def test_a_real_intraday_rally_is_still_a_shock():
    """Same lane, inside the session and inside the window: must still fire."""
    frames = {}
    for inst, base in (("NIFTY", 23400.0), ("BANKNIFTY", 56300.0), ("SENSEX", 74800.0), ("MIDCPNIFTY", 14300.0)):
        flat = [(f"2026-09-15 13:{m:02d}", base, 1_000_000.0) for m in range(10, 28)]
        rally = [
            ("2026-09-15 13:28", base * 1.002, 3_000_000.0),
            ("2026-09-15 13:29", base * 1.004, 3_000_000.0),
            ("2026-09-15 13:30", base * 1.006, 3_000_000.0),
        ]
        frames[inst] = _frame(flat + rally)
    scanner = _scanner(frames)

    shock = Scanner._evaluate_market_ltp_shock(
        scanner,
        "NIFTY",
        "BUY",
        datetime.strptime("2026-09-15 13:30", IST_FMT),
        timeframe="5min",
    )

    assert shock is not None, "an in-session multi-index rally must still be detected"
    assert shock["core_votes"] >= 2
    assert shock["avg_abs_move_pct"] >= 0.35


def test_missing_prior_volume_is_recorded_as_unavailable_not_one():
    """A zero prior-volume baseline must not be reported as a 1.0 ratio."""
    frames = {}
    for inst, base in (("NIFTY", 23400.0), ("BANKNIFTY", 56300.0), ("SENSEX", 74800.0), ("MIDCPNIFTY", 14300.0)):
        quiet = [(f"2026-09-15 13:{m:02d}", base, 0.0) for m in range(10, 28)]
        rally = [
            ("2026-09-15 13:28", base * 1.002, 3_000_000.0),
            ("2026-09-15 13:29", base * 1.004, 3_000_000.0),
            ("2026-09-15 13:30", base * 1.006, 3_000_000.0),
        ]
        frames[inst] = _frame(quiet + rally)
    scanner = _scanner(frames)

    shock = Scanner._evaluate_market_ltp_shock(
        scanner,
        "NIFTY",
        "BUY",
        datetime.strptime("2026-09-15 13:30", IST_FMT),
        timeframe="5min",
    )

    # The move clears the strong tier, so the price-only fallback still admits it.
    assert shock is not None
    for vote in shock["votes"].values():
        assert vote["volume_ratio"] == 0.0, (
            "with no prior volume the ratio is unknown; 1.0 reads as measured confirmation"
        )
