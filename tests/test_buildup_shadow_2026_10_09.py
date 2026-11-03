"""Early-entry build-up detector (2026-10-09): shadow only, judged later on outcomes."""
import json
import threading
import time
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from engine.buildup_shadow import BuildupShadow, chain_pcr

T0 = 1_791_516_600.0  # 2026-10-09 09:00 IST
SETTINGS = NS(buildup_shadow_enabled=True, buildup_window_seconds=180.0, buildup_impulse_min_pct=0.08,
              buildup_impulse_baseline_mult=2.0, buildup_baseline_minutes=60.0, buildup_min_baseline_windows=5,
              buildup_volume_ratio=1.5, buildup_oi_window_seconds=300.0, buildup_oi_min_pct=0.05,
              buildup_pcr_window_seconds=300.0, buildup_pcr_max_age_seconds=240.0, buildup_pcr_min_change=0.02,
              buildup_min_legs=2, buildup_cooldown_seconds=600.0)


def _shadow(tmp_path, **overrides):
    settings = NS(**{**vars(SETTINGS), **overrides})
    return BuildupShadow(settings_getter=lambda: settings, log_path=tmp_path / "buildup.jsonl")


def _quiet_hour(shadow, index="NIFTY", price=22000.0, volume=1_000_000.0, oi=10_000_000.0):
    """60 minutes of 15s quotes: price wobbling 0.01%, 3,000 contracts a minute, flat OI."""
    t = T0
    for i in range(240):
        wobble = 2.2 if i % 2 else -2.2
        shadow.observe_futures(index, t, price + wobble, volume, oi)
        volume += 750.0
        t += 15.0
    return t, price, volume, oi


def _push(shadow, t, price, volume, oi, *, steps=12, price_step=2.5, volume_step=3000.0, oi_step=1000.0,
          index="NIFTY"):
    event = None
    for _ in range(steps):
        price += price_step
        volume += volume_step
        oi += oi_step
        event = shadow.observe_futures(index, t, price, volume, oi) or event
        t += 15.0
    return event, t, price, volume, oi


def test_impulse_with_volume_and_oi_building_is_recorded(tmp_path):
    shadow = _shadow(tmp_path)
    t, price, volume, oi = _quiet_hour(shadow)
    event, *_ = _push(shadow, t, price, volume, oi)   # +0.14% in 3 min, 4x volume, OI +0.12%
    assert event and event["direction"] == "LONG" and event["impulse"]
    assert event["volume_leg"] and event["oi_leg"] and event["oi_type"] == "long_buildup"
    line = json.loads((tmp_path / "buildup.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["index"] == "NIFTY" and line["legs_true"] >= 2


def test_price_alone_or_one_leg_is_not_enough(tmp_path):
    shadow = _shadow(tmp_path)
    t, price, volume, oi = _quiet_hour(shadow)
    event, *_ = _push(shadow, t, price, volume, oi, volume_step=750.0, oi_step=0.0)
    assert event is None
    shadow = _shadow(tmp_path)
    t, price, volume, oi = _quiet_hour(shadow)
    event, *_ = _push(shadow, t, price, volume, oi, oi_step=0.0)  # volume only
    assert event is None


def test_pcr_moving_with_the_trade_counts_as_a_leg(tmp_path):
    shadow = _shadow(tmp_path)
    t, price, volume, oi = _quiet_hour(shadow)
    shadow.observe_chain("NIFTY", pd.DataFrame({"call_oi": [100.0], "put_oi": [90.0]}), at=t - 300)
    shadow.observe_chain("NIFTY", pd.DataFrame({"call_oi": [100.0], "put_oi": [95.0]}), at=t + 100)
    event, *_ = _push(shadow, t, price, volume, oi, oi_step=0.0)   # volume + PCR
    assert event and event["pcr_leg"] and event["volume_leg"] and not event["oi_leg"]


def test_short_build_up_and_cooldown(tmp_path):
    shadow = _shadow(tmp_path)
    t, price, volume, oi = _quiet_hour(shadow)
    event, t, price, volume, oi = _push(shadow, t, price, volume, oi, price_step=-2.5)
    assert event and event["direction"] == "SHORT" and event["oi_type"] == "short_buildup"
    again, *_ = _push(shadow, t, price, volume, oi, price_step=-2.5)
    assert again is None   # same index and direction inside the 10-minute cooldown


def test_switched_off_records_nothing(tmp_path):
    shadow = _shadow(tmp_path, buildup_shadow_enabled=False)
    t, price, volume, oi = _quiet_hour(shadow)
    event, *_ = _push(shadow, t, price, volume, oi)
    assert event is None and not (tmp_path / "buildup.jsonl").exists()


def test_chain_pcr_reads_wide_and_long_chains():
    assert chain_pcr(pd.DataFrame({"call_oi": [100, 300], "put_oi": [200, 200]}))[0] == pytest.approx(1.0)
    long = pd.DataFrame({"type": ["CE", "PE", "PE"], "oi": [100, 50, 100]})
    assert chain_pcr(long)[0] == pytest.approx(1.5)
    assert chain_pcr(pd.DataFrame({"x": [1]}))[0] == 0.0


def test_market_data_hands_each_futures_quote_to_the_observer():
    from data.market_data import MarketDataProvider

    md = MarketDataProvider.__new__(MarketDataProvider)
    now = time.time()
    md.get_index_activity = lambda name: {"source": "none", "volume": 0.0, "futures": {
        "available": True, "volume": 5000.0, "oi": 123.0, "last_price": 22010.0,
        "tradingsymbol": "NIFTY26OCTFUT", "exchange_timestamp": now}}
    md._participation_store = lambda: NS(observe=lambda *a, **k: None, flush=lambda: None)
    md._record_index_futures_volume_snapshot = lambda *a, **k: None
    md._index_volume_refresh_guard = threading.Lock()
    md._index_volume_refresh_inflight = {"NIFTY"}
    seen = []
    md.futures_observer = lambda *args: seen.append(args)
    md._refresh_index_volume_worker("NIFTY")
    assert seen and seen[0][0] == "NIFTY" and seen[0][2:] == (22010.0, 5000.0, 123.0)
