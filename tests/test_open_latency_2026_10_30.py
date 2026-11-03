"""Avoid repeated historical replay and duplicate failing broker fallback calls."""
from unittest.mock import Mock

import pandas as pd

from engine.ut_ema_engine import UTEmaEngine
from tests.test_three_broker_priority_2026_10_23 import three


def test_same_closed_history_is_computed_once_and_corrections_invalidate(monkeypatch):
    import engine.ut_ema_engine as native
    frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100.),
                         index=pd.date_range("2026-10-29 09:15", periods=50, freq="5min"))
    compute = Mock(wraps=native.compute_indicators)
    monkeypatch.setattr(native, "compute_indicators", compute)
    engine = UTEmaEngine()
    first = engine.process(frame, "NIFTY", "5min", as_of="2026-10-29 15:00", realtime=False)
    second = engine.process(frame, "NIFTY", "5min", as_of="2026-10-29 15:00:01", realtime=False)
    assert compute.call_count == 1
    second["state"]["current_price"] = -1
    assert first["state"]["current_price"] == 100.
    frame.iloc[10, frame.columns.get_loc("close")] = 100.5
    engine.process(frame, "NIFTY", "5min", as_of="2026-10-29 15:00:02", realtime=False)
    assert compute.call_count == 2


def test_rate_limited_fyers_history_is_not_retried_in_same_request(tmp_path, monkeypatch):
    import data.market_data as market
    provider = three(tmp_path)
    provider._request_replacement_history = lambda *a, **k: pd.DataFrame()
    provider.fyers = Mock()
    response = dict(s="error", code=-360, message="Standard plan limit of 50 requests/minute")
    provider._call_bounded = Mock(return_value=(True, response))
    monkeypatch.setattr(market.yf, "Ticker", lambda *a: Mock(history=lambda **k: pd.DataFrame()))
    provider.get_historical_candles("1", "NSE", "1min", instrument_name="NIFTY", persist_cache=False)
    assert provider._call_bounded.call_count == 1
    # The limit is provider-wide; another index must not immediately hammer it.
    provider.get_historical_candles("2", "NSE", "1min", instrument_name="BANKNIFTY", persist_cache=False)
    assert provider._call_bounded.call_count == 1


def test_repeated_chart_replay_uses_same_cache(monkeypatch):
    import engine.ut_ema_engine as native
    frame = pd.DataFrame(dict(open=100., high=101., low=99., close=100.),
                         index=pd.date_range("2026-10-29 09:15", periods=20, freq="5min"))
    compute = Mock(wraps=native.compute_indicators)
    monkeypatch.setattr(native, "compute_indicators", compute)
    engine = UTEmaEngine()
    engine.chart_result(frame, "NIFTY", "5min", as_of="2026-10-29 15:00")
    engine.chart_result(frame, "NIFTY", "5min", as_of="2026-10-29 15:00:01")
    assert compute.call_count == 1


def test_open_has_no_missing_completed_bar_before_first_minute(tmp_path, monkeypatch):
    import data.market_data as market
    from datetime import datetime
    class Opening(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 30, 9, 15, 30)
    monkeypatch.setattr(market, "datetime", Opening)
    monkeypatch.chdir(tmp_path)
    provider = three(tmp_path)
    preferred = pd.DataFrame(dict(open=100., high=101., low=99., close=100., volume=float("nan")),
                             index=pd.DatetimeIndex(["2026-10-29 15:29"]))
    provider._request_replacement_history = lambda *a, **k: preferred
    yahoo = Mock()
    monkeypatch.setattr(market.yf, "Ticker", yahoo)
    provider.get_historical_candles("1", "NSE", "1min", instrument_name="NIFTY", persist_cache=False)
    yahoo.assert_not_called()


def test_yahoo_cannot_overwrite_preferred_broker_prices(tmp_path, monkeypatch):
    import data.market_data as market
    from datetime import datetime
    class DuringSession(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 30, 10, 0)
    # The fallback must run after the session's first candle closes. Wall-clock
    # execution before 09:16 otherwise tests the separate pre-open abstention.
    monkeypatch.setattr(market, "datetime", DuringSession)
    monkeypatch.chdir(tmp_path)
    provider = three(tmp_path)
    preferred = pd.DataFrame(dict(open=100., high=101., low=99., close=100., volume=float("nan")),
                             index=pd.DatetimeIndex(["2026-10-28 15:29"]))
    provider._request_replacement_history = lambda *a, **k: preferred
    yahoo = pd.DataFrame(dict(open=[200.,201.], high=[201.,202.], low=[199.,200.],
                              close=[200.,201.], volume=0.),
                         index=pd.DatetimeIndex(["2026-10-28 15:29", "2026-10-29 15:29"]))
    monkeypatch.setattr(market.yf, "Ticker", lambda *a: Mock(history=lambda **k: yahoo))
    result = provider.get_historical_candles("1", "NSE", "1min", instrument_name="NIFTY", persist_cache=False)
    assert result.loc[preferred.index[0], "close"] == 100.
    assert result.loc[yahoo.index[1], "close"] == 201.
