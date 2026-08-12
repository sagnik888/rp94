"""Re-entry on a rejected-but-maturing signal fired 3 times in 19 sessions.

Against 60,425 rejections. The feature is fully built — 17 settings, .env pins, its own
candidate builder — and it is the piece of the operator's described workflow that does
not run: "rejected signals are re-tracked till the next candle, and if the move is going
as expected the system re-enters".

The strict path demands FOUR conditions simultaneously:

    is_trending_regime(regime) AND regime_aligns(regime, side) AND adx >= 24 AND intel_ok

with confidence and cascade gating separately. The engine labels RANGING on ~62% of
events, so is_trending_regime alone fails most seeds and the other three are rarely even
evaluated — a short-circuit chain that could not report where it died.

Two changes here:
  * every condition is now COUNTED, not short-circuited, and each failure increments a
    diagnostic counter, so the binding condition is visible
  * an A/B mode admits N of 6 instead of all of one branch

What scored mode does NOT relax — these are safety, not quality: the seed must still be
20s-6min old, there must be runway before square-off, price must not have run past the
chase limit, and every repaint rejection stands.
"""
import inspect

import pytest

from scanner import Scanner


@pytest.fixture(autouse=True)
def _pinned():
    from config.settings import Settings, get_settings

    s = get_settings()
    keys = ("missed_signal_reentry_scored_mode_enabled", "missed_signal_reentry_min_conditions",
            "missed_signal_reentry_min_confidence", "missed_signal_reentry_min_trend_adx")
    saved = {k: getattr(s, k, None) for k in keys}
    for k in keys:
        setattr(s, k, Settings.model_fields[k].default)
    yield
    for k, v in saved.items():
        setattr(s, k, v)


SRC = inspect.getsource(Scanner)


# ── the A/B ships inert ──────────────────────────────────────────────────────

def test_scored_mode_is_off_by_default():
    """It is an A/B: the strict path must be byte-identical until switched on."""
    from config.settings import Settings

    assert Settings.model_fields["missed_signal_reentry_scored_mode_enabled"].default is False
    assert Settings.model_fields["missed_signal_reentry_min_conditions"].default == 5


def test_the_strict_branch_still_admits_on_its_own():
    """strict_ok is evaluated first and independently, so nothing about scored mode can
    take away an entry the old logic would have made."""
    idx = SRC.index("strict_ok = bool(")
    assert SRC.index("if strict_ok:", idx) > idx
    assert SRC.index("elif scored_mode", idx) > SRC.index("if strict_ok:", idx)


def test_scored_mode_is_only_a_fallback():
    """It runs in the elif — it can only ADD entries the strict path refused."""
    i_strict = SRC.index("if strict_ok:")
    i_scored = SRC.index("elif scored_mode and met >= need:")
    i_reject = SRC.index('return False, "no fresh cascade/trend-intel support"')
    assert i_strict < i_scored < i_reject


# ── the six counted conditions ───────────────────────────────────────────────

EXPECTED = ("confidence", "trending_regime", "regime_aligns", "adx", "intel", "cascade")


@pytest.mark.parametrize("name", EXPECTED)
def test_each_condition_is_counted(name):
    block = SRC[SRC.index("checks = {"):SRC.index("met = sum(")]
    assert f'"{name}"' in block


def test_there_are_exactly_six():
    block = SRC[SRC.index("checks = {"):SRC.index("for name, ok in checks.items()")]
    assert block.count('": ') == 6, "the scored set changed size; min_conditions bound moves with it"


def test_conditions_are_evaluated_not_short_circuited():
    """The whole point of the rewrite: a short-circuit chain cannot say which condition
    killed a seed, and 3-in-19-sessions gave nothing to work from."""
    block = SRC[SRC.index("checks = {"):SRC.index("met = sum(")]
    assert " and " not in block, "the checks dict short-circuits again"


def test_every_failure_increments_a_diagnostic():
    assert '_diag_inc("missed_reentry_fail", name)' in SRC


@pytest.mark.parametrize("gate", ["age_too_new", "age_expired", "no_runway", "chase"])
def test_the_mandatory_gates_are_instrumented_too(gate):
    """Otherwise a seed dying on runway looks identical to one dying on regime."""
    assert f'_diag_inc("missed_reentry_fail", "{gate}")' in SRC


# ── the requirement is bounded ───────────────────────────────────────────────

def test_min_conditions_is_clamped_to_the_set_size():
    """A stray 0 would admit everything; a stray 99 would admit nothing."""
    seg = SRC[SRC.index("need = max(1, min(len(checks), need))") - 400:]
    assert "need = max(1, min(len(checks), need))" in seg


def test_a_non_numeric_requirement_falls_back():
    seg = SRC[SRC.index("missed_signal_reentry_min_conditions"):]
    assert "except (TypeError, ValueError)" in seg[:400]


# ── what must never be relaxed ───────────────────────────────────────────────

def test_the_age_window_is_never_relaxed():
    """A stale seed is a different trade, not a late one."""
    for gate in ('return False, "too new for re-entry"', 'return False, "missed signal expired"'):
        idx = SRC.index(gate)
        assert "scored_mode" not in SRC[max(0, idx - 300):idx]


def test_the_chase_limit_is_never_relaxed():
    """Re-entering after price has already run is buying the top of the move."""
    idx = SRC.index("if not chase_ok:")
    assert "scored_mode" not in SRC[max(0, idx - 300):idx]


def test_the_runway_check_is_never_relaxed():
    idx = SRC.index('return False, "not enough runway for missed re-entry"')
    assert "scored_mode" not in SRC[max(0, idx - 300):idx]


def test_confidence_becomes_a_counted_condition_only_in_scored_mode():
    """In strict mode it must still hard-gate exactly as before; in scored mode it has to
    stop short-circuiting or the score is never computed."""
    idx = SRC.index('return False, f"confidence below re-entry floor')
    guard = SRC[max(0, idx - 400):idx]
    assert "missed_signal_reentry_scored_mode_enabled" in guard
    assert "not self._setting_enabled" in guard


def test_repaint_rejection_is_untouched():
    """The operator's rule: ignore every repainted signal, re-entry or not."""
    assert "Repainted during" in SRC or "_repaint_signal_status" in SRC


# ── the admitted reason is legible ───────────────────────────────────────────

def test_a_scored_admission_names_the_conditions_it_met():
    block = SRC[SRC.index("elif scored_mode and met >= need:"):]
    assert "re-entry conditions met" in block[:900]
    assert "support_type = \"scored_reentry\"" in block[:900]


def test_a_refusal_reports_what_failed():
    """So the operator can see which condition to move, rather than guessing."""
    idx = SRC.index('return False, "no fresh cascade/trend-intel support"')
    block = SRC[idx:idx + 500]
    assert '"failed"' in block and '"conditions_met"' in block


# ── dashboard reachability ───────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["missed_signal_reentry_scored_mode_enabled",
                                  "missed_signal_reentry_min_conditions"])
def test_the_control_reaches_the_dashboard(name):
    import dashboard.server as server

    assert name in server._ADVANCED_CONFIG_FIELDS
    assert f'{name}=msg.get("{name}")' in inspect.getsource(server)


def test_only_the_boolean_is_coerced():
    import dashboard.server as server

    assert "missed_signal_reentry_scored_mode_enabled" in server._ADVANCED_BOOLEAN_CONFIG_FIELDS
    assert "missed_signal_reentry_min_conditions" not in server._ADVANCED_BOOLEAN_CONFIG_FIELDS
