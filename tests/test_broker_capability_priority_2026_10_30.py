"""Kite/Angel capability priority with bounded, explicitly reported FYERS gaps."""
from datetime import date, timedelta
from types import SimpleNamespace
import threading
import time

import pandas as pd
import pytest

from data.kite_angel_migration import KiteAngelMigration
from data.angelone_market_data import AngelOneIndexQuoteSource
from tests.test_kite_angel_replacement_2026_10_23 import provider


def preferred(quantities=(123.,0.,789.)):
    index=pd.date_range("2026-10-30 09:15",periods=3,freq="min")
    return pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=list(quantities),
        volume_basis="constituent_shares",volume_complete=True,volume_members_observed=2,
        volume_members_expected=2,membership_version="verified-membership",
        volume_provider="kite",source="kite_constituent_history"),index=index)


def backup():
    frame=preferred()
    frame.loc[:,"volume"]=[456.,500.,600.]
    frame.loc[:,"volume_basis"]="fyers_index"
    frame.loc[:,"volume_provider"]="fyers"
    frame.loc[:,"source"]="fyers_history"
    return frame


def test_certified_preferred_volume_wins_over_fyers_including_real_zero():
    result=KiteAngelMigration._merge_preferred_index_history(preferred(),backup())
    assert result["volume"].tolist()==[123.,0.,789.]
    assert result["volume_provider"].eq("kite").all()
    assert result["volume_basis"].eq("constituent_shares").all()
    assert result["volume_complete"].all()


def test_fyers_only_fills_missing_constituent_intervals():
    frame=preferred()
    frame.loc[frame.index[1],"volume_complete"]=False
    frame.loc[frame.index[2],"volume_members_observed"]=1
    result=KiteAngelMigration._merge_preferred_index_history(frame,backup())
    assert result["volume"].tolist()==[123.,500.,600.]
    assert result["volume_provider"].tolist()==["kite","fyers","fyers"]
    assert result["close"].eq(100.).all()


def test_angel_certified_volume_precedes_fyers():
    frame=preferred().assign(volume_provider="angelone",source="angel_constituent_history")
    result=KiteAngelMigration._merge_preferred_index_history(frame,backup())
    assert result["volume"].tolist()==[123.,0.,789.]
    assert result["volume_provider"].eq("angelone").all()


@pytest.mark.parametrize("column,value",[("membership_version",""),("volume_provider","unknown"),
    ("volume_members_expected",0),("volume_complete",False),("volume",float("inf"))])
def test_uncertified_constituent_totals_cannot_displace_fyers(column,value):
    frame=preferred()
    frame.loc[:,column]=value
    result=KiteAngelMigration._merge_preferred_index_history(frame,backup())
    assert result["volume"].tolist()==[456.,500.,600.]
    assert result["volume_provider"].eq("fyers").all()


def test_replacement_health_names_constituent_primary_and_fyers_backup(tmp_path):
    source=provider(tmp_path,route="angel_fyers")
    health=source._replacement_health()
    assert health["cash_volume_provider"]=="constituent_shares"
    assert health["cash_volume_source_priority"]==["kite_constituent_shares","angelone_constituent_shares","fyers_index"]
    assert health["capability_backups"]["reported_day_oi_change"]=="fyers"


def day_source(tmp_path,monkeypatch):
    source=provider(tmp_path,route="angel_fyers")
    source.fyers=object()
    expiry=date(2026,11,3)
    clock=[pd.Timestamp("2026-10-30 10:00",tz="Asia/Kolkata").timestamp()]
    monkeypatch.setattr("data.kite_angel_migration.time.time",lambda:clock[0])
    calls=[]
    response={"s":"ok","data":{"expiryData":[{"date":"03-11-2026","expiry":"1"}],
        "optionsChain":[{"strike_price":strike,"option_type":side,"oich":value}
            for strike,value in [(100.,-5.),(150.,0.)] for side in ["CE","PE"]]}}
    source._call_fyers_bounded=lambda *a,**k:(calls.append(k) or True,response)
    chain=pd.DataFrame({"strike":[100.,150.],"call_oi":[100.,100.],"put_oi":[100.,100.],
                        "call_oi_change":[1.,2.],"put_oi_change":[1.,2.]})
    return source,expiry,clock,calls,chain


def test_reported_day_oi_reuses_exact_expiry_cache_without_changing_interval_delta(tmp_path,monkeypatch):
    source,expiry,clock,calls,chain=day_source(tmp_path,monkeypatch)
    first=source._supplement_reported_day_oi("NIFTY",expiry,chain)
    clock[0]+=59.
    cached=source._supplement_reported_day_oi("NIFTY",expiry,chain)
    assert len(calls)==1
    assert cached["call_day_oi_change"].tolist()==[-5.,0.]
    assert cached["call_oi_change"].tolist()==[1.,2.]
    assert cached["day_oi_change_received_at"].equals(first["day_oi_change_received_at"])
    clock[0]+=1.
    source._supplement_reported_day_oi("NIFTY",expiry,chain)
    assert len(calls)==2


def test_reported_day_oi_failure_has_cooldown_and_preserves_unknown(tmp_path,monkeypatch):
    source,expiry,clock,calls,chain=day_source(tmp_path,monkeypatch)
    def failed(*a,**k):
        calls.append(k)
        raise TimeoutError("offline")
    source._call_fyers_bounded=failed
    for delta in [0.,10.,40.]:
        clock[0]+=delta
        result=source._supplement_reported_day_oi("NIFTY",expiry,chain)
        assert result[["call_day_oi_change","put_day_oi_change"]].isna().all().all()
        assert result["day_oi_change_provider"].eq("unavailable").all()
    assert len(calls)==1
    clock[0]+=11.
    source._supplement_reported_day_oi("NIFTY",expiry,chain)
    assert len(calls)==2


def test_reported_day_oi_cache_does_not_leak_to_new_strikes_or_other_expiry(tmp_path,monkeypatch):
    source,expiry,clock,calls,chain=day_source(tmp_path,monkeypatch)
    source._supplement_reported_day_oi("NIFTY",expiry,chain)
    wider=source._supplement_reported_day_oi("NIFTY",expiry,pd.DataFrame({"strike":[100.,200.]}))
    assert len(calls)==1
    assert pd.isna(wider.iloc[1]["call_day_oi_change"])
    assert wider.iloc[1]["day_oi_change_provider"]=="unavailable"
    other=source._supplement_reported_day_oi("NIFTY",date(2026,11,10),chain)
    assert len(calls)==2
    assert other["call_day_oi_change"].isna().all()


def test_failed_refresh_does_not_relabel_expired_day_oi_as_fresh(tmp_path,monkeypatch):
    source,expiry,clock,calls,chain=day_source(tmp_path,monkeypatch)
    assert source._supplement_reported_day_oi("NIFTY",expiry,chain)["call_day_oi_change"].notna().all()
    source._call_fyers_bounded=lambda *a,**k:(False,None)
    clock[0]+=61.
    unavailable=source._supplement_reported_day_oi("NIFTY",expiry,chain)
    assert unavailable["call_day_oi_change"].isna().all()
    assert unavailable["day_oi_change_provider"].eq("unavailable").all()
    assert unavailable["day_oi_change_received_at"].isna().all()


def independent_provider(tmp_path, index="NIFTY", count=3):
    source=provider(tmp_path,route="angel_fyers")
    angel=AngelOneIndexQuoteSource()
    expiry=date.today()+timedelta(days=7)
    exchange="BFO" if index=="SENSEX" else "NFO"
    strikes=[73000.+i*100. if index=="SENSEX" else 22000.+i*50. for i in range(count)]
    raw=[dict(name=index,instrumenttype="OPTIDX",exch_seg=exchange,
        symbol=f"{index}{expiry.strftime('%d%b%y').upper()}{int(strike)}{side}",
        expiry=expiry.strftime("%d%b%Y").upper(),strike=strike*100.,token=f"own-{i}-{side}")
        for i,strike in enumerate(strikes) for side in ("CE","PE")]
    angel._option_master_date=angel._master_day()
    angel._option_master_by_symbol=angel._normalize_master_rows(raw)
    angel.connected=True
    angel._api=object()
    def quotes(wanted,**kwargs):
        return {(venue,token):dict(ltp=100.,opnInterest=100.,tradeVolume=20.,exchange_at=time.time(),
            depth={"buy":[dict(price=99.95,quantity=10)],"sell":[dict(price=100.,quantity=10)]})
            for venue,tokens in wanted.items() for token in tokens}
    angel.get_full_quotes=quotes
    def unavailable(*a,**k):raise RuntimeError("Kite data unavailable")
    source.kite=SimpleNamespace(available_strikes=unavailable,resolve_option=unavailable,
        get_option_chain=unavailable,get_depth=unavailable,_lock=threading.RLock(),_instruments={})
    source.angel_index=angel
    source._replacement_expiry=lambda name:expiry
    source.get_latest_price=lambda name:strikes[len(strikes)//2]
    source._apply_option_oi_snapshot_deltas=lambda name,frame,**kwargs:frame.assign(oi_change_source="snapshot_warmup")
    source._archive_option_chain=lambda *a,**k:None
    return source,angel,expiry,raw,strikes


@pytest.mark.parametrize("index",["NIFTY","SENSEX"])
def test_cold_kite_master_still_gets_full_angel_chain(tmp_path,index):
    source,angel,expiry,raw,strikes=independent_provider(tmp_path,index)
    chain=source._get_replacement_option_chain(index,time.time())
    assert chain["strike"].tolist()==strikes
    assert source._option_chain_meta[index]["source"]=="angelone"
    assert source._option_chain_meta[index]["auto_certified"]
    assert chain["call_token"].str.startswith("own-").all()
    assert chain["day_oi_change_provider"].eq("unavailable").all()


def test_independent_chain_keeps_configured_breadth_limit(tmp_path):
    source,angel,expiry,raw,strikes=independent_provider(tmp_path,count=100)
    chain=source._get_replacement_option_chain("NIFTY",time.time())
    assert len(chain)==61
    assert source._option_chain_meta["NIFTY"]["requested_strike_count"]==61


@pytest.mark.parametrize("index",["NIFTY","SENSEX"])
def test_cold_kite_exact_premium_and_touch_use_angel_own_metadata(tmp_path,index):
    source,angel,expiry,raw,strikes=independent_provider(tmp_path,index)
    premium=source.get_replacement_option_premium(index,strikes[1],"CE",expiry)
    assert premium==100.
    symbol=raw[2]["symbol"]
    exchange=raw[2]["exch_seg"]
    quote=source._replacement_touch(exchange,symbol,time.time(),1.5)
    assert quote["usable"] and quote["source"]=="angel_rest_touch"
    assert quote["bid"]==99.95 and quote["ask"]==100.
    source._derivative_token_identity={"execution-token":(index,f"KITE_ALIAS|{expiry.isoformat()}|execution-token",
        expiry.isoformat(),"CE",strikes[1])}
    bridged=source._replacement_touch(exchange,"KITE_ALIAS",time.time(),1.5)
    assert bridged["usable"] and bridged["ask"]==100.


@pytest.mark.parametrize("changes",[{"expiry":"invalid"},{"strike":"nan"},{"instrumenttype":"FUTIDX"},
    {"name":"OTHER"},{"exch_seg":"BFO"}])
def test_unverified_angel_metadata_cannot_define_chain_universe(tmp_path,changes):
    source,angel,expiry,raw,strikes=independent_provider(tmp_path)
    angel._option_master_by_symbol=angel._normalize_master_rows([dict(row,**changes) for row in raw])
    # No network refresh is allowed in this invalid-master probe.
    angel._load_option_master=lambda **kwargs:angel._option_master_by_symbol
    assert angel.option_contracts("NIFTY",expiry=expiry)==[]
    assert source._get_replacement_option_chain("NIFTY",time.time()).empty


def test_master_cache_roundtrip_keeps_verified_option_metadata(tmp_path):
    source,angel,expiry,raw,strikes=independent_provider(tmp_path)
    angel._option_master_cache_path=tmp_path/"angel-independent-master.json"
    angel._write_option_master_cache(angel._master_day(),angel._option_master_by_symbol)
    restored=AngelOneIndexQuoteSource()
    restored._option_master_cache_path=angel._option_master_cache_path
    contracts=restored.option_contracts("NIFTY",expiry=expiry)
    assert len(contracts)==6
    assert {row["strike"] for row in contracts}==set(strikes)


def test_primary_index_history_exception_still_uses_angel_prices(tmp_path):
    source=provider(tmp_path,route="angel_fyers")
    frame=preferred().iloc[:2]
    frame.index=frame.index-pd.Timedelta(days=1)
    target=frame.index[-1]+pd.Timedelta(minutes=1)
    def unavailable(*a,**k):raise RuntimeError("offline Kite history")
    source._kite_token_for_index=lambda name:1
    source.kite=SimpleNamespace(get_historical=unavailable,instrument_token=unavailable)
    source.angel_index=SimpleNamespace(get_history=lambda *a:frame[["open","high","low","close"]].copy(),
        resolve_market_instruments=lambda *a:[])
    key=("NIFTY",1,target.isoformat(),False)
    source._replacement_stop.wait=lambda timeout:False
    source._build_replacement_history(key,target)
    assert not source._replacement_history[key][1].empty
    assert source._replacement_history[key][1]["source"].eq("angel_constituent_history").all()
