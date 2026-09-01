"""A signal that goes unmeasured must say why it went unmeasured.

Measured 2026-08-25..2026-09-01 -- the only sessions carrying lane stamping, so the only
ones where live can be told from replay at all:

    465 live signals seen, 58 accepted (12.5%), 407 rejected
    312 of those (77%) were stopped by exactly ONE gate
    127 of the 312 (41%) had any recorded outcome
    185 had none

The 312 matter more than the 407: when one gate is the only thing between a signal and a
trade, that gate is individually answerable for it. Without an outcome it cannot be
judged, which is the whole reason no threshold on this system can currently be defended
with evidence. Two separate attempts to justify a threshold this week collapsed for
exactly this: the surviving samples were three trades on one session.

Coverage is also very uneven by gate:

    Repainted during 30s stabilization   142 signals,  30% measured
    quad-prime raw quality floor          83 signals,  58% measured
    Weak-market scalp                     24 signals,  33% measured
    15min choppy scalp guard               7 signals, 100% measured

That spread points at WHEN in the pipeline a refusal happens rather than at the gate --
a signal refused before its option contract is resolved has no instrument to mark out,
and `_register_entry_shadow_outcome` requires trading_symbol, symbol_token and a
quantity. But that is a HYPOTHESIS. The archive cannot confirm it, because a rejection
row does not carry the contract, so the skip reason has to be recorded at the moment it
happens.

This is a measurement change. It counts; it does not alter any entry or rejection.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner


@pytest.fixture
def scanner(monkeypatch):
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc.is_warmup = False
    sc._diagnostics = {}
    sc._setting_enabled = lambda value, default=False: True
    sc._entry_shadow_quantity = lambda holder: int(getattr(holder, "quantity", 0) or 0)
    return sc


def _candidate(**overrides):
    base = dict(
        instrument="NIFTY", inst_type="OPT", trading_symbol="NIFTY2690824100PE",
        symbol_token="12345", quantity=75, action="ENTRY", direction="SHORT",
        timeframe="5min", is_recovery=False, is_ghost=False,
        live_gate_rejection_reason="Repainted during 30s stabilization window",
        status="REJECTED",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _skips(scanner):
    return scanner._diagnostics.get("entry_shadow_skipped", {})


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("trading_symbol", "", "no_trading_symbol"),
        ("symbol_token", "", "no_symbol_token"),
        ("quantity", 0, "no_quantity"),
        ("inst_type", "", "no_inst_type"),
        ("instrument", "TCS", "unsupported_instrument"),
    ],
)
def test_each_missing_precondition_is_named(scanner, field, value, expected):
    """Silent drops are what made the coverage gap undiagnosable."""
    scanner._register_entry_shadow_outcome(_candidate(**{field: value}), "REJECTED")

    skips = _skips(scanner)
    assert expected in skips, f"a skip for {field}={value!r} was not recorded"
    assert skips[expected]["count"] == 1


def test_the_skip_is_attributed_to_the_gate_that_refused(scanner):
    """Which gate loses coverage is the question; a total would not answer it."""
    scanner._register_entry_shadow_outcome(
        _candidate(trading_symbol="",
                   live_gate_rejection_reason="Repainted during 30s stabilization window"),
        "REJECTED",
    )
    scanner._register_entry_shadow_outcome(
        _candidate(trading_symbol="",
                   live_gate_rejection_reason="quad-prime raw quality floor failed"),
        "REJECTED",
    )

    gates = _skips(scanner)["no_trading_symbol"]["gates"]
    assert gates["Repainted during 30s stabilization window"] == 1
    assert gates["quad-prime raw quality floor failed"] == 1


def test_repeated_skips_accumulate(scanner):
    for _ in range(4):
        scanner._register_entry_shadow_outcome(
            _candidate(symbol_token=""), "REJECTED"
        )
    assert _skips(scanner)["no_symbol_token"]["count"] == 4


def test_a_registrable_signal_records_no_skip(scanner, monkeypatch):
    """The counter must only fire on the failure path."""
    monkeypatch.setattr(
        Scanner, "_candidate_lifecycle_key", lambda self, c: "", raising=False
    )
    scanner._register_entry_shadow_outcome(_candidate(), "REJECTED")

    assert "no_trading_symbol" not in _skips(scanner)
    assert "no_symbol_token" not in _skips(scanner)
    assert "no_quantity" not in _skips(scanner)


def test_counting_never_breaks_the_rejection_path(scanner):
    """Measurement may not become an entry or rejection failure mode.

    The caller already wraps this in try/except for that reason; the counter must not
    lean on that. A candidate with no diagnostics dict at all must still be handled.
    """
    del scanner._diagnostics
    scanner._register_entry_shadow_outcome(_candidate(trading_symbol=""), "REJECTED")
    # reaching here without raising is the assertion


def test_it_still_declines_outside_real_mode(scanner):
    """Replay must not fill the book; that is how live and replay got mixed before."""
    scanner.mode = "HISTORICAL"
    scanner._register_entry_shadow_outcome(_candidate(trading_symbol=""), "REJECTED")
    assert _skips(scanner) == {}


def test_the_counter_survives_a_simulation_reset():
    """_reset_simulation_diagnostics REPLACES the dict; it does not update it.

    A key created only by setdefault() at the write site is therefore erased on every
    reset. This counter was shipped that way on 2026-09-01 and would have recorded
    nothing across a session boundary -- the exact evidence it exists to gather.
    """
    import inspect

    from engine.scanner_telemetry import ScannerTelemetryMixin

    source = inspect.getsource(ScannerTelemetryMixin._reset_simulation_diagnostics)
    assert "entry_shadow_skipped" in source, (
        "entry_shadow_skipped is not declared in the reset skeleton, so a simulation "
        "reset silently drops it"
    )


def test_the_counter_reaches_the_diagnostics_payload():
    """A counter nobody can read is not instrumentation.

    _get_diagnostics_payload builds a CURATED payload -- it does not return the raw
    diagnostics dict -- so a new key is invisible until it is added explicitly.
    """
    import inspect

    from engine.scanner_telemetry import ScannerTelemetryMixin

    source = inspect.getsource(ScannerTelemetryMixin._get_diagnostics_payload)
    assert '"entry_shadow_skipped": entry_shadow_skipped' in source, (
        "the shadow-coverage counter is not returned in the diagnostics payload, so it "
        "cannot be read from the dashboard or the API"
    )
    assert 'diagnostics.get("entry_shadow_skipped"' in source, (
        "the payload does not read the counter out of _diagnostics"
    )
