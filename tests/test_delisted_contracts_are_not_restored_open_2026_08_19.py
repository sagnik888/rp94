"""Known synthetic fixtures are not restored, while broker exposure fails open.

2026-08-19, straight off the live dashboard, unchanged across six restarts:

    TRADE MANAGER (4)   DAILY P&L: Rs.199,081   TRADES: 4   W/L: 0/0
    TF: --   Grade: --   Index: --   Type: --   Exit: --

    T-MIDCPNIFTY  MIDCPNIFTY25AUG12800CE   88.15 -> mark 1118.94   +Rs.123,180
    T-BANKNIFTY   BANKNIFTY25AUG52000CE   213.30 -> mark 2795.64   + Rs.89,986
    T-SENSEX      SENSEX25AUG81000CE      301.60 -> mark    0.05   -  Rs.6,129
    T-NIFTY       NIFTY25AUG24500CE       104.75 -> mark    0.05   -  Rs.7,956
                                                                     ----------
                                                                     Rs.199,081

Fixtures, not trades: ids T-<INDEX>, broker ids O-<INDEX>, symbol_token '1' on all
four, identical entry_timestamp, all written inside 17 milliseconds at 01:57:35, empty
data_provenance. Live series that day was 26AUG; these are 25AUG. Strikes nowhere near
spot (BANKNIFTY 52000 against 57,178). Marked to today's spot, an expired deep-ITM call
invents six figures of P&L.

Deleting the rows does NOT hold: the running process owns this state in memory and
re-persists it within seconds, with byte-identical entry_filled_at stamps. The row has
to be refused at LOAD, which is what this covers.

WHY MEMBERSHIP AND NOT EXPIRY. The first attempt parsed the expiry year off the symbol
and was reverted before shipping, because this codebase uses at least three layouts:

    NIFTY26AUG24500CE     year first    26 -> 2026
    NIFTY04AUG2624350CE   DAY first     04 -> would read as year 2004
    NIFTY2681824350CE     weekly

"first two digits are the year" therefore drops a LIVE position on the middle format —
worse than the bug it fixes. Membership in the broker's own instrument listing needs no
parsing and is format-agnostic, but it is not authoritative for exposure: a carried
position may be absent from a stale/incomplete dump. The guard therefore also requires
the exact synthetic id/order-id/token fingerprint recorded on the four fixtures.

Every failure mode keeps the row. This may only remove what it can positively prove.

These tests drive the guard as the pure list-of-dicts function it is, and never build a
DatabaseManager. An earlier version did, and its fixture perturbed the module-level DB
singleton enough to fail three unrelated persistence tests when run in the same session.
"""
import json

import pytest

from data.sqlite_db import DatabaseManager
from trading.trade_manager import TradeManager

REAL = [
    "MIDCPNIFTY26AUG14850CE", "NIFTY26AUG24100PE",
    "BANKNIFTY26AUG57200PE", "SENSEX26AUG77000PE",
]
FIXTURES = [
    "MIDCPNIFTY25AUG12800CE", "BANKNIFTY25AUG52000CE",
    "SENSEX25AUG81000CE", "NIFTY25AUG24500CE",
]


def _rec(tid, symbol, pnl=0.0, *, fixture=False, **extra):
    record = {"id": tid, "trading_symbol": symbol, "status": "OPEN", "pnl": pnl}
    if fixture:
        index = str(tid).removeprefix("T-")
        record.update(
            broker_order_id=f"O-{index}",
            symbol_token="1",
            data_provenance="",
        )
    record.update(extra)
    return record


def _listing(monkeypatch, symbols):
    monkeypatch.setattr(
        DatabaseManager, "_tradable_symbols",
        classmethod(lambda cls, path=None: frozenset(symbols) if symbols else None),
    )


def _drop(records):
    return TradeManager._drop_delisted_open_records(records)


# ── the failure this exists to stop ──────────────────────────────────────────

def test_the_four_recorded_fixtures_are_refused(monkeypatch):
    _listing(monkeypatch, REAL + ["NIFTY26AUGFUT"])
    records = [
        _rec("T-MIDCPNIFTY", "MIDCPNIFTY25AUG12800CE", 123180.0, fixture=True),
        _rec("T-BANKNIFTY", "BANKNIFTY25AUG52000CE", 89986.0, fixture=True),
        _rec("T-SENSEX", "SENSEX25AUG81000CE", -6129.0, fixture=True),
        _rec("T-NIFTY", "NIFTY25AUG24500CE", -7956.0, fixture=True),
    ]
    assert sum(r["pnl"] for r in records) == pytest.approx(199081.0)
    assert _drop(records) == []


def test_the_contracts_actually_traded_that_day_all_survive(monkeypatch):
    """The other half: this must not touch a real position."""
    _listing(monkeypatch, REAL)
    records = [_rec(f"REAL-{i}", s, 100.0 * i) for i, s in enumerate(REAL)]
    assert len(_drop(records)) == len(REAL)


def test_a_mixed_set_keeps_only_the_live_side(monkeypatch):
    _listing(monkeypatch, REAL)
    records = [
        _rec("REAL-1", "MIDCPNIFTY26AUG14850CE", 420.0),
        _rec("T-NIFTY", "NIFTY25AUG24500CE", -7956.0, fixture=True),
    ]
    assert [r["id"] for r in _drop(records)] == ["REAL-1"]


# ── every failure mode keeps the row ─────────────────────────────────────────

def test_no_instrument_list_keeps_everything(monkeypatch):
    """Absence of the file is not evidence a contract is dead."""
    _listing(monkeypatch, None)
    records = [_rec(f"T-{s}", s, 1.0, fixture=True) for s in FIXTURES]
    assert len(_drop(records)) == len(FIXTURES)


def test_a_row_with_no_symbol_is_kept(monkeypatch):
    _listing(monkeypatch, REAL)
    assert len(_drop([_rec("NO-SYM", ""), _rec("NULL-SYM", None)])) == 2


def test_an_empty_input_is_fine(monkeypatch):
    _listing(monkeypatch, REAL)
    assert _drop([]) == []


def test_a_raising_listing_keeps_everything(monkeypatch):
    """The guard may never take down startup."""
    def _boom(cls, path=None):
        raise RuntimeError("instrument listing exploded")

    monkeypatch.setattr(DatabaseManager, "_tradable_symbols", classmethod(_boom))
    records = [_rec("T-NIFTY", "NIFTY25AUG24500CE", -7956.0, fixture=True)]
    assert _drop(records) == records


def test_unlisted_broker_backed_position_survives_for_reconciliation(monkeypatch):
    """The broker book, not a cached listing, is authoritative for exposure."""
    _listing(monkeypatch, REAL)
    record = _rec(
        "T123_NIFTY_5min",
        "NIFTY26AUG25000CE",
        broker_order_id="260822123456789",
        symbol_token="123456",
        data_provenance="LIVE",
    )
    assert _drop([record]) == [record]


def test_unlisted_legacy_row_without_fixture_fingerprint_fails_open(monkeypatch):
    _listing(monkeypatch, REAL)
    record = _rec("LEGACY-OPEN", "NIFTY25AUG24500CE", -7956.0)
    assert _drop([record]) == [record]


# ── the listing loader itself ────────────────────────────────────────────────

def test_a_truncated_instrument_file_is_ignored(tmp_path):
    """A half-written dump must not be read as 'these contracts do not exist'."""
    f = tmp_path / "kite_instruments.json"
    f.write_text(json.dumps([{"symbol": s} for s in REAL]), encoding="utf-8")
    assert DatabaseManager._tradable_symbols(str(f)) is None


def test_a_full_size_listing_is_accepted(tmp_path):
    """The other side of the size guard: a plausible dump is used."""
    f = tmp_path / "kite_instruments.json"
    rows = [{"symbol": f"FILLER{i}"} for i in range(2000)] + [{"symbol": s} for s in REAL]
    f.write_text(json.dumps(rows), encoding="utf-8")
    out = DatabaseManager._tradable_symbols(str(f))
    assert out and "MIDCPNIFTY26AUG14850CE" in out


def test_an_unreadable_instrument_file_is_ignored(tmp_path):
    f = tmp_path / "kite_instruments.json"
    f.write_text("{not json", encoding="utf-8")
    assert DatabaseManager._tradable_symbols(str(f)) is None


def test_a_missing_instrument_file_is_ignored(tmp_path):
    assert DatabaseManager._tradable_symbols(str(tmp_path / "nope.json")) is None


def test_an_explicit_path_does_not_poison_the_cache(tmp_path):
    """Test paths must not become the process-wide answer."""
    before = DatabaseManager._TRADABLE_SYMBOLS
    f = tmp_path / "kite_instruments.json"
    rows = [{"symbol": f"FILLER{i}"} for i in range(2000)]
    f.write_text(json.dumps(rows), encoding="utf-8")
    DatabaseManager._tradable_symbols(str(f))
    assert DatabaseManager._TRADABLE_SYMBOLS is before


# ── wiring, and the real file ────────────────────────────────────────────────

def test_the_accessor_itself_stays_unfiltered():
    """db.load_open_trades is a plain accessor -- the persistence round-trip depends on
    'save a row then read it back' meaning exactly that."""
    import inspect

    src = inspect.getsource(DatabaseManager.load_open_trades)
    assert "_tradable_symbols()" not in src


def test_the_restore_path_actually_calls_the_guard():
    """A guard nothing calls is the bug it was written to fix."""
    import inspect

    src = inspect.getsource(TradeManager.load_state)
    assert "_drop_delisted_open_records(" in src


def test_the_real_listing_separates_the_fixtures_from_the_live_contracts():
    """No mocking: the actual 39,892-symbol file this ships against."""
    import os

    if not os.path.exists("data_store/kite_instruments.json"):
        pytest.skip("instrument listing not present")
    symbols = DatabaseManager._tradable_symbols()
    if not symbols:
        pytest.skip("instrument listing unreadable in this environment")
    assert len(symbols) > 1000
    for sym in REAL:
        assert sym in symbols, f"{sym} traded on 2026-08-19 but is not listed"
    for sym in FIXTURES:
        assert sym not in symbols, f"{sym} is a delisted fixture but appears listed"
