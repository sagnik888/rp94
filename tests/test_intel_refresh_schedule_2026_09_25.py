"""Background intelligence: event-driven refresh, bar-aware freshness, cheaper chain math.

Live profile, 2026-09-25 12:30 (py-spy, 60s):
- The worker walked the indices one by one with 5s between them and then slept 25s, so an
  index came round every ~45-50s against a 30s analysis TTL. For a third of each cycle the
  scan thread found the 5m/15m bundle expired and recomputed it inline: 119 of 231 analyze()
  samples ran on the scan path.
- A bundle was judged by age only, so a signal on a just-closed bar could be graded with
  intelligence computed up to 30s earlier on the still-forming bar.
- analyze() cost 117ms. Half of it was the max-pain loop (two pandas clip() per strike on a
  61-strike chain); OI and options-flow copied and re-sorted the whole chain, symbol and
  token string columns included.
"""
import asyncio
import types

import numpy as np
import pandas as pd
import pytest

import scanner as scanner_module
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.oi_tracker import OITracker
from intelligence.order_flow import OrderFlowAnalyzer
from engine.entry_analysis_service import EntryAnalysisService
from scanner import Scanner

TFS = ("1min", "5min", "15min")


class _Candles:
    def __init__(self):
        self.bars = {tf: pd.Timestamp("2026-09-25 10:05:00") for tf in TFS}

    def get_latest_price(self, _name):
        return 23000.0

    def latest_bar_start(self, _name, tf):
        return self.bars[tf]

    def get_candles(self, _name, tf):
        index = pd.date_range(end=self.bars[tf], periods=40, freq={"1min": "1min", "5min": "5min", "15min": "15min"}[tf])
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=index)


def _shell(mode="REAL"):
    sc = Scanner.__new__(Scanner)
    sc.mode = mode
    sc.candles = _Candles()
    sc._intel_cache = {}
    sc._intel_cache_time = {}
    sc._last_intel_fetch = {}
    sc._background_intel_schedule = {}
    return sc


@pytest.fixture
def settings(monkeypatch):
    values = types.SimpleNamespace(intelligence_analysis_ttl_seconds=30.0,
                                   intelligence_background_refresh_seconds=20.0)
    monkeypatch.setattr(scanner_module, "get_settings", lambda: values)
    return values


# ---------------------------------------------------------------- schedule


def test_refresh_stays_under_the_ttl(settings):
    sc = _shell()
    assert sc._intel_background_refresh_seconds() == 20.0
    settings.intelligence_background_refresh_seconds = 60.0       # above the TTL
    assert sc._intel_background_refresh_seconds() == 24.0          # 80% of 30s
    settings.intelligence_analysis_ttl_seconds = 10.0
    assert sc._intel_background_refresh_seconds() == 8.0


def test_an_index_is_due_first_then_on_age_or_a_bar_close(settings):
    sc = _shell()
    assert sc._background_intel_due("NIFTY", 1000.0) == "first"
    sc._background_intel_schedule["NIFTY"] = {"at": 1000.0, "bars": {tf: sc._intel_bar_start("NIFTY", tf) for tf in TFS}}
    assert sc._background_intel_due("NIFTY", 1005.0) == ""
    assert sc._background_intel_due("NIFTY", 1020.0) == "age"
    sc.candles.bars["1min"] = pd.Timestamp("2026-09-25 10:06:00")   # a 1m bar closed
    assert sc._background_intel_due("NIFTY", 1006.0) == "1min_bar_closed"


def test_replay_keeps_the_old_cadence_and_ignores_bar_closes(settings):
    sc = _shell(mode="HISTORICAL")
    sc._background_intel_schedule["NIFTY"] = {"at": 1000.0, "bars": {tf: sc._intel_bar_start("NIFTY", tf) for tf in TFS}}
    sc.candles.bars["1min"] = pd.Timestamp("2026-09-25 10:06:00")
    assert sc._background_intel_due("NIFTY", 1030.0) == ""
    assert sc._background_intel_due("NIFTY", 1045.0) == "age"


# ---------------------------------------------------------------- bar-aware freshness


def test_a_bundle_from_before_the_last_bar_close_is_stale_in_real_mode():
    sc = _shell()
    sc._intel_bar_starts = {"NIFTY_5min": "2026-09-25 10:00:00"}
    assert sc._intel_bundle_predates_bar("NIFTY", "5min", "2026-09-25 10:05:00") is True
    assert sc._intel_bundle_predates_bar("NIFTY", "5min", "2026-09-25 10:00:00") is False
    # A scan holding a frame read just before the close keeps the newer bundle.
    assert sc._intel_bundle_predates_bar("NIFTY", "5min", "2026-09-25 09:55:00") is False
    assert sc._intel_bundle_predates_bar("NIFTY", "15min", "2026-09-25 10:05:00") is False  # never stamped
    sc.mode = "HISTORICAL"
    assert sc._intel_bundle_predates_bar("NIFTY", "5min", "2026-09-25 10:05:00") is False


def test_the_timeframe_reader_recomputes_after_a_bar_close(settings):
    sc = _shell()
    bundle = {"regime": {"regime": "TRENDING_UP", "candle_count": 50}}
    sc._cache_timeframe_intel("NIFTY", "15min", bundle, at=1000.0, bar_start="2026-09-25 10:00:00")
    sc.candles.bars["15min"] = pd.Timestamp("2026-09-25 10:00:00")
    assert sc._get_timeframe_intel("NIFTY", "15min", now=1010.0, allow_fallback=False) is bundle
    sc.candles.bars["15min"] = pd.Timestamp("2026-09-25 10:15:00")     # the 15m bar closed
    sc.candles.get_candles = lambda *_a: None                           # recompute path: no frame
    sc._live_analysis_window = lambda df, _tf: df
    assert sc._get_timeframe_intel("NIFTY", "15min", now=1010.0, allow_fallback=False) is None


def test_the_scan_path_checks_the_bar_as_well_as_the_age():
    import inspect

    src = inspect.getsource(EntryAnalysisService.process)
    fresh = src[src.index("intel_is_fresh = ("):src.index("intel_computed_this_pass = False")]
    assert '_intel_bundle_predates_bar(name, "5min"' in fresh
    assert "bar_start=scanner._intel_bar_start(name, \"5min\", candle_5min)" in src


# ---------------------------------------------------------------- the worker, on a fake clock


def test_the_worker_refreshes_all_three_frames_on_schedule(monkeypatch, settings):
    sc = _shell()
    sc.is_running = True
    sc._initial_setup_completed = True
    sc.config = {"indices": {"NIFTY": {"strike_interval": 50}}}
    sc.active_indices = ["NIFTY"]
    chain = pd.DataFrame({"strike": [23000.0]})
    sc._get_option_chain_cached = lambda _n, _c, **kw: (chain, {})
    sc._get_depth_context = lambda _n, _c: {}
    sc._live_analysis_window = lambda df, _tf: df
    sc._regime_history_ready = lambda _df: True
    sc._get_live_volatility_context = lambda: {}
    sc._previous_close_for = lambda _n: 22900.0
    sc._price_change_pct_for = lambda *_a, **_k: (0.0, 0.4)
    sc._days_to_expiry_for = lambda *_a: 3.0
    sc._build_intelligence_contexts = lambda *_a, **_k: {}
    sc._instrument_iv_observations = lambda _n: []
    sc._record_instrument_iv = lambda *_a: None
    sc._record_bar_aligned_intelligence = lambda *_a: None
    calls = []
    sc.intel = types.SimpleNamespace(analyze=lambda **kw: calls.append(kw["timeframe"]) or
                                     {"regime": {"regime": "TRENDING_UP", "candle_count": 50}})

    clock = [1000.0]
    monkeypatch.setattr(scanner_module.time, "time", lambda: clock[0])
    # What happens between polls: (seconds to advance, event).
    script = iter([(1.0, None), (1.0, "close_1m"), (22.0, None), (1.0, "stop")])
    real_sleep = asyncio.sleep
    seen = []

    async def fake_sleep(seconds, *a, **k):
        if seconds < 1.0:
            return await real_sleep(0)
        await asyncio.gather(*sc._background_intel_analysis_tasks.values())
        seen.append(len(calls))
        step, event = next(script)
        clock[0] += step
        if event == "close_1m":
            sc.candles.bars["1min"] = pd.Timestamp("2026-09-25 10:06:00")
        if event == "stop":
            sc.is_running = False
        await real_sleep(0)

    monkeypatch.setattr(scanner_module.asyncio, "sleep", fake_sleep)
    asyncio.run(sc._background_intel_worker())

    # poll 1: first -> 3 frames; poll 2: nothing due; poll 3: a 1m bar closed -> 3;
    # poll 4: 23s later, age -> 3.
    assert seen == [3, 3, 6, 9]
    assert calls == list(TFS) * 3
    assert sc._intel_bar_starts["NIFTY_1min"] == "2026-09-25 10:06:00"
    assert sc._background_intel_inflight is False


# ---------------------------------------------------------------- cheaper chain math


def _max_pain_reference(chain):
    oc = chain.dropna(subset=["strike"])
    strikes = oc["strike"].astype(float)
    coi = oc["call_oi"].astype(float).fillna(0.0)
    poi = oc["put_oi"].astype(float).fillna(0.0)
    best_k = best_pain = None
    for p in strikes:
        pain = (coi * (p - strikes).clip(lower=0)).sum() + (poi * (strikes - p).clip(lower=0)).sum()
        if best_pain is None or pain < best_pain:
            best_pain, best_k = pain, float(p)
    return best_k


def _chain(rng, n, spot=23000.0):
    strikes = np.sort(rng.choice(np.arange(spot - 1500, spot + 1500, 50.0), n, replace=False))
    return pd.DataFrame({
        "strike": strikes,
        "call_oi": rng.integers(0, 5_000_000, n).astype(float),
        "put_oi": rng.integers(0, 5_000_000, n).astype(float),
        "call_oi_change": rng.integers(-200_000, 200_000, n).astype(float),
        "put_oi_change": rng.integers(-200_000, 200_000, n).astype(float),
        "call_volume": rng.integers(0, 9_000_000, n).astype(float),
        "put_volume": rng.integers(0, 9_000_000, n).astype(float),
        "call_ltp": np.linspace(900, 1, n), "put_ltp": np.linspace(1, 900, n),
        "call_symbol": [f"NIFTY26SEP{int(s)}CE" for s in strikes],
        "put_symbol": [f"NIFTY26SEP{int(s)}PE" for s in strikes],
    })


@pytest.mark.parametrize("seed", range(12))
def test_max_pain_matches_the_strike_by_strike_reference(seed):
    rng = np.random.default_rng(seed)
    chain = _chain(rng, int(rng.integers(3, 61)))
    if seed % 3 == 0:
        chain[["call_oi", "put_oi"]] = 0.0                     # every strike ties
    if seed % 4 == 1:
        chain.loc[rng.random(len(chain)) < 0.3, "put_oi"] = np.nan
    res = IntelligenceAggregator().analyze(
        "NIFTY", "5min", pd.DataFrame(), options_chain=chain, spot_price=23000.0, strike_interval=50,
        days_to_expiry=3, chain_quality={"source": "kite", "strike_count": len(chain), "score": 95,
                                         "fallback": False, "expiry_aligned": True, "stale": False,
                                         "intelligence_usable": True},
    )
    assert res["max_pain"]["strike"] == _max_pain_reference(chain)


@pytest.mark.parametrize("seed", range(8))
def test_oi_and_options_flow_ignore_columns_they_never_read(seed):
    rng = np.random.default_rng(100 + seed)
    chain = _chain(rng, 41)
    chain["oi_change_source"] = "fyers_reported"
    chain["oi_change_horizon_seconds"] = 300.0
    chain.attrs.update({"expiry": "29SEP2026", "instrument": "NIFTY"})
    lean = chain[["strike", "call_oi", "put_oi", "call_oi_change", "put_oi_change", "call_volume", "put_volume",
                  "oi_change_source", "oi_change_horizon_seconds"]]
    spot = 23000.0 + float(rng.normal(0, 200))
    for tf in (None, 300):
        full = OITracker().analyze(chain, 0.3, spot, 50, timeframe_seconds=tf)
        assert full == OITracker().analyze(lean, 0.3, spot, 50, timeframe_seconds=tf)
        # The columns the analysis does read still reach it.
        assert full["oi_change_source"] == "fyers_reported"
        assert full["oi_change_horizon_seconds"] == 300.0 and full["change_comparable"] is True
    flow = OrderFlowAnalyzer()._options_flow(chain, spot, 50)
    assert flow == OrderFlowAnalyzer()._options_flow(lean, spot, 50)
    assert len(flow["top_ce"]) == 4 and flow["ce_pe_activity_ratio"] > 0
