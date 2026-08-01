"""Kite market data: contracts are looked up, never invented, and depth is real.

Both properties fix live 2026-07-31 defects:

  * The AngelOne path built option symbols by string formatting, so it kept requesting
    SENSEX26JUL77700CE after that expiry had died — 887 AB4046 "symbol token not found"
    errors in one session. Resolution against Kite's contract master makes an expired
    or non-existent contract unrepresentable: resolve_option() returns None instead.
  * Every option entry logged bid=0.00 ask=0.00 reason=missing_bid_ask and fell back to
    a blind LTP mark-up. Kite quotes carry depth, and get_depth() must distinguish
    "no book" from "book says zero".
"""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from data.kite_market_data import KiteDataSource


def _instrument(**over):
    row = {
        "instrument_token": 12345, "tradingsymbol": "NIFTY2680424350CE", "name": "NIFTY",
        "expiry": date(2026, 8, 4), "strike": 24350.0, "instrument_type": "CE",
        "exchange": "NFO", "lot_size": 65,
    }
    row.update(over)
    return row


class _Kite:
    def __init__(self, instruments=None, ltp=None, quote=None, historical=None):
        self._instruments = instruments if instruments is not None else {}
        self._ltp = ltp or {}
        self._quote = quote or {}
        self._historical = historical

    def instruments(self, exchange=None):
        value = self._instruments.get(exchange, [])
        if isinstance(value, Exception):
            raise value
        return value

    def ltp(self, keys):
        return {k: v for k, v in self._ltp.items() if k in keys}

    def quote(self, keys):
        return {k: v for k, v in self._quote.items() if k in keys}

    def historical_data(self, token, start, end, interval, oi=False):
        if isinstance(self._historical, Exception):
            raise self._historical
        return self._historical or []


def _source(kite):
    src = KiteDataSource(kite=kite)
    return src


# ── contract master ──────────────────────────────────────────────────────────
def test_option_is_resolved_from_the_contract_master():
    src = _source(_Kite(instruments={"NFO": [_instrument()], "BFO": [], "NSE": [], "BSE": []}))
    src.load_instruments()
    row = src.resolve_option("NIFTY", 24350, "CE", expiry=date(2026, 8, 4))
    assert row["tradingsymbol"] == "NIFTY2680424350CE"
    assert row["instrument_token"] == 12345


def test_a_contract_that_does_not_exist_returns_none_not_a_guessed_symbol():
    """The AngelOne path would have happily constructed a symbol for this."""
    src = _source(_Kite(instruments={"NFO": [_instrument()], "BFO": [], "NSE": [], "BSE": []}))
    src.load_instruments()
    assert src.resolve_option("NIFTY", 99999, "CE", expiry=date(2026, 8, 4)) is None


def test_expired_contracts_are_never_offered():
    """SENSEX26JUL77700CE lived on in the AngelOne path for a whole session."""
    dead = _instrument(expiry=date(2026, 7, 28), tradingsymbol="NIFTY2672824350CE")
    live = _instrument(expiry=date(2026, 8, 4))
    src = _source(_Kite(instruments={"NFO": [dead, live], "BFO": [], "NSE": [], "BSE": []}))
    src.load_instruments()
    expiries = src.expiries_for("NIFTY", on_or_after=date(2026, 7, 31))
    assert expiries == [date(2026, 8, 4)]
    # With no explicit expiry it must pick the nearest LIVE one, never the dead one.
    assert src.resolve_option("NIFTY", 24350, "CE", on_or_after=date(2026, 7, 31))["expiry"] == date(2026, 8, 4)


def test_nearest_live_expiry_is_chosen_by_default():
    near = _instrument(expiry=date(2026, 8, 4))
    far = _instrument(expiry=date(2026, 8, 11), tradingsymbol="NIFTY2681124350CE", instrument_token=999)
    src = _source(_Kite(instruments={"NFO": [far, near], "BFO": [], "NSE": [], "BSE": []}))
    src.load_instruments()
    assert src.resolve_option("NIFTY", 24350, "CE", on_or_after=date(2026, 7, 31))["expiry"] == date(2026, 8, 4)


def test_a_failed_exchange_dump_keeps_the_previous_cache():
    """A partial master reads exactly like 'this contract does not exist'."""
    good = _Kite(instruments={"NFO": [_instrument()], "BFO": [], "NSE": [], "BSE": []})
    src = _source(good)
    assert src.load_instruments() == 1
    src._kite = _Kite(instruments={"NFO": RuntimeError("dump down"), "BFO": [], "NSE": [], "BSE": []})
    src.load_instruments(force=True)
    assert src.resolve_option("NIFTY", 24350, "CE", expiry=date(2026, 8, 4)) is not None


def test_sensex_options_are_looked_up_on_bfo():
    row = _instrument(name="SENSEX", tradingsymbol="SENSEX2680678100CE", strike=78100.0, exchange="BFO")
    src = _source(_Kite(instruments={"NFO": [], "BFO": [row], "NSE": [], "BSE": []}))
    src.load_instruments()
    assert src.resolve_option("SENSEX", 78100, "CE", expiry=date(2026, 8, 4))["exchange"] == "BFO"


def test_instrument_token_lookup_by_symbol():
    src = _source(_Kite(instruments={"NFO": [_instrument()], "BFO": [], "NSE": [], "BSE": []}))
    src.load_instruments()
    assert src.instrument_token("NIFTY2680424350CE", "NFO") == 12345
    assert src.instrument_token("NOPE", "NFO") is None


# ── quotes and depth ─────────────────────────────────────────────────────────
def test_depth_returns_a_real_book():
    quote = {"NFO:NIFTY2680424350CE": {
        "last_price": 124.45,
        "depth": {"buy": [{"price": 124.0, "quantity": 150}], "sell": [{"price": 124.9, "quantity": 75}]},
    }}
    depth = _source(_Kite(quote=quote)).get_depth("NFO", "NIFTY2680424350CE")
    assert depth["bid"] == 124.0 and depth["ask"] == 124.9
    assert depth["usable"] is True


def test_no_book_is_distinguished_from_a_zero_book():
    """'missing_bid_ask' silently became a blind LTP mark-up on every 31 Jul entry."""
    depth = _source(_Kite(quote={})).get_depth("NFO", "NIFTY2680424350CE")
    assert depth["usable"] is False and depth["reason"] == "quote unavailable"

    crossed = {"NFO:X": {"depth": {"buy": [{"price": 0}], "sell": [{"price": 0}]}}}
    depth = _source(_Kite(quote=crossed)).get_depth("NFO", "X")
    assert depth["usable"] is False and "book" in depth["reason"]


def test_book_far_from_the_last_trade_is_refused():
    """Observed live on 2026-08-01, and it passed the original bid>0/ask>0 check.

    MIDCPNIFTY quoted ltp=421.55 against bid=521.90 / ask=563.35 — a bid 24% ABOVE the
    last trade. Anchoring a marketable limit to that ask pays ~33% over last traded.
    """
    quote = {"NFO:MIDCPNIFTY26AUG14325CE": {
        "last_price": 421.55,
        "depth": {"buy": [{"price": 521.90}], "sell": [{"price": 563.35}]},
    }}
    depth = _source(_Kite(quote=quote)).get_depth("NFO", "MIDCPNIFTY26AUG14325CE")
    assert depth["usable"] is False
    assert "stale quote" in depth["reason"]
    # The prices are still reported — the caller may want them for diagnostics.
    assert depth["bid"] == 521.90


@pytest.mark.parametrize(
    "bid,ask,last,usable",
    [
        (474.05, 479.05, 475.05, True),    # real NIFTY book
        (1440.0, 1462.25, 1446.0, True),   # real BANKNIFTY book
        (521.90, 563.35, 421.55, False),   # real MIDCPNIFTY book — nonsense
        (0.0, 0.0, 1180.0, False),         # real SENSEX — no book
        (100.0, 90.0, 95.0, False),        # crossed
        (10.0, 30.0, 20.0, False),         # spread too wide to execute against
    ],
)
def test_book_usability_matches_observed_live_quotes(bid, ask, last, usable):
    assert KiteDataSource._book_is_usable(bid, ask, last)[0] is usable


def test_ltp_reads_and_rejects_zero():
    src = _source(_Kite(ltp={"NFO:NIFTY2680424350CE": {"last_price": 124.45}}))
    assert src.get_ltp("NFO", "NIFTY2680424350CE") == 124.45
    assert _source(_Kite(ltp={"NFO:X": {"last_price": 0}})).get_ltp("NFO", "X") is None


def test_batch_ltp_returns_a_keyed_map():
    src = _source(_Kite(ltp={"NFO:A": {"last_price": 1.5}, "NFO:B": {"last_price": 2.5}}))
    assert src.get_ltps([("NFO", "A"), ("NFO", "B")]) == {"NFO:A": 1.5, "NFO:B": 2.5}


# ── historical ───────────────────────────────────────────────────────────────
def test_historical_returns_an_indexed_ohlcv_frame():
    rows = [
        {"date": datetime(2026, 7, 31, 9, 15), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
        {"date": datetime(2026, 7, 31, 9, 20), "open": 1.5, "high": 3, "low": 1, "close": 2.5, "volume": 200},
    ]
    frame = _source(_Kite(historical=rows)).get_historical(12345, "5min", days_back=2)
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert len(frame) == 2 and frame["close"].iloc[-1] == 2.5


def test_historical_failure_returns_empty_never_partial():
    frame = _source(_Kite(historical=RuntimeError("api down"))).get_historical(12345, "5min")
    assert isinstance(frame, pd.DataFrame) and frame.empty


def test_unsupported_interval_is_refused():
    assert _source(_Kite(historical=[])).get_historical(12345, "7min").empty


def _chain_source():
    rows = []
    for strike in (24300.0, 24350.0, 24400.0):
        for kind in ("CE", "PE"):
            rows.append(_instrument(
                tradingsymbol=f"NIFTY26804{int(strike)}{kind}",
                strike=strike, instrument_type=kind,
                instrument_token=int(strike) + (1 if kind == "CE" else 2),
            ))
    quote = {}
    oi = {24300.0: (5065970, 10366265), 24350.0: (4128150, 6011070), 24400.0: (8956025, 6411665)}
    for strike, (coi, poi) in oi.items():
        quote[f"NFO:NIFTY26804{int(strike)}CE"] = {"oi": coi, "last_price": 124.45, "volume": 100}
        quote[f"NFO:NIFTY26804{int(strike)}PE"] = {"oi": poi, "last_price": 56.0, "volume": 90}
    src = _source(_Kite(instruments={"NFO": rows, "BFO": [], "NSE": [], "BSE": []}, quote=quote))
    src.load_instruments()
    return src


def test_option_chain_emits_the_columns_the_intelligence_layer_reads():
    chain = _chain_source().get_option_chain("NIFTY", spot=24350.0, strikes_each_side=5)
    assert set(["strike", "call_oi", "put_oi", "call_volume", "put_volume",
                "call_ltp", "put_ltp"]).issubset(chain.columns)
    assert len(chain) == 3
    assert chain["call_oi"].sum() == 18150145


def test_pcr_is_computed_from_real_open_interest():
    """The Fyers path reported PCR=0 on a 123-row chain; this must produce a number."""
    src = _chain_source()
    chain = src.get_option_chain("NIFTY", spot=24350.0)
    assert src.put_call_ratio(chain) == pytest.approx(22788999 / 18150145, abs=0.001)
    assert src.put_call_ratio(pd.DataFrame()) == 0.0


def test_oi_change_is_left_at_zero_with_stated_provenance():
    """Kite reports an OI LEVEL, not a delta. Inventing one would feed the OI signal."""
    chain = _chain_source().get_option_chain("NIFTY", spot=24350.0)
    assert (chain["call_oi_change"] == 0).all()
    assert (chain["put_oi_change"] == 0).all()
    assert chain.attrs["oi_change_source"] == "kite_quote_levels_only"


def test_option_chain_is_empty_when_no_live_expiry_exists():
    src = _source(_Kite(instruments={"NFO": [], "BFO": [], "NSE": [], "BSE": []}))
    src.load_instruments()
    assert src.get_option_chain("NIFTY", spot=24350.0).empty


def test_no_session_degrades_quietly():
    src = KiteDataSource(kite=None)
    assert src.is_connected is False
    assert src.get_ltp("NFO", "X") is None
    assert src.get_historical(1, "5min").empty
