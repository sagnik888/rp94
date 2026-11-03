from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_JS = ROOT / "dashboard" / "static" / "js" / "dashboard-settings.js"
APP_JS = ROOT / "dashboard" / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "dashboard" / "static" / "index.html"


def _function_body(source: str, name: str, next_name: str) -> str:
    """Return one top-level dashboard function without relying on line numbers."""
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def test_general_settings_cannot_change_auto_execution():
    """Saving an unrelated setting must never replay stale Auto state."""
    source = SETTINGS_JS.read_text(encoding="utf-8")
    body = _function_body(source, "saveSettings", "resetCache")

    assert "settAutoMode" not in source
    assert "auto_mode:" not in body
    assert "confirm_real_mode:" not in body
    assert "real_mode_verification:" not in body


def test_topbar_is_the_only_guarded_auto_command_path():
    source = APP_JS.read_text(encoding="utf-8")
    settings_source = SETTINGS_JS.read_text(encoding="utf-8")

    assert "const autoSwitch = document.getElementById('autoModeSwitch');" in source
    assert "REAL AUTO MODE will allow live broker orders" in source
    assert "auto_mode: isChecked" in source
    assert "confirm_real_mode: confirmRealMode" in source
    assert "real_mode_verification: realModeVerification" in source
    assert "if (autoSwitch && data.config) autoSwitch.checked = data.config.auto_mode === true;" in settings_source


def test_auto_switch_renders_fail_safe_off_until_server_state_arrives():
    html = INDEX_HTML.read_text(encoding="utf-8")
    tag = re.search(r'<input[^>]+id="autoModeSwitch"[^>]*>', html)

    assert tag, "the dedicated Auto switch is missing"
    assert not re.search(r"\bchecked\b", tag.group(0))
    assert re.search(r'dashboard-settings\.js\?v=[^"\s]+', html)
