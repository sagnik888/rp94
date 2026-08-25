"""The session tracker must poll THIS build's dashboard, not a hardcoded port.

scripts/live_session_tracker.py carried:

    API = "http://127.0.0.1:8001"

rp46-3 serves DASHBOARD_PORT=8500. Port 8001 belongs to a different build on this
machine ("ut index 2", CAPITAL_FUT=900000 / RISK_FUT_PCT=2.0). The consequences ran
in both directions and both are bad:

  * When 8001 was down, every poll failed. The 2026-08-20 report says so on its face:
    "Dashboard API unreachable polls: 316/316", with every health field `n/a`.

  * When 8001 was UP, the tracker reported the OTHER build's state as this one's.
    The 2026-08-21 report claims "FUT budget 18000.0 · max trade loss 5000.0 ·
    warnings 0". This build's figures are Rs.0.015, 0, and one warning. 900000 * 2.0%
    is exactly 18,000 — that is "ut index 2" being described under rp46-3's filename.

Every rp46-3 session report ever written is therefore about some other system, which
also retracts anything concluded from them (e.g. "17 of 34 sessions BLOCKED on trade
contract mismatch").

An explicit SESSION_TRACKER_API override wins, so a build can still be pointed at a
remote or non-default dashboard on purpose.
"""
import importlib

import pytest


def _resolve(monkeypatch, **env):
    """Re-import the tracker under a given environment and return its API base."""
    for key in ("SESSION_TRACKER_API",):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    module = importlib.import_module("scripts.live_session_tracker")
    return module._resolve_api_base()


def test_it_follows_this_builds_dashboard_port(monkeypatch):
    from config.settings import get_settings

    port = int(get_settings().dashboard_port)
    assert _resolve(monkeypatch).endswith(f":{port}")


def test_it_is_not_pinned_to_8001(monkeypatch):
    """The specific bug: another build owns 8001 on this machine."""
    from config.settings import get_settings

    if int(get_settings().dashboard_port) == 8001:
        pytest.skip("this build genuinely serves 8001")
    assert not _resolve(monkeypatch).endswith(":8001")


def test_an_explicit_override_wins(monkeypatch):
    assert _resolve(monkeypatch, SESSION_TRACKER_API="http://127.0.0.1:9999") == (
        "http://127.0.0.1:9999"
    )


def test_a_trailing_slash_is_trimmed(monkeypatch):
    """Paths are appended as f"{API}{path}" with a leading slash — no doubling."""
    assert _resolve(monkeypatch, SESSION_TRACKER_API="http://127.0.0.1:9999/") == (
        "http://127.0.0.1:9999"
    )


def test_a_bind_all_host_becomes_loopback(monkeypatch):
    """0.0.0.0 is a bind address, not something you can dial."""
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "dashboard_host", "0.0.0.0")
    assert _resolve(monkeypatch).startswith("http://127.0.0.1:")


def test_the_module_constant_uses_the_resolver(monkeypatch):
    """The helper is worthless if API is still a literal."""
    module = importlib.import_module("scripts.live_session_tracker")
    from config.settings import get_settings

    assert module.API.endswith(f":{int(get_settings().dashboard_port)}")


def test_it_resolves_when_RUN_AS_A_SCRIPT(monkeypatch):
    """The way it actually runs — `python scripts/live_session_tracker.py`.

    Importing it as `scripts.live_session_tracker` puts the repo root on sys.path, so
    `from config.settings import get_settings` succeeds and every test above passes.
    Executed as a script, sys.path[0] is the SCRIPTS directory, `config` is not
    importable, and a resolver that leans on that import falls silently back to the
    old hardcoded 8001 — which is exactly what the first version of this fix did. The
    tracker only ever runs the second way, so this is the case that matters.
    """
    import subprocess
    import sys
    from pathlib import Path

    from config.settings import get_settings

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "live_session_tracker.py"
    probe = (
        "import runpy,sys;"
        f"mod=runpy.run_path(r'{script}', run_name='not_main');"
        "print(mod['API'])"
    )
    # cwd = scripts/, so nothing puts the repo root on sys.path for us.
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(root / "scripts"), capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-800:]
    resolved = out.stdout.strip().splitlines()[-1]
    assert resolved.endswith(f":{int(get_settings().dashboard_port)}"), resolved
