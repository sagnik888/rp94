from datetime import datetime
from types import SimpleNamespace

import pytz

from scanner import Scanner
from engine.safe_mode import (
    option_chain_auto_readiness_reasons,
    option_chain_readiness_max_age_seconds,
)


IST = pytz.timezone("Asia/Kolkata")


def test_auto_chain_readiness_uses_startup_bound_not_management_grace():
    settings = SimpleNamespace(
        intelligence_cache_ttl_seconds=90.0,
        option_chain_startup_max_age_seconds=150.0,
        option_chain_intelligence_max_age_seconds=300.0,
    )
    assert option_chain_readiness_max_age_seconds(settings) == 150.0


def test_verified_angel_fallback_can_arm_but_uncertified_fallback_cannot():
    meta = {
        "source": "angelone_near_atm_realtime",
        "fallback": True,
        "auto_certified": True,
        "active_expiry": "15SEP2026",
        "expiry_aligned": True,
    }
    assert option_chain_auto_readiness_reasons(
        meta,
        strike_count=5,
        age_seconds=40.0,
        min_strikes=3,
        max_age_seconds=150.0,
    ) == []
    meta["auto_certified"] = False
    assert "fallback chain" in option_chain_auto_readiness_reasons(
        meta,
        strike_count=5,
        age_seconds=40.0,
        min_strikes=3,
        max_age_seconds=150.0,
    )


def test_startup_chain_age_rejects_management_only_grace():
    meta = {
        "source": "fyers",
        "fallback": False,
        "active_expiry": "15SEP2026",
        "expiry_aligned": True,
    }
    reasons = option_chain_auto_readiness_reasons(
        meta,
        strike_count=61,
        age_seconds=151.0,
        min_strikes=3,
        max_age_seconds=150.0,
    )
    assert reasons == ["chain stale or missing"]


def _scanner(summary):
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
    scanner._morning_refresh_summary = summary
    return scanner


def _settings(**overrides):
    values = {
        "morning_refresh_require_history": True,
        "morning_refresh_require_expiry": True,
        "morning_refresh_require_all_quotes": True,
        "morning_refresh_require_all_chains": True,
        "morning_refresh_require_vix": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_auto_readiness_rejects_missing_or_previous_day_summary(monkeypatch):
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "get_settings", _settings)
    now = IST.localize(datetime(2026, 8, 21, 9, 16))
    missing = _scanner({})
    prior = _scanner({"session_day": "2026-08-20", "complete": True})

    assert "unavailable" in missing._auto_execution_morning_readiness_blockers(now)[0]
    assert "unavailable" in prior._auto_execution_morning_readiness_blockers(now)[0]


def test_auto_readiness_requires_all_four_quotes_and_chains(monkeypatch):
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "get_settings", _settings)
    now = IST.localize(datetime(2026, 8, 21, 9, 16))
    summary = {
        "session_day": "2026-08-21",
        "history": True,
        "expiry": True,
        "index_quotes": 4,
        "chains": 3,
        "indices": {
            name: {"quote": True, "chain": name != "MIDCPNIFTY"}
            for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
        },
        "complete": False,
    }

    blocker = _scanner(summary)._auto_execution_morning_readiness_blockers(now)[0]

    assert "incomplete" in blocker
    assert "chains" in blocker
    assert "chain:MIDCPNIFTY" in blocker


def test_auto_readiness_accepts_complete_current_four_index_summary(monkeypatch):
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "get_settings", _settings)
    now = IST.localize(datetime(2026, 8, 21, 9, 16))
    summary = {
        "session_day": "2026-08-21",
        "history": True,
        "expiry": True,
        "index_quotes": 4,
        "chains": 4,
        "indices": {
            name: {"quote": True, "chain": True}
            for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
        },
        "complete": True,
    }

    assert _scanner(summary)._auto_execution_morning_readiness_blockers(now) == []


def test_auto_readiness_rejects_an_equal_count_but_wrong_index_universe(monkeypatch):
    """Two warmed indices are not proof that a different two were warmed."""
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "get_settings", _settings)
    now = IST.localize(datetime(2026, 8, 21, 9, 16))
    summary = {
        "session_day": "2026-08-21",
        "history": True,
        "expiry": True,
        # Stale aggregate counters deliberately look complete for a two-index setup.
        "index_quotes": 2,
        "chains": 2,
        "active_indices": ["NIFTY", "BANKNIFTY"],
        "indices": {
            "NIFTY": {"quote": True, "chain": True},
            "BANKNIFTY": {"quote": True, "chain": True},
        },
        "complete": True,
    }
    scanner = _scanner(summary)

    blockers = scanner._auto_execution_morning_readiness_blockers(
        now,
        active_indices=["SENSEX", "MIDCPNIFTY"],
    )

    assert blockers
    assert "quote:SENSEX" in blockers[0]
    assert "chain:MIDCPNIFTY" in blockers[0]


def test_auto_readiness_accepts_a_clean_named_subset_of_an_incomplete_global_refresh(
    monkeypatch,
):
    """A disabled index must not veto the exact universe the operator requested."""
    import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "get_settings", _settings)
    now = IST.localize(datetime(2026, 8, 21, 9, 16))
    summary = {
        "session_day": "2026-08-21",
        "history": True,
        "expiry": True,
        "index_quotes": 1,
        "chains": 1,
        "indices": {
            "NIFTY": {"quote": True, "chain": True},
            "SENSEX": {"quote": False, "chain": False},
        },
        # The worker attempted both names, so its global result is honestly partial.
        # NIFTY alone is nevertheless fully established.
        "complete": False,
        "missing_keys": ["quote:SENSEX", "chain:SENSEX"],
    }

    blockers = _scanner(summary)._auto_execution_morning_readiness_blockers(
        now,
        active_indices=["NIFTY"],
    )

    assert blockers == []


def _armed_real_scanner():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.auto_mode = True
    scanner.on_notification = None
    scanner.data = SimpleNamespace()
    scanner.active_indices = ["NIFTY"]
    scanner.capital_total = 500_000.0
    scanner.capital_fut = 250_000.0
    scanner.capital_opt = 100_000.0
    scanner.risk_fut_pct = 2.0
    scanner.risk_opt_pct = 8.0
    scanner._auto_execution_morning_readiness_blockers = (
        lambda now_ist=None, *, active_indices=None: []
    )
    return scanner


def _configure_settings():
    return SimpleNamespace(
        production_safe_mode_required=True,
        active_indices=["NIFTY"],
        capital_total=500_000.0,
        capital_fut=250_000.0,
        capital_opt=100_000.0,
        risk_fut_pct=2.0,
        risk_opt_pct=8.0,
        fut_cost=200.0,
        opt_cost=80.0,
        futures_sl_pct=0.3,
        options_sl_pct=15.0,
    )


def test_armed_real_auto_revalidates_the_proposed_capital_and_risk_atomically(
    monkeypatch,
):
    import config.settings as settings_module
    import scanner as scanner_module

    live_settings = _configure_settings()
    scanner = _armed_real_scanner()
    seen = []

    monkeypatch.setattr(settings_module, "get_settings", lambda: live_settings)

    def validate(staged, provider, **kwargs):
        seen.append(staged)
        issues = staged.capital_fut + staged.capital_opt > staged.capital_total
        return {
            "ok": not issues,
            "blockers": ["risk configuration: segment capital exceeds total capital"]
            if issues
            else [],
            "warnings": [],
        }

    monkeypatch.setattr(scanner_module, "validate_real_mode_readiness", validate)

    configured = scanner.configure(
        capital_total=100_000,
        capital_fut=80_000,
        capital_opt=80_000,
        risk_fut_pct=3.0,
        risk_opt_pct=9.0,
        persist_settings=False,
    )

    assert configured is False
    assert len(seen) == 1
    assert seen[0] is not live_settings
    assert (
        seen[0].capital_total,
        seen[0].capital_fut,
        seen[0].capital_opt,
        seen[0].risk_fut_pct,
        seen[0].risk_opt_pct,
    ) == (100_000.0, 80_000.0, 80_000.0, 3.0, 9.0)
    assert scanner.capital_total == 500_000.0
    assert scanner.capital_fut == 250_000.0
    assert scanner.capital_opt == 100_000.0
    assert live_settings.capital_total == 500_000.0


def test_armed_real_auto_revalidates_a_changed_execution_universe(monkeypatch):
    import config.settings as settings_module
    import scanner as scanner_module

    live_settings = _configure_settings()
    scanner = _armed_real_scanner()
    seen = []
    monkeypatch.setattr(settings_module, "get_settings", lambda: live_settings)

    def validate(staged, provider, **kwargs):
        seen.append((list(staged.active_indices), kwargs))
        return {"ok": False, "blockers": ["SENSEX chain stale"], "warnings": []}

    monkeypatch.setattr(scanner_module, "validate_real_mode_readiness", validate)

    configured = scanner.configure(
        active_indices=["SENSEX"],
        persist_settings=False,
    )

    assert configured is False
    assert seen == [
        (["SENSEX"], {"require_option_chains": True, "active_indices": ["SENSEX"]})
    ]
    assert scanner.active_indices == ["NIFTY"]
    assert live_settings.active_indices == ["NIFTY"]
