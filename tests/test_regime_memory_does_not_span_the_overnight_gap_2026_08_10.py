"""A process left running overnight voted yesterday's regimes into today's open.

RegimeDetector keeps one label PER BAR in `_regime_history`, keyed "{instrument}_{tf}",
and `_smooth_regime` returns the majority over the last `regime_smoothing_window` (5)
entries. That dict lives on the long-lived `self.intel.regime` detector.

The session rollover explicitly purges the caches that must not cross midnight —
_prev_close_cache, _vix_session_open_cache, _breadth_asof_cache — with a comment saying
"a process running overnight must not serve yesterday's ... into today's gap analysis".
It never reset the regime history, and never cleared latest_regimes/_timeframe_regimes.
The only live `reset_history()` call is guarded by `if self.mode == "HISTORICAL"`
(scanner.py ~9799), so in REAL it never runs.

    Monday closes with four TRENDING_UP 5-min bars on NIFTY.
    Tuesday gaps down 1.2%; the first bar detects TRENDING_DOWN.
    _smooth_regime sees [UP, UP, UP, UP, DOWN] -> returns TRENDING_UP.
    _recent_trend_regime sees ups=4 >= 3 -> also TRENDING_UP.

For the first ~20 minutes every consumer is wrong at once: regime_aligns admits fresh
BUY signals into a gap-down, uses_tight_trailing returns False so live SHORT trades keep
the loose trend trail, and _regime_opposes_direction reports no opposition so
LIVE_REVERSAL_EXIT withholds its profit-take.

Resetting is safe by construction: _smooth_regime returns None on an empty history and
_recent_trend_regime returns None below 3 entries, so the detector simply falls back to
today's raw label until it has real votes. Indicator WARM-UP is untouched — ADX/ATR still
read the multi-day candle frame; only the vote memory is cleared.
"""
from types import SimpleNamespace

import pytest

from intelligence.regime_detector import RegimeDetector
from scanner import Scanner
from tests.source_views import scanner_runtime_source


# ── the premise: yesterday's votes really do decide today ────────────────────

def test_yesterdays_votes_outvote_todays_gap_down():
    d = RegimeDetector()
    d._regime_history["NIFTY_5min"] = [
        "TRENDING_UP", "TRENDING_UP", "TRENDING_UP", "TRENDING_UP", "TRENDING_DOWN"
    ]
    assert d._smooth_regime("NIFTY_5min", 5) == "TRENDING_UP"
    assert str(d._recent_trend_regime("NIFTY_5min")).endswith("TRENDING_UP")


def test_an_empty_history_is_safe_not_a_crash():
    """Why clearing is the right remedy rather than a hazard."""
    d = RegimeDetector()
    assert d._smooth_regime("NIFTY_5min", 5) is None
    assert d._recent_trend_regime("NIFTY_5min") is None


def test_a_reset_lets_todays_first_bar_speak_for_itself():
    d = RegimeDetector()
    d._regime_history["NIFTY_5min"] = ["TRENDING_UP"] * 4
    d.reset_history()
    d._regime_history["NIFTY_5min"] = ["TRENDING_DOWN"]
    assert d._smooth_regime("NIFTY_5min", 5) == "TRENDING_DOWN"


# ── the rollover must clear it ───────────────────────────────────────────────

def _scanner_at_rollover():
    sc = Scanner.__new__(Scanner)
    det = RegimeDetector()
    det._regime_history["NIFTY_5min"] = ["TRENDING_UP"] * 5
    det._regime_history["BANKNIFTY_15min"] = ["TRENDING_DOWN"] * 5
    det._regime_history_bar["NIFTY_5min"] = "2026-08-10 15:25:00"

    sc.intel = SimpleNamespace(regime=det)
    sc.mode = "REAL"
    sc.latest_regimes = {"NIFTY": "TRENDING_UP", "BANKNIFTY": "TRENDING_DOWN"}
    sc._timeframe_regimes = {"NIFTY_5min": "TRENDING_UP", "BANKNIFTY_15min": "TRENDING_DOWN"}

    for attr in (
        "_session_trade_candidates", "_pending_live_signals",
        "_last_opposite_exit_signal_time", "_pending_opposite_exit", "_last_intel_fetch",
        "_chain_cache", "_chain_quality_cache", "_last_chain_fetch", "_premium_cache",
        "_candidate_ltp_cache", "_candidate_process_cache", "_cached_results",
        "_signal_preflight_cache", "_prev_close_cache", "_vix_session_open_cache",
        "_breadth_asof_cache", "_intel_cache", "_intel_cache_time",
    ):
        setattr(sc, attr, {})

    import datetime as _dt

    sc._session_rollover_date = _dt.date(2026, 8, 10)
    sc._session_candidate_day = "2026-08-10"
    sc._loaded_session_candidate_day = ""
    sc._loaded_session_payload_count = 0
    sc._last_empty_session_persist_skip = ""
    sc._daily_reset_done = False
    sc.trades = SimpleNamespace(
        reset_session=lambda d: {"active_retained": 0},
        reconcile_broker_positions=lambda: None,
    )
    sc.risk_manager = SimpleNamespace(reset_for_session=lambda d: None)
    sc.expiry = SimpleNamespace(pre_market_check=lambda: {})
    sc.market_info = {}
    return sc, det


def _rollover(sc, day="2026-08-11"):
    """Rollover takes a datetime and only runs after 09:00."""
    import datetime as _dt

    when = _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(9, 20))
    return Scanner._rollover_session_if_needed(sc, when)


def test_rollover_clears_the_regime_vote_history():
    sc, det = _scanner_at_rollover()
    _rollover(sc)
    assert not det._regime_history, (
        f"yesterday's regime votes survived into today: {dict(det._regime_history)}"
    )


def test_rollover_clears_the_bar_dedup_map_too():
    """Otherwise the first bar of the new day can be mistaken for a revision of the last
    bar of the old one and overwrite instead of appending."""
    sc, det = _scanner_at_rollover()
    _rollover(sc)
    assert not det._regime_history_bar


def test_rollover_clears_the_published_regime_labels():
    sc, _ = _scanner_at_rollover()
    _rollover(sc)
    assert not sc.latest_regimes, "yesterday's 5min labels still feed today's entry gates"
    assert not sc._timeframe_regimes


def test_rollover_still_does_everything_it_did_before():
    """The reset must not disturb the caches the rollover already handled."""
    sc, _ = _scanner_at_rollover()
    assert _rollover(sc) is True
    import datetime as _dt

    assert sc._prev_close_cache == {}
    assert sc._vix_session_open_cache == {}
    assert sc._cached_results == {}
    assert sc._session_rollover_date == _dt.date(2026, 8, 11)


def test_a_detector_without_reset_history_does_not_break_the_rollover():
    sc, _ = _scanner_at_rollover()
    sc.intel = SimpleNamespace(regime=SimpleNamespace())
    assert _rollover(sc) is True


def test_a_raising_reset_does_not_break_the_rollover():
    """The rollover also reconciles broker positions; a regime-cache failure must not
    prevent that from running."""
    sc, _ = _scanner_at_rollover()
    reconciled = []
    sc.trades.reconcile_broker_positions = lambda: reconciled.append(1)

    class Boom:
        def reset_history(self, instrument=None):
            raise RuntimeError("detector busy")

    sc.intel = SimpleNamespace(regime=Boom())
    assert _rollover(sc) is True
    assert reconciled == [1], "broker reconciliation was skipped by a regime-reset failure"


def test_no_rollover_on_the_same_day_leaves_history_alone():
    sc, det = _scanner_at_rollover()
    assert _rollover(sc, "2026-08-10") is False
    assert det._regime_history, "history was cleared without a session change"


# ── the premise that made this reachable ─────────────────────────────────────

def test_the_scan_path_reset_is_still_historical_only():
    """The pre-existing call site stays REAL-unreachable, which is why the rollover had
    to own this. Scans the OTHER occurrence, not the one added to the rollover."""
    import inspect

    src = scanner_runtime_source()
    sites = [i for i in range(len(src)) if src.startswith('"reset_history"', i)]
    assert len(sites) >= 2, "expected the scan-path site plus the rollover site"

    guarded = [i for i in sites if 'scanner.mode == "HISTORICAL"' in src[max(0, i - 400):i]]
    assert guarded, "the scan-path reset is no longer HISTORICAL-gated"


def test_the_rollover_reset_is_not_mode_gated():
    """It must run in REAL — that is the entire point."""
    import inspect

    src = inspect.getsource(Scanner._rollover_session_if_needed)
    assert "reset_history" in src
    idx = src.index("reset_history")
    assert 'mode == "HISTORICAL"' not in src[max(0, idx - 400):idx], (
        "the rollover reset inherited the HISTORICAL guard and cannot fire in REAL"
    )
