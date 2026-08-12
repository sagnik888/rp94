"""The grade the engine computes did not rank outcomes, so the gate got A/B levers.

Across 12 live sessions / 106 trades (rtracker, 28 Jul - 11 Aug 2026):

    corr(grade rank, pnl) = +0.096        i.e. no information, and what little ordering
    A+  n=27  avg Rs. 49   capture 10.8%  there was ran the WRONG way
    A   n=55  avg Rs. 48   capture 14.8%
    B+  n=11  avg Rs.260   capture 42.6%
    B   n= 4  avg Rs.407   capture 62.5%
    C   n= 8  avg Rs.118   capture 32.3%

while grade drove 2,468 "C grade blocked" and 424 "Grade rank below required 2"
rejections — the second and fifth largest reject reasons in the window.

Sharper still: .env pins SIGNAL_GRADE_PREFERENCE='B', so options run at min_rank 2 and
every B row in that book reached the market through the fifteen_min_bplus_ok or
power_move exemption. The best grades are the ones the main gate refused and an
OR-branch let past. Suggestive — but n=15 across B and B+, far too thin to move a gate
that touches every signal.

So both levers ship OFF, and the first assertions here are that they change nothing
until switched on.
"""
import inspect

import pytest

from engine.signal_processor import SignalProcessor

GRADE_HIERARCHY = {"C": 0, "B": 1, "B+": 2, "A": 3, "A+": 4}


# ── they are genuinely off by default ────────────────────────────────────────

def test_both_levers_default_to_inert():
    from config.settings import Settings

    assert Settings.model_fields["grade_gate_min_rank_override"].default == -1
    assert Settings.model_fields["grade_gate_allow_c_enabled"].default is False


def test_the_default_override_does_not_touch_the_preference_ladder():
    """-1 must fall through to signal_grade_preference, not clamp min_rank to 0."""
    src = inspect.getsource(SignalProcessor)
    idx = src.index("grade_gate_min_rank_override")
    window = src[idx:idx + 400]
    assert "rank_override >= 0" in window, (
        "the override no longer distinguishes 'off' from 'allow everything'"
    )


def test_env_pins_the_preference_so_the_default_is_not_the_live_value():
    """The measured book ran at min_rank 2 (options, pref 'B'), not the dataclass 'A'.
    A reader who trusts the dataclass default will mis-size this experiment."""
    import os

    from config.settings import Settings

    assert Settings.model_fields["signal_grade_preference"].default == "A"
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env):
        pytest.skip(".env not present")
    text = open(env, encoding="utf-8", errors="replace").read()
    if "SIGNAL_GRADE_PREFERENCE" in text:
        assert "SIGNAL_GRADE_PREFERENCE='B'" in text, (
            "the pinned grade preference changed; the 424 'Grade rank below required 2' "
            "rejections this experiment is sized against no longer describe live"
        )


# ── the override is bounded ──────────────────────────────────────────────────

def test_the_override_is_clamped_to_the_hierarchy():
    """A stray value must not silently open or close the gate entirely."""
    src = inspect.getsource(SignalProcessor)
    idx = src.index("grade_gate_min_rank_override")
    window = src[idx:idx + 400]
    assert "max(0, min(4, rank_override))" in window


def test_a_non_numeric_override_falls_back_to_off():
    src = inspect.getsource(SignalProcessor)
    idx = src.index("grade_gate_min_rank_override")
    window = src[idx:idx + 400]
    assert "except (TypeError, ValueError)" in window
    assert "rank_override = -1" in window


def test_the_override_is_read_after_the_preference_ladder():
    """Read before it, the preference would overwrite the operator's choice."""
    src = inspect.getsource(SignalProcessor)
    ladder = src.index('elif grade_pref == "A":')
    override = src.index("grade_gate_min_rank_override")
    gate = src.index("Grade rank below required")
    assert ladder < override < gate


# ── allow_c admits nothing on its own ────────────────────────────────────────

def test_allow_c_only_bypasses_the_grade_refusal():
    """It must skip the C REFUSAL, not the confidence/choppy/quality gates below it."""
    src = inspect.getsource(SignalProcessor)
    idx = src.index('record_reject("C grade blocked")')
    window = src[max(0, idx - 500):idx]
    assert "allow_c_grade" in window
    # Everything downstream still runs. Searched FORWARD from the C gate: both names
    # also occur earlier in the class, so a plain .index() finds the wrong one, and a
    # fixed-size slice passes or fails on unrelated edits above it.
    assert src.find("Confidence below", idx) > idx
    assert src.find("_passes_quality_gate", idx) > idx


def test_allow_c_is_read_through_the_settings_helper():
    """A raw truthy read would make the string "false" enable it."""
    src = inspect.getsource(SignalProcessor)
    idx = src.index("grade_gate_allow_c_enabled")
    assert "_setting_enabled(" in src[max(0, idx - 200):idx + 120]


# ── reachable from the dashboard ─────────────────────────────────────────────

NEW_CONTROLS = (
    "entry_preopen_signal_guard_enabled",
    "entry_mark_touch_validation_enabled",
    "entry_mark_max_ask_multiple",
    "entry_mark_min_bid_multiple",
    "profit_lock_min_peak_rs",
    "profit_lock_nonscalp_min_peak_rs",
    "profit_lock_min_r_multiple",
    "pnl_path_logging_enabled",
    "pnl_path_min_interval_seconds",
    "pnl_path_min_move_rs",
    "pnl_path_max_samples",
    "slippage_calibration_max_age_days",
    "grade_gate_min_rank_override",
    "grade_gate_allow_c_enabled",
)
BOOLEAN_CONTROLS = (
    "entry_preopen_signal_guard_enabled",
    "entry_mark_touch_validation_enabled",
    "pnl_path_logging_enabled",
    "grade_gate_allow_c_enabled",
)


@pytest.fixture(scope="module")
def server_src():
    import dashboard.server as server

    return inspect.getsource(server)


@pytest.mark.parametrize("name", NEW_CONTROLS)
def test_the_control_is_whitelisted(name):
    import dashboard.server as server

    assert name in server._ADVANCED_CONFIG_FIELDS


@pytest.mark.parametrize("name", NEW_CONTROLS)
def test_the_control_is_forwarded(name, server_src):
    """The 2026-07-10 regression this file guards against: whitelisted, rendered,
    clicked, silently dropped."""
    assert f'{name}=msg.get("{name}")' in server_src


@pytest.mark.parametrize("name", BOOLEAN_CONTROLS)
def test_boolean_controls_are_coerced(name):
    import dashboard.server as server

    assert name in server._ADVANCED_BOOLEAN_CONFIG_FIELDS


@pytest.mark.parametrize("name", [n for n in NEW_CONTROLS if n not in BOOLEAN_CONTROLS])
def test_numeric_controls_are_not_coerced_to_bool(name):
    import dashboard.server as server

    assert name not in server._ADVANCED_BOOLEAN_CONFIG_FIELDS


@pytest.mark.parametrize("name", NEW_CONTROLS)
def test_the_setting_exists(name):
    from config.settings import Settings

    assert name in Settings.model_fields
