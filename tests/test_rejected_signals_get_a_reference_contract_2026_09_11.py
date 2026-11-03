"""A signal refused before its option contract is chosen must still be scored.

The shadow sampler marks out the executable bid/ask of the contract a signal would have
traded. Filters that run BEFORE contract selection (price-action ADVERSE, quality/grade
gates) left the candidate with no trading_symbol, so registration skipped it with
"no_trading_symbol": only 14 of ~170 price-action rejections since 2026-08-28 have any
outcome, so that filter has never been scored on evidence.

Such a candidate now gets the contract the entry path itself would pick — the ATM
CE (LONG) / PE (SHORT) at the configured strike interval, via the same
data.get_option_token lookup, 1 lot — marked contract_basis="reference_atm_1lot" so
analysis can tell it from a sized, entry-resolved contract.
"""
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from scanner import Scanner


class _Data:
    def __init__(self, tokens=None):
        self.tokens = {} if tokens is None else tokens
        self.lookups = []
        self.subscribed = []

    def get_option_token(self, name, strike, option_type, *a, **k):
        self.lookups.append((name, strike, option_type))
        return self.tokens.get((name, strike, option_type))

    def subscribe_tokens(self, tokens, exchange_type=1):
        self.subscribed.extend(tokens)


def _scanner(data):
    s = Scanner.__new__(Scanner)
    s.mode = "REAL"
    s.is_warmup = False
    s.data = data
    s.config = {
        "indices": {
            "NIFTY": {"strike_interval": 50, "lot_size": 65},
            "BANKNIFTY": {"strike_interval": 100, "lot_size": 30},
        }
    }
    s._entry_shadow_book = {}
    s._entry_shadow_lock = threading.RLock()
    s._diagnostics = {}
    s._entry_policy_live_index_quote = lambda instrument, now: (
        {"NIFTY": 23411.7, "BANKNIFTY": 56474.95}[instrument], now.isoformat(), "kite_ws"
    )
    return s


def _candidate(direction="LONG", instrument="NIFTY", **extra):
    values = dict(
        instrument=instrument,
        timeframe="15min",
        direction=direction,
        action="ENTRY",
        price=23411.7,
        inst_type="",
        trading_symbol="",
        symbol_token="",
        lots=0,
        lot_size=0,
        signal_timestamp=datetime(2026, 9, 11, 13, 15),
        market_regime="TRENDING",
        live_gate_rejection_reason="Price-action ADVERSE [-0.40: structure=DOWN]",
    )
    values.update(extra)
    return SimpleNamespace(**values)


def _only_watch(s):
    assert len(s._entry_shadow_book) == 1, s._diagnostics.get("entry_shadow_skipped")
    return next(iter(s._entry_shadow_book.values()))


def test_a_contractless_rejection_is_registered_on_the_atm_call():
    data = _Data({("NIFTY", 23400, "CE"): {"symbol": "NIFTY2691523400CE", "token": "111"}})
    s = _scanner(data)
    s._register_entry_shadow_outcome(_candidate("LONG"), "REJECTED")
    watch = _only_watch(s)
    assert watch["trading_symbol"] == "NIFTY2691523400CE"
    assert watch["inst_type"] == "OPT"
    assert watch["quantity"] == 65, "one lot of the configured lot size"
    assert watch["contract_basis"] == "reference_atm_1lot"
    assert data.subscribed == ["111"], "the sampler reads a cache-only quote; it must subscribe"


def test_a_short_rejection_uses_the_atm_put_at_its_own_strike_interval():
    data = _Data({("BANKNIFTY", 56500, "PE"): {"symbol": "BANKNIFTY26SEP56500PE", "token": "222"}})
    s = _scanner(data)
    s._register_entry_shadow_outcome(_candidate("SHORT", "BANKNIFTY"), "REJECTED")
    assert _only_watch(s)["trading_symbol"] == "BANKNIFTY26SEP56500PE"


def test_the_callers_candidate_is_not_mutated():
    data = _Data({("NIFTY", 23400, "CE"): {"symbol": "NIFTY2691523400CE", "token": "111"}})
    s = _scanner(data)
    cand = _candidate("LONG")
    s._register_entry_shadow_outcome(cand, "REJECTED")
    assert cand.trading_symbol == "" and cand.inst_type == "", "measurement changed the candidate"


def test_an_unresolvable_contract_is_still_counted_not_silently_dropped():
    s = _scanner(_Data())
    s._register_entry_shadow_outcome(_candidate("LONG"), "REJECTED")
    assert s._entry_shadow_book == {}
    skipped = s._diagnostics.get("entry_shadow_skipped", {})
    assert skipped, "a signal went unmeasured with no attributable reason"


def test_a_candidate_with_its_own_contract_is_unchanged():
    data = _Data()
    s = _scanner(data)
    cand = _candidate(
        "LONG", inst_type="OPT", trading_symbol="NIFTY2691523450CE", symbol_token="999",
        lots=2, lot_size=65,
    )
    s._register_entry_shadow_outcome(cand, "REJECTED")
    watch = _only_watch(s)
    assert watch["trading_symbol"] == "NIFTY2691523450CE"
    assert watch["quantity"] == 130
    assert watch["contract_basis"] == "candidate_contract"
    assert data.lookups == [], "resolved a reference contract for a candidate that had one"


def test_accepted_entries_never_get_a_reference_contract():
    data = _Data({("NIFTY", 23400, "CE"): {"symbol": "NIFTY2691523400CE", "token": "111"}})
    s = _scanner(data)
    s._register_entry_shadow_outcome(_candidate("LONG"), "ACCEPTED")
    assert s._entry_shadow_book == {}, "an accepted entry must be scored on its own contract"
