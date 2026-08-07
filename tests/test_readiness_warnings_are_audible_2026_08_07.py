"""The readiness check's warnings were computed and thrown away.

validate_real_mode_readiness returns {"ok", "blockers", "warnings"}. Every call site —
main.py at startup and both arming gates in scanner.configure — read only `ok` and
`blockers`. The warnings list went nowhere.

What that swallowed, measured against the live config on 2026-08-07:

  * broker free cash is 11,255.40, below the 50,000 floor for one index lot — RMS will
    refuse entries until the account is funded
  * slippage_opt_pct is 0 — option P&L is modelled with NO slippage on the instrument
    this system actually trades
  * 32 setting(s) in .env have no consumer and do nothing

readiness["ok"] was True. The scanner armed REAL at 10:03:02 knowing it could not fund
a single lot, and said nothing about any of it.

Warnings must stay warnings — promoting them to blockers would refuse to arm over a
funding level the operator may be deliberately testing under. They only have to be
audible.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from scanner import Scanner


@pytest.fixture
def logged(monkeypatch):
    lines = []
    import scanner as scanner_module

    monkeypatch.setattr(
        scanner_module.logger, "warning", lambda msg, *a, **k: lines.append(str(msg))
    )
    return lines


def _scanner(notify=None):
    s = Scanner.__new__(Scanner)
    s.on_notification = notify
    return s


READINESS = {
    "ok": True,
    "blockers": [],
    "warnings": [
        "broker free cash is 11,255.40, below the 50,000 floor for one index lot",
        "slippage_opt_pct is 0 — option P&L is modelled with NO slippage",
    ],
}


def test_every_warning_reaches_the_log(logged):
    _scanner()._surface_readiness_warnings(READINESS)
    assert len(logged) == 2
    assert any("11,255.40" in ln for ln in logged), "the funding shortfall stayed silent"
    assert any("NO slippage" in ln for ln in logged)


def test_the_operator_gets_one_notification_not_one_each(logged):
    notes = []
    _scanner(notify=lambda m, lvl="info": notes.append((m, lvl)))._surface_readiness_warnings(
        READINESS
    )
    assert len(notes) == 1
    assert "2" in notes[0][0]
    assert notes[0][1] == "warning"


def test_a_clean_readiness_says_nothing(logged):
    notes = []
    _scanner(notify=lambda m, lvl="info": notes.append(m))._surface_readiness_warnings(
        {"ok": True, "blockers": [], "warnings": []}
    )
    assert logged == [] and notes == []


def test_warnings_are_not_promoted_to_blockers():
    """Arming must not be refused over a funding level being deliberately tested."""
    readiness = dict(READINESS)
    assert readiness["ok"] is True
    _scanner()._surface_readiness_warnings(readiness)
    assert readiness["ok"] is True and readiness["blockers"] == []


@pytest.mark.parametrize("bad", [None, {}, {"ok": True}, SimpleNamespace()])
def test_a_malformed_readiness_never_breaks_arming(bad, logged):
    _scanner()._surface_readiness_warnings(bad)


def test_a_raising_notifier_does_not_break_arming(logged):
    def _boom(*a, **k):
        raise RuntimeError("dashboard gone")

    _scanner(notify=_boom)._surface_readiness_warnings(READINESS)
    assert len(logged) == 2


# ── the wiring: every call site must consult it ──────────────────────────────

def test_both_scanner_arming_gates_surface_warnings():
    """Two validate_real_mode_readiness calls in configure; one wired is not wired."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Scanner.configure)))
    validations = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "validate_real_mode_readiness"
    ]
    surfaced = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_surface_readiness_warnings"
    ]
    assert len(validations) >= 2, "arming gate count changed — re-check this test"
    assert len(surfaced) == len(validations), (
        f"{len(validations)} readiness checks but only {len(surfaced)} surface their "
        f"warnings — an arming path is silently discarding them again"
    )


def test_main_startup_logs_them_too():
    import main

    src = inspect.getsource(main.create_system)
    assert 'readiness.get("warnings")' in src, (
        "the startup readiness path still throws its warnings away"
    )


def test_the_readiness_check_still_produces_warnings():
    """The premise. If this returns no warnings key, everything above is vacuous."""
    from engine.safe_mode import validate_real_mode_readiness

    src = inspect.getsource(validate_real_mode_readiness)
    assert '"warnings": warnings' in src
