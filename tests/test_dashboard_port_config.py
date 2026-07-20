from pathlib import Path

from config.settings import get_settings


DASHBOARD_PORT = "8001"
OLD_DASHBOARD_PORTS = ("7000", "7001", "7005")


ACTIVE_DASHBOARD_FILES = [
    Path(".env"),
    Path("README.md"),
    Path("Start_UTBot.bat"),
    Path("config/settings.py"),
    Path("dashboard/server.py"),
    Path("dashboard/static/index.html"),
    Path("engine/notification_manager.py"),
    Path("main.py"),
    Path("scripts/audit_backtest_windows.py"),
    Path("scripts/open_dashboard_when_ready.ps1"),
    Path("start.bat"),
    Path("test_ws.py"),
]


def test_dashboard_settings_use_single_port():
    settings = get_settings()

    assert settings.dashboard_port == int(DASHBOARD_PORT)
    assert settings.fyers_redirect_uri == f"http://127.0.0.1:{DASHBOARD_PORT}/"


def test_active_dashboard_files_do_not_reference_retired_ports():
    forbidden = [
        f"localhost:{port}"
        for port in OLD_DASHBOARD_PORTS
    ] + [
        f"127.0.0.1:{port}"
        for port in OLD_DASHBOARD_PORTS
    ]

    for path in ACTIVE_DASHBOARD_FILES:
        # `.env` is a gitignored, per-host secrets file: it is absent on fresh
        # checkouts and in CI. Enforce the port rule on it only when it exists.
        if not path.exists():
            assert path == Path(".env"), f"committed dashboard file missing: {path}"
            continue
        text = path.read_text(encoding="utf-8")
        assert DASHBOARD_PORT in text, f"{path} should reference the active dashboard port"
        for marker in forbidden:
            assert marker not in text, f"{path} still references retired dashboard URL {marker}"
