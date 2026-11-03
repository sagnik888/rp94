"""A chain that aged past the entry limit between background refreshes is fetched once on demand (2026-10-09)."""
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from config.settings import get_settings
from scanner import Scanner

NOW = 1_000_000.0


@pytest.fixture
def clock(monkeypatch):
    import scanner as module
    value = [NOW]
    monkeypatch.setattr(module.time, "time", lambda: value[0])
    return value


def _scanner(fetch_ok=True, mode="REAL"):
    sc = Scanner.__new__(Scanner)
    sc.mode = mode
    sc.config = {"indices": {"MIDCPNIFTY": {"option_exchange": "NFO"}}}
    sc.fetches = []

    def fetch(name, cfg, force=False, allow_fetch=True):
        sc.fetches.append((name, force))
        if not fetch_ok:
            return pd.DataFrame({"strike": [1]}), {"stale": True}
        quality = {"age_seconds": 0.0, "fetched_at_epoch": NOW, "stale": False}
        sc._chain_quality_cache = {name: quality}
        return pd.DataFrame({"strike": [1]}), quality

    sc._get_option_chain_cached = fetch
    return sc


def _candidate():
    # High IV percentile selects the 90s limit; 96s is the 2026-10-09 MIDCPNIFTY refusal.
    return NS(instrument="MIDCPNIFTY", inst_type="OPT", iv_percentile=90.0,
              chain_quality={"age_seconds": 96.0, "captured_at_epoch": NOW, "stale": False})


def test_stale_chain_is_refreshed_and_then_admitted(clock):
    sc, candidate = _scanner(), _candidate()
    assert "exceeds" in sc._option_chain_entry_freshness_blocker(candidate, "VOLATILE")
    assert sc._refresh_stale_entry_chain(candidate)
    assert sc._option_chain_entry_freshness_blocker(candidate, "VOLATILE") == ""
    assert sc.fetches == [("MIDCPNIFTY", True)]


def test_failed_refresh_keeps_the_refusal(clock):
    sc, candidate = _scanner(fetch_ok=False), _candidate()
    assert not sc._refresh_stale_entry_chain(candidate)
    assert "exceeds" in sc._option_chain_entry_freshness_blocker(candidate, "VOLATILE")


def test_refresh_is_rate_limited_per_index(clock):
    sc = _scanner()
    assert sc._refresh_stale_entry_chain(_candidate())
    clock[0] += 5
    assert not sc._refresh_stale_entry_chain(_candidate())
    clock[0] += 15
    assert sc._refresh_stale_entry_chain(_candidate())
    assert len(sc.fetches) == 2


@pytest.mark.parametrize("variant", ["historical", "disabled"])
def test_refresh_stays_off_in_replay_or_when_disabled(clock, monkeypatch, variant):
    monkeypatch.setattr(get_settings(), "option_chain_entry_refresh_on_stale", variant != "disabled")
    sc = _scanner(mode="HISTORICAL" if variant == "historical" else "REAL")
    assert not sc._refresh_stale_entry_chain(_candidate())
    assert sc.fetches == []
