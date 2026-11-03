"""Contract resolution must not rescan unrelated exchange contracts per leg."""
from datetime import date, timedelta
from unittest.mock import Mock

from data.kite_market_data import KiteDataSource


EXPIRY = date(2026, 11, 10)


def row(name="NIFTY", strike=22000., side="CE", **extra):
    return dict(name=name, strike=strike, instrument_type=side, expiry=EXPIRY,
                tradingsymbol=f"{name}_{strike}_{side}", instrument_token=1,
                exchange="NFO", **extra)


def source(rows):
    api = Mock()
    api.instruments.side_effect = lambda exchange: rows if exchange == "NFO" else []
    result = KiteDataSource(kite=api)
    result.load_instruments()
    return result, api


class CountedRow(dict):
    reads = 0

    def __getitem__(self, name):
        type(self).reads += 1
        return super().__getitem__(name)


def test_warm_resolution_does_not_visit_unrelated_contract_rows():
    irrelevant = [CountedRow(row(name=f"STOCK{i}")) for i in range(4000)]
    result, _ = source([*irrelevant, row(), row(side="PE")])
    # Normalization intentionally copies rows. Instrument the published master,
    # then force a complete load so both the master and its index see those rows.
    original = result._normalise_instrument
    result._normalise_instrument = lambda value: CountedRow(original(value))
    result.load_instruments(force=True)
    result.resolve_option("NIFTY", 22000, "CE", expiry=EXPIRY)
    CountedRow.reads = 0
    for _ in range(30):
        assert result.resolve_option("nifty", 22000, "ce", expiry=EXPIRY)
        assert result.resolve_option("NIFTY", 22000, "PE", expiry=EXPIRY)
        assert result.available_strikes("NIFTY", expiry=EXPIRY) == [22000.]
        assert result.expiries_for("NIFTY", on_or_after=EXPIRY) == [EXPIRY]
    assert CountedRow.reads < 1000


def test_resolution_preserves_first_match_and_strict_strike_tolerance():
    rows = [row(strike=22000.009), row(strike=22000.), row(strike=22000.011)]
    result, _ = source(rows)
    assert result.resolve_option("NIFTY", 22000., "CE", expiry=EXPIRY)["strike"] == 22000.009
    assert result.resolve_option("NIFTY", 22000.03, "CE", expiry=EXPIRY) is None
    assert result.resolve_option("NIFTY", 22000., "PE", expiry=EXPIRY) is None


def test_successful_reload_replaces_index_and_failed_reload_keeps_it():
    result, api = source([row()])
    assert result.resolve_option("NIFTY", 22000, "CE", expiry=EXPIRY)
    new = row(strike=22100.)
    api.instruments.side_effect = lambda exchange: [new] if exchange == "NFO" else []
    result.load_instruments(force=True)
    assert result.resolve_option("NIFTY", 22000, "CE", expiry=EXPIRY) is None
    assert result.resolve_option("NIFTY", 22100, "CE", expiry=EXPIRY)
    api.instruments.side_effect = RuntimeError("master unavailable")
    result.load_instruments(force=True)
    assert result.resolve_option("NIFTY", 22100, "CE", expiry=EXPIRY)


def test_expiry_and_strike_queries_preserve_duplicates_and_date_boundary():
    result, _ = source([row(), row(), row(side="PE"),
                        {**row(), "expiry": EXPIRY-timedelta(days=7)},
                        {**row(), "expiry": EXPIRY+timedelta(days=7)}])
    assert result.available_strikes("NIFTY", expiry=EXPIRY) == [22000., 22000.]
    assert result.expiries_for("NIFTY", on_or_after=EXPIRY) == [EXPIRY, EXPIRY+timedelta(days=7)]
    assert result.resolve_option("NIFTY", 22000, "CE", on_or_after=EXPIRY)["expiry"] == EXPIRY
