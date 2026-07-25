"""Dashboard port configuration invariants.

Canonical committed port is 8001 (the Fyers redirect URI the app ships with). A host
may legitimately run a second instance on another port while versions are being tested:
`.env` is gitignored and per-host, and start.bat exports its own DASHBOARD_PORT. So the
rules that actually matter are:

  1. the CODE default is the canonical port (env-independent),
  2. no file references a RETIRED port (7000 / 7001 / 7005),
  3. whatever port a host runs, its dashboard port and Fyers redirect URI AGREE —
     a mismatch silently breaks the OAuth redirect.

Asserting a literal 8001 against the env-loaded singleton instead just fails on any
host running a test instance, which is noise rather than a real defect.
"""
import re
from pathlib import Path

from config.settings import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]

CANONICAL_PORT = "8001"
OLD_DASHBOARD_PORTS = ("7000", "7001", "7005")

ACTIVE_DASHBOARD_FILES = [
    Path(".env"),
    Path("README.md"),
    Path("config/settings.py"),
    Path("dashboard/server.py"),
    Path("dashboard/static/index.html"),
    Path("engine/notification_manager.py"),
    Path("main.py"),
    Path("scripts/audit_backtest_windows.py"),
    Path("scripts/open_dashboard_when_ready.ps1"),
    Path("start.bat"),
    Path("Start_UT1.bat"),
    Path("test_ws.py"),
]


def test_canonical_dashboard_port_default(monkeypatch):
    """The shipped code default is the canonical port, independent of any host config.

    `_env_file=None` only skips the .env FILE — pydantic still reads os.environ, and
    importing main.py runs load_dotenv(), so the host's values leak in during a full
    suite run. Clear them explicitly to test the true code default.
    """
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    monkeypatch.delenv("FYERS_REDIRECT_URI", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.dashboard_port == int(CANONICAL_PORT)
    assert defaults.fyers_redirect_uri == f"http://127.0.0.1:{CANONICAL_PORT}/"


def test_runtime_port_and_fyers_redirect_agree():
    """A host may run any port, but the redirect URI must point at that same port —
    a mismatch breaks the Fyers OAuth callback."""
    settings = get_settings()
    redirect = str(getattr(settings, "fyers_redirect_uri", "") or "")
    match = re.search(r":(\d{2,5})", redirect)
    assert match, f"fyers_redirect_uri has no port: {redirect!r}"
    assert int(match.group(1)) == int(settings.dashboard_port), (
        f"dashboard_port={settings.dashboard_port} but fyers_redirect_uri={redirect!r}; "
        "the OAuth callback would not reach the running dashboard."
    )


def test_active_dashboard_files_do_not_reference_retired_ports():
    forbidden = [f"localhost:{p}" for p in OLD_DASHBOARD_PORTS] + [
        f"127.0.0.1:{p}" for p in OLD_DASHBOARD_PORTS
    ]
    for rel in ACTIVE_DASHBOARD_FILES:
        path = ROOT / rel
        if not path.exists():
            # `.env` is gitignored and absent on fresh checkouts / CI.
            assert rel == Path(".env"), f"committed dashboard file missing: {rel}"
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in forbidden:
            assert marker not in text, f"{rel} still references retired dashboard URL {marker}"
