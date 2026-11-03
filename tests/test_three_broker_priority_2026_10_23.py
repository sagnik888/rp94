"""Three-provider priority and capability-preservation regression tests."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import time

import pandas as pd
import pytest

from config.settings import Settings
from data.market_data import FYERS_INDEX_SYMBOLS, MarketDataProvider
from data.market_source_health import build_source_health
from engine.expiry_manager import expiry_manager
from scanner import Scanner
from tests.test_kite_angel_replacement_2026_10_23 import provider, chain_provider, chain_frame


def three(tmp_path):
    result = provider(tmp_path, route="angel_fyers")
    result._flag_quote_divergence = lambda *args: None
    result.kite = SimpleNamespace(is_connected=False)
    return result


def cached(price, source, now=None):
    stamp = time.time() if now is None else now
    return dict(ltp=price, source=source, time=stamp, received_at=stamp, exchange_at=stamp)


def test_new_default_keeps_both_supplementary_and_backup():
    assert Settings.model_fields["supplementary_market_data"].default == "angel_fyers"


def test_constructor_loads_fyers_backup_without_enabling_angel_execution(tmp_path, monkeypatch):
    settings = SimpleNamespace(supplementary_market_data="angel_fyers", constituent_membership_file=str(tmp_path/"absent.json"))
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    loads = []
    monkeypatch.setattr(MarketDataProvider, "_load_fyers_token", lambda self: loads.append(True))
    result = MarketDataProvider(start_streams=False)
    assert loads == [True]
    assert result._replacement_enabled() and result._fyers_enabled() and result._fyers_backup_enabled()
    assert result.smart_api is None
    assert not hasattr(result.angel_index, "place_order")


def test_websocket_priority_and_primary_rest_priority(tmp_path):
    result = three(tmp_path)
    result._ws_cache = {"100": cached(100, "kite_ws"), "angel:NIFTY": cached(101, "angel_ws"),
                        FYERS_INDEX_SYMBOLS["NIFTY"]: cached(102, "fyers_ws")}
    assert result._select_index_ws_quote("NIFTY")[0]["source"] == "kite_ws"
    result._ws_cache.pop("100")
    assert result._select_index_ws_quote("NIFTY")[0]["source"] == "angel_ws"
    result._ltp_cache["NIFTY"] = cached(99, "kite")
    assert result._select_index_ws_quote("NIFTY")[0]["source"] == "kite"
    result._ltp_cache.clear()
    result._ws_cache.pop("angel:NIFTY")
    assert result._select_index_ws_quote("NIFTY")[0]["source"] == "fyers_ws"


@pytest.mark.parametrize("kite,angel,expected,calls", [(100,101,100,["kite"]),
    (0,101,101,["kite","angel"]), (0,0,102,["kite","angel","fyers"])])
def test_rest_priority_never_queries_lower_provider_unnecessarily(tmp_path, kite, angel, expected, calls):
    result = three(tmp_path)
    observed = []
    result._kite_quote_target = lambda *args: ("NIFTY", "NSE")
    result.kite = SimpleNamespace(is_connected=True, get_ltp=lambda *args: observed.append("kite") or kite)
    result.angel_index = SimpleNamespace(get_index_quote=lambda *args: observed.append("angel") or
        dict(ltp=angel, received_at=time.time(), source="angel_rest"))
    result.fyers = object()
    result._call_fyers_bounded = lambda *args, **kwargs: (observed.append("fyers") or True,
        dict(s="ok", d=[dict(v=dict(lp=102, prev_close_price=98))]))
    assert result.get_latest_price_by_token("99926000", "NIFTY") == expected
    assert observed == calls


def test_malformed_supplementary_quote_still_reaches_backup(tmp_path):
    result = three(tmp_path)
    result._kite_quote_target = lambda *args: ("NIFTY", "NSE")
    result.angel_index = SimpleNamespace(get_index_quote=lambda *args: dict(ltp="invalid"))
    result.fyers = object()
    result._call_fyers_bounded = lambda *args, **kwargs: (True, dict(s="ok", d=[dict(v=dict(lp=102))]))
    assert result.get_latest_price_by_token("99926000", "NIFTY") == 102


def test_backup_volume_cannot_take_price_ownership_from_kite_or_angel(tmp_path):
    result = three(tmp_path)
    emitted = []
    result._emit_index_tick = lambda *args, **kwargs: emitted.append(args)
    now = time.time()
    result._ws_cache["100"] = cached(100, "kite_ws", now)
    result._ws_cache["angel:NIFTY"] = cached(101, "angel_ws", now)
    result._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = cached(999, "fyers_ws", now)
    assert result._emit_arbitrated_index_tick("NIFTY",999,10,"fyers_index","fyers_ws",now,now)
    assert emitted[-1][1:4] == (100,10,"fyers_index")
    assert not result._emit_arbitrated_index_tick("NIFTY",999,0,"","fyers_ws",now,now)
    result._ws_cache["100"] = cached(100,"kite_ws",now-100)
    assert not result._emit_arbitrated_index_tick("NIFTY",101,0,"","angel_ws",now+1,now+1)
    result._ws_cache["angel:NIFTY"] = cached(101,"angel_ws",now+1.3)
    assert result._emit_arbitrated_index_tick("NIFTY",101,0,"","angel_ws",now+1.3,now+1.3)
    assert result._emit_arbitrated_index_tick("NIFTY",999,20,"fyers_index","fyers_ws",now+1.4,now+1.4)
    assert emitted[-1][1:4] == (101,20,"fyers_index")
    assert result._index_price_route["NIFTY"]["active_source"] == "angel_ws"
    result._ws_cache["angel:NIFTY"] = cached(101,"angel_ws",now-100)
    result._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = cached(102,"fyers_ws",now+2)
    assert not result._emit_arbitrated_index_tick("NIFTY",102,20,"fyers_index","fyers_ws",now+2,now+2)
    result._ws_cache[FYERS_INDEX_SYMBOLS["NIFTY"]] = cached(102,"fyers_ws",now+2.3)
    assert result._emit_arbitrated_index_tick("NIFTY",102,21,"fyers_index","fyers_ws",now+2.3,now+2.3)
    assert result._index_price_route["NIFTY"]["active_source"] == "fyers_ws"
    assert result._emit_arbitrated_index_tick("NIFTY",100,21,"fyers_index","kite_ws",now+2.4,now+2.4)
    assert emitted[-1][1] == 100


def test_backup_prevents_false_total_outage_but_cannot_authorize_orders(tmp_path):
    result = three(tmp_path)
    result.fyers = object()
    result._last_fyers_ws_tick = time.time()
    health = build_source_health(result)
    assert not health["all_brokers_unavailable"]
    assert health["broker_degraded"]
    assert health["backup_provider"] == "fyers"
    assert not result.get_market_source_snapshot("NIFTY")["entry_eligible"]
    replacement = result._replacement_health()
    assert replacement["independent_broker_sources"] == 3
    assert replacement["third_provider_redundancy_preserved"]
    assert replacement["cash_volume_provider"] == "constituent_shares"


def test_fresh_fyers_rest_is_available_even_if_socket_is_silent(tmp_path):
    result = three(tmp_path)
    result.fyers = object()
    result._ltp_cache["NIFTY"] = cached(100,"fyers")
    assert not build_source_health(result)["all_brokers_unavailable"]


def test_preferred_prices_preserve_fyers_volume_and_missing_interval_truth(tmp_path):
    result = three(tmp_path)
    index = pd.date_range("2026-10-22 09:15",periods=3,freq="min")
    preferred = pd.DataFrame(dict(open=100,high=101,low=99,close=100,volume=999999,
        volume_basis="constituent_shares",volume_complete=True,source="kite_constituent_history"), index=index)
    backup = pd.DataFrame(dict(open=200,high=201,low=199,close=200,volume=[0,50],
        volume_basis="fyers_index",source="fyers_history"), index=index[:2].tz_localize("Asia/Kolkata"))
    merged = result._merge_preferred_index_history(preferred,backup)
    assert merged["close"].eq(100).all()
    assert merged["volume"].iloc[:2].tolist() == [0,50]
    assert pd.isna(merged["volume"].iloc[2])
    assert merged["volume_provider"].iloc[:2].eq("fyers").all()
    assert not bool(merged["volume_complete"].iloc[2])
    assert merged["volume_basis"].iloc[2] == "UNAVAILABLE"


def test_full_history_keeps_cash_volume_when_constituent_membership_missing(tmp_path):
    result = three(tmp_path)
    result.is_market_open = lambda *args: False
    index = pd.date_range("2026-10-22 09:15",periods=3,freq="min")
    preferred = pd.DataFrame(dict(open=100,high=101,low=99,close=100,volume=float("nan"),
        volume_complete=False,source="kite_constituent_history"),index=index)
    result._request_replacement_history = lambda *args,**kwargs: preferred
    result.fyers = SimpleNamespace(history=lambda **kwargs: None)
    candles = [[stamp.tz_localize("Asia/Kolkata").timestamp(),200,201,199,200,50] for stamp in index]
    result._call_bounded = lambda *args,**kwargs: (True,dict(s="ok",candles=candles))
    history = result.get_historical_candles("99926000","NSE","1min",instrument_name="NIFTY",persist_cache=False)
    assert history["close"].eq(100).all()
    assert history["volume"].eq(50).all()
    assert history["volume_basis"].eq("fyers_index").all()


def native_fyers_chain(result, monkeypatch):
    expiry = result._replacement_expiry("NIFTY")
    label = expiry.strftime("%d%b%Y").upper()
    monkeypatch.setattr(expiry_manager,"expiries",{"NIFTY":{"active_weekly":label}})
    monkeypatch.setattr(expiry_manager,"effective_expiry",lambda *args:label)
    result.fyers = object()
    result._maybe_self_heal_fyers = lambda *args: None
    calls = []
    rows = [dict(strike_price=strike,option_type=side,oi=100,volume=10,oich=1,ltp=10,
                 symbol=f"NSE:OPT{strike}{side}") for strike in [50,100,150] for side in ["CE","PE"]]
    response = dict(s="ok",data=dict(expiryData=[dict(date=expiry.strftime("%d-%m-%Y"),expiry="123")],optionsChain=rows))
    result._call_fyers_bounded = lambda *args,**kwargs: (calls.append("fyers") or True,response)
    return calls


def test_fyers_chain_is_used_only_after_kite_and_angel_fail(tmp_path,monkeypatch):
    result = chain_provider(tmp_path)
    result._market_data_route = "angel_fyers"
    calls = native_fyers_chain(result,monkeypatch)
    result.kite.get_option_chain = lambda *args,**kwargs: pd.DataFrame()
    result.angel_index.get_option_pairs = lambda *args: []
    chain = result.get_option_chain("NIFTY")
    assert len(chain) == 3 and calls == ["fyers"]
    meta = result._option_chain_meta["NIFTY"]
    assert meta["source"] == "fyers" and meta["role"] == "backup" and meta["auto_certified"]
    assert result.get_cached_option_chain("NIFTY") is chain


def test_usable_kite_chain_uses_fyers_only_for_native_day_change_capability(tmp_path,monkeypatch):
    result = chain_provider(tmp_path)
    result._market_data_route = "angel_fyers"
    calls = native_fyers_chain(result,monkeypatch)
    result.kite.get_option_chain = lambda *args,**kwargs: chain_frame()
    assert len(result.get_option_chain("NIFTY")) == 3
    result._replacement_day_oi_thread.join(2.)
    assert calls == ["fyers"]
    assert result._option_chain_meta["NIFTY"]["source"] == "kite"
    assert result._option_chain_meta["NIFTY"]["reported_day_oi_available"]
    chain = result._opt_chain_cache["opt_chain_NIFTY"]
    assert chain["call_day_oi_change"].eq(1).all()
    assert chain["call_oi_change"].eq(0).all()


def test_missing_kite_oi_tries_angel_before_fyers(tmp_path,monkeypatch):
    result = chain_provider(tmp_path)
    result._market_data_route = "angel_fyers"
    calls = native_fyers_chain(result,monkeypatch)
    result.kite.get_option_chain = lambda *args,**kwargs: chain_frame().assign(call_oi=0,put_oi=0)
    observed = []
    def pairs(index,contracts):
        observed.append("angel")
        return [dict(strike=ce["strike"]/100,CE=dict(ltp=10,oi=100,volume=20,symbol="ACE",token="a",exchange_at=time.time()),
            PE=dict(ltp=10,oi=100,volume=20,symbol="APE",token="b",exchange_at=time.time())) for ce,pe in contracts]
    result.angel_index.get_option_pairs = pairs
    assert len(result.get_option_chain("NIFTY")) == 3
    result._replacement_day_oi_thread.join(2.)
    assert observed == ["angel"] and calls == ["fyers"]
    assert result._option_chain_meta["NIFTY"]["source"] == "angelone"


def test_partial_angel_chain_recovers_full_breadth_from_fyers(tmp_path,monkeypatch):
    result = chain_provider(tmp_path)
    result._market_data_route = "angel_fyers"
    calls = native_fyers_chain(result,monkeypatch)
    result.kite.get_option_chain = lambda *args,**kwargs: pd.DataFrame()
    result.angel_index.get_option_pairs = lambda *args: [dict(strike=100,
        CE=dict(ltp=10,oi=100,volume=20,symbol="ACE",token="a",exchange_at=time.time()),
        PE=dict(ltp=10,oi=100,volume=20,symbol="APE",token="b",exchange_at=time.time()))]
    assert len(result.get_option_chain("NIFTY")) == 3
    assert calls == ["fyers"]
    assert result._option_chain_meta["NIFTY"]["source"] == "fyers"


def test_reported_day_zero_is_preserved_separately_from_snapshot_warmup(tmp_path,monkeypatch):
    result = chain_provider(tmp_path)
    result._market_data_route = "angel_fyers"
    native_fyers_chain(result,monkeypatch)
    native_call = result._call_fyers_bounded
    def zero(*args,**kwargs):
        ok,response = native_call(*args,**kwargs)
        for row in response["data"]["optionsChain"]:
            row["oich"] = 0
        return ok,response
    result._call_fyers_bounded = zero
    result.kite.get_option_chain = lambda *args,**kwargs: chain_frame()
    chain = result.get_option_chain("NIFTY")
    result._replacement_day_oi_thread.join(2.)
    chain = result._opt_chain_cache["opt_chain_NIFTY"]
    assert chain["call_day_oi_change"].eq(0).all()
    assert chain["day_oi_change_provider"].eq("fyers").all()
    assert result._option_chain_meta["NIFTY"]["reported_day_oi_available"]
    assert chain["oi_change_source"].eq("snapshot_warmup").all()


def test_exact_premium_reaches_fyers_backup_after_preferred_failure(tmp_path,monkeypatch):
    result = three(tmp_path)
    result._replacement_expiry = lambda *args: datetime.now().date()+timedelta(days=7)
    result.get_replacement_option_premium = lambda *args: None
    result.fyers = object()
    scanner = Scanner.__new__(Scanner)
    scanner.data = result
    scanner._candidate_matches_active_option_contract = lambda *args: True
    active = result._replacement_expiry("NIFTY")
    monkeypatch.setattr(expiry_manager,"expiries",{"NIFTY":{"active_weekly":active.strftime("%d%b%Y").upper()}})
    result.fyers = SimpleNamespace(optionchain=lambda **kwargs: dict(data=dict(
        expiryData=[dict(date=active.strftime("%d-%m-%Y"),expiry="123")],
        optionsChain=[dict(strike_price=100,option_type="CE",ltp=12)])))
    assert scanner._get_fyers_option_chain_premium_direct(SimpleNamespace(),"NIFTY","CE",100) == 12
