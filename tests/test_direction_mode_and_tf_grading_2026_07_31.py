"""Direction-mode enforcement + per-timeframe grading (from the v6 comparison).

1. `strategy_direction_mode` (LONG_SHORT / LONG_ONLY / SHORT_ONLY) was declared,
   persisted onto candidates and trades, and shown in the UI — but never used to block
   anything, so selecting LONG_ONLY did not stop SHORT entries. Now enforced in the
   live entry loop. No-op under the default LONG_SHORT.

2. process_best_signal can grade each candidate with its own independently refreshed
   timeframe snapshot. Per-timeframe grading is enabled by default; 1-minute data is a
   fast support/monitoring layer for live 15-minute signals, not a renamed 15-minute
   decision.
"""
from types import SimpleNamespace

import pytest

from config.settings import Settings
from engine.signal_processor import SignalProcessor


# ── 1. direction mode ────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,buy_ok,sell_ok", [
    ("LONG_SHORT", True, True),
    ("LONG_ONLY", True, False),
    ("SHORT_ONLY", False, True),
])
def test_direction_mode_gates_the_right_side(mode, buy_ok, sell_ok):
    s = SimpleNamespace(strategy_direction_mode=mode)
    assert SignalProcessor._direction_mode_allows("BUY", s)[0] is buy_ok
    assert SignalProcessor._direction_mode_allows("SELL", s)[0] is sell_ok


def test_direction_mode_accepts_long_short_aliases():
    s = SimpleNamespace(strategy_direction_mode="LONG_ONLY")
    assert SignalProcessor._direction_mode_allows("LONG", s)[0] is True
    assert SignalProcessor._direction_mode_allows("SHORT", s)[0] is False


def test_direction_mode_defaults_to_permissive():
    """A missing/blank setting must never silently block trading."""
    assert SignalProcessor._direction_mode_allows("SELL", SimpleNamespace())[0] is True
    assert SignalProcessor._direction_mode_allows(
        "SELL", SimpleNamespace(strategy_direction_mode="")
    )[0] is True


def test_blocked_direction_gives_a_reason():
    s = SimpleNamespace(strategy_direction_mode="LONG_ONLY")
    ok, reason = SignalProcessor._direction_mode_allows("SELL", s)
    assert ok is False and "LONG_ONLY" in reason


def test_direction_mode_is_enforced_in_the_entry_loop():
    import inspect
    src = inspect.getsource(SignalProcessor.process_best_signal)
    assert "_direction_mode_allows" in src, "declared-but-unenforced was the whole defect"


# ── 2. per-timeframe grading (enabled by default; own-timeframe evidence is fail-closed) ──

def test_feature_ships_enabled_with_fail_closed_own_timeframe_evidence():
    settings = Settings(_env_file=None)
    assert settings.per_timeframe_grading_enabled is True
    assert settings.own_timeframe_intel_fail_closed is True


def _scanner_with_intel():
    return SimpleNamespace(
        _cached_timeframe_intel=lambda inst, tf, allow_fallback=True: {
            "aggregate": {"score": {"1min": 1.1, "5min": 5.5, "15min": 15.5}[tf]},
            "regime": {"regime": {"1min": "RANGING", "5min": "VOLATILE", "15min": "TRENDING_UP"}[tf]},
        }
    )


def test_disabled_yields_no_contexts_so_grading_is_unchanged():
    ctx = SignalProcessor._build_timeframe_contexts(
        _scanner_with_intel(), "NIFTY", SimpleNamespace(per_timeframe_grading_enabled=False)
    )
    assert ctx is None
    # None -> caller's 5-minute values are returned verbatim
    result, score, regime = SignalProcessor._candidate_timeframe_context(
        "15min", {"a": 1}, 3.3, "RANGING", ctx
    )
    assert (result, score, regime) == ({"a": 1}, 3.3, "RANGING")


def test_enabled_grades_each_timeframe_with_its_own_intelligence():
    ctx = SignalProcessor._build_timeframe_contexts(
        _scanner_with_intel(), "NIFTY", SimpleNamespace(per_timeframe_grading_enabled=True)
    )
    _, score15, regime15 = SignalProcessor._candidate_timeframe_context(
        "15min", {}, 3.3, "RANGING", ctx
    )
    _, score5, regime5 = SignalProcessor._candidate_timeframe_context(
        "5min", {}, 3.3, "RANGING", ctx
    )
    # CANONICAL since 2026-08-14, not the raw aggregate["score"] this used to assert.
    # That field is legacy-scaled (-100..100) and was read raw, then handed to
    # normalize_intelligence_score, which infers the scale from magnitude -- so a small
    # legacy reading like 0.9 was never divided and became near-maximum conviction.
    # The builder now converts explicitly, which also matches the flag-OFF path: the
    # caller's intel_score is computed the same way at scanner.py:10414.
    assert (score15, regime15) == (0.155, "TRENDING_UP")
    assert (score5, regime5) == (0.055, "VOLATILE")


def test_stale_exact_timeframe_cache_is_not_used_for_grading():
    import time

    scanner = _scanner_with_intel()
    scanner._intel_cache_time = {
        "NIFTY_1min": time.time() - 31.0,
        "NIFTY_5min": time.time() - 31.0,
        "NIFTY_15min": time.time() - 31.0,
    }
    settings = SimpleNamespace(
        per_timeframe_grading_enabled=True,
        intelligence_analysis_ttl_seconds=30.0,
    )
    assert SignalProcessor._build_timeframe_contexts(scanner, "NIFTY", settings) == {}


def test_unknown_timeframe_falls_back_safely():
    ctx = SignalProcessor._build_timeframe_contexts(
        _scanner_with_intel(), "NIFTY", SimpleNamespace(per_timeframe_grading_enabled=True)
    )
    _, score, regime = SignalProcessor._candidate_timeframe_context(
        "30min", {}, 3.3, "RANGING", ctx
    )
    assert (score, regime) == (3.3, "RANGING")


def test_missing_scanner_cache_is_tolerated():
    ctx = SignalProcessor._build_timeframe_contexts(
        SimpleNamespace(), "NIFTY", SimpleNamespace(per_timeframe_grading_enabled=True)
    )
    assert ctx == {}, "enabled own-timeframe mode must expose a cache miss to fail closed"


def test_grading_call_uses_the_timeframe_context():
    import inspect
    src = inspect.getsource(SignalProcessor.process_best_signal)
    assert "_candidate_timeframe_context" in src
    assert "_tf_intel_score" in src and "_tf_regime" in src


# ── dashboard A/B toggle wiring ──────────────────────────────────────────────

def test_per_timeframe_grading_toggle_is_fully_wired():
    """A whitelisted advanced field that is not ALSO forwarded, broadcast, and placed in
    the General tab's System Controls list is silently dropped or invisible — both have
    happened before in this codebase."""
    from pathlib import Path
    import dashboard.server as srv

    assert "per_timeframe_grading_enabled" in srv._ADVANCED_CONFIG_FIELDS
    assert "per_timeframe_grading_enabled" in srv._ADVANCED_BOOLEAN_CONFIG_FIELDS

    root = Path(__file__).resolve().parents[1]
    assert "per_timeframe_grading_enabled=msg.get(" in (
        root / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert '"per_timeframe_grading_enabled": getattr(get_settings()' in (
        root / "scanner.py").read_text(encoding="utf-8")

    js = (root / "dashboard" / "static" / "js" / "dashboard-settings.js").read_text(encoding="utf-8")
    assert "settPerTfGrading" in js
    assert "cfg.per_timeframe_grading_enabled" in js
    assert "per_timeframe_grading_enabled: document.getElementById" in js
    assert "settPerTfGrading" in js.split("makeCard('settings-system'")[1].split("]))")[0]


def test_toggle_applies_and_reverts_the_live_setting():
    from types import SimpleNamespace as NS
    from config.settings import get_settings

    settings = get_settings()
    original = settings.per_timeframe_grading_enabled
    try:
        sc = SignalProcessor  # noqa: F841 - import guard only
        from scanner import Scanner

        s = Scanner.__new__(Scanner)
        s.mode = "HISTORICAL"
        s.auto_mode = False
        s.on_notification = None
        s.futures_sl_pct, s.options_sl_pct, s.max_daily_loss_pct = 0.3, 15.0, 3.0
        s.risk_fut_pct, s.risk_opt_pct = 2.0, 8.0
        s.capital_fut, s.capital_opt, s.capital_total = 250000.0, 100000.0, 1100000.0
        s.trades = NS(update_risk_settings=lambda *a, **k: None, has_live_exposure=lambda: False)
        s.risk_manager = NS(update_settings=lambda *a, **k: None)
        s._publish_config_snapshot = lambda *a, **k: None
        s.mtf = NS(apply_engine_params=lambda *a, **k: None)

        assert s.configure(per_timeframe_grading_enabled=True, persist_settings=False) is True
        assert get_settings().per_timeframe_grading_enabled is True
        assert s.configure(per_timeframe_grading_enabled=False, persist_settings=False) is True
        assert get_settings().per_timeframe_grading_enabled is False
    finally:
        settings.per_timeframe_grading_enabled = original
