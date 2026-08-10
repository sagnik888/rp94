"""Twelve settings added this session were unreachable from the dashboard.

A control needs THREE registrations in dashboard/server.py to work, and the file itself
records what happens when one is missed:

    "Market LTP shock + rolling mood: whitelisted in _ADVANCED_CONFIG_FIELDS but
     previously never forwarded here — the dashboard controls were silently
     dropped (2026-07-10)."

So whitelisting alone renders a control in the UI, accepts the click, and discards the
value. All three are asserted here:

  1. _ADVANCED_CONFIG_FIELDS          — the whitelist
  2. _ADVANCED_BOOLEAN_CONFIG_FIELDS  — bools only, so "false" is not read as truthy
  3. explicit  name=msg.get("name")   — the forwarding the 2026-07-10 regression missed

The two that matter most are the backtest A/B levers: block_opposite_direction_stack
(measured as the bulk of a 194-trade shortfall against v7) and the 1min lane controls.
Without these an operator has to stop the process and hand-edit .env, because the running
system rewrites that file.
"""
import re

import pytest

import dashboard.server as server

BOOLEAN_CONTROLS = (
    "block_opposite_direction_stack",
    "consecutive_loss_streak_per_index",
    "surge_tick_proxy_enabled",
    "intel_reversal_corroboration_enabled",
    "partial_entry_remainder_cancel_enabled",
    "morning_refresh_catchup_at_live_open",
)
NUMERIC_CONTROLS = (
    "surge_tick_percentile_floor",
    "intel_reversal_corroboration_votes",
    "partial_entry_remainder_timeout_seconds",
    "kite_rest_backoff_seconds",
    "kite_rest_backoff_max_seconds",
    "calendar_coverage_warn_days",
)
ALL_CONTROLS = BOOLEAN_CONTROLS + NUMERIC_CONTROLS


@pytest.fixture(scope="module")
def source():
    import inspect

    return inspect.getsource(server)


@pytest.mark.parametrize("name", ALL_CONTROLS)
def test_the_control_is_whitelisted(name):
    assert name in server._ADVANCED_CONFIG_FIELDS


@pytest.mark.parametrize("name", BOOLEAN_CONTROLS)
def test_boolean_controls_are_coerced(name):
    """Without this a UI "false" arrives as a truthy string and the toggle only ever
    turns things ON."""
    assert name in server._ADVANCED_BOOLEAN_CONFIG_FIELDS


@pytest.mark.parametrize("name", ALL_CONTROLS)
def test_the_control_is_actually_forwarded(name, source):
    """The 2026-07-10 regression: whitelisted, rendered, clicked, silently dropped."""
    assert f'{name}=msg.get("{name}")' in source, (
        f"{name} is whitelisted but never forwarded — the control does nothing"
    )


@pytest.mark.parametrize("name", NUMERIC_CONTROLS)
def test_numeric_controls_are_not_coerced_to_bool(name):
    assert name not in server._ADVANCED_BOOLEAN_CONFIG_FIELDS


# ── the receiving end actually applies them ──────────────────────────────────

def test_configure_applies_arbitrary_forwarded_settings():
    """configure() takes **kwargs and setattr()s every non-None one onto settings, which
    is what makes the forwarding above sufficient. If that loop is ever narrowed to a
    fixed list, every control here goes inert again."""
    import ast
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner.configure)
    assert "for k, v in kwargs.items():" in src
    assert "setattr(settings, k, v)" in src
    sig = inspect.signature(Scanner.configure)
    assert any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()), (
        "configure no longer accepts **kwargs; forwarded controls would raise TypeError"
    )


@pytest.mark.parametrize("name", ALL_CONTROLS)
def test_the_setting_exists_on_settings(name):
    """A control forwarded for a field that does not exist would silently create an
    attribute nothing reads."""
    from config.settings import Settings

    assert name in Settings.model_fields


# ── the two that carry the backtest experiment ───────────────────────────────

def test_the_ab_levers_are_reachable():
    """These are the ones being A/B'd against the v7 trade-count gap; if either is
    unreachable the experiment needs a process stop and a hand-edited .env."""
    for lever in ("block_opposite_direction_stack", "consecutive_loss_streak_per_index"):
        assert lever in server._ADVANCED_CONFIG_FIELDS
        assert lever in server._ADVANCED_BOOLEAN_CONFIG_FIELDS
