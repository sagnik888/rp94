"""S1 signals refused by gates built for the other engines' evidence (live, 2026-09-24).

- "fresh comparable 1-minute cash volume is unavailable" refused seven S1 signals, five of
  them the 13:50 SELL wave on every index. The exceptional-data demand exists for entries
  that claim an exception from volume/shock evidence; an S1 5m/15m entry claims none (the
  volume-spike label only names it), so it no longer applies. A 1m S1 entry exists only
  because of its burst and still needs the data.
- An aged option-chain snapshot (95s against a 90s limit) refused the 15:01 NIFTY SELL. S1
  reads no chain intelligence; with its own premium quote verified the chain age is not
  evidence about it.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner
from tests.test_s1_lane_2026_09_23 import _candidate, _gate_scanner


def _quality_scanner():
    sc = Scanner.__new__(Scanner)
    sc.mode = "REAL"
    sc._prediction_volume_context = lambda instrument, frame: {}      # no fresh 1m cash volume
    sc.candles = SimpleNamespace(get_candles=lambda *a, **k: None)
    sc._live_entry_data_blocks = {}
    return sc


@pytest.mark.parametrize("engine,tf,allowed", [
    ("s1", "5min", True), ("s1", "15min", True), ("s1", "1min", False), ("utbot", "5min", False),
])
def test_the_exceptional_data_demand_applies_to_s1_only_on_1m_bursts(engine, tf, allowed):
    cand = SimpleNamespace(instrument="NIFTY", timeframe=tf, grade="A+", inst_type="OPT", volume_spike_entry=True,
                           market_ltp_shock_entry=False, source_engine=engine,
                           source_lane="S1" if engine == "s1" else "")
    verdict = Scanner._live_entry_data_quality(_quality_scanner(), cand)
    assert verdict["allowed"] is allowed
    if not allowed:
        assert verdict["reason_code"] == "ONE_MINUTE_VOLUME_UNAVAILABLE"


def test_a_stale_scan_still_blocks_s1():
    sc = _quality_scanner()
    sc._live_entry_data_blocks = {"NIFTY": "live index analysis timed out after 9.0s"}
    cand = SimpleNamespace(instrument="NIFTY", timeframe="5min", grade="C", inst_type="OPT", source_engine="s1")
    assert Scanner._live_entry_data_quality(sc, cand)["reason_code"] == "STALE_SCAN"


@pytest.mark.parametrize("engine,quote_verified,passes", [
    ("s1", True, True), ("s1", False, False), ("utbot", True, False),
])
def test_an_aged_chain_does_not_block_an_s1_entry_with_a_verified_quote(monkeypatch, engine, quote_verified, passes):
    _, sc = _gate_scanner(monkeypatch, [])
    monkeypatch.setattr(Scanner, "_option_chain_entry_freshness_blocker",
                        lambda self, c, r: "option chain age 95.4s exceeds high-volatility entry limit 90.0s")
    cand = _candidate(source_engine=engine, source_lane="S1" if engine == "s1" else "",
                      quote_verified=quote_verified)
    assert sc._passes_live_trade_ready_gate(cand) is passes
