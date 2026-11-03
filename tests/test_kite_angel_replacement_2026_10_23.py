import json
import threading
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from data.angelone_market_data import AngelOneIndexQuoteSource
from data.angelone_readonly import ReadOnlyBudget
from data.constituent_volume import ConstituentVolumeService
from data.market_data import MarketDataProvider
from data.market_source_health import build_market_source_snapshot, build_source_health
from data.candle_builder import CandleBuilder
from scanner import Scanner


def manifest(tmp_path, **changes):
    record = dict(index="NIFTY", effective_from="2026-01-01", effective_until="2027-01-01",
                  version="official-2026", source="https://www.niftyindices.com/indices",
                  expected_count=2, members=[dict(exchange="NSE", symbol="AAA"), dict(exchange="NSE", symbol="BBB")])
    record.update(changes)
    path = tmp_path/"members.json"
    path.write_text(json.dumps(dict(schema_version=1, memberships=[record])))
    return path


def provider(tmp_path, route="kite_angel"):
    result = MarketDataProvider.__new__(MarketDataProvider)
    result._initialize_replacement(SimpleNamespace(supplementary_market_data=route,
        replacement_chain_strikes_each_side=30, constituent_membership_file=str(manifest(tmp_path)),
        constituent_history_refresh_seconds=60, constituent_cache_dir=str(tmp_path/"candidate")))
    result._ws_cache, result._ltp_cache = {}, {}
    result._index_quote_lock = threading.RLock()
    result._kite_index_names = {"100": "NIFTY"}
    result._last_kite_ws_tick = result._last_fyers_ws_tick = result._last_angel_ws_tick = 0.0
    result._fyers_auth_required, result._fyers_refresh_disabled = False, False
    result._fyers_auth_reason = ""
    result._yahoo_request_count, result._yahoo_last_used_at = 0, None
    result._provider_started_at = time.time()-100
    result.is_connected, result.fyers, result.smart_api = False, None, None
    result.is_market_open = lambda *args: True
    result._option_chain_meta, result._opt_chain_cache = {}, {}
    result._last_opt_chain_fetch, result._last_opt_chain_error = {}, {}
    return result


@pytest.mark.parametrize("changes", [dict(expected_count=3), dict(source="https://example.com/list"),
    dict(effective_until="2025-01-01"), dict(members=[dict(exchange="BSE", symbol="AAA")], expected_count=1),
    dict(members=[dict(exchange="NSE", symbol="AAA")]*2)])
def test_invalid_membership_never_certifies_volume(tmp_path, changes):
    service = ConstituentVolumeService(manifest(tmp_path, **changes))
    assert service.membership("NIFTY", date(2026, 10, 23)) is None
    assert service.last_error


def test_membership_versions_apply_on_effective_day(tmp_path):
    path = manifest(tmp_path, effective_until="2026-10-23")
    content = json.loads(path.read_text())
    content["memberships"].append(dict(content["memberships"][0], effective_from="2026-10-23",
        effective_until="2027-01-01", version="replacement", expected_count=1,
        members=[dict(exchange="NSE", symbol="CCC")]))
    path.write_text(json.dumps(content))
    service = ConstituentVolumeService(path)
    assert service.membership("NIFTY", date(2026, 10, 22)).version == "official-2026"
    assert service.membership("NIFTY", date(2026, 10, 23)).members == (("NSE", "CCC"),)
    assert service.membership("NIFTY", date(2027, 1, 1)) is None
    content["memberships"][1]["effective_from"] = "2026-10-22"
    path.write_text(json.dumps(content))
    assert service.load() is False


def test_zero_is_measured_missing_and_forming_minutes_are_not(tmp_path):
    service = ConstituentVolumeService(manifest(tmp_path))
    bars = pd.date_range("2026-10-23 09:15", periods=3, freq="min", tz="Asia/Kolkata")
    stocks = {("NSE", "AAA"): pd.DataFrame({"volume": [0, 10, 20]}, index=bars),
              ("NSE", "BBB"): pd.DataFrame({"volume": [0, 30]}, index=bars[[0, 2]])}
    result = service.aggregate("NIFTY", bars, stocks, "kite", now="2026-10-23 09:17:30+05:30")
    assert result.iloc[0]["volume"] == 0
    assert bool(result.iloc[0]["volume_complete"])
    assert result["volume"].iloc[1:].isna().all()
    assert result.iloc[1]["volume_members_observed"] == 1
    assert not result["volume_complete"].iloc[1:].any()


def test_live_cumulative_never_certifies_an_exact_interval(tmp_path):
    service = ConstituentVolumeService(manifest(tmp_path))
    now = pd.Timestamp("2026-10-23 09:16:30+05:30").timestamp()
    assert service.observe("NSE", "AAA", 100, now-1, now, "angel_ws")
    assert not service.observe("NSE", "AAA", 99, now, now, "angel_ws")
    assert service.activity("NIFTY", now=now)["volume"] is None
    assert service.observe("NSE", "BBB", 200, now, now, "angel_ws")
    activity = service.activity("NIFTY", now=now)
    assert activity["volume"] == 300
    assert activity["volume_complete"] is False
    assert activity["provisional"] is True
    assert service.activity("NIFTY", now=now+21)["volume"] is None


def test_full_quote_batches_share_budget_and_separate_exchange_tokens():
    source = AngelOneIndexQuoteSource()
    source.connected = True
    calls, reservations = [], []
    source._budget = SimpleNamespace(reserve=lambda endpoint, **kwargs: reservations.append(endpoint) or True)
    def quote(mode, exchangeTokens):
        calls.append(exchangeTokens)
        return dict(status=True, data=dict(fetched=[dict(exchange=exchange, symbolToken=token,
            ltp=100, exchFeedTime="23-Oct-2026 09:20:00") for exchange, tokens in exchangeTokens.items() for token in tokens]))
    source._api = SimpleNamespace(getMarketData=quote)
    rows = source.get_full_quotes({"NFO": [str(i) for i in range(60)], "BFO": ["0"]})
    assert len(calls) == 2 and len(rows) == 61
    assert all(sum(map(len, batch.values())) <= 50 for batch in calls)
    assert ("BFO", "0") in rows and ("NFO", "0") in rows
    assert rows[("NFO", "0")]["exchange_at"] == pd.Timestamp("2026-10-23 09:20:00+05:30").timestamp()
    assert reservations == ["quote", "quote"]


def test_quote_budget_never_queues_when_caller_does_not_allow_wait():
    budget = ReadOnlyBudget()
    assert budget.reserve("quote")
    assert not budget.reserve("quote")
    assert budget.reserve("history")


def test_angel_history_chunks_and_excludes_unfinished_candles(monkeypatch):
    source = AngelOneIndexQuoteSource()
    source.connected = True
    monkeypatch.setattr(source, "connect", lambda: True)
    source._budget = SimpleNamespace(reserve=lambda *args, **kwargs: True)
    calls = []
    def candles(params):
        calls.append(params)
        return dict(status=True, data=[[params["fromdate"]+":00+05:30", 1, 2, 1, 2, 0]])
    source._api = SimpleNamespace(getCandleData=candles)
    result = source.get_history("NSE", "angel-token", "1min", "2026-08-01 09:15", "2026-09-15 09:15")
    assert len(calls) == 3
    assert result.index.tz is None
    assert result["volume"].eq(0).all()
    assert all(row["symboltoken"] == "angel-token" for row in calls)


def test_stream_rejects_old_ticks_and_callbacks_after_stop(monkeypatch):
    source = AngelOneIndexQuoteSource()
    source.connected = True
    source._api = object()
    source._jwt_token, source._feed_token = "jwt", "feed"
    monkeypatch.setattr(source, "connect", lambda: True)
    ready, stop = threading.Event(), threading.Event()
    instances, ticks = [], []
    class Socket:
        def __init__(self, *args, **kwargs): instances.append(self)
        def subscribe(self, correlation, mode, rows): self.subscription = (mode, rows)
        def connect(self):
            self.on_open(self)
            ready.set()
            stop.wait(2)
        def close_connection(self): stop.set()
    monkeypatch.setattr(source, "_load_stream_sdk", lambda: Socket)
    assert source.start_stream([dict(exchange="NSE", symbol="NIFTY", token="own-token", exchange_type=1)],
                               lambda identity, quote: ticks.append(quote))
    assert ready.wait(1)
    raw = dict(exchange_type=1, token="own-token", last_traded_price=10000, exchange_timestamp=time.time()*1000)
    instances[0].on_data(None, raw)
    assert ticks[-1]["ltp"] == 100
    instances[0].on_data(None, dict(raw, exchange_timestamp=(time.time()-60)*1000))
    assert len(ticks) == 1
    source.stop_stream()
    instances[0].on_data(None, raw)
    assert len(ticks) == 1
    source._stream_thread.join(1)


def test_angel_health_is_data_only_and_does_not_authorize_entries(tmp_path):
    result = provider(tmp_path)
    now = time.time()
    result._last_angel_ws_tick = now
    result._ws_cache["angel:NIFTY"] = dict(ltp=100, time=now, exchange_at=now, source="angel_ws")
    health = build_source_health(result, now_ts=now)
    assert health["angelone"] and not health["all_brokers_unavailable"]
    snapshot = build_market_source_snapshot(result, "NIFTY", {}, {}, now_ts=now)
    assert not snapshot["entry_eligible"]
    assert snapshot["sources"][0]["order_authority"] is False
    result._last_angel_ws_tick = now-100
    assert build_source_health(result, now_ts=now)["all_brokers_unavailable"]
    result._market_data_route = "fyers"
    assert result._replacement_health()["independent_broker_sources"] == 3


def test_vix_callback_never_builds_a_tradable_index_candle(tmp_path):
    result = provider(tmp_path)
    result._emit_arbitrated_index_tick = lambda *args: pytest.fail("VIX entered index candles")
    result._on_replacement_tick(dict(auxiliary="INDIAVIX"), dict(ltp=15, received_at=time.time()))
    assert result._ws_cache["angel:INDIAVIX"]["ltp"] == 15


def test_resample_requires_every_minute_and_preserves_missing_volume(tmp_path):
    result = provider(tmp_path)
    result._replacement_stop.set()
    bars = pd.date_range("2026-10-23 09:15", periods=5, freq="min")
    base = pd.DataFrame(dict(open=1, high=2, low=1, close=2, volume=[1, 2, float("nan"), 4, 5],
        volume_complete=[True, True, False, True, True], volume_basis="constituent_shares",
        source="kite_constituent_history", membership_version="official-2026", volume_provider="kite"), index=bars)
    result._replacement_history[("NIFTY", 1, "latest", True)] = (time.time(), base)
    frame = result._request_replacement_history("NIFTY", "5min", 1, None)
    assert pd.isna(frame.iloc[0]["volume"])
    assert not bool(frame.iloc[0]["volume_complete"])
    assert frame.iloc[0]["volume_basis"] == "UNAVAILABLE"
    result._replacement_history[("NIFTY", 1, "latest", True)] = (time.time(), base.iloc[:4])
    assert result._request_replacement_history("NIFTY", "5min", 1, None).empty


def chain_provider(tmp_path):
    result = provider(tmp_path)
    expiry = date.today()+timedelta(days=7)
    result._replacement_expiry = lambda index: expiry
    result.get_latest_price = lambda index: 100
    result._archive_option_chain = lambda *args: None
    def deltas(index, frame, **kwargs):
        frame["oi_change_source"] = "snapshot_warmup"
        return frame
    result._apply_option_oi_snapshot_deltas = deltas
    result.kite = SimpleNamespace(available_strikes=lambda *args, **kwargs: [50., 100., 150.])
    def resolve(index, strike, side, **kwargs):
        assert kwargs["expiry"] == expiry
        return dict(name=index, strike=strike, instrument_type=side, expiry=expiry, exchange="NFO",
                    tradingsymbol=f"KITE{int(strike)}{side}")
    result.kite.resolve_option = resolve
    result.angel_index = SimpleNamespace(get_option_pairs=lambda *args: pytest.fail("unneeded Angel request"))
    return result


def chain_frame():
    return pd.DataFrame(dict(strike=[50., 100., 150.], call_ltp=10, put_ltp=20, call_oi=100, put_oi=200,
        call_volume=10, put_volume=20, call_oi_change=0, put_oi_change=0,
        call_quote_at=datetime.now(), put_quote_at=datetime.now()))


def test_complete_kite_chain_reuses_existing_builder_without_angel(tmp_path):
    result = chain_provider(tmp_path)
    result.kite.get_option_chain = lambda *args, **kwargs: chain_frame()
    chain = result.get_option_chain("NIFTY")
    assert len(chain) == 3
    assert result._option_chain_meta["NIFTY"]["auto_certified"]
    assert result._option_chain_meta["NIFTY"]["source"] == "kite"
    assert result.get_cached_option_chain("NIFTY") is chain


def test_partial_kite_chain_uses_same_expiry_and_breadth_on_angel(tmp_path):
    result = chain_provider(tmp_path)
    result.kite.get_option_chain = lambda *args, **kwargs: chain_frame().iloc[:1]
    def pairs(index, contracts):
        assert len(contracts) == 3
        return [dict(strike=ce["strike"]/100, CE=dict(ltp=10, oi=100, volume=20, symbol="ACE", token="a", exchange_at=time.time()),
                     PE=dict(ltp=10, oi=100, volume=20, symbol="APE", token="b", exchange_at=time.time())) for ce, pe in contracts]
    result.angel_index.get_option_pairs = pairs
    assert len(result.get_option_chain("NIFTY")) == 3
    assert result._option_chain_meta["NIFTY"]["auto_certified"]
    assert result._option_chain_meta["NIFTY"]["source"] == "angelone"


def test_partial_angel_chain_cannot_claim_equivalent_coverage(tmp_path):
    result = chain_provider(tmp_path)
    result.kite.get_option_chain = lambda *args, **kwargs: pd.DataFrame()
    result.angel_index.get_option_pairs = lambda *args: [dict(strike=100,
        CE=dict(ltp=10, oi=100, volume=20, symbol="ACE", token="a"),
        PE=dict(ltp=10, oi=100, volume=20, symbol="APE", token="b"))]
    result.get_option_chain("NIFTY")
    meta = result._option_chain_meta["NIFTY"]
    assert meta["requested_strike_count"] == 3 and meta["strike_count"] == 1
    assert not meta["coverage_complete"] and not meta["auto_certified"]


def test_candidate_history_reconciles_stock_candles_without_touching_fyers_cache(tmp_path):
    result = provider(tmp_path)
    bars = pd.date_range("2026-10-22 09:15", periods=5, freq="min")
    index_frame = pd.DataFrame(dict(open=100, high=101, low=99, close=100, volume=0), index=bars)
    def history(token, *args, **kwargs):
        return index_frame if token == 100 else pd.DataFrame(dict(volume=token), index=bars)
    result.kite = SimpleNamespace(get_historical=history, instrument_token=lambda symbol, exchange: {"AAA": 1, "BBB": 2}[symbol])
    result.angel_index = SimpleNamespace(resolve_market_instruments=lambda keys: pytest.fail("Kite coverage complete"))
    key = ("NIFTY", 1, "latest", True)
    result._replacement_history_pending.add(key)
    result._build_replacement_history(key, pd.Timestamp("2026-10-22 10:00"))
    _, frame = result._replacement_history[key]
    assert frame["volume"].eq(3).all()
    assert frame["volume_complete"].all()
    assert frame["volume_basis"].eq("constituent_shares").all()
    assert (tmp_path/"candidate"/"NIFTY_1min.csv").exists()
    assert not result._replacement_history_pending
    assert result._replacement_health()["third_provider_redundancy_preserved"] is False


def test_completed_zero_corrects_prior_volume_without_losing_provenance():
    bars = pd.DatetimeIndex(["2026-10-23 09:15"])
    old = pd.DataFrame(dict(volume=[50.], volume_complete=True, volume_basis="constituent_shares",
                           source="kite_constituent_history", volume_provider="kite", membership_version="v1"), index=bars)
    new = old.copy()
    new["volume"] = 0.0
    corrected = CandleBuilder._restore_zeroed_volume(new, old)
    corrected = CandleBuilder._restore_candle_telemetry(corrected, old)
    assert corrected.iloc[0]["volume"] == 0
    tick = pd.DataFrame(dict(volume=[0.], volume_complete=False, volume_basis="", source="live_tick",
                            volume_provider="", membership_version=""), index=bars)
    retained = CandleBuilder._restore_candle_telemetry(tick, old)
    assert retained.iloc[0]["volume"] == 50
    assert retained.iloc[0]["volume_complete"]
    assert retained.iloc[0]["membership_version"] == "v1"
    # A legacy FYERS row has no completeness column; the incoming explicit zero
    # must still replace it rather than resurrecting the previous provider's 50.
    legacy = old.drop(columns=["volume_complete"]).assign(volume_basis="fyers_index", source="fyers_history")
    corrected = CandleBuilder._restore_candle_telemetry(new, legacy)
    assert corrected.iloc[0]["volume"] == 0
    assert corrected.iloc[0]["volume_basis"] == "constituent_shares"
    zero_retained = CandleBuilder._restore_candle_telemetry(tick, new)
    assert zero_retained.iloc[0]["volume_complete"]
    assert zero_retained.iloc[0]["volume_basis"] == "constituent_shares"


def test_stale_contract_clocks_block_cached_premiums_and_chain_quality(tmp_path):
    result = chain_provider(tmp_path)
    result.kite.get_option_chain = lambda *args, **kwargs: chain_frame().assign(
        call_quote_at=datetime.now()-timedelta(minutes=5), put_quote_at=datetime.now()-timedelta(minutes=5))
    result.angel_index.get_option_pairs = lambda index, contracts: [dict(strike=ce["strike"]/100,
        CE=dict(ltp=10, oi=100, volume=20, symbol="ACE", token="a", exchange_at=time.time()-300),
        PE=dict(ltp=10, oi=100, volume=20, symbol="APE", token="b", exchange_at=time.time()-300)) for ce, pe in contracts]
    # Angel is permitted to recover stale or incomplete Kite data, but this
    # deliberately models both routes lacking an acceptable fresh replacement.
    chain = result.get_option_chain("NIFTY")
    assert not result._option_chain_meta["NIFTY"]["auto_certified"]
    assert result.get_cached_option_chain("NIFTY") is None
    scanner = Scanner.__new__(Scanner)
    assessed = scanner._assess_chain_quality("NIFTY", chain, result._option_chain_meta["NIFTY"])
    assert not assessed["intelligence_usable"]


def test_angel_index_stream_never_replaces_fresh_kite_authority(tmp_path):
    result = provider(tmp_path)
    now = time.time()
    result._ws_cache["100"] = dict(ltp=100., time=now, exchange_at=now, source="kite_ws")
    result._ws_cache["angel:NIFTY"] = dict(ltp=100.1, time=now, exchange_at=now, source="angel_ws")
    result._flag_quote_divergence = lambda *args: None
    assert result._select_index_ws_quote("NIFTY")[0]["source"] == "kite_ws"
    result._ws_cache["100"]["time"] = now-60
    result._ws_cache["100"]["exchange_at"] = now-60
    assert result._select_index_ws_quote("NIFTY")[0]["source"] == "angel_ws"


def test_missing_parity_captures_block_acceptance(tmp_path):
    from scripts.verify_kite_angel_parity import compare_captures
    result = compare_captures(tmp_path/"missing_old", tmp_path/"missing_new")
    assert len(result["failures"]) == 18
    assert result["captured_field_parity"] is False
    assert result["cutover_authorized"] is False


def test_capture_parity_detects_volume_and_contract_coverage_changes(tmp_path):
    from scripts.verify_kite_angel_parity import compare_captures, INDICES
    baseline, candidate = tmp_path/"baseline", tmp_path/"candidate"
    baseline.mkdir(); candidate.mkdir()
    histories = pd.DataFrame(dict(timestamp=["2026-10-23 09:15+05:30"], open=[100], high=[101], low=[99], close=[100], volume=[50],
        volume_complete=[True], volume_basis=["constituent_shares"], membership_version=["v1"]))
    chains = pd.DataFrame(dict(timestamp=["2026-10-23 09:20+05:30"], expiry=["2026-10-30"], strike=[100],
        call_ltp=[10], put_ltp=[10], call_oi=[100], put_oi=[200], call_volume=[10], put_volume=[20], call_oi_change=[1], put_oi_change=[2]))
    for folder in (baseline, candidate):
        for index in INDICES:
            for interval in ("1min", "5min", "15min"):
                histories.to_csv(folder/f"{index}_{interval}.csv", index=False)
            chains.to_csv(folder/f"{index}_chain.csv", index=False)
        histories.rename(columns={"close": "ltp"})[["timestamp", "ltp"]].to_csv(folder/"INDIAVIX.csv", index=False)
        (folder/"decisions.json").write_text(json.dumps([dict(timestamp="2026-10-23T09:20:00+05:30", instrument="NIFTY",
            timeframe="5min", entry_eligible=False, selected_contract=None, management="none", exit=None)]))
    assert compare_captures(baseline, candidate)["captured_field_parity"]
    histories.assign(volume=0).to_csv(candidate/"NIFTY_1min.csv", index=False)
    chains.assign(expiry="2026-11-06").to_csv(candidate/"SENSEX_chain.csv", index=False)
    result = compare_captures(baseline, candidate)
    assert {row["reason"] for row in result["failures"]} == {"volume_differs", "coverage_or_contract_mismatch"}


def test_history_workers_cannot_expand_with_backtest_dates(tmp_path, monkeypatch):
    result = provider(tmp_path)
    launched = []
    monkeypatch.setattr("data.kite_angel_migration.threading.Thread", lambda **kwargs:
                        SimpleNamespace(start=lambda: launched.append(kwargs)))
    for offset in range(10):
        result._request_replacement_history("NIFTY", "1min", 15, datetime(2026, 10, 23)-timedelta(days=offset))
    assert len(launched) == 1
    assert len(result._replacement_history_pending) == 1


def test_late_prewarm_cannot_restart_a_closed_provider(tmp_path):
    result = provider(tmp_path)
    result.start_streams = True
    result._replacement_closed = True
    result.angel_index = SimpleNamespace(resolve_market_instruments=lambda keys: pytest.fail("provider closed"))
    assert result._start_replacement_stream() is False
