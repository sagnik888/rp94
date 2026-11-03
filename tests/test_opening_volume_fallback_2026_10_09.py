"""Opening volume: a closed Kite futures bar stands in until Fyers cash volume arrives (2026-10-09)."""
import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from config.settings import get_settings
from data.futures_bar_volume import BASIS, FuturesBarVolume

NOW = pd.Timestamp("2026-10-09 09:30:04", tz="Asia/Kolkata")
NOW_EPOCH = NOW.timestamp()


class FakeKite:
    def __init__(self, frame=None, expiries=(date(2026, 10, 27), date(2026, 11, 24)), fail=False, delay=0.0):
        self._lock = threading.Lock()
        self._instruments = {"NFO": [
            {"name": "NIFTY", "instrument_type": "FUT", "expiry": e,
             "tradingsymbol": f"NIFTY{e:%y%b}FUT".upper(), "instrument_token": 100 + i}
            for i, e in enumerate(expiries)]}
        self.frame = frame
        self.fail = fail
        self.delay = delay
        self.calls = []

    def get_historical(self, token, interval, days_back=15, record_health=True):
        self.calls.append((token, interval))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("kite down")
        return self.frame.copy() if self.frame is not None else pd.DataFrame()


def _frame(minutes=15, last_start="2026-10-09 09:15", bars=30, base=100.0, last=400.0):
    end = pd.Timestamp(last_start, tz="Asia/Kolkata")
    index = pd.date_range(end=end, periods=bars, freq=f"{minutes}min")
    volume = [base] * (bars - 1) + [last]
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": volume}, index=index)


def _source(kite, **kwargs):
    return FuturesBarVolume(kite, clock=lambda: NOW_EPOCH, **kwargs)


def test_closed_signal_bar_is_measured_against_its_previous_bars():
    kite = FakeKite(_frame())
    result = _source(kite).evidence("NIFTY", "15min", "2026-10-09 09:15:00")
    assert result["available"] and result["basis"] == BASIS
    assert result["ratio"] == pytest.approx(4.0)
    assert result["baseline_bars"] == 20 and result["tradingsymbol"] == "NIFTY26OCTFUT"


def test_signal_time_given_as_bar_close_measures_the_bar_that_closed():
    result = _source(FakeKite(_frame())).evidence("NIFTY", "15min", "2026-10-09 09:30:00")
    assert result["available"] and result["bar_start"].startswith("2026-10-09T09:15")


def test_forming_or_stale_bars_are_never_measured():
    forming = _frame(last_start="2026-10-09 09:30")
    assert not _source(FakeKite(forming)).evidence("NIFTY", "15min", "2026-10-09 09:31:00")["available"]
    stale = _source(FakeKite(_frame())).evidence("NIFTY", "15min", "2026-10-09 08:15:00")
    assert not stale["available"] and "stale" in stale["reason"]


def test_thin_baseline_or_empty_bar_is_unavailable():
    assert not _source(FakeKite(_frame(bars=6))).evidence("NIFTY", "15min", "2026-10-09 09:15")["available"]
    assert not _source(FakeKite(_frame(last=0.0))).evidence("NIFTY", "15min", "2026-10-09 09:15")["available"]


def test_rolls_to_next_month_inside_the_rollover_window():
    kite = FakeKite(_frame(), expiries=(date(2026, 10, 10), date(2026, 11, 24)))
    assert _source(kite).contract("NIFTY", date(2026, 10, 9))["tradingsymbol"] == "NIFTY26NOVFUT"
    assert _source(FakeKite()).contract("NIFTY", date(2026, 10, 9))["tradingsymbol"] == "NIFTY26OCTFUT"


def test_history_is_cached_and_failures_back_off():
    kite = FakeKite(_frame())
    source = _source(kite)
    source.evidence("NIFTY", "15min", "2026-10-09 09:15")
    source.evidence("NIFTY", "15min", "2026-10-09 09:15")
    assert len(kite.calls) == 1
    failing = FakeKite(fail=True)
    source = _source(failing)
    assert not source.evidence("NIFTY", "15min", "2026-10-09 09:15")["available"]
    assert not source.evidence("NIFTY", "15min", "2026-10-09 09:15")["available"]
    assert len(failing.calls) == 1


def test_slow_broker_is_capped():
    source = FuturesBarVolume(FakeKite(_frame(), delay=1.0), clock=lambda: NOW_EPOCH)
    started = time.monotonic()
    result = source.evidence_within("NIFTY", "15min", "2026-10-09 09:15", timeout=0.2)
    assert not result["available"] and "timed out" in result["reason"]
    assert time.monotonic() - started < 0.9


def _scanner(kite, monkeypatch, *, enabled=True, mode="REAL", cash=None):
    from scanner import Scanner
    settings = get_settings()
    monkeypatch.setattr(settings, "volume_futures_fallback_enabled", enabled)
    sc = Scanner.__new__(Scanner)
    sc.mode = mode
    sc.data = NS(kite=kite)
    sc._futures_bar_volume = FuturesBarVolume(kite, clock=lambda: NOW_EPOCH)
    volume = cash or {"available": False, "measurement_basis": "UNAVAILABLE", "volume_ratio": 1.0}
    sc._candidate_market_intelligence_context = lambda c: {"volume": volume}
    sc._candidate_15m_trend_agrees = lambda c: True
    sc._get_trade_regime = lambda c, default="UNKNOWN": "VOLATILE"
    return sc


def _candidate(**extra):
    return NS(instrument="NIFTY", timeframe="15min", direction="LONG", inst_type="OPT",
              signal_timestamp=pd.Timestamp("2026-10-09 09:15"), grade="A", confidence=0.75,
              adx_value=40.0, source_engine="quad_prime", source_engines=["quad_prime"],
              action="ENTRY", data_provenance="LIVE", **extra)


def test_unmeasured_cash_volume_borrows_the_futures_bar(monkeypatch):
    sc = _scanner(FakeKite(_frame()), monkeypatch)
    candidate = _candidate()
    verdict = sc._quality_verdict_for_candidate(candidate, regime="VOLATILE", risky_regime=True)
    assert verdict.checks["volume_basis"] == BASIS
    assert verdict.checks["volume_confirmed"] is True
    assert candidate.volume_futures_fallback["ratio"] == pytest.approx(4.0)


@pytest.mark.parametrize("variant", ["disabled", "historical", "measured_cash", "exit"])
def test_fallback_stays_out_of_its_way(monkeypatch, variant):
    kite = FakeKite(_frame())
    cash = {"available": True, "measurement_basis": "EXCHANGE_VOLUME", "volume_ratio": 0.6} \
        if variant == "measured_cash" else None
    sc = _scanner(kite, monkeypatch, enabled=variant != "disabled",
                  mode="HISTORICAL" if variant == "historical" else "REAL", cash=cash)
    candidate = _candidate()
    if variant == "exit":
        candidate.action = "EXIT"
    verdict = sc._quality_verdict_for_candidate(candidate, regime="VOLATILE", risky_regime=True)
    assert verdict.checks["volume_basis"] != BASIS
    assert verdict.checks["volume_confirmed"] is False
    assert kite.calls == []
