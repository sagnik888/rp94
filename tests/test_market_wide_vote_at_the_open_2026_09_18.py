"""A gap is not a market-wide thrust, and a forming bar is not a closed one.

2026-09-18 09:17:30: SENSEX 15min LONG, B+ 63%, RANGING, ADX 14.9. Two minutes earlier the
same signal had been refused (BPLUS_RISKY_SETUP_INSUFFICIENT). After a 09:16:23 restart it
was admitted through route=confirmed_market_shift on a market-wide vote of "core 3/3,
total 4, range>=0.50x, close>=65%". The gap (+260: 74,314.59 -> 74,575.24) faded 77
points the next minute; early loss cut at 69 seconds, -Rs.599, never green.

Reproducing the vote with the real function showed two independent faults:

1. The bar it scored was the 09:15 15min bar, 2.5 minutes old. require_closed judged
   "closed" by _session_squareoff_clock(), which is advance-only by design (right for a
   square-off, wrong for an entry). At the wall clock every index returns None; only a
   clock reading 09:30 reproduces the votes. The volume row of the same decision proves
   the bar was correctly labelled 09:15 and seen as forming.
2. Even closed, the opening auction bar is scored against ATR14 of the PREVIOUS session's
   closing bars, the quietest of the day. Any gap that has not faded reads as a thrust:
   SENSEX 1.39x / 0.69, BANKNIFTY 1.99x / 0.98, MIDCPNIFTY 2.39x / 0.96.
"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import Settings, get_settings
from data.candle_builder import CandleBuilder
from scanner import Scanner

IDX = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
ROOT = Path(__file__).resolve().parents[1]


def _frame(today_bars, *, gap=0.0, base=1000.0):
    """Quiet previous session, then `today_bars` 15min bars that each thrust upward.

    Yesterday: 25 bars of range 4 closing mid-bar. Today: every bar ranges 20 and closes at
    its high, so each one clears 0.50x ATR and the 65% close location on any baseline.
    """
    y_idx = pd.date_range("2026-09-17 09:15", periods=25, freq="15min")
    rows = [dict(open=base, high=base + 2, low=base - 2, close=base, volume=1000.0) for _ in y_idx]
    t_idx = pd.date_range("2026-09-18 09:15", periods=today_bars, freq="15min")
    price = base + gap
    for _ in t_idx:
        rows.append(dict(open=price, high=price + 20, low=price, close=price + 20, volume=1500.0))
        price += 20
    return pd.DataFrame(rows, index=y_idx.append(t_idx))


def _scanner(frames, *, signal_at, market_clock, wall_clock, mode="REAL"):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = mode
    scanner.active_indices = list(IDX)
    scanner._cached_results = {
        f"{inst}_15min": {"signals": [SimpleNamespace(signal_type="BUY", timestamp=signal_at)]}
        for inst in IDX
    }
    scanner.candles = SimpleNamespace(get_candles=lambda inst, tf: frames[inst].copy())
    scanner._session_squareoff_clock = lambda: market_clock
    scanner._entry_wall_clock = lambda: wall_clock
    return scanner


def _vote(scanner, signal_at):
    return Scanner._market_wide_reversal_context(scanner, "SENSEX", "15min", "BUY", signal_at)


# ── fault 1: a forming bar counted as closed ──────────────────────────────────


def test_a_forming_bar_does_not_become_closed_because_the_market_clock_runs_ahead():
    """Mid-session, so the session-bars rule cannot be what refuses it."""
    frames = {inst: _frame(7) for inst in IDX}          # 09:15 .. 10:45, 10:45 forming
    forming = datetime(2026, 9, 18, 10, 45)

    scanner = _scanner(
        frames,
        signal_at=forming,
        market_clock=datetime(2026, 9, 18, 11, 0),       # some frame advanced the clock
        wall_clock=datetime(2026, 9, 18, 10, 47, 30),     # the bar is 2.5 minutes old
    )

    assert _vote(scanner, forming) is None


def test_the_same_bar_votes_once_it_has_actually_closed():
    frames = {inst: _frame(7) for inst in IDX}
    bar = datetime(2026, 9, 18, 10, 45)
    closed = datetime(2026, 9, 18, 11, 0, 5)

    ctx = _vote(_scanner(frames, signal_at=bar, market_clock=closed, wall_clock=closed), bar)

    assert ctx is not None and ctx["core_votes"] == 3


def test_the_entry_clock_takes_the_earlier_of_host_and_market():
    frames = {inst: _frame(7) for inst in IDX}
    scanner = _scanner(
        frames,
        signal_at=datetime(2026, 9, 18, 10, 45),
        market_clock=datetime(2026, 9, 18, 11, 0),
        wall_clock=datetime(2026, 9, 18, 10, 47, 30),
    )

    metrics = Scanner._candle_thrust_metrics(
        scanner, "SENSEX", "15min", "BUY",
        as_of=pd.Timestamp("2026-09-18 10:45"), require_closed=True,
    )

    assert metrics is None


# ── fault 2: the opening auction bar scored against yesterday's ATR ───────────


def test_the_opening_bar_cannot_vote_even_after_it_closes():
    frames = {inst: _frame(1, gap=260.0) for inst in IDX}
    bar = datetime(2026, 9, 18, 9, 15)
    closed = datetime(2026, 9, 18, 9, 30, 5)

    assert _vote(_scanner(frames, signal_at=bar, market_clock=closed, wall_clock=closed), bar) is None


@pytest.mark.parametrize("bars_today, votes", [(2, False), (3, False), (4, True)])
def test_votes_resume_once_the_session_has_three_bars_of_its_own(bars_today, votes):
    frames = {inst: _frame(bars_today) for inst in IDX}
    bar = pd.Timestamp("2026-09-18 09:15") + pd.Timedelta(minutes=15 * (bars_today - 1))
    closed = (bar + pd.Timedelta(minutes=15, seconds=5)).to_pydatetime()

    ctx = _vote(
        _scanner(frames, signal_at=bar.to_pydatetime(), market_clock=closed, wall_clock=closed),
        bar.to_pydatetime(),
    )

    assert (ctx is not None) is votes


def test_the_session_bars_rule_is_the_one_refusing_the_open(monkeypatch):
    """With the rule off the same closed opening bar votes, so the test above is not vacuous."""
    monkeypatch.setattr(get_settings(), "market_wide_reversal_min_session_bars", 0, raising=False)
    frames = {inst: _frame(1, gap=260.0) for inst in IDX}
    bar = datetime(2026, 9, 18, 9, 15)
    closed = datetime(2026, 9, 18, 9, 30, 5)

    ctx = _vote(_scanner(frames, signal_at=bar, market_clock=closed, wall_clock=closed), bar)

    assert ctx is not None and ctx["core_votes"] == 3


def test_the_setting_ships_at_three():
    assert Settings().market_wide_reversal_min_session_bars == 3


# ── provenance ────────────────────────────────────────────────────────────────


def test_metrics_say_which_bar_they_scored_and_how_much_session_it_had():
    frames = {inst: _frame(5) for inst in IDX}
    closed = datetime(2026, 9, 18, 10, 30, 5)
    scanner = _scanner(frames, signal_at=datetime(2026, 9, 18, 10, 15), market_clock=closed, wall_clock=closed)

    metrics = Scanner._candle_thrust_metrics(
        scanner, "SENSEX", "15min", "BUY",
        as_of=pd.Timestamp("2026-09-18 10:15"), require_closed=True,
    )

    assert metrics["bar_timestamp"] == "2026-09-18 10:15:00"
    assert metrics["session_prior_bars"] == 4
    assert isinstance(metrics["bar_timestamp"], str), "the dict is JSON-serialised downstream"


def test_each_vote_records_its_bar():
    frames = {inst: _frame(5) for inst in IDX}
    bar = datetime(2026, 9, 18, 10, 15)
    closed = datetime(2026, 9, 18, 10, 30, 5)

    ctx = _vote(_scanner(frames, signal_at=bar, market_clock=closed, wall_clock=closed), bar)

    assert ctx is not None
    for vote in ctx["votes"].values():
        assert vote["bar_timestamp"] == "2026-09-18 10:15:00"
        assert vote["session_prior_bars"] == 4


# ── replay is untouched ───────────────────────────────────────────────────────


def test_historical_mode_still_votes_on_closed_bars():
    frames = {inst: _frame(5) for inst in IDX}
    bar = datetime(2026, 9, 18, 10, 15)
    # Replay passes the bar's availability (its close) as the signal clock.
    available = datetime(2026, 9, 18, 10, 30)
    scanner = _scanner(
        frames, signal_at=available, market_clock=datetime(2026, 9, 18, 15, 30),
        wall_clock=datetime(2026, 9, 18, 9, 0), mode="HISTORICAL",
    )
    for key in scanner._cached_results:
        scanner._cached_results[key]["signals"][0].signal_available_at = available

    assert _vote(scanner, available) is not None


# ── the live incident, on the recorded candles ────────────────────────────────


def _live_frames():
    frames = {}
    for inst in IDX:
        path = ROOT / "data_store" / "candles" / f"{inst}_1min.csv"
        if not path.exists():
            pytest.skip(f"{path.name} not present")
        df = pd.read_csv(path, parse_dates=[0], index_col=0)
        df = df[[c for c in ("open", "high", "low", "close", "volume") if c in df.columns]]
        df = df[df.index <= pd.Timestamp("2026-09-18 09:17")]
        if df.empty or df.index[-1] != pd.Timestamp("2026-09-18 09:17"):
            pytest.skip("recorded 2026-09-18 opening bars not present")
        frames[inst] = CandleBuilder._resample_ohlcv(df, "15min")
    return frames


def test_the_0917_sensex_entry_no_longer_gets_its_votes():
    frames = _live_frames()
    bar = datetime(2026, 9, 18, 9, 15)
    scanner = _scanner(
        frames, signal_at=bar,
        market_clock=datetime(2026, 9, 18, 9, 30),
        wall_clock=datetime(2026, 9, 18, 9, 17, 30),
    )

    assert _vote(scanner, bar) is None


def test_the_recorded_candles_do_reproduce_the_incident_without_the_fixes(monkeypatch):
    """Both guards off: the recorded data yields the market-wide BUY the trade was admitted on."""
    monkeypatch.setattr(get_settings(), "market_wide_reversal_min_session_bars", 0, raising=False)
    frames = _live_frames()
    bar = datetime(2026, 9, 18, 9, 15)
    ahead = datetime(2026, 9, 18, 9, 30)
    scanner = _scanner(frames, signal_at=bar, market_clock=ahead, wall_clock=ahead)

    ctx = _vote(scanner, bar)

    assert ctx is not None
    assert ctx["core_votes"] >= 2 and "SENSEX" in ctx["indices"]


# ── the diagnostic for the clock that ran ahead ───────────────────────────────


def test_the_builder_names_the_frame_holding_the_newest_stamp():
    builder = CandleBuilder.__new__(CandleBuilder)
    import threading
    builder._lock = threading.RLock()
    early = pd.DataFrame({"close": [1.0]}, index=[pd.Timestamp("2026-09-18 09:17")])
    ahead = pd.DataFrame({"close": [1.0]}, index=[pd.Timestamp("2026-09-18 09:30")])
    builder._candles_1min = {"SENSEX": early}
    builder._candles_5min = {}
    builder._candles_15min = {"ODD_FRAME": ahead}

    assert builder.get_max_timestamp_source() == ("ODD_FRAME", "15min", datetime(2026, 9, 18, 9, 30))


def test_a_clock_well_ahead_of_the_host_is_logged_once_a_minute(monkeypatch):
    import scanner as scanner_module

    lines = []
    monkeypatch.setattr(scanner_module.logger, "warning", lambda msg, *a, **k: lines.append(msg))
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(
        get_max_timestamp_source=lambda: ("ODD_FRAME", "15min", datetime(2026, 9, 18, 9, 30))
    )
    wall = datetime(2026, 9, 18, 9, 17, 30)

    Scanner._note_market_clock_ahead(scanner, datetime(2026, 9, 18, 9, 30), wall)
    Scanner._note_market_clock_ahead(scanner, datetime(2026, 9, 18, 9, 30), wall)
    Scanner._note_market_clock_ahead(scanner, datetime(2026, 9, 18, 9, 17, 50), wall)

    assert len(lines) == 1, "once per minute, and never for ordinary tick jitter"
    assert "ODD_FRAME" in lines[0] and "750s AHEAD" in lines[0]


def test_the_diagnostic_can_never_change_the_eod_clock(monkeypatch):
    """A failing diagnostic once dropped the advance-only clock back to the host clock."""
    import scanner as scanner_module
    from datetime import datetime as real_datetime

    class _Fixed(real_datetime):
        @classmethod
        def now(cls, tz=None):
            wall = cls(2026, 9, 18, 15, 16, 0)
            return tz.localize(wall) if tz is not None else wall

    monkeypatch.setattr(scanner_module, "datetime", _Fixed)

    def _boom(*_a, **_k):
        raise RuntimeError("diagnostic failure")

    subject = SimpleNamespace(
        candles=SimpleNamespace(get_max_timestamp=lambda: real_datetime(2026, 9, 18, 15, 20)),
        _note_market_clock_ahead=_boom,
    )

    observed = Scanner._session_squareoff_clock(subject)

    assert (observed.hour, observed.minute) == (15, 20)
