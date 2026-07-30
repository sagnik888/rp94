"""Findings from the v6 report, analysed against THIS build before acting.

Two were real here and are fixed; one was analysed and deliberately NOT changed.

APPLIED
  * Top-tier setup test was duplicated verbatim in trading/trade_manager.py and
    scanner.py as `grade_rank >= A+ OR confidence >= 0.90`. Harmless today because grade
    is bucket(confidence) (A+ band floor 0.85, so conf >= 0.90 always grades A+), but it
    becomes a live defect the moment grade is decoupled from confidence — a contradicted
    grade-C signal at 0.92 would gain top-tier privileges. Now one shared definition so
    the two copies cannot drift.
  * Runner mode could be granted on a BORROWED regime: _get_trade_regime falls back to
    the 5-minute label for any timeframe, so a 15-minute trade could ride the looser
    runner trail on a 5-minute reading (sampled 5m/15m regimes disagree in 83% of
    same-timestamp pairs). Runner now requires the trade's own-timeframe regime; the
    fallback is retained for tightening decisions.

NOT APPLIED (analysed, intentional here)
  * The HISTORICAL choppy floor `min(grade_min_confidence("B"), relaxed(0.62))` -> 0.40
    is NOT a typo in this build. `ut_backtest_more_results` is documented as "Loosen
    historical-only gates for exploratory backtests", and the clause is structurally
    identical to its production sibling (both bare 2-arg min); the loosening comes from
    using grade B (0.40) vs grade A (0.70). Changing it to max would make backtests
    stricter than designed. Pinned below so it is not "fixed" by mistake later.
"""
from types import SimpleNamespace

import pytest

import scanner as scanner_module
from engine.signal_quality_policy import (
    GRADE_BANDS, confidence_to_grade, grade_min_confidence, is_top_tier_setup,
)
from scanner import Scanner


# ── shared top-tier definition ───────────────────────────────────────────────

def _legacy_expression(grade, conf):
    ranks = {"C": 0, "B": 1, "B+": 2, "A": 3, "A+": 4, "Recovered": 4}
    return ranks.get(str(grade or "C").split()[0], 0) >= 4 or float(conf or 0.0) >= 0.90


@pytest.mark.parametrize("grade,conf", [
    ("A+", 0.95), ("A+", 0.50), ("A", 0.92), ("A", 0.70), ("B", 0.91),
    ("C", 0.92), ("C", 0.10), ("", 0.0), ("Recovered", 0.10), ("B+", 0.89),
])
def test_shared_helper_preserves_the_original_expression(grade, conf):
    assert is_top_tier_setup(grade, conf) == _legacy_expression(grade, conf)


def test_the_redundancy_is_currently_unreachable():
    """conf >= 0.90 already grades A+, so the second clause adds nothing TODAY."""
    assert confidence_to_grade(0.90) == "A+"
    assert dict(GRADE_BANDS)["A+"] <= 0.90


def test_the_landmine_is_real_if_grade_is_ever_decoupled():
    """A contradicted grade-C at high confidence would gain top-tier privileges."""
    assert is_top_tier_setup("C", 0.92) is True


def test_both_call_sites_use_the_shared_definition():
    """They were duplicated verbatim and drifted together; pin that they cannot again."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("trading/trade_manager.py", "scanner.py"):
        code = "\n".join(
            line.split("#", 1)[0]
            for line in (root / rel).read_text(encoding="utf-8").splitlines()
        )
        assert "is_top_tier_setup" in code, f"{rel} must use the shared definition"
        assert ">= 0.90" not in code.split("_is_aplus_setup")[-1][:400], (
            f"{rel} still inlines the raw confidence clause"
        )


# ── runner must not be granted on a borrowed regime ──────────────────────────

def _scanner(tf_regimes=None, latest=None):
    sc = Scanner.__new__(Scanner)
    sc._timeframe_regimes = dict(tf_regimes or {})
    sc.latest_regimes = dict(latest or {})
    return sc


def _trade(tf="15min"):
    return SimpleNamespace(instrument="NIFTY", timeframe=tf, inst_type="OPT",
                           option_type="CE", direction="LONG", adx_value=30.0)


def _settings(require_own=True):
    return SimpleNamespace(
        trend_runner_let_winners_run_enabled=True,
        trend_runner_min_peak_r_multiple=0.4,
        trend_runner_big_move_min_adx=25.0,
        trend_runner_require_own_timeframe_regime=require_own,
    )


def test_own_timeframe_regime_is_detected():
    key = Scanner._timeframe_intel_key
    sc = _scanner({key("NIFTY", "15min"): "TRENDING_UP"})
    assert sc._trade_regime_is_own_timeframe(_trade("15min")) is True


def test_borrowed_five_minute_regime_is_not_own_timeframe():
    key = Scanner._timeframe_intel_key
    sc = _scanner({key("NIFTY", "5min"): "TRENDING_UP"}, {"NIFTY": "TRENDING_UP"})
    assert sc._trade_regime_is_own_timeframe(_trade("15min")) is False
    # ...but a 5-minute trade legitimately uses the instrument-level label
    assert sc._trade_regime_is_own_timeframe(_trade("5min")) is True


def test_runner_denied_on_borrowed_regime(monkeypatch):
    key = Scanner._timeframe_intel_key
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    sc = _scanner({key("NIFTY", "5min"): "TRENDING_UP"}, {"NIFTY": "TRENDING_UP"})
    assert sc._confirmed_trend_runner(
        _trade("15min"), regime="TRENDING_UP", peak_pnl=1000, initial_risk_rs=1000
    ) is False


def test_runner_allowed_on_own_timeframe_regime(monkeypatch):
    key = Scanner._timeframe_intel_key
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings())
    sc = _scanner({key("NIFTY", "15min"): "TRENDING_UP"})
    assert sc._confirmed_trend_runner(
        _trade("15min"), regime="TRENDING_UP", peak_pnl=1000, initial_risk_rs=1000
    ) is True


def test_borrow_behaviour_is_revertible(monkeypatch):
    key = Scanner._timeframe_intel_key
    monkeypatch.setattr(scanner_module, "get_settings", lambda: _settings(require_own=False))
    sc = _scanner({key("NIFTY", "5min"): "TRENDING_UP"}, {"NIFTY": "TRENDING_UP"})
    assert sc._confirmed_trend_runner(
        _trade("15min"), regime="TRENDING_UP", peak_pnl=1000, initial_risk_rs=1000
    ) is True


def test_fallback_regime_still_available_for_tightening():
    """_get_trade_regime must KEEP borrowing — only the runner privilege is gated."""
    key = Scanner._timeframe_intel_key
    sc = _scanner({key("NIFTY", "5min"): "TRENDING_UP"}, {"NIFTY": "TRENDING_UP"})
    assert sc._get_trade_regime(_trade("15min")) == "TRENDING_UP"


# ── deliberately NOT changed ─────────────────────────────────────────────────

def test_historical_choppy_floor_is_intentionally_looser():
    """`ut_backtest_more_results` exists to loosen historical gates; the 0.40 floor is by
    design, not the typo it resembles. Guards against a well-meant min->max 'fix'."""
    from config.settings import Settings
    assert Settings(_env_file=None).ut_backtest_more_results is True
    assert grade_min_confidence("B") == 0.40
    assert grade_min_confidence("A") == 0.70
    assert min(grade_min_confidence("B"), 0.62) == 0.40
