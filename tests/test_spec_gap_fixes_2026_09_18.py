"""Four defects found auditing the live system against the operator's spec (2026-09-18).

1. The 09:00 in-session reset sat behind `if False and now_ist.hour == 9 ...`, so it had
   not run since it was disabled. _rollover_session_if_needed covers everything it did
   EXCEPT the provider quote/history caches, which therefore kept yesterday's expired
   option contracts for the whole session.
2. _peak_giveback_cap_exit defaulted an unknown trade age to the minimum hold
   ("default to pass if we can't find age"), so a trade with a missing or unparseable
   entry stamp SKIPPED the 60s breakeven hold and could be closed on its first pull-back.
   Every other guard in this file refuses to conclude anything from absent evidence.
3. The stabilization wait was recomputed from the CURRENT regime on every maturity pass,
   so a signal announced "for 30s" was silently held to 60s when the regime turned risky
   mid-buffer. Spec point 3 asks for the opposite: cleaner trend, faster entry.
4. A later arrow on the same instrument/timeframe/direction was dropped in silence while
   the first snapshot kept waiting, so the candidate that finally executed carried the
   older price, levels and intelligence.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from scanner import IST, Scanner


# ── 1. session rollover evicts yesterday's contracts, keeps index quotes ──────


def _provider():
    return SimpleNamespace(
        _ws_cache={
            "NSE:NIFTY50-INDEX": {"ltp": 23200.0},
            "BSE:SENSEX-INDEX": {"ltp": 74300.0},
            "NSE:MIDCPNIFTY-INDEX": {"ltp": 14300.0},
            "NIFTY2691823200CE": {"ltp": 180.0},
            "BANKNIFTY26SEP56100PE": {"ltp": 520.0},
        },
        _ltp_cache={"NFO:NIFTY2691823200CE": 180.0, "NSE:NIFTY 50": 23200.0},
        _api_hist_cache={"NIFTY|5min": object()},
        _last_api_hist_fetch={"NIFTY|5min": 1.0},
    )


def test_rollover_drops_expired_contracts_and_keeps_index_rows():
    scanner = Scanner.__new__(Scanner)
    scanner.data = _provider()

    scanner._evict_expired_contract_caches()

    assert sorted(scanner.data._ws_cache) == [
        "BSE:SENSEX-INDEX", "NSE:MIDCPNIFTY-INDEX", "NSE:NIFTY50-INDEX",
    ]
    assert list(scanner.data._ltp_cache) == ["NSE:NIFTY 50"]
    assert scanner.data._api_hist_cache == {}
    assert scanner.data._last_api_hist_fetch == {}


@pytest.mark.parametrize(
    "key, expiring",
    [
        ("NIFTY2691823200CE", True),
        ("NFO:BANKNIFTY26SEP56100PE", True),
        ("SENSEX2691774300PE|17SEP2026|220887813", True),
        ("NSE:NIFTY50-INDEX", False),
        ("NSE:MIDCPNIFTY-INDEX", False),   # contains "CP", not "CE"
        ("NIFTY26SEPFUT", False),
        ("", False),
    ],
)
def test_contract_key_detection(key, expiring):
    assert Scanner._is_expiring_contract_key(key) is expiring


def test_eviction_is_safe_without_a_provider():
    scanner = Scanner.__new__(Scanner)
    scanner.data = None
    scanner._evict_expired_contract_caches()  # must not raise


def test_the_dead_nine_oclock_reset_branch_is_gone():
    """`if False and ...` is not a guard, it is dead code pretending to be one."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("scanner.py").read_text(encoding="utf-8")
    assert "if False and now_ist.hour == 9" not in source


# ── 2. an unknown trade age cannot satisfy the breakeven hold ─────────────────


@pytest.fixture
def _breakeven_only(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "breakeven_stop_enabled", True)
    monkeypatch.setattr(settings, "peak_giveback_cap_enabled", False)
    monkeypatch.setattr(settings, "peak_trail_enabled", False)
    return settings


def _breakeven_trade(entry_time):
    return SimpleNamespace(
        instrument="NIFTY", inst_type="OPT", timeframe="5min", entry_time=entry_time,
    )


def _breakeven_fire(trade, evaluated_at):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "LIVE"
    return Scanner._peak_giveback_cap_exit(
        scanner, trade, current_pnl_rs=5.0, peak_pnl_rs=400.0, evaluated_at=evaluated_at,
    )


def test_a_held_trade_still_hits_the_breakeven_stop(_breakeven_only):
    now = datetime(2026, 9, 18, 11, 0, 0)
    fired, why = _breakeven_fire(_breakeven_trade(now - timedelta(minutes=5)), now)

    assert fired is True
    assert "BREAKEVEN_STOP" in why or "proven trade" in why


def test_a_fresh_trade_is_still_protected_by_the_hold(_breakeven_only):
    now = datetime(2026, 9, 18, 11, 0, 0)
    fired, _ = _breakeven_fire(_breakeven_trade(now - timedelta(seconds=5)), now)

    assert fired is False


@pytest.mark.parametrize("entry_time", [None, "", "not-a-timestamp"])
def test_an_unknown_age_no_longer_passes_the_hold(_breakeven_only, entry_time):
    """Previously `age_seconds = min_age_seconds` made absence satisfy the gate."""
    now = datetime(2026, 9, 18, 11, 0, 0)
    fired, _ = _breakeven_fire(_breakeven_trade(entry_time), now)

    assert fired is False


def test_replay_projections_without_a_clock_keep_their_absolute_arm(_breakeven_only):
    """Replay/session rows carry no entry stamp by design and pass no evaluated_at;
    every production caller does pass one, so only they gain the stricter rule."""
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "LIVE"

    fired, _ = Scanner._peak_giveback_cap_exit(
        scanner, _breakeven_trade(None), current_pnl_rs=5.0, peak_pnl_rs=400.0,
    )

    assert fired is True


# ── 3. the announced stabilization wait binds ─────────────────────────────────


def _stab_scanner(fresh):
    scanner = Scanner.__new__(Scanner)
    scanner._live_signal_stabilization_requirements = lambda candidate: fresh
    return scanner


CANDIDATE = SimpleNamespace(instrument="MIDCPNIFTY", timeframe="15min", direction="SHORT")


def test_a_risky_regime_cannot_silently_extend_an_announced_wait():
    scanner = _stab_scanner((60.0, 3, True))
    pending = {"announced_seconds": 30.0, "announced_scans": 1, "announced_adaptive": False}

    seconds, scans, adaptive = scanner._live_stabilization_requirements_for_pending(CANDIDATE, pending)

    assert (seconds, scans, adaptive) == (30.0, 1, False)
    assert pending["announced_extension_logged"] is True


def test_a_cleaner_regime_may_still_shorten_the_wait():
    scanner = _stab_scanner((10.0, 1, True))
    pending = {"announced_seconds": 30.0, "announced_scans": 2, "announced_adaptive": True}

    seconds, scans, _ = scanner._live_stabilization_requirements_for_pending(CANDIDATE, pending)

    assert (seconds, scans) == (10.0, 1)
    assert "announced_extension_logged" not in pending


def test_a_buffer_restored_without_an_announcement_uses_the_live_requirement():
    scanner = _stab_scanner((45.0, 2, True))

    assert scanner._live_stabilization_requirements_for_pending(CANDIDATE, {}) == (45.0, 2, True)


# ── 4. a newer arrow is adopted into the running buffer ───────────────────────


def _pending_scanner(sig_timestamp):
    scanner = Scanner.__new__(Scanner)
    scanner._diag_inc = lambda *args, **kwargs: None
    scanner._pending_live_signals = {
        "NIFTY_5min_SHORT": {
            "timestamp": datetime(2026, 9, 18, 11, 0, 0),
            "candidate": SimpleNamespace(
                instrument="NIFTY", timeframe="5min", direction="SHORT",
                price=100.0, signal_timestamp=sig_timestamp,
            ),
            "sig_timestamp": sig_timestamp,
            "stable_scans": 2,
        }
    }
    return scanner


def _arrow(stamp, price):
    return SimpleNamespace(
        instrument="NIFTY", timeframe="5min", direction="SHORT",
        price=price, signal_timestamp=stamp,
    )


def test_a_newer_arrow_replaces_the_buffered_snapshot_without_restarting_the_wait():
    first = datetime(2026, 9, 18, 11, 0, 0)
    scanner = _pending_scanner(first)
    started_at = scanner._pending_live_signals["NIFTY_5min_SHORT"]["timestamp"]

    adopted = scanner._refresh_pending_live_signal(
        "NIFTY_5min_SHORT", _arrow(first + timedelta(minutes=5), 118.0),
        datetime(2026, 9, 18, 11, 5, 20),
    )

    pending = scanner._pending_live_signals["NIFTY_5min_SHORT"]
    assert adopted is True
    assert pending["candidate"].price == 118.0
    assert pending["sig_timestamp"] == first + timedelta(minutes=5)
    assert pending["timestamp"] == started_at, "the running wait must not restart"
    assert pending["stable_scans"] == 2
    assert pending["refresh_count"] == 1


@pytest.mark.parametrize("delta_minutes", [0, -5])
def test_a_repeat_or_older_arrow_changes_nothing(delta_minutes):
    first = datetime(2026, 9, 18, 11, 0, 0)
    scanner = _pending_scanner(first)

    adopted = scanner._refresh_pending_live_signal(
        "NIFTY_5min_SHORT", _arrow(first + timedelta(minutes=delta_minutes), 118.0),
        datetime(2026, 9, 18, 11, 5, 20),
    )

    pending = scanner._pending_live_signals["NIFTY_5min_SHORT"]
    assert adopted is False
    assert pending["candidate"].price == 100.0
    assert "refresh_count" not in pending


def test_refreshing_an_unknown_key_is_a_no_op():
    scanner = _pending_scanner(datetime(2026, 9, 18, 11, 0, 0))

    assert scanner._refresh_pending_live_signal(
        "SENSEX_5min_LONG", _arrow(datetime(2026, 9, 18, 11, 5, 0), 55.0),
        datetime(2026, 9, 18, 11, 5, 20),
    ) is False
