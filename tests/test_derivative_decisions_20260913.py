from copy import deepcopy
from datetime import date
import math
from types import SimpleNamespace
from pathlib import Path
import threading

import pandas as pd
import pytest

from data.derivative_participation import DerivativeParticipation
from data.market_data import MarketDataProvider
from intelligence.derivative_context import derivative_corroboration
from intelligence.intelligence_aggregator import IntelligenceAggregator
from intelligence.oi_tracker import OITracker

INDICES = ["NIFTY", "BANKNIFTY", "MIDCPNIFTY", "SENSEX"]
FRAMES = [("1min", 60), ("5min", 300), ("15min", 900)]
END = pd.Timestamp("2026-09-11 13:00", tz="Asia/Kolkata").timestamp()


def observations(index="NIFTY", timeframe="5min", ratio=3.0, direction=1, family="futures", archive=None):
    seconds = dict(FRAMES)[timeframe]
    store = DerivativeParticipation(archive)
    for offset in range(0, seconds*2+1, 15):
        qty = 10000+min(offset, seconds)+max(0, offset-seconds)*ratio
        base = END-seconds*2
        if family == "futures":
            store.observe(index, family, "kite", "FUT", base+offset, qty,
                          oi=10000+offset, price=25000+direction*offset/100,
                          expiry="2026-09-29")
        else:
            for side, sign in (("CE", 1), ("PE", -1)):
                store.observe(index, family, "kite", side, base+offset, qty,
                              oi=10000+offset, price=100+direction*sign*offset/100,
                              expiry="2026-09-22", option_type=side, strike=25000)
    return store


def candles(tf):
    start = pd.Timestamp(END, unit="s", tz="UTC").tz_convert("Asia/Kolkata")-pd.Timedelta(minutes=dict(FRAMES)[tf]/60*60)
    closes = [25000+i for i in range(60)]
    return pd.DataFrame({"open": [v-.5 for v in closes], "high": [v+.5 for v in closes],
                         "low": [v-1 for v in closes], "close": closes,
                         "volume": 1000., "volume_basis": "fyers_index"},
                        index=pd.date_range(start, periods=60, freq=tf))


@pytest.mark.parametrize("index", INDICES)
@pytest.mark.parametrize("tf,seconds", FRAMES)
@pytest.mark.parametrize("family", ["futures", "options"])
def test_real_aggregator_consumes_eligible_derivatives_without_relabeling_index(index, tf, seconds, family):
    frame = candles(tf)
    scores = []
    for ratio in (None, 0, 1, 3):
        aggregate = IntelligenceAggregator()
        if ratio is not None:
            store = observations(index, tf, ratio=ratio, family=family)
            aggregate.derivative_source = store.features
        result = aggregate.analyze(index, tf, frame, spot_price=25000)
        scores.append(result["aggregate"]["score_norm"])
        assert result["aggregate"]["measured_volume"] == 1000
    assert scores[0] == scores[2]
    assert 0 < scores[1] < scores[2] < scores[3]
    assert frame.volume.eq(1000).all()


@pytest.mark.parametrize("mutation", [
    {"available": False}, {"entry_eligible": False}, {"clock": "receipt"},
    {"instrument": "SENSEX"}, {"timeframe": "1min"}, {"family": "options"},
    {"end_at": END-301}, {"end_at": END+1}, {"coverage_seconds": 90},
    {"max_observation_gap_seconds": 121}, {"relative_volume_ratio": None},
    {"relative_volume_ratio": math.nan}, {"expiry": "2026-09-01"},
])
def test_invalid_or_mismatched_evidence_abstains(mutation):
    payload = observations().features("NIFTY", "5min", END)
    payload["futures"][0].update(mutation)
    result = derivative_corroboration(payload, "NIFTY", "5min", END, .5)
    assert result["multiplier"] == 1 and not result["available"]


@pytest.mark.parametrize("conviction", [-.8, 0, .8])
@pytest.mark.parametrize("direction", [-1, 1])
def test_participation_never_creates_or_inverts_direction(conviction, direction):
    payload = observations(direction=direction).features("NIFTY", "5min", END)
    factor = derivative_corroboration(payload, "NIFTY", "5min", END, conviction)["multiplier"]
    assert .9 <= factor <= 1.1
    assert math.copysign(1, conviction*factor) == math.copysign(1, conviction)
    if conviction == 0:
        assert factor == 1


@pytest.mark.parametrize("mutation", ["provider", "expiry", "strike", "window", "same_direction", "oi_only", "single_leg"])
def test_options_need_matched_opposite_premium_moves(mutation):
    payload = observations(family="options").features("NIFTY", "5min", END)
    pe = payload["options"][1]
    if mutation == "provider": pe["provider"] = "angelone"
    if mutation == "expiry": pe["expiry"] = "2026-09-29"
    if mutation == "strike": pe["strike"] += 50
    if mutation == "window":
        pe["start_at"] -= 30
        pe["end_at"] -= 30
    if mutation == "same_direction": pe["price_change"] *= -1
    if mutation == "oi_only":
        for row in payload["options"]: row.update(price_change=0, oi_change=999999)
    if mutation == "single_leg": payload["options"].pop()
    result = derivative_corroboration(payload, "NIFTY", "5min", END, .5, spot_price=25000)
    assert result["multiplier"] == 1


def test_cross_provider_duplicate_futures_do_not_count_twice():
    payload = observations().features("NIFTY", "5min", END)
    second = dict(payload["futures"][0], provider="fyers")
    payload["futures"].append(second)
    assert derivative_corroboration(payload, "NIFTY", "5min", END, .5)["multiplier"] == 1


def test_archive_roundtrip_asof_reset_and_no_future(tmp_path):
    store = observations(archive=tmp_path)
    expected = store.features("NIFTY", "5min", END)
    store.flush()
    recovered = DerivativeParticipation(tmp_path)
    assert recovered.features("NIFTY", "5min", END) == expected
    assert recovered.features("NIFTY", "5min", END-86400)["futures"] == []
    assert not recovered.features("NIFTY", "5min", END-301)["futures"][0]["available"]
    recovered.observe("NIFTY", "futures", "kite", "FUT", END+15, 10, expiry="2026-09-29")
    recovered.flush()
    assert not DerivativeParticipation(tmp_path).features("NIFTY", "5min", END+15)["futures"][0]["available"]


def test_recorded_chain_preserves_identity_quality_and_never_looks_forward(tmp_path):
    store = DerivativeParticipation(tmp_path)
    chain = pd.DataFrame({"strike": [25000], "call_oi": [100], "put_oi": [200]})
    chain.attrs.update(expiry="22SEP2026", provider="fyers", observation_id="actual")
    store.record_option_chain("NIFTY", chain, END, {"intelligence_usable": False, "fallback": True})
    store.flush()
    restored = DerivativeParticipation(tmp_path)
    result, quality = restored.recorded_option_chain("NIFTY", END+60, expiry="22SEP2026")
    pd.testing.assert_frame_equal(result, chain)
    assert result.attrs == chain.attrs and not quality["intelligence_usable"]
    for index, cutoff, expiry in [("NIFTY", END-1, ""), ("SENSEX", END+1, ""),
                                   ("NIFTY", END+121, ""), ("NIFTY", END+1, "29SEP2026")]:
        assert restored.recorded_option_chain(index, cutoff, expiry)[0].empty


@pytest.mark.parametrize("tf,seconds", FRAMES)
def test_measured_full_window_oi_supersedes_fyers_day_reported_change(tf, seconds):
    chain = pd.DataFrame({"strike": [24950,25000,25050], "call_oi": 10000, "put_oi": 10000,
                          "call_oi_change": 100, "put_oi_change": 500})
    chain.attrs.update(instrument="NIFTY", expiry="22SEP2026", provider="fyers", oi_provider="fyers", oi_change_horizon_seconds=86400)
    payload = observations(timeframe=tf, family="options").features("NIFTY", tf, END)
    pair = payload["options"]
    payload["options"] = []
    for strike in chain.strike:
        for row in pair:
            row = dict(row, strike=float(strike), contract=str(strike)+row["option_type"],
                       oi_change=1000 if row["option_type"] == "PE" else 0)
            payload["options"].append(row)
    tracker = OITracker()
    assert not tracker.analyze(chain, timeframe_seconds=seconds)["change_comparable"]
    measured = tracker.analyze(chain, price_change=.2, timeframe_seconds=seconds, derivative_features=payload)
    assert measured["change_comparable"] and measured["signal"] == "BULLISH"
    assert measured["total_put_oi_change"] == 3000
    assert measured["oi_change_source"] == "exchange_measured_interval"
    neutral = tracker.analyze(chain, price_change=0, timeframe_seconds=seconds, derivative_features=payload)
    assert neutral["signal"] == "NEUTRAL" and neutral["score"] == 50
    payload["options"][0]["provider"] = "fyers"
    assert not tracker.analyze(chain, timeframe_seconds=seconds, derivative_features=payload)["change_comparable"]


@pytest.mark.parametrize("seconds", [300,900])
def test_poll_delta_cannot_pretend_full_window_oi(seconds):
    chain = pd.DataFrame({"strike": [24950,25000,25050], "call_oi": 10000, "put_oi": 10000,
                          "call_oi_change": 100, "put_oi_change": 500})
    chain.attrs["oi_change_horizon_seconds"] = 90
    result = OITracker().analyze(chain, price_change=.2, timeframe_seconds=seconds)
    assert not result["change_comparable"] and result["signal"] == "NEUTRAL"


def test_background_chain_subscribes_both_exact_legs_without_new_broker_requests():
    provider = MarketDataProvider.__new__(MarketDataProvider)
    provider.start_streams = True
    provider._ltp_cache = {"NIFTY": {"ltp": 25000}}
    requests = []
    def resolve(index, strike, side, expiry):
        requests.append((index, strike, side, expiry))
        return {"instrument_token": 1 if side == "CE" else 2, "tradingsymbol": side,
                "expiry": expiry, "strike": strike}
    provider.kite = SimpleNamespace(resolve_option=resolve)
    subscriptions = []
    provider.subscribe_tokens = lambda tokens: subscriptions.append(tokens)
    chain = pd.DataFrame({"strike": [25000]})
    chain.attrs["expiry"] = "22SEP2026"
    provider._subscribe_derivative_pairs("NIFTY", chain)
    assert subscriptions == [["1", "2"]]
    assert requests == [("NIFTY", 25000., side, date(2026,9,22)) for side in ("CE", "PE")]
    assert provider._derivative_token_identity["1"][2:] == ("22SEP2026", "CE", 25000.)
    assert provider._derivative_token_identity["2"][2:] == ("22SEP2026", "PE", 25000.)
    identity = provider._derivative_token_identity["1"]
    provider._register_derivative_contract("NIFTY", {"token": "1", "symbol": "CE", "expiry": "22SEP2026",
                                                  "strike": 2500000, "instrumenttype": "OPTIDX"})
    assert provider._derivative_token_identity["1"] == identity
    provider.start_streams = False
    provider._subscribe_derivative_pairs("NIFTY", chain)
    assert len(subscriptions) == 1


def test_archive_nan_is_missing_and_pending_buffer_is_bounded(tmp_path):
    store = DerivativeParticipation(tmp_path)
    chain = pd.DataFrame({"strike": [25000], "call_iv": [float("nan")]})
    store.record_option_chain("NIFTY", chain, END, {"score": float("nan")})
    store.flush()
    result, quality = DerivativeParticipation(tmp_path).recorded_option_chain("NIFTY", END)
    assert result.call_iv.isna().all() and quality["score"] is None
    for offset in range(20050):
        store.observe("NIFTY", "futures", "kite", "FUT", END+offset*5, 1000+offset)
    assert len(store._pending) == 20000 and store.archive_dropped_events > 0


def test_high_rate_ticks_keep_real_fifteen_minute_coverage():
    store = DerivativeParticipation()
    for decisecond in range(19001):
        stamp = END-1900+decisecond/10
        store.observe("NIFTY", "futures", "kite", "FUT", stamp,
                      10000+decisecond, expiry="2026-09-29")
    row = store.features("NIFTY", "15min", END)["futures"][0]
    assert row["available"] and row["relative_volume_ratio"] == pytest.approx(1)
    assert 900 <= row["coverage_seconds"] <= 905
    assert len(next(iter(store._rows.values()))) <= 381
    assert row["end_at"] == END


def test_slow_archive_write_does_not_block_tick_observation(tmp_path, monkeypatch):
    store = observations(archive=tmp_path)
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    original = Path.open
    def slow_open(path, *args, **kwargs):
        if args and args[0] == "a" and path.suffix == ".jsonl":
            entered.set()
            assert release.wait(3)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", slow_open)
    writer = threading.Thread(target=store.flush)
    writer.start()
    try:
        assert entered.wait(2)
        def tick():
            store.observe("NIFTY", "futures", "kite", "FUT", END+15, 20000, expiry="2026-09-29")
            observed.set()
        reader = threading.Thread(target=tick)
        reader.start()
        assert observed.wait(.5)
        reader.join(1)
    finally:
        release.set()
        writer.join(3)
