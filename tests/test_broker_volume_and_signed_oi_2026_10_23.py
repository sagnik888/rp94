import json
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from data.angelone_market_data import AngelOneIndexQuoteSource
from data.market_data import MarketDataProvider
from scripts.verify_kite_angel_parity import INDICES, compare_captures


@pytest.mark.parametrize("call_volume,put_volume", [(12345, 23456), (0, 0)])
def test_angel_option_pairs_preserve_full_quote_trade_volume(call_volume, put_volume):
    source = SimpleNamespace(connected=True, _api=object(), _lock=threading.RLock())
    source._load_option_master = lambda: {
        ("NFO", side): dict(exchange="NFO", symbol=side, token=side)
        for side in ("CE", "PE")
    }
    source._option_identity = lambda index, contract, side: dict(
        exchange="NFO", symbol=side, query="same-contract")
    source._contract_strike = AngelOneIndexQuoteSource._contract_strike
    source._number = AngelOneIndexQuoteSource._number
    source._option_volume = AngelOneIndexQuoteSource._option_volume
    source.get_full_quotes = lambda tokens, **kwargs: {
        ("NFO", "CE"): dict(ltp=10, opnInterest=100, tradeVolume=call_volume),
        ("NFO", "PE"): dict(ltp=11, opnInterest=110, tradeVolume=put_volume),
    }

    pairs = AngelOneIndexQuoteSource.get_option_pairs(source, "NIFTY", [
        (dict(strike=25000, expiry="2026-10-27"),
         dict(strike=25000, expiry="2026-10-27"))])

    assert len(pairs) == 1
    assert pairs[0]["CE"]["volume"] == call_volume
    assert pairs[0]["PE"]["volume"] == put_volume


def test_angel_touch_preserves_full_quote_trade_volume():
    source = SimpleNamespace(
        _option_identity=lambda *args: dict(exchange="NFO", symbol="OPTION"),
        _load_option_master=lambda: {("NFO", "OPTION"): dict(token="own-token")},
        get_full_quotes=lambda tokens: {("NFO", "own-token"): dict(
            exchange_at=1000, ltp=10, opnInterest=100, tradeVolume=12345,
            depth=dict(buy=[dict(price=9, quantity=10)], sell=[dict(price=11, quantity=20)]))},
    )
    provider = SimpleNamespace(
        angel_index=source,
        kite=SimpleNamespace(_lock=threading.RLock(), _instruments={"NFO": [dict(
            tradingsymbol="OPTION", instrument_type="CE", name="NIFTY")]}),
        _angel_contract=lambda contract: {},
        _replacement_epoch=lambda value: value,
        _touch_quote_from_payload=lambda payload, *args: payload,
    )

    quote = MarketDataProvider._replacement_touch(provider, "NFO", "OPTION", 1001, 5)

    assert quote["volume"] == 12345
    assert quote["bid"] == 9
    assert quote["ask"] == 11


@pytest.fixture
def captures(tmp_path):
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    history = dict(timestamp="2026-10-23T09:30:00+05:30", open=100, high=101,
                   low=99, close=100, volume=100, volume_complete=True,
                   volume_basis="constituent_shares", membership_version="fixture-v1")
    chain = dict(timestamp=history["timestamp"], expiry="2026-10-27", strike=25000,
                 call_ltp=10, put_ltp=11, call_oi=100, put_oi=200,
                 call_volume=10, put_volume=20, call_oi_change=0, put_oi_change=0)
    for folder in (baseline, candidate):
        folder.mkdir()
        for index in INDICES:
            for interval in ("1min", "5min", "15min"):
                pd.DataFrame([history]).to_csv(folder / f"{index}_{interval}.csv", index=False)
            pd.DataFrame([chain]).to_csv(folder / f"{index}_chain.csv", index=False)
        pd.DataFrame([dict(timestamp=history["timestamp"], ltp=15)]).to_csv(
            folder / "INDIAVIX.csv", index=False)
        (folder / "decisions.json").write_text(json.dumps([dict(
            timestamp=history["timestamp"], instrument="NIFTY", timeframe="5min",
            entry_eligible=False, selected_contract=None, management={}, exit=False)]))
    return baseline, candidate


def set_chain_values(folder, **values):
    path = folder / "NIFTY_chain.csv"
    pd.read_csv(path).assign(**values).to_csv(path, index=False)


@pytest.mark.parametrize("call,put", [(-10, -20), (-10, 20), (0, 0)])
def test_parity_accepts_equal_finite_signed_oi_changes(captures, call, put):
    for folder in captures:
        set_chain_values(folder, call_oi_change=call, put_oi_change=put)

    result = compare_captures(*captures)

    assert result["captured_field_parity"], result["failures"]
    assert result["cutover_authorized"] is False


@pytest.mark.parametrize("column,value", [
    ("call_oi_change", float("nan")),
    ("put_oi_change", float("inf")),
    ("call_oi_change", float("-inf")),
    ("call_oi", -1),
    ("put_volume", -1),
    ("call_ltp", -1),
])
def test_parity_still_rejects_invalid_values(captures, column, value):
    for folder in captures:
        set_chain_values(folder, **{column: value})

    result = compare_captures(*captures)

    assert not result["captured_field_parity"]
    assert result["failures"] == [dict(capture="NIFTY_chain.csv", reason=column + "_invalid")]


def test_parity_detects_different_negative_oi_changes(captures):
    set_chain_values(captures[0], call_oi_change=-10)
    set_chain_values(captures[1], call_oi_change=-11)

    result = compare_captures(*captures)

    assert not result["captured_field_parity"]
    assert result["failures"] == [dict(capture="NIFTY_chain.csv", reason="call_oi_change_differs")]
