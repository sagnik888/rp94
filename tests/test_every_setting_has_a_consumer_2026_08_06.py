"""A setting nothing reads is a control that looks like it is working.

41 fields in config/settings.py have no consumer anywhere in the codebase, and the live
.env sets every one of them. The sharp example is ut_correlated_core_limit: its own
comment reads "Maximum simultaneous same-direction NIFTY/BANKNIFTY/SENSEX exposures" and
it is set to 2, while _passes_correlated_index_guard takes its limit from
_active_bucket_cap('CORE'), whose live answer is 6 — and 9 on a rare burst. Three times
the configured limit, on the concentration control that matters most as size grows.

Another eleven belong to a market-intel pullback guard that does not exist at all, with
market_intel_pullback_guard_enabled=True in .env.

This test keeps the list honest: a NEW settings field with no consumer fails here unless
it is listed in config/inert_settings.py with a reason. Nothing is wired up automatically
— that is a decision about live risk and it belongs to the operator.
"""
import ast
import io
import os
import re

import pytest

from config.inert_settings import INERT, configured_but_inert, env_keys_from_file

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {
    "venv", ".claude", ".git", "node_modules", "data_store", "logs",
    "runtime_logs", "reports", "plans", "tests", "__pycache__",
}


def _settings_fields():
    src = io.open(os.path.join(REPO, "config", "settings.py"), encoding="utf-8").read()
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef))
    return [
        n.target.id
        for n in cls.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    ]


def _consumer_text():
    """Text in which a setting name appearing means something READS it.

    Everything outside config/settings.py counts, including scripts/ — an operator tool is
    a real consumer. Inside settings.py only genuine reads count: `self.<name>` and
    `getattr(self, "<name>")`. That deliberately excludes the `name: type = default`
    declaration, a comment naming a path that was never built, and a @field_validator,
    which checks a value nothing goes on to use.
    """
    blob = []
    settings_src = ""
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith((".py", ".js")):
                continue
            path = os.path.normpath(os.path.join(root, name))
            if path.endswith(os.path.join("config", "inert_settings.py")):
                continue          # a list of dead names is not a consumer of them
            try:
                text = io.open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if path.endswith(os.path.join("config", "settings.py")):
                settings_src = text
                continue
            # A comment NAMING a setting is not a consumer of it. The preflight warning
            # about inert settings names one in a comment, and without this the setting
            # it warns about looked wired by virtue of being warned about.
            blob.append(re.sub(r"#.*$", "", text, flags=re.M))
    blob.extend(re.findall(r"self\.(\w+)", settings_src))
    blob.extend(re.findall(r"getattr\(\s*self\s*,\s*[\"'](\w+)[\"']", settings_src))
    return "\n".join(blob)


def test_no_setting_is_silently_unread():
    fields = _settings_fields()
    text = _consumer_text()
    unread = {f for f in fields if f not in text}
    undeclared = sorted(unread - set(INERT))
    assert not undeclared, (
        "these settings have no consumer and are not listed in config/inert_settings.py "
        "— an operator setting one of them in .env would see no effect and no warning:\n  "
        + "\n  ".join(undeclared)
    )


def test_the_inert_list_does_not_rot():
    """A listed setting that gained a consumer must come OFF the list."""
    text = _consumer_text()
    now_wired = sorted(name for name in INERT if name in text)
    assert not now_wired, (
        "these are listed as inert but something reads them now; remove them from "
        "config/inert_settings.py so the list stays trustworthy:\n  "
        + "\n  ".join(now_wired)
    )


def test_every_inert_entry_explains_itself():
    """A bare name teaches nobody anything; the reason is the whole value."""
    thin = sorted(n for n, why in INERT.items() if len(str(why or "").strip()) < 25)
    assert not thin, f"inert entries without a real explanation: {thin}"


def test_the_core_concentration_limit_is_no_longer_inert():
    """It was the sharpest example on this list; it is now wired to the guard."""
    assert "ut_correlated_core_limit" not in INERT
    assert "ut_correlated_allow_aplus_override" not in INERT


def test_the_pullback_guard_settings_are_gone_rather_than_catalogued():
    """A guard that was configured and never built is better removed than documented."""
    import config.settings as cs

    fields = set(cs.Settings.model_fields)
    dead = {f for f in fields if f.startswith("market_intel_pullback_")}
    assert not dead, f"still declared: {sorted(dead)}"
    assert not any(k.startswith("market_intel_pullback_") for k in INERT)


def test_configured_but_inert_reports_what_the_operator_actually_set():
    found = dict(configured_but_inert(["pcr_near_strikes", "kite_api_key", "UT_PRESET"]))
    assert "pcr_near_strikes" in found
    assert "kite_api_key" not in found


def test_configured_but_inert_is_case_insensitive():
    assert dict(configured_but_inert(["PCR_NEAR_STRIKES"]))


def test_an_empty_env_reports_nothing():
    assert configured_but_inert([]) == []


def test_a_missing_env_file_is_not_an_error():
    assert env_keys_from_file("no-such-file-here.env") == []


def test_the_live_env_is_reported(capsys):
    """Informational: print what this account currently has set but inert."""
    rows = configured_but_inert(env_keys_from_file(os.path.join(REPO, ".env")))
    if rows:
        print(f"\n{len(rows)} setting(s) set in .env with no consumer:")
        for name, why in rows:
            print(f"  {name}\n      {why}")
    assert isinstance(rows, list)


# ── the operator has to actually see it ──────────────────────────────────────

# These two drove the real .env, so they passed only while the operator's config was
# actually dirty and FAILED the moment it was cleaned (2026-08-07: 32 inert keys were
# commented out, and both tests inverted). A test that breaks when the bug is fixed is
# testing the config, not the code. They now drive a synthetic key list, and the state
# of the real .env is asserted separately below.

def _with_env_keys(monkeypatch, keys):
    """Point the preflight's env reader at a synthetic key list.

    safe_mode imports env_keys_from_file INSIDE the function, so patching the attribute
    on config.inert_settings is what the call actually resolves.
    """
    import config.inert_settings as inert_mod

    monkeypatch.setattr(inert_mod, "env_keys_from_file", lambda *a, **k: list(keys))


def test_the_preflight_warns_about_configured_but_inert_settings(monkeypatch):
    """Drive validate_real_mode_readiness, not just the helper."""
    import config.inert_settings as inert_mod
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    _with_env_keys(monkeypatch, sorted(inert_mod.INERT)[:8])
    text = " | ".join(validate_real_mode_readiness(get_settings()).get("warnings") or [])
    assert "have no consumer and do nothing" in text, (
        "an .env full of settings that do nothing and startup said nothing"
    )
    assert "config/inert_settings.py" in text, "the warning does not say where to look"


def test_the_warning_names_the_worst_offender_first(monkeypatch):
    """Alphabetical order happens to put the risk knobs late; make sure the count is
    stated so a truncated list is never mistaken for the whole list."""
    import config.inert_settings as inert_mod
    from config.settings import get_settings
    from engine.safe_mode import validate_real_mode_readiness

    sample = sorted(inert_mod.INERT)[:10]
    _with_env_keys(monkeypatch, sample)
    line = next(
        w for w in validate_real_mode_readiness(get_settings())["warnings"]
        if "have no consumer" in w
    )
    assert str(len(sample)) in line
    assert "more" in line, "truncated the list without saying it was truncated"


def test_no_inert_setting_is_live_in_the_real_env():
    """The regression that matters now: a dead knob must not creep back into .env.

    Commented-out lines do not count -- env_keys_from_file skips them -- so the record
    of what was once configured, and why it does nothing, stays in the file.
    """
    rows = configured_but_inert(env_keys_from_file(os.path.join(REPO, ".env")))
    assert not rows, (
        "these .env settings have no consumer and will read as working controls: "
        + ", ".join(name for name, _ in rows)
    )
