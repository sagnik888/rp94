"""Measure local scan computation without broker access or order submission.

This covers twelve native scanner lanes, not the complete live application.
The market-data masters and OHLC files are read only.
"""
from datetime import date, datetime
import json
from pathlib import Path
from statistics import mean, median
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

import config.settings as settings_module
from config.settings import Settings
from data.kite_market_data import KiteDataSource
import engine.multi_timeframe as mtf_module
from engine.multi_timeframe import MultiTimeframeEngine


def contract_lookup():
    expiry = date(2026, 11, 10)
    source = KiteDataSource()
    rows = [dict(name=f"OTHER{i}", instrument_type="CE", expiry=expiry, strike=100.)
            for i in range(40000)]
    rows.extend(dict(name="NIFTY", instrument_type=side, expiry=expiry, strike=float(strike))
                for strike in range(20500, 23550, 50) for side in ("CE", "PE"))
    source._instruments = {"NFO": rows}
    source._option_series = source._index_option_series(source._instruments)

    def original(strike, side):
        with source._lock:
            contracts = list(source._instruments["NFO"])
        target = round(float(strike), 2)
        for row in contracts:
            if (row["name"] == "NIFTY" and row["instrument_type"] == side
                    and row["expiry"] == expiry and abs(row["strike"]-target) < .01):
                return row
        return None

    calls = [(strike, side) for strike in range(20500, 23550, 50) for side in ("CE", "PE")]
    samples = {"before": [], "after": []}
    for iteration in range(8):
        for name in (("before", "after") if iteration % 2 == 0 else ("after", "before")):
            started = perf_counter()
            if name == "before":
                output = [original(strike, side) for strike, side in calls]
            else:
                output = [source.resolve_option("NIFTY", strike, side, expiry=expiry)
                          for strike, side in calls]
            elapsed = (perf_counter()-started)*1000
            samples[name].append(elapsed)
            assert all(row is rows[40000+i] for i, row in enumerate(output))
    return dict(master_rows=len(rows), contracts=len(calls), identical=True,
                before_median_ms=median(samples["before"]),
                after_median_ms=median(samples["after"]))


def twelve_lanes():
    settings = Settings(scanner_engine="ut_ema", trading_mode="REAL",
                        ut_signal_mode="confirmed", live_intrabar_signal_entries=False)
    previous_settings, previous_datetime = settings_module.get_settings, mtf_module.datetime
    frames = {}
    for index in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"):
        for tf in ("1min", "5min", "15min"):
            frame = pd.read_csv(ROOT / f"data_store/candles/{index}_{tf}.csv", index_col=0)
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index))
            if frame.index.tz is not None:
                frame.index = frame.index.tz_convert("Asia/Kolkata").tz_localize(None)
            frames[index, tf] = frame
    clock = max(frame.index[-1] for frame in frames.values()) + pd.Timedelta(seconds=10)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            at = clock.tz_localize("Asia/Kolkata").to_pydatetime()
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)

    try:
        settings_module.get_settings = lambda: settings
        mtf_module.datetime = Clock
        engine = MultiTimeframeEngine()
        for (index, tf), frame in frames.items():
            engine.update_data(index, tf, frame)
        indices = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
        for index in indices:
            engine.process_instrument(index)
        timings, cache_counts = [], []
        for iteration in range(25):
            # Each iteration changes forming prices while closed OHLC stays fixed.
            for (index, tf), frame in frames.items():
                length = int(tf.replace("min", ""))
                if frame.index[-1]+pd.Timedelta(minutes=length) > clock:
                    frame.iloc[-1, frame.columns.get_loc("close")] = (
                        float(frame.iloc[-1]["open"]) if iteration % 2 else
                        (float(frame.iloc[-1]["high"])+float(frame.iloc[-1]["low"]))/2)
                engine.update_data(index, tf, frame)
            started = perf_counter()
            results = [engine.process_instrument(index) for index in indices]
            timings.append((perf_counter()-started)*1000)
            cache_counts.append(sum(meta.get("used_cache", False) for result in results
                                    for meta in result.timeframe_metadata.values()))
        assert cache_counts == [12]*len(timings), cache_counts
        return dict(lanes=12, iterations=len(timings), cached_lanes_each_pass=12,
                    mean_ms=mean(timings), median_ms=median(timings), maximum_ms=max(timings),
                    rows={f"{index}_{tf}": len(frame) for (index, tf), frame in frames.items()})
    finally:
        settings_module.get_settings, mtf_module.datetime = previous_settings, previous_datetime


if __name__ == "__main__":
    report = dict(contract_lookup=contract_lookup(), twelve_lane_computation=twelve_lanes(),
                  basis="Local computation only; excludes broker I/O, ledger, dashboard, and live thread contention.")
    target = ROOT / "reports/latency_restarted_20261102/scan_benchmark_20261103.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
