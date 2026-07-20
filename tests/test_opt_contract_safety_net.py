"""OPT contract safety net: no half-built OPT candidate may reach execution.

Rare dual-TF sibling clones can flip a FUT winner's copy to inst_type=OPT with
the futures symbol/token/levels still attached. The coordinator must convert
such hybrids into real option contracts or drop them — never book premium math
against a futures token.
"""

from __future__ import annotations

from datetime import datetime

from engine.signal_processor import TradeCandidate
from scanner import Scanner


def _candidate(**overrides):
    base = dict(
        instrument="NIFTY",
        direction="LONG",
        price=25_000.0,
        stop=24_975.0,
        target=25_050.0,
        lots=1,
        lot_size=65,
        grade="A",
        confidence=0.75,
        timeframe="5min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=0.0,
        multiplier=1.0,
        trading_symbol="NIFTY26JULFUT",
        symbol_token="55555",
        signal_timestamp=datetime(2026, 7, 10, 10, 0, 0),
    )
    base.update(overrides)
    return TradeCandidate(**base)


def _scanner():
    sc = Scanner.__new__(Scanner)
    sc.log_event = lambda *a, **k: None
    return sc


def test_hybrid_opt_with_futures_symbol_gets_converted():
    sc = _scanner()
    calls = []

    def _convert(c):
        calls.append(c)
        c.atm_strike = 25_000.0
        c.trading_symbol = "NIFTY10JUL2625000CE"
        c.symbol_token = "77777"
        c.price = 150.0
        c.stop = 142.5
        c.target = 165.0
        return True

    sc._force_candidate_to_options = _convert
    hybrid = _candidate()
    out = sc._resolve_unbuilt_opt_entries([hybrid])
    assert out == [hybrid]
    assert calls == [hybrid]
    assert hybrid.trading_symbol.endswith("CE")
    assert hybrid.atm_strike > 0


def test_hybrid_opt_dropped_when_conversion_fails():
    sc = _scanner()
    sc._force_candidate_to_options = lambda c: False
    hybrid = _candidate()
    out = sc._resolve_unbuilt_opt_entries([hybrid])
    assert out == []
    assert hybrid.status == "REJECTED - OPT Contract Unresolved"


def test_fully_built_opt_untouched():
    sc = _scanner()

    def _boom(c):  # converter must never run for a real option contract
        raise AssertionError("conversion attempted on a fully built OPT")

    sc._force_candidate_to_options = _boom
    real_opt = _candidate(
        atm_strike=25_000.0,
        trading_symbol="NIFTY10JUL2625000CE",
        symbol_token="77777",
        price=150.0,
        stop=142.5,
        target=165.0,
    )
    out = sc._resolve_unbuilt_opt_entries([real_opt])
    assert out == [real_opt]


def test_fut_and_exit_rows_pass_through():
    sc = _scanner()
    sc._force_candidate_to_options = lambda c: (_ for _ in ()).throw(AssertionError)
    fut = _candidate(inst_type="FUT", option_type="")
    exit_row = _candidate(action="EXIT")
    out = sc._resolve_unbuilt_opt_entries([fut, exit_row])
    assert out == [fut, exit_row]
