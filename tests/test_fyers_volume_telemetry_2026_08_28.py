"""Fyers volume must be cumulative, provenance-aware, and merge-safe."""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from data.candle_builder import CandleBuilder
from data.market_data import MarketDataProvider
from intelligence.rtracker import RegimeTracker


def test_index_tick_does_not_promote_per_trade_quantity_to_day_volume():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    volume, key, fields = provider._extract_index_tick_volume(
        {"ltp": 100.0, "ltq": 37, "qty": 37},
        ("vol_traded_today", "volume_traded_today", "volume", "vol"),
    )
    assert volume == 0.0 and key == ""
    assert fields.get("qty") == 37.0
    assert fields.get("ltq") == 37.0


def test_volume_diagnostic_has_explicit_unavailable_fields_and_no_fake_age(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._index_tick_volume_diag = {}
    provider._record_index_tick_volume_diag(
        "NIFTY",
        "fyers_ws",
        0.0,
        "",
        {"ltq": 37.0},
        100.0,
    )
    monkeypatch.setattr("data.market_data.time.time", lambda: 150.0)

    row = provider.get_index_tick_volume_diagnostics()["NIFTY"]
    assert row["volume_available"] is False
    assert row["volume_source"] == ""
    assert row["volume_basis"] == ""
    assert row["volume_unit"] == ""
    assert row["volume_comparable"] is False
    assert row["volume_age_seconds"] is None
    assert row["volume_unavailable_reason"] == (
        "cumulative_volume_missing; per_trade_quantity_ignored"
    )


def test_repeated_frozen_positive_volume_does_not_refresh_its_age(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._index_tick_volume_diag = {}
    kwargs = {
        "volume_source": "fyers_index",
        "volume_basis": "cumulative_cash_index_volume",
        "volume_unit": "shares",
        "volume_comparable": True,
    }
    provider._record_index_tick_volume_diag(
        "NIFTY", "fyers_ws", 1000.0, "volume", {"volume": 1000.0}, 100.0, **kwargs
    )
    provider._record_index_tick_volume_diag(
        "NIFTY", "fyers_ws", 1000.0, "volume", {"volume": 1000.0}, 110.0, **kwargs
    )
    monkeypatch.setattr("data.market_data.time.time", lambda: 120.0)

    row = provider.get_index_tick_volume_diagnostics()["NIFTY"]
    assert row["volume_age_seconds"] == 20.0
    assert row["volume_comparable"] is True


def test_price_only_tick_preserves_last_positive_volume_and_original_clock(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._index_tick_volume_diag = {}
    provider._record_index_tick_volume_diag(
        "NIFTY",
        "fyers_ws",
        1000.0,
        "volume",
        {"volume": 1000.0},
        100.0,
        volume_source="fyers_index",
        volume_basis="cumulative_cash_index_volume",
        volume_unit="shares",
        volume_comparable=True,
    )
    provider._record_index_tick_volume_diag(
        "NIFTY", "fyers_ws", 0.0, "", {"ltq": 4.0}, 110.0
    )
    monkeypatch.setattr("data.market_data.time.time", lambda: 120.0)

    row = provider.get_index_tick_volume_diagnostics()["NIFTY"]
    assert row["volume_available"] is True
    assert row["volume"] == 1000.0
    assert row["volume_age_seconds"] == 20.0
    assert row["volume_source"] == "fyers_index"
    assert row["volume_comparable"] is True


def test_retained_positive_volume_becomes_explicitly_stale(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._index_tick_volume_diag = {}
    provider._record_index_tick_volume_diag(
        "NIFTY",
        "fyers_ws",
        1000.0,
        "volume",
        {"volume": 1000.0},
        100.0,
        volume_source="fyers_index",
        volume_basis="cumulative_cash_index_volume",
        volume_unit="shares",
        volume_comparable=True,
    )
    provider._record_index_tick_volume_diag(
        "NIFTY", "fyers_ws", 0.0, "", {}, 110.0
    )
    monkeypatch.setattr(
        "data.market_data.time.time",
        lambda: 100.0 + MarketDataProvider.FYERS_VOLUME_STALE_SECONDS + 1.0,
    )

    row = provider.get_index_tick_volume_diagnostics()["NIFTY"]
    assert row["volume_available"] is False
    assert row["volume_comparable"] is False
    assert row["volume_unavailable_reason"] == "cumulative_volume_stale"


def test_price_tick_time_cannot_make_unstamped_volume_online(monkeypatch):
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider._ws_cache = {
        "NSE:NIFTY50-INDEX": {"vol": 1000.0, "time": 999.0}
    }
    provider.fyers = None
    provider.kite = type("Kite", (), {"is_connected": False})()
    provider._volume_source_active = {}
    monkeypatch.setattr("data.market_data.time.time", lambda: 1000.0)

    result = provider.get_index_activity("NIFTY")
    assert result["fyers_online"] is False
    assert result["fyers_volume_age_seconds"] is None
    assert result["volume_available"] is False
    assert result["volume_comparable"] is False
    assert result["volume_unavailable_reason"] == "no_positive_cash_index_volume"


def test_fyers_history_has_distinct_provider_and_receipt_clocks():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider_time = datetime(2026, 8, 28, 9, 15)
    received_at = datetime(2026, 8, 28, 9, 20)
    frame = pd.DataFrame(
        {"open": [100], "high": [101], "low": [99], "close": [100], "volume": [500]},
        index=pd.DatetimeIndex([provider_time]),
    )
    result = provider._attach_fyers_history_provenance(frame, received_at=received_at)
    row = result.iloc[0]
    assert row["volume_basis"] == "fyers_index"
    assert row["source"] == "fyers_history"
    assert row["provider_time"] == provider_time
    assert row["receipt_at"] == received_at
    assert row["tick_count"] == 0


def test_history_merge_preserves_live_tick_telemetry():
    builder = CandleBuilder()
    idx = pd.DatetimeIndex([datetime(2026, 8, 28, 9, 15)])
    live = pd.DataFrame(
        {
            "open": [100], "high": [101], "low": [99], "close": [100], "volume": [12],
            "tick_count": [7], "volume_basis": ["fyers_index"], "source": ["live_tick"],
            "provider_time": [idx[0]], "receipt_at": [datetime(2026, 8, 28, 9, 15, 30)],
        }, index=idx,
    )
    history = MarketDataProvider._attach_fyers_history_provenance(
        live[["open", "high", "low", "close", "volume"]].assign(volume=[25]),
        received_at=datetime(2026, 8, 28, 9, 20),
    )
    builder.update_candles("NIFTY", live)
    builder.update_candles("NIFTY", history)
    row = builder.get_candles("NIFTY", "1min").iloc[0]
    assert row["volume"] == 25
    assert row["tick_count"] == 7
    assert row["source"] == "live_tick"
    assert row["receipt_at"] == datetime(2026, 8, 28, 9, 15, 30)


def test_rtracker_persists_complete_volume_semantics(tmp_path):
    tracker = RegimeTracker(tmp_path, enabled=False)
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="1min",
        direction="LONG",
        signal_timestamp=datetime(2026, 8, 28, 10, 0),
        market_intelligence_context={
            "volume": {
                "volume_available": False,
                "volume_source": "kite_index_futures",
                "volume_basis": "near_month_futures_confirmation",
                "volume_unit": "contracts",
                "volume_comparable": False,
                "volume_unavailable_reason": (
                    "cash_index_volume_unavailable; futures_confirmation_only"
                ),
            }
        },
    )

    event = tracker._event_from_candidate(
        candidate,
        decision="REJECTED",
        reason="volume unmeasurable",
        source="test",
        extra=None,
    )
    assert event["volume"] == {
        "signal": "",
        "available": False,
        "source": "kite_index_futures",
        "basis": "near_month_futures_confirmation",
        "unit": "contracts",
        "comparable": False,
        "unavailable_reason": (
            "cash_index_volume_unavailable; futures_confirmation_only"
        ),
        "volume_ratio": None,
        "current_volume": None,
        "buy_sell_ratio": None,
    }
