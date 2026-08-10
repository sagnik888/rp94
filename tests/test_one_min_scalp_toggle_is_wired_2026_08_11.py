"""The 1MSCALP top-panel switch, end to end.

This control has more moving parts than most: a top-panel switch in index.html, a handler
in app.js, a second control in the advanced settings panel, and the three server-side
registrations every dashboard setting needs. A break in any one of them leaves a switch
that flips, toasts "1MSCALP: ON", and changes nothing — which is exactly the 2026-07-10
class of regression server.py already records.

Verified behaviourally alongside these assertions: with a genuine 1min tick surge
(percentile 100, range 2.5x ATR, ADX 30) the entry is ADMITTED with the toggle on and
REFUSED with it off, while a 5min spike is unaffected either way.
"""
import inspect
import os
import re

import pytest

import dashboard.server as server

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "dashboard", "static")


@pytest.fixture(scope="module")
def server_src():
    return inspect.getsource(server)


@pytest.fixture(scope="module")
def app_js():
    p = os.path.join(STATIC, "js", "app.js")
    if not os.path.exists(p):
        pytest.skip("app.js not present")
    return open(p, encoding="utf-8", errors="replace").read()


@pytest.fixture(scope="module")
def index_html():
    p = os.path.join(STATIC, "index.html")
    if not os.path.exists(p):
        pytest.skip("index.html not present")
    return open(p, encoding="utf-8", errors="replace").read()


# ── the switch exists and is bound ───────────────────────────────────────────

def test_the_switch_element_exists(index_html):
    assert 'id="oneMinScalpSwitch"' in index_html


def test_the_switch_has_a_change_handler(app_js):
    assert "getElementById('oneMinScalpSwitch')" in app_js
    assert "one_min_scalping_enabled: isChecked" in app_js


def test_the_switch_sends_a_configure_command(app_js):
    idx = app_js.index("one_min_scalping_enabled: isChecked")
    assert "cmd: 'configure'" in app_js[max(0, idx - 300):idx]


def test_the_switch_reflects_server_state(app_js):
    """Otherwise a reload shows the switch in a position the engine is not in."""
    assert "data.config.one_min_scalping_enabled" in app_js


def test_the_html_default_matches_the_settings_default(index_html):
    """A checked box against a False default would misreport the engine on first load."""
    from config.settings import Settings

    m = re.search(r'id="oneMinScalpSwitch"([^>]*)>', index_html)
    assert m, "switch element not found"
    html_on = "checked" in m.group(1)
    assert html_on is bool(Settings.model_fields["one_min_scalping_enabled"].default)


# ── the three server registrations ───────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "one_min_scalping_enabled",
    "one_min_scalping_high_burst_only",
    "volume_spike_entry_enabled",
])
def test_the_setting_is_whitelisted_coerced_and_forwarded(name, server_src):
    assert name in server._ADVANCED_CONFIG_FIELDS, f"{name} not whitelisted"
    assert name in server._ADVANCED_BOOLEAN_CONFIG_FIELDS, (
        f'{name} not boolean-coerced; a UI "false" would read as truthy'
    )
    assert f'{name}=msg.get("{name}")' in server_src, (
        f"{name} whitelisted but never forwarded — the switch would do nothing"
    )


def test_the_configure_command_accepts_advanced_fields(server_src):
    assert re.search(r'"configure":\s*\{.*?\}\s*\|\s*_ADVANCED_CONFIG_FIELDS',
                     server_src, re.S), "configure no longer merges the advanced set"


# ── the engine side actually honours it ──────────────────────────────────────

def test_the_engine_gates_1min_on_the_toggle():
    """scanner refuses a 1min spike outright when the toggle is off."""
    from scanner import Scanner

    src = inspect.getsource(Scanner._detect_volume_spike_entry)
    assert "one_min_scalping_enabled" in src
    idx = src.index("one_min_scalping_enabled")
    assert "return None" in src[idx:idx + 400]


def test_the_toggle_does_not_reach_5min():
    """1MSCALP must not gate the 5min spike path."""
    from scanner import Scanner

    src = inspect.getsource(Scanner._detect_volume_spike_entry)
    idx = src.index("one_min_scalping_enabled")
    window = src[max(0, idx - 200):idx]
    assert '"1min"' in window, "the 1min guard is not scoped to the 1min timeframe"
