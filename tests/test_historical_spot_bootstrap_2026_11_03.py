"""Historical analysis uses OHLC references without touching live quote state."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from data.candle_builder import CandleBuilder
from engine import entry_analysis_service as service_module
from engine import ut_ema_lane
from tests.test_live_spot_bootstrap_deadline_2026_10_05 import _coordinator, _host


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"])
async def test_historical_zero_quote_never_bootstraps_broker_ltp(monkeypatch, name):
    lookup = Mock(side_effect=AssertionError("historical analysis requested live LTP"))
    host = _host(lookup, {name: 0.0})
    host.mode = "HISTORICAL"
    host.candles.update_latest_price = Mock()
    coordinator = _coordinator(monkeypatch, host)
    with ThreadPoolExecutor(max_workers=1) as pool:
        host._scanner_live_executor = pool
        _, result = await coordinator(name, {})
    assert result == ({"processed": name}, [])
    assert host.calls == [name]
    lookup.assert_not_called()
    host.candles.update_latest_price.assert_not_called()
    assert host._live_processing_futures == host._live_entry_data_blocks == {}


def _frame(close):
    return pd.DataFrame({"close": [close]}, index=pd.to_datetime(["2026-10-30 15:29"]))


def _service_host(monkeypatch, frames, quote=0.0):
    candles = CandleBuilder()
    for timeframe, frame in frames.items():
        getattr(candles, f"_candles_{timeframe}")["SENSEX"] = frame
    if quote:
        candles.update_latest_price("SENSEX", quote)
    candles._last_candle_receipt = {"SENSEX": {"1min": datetime(2026, 10, 30, 15, 30)}}
    candles.update_latest_price = Mock(wraps=candles.update_latest_price)
    host = SimpleNamespace(
        mode="HISTORICAL", user_lots={}, capital_fut=100000., capital_opt=100000.,
        risk_fut_pct=.2, candles=candles,
        data=SimpleNamespace(
            get_cached_index_quote_snapshot=Mock(side_effect=AssertionError("live quote read")),
            get_index_quote=Mock(side_effect=AssertionError("broker quote read")),
            get_cached_previous_close=lambda name: 0.0),
        _prepare_live_signal_data=lambda name, timeframe, data: data,
        mtf=SimpleNamespace(update_data=Mock(), process_instrument=Mock(return_value=SimpleNamespace())),
        _cached_results={}, _cached_result_metadata={}, _record_scan_phase_ms=Mock(),
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: SimpleNamespace(scanner_engine="ut_ema"))
    view = Mock(side_effect=lambda scanner, name, cfg, mtf, spot, *args: ({"ltp": spot}, []))
    monkeypatch.setattr(ut_ema_lane, "analysis_view", view)
    return host, view


@pytest.mark.parametrize("frames, expected", [
    ({"1min": _frame(81001.), "5min": _frame(81005.), "15min": _frame(81015.)}, 81001.),
    ({"5min": _frame(81005.), "15min": _frame(81015.)}, 81005.),
    ({"1min": _frame(float("nan")), "5min": _frame(float("inf")), "15min": _frame(81015.)}, 81015.),
    ({"1min": pd.DataFrame(), "5min": _frame(81005.)}, 81005.),
])
@pytest.mark.parametrize("quote", [0.0, 99999.0])
def test_historical_cached_close_reaches_native_analysis_without_quote_writes(monkeypatch, frames, expected, quote):
    host, view = _service_host(monkeypatch, frames, quote)
    prices = dict(host.candles._latest_prices)
    quote_receipts = dict(host.candles._last_quote_receipt)
    candle_receipts = {name: dict(values) for name, values in host.candles._last_candle_receipt.items()}
    originals = {timeframe: frame.copy(deep=True) for timeframe, frame in frames.items()}
    result = service_module.EntryAnalysisService(host).process("SENSEX", {})
    assert result == ({"ltp": expected}, [])
    view.assert_called_once()
    host.candles.update_latest_price.assert_not_called()
    assert host.candles._latest_prices == prices
    assert host.candles._last_quote_receipt == quote_receipts
    assert host.candles._last_candle_receipt == candle_receipts
    host.data.get_cached_index_quote_snapshot.assert_not_called()
    host.data.get_index_quote.assert_not_called()
    for timeframe, original in originals.items():
        pd.testing.assert_frame_equal(host.candles.get_candles("SENSEX", timeframe), original)


@pytest.mark.parametrize("frames", [
    {},
    {"1min": pd.DataFrame()},
    {"1min": pd.DataFrame({"open": [81000.]})},
    {"1min": _frame(None), "5min": _frame(float("nan")), "15min": _frame(float("inf"))},
    {"1min": _frame(0.), "5min": _frame(-1.), "15min": _frame("invalid")},
])
def test_historical_empty_or_invalid_cache_abstains_even_with_current_quote(monkeypatch, frames):
    host, view = _service_host(monkeypatch, frames, quote=99999.)
    prices = dict(host.candles._latest_prices)
    quote_receipts = dict(host.candles._last_quote_receipt)
    result, candidates = service_module.EntryAnalysisService(host).process("SENSEX", {})
    assert "Invalid spot price" in result.get("error", "")
    assert candidates == []
    host.mtf.process_instrument.assert_not_called()
    view.assert_not_called()
    host.candles.update_latest_price.assert_not_called()
    assert host.candles._latest_prices == prices
    assert host.candles._last_quote_receipt == quote_receipts
