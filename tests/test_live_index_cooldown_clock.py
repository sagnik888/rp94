from datetime import datetime, timedelta

import scanner as scanner_module
from scanner import Scanner


def _scanner(mode="REAL", regime=""):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = mode
    scanner.max_trades_per_index = 9
    scanner.max_consecutive_losses = 2
    scanner.index_cooldown_minutes = 4.0
    scanner._trades_today = {}
    scanner._losses_today = {}
    scanner._last_exit_time = {}
    scanner._reset_daily_counters = lambda: None
    scanner.latest_regimes = {"NIFTY": regime} if regime else {}
    # Bare fixture: no intel cache → _current_day_type falls back to UNDECIDED.
    scanner._cached_timeframe_intel = lambda *a, **k: {}
    return scanner


def _freeze_now(monkeypatch, now):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = now
            return tz.localize(value) if tz is not None else value

    monkeypatch.setattr(scanner_module, "datetime", FixedDateTime)


def test_live_cooldown_blocks_in_choppy_regime(monkeypatch):
    """Regime-scoped cooldown: chop/range tape keeps the revenge-churn guard."""
    now = datetime(2026, 6, 22, 12, 21, 27)
    _freeze_now(monkeypatch, now)
    scanner = _scanner("REAL", regime="CHOPPY")
    scanner._last_exit_time["NIFTY"] = now - timedelta(minutes=2)

    allowed, reason = scanner._can_trade_instrument(
        "NIFTY",
        datetime(2026, 6, 22, 12, 0, 0),
    )

    assert allowed is False
    assert reason.startswith("Cooldown active: 2m remaining")
    assert "choppy" in reason.lower()


def test_live_cooldown_uses_evaluation_time_not_source_candle(monkeypatch):
    """Cooldown clock = live evaluation time, never the forming candle's open."""
    now = datetime(2026, 6, 22, 12, 21, 27)
    _freeze_now(monkeypatch, now)
    scanner = _scanner("REAL", regime="RANGING")
    scanner._last_exit_time["NIFTY"] = now - timedelta(minutes=2)

    allowed, reason = scanner._can_trade_instrument(
        "NIFTY",
        datetime(2026, 6, 22, 12, 0, 0),  # stale candle time must be ignored
    )

    assert allowed is False
    assert reason.startswith("Cooldown active: 2m remaining")


def test_live_cooldown_skipped_on_trend_tape(monkeypatch):
    """Flip engine rule: trend/normal tape re-enters immediately after an exit."""
    now = datetime(2026, 6, 22, 12, 21, 27)
    _freeze_now(monkeypatch, now)
    scanner = _scanner("REAL", regime="TRENDING_UP")
    scanner._last_exit_time["NIFTY"] = now - timedelta(seconds=10)

    allowed, reason = scanner._can_trade_instrument(
        "NIFTY",
        datetime(2026, 6, 22, 12, 0, 0),
    )

    assert allowed is True
    assert reason == ""


def test_live_cooldown_skipped_when_regime_unknown(monkeypatch):
    """Unknown tape is NOT treated as chop — reversals must not be blocked."""
    now = datetime(2026, 6, 22, 12, 21, 27)
    _freeze_now(monkeypatch, now)
    scanner = _scanner("REAL")  # no regime info at all
    scanner._last_exit_time["NIFTY"] = now - timedelta(minutes=1)

    allowed, reason = scanner._can_trade_instrument(
        "NIFTY",
        datetime(2026, 6, 22, 12, 0, 0),
    )

    assert allowed is True


def test_legacy_blanket_cooldown_when_scoping_disabled(monkeypatch):
    """index_cooldown_regime_scoped=False restores the old always-on cooldown."""
    now = datetime(2026, 6, 22, 12, 21, 27)
    _freeze_now(monkeypatch, now)
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "index_cooldown_regime_scoped", False)
    scanner = _scanner("REAL", regime="TRENDING_UP")
    scanner._last_exit_time["NIFTY"] = now - timedelta(minutes=2)

    allowed, reason = scanner._can_trade_instrument(
        "NIFTY",
        datetime(2026, 6, 22, 12, 0, 0),
    )

    assert allowed is False
    assert "legacy" in reason.lower()


def test_live_cooldown_expires_four_minutes_after_exit(monkeypatch):
    now = datetime(2026, 6, 22, 12, 21, 27)
    _freeze_now(monkeypatch, now)
    scanner = _scanner("REAL", regime="CHOPPY")
    scanner._last_exit_time["NIFTY"] = now - timedelta(minutes=4, seconds=1)

    allowed, reason = scanner._can_trade_instrument(
        "NIFTY",
        datetime(2026, 6, 22, 12, 0, 0),
    )

    assert allowed is True
    assert reason == ""


def test_reversion_window_bypasses_choppy_cooldown(monkeypatch):
    """Post-major-win reversion window trades the snap-back even in chop."""
    now = datetime(2026, 6, 22, 12, 21, 27)
    _freeze_now(monkeypatch, now)
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "post_exit_reversion_enabled", True)
    scanner = _scanner("REAL", regime="CHOPPY")
    scanner._last_exit_time["NIFTY"] = now - timedelta(minutes=1)
    scanner._last_major_exits = {
        "NIFTY": {"time": now - timedelta(minutes=1), "direction": "LONG"}
    }

    allowed, reason = scanner._can_trade_instrument(
        "NIFTY",
        datetime(2026, 6, 22, 12, 0, 0),
    )

    assert allowed is True
