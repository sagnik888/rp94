"""A broker-orphan row rendered as a phantom pre-market trade.

Reported from the live dashboard:

    INSTR   TYPE   DIR      ENTRY   LTP     P&L       CONF/TF   ENTRY/EXIT
    NIFTY   25CE   BUY CE   --      78.80   Rs.608.4  0% --     --  --:--:--

A P&L on a row with no entry price, no timestamps and a strike of "25" reads as an
invented trade. The position is REAL — it is a genuine broker position with no matching
local trade, which is what "broker orphan" means — but three separate defects made it
look fabricated.

1. THE STRIKE WAS THE EXPIRY YEAR. _serialize_orphan_position set atm_strike to a hard
   0.0, which is FALSY in JS, so trade-table.js fell through to its fallback: the FIRST
   run of digits in the tradingsymbol. On a monthly symbol that is the year.
       NIFTY25AUG24350CE -> "25"     (real strike 24350)
       NIFTY26AUG24350CE -> "26"
   Weeklies were wrong differently — NIFTY2681824350CE -> "2681824350".

2. ENTRY WAS BLANK WHILE P&L WAS NOT. The row read buyavgprice/sellavgprice, which Kite
   reports as 0 for a position CARRIED from a previous session — the buy did not happen
   today. Meanwhile `unrealised` IS a real Kite field, so the P&L populated. Blank cost
   beside a live number is what made it look invented. `average_price` is the net
   position's own average and is populated in exactly that case.

3. LTP WAS ALWAYS ZERO. The row read position["ltp"], the AngelOne name. Kite sends
   last_price, and get_positions mapped buyavgprice/sellavgprice/producttype/netqty but
   never this one.

Why it matters beyond cosmetics: can_open_trade (trade_manager.py:4951) refuses EVERY
entry while any orphan is present. A row nobody can interpret is therefore also the row
that silently stops the session from trading.
"""
import json
import pathlib
import re
import threading

import pytest

from trading.kite_broker import KiteBroker
from trading.trade_dashboard import _serialize_orphan_position, option_strike_from_symbol


class _Mgr:
    @staticmethod
    def _position_quantity(position):
        return int(position.get("netqty") or position.get("quantity") or 0)


def _carried_position(symbol="NIFTY25AUG24350CE"):
    """Kite's shape for a position carried from a previous session."""
    return {
        "tradingsymbol": symbol,
        "quantity": 65, "netqty": 65,
        "buy_price": 0.0, "sell_price": 0.0,   # no trade happened today
        "average_price": 69.44,
        "last_price": 78.80,
        "unrealised": 608.40,
        "product": "NRML",
    }


# ── 1. the strike parser ─────────────────────────────────────────────────────

@pytest.mark.parametrize("symbol,strike", [
    ("NIFTY26AUG24350CE", 24350.0),        # monthly
    ("NIFTY25AUG24350CE", 24350.0),        # monthly, prior year — the reported row
    ("NIFTY2681824350CE", 24350.0),        # weekly, single-digit month
    ("NIFTY26O0724350PE", 24350.0),        # weekly, October is "O"
    ("BANKNIFTY26AUG57500PE", 57500.0),    # must not be read as NIFTY
    ("MIDCPNIFTY26AUG14975CE", 14975.0),
    ("SENSEX26AUG77800CE", 77800.0),
    ("BANKEX26AUG62000PE", 62000.0),
])
def test_the_strike_is_read_not_guessed(symbol, strike):
    assert option_strike_from_symbol(symbol) == strike


@pytest.mark.parametrize("symbol", ["360ONE26AUG1160CE", "RELIANCE26AUG1400PE"])
def test_a_stock_option_returns_zero_rather_than_a_guess(symbol):
    """This system trades index derivatives only; 0.0 is the honest answer."""
    assert option_strike_from_symbol(symbol) == 0.0


@pytest.mark.parametrize("symbol", ["", None, "NIFTY26AUGFUT", "GARBAGE", "NIFTY"])
def test_an_unreadable_symbol_returns_zero(symbol):
    assert option_strike_from_symbol(symbol) == 0.0


def test_the_parser_agrees_with_the_contract_master():
    """Every index option Kite lists, not a handful of hand-picked ones."""
    path = pathlib.Path("data_store/kite_instruments.json")
    if not path.exists():
        pytest.skip("contract master not present")
    rows = json.loads(path.read_text(encoding="utf-8"))
    names = {"MIDCPNIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX", "NIFTY"}
    checked = mismatched = 0
    for row in rows:
        if str(row.get("name")) not in names:
            continue
        if "OPT" not in str(row.get("instrumenttype") or "").upper():
            continue
        checked += 1
        # the master stores strike x100
        want = float(row.get("strike") or 0.0) / 100.0
        if abs(option_strike_from_symbol(row.get("symbol")) - want) > 0.01:
            mismatched += 1
    assert checked > 1000, f"only {checked} index options found — master looks wrong"
    assert mismatched == 0, f"{mismatched} of {checked} symbols parsed to the wrong strike"


# ── 2. the orphan row itself ─────────────────────────────────────────────────

def test_a_carried_position_shows_its_real_cost():
    """Kite reports today's buy/sell price as 0 for a carried position, so the row
    displayed a blank entry beside a live P&L — which reads as an invented number."""
    row = _serialize_orphan_position(_Mgr(), "NIFTY25AUG24350CE", _carried_position())
    assert row["entry_price"] == pytest.approx(69.44)


def test_a_same_day_position_still_prefers_the_day_price():
    """average_price is the FALLBACK. A position opened today must not lose its own
    buy price to the net average."""
    position = _carried_position()
    position["buyavgprice"] = 71.10          # what get_positions maps from buy_price
    row = _serialize_orphan_position(_Mgr(), "NIFTY25AUG24350CE", position)
    assert row["entry_price"] == pytest.approx(71.10)


def test_the_last_price_is_read():
    row = _serialize_orphan_position(_Mgr(), "NIFTY25AUG24350CE", _carried_position())
    assert row["current_price"] == pytest.approx(78.80)


def test_the_strike_is_not_the_expiry_year():
    """The reported symptom, asserted directly."""
    row = _serialize_orphan_position(_Mgr(), "NIFTY25AUG24350CE", _carried_position())
    assert row["atm_strike"] == 24350.0
    assert row["atm_strike"] not in (25.0, 26.0, 0.0)


def test_the_row_carries_an_unambiguous_symbol():
    """Without this the UI had only `instrument` and guessed the strike from it."""
    row = _serialize_orphan_position(_Mgr(), "NIFTY25AUG24350CE", _carried_position())
    assert row["trading_symbol"] == "NIFTY25AUG24350CE"
    assert row["base_instrument"] == "NIFTY"


def test_the_pnl_still_comes_from_the_broker():
    """It was never fabricated — `unrealised` is a real Kite field. The row is a real
    position; only its presentation was wrong."""
    row = _serialize_orphan_position(_Mgr(), "NIFTY25AUG24350CE", _carried_position())
    assert row["pnl"] == pytest.approx(608.40)
    assert row["status"] == "ORPHANED"
    assert row["exit_reason"] == "BROKER_ORPHAN"


# ── 3. the missing field mapping at the source ───────────────────────────────

def test_get_positions_maps_last_price_to_ltp():
    """buyavgprice/sellavgprice/producttype/netqty were mapped; ltp was the one missed,
    so every consumer reading position["ltp"] silently got 0.0."""
    class _Api:
        def positions(self):
            return {"net": [{
                "tradingsymbol": "NIFTY25AUG24350CE", "quantity": 65,
                "buy_price": 69.44, "sell_price": 0.0,
                "last_price": 78.80, "product": "NRML",
            }]}

    b = KiteBroker.__new__(KiteBroker)
    b.api = _Api()
    b._order_lock = threading.Lock()
    b._auth = None
    b._last_call_at = 0.0
    b._min_call_interval = 0.0
    b._api_call = lambda ctx, fn, timeout=5.0: fn()
    b._position_backoff_until = 0.0
    b._position_backoff_failures = 0

    row = b.get_positions()["data"][0]
    assert row["ltp"] == pytest.approx(78.80)
    assert row["buyavgprice"] == pytest.approx(69.44)
    assert row["netqty"] == 65


# ── 4. the frontend fallback ─────────────────────────────────────────────────

def test_the_js_fallback_no_longer_takes_the_first_digit_run():
    src = pathlib.Path("dashboard/static/js/trade-table.js").read_text(encoding="utf-8")
    assert "t.instrument.match(/\\d+/)[0]" not in src, "the year-grabbing fallback is back"
    # Backslash-free needle on purpose: test_source_assertions_are_not_stale compares the
    # RAW literal from this file against the source blob, so an escaped "\\d" here would
    # never match the "\d" in the JS and the guard would read as stale.
    assert "(?:CE|PE)$/" in src, "the trailing-strike pattern is gone"


@pytest.mark.parametrize("symbol,expected", [
    ("NIFTY26AUG24350CE", "24350"),
    ("BANKNIFTY26AUG57500PE", "57500"),
])
def test_the_js_fallback_pattern_reads_the_trailing_strike(symbol, expected):
    """Mirrors the JS regex so a change to it is caught here. The server now supplies
    atm_strike anyway, so this is the second line of defence, not the first."""
    assert re.search(r"(\d+)(?:CE|PE)$", symbol).group(1) == expected


# ── 5. the reason this is not merely cosmetic ────────────────────────────────

def test_an_orphan_blocks_every_entry():
    """A row nobody can interpret is also the row that stops the session trading."""
    import inspect

    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager.can_open_trade)
    assert "self.orphan_positions" in src
    assert "requires reconciliation" in src
