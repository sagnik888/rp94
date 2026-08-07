"""The live system told the operator it had swapped to AngelOne.

Requirement 23 is that AngelOne/SmartAPI is gone and replaced by Zerodha Kite. The
client is: `MarketData.smart_api` is only ever assigned None, so no Angel REST path can
execute. But three strings on the LIVE mode-switch path still named it, and one of them
reached the screen. Captured in the running session on 2026-08-07:

    10:03:02.169 | scanner:configure:3788 -
        📡 Broker Hot-Swapped to: LIVE EXECUTION (AngelOne)

— logged while swapping to Kite. The other two are the failure branch: a block reason of
"AngelOne broker not connected" and a dashboard popup reading "Failed to switch to REAL
mode! AngelOne not connected."

That is worse than untidy. Those fire exactly when live execution could not be armed,
which is the moment the operator is debugging under pressure, and they name a broker
that has not existed in this system for weeks.

Scope is deliberately the LIVE path. Dead Angel REST branches in market_data.py, the
retired-settings documentation in inert_settings.py, and standalone scripts are all
out of scope: they are unreachable or explicitly labelled as retired.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Modules the running system actually executes on the trading path.
#
# main.py is deliberately NOT here. Its six references are about historical artifacts
# rather than the current broker: four name ANGELONE_* keys while scanning .env so
# leftover plaintext secrets still get flagged, and two name legacy AngelOne-era log
# FOLDERS being compressed off disk. Renaming those would stop the warning working.
# They are pinned by test_main_only_references_angelone_as_a_legacy_artifact below.
LIVE_MODULES = [
    "scanner.py",
    "trading/trade_manager.py",
    "trading/kite_broker.py",
    "trading/broker.py",
    "engine/safe_mode.py",
    "engine/recalculation_worker.py",
]

RETIRED = ("angelone", "angel one", "smartapi", "smart api")


def _string_constants(path: Path):
    """Every string literal in the file, with its line number."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _docstring_lines(path: Path):
    """Docstrings are prose about history, not messages to the operator."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                body = node.body[0]
                out.update(range(body.lineno, getattr(body, "end_lineno", body.lineno) + 1))
    return out


@pytest.mark.parametrize("rel", LIVE_MODULES)
def test_no_live_message_names_a_retired_broker(rel):
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    skip = _docstring_lines(path)
    offenders = [
        (lineno, text.strip()[:90])
        for lineno, text in _string_constants(path)
        if lineno not in skip and any(w in text.lower() for w in RETIRED)
    ]
    assert not offenders, (
        f"{rel} still names a retired broker in a runtime string: {offenders}"
    )


def test_the_hot_swap_line_names_the_broker_it_actually_swapped_to():
    """The exact line captured live at 10:03:02 on 2026-08-07."""
    src = (ROOT / "scanner.py").read_text(encoding="utf-8", errors="replace")
    assert "Broker Hot-Swapped to: LIVE EXECUTION (Zerodha Kite)" in src
    assert "LIVE EXECUTION (AngelOne)" not in src


def test_main_only_references_angelone_as_a_legacy_artifact():
    """main.py may name AngelOne, but only about the PAST, never the live broker.

    Two legitimate classes: ANGELONE_* env keys it scans .env for so leftover plaintext
    secrets are still reported, and legacy AngelOne-era log folders it compresses off
    disk. Anything else appearing here is a live message that needs rewording.
    """
    path = ROOT / "main.py"
    skip = _docstring_lines(path)
    allowed = ("angelone_api_key", "angelone_password", "angelone_totp_secret",
               "angelone_secret_key", "compressed legacy angelone log folder",
               "error in angelone log cleaner")
    unexpected = [
        (lineno, text.strip()[:90])
        for lineno, text in _string_constants(path)
        if lineno not in skip
        and any(w in text.lower() for w in RETIRED)
        and text.strip().lower().rstrip(":") not in allowed
    ]
    assert not unexpected, f"new AngelOne reference on the live path in main.py: {unexpected}"


def test_smart_api_is_still_only_ever_none():
    """The premise the scope of this test rests on.

    If something ever assigns a real client, the dead Angel REST branches in
    market_data.py become reachable and this test's exclusions stop being safe.
    """
    tree = ast.parse((ROOT / "data" / "market_data.py").read_text(encoding="utf-8", errors="replace"))
    assigned = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Attribute) and tgt.attr == "smart_api":
                assigned.append((node.lineno, ast.dump(node.value)[:60]))
    assert assigned, "smart_api assignment vanished — re-check this test's assumptions"
    for lineno, value in assigned:
        assert "Constant(value=None)" in value, (
            f"market_data.py:{lineno} assigns a non-None smart_api; the retired Angel "
            f"REST paths are reachable again"
        )
