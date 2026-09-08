"""The gate that decides whether same-contract stacking may be re-enabled.

Step 5 says stacking comes back "only after replay and paper certification". The claim
this checks is narrow and falsifiable: for every contract the account holds, the sum of
the ledger's per-trade holdings equals the broker's net position.

A disagreement is the ledger mis-attributing, which is precisely what would mis-size a
protective stop on a shared contract — so the check must FAIL rather than warn.
"""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "ledger_cert", Path(__file__).resolve().parents[1] / "scripts" / "ledger_certification.py"
)
cert = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cert)


SYM = "NIFTY2681424300CE"


def test_agreement_certifies(capsys):
    ledger = {SYM: {"T1": 75, "T2": 75}}

    assert cert._report(ledger, {SYM: 150}, offline=False) == 0
    assert "agree on every contract" in capsys.readouterr().out


def test_a_shortfall_fails(capsys):
    """Ledger attributes more than the account holds: a stop would be oversized."""
    ledger = {SYM: {"T1": 75, "T2": 75}}

    assert cert._report(ledger, {SYM: 75}, offline=False) == 1
    assert "NOT CERTIFIED" in capsys.readouterr().out


def test_an_excess_fails(capsys):
    """Broker holds more than the ledger explains: quantity nobody is protecting."""
    ledger = {SYM: {"T1": 75}}

    assert cert._report(ledger, {SYM: 150}, offline=False) == 1


def test_a_short_position_is_compared_on_magnitude():
    ledger = {SYM: {"T1": 75}}

    assert cert._report(ledger, {SYM: -75}, offline=False) == 0


def test_a_closed_trade_does_not_count_toward_the_holding():
    """A fully exited trade sums to zero and must not inflate the comparison."""
    ledger = {SYM: {"T1": 75, "T2": 0}}

    assert cert._report(ledger, {SYM: 75}, offline=False) == 0


def test_an_empty_ledger_certifies_nothing(capsys):
    """Silence must not read as success — the honest answer is 'no evidence'."""
    assert cert._report({}, {}, offline=False) == 0
    out = capsys.readouterr().out
    assert "Nothing to certify" in out
    assert "must stay disabled" in out


def test_a_shared_contract_is_called_out(capsys):
    """The case stacking depends on should be visible in the report."""
    cert._report({SYM: {"T1": 75, "T2": 75}}, {SYM: 150}, offline=False)

    assert "shared contract" in capsys.readouterr().out


def test_offline_never_claims_certification(capsys):
    """Without the broker there is nothing to compare against."""
    assert cert._report({SYM: {"T1": 75}}, {}, offline=True) == 0
    out = capsys.readouterr().out
    assert "nothing compared" in out
    assert "NOT CERTIFIED" not in out


def test_one_clean_run_is_not_presented_as_sufficient(capsys):
    cert._report({SYM: {"T1": 75}}, {SYM: 75}, offline=False)
    out = capsys.readouterr().out

    assert "One clean run is not certification" in out
    assert "protective stop firing" in out


def test_requested_live_comparison_fails_closed_when_broker_is_unavailable(
    monkeypatch, capsys
):
    monkeypatch.setattr(cert, "_ledger_holdings", lambda: {})
    monkeypatch.setattr(
        cert,
        "_broker_positions",
        lambda: ({}, "positions unreadable (test outage)"),
    )
    monkeypatch.setattr(sys, "argv", ["ledger_certification.py"])

    assert cert.main() == 2
    out = capsys.readouterr().out
    assert "Live broker comparison was requested" in out
    assert "NOT CERTIFIED" in out


def test_broker_auth_import_uses_the_current_data_package():
    source = Path(cert.__file__).read_text(encoding="utf-8")
    assert "from data.kite_auth import get_kite_auth" in source
    assert "from trading.kite_auth import get_kite_auth" not in source


def test_direct_offline_cli_can_import_the_repository_packages():
    """The documented audit file must work outside ``python -m`` as well."""
    script = Path(cert.__file__).resolve()
    completed = subprocess.run(
        [sys.executable, str(script), "--offline"],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (
        "Offline: ledger contents only" in completed.stdout
        or "Nothing to certify" in completed.stdout
    )
