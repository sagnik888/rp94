"""Certified preferred history avoids backup work; holes preserve fallback."""
import pandas as pd
from tests.test_three_broker_priority_2026_10_23 import three, cached


def history():
    index=pd.date_range("2026-10-22 09:15",periods=375,freq="min")
    return pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=123.,
        volume_complete=True,volume_basis="constituent_shares",volume_provider="angelone",
        volume_members_observed=50,volume_members_expected=50,membership_version="dated-membership",
        source="angel_constituent_history"),index=index)


def test_complete_preferred_history_is_certified(tmp_path):
    provider=three(tmp_path)
    assert provider._preferred_history_complete(history(),"1min",1,"2026-10-23 00:00")


def test_missing_interval_or_membership_preserves_backup(tmp_path):
    provider=three(tmp_path)
    frame=history()
    assert not provider._preferred_history_complete(frame.drop(frame.index[20]),"1min",1,"2026-10-23 00:00")
    assert not provider._preferred_history_complete(frame.assign(membership_version=""),"1min",1,"2026-10-23 00:00")
    assert not provider._preferred_history_complete(frame.assign(volume_complete=False),"1min",1,"2026-10-23 00:00")


def test_requested_intraday_start_does_not_require_unrequested_morning(tmp_path):
    provider=three(tmp_path)
    old=history().loc["2026-10-22 10:00":]
    current=history().iloc[:45].copy()
    current.index=pd.date_range("2026-10-23 09:15",periods=45,freq="min")
    frame=pd.concat([old,current])
    assert provider._preferred_history_complete(frame,"1min",1,"2026-10-23 10:00")


def test_complete_preferred_history_never_calls_fyers(tmp_path):
    provider=three(tmp_path)
    frame=history()
    provider.is_market_open=lambda *a:False
    provider._request_replacement_history=lambda *a,**k:frame
    provider.fyers=object()
    provider._call_bounded=lambda *a,**k:(_ for _ in ()).throw(AssertionError("healthy preferred history called backup"))
    result=provider.get_historical_candles("99926000","NSE","1min",days_back=1,
        instrument_name="NIFTY",to_date=pd.Timestamp("2026-10-23 00:00").to_pydatetime(),persist_cache=False)
    assert result.volume.eq(123.).all()


def test_fresh_preferred_preopen_quote_does_not_call_fyers(tmp_path):
    provider=three(tmp_path)
    provider.get_market_session_label=lambda *a:"PRE-OPEN"
    provider._preopen_rest_quote_allowed=lambda **k:True
    quote=cached(100.,"kite_ws");quote["previous_close"]=99.
    provider._select_index_ws_quote=lambda *a,**k:(quote,0.)
    provider._get_preopen_fyers_index_quote=lambda *a:(_ for _ in ()).throw(AssertionError("preferred quote called backup"))
    result=provider.get_index_quote("NIFTY")
    assert result["ltp"]==100. and result["source"]=="kite_ws"
