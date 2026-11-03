"""The global entry timing switch applies to UT/EMA native observations."""
from types import SimpleNamespace as NS
from dataclasses import replace

import pandas as pd
import pytest

from engine.ut_ema_engine import Params, UTEmaEngine, signal_fire_mode
from engine.ut_ema_stream import UTEmaTickService
from tests.test_ut_ema_stream_2026_10_29 import real_host


@pytest.mark.parametrize("timeframe", ["5min", "15min"])
def test_off_waits_for_close_and_uses_final_crossover(timeframe):
    period = pd.Timedelta(timeframe)
    start = pd.Timestamp("2026-10-29 09:15")
    values = [1000. - i * .05 for i in range(20)]
    frame = pd.DataFrame({"open": values, "high": [v+.1 for v in values],
                          "low": [v-.1 for v in values], "close": values},
                         index=pd.date_range(start, periods=20, freq=timeframe))
    at = frame.index[-1] + period
    core = UTEmaEngine(replace(Params(), execution_mode="Signal Bar Close"))
    core.seed_stream(frame, "NIFTY", timeframe, as_of=at)
    before = len(core._timeline.marks)
    for seconds, price in enumerate([1002., 998., 1002.]):
        assert core.observe_tick(price, at + pd.Timedelta(seconds=seconds)) is None
    assert len(core._timeline.marks) == before
    result = core.observe_tick(1002., at + period)
    fresh = result["utema_events"][before:]
    assert any(e["entry"] for e in fresh)
    assert all(e.get("confirmed") and not e["realtime"] for e in fresh)
    assert all(pd.Timestamp(e["observed_at"]) >= at + period for e in fresh)
    # Its native decisions match the Pine Signal Bar Close replay of those OHLC.
    expected = UTEmaEngine(core.params).process(core._stream_frame(), "NIFTY", timeframe,
                                               realtime=False, as_of=at+period)
    def normalize(events):
        return [(e["engine"], e["side"], e["entry"], e["price"], e["reason"]) for e in events]
    assert normalize(fresh) == normalize(expected["utema_events"][before:])


def test_stream_off_only_authorizes_newly_observed_close(monkeypatch, tmp_path):
    scanner, adapter, frame = real_host(monkeypatch)
    import config.settings as config
    settings = NS(trading_mode="REAL", scanner_engine="ut_ema",
                  live_intrabar_signal_entries=False, ut_signal_mode="confirmed")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    at = pd.Timestamp("2026-10-29 12:35")
    service.process_observation("NIFTY", 1002., at)
    service.process_observation("NIFTY", 998., at+pd.Timedelta(seconds=1))
    service.process_observation("NIFTY", 1002., at+pd.Timedelta(seconds=2))
    assert not adapter.stream_snapshot()["utema_executable_event_ids"]
    service.process_observation("NIFTY", 1002., at+pd.Timedelta(minutes=5))
    result = adapter.stream_snapshot()
    permitted = result["utema_executable_event_ids"]
    assert permitted
    for sig in result["signals"]:
        if sig.utema_event_id in permitted:
            assert sig.utema_event.get("confirmed")
            assert not sig.utema_event["realtime"]


@pytest.mark.parametrize("enabled,legacy,want", [(False,"realtime","CANDLE_CLOSE"),
    (True,"confirmed","CANDLE_CLOSE"),(True,"realtime","INTRACANDLE"),
    ("False","realtime","CANDLE_CLOSE")])
def test_legacy_switches(enabled, legacy, want):
    assert signal_fire_mode(NS(live_intrabar_signal_entries=enabled, ut_signal_mode=legacy)) == want


def test_off_blocks_previously_queued_intrabar_order(monkeypatch):
    import config.settings as config
    from tests.test_ut_ema_queue_age_2026_10_29 import queued
    from engine import ut_ema_lane as lane
    scanner, trade, _ = queued(monkeypatch)
    monkeypatch.setattr(config, "get_settings", lambda: NS(live_intrabar_signal_entries=False))
    assert lane.submission_authority(scanner, trade=trade) == (False, "Intracandle entries are disabled")


def test_off_refuses_intrabar_candidate_before_option_resolution(monkeypatch):
    from engine import ut_ema_lane as lane
    from tests.test_ut_ema_lane_2026_10_29 import host, native_result, settings
    scanner, processor, _ = host(monkeypatch)
    found = lane.build_candidates(processor, "NIFTY", NS(results_5min=native_result(), results_15min={}),
                                  {"strike_interval": 50}, 2, 75,
                                  settings(live_intrabar_signal_entries=False))
    assert found == []
    scanner.data.get_option_token.assert_not_called()


def test_switching_off_revokes_existing_tick_authorization(monkeypatch, tmp_path):
    import config.settings as config
    scanner, adapter, _ = real_host(monkeypatch)
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    at = pd.Timestamp("2026-10-29 12:35")
    service.process_observation("NIFTY", 998.05, at)
    service.process_observation("NIFTY", 1000., at+pd.Timedelta(seconds=1))
    service.process_observation("NIFTY", 997.95, at+pd.Timedelta(seconds=2))
    assert adapter.stream_snapshot()["utema_executable_event_ids"]
    timeline = adapter._native._timeline
    monkeypatch.setattr(config, "get_settings", lambda: NS(live_intrabar_signal_entries=False))
    service.process_observation("NIFTY", 998., at+pd.Timedelta(seconds=3))
    assert adapter._native._timeline is timeline
    assert adapter._native.params.execution_mode == "Signal Bar Close"
    assert adapter.stream_snapshot()["utema_executable_event_ids"] == []


def test_transient_cross_disappears_before_close(monkeypatch, tmp_path):
    import config.settings as config
    scanner, adapter, _ = real_host(monkeypatch)
    monkeypatch.setattr(config, "get_settings", lambda: NS(live_intrabar_signal_entries=False))
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    at = pd.Timestamp("2026-10-29 12:35")
    service.process_observation("NIFTY", 1002., at)
    service.process_observation("NIFTY", 998., at+pd.Timedelta(seconds=1))
    service.process_observation("NIFTY", 1002., at+pd.Timedelta(minutes=5))
    result = adapter.stream_snapshot()
    assert not result["utema_executable_event_ids"]
    assert not any(e["entry"] and e.get("confirmed") for e in result["utema_events"])


def test_confirmed_checkpoint_restart_does_not_reauthorize_old_entry(monkeypatch, tmp_path):
    import config.settings as config
    scanner, adapter, _ = real_host(monkeypatch)
    off = NS(live_intrabar_signal_entries=False)
    monkeypatch.setattr(config, "get_settings", lambda: off)
    service = UTEmaTickService(scanner, checkpoint_dir=tmp_path)
    at = pd.Timestamp("2026-10-29 12:35")
    service.process_observation("NIFTY", 1002., at)
    service.process_observation("NIFTY", 1002., at+pd.Timedelta(minutes=5))
    assert adapter.stream_snapshot()["utema_executable_event_ids"]
    scanner2, adapter2, _ = real_host(monkeypatch)
    monkeypatch.setattr(config, "get_settings", lambda: off)
    restored = UTEmaTickService(scanner2, checkpoint_dir=tmp_path)
    restored.process_observation("NIFTY", 1002., at+pd.Timedelta(minutes=5, seconds=1))
    assert restored.health["NIFTY_5min"]["restored"]
    assert not adapter2.stream_snapshot()["utema_executable_event_ids"]


def test_confirmed_close_reaches_paper_entry_and_alignment(monkeypatch, tmp_path):
    import asyncio
    import config.settings as config
    from engine import ut_ema_lane as lane
    from tests.test_ut_ema_lane_2026_10_29 import host, settings
    stream_host, adapter, frame = real_host(monkeypatch)
    scanner, processor, rows = host(monkeypatch)
    off = settings(live_intrabar_signal_entries=False, ut_signal_mode="confirmed")
    monkeypatch.setattr(config, "get_settings", lambda: off)
    monkeypatch.setattr(lane, "market_stamp", lambda value=None:
        pd.Timestamp(value if value is not None else "2026-10-29 12:40"))
    service = UTEmaTickService(stream_host, checkpoint_dir=tmp_path)
    at = pd.Timestamp("2026-10-29 12:35")
    service.process_observation("NIFTY", 1002., at)
    service.process_observation("NIFTY", 1002., at+pd.Timedelta(minutes=5))
    result = adapter.stream_snapshot()
    scanner._utema_tick_service = service
    scanner.mtf = stream_host.mtf
    view = NS(results_5min=result, results_15min={})
    found = lane.build_candidates(processor, "NIFTY", view, {"strike_interval": 50}, 2, 75, off)
    assert found
    assert all(c.signal_fire_mode == "CANDLE_CLOSE" and not c.entered_intracandle for c in found)
    assert asyncio.run(lane.coordinate(scanner, found))
    assert len(rows) == 1
    assert rows[0].lots == 3
    scanner.trades.open_trade.assert_not_called()


def test_cold_start_retains_known_forming_range_without_emitting_entry():
    frame = pd.DataFrame({"open": [1000., 1000.], "high": [1001., 1010.],
                          "low": [999., 990.], "close": [1000., 1002.]},
                         index=pd.date_range("2026-10-29 09:15", periods=2, freq="5min"))
    core = UTEmaEngine(replace(Params(), execution_mode="Signal Bar Close"))
    core.seed_stream(frame, "NIFTY", "5min", as_of="2026-10-29 09:22")
    assert core.observe_tick(1003., "2026-10-29 09:22:01") is None
    assert core._stream_rows[-1] == (1000., 1010., 990., 1003.)
    assert not any(e.get("confirmed") for e in core._timeline.marks)


def test_overnight_update_does_not_stamp_yesterdays_close_as_fresh():
    values = [1000. - i*.05 for i in range(20)]
    frame = pd.DataFrame({"open": values, "high": [v+.1 for v in values],
                          "low": [v-.1 for v in values], "close": values},
                         index=pd.date_range("2026-10-29 13:45", periods=20, freq="5min"))
    core = UTEmaEngine(replace(Params(), execution_mode="Signal Bar Close"))
    core.seed_stream(frame, "NIFTY", "5min", as_of="2026-10-29 15:25")
    core.observe_tick(1002., "2026-10-29 15:25")
    core.observe_tick(1002., "2026-10-30 09:15",
                      session_open="2026-10-30 09:15", previous_session_close="2026-10-29 15:30")
    confirmed = [e for e in core._timeline.marks if e.get("confirmed") and e["entry"]]
    assert confirmed
    assert all(e["observed_at"].startswith("2026-10-29") for e in confirmed)
