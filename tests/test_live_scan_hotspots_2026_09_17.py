"""Two hot spots that slowed live scans after the 2026-09-17 13:40 restart.

py-spy on the live process, GIL holder only, 30 s:
  30.5%  DerivativeParticipation.features walking the whole day's archive (258 MB)
  17.3%  ScannerDashboardCache.update deep-copying a state the server had just rebuilt
Both fixes must leave outputs unchanged.
"""
import copy
import json
import random

from data.derivative_participation import DerivativeParticipation
from engine.scanner_dashboard import ScannerDashboardCache


BASE = 1789619100.0  # 2026-09-17 09:15 IST


def _full_scan_features(store, instrument, timeframe, as_of):
    """The pre-fix algorithm: every archived event of the day, filtered in Python."""
    seconds = {"1min": 60, "5min": 300, "15min": 900}[timeframe]
    max_gap = min(120.0, seconds / 2)
    cutoff = as_of - 2 * seconds - 2 * max_gap
    windowed = store._archived_observations
    try:
        store._archived_observations = lambda inst, at, cut: [
            event for event in store._archived(at)
            if event.get("kind") == "observation" and event["key"][0] == inst
            and cut <= event["at"] <= at
        ]
        return store.features(instrument, timeframe, as_of)
    finally:
        store._archived_observations = windowed


def _drive(store, rng, count, start):
    t = start
    cumulative = {}
    for _ in range(count):
        t += rng.uniform(0.2, 6.0)
        if rng.random() < 0.01:
            t += rng.uniform(60, 400)
        instrument = rng.choice(["NIFTY", "BANKNIFTY"])
        family = rng.choice(["options", "futures"])
        contract = f"{instrument}{family}{rng.randint(0, 3)}"
        cumulative[contract] = cumulative.get(contract, 0.0) + rng.uniform(0, 500)
        store.observe(instrument, family, "kite", contract, t, cumulative[contract],
                      oi=rng.uniform(1e5, 2e5), price=rng.uniform(50, 300),
                      expiry="22SEP2026", option_type="CE", strike=23000.0)
        if rng.random() < 0.02:
            store.flush()
    store.flush()
    return t


def _assert_same(store, rng, start, end, samples=120):
    for _ in range(samples):
        as_of = rng.uniform(start, end + 120)
        for instrument in ("NIFTY", "BANKNIFTY", "SENSEX"):
            for timeframe in ("1min", "5min", "15min"):
                got = store.features(instrument, timeframe, as_of)
                expected = _full_scan_features(store, instrument, timeframe, as_of)
                assert json.dumps(got, sort_keys=True, default=str) == json.dumps(
                    expected, sort_keys=True, default=str
                ), (instrument, timeframe, as_of)


def test_windowed_archive_read_matches_full_scan_in_process(tmp_path):
    rng = random.Random(3)
    store = DerivativeParticipation(archive_dir=tmp_path)
    middle = _drive(store, rng, 1500, BASE)
    store.features("NIFTY", "5min", middle)  # load the archive cache mid-session
    end = _drive(store, rng, 1500, middle)   # later flushes must extend the index

    _assert_same(store, rng, BASE, end)


def test_windowed_archive_read_matches_full_scan_after_restart(tmp_path):
    rng = random.Random(5)
    end = _drive(DerivativeParticipation(archive_dir=tmp_path), rng, 2500, BASE)

    restarted = DerivativeParticipation(archive_dir=tmp_path)

    _assert_same(restarted, rng, BASE, end)


def test_windowed_read_returns_only_the_window(tmp_path):
    store = DerivativeParticipation(archive_dir=tmp_path)
    for step in range(200):
        store.observe("NIFTY", "futures", "kite", "NIFTYFUT", BASE + step * 6, 100.0 + step)
    store.flush()
    restarted = DerivativeParticipation(archive_dir=tmp_path)

    window = restarted._archived_observations("NIFTY", BASE + 600, BASE + 300)

    assert window
    assert all(BASE + 300 <= event["at"] <= BASE + 600 for event in window)
    assert restarted._archived_observations("SENSEX", BASE + 600, BASE + 300) == []


def test_dashboard_cache_copies_scanner_owned_results_by_default():
    cache = ScannerDashboardCache()
    results = {"trades": {"open": [{"id": 1}]}, "diagnostics": {"latency": 5}}

    cache.update(results)
    results["trades"]["open"].append({"id": 2})

    assert cache.trades()["open"] == [{"id": 1}]


def test_dashboard_cache_takes_a_detached_snapshot_without_copying():
    cache = ScannerDashboardCache()
    snapshot = {"trades": {"open": [{"id": 1}]}, "diagnostics": {"latency": 5}}
    frozen = copy.deepcopy(snapshot)

    cache.update(snapshot, detached=True)

    assert cache.state() is snapshot
    assert cache.state() == frozen
    assert cache.trades() is snapshot["trades"]


# --- Live archive memory: keep a trailing hour, fall back to the full session ---------


def _full_scan_reference(archive_dir):
    """A store that always answers from the whole session (the pre-fix behaviour)."""
    store = DerivativeParticipation(archive_dir=archive_dir)
    store._clock = lambda: 0.0  # never "live", so every read loads the full session
    return store


def test_live_reads_use_a_trailing_hour_and_match_the_full_session(tmp_path):
    rng = random.Random(9)
    end = _drive(DerivativeParticipation(archive_dir=tmp_path), rng, 4000, BASE)

    live = DerivativeParticipation(archive_dir=tmp_path)
    live.TAIL_READ_CHUNK_BYTES = 32 * 1024
    live._clock = lambda: end
    reference = _full_scan_reference(tmp_path)
    for _ in range(40):
        as_of = rng.uniform(end - 600, end + 30)
        for instrument in ("NIFTY", "BANKNIFTY"):
            for timeframe in ("1min", "5min", "15min"):
                assert json.dumps(live.features(instrument, timeframe, as_of), sort_keys=True, default=str) == \
                    json.dumps(reference.features(instrument, timeframe, as_of), sort_keys=True, default=str)

    day = live._session(end)
    assert live._archive_floor[day] is not None, "a live read must not load the whole session"
    assert day not in live._archive_cache
    held = sum(len(bucket["ats"]) for bucket in live._archive_obs[day].values())
    full = sum(len(bucket["ats"]) for bucket in reference._archive_obs[day].values())
    assert held < full


def test_an_old_read_falls_back_to_the_full_session(tmp_path):
    rng = random.Random(10)
    end = _drive(DerivativeParticipation(archive_dir=tmp_path), rng, 4000, BASE)
    live = DerivativeParticipation(archive_dir=tmp_path)
    live._clock = lambda: end
    reference = _full_scan_reference(tmp_path)
    day = live._session(end)

    live.features("NIFTY", "5min", end)
    assert live._archive_floor[day] is not None

    old = BASE + 900
    assert json.dumps(live.features("NIFTY", "15min", old), sort_keys=True, default=str) == \
        json.dumps(reference.features("NIFTY", "15min", old), sort_keys=True, default=str)
    assert live._archive_floor[day] is None


def test_running_process_prunes_the_live_index(tmp_path):
    rng = random.Random(11)
    store = DerivativeParticipation(archive_dir=tmp_path)
    end = _drive(store, rng, 1500, BASE)
    store._clock = lambda: end
    store.features("NIFTY", "5min", end)
    day = store._session(end)
    first_floor = store._archive_floor[day]

    later = _drive(store, rng, 6000, end)
    store._clock = lambda: later

    assert store._archive_floor[day] > first_floor
    oldest = min(bucket["ats"][0] for bucket in store._archive_obs[day].values() if bucket["ats"])
    assert oldest >= store._archive_floor[day]


# --- Forming-row fast path in CandleBuilder ------------------------------------------


def _candle_frame(periods, end="2026-09-17 10:00"):
    import pandas as pd

    index = pd.date_range(end=end, periods=periods, freq="1min")
    return pd.DataFrame(
        {
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
            "volume": 1000.0, "volume_basis": "fyers_index", "source": "fyers_history",
            "tick_count": 5.0,
        },
        index=index,
    )


def _general_merge(existing, new_data):
    import pandas as pd
    from data.candle_builder import CandleBuilder

    combined = pd.concat([existing, new_data])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined = CandleBuilder._restore_zeroed_volume(combined, existing, new_data.index)
    return CandleBuilder._restore_candle_telemetry(combined, existing, new_data.index)


def test_forming_row_fast_path_matches_the_general_merge():
    import pandas as pd
    from data.candle_builder import CandleBuilder

    existing = _candle_frame(50)
    last = existing.index[-1]
    cases = [
        # same forming bar, price-only tick with zero volume and blank telemetry
        pd.DataFrame({"close": 101.0, "open": 100.0, "high": 101.5, "low": 99.5, "volume": 0.0,
                      "volume_basis": "", "source": "live_tick", "tick_count": 0.0}, index=[last]),
        # same bar with measured volume
        pd.DataFrame({"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.8, "volume": 2500.0,
                      "volume_basis": "fyers_index", "source": "live_tick", "tick_count": 9.0}, index=[last]),
        # next bar
        pd.DataFrame({"open": 101.8, "high": 102.0, "low": 101.0, "close": 101.2, "volume": 0.0,
                      "volume_basis": "", "source": "live_tick", "tick_count": 1.0},
                     index=[last + pd.Timedelta(minutes=1)]),
    ]
    for new_row in cases:
        fast = CandleBuilder._merge_forming_row(existing, new_row)
        assert fast is not None
        pd.testing.assert_frame_equal(fast, _general_merge(existing, new_row))


def test_forming_row_fast_path_declines_what_it_cannot_prove():
    import pandas as pd
    from data.candle_builder import CandleBuilder

    existing = _candle_frame(50)
    last = existing.index[-1]
    older = existing.iloc[[-3]].copy()
    two_rows = existing.iloc[-2:].copy()
    extra_column = existing.iloc[[-1]].assign(receipt_at=pd.Timestamp("2026-09-17 10:00:05"))

    assert CandleBuilder._merge_forming_row(existing, older) is None
    assert CandleBuilder._merge_forming_row(existing, two_rows) is None
    assert CandleBuilder._merge_forming_row(existing, extra_column) is None
    assert CandleBuilder._merge_forming_row(existing.iloc[0:0], existing.iloc[[-1]]) is None
