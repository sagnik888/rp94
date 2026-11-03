"""Queued native entries retain their owning-timeframe observation deadline."""
from types import SimpleNamespace as NS
from types import MethodType

import pytest

from engine import ut_ema_lane as lane
from engine.ut_ema_engine import market_stamp


def queued(monkeypatch, *, entry_time="2026-10-29 10:00", clock="2026-10-29 10:01",
           timeframe="5min", manual=False, adding=False):
    import config.settings as config
    monkeypatch.setattr(config, "get_settings", lambda: NS(live_intrabar_signal_entries=True))
    monkeypatch.setattr(lane, "market_stamp", lambda value=None: market_stamp(clock if value is None else value))
    item={"lifecycle_id":"queued-lifecycle", "entry_time":entry_time,
          "applied_event_ids":["entry-id"], "alignment_event_id":"add-id"}
    native={"entry_time":entry_time,"exit_time":""}
    result={"source_engine":"ut_ema", "utema_records":{"queued-lifecycle":native},
            "utema_executable_event_ids":["entry-id","add-id"]}
    trade=NS(instrument="NIFTY",timeframe=timeframe,status="OPEN",execution_status="OPEN",
             utema_state=item,_scale_in_inflight=adding,source_engine="ut_ema",source_lane="UTEMA")
    scanner=NS(mode="REAL",auto_mode=not manual,is_running=True,system_power="ON",
               mtf=NS(_result_cache={f"NIFTY_{timeframe}":result}))
    return scanner,trade,native


@pytest.mark.parametrize("manual",[False,True])
@pytest.mark.parametrize("timeframe,clock",[("5min","10:05:00.000001"),("15min","10:15:00.000001")])
def test_entry_queue_cannot_extend_native_observation_deadline(monkeypatch,manual,timeframe,clock):
    scanner,trade,_=queued(monkeypatch,manual=manual,timeframe=timeframe,clock=f"2026-10-29 {clock}")
    allowed,reason=lane.submission_authority(scanner,manual=manual,trade=trade)
    assert not allowed and "expired" in reason.lower()


@pytest.mark.parametrize("manual",[False,True])
@pytest.mark.parametrize("timeframe,clock",[("5min","10:05"),("15min","10:15")])
def test_entry_queue_keeps_exact_deadline_boundary_eligible(monkeypatch,manual,timeframe,clock):
    scanner,trade,_=queued(monkeypatch,manual=manual,timeframe=timeframe,clock=f"2026-10-29 {clock}")
    assert lane.submission_authority(scanner,manual=manual,trade=trade)==(True,"")


@pytest.mark.parametrize("entry_time",[None,"","invalid","NaT","2026-10-29 10:02"])
def test_entry_queue_rejects_unavailable_invalid_or_future_native_clock(monkeypatch,entry_time):
    scanner,trade,_=queued(monkeypatch,entry_time=entry_time)
    allowed,reason=lane.submission_authority(scanner,trade=trade)
    assert not allowed and reason


def test_delayed_alignment_uses_held_lifecycle_without_entry_age_expiry(monkeypatch):
    scanner,trade,_=queued(monkeypatch,clock="2026-10-29 11:00",adding=True)
    assert lane.submission_authority(scanner,trade=trade)==(True,"")


def test_expired_entry_authority_does_not_remove_held_exit_ownership(monkeypatch):
    scanner,trade,native=queued(monkeypatch,clock="2026-10-29 11:00")
    native.update(exit_time="2026-10-29 10:59",exit_reason="EMA RSI-50",exit_price=1000.)
    result=scanner.mtf._result_cache["NIFTY_5min"]
    assert lane.engine_exit_for(trade,result,now="2026-10-29 11:00")["reason"]=="EMA RSI-50"


def _paper_exit_during_queue_wait(scanner,trade,*,matching=True):
    """Use the production lifecycle helper and key, with a real terminal row."""
    from scanner import Scanner
    scanner._candidate_lifecycle_key=MethodType(Scanner._candidate_lifecycle_key,scanner)
    scanner._manual_lifecycle_time=Scanner._manual_lifecycle_time
    scanner._manual_signal_lifecycle_exited=MethodType(Scanner._manual_signal_lifecycle_exited,scanner)
    trade.lifecycle_id="queued-lifecycle"
    trade.exec_type="M"
    trade.entry_timestamp=market_stamp("2026-10-29 10:00:05").to_pydatetime()
    exited=NS(lifecycle_id="queued-lifecycle" if matching else "another-native-lifecycle",
        action="EXIT",exit_reason="UTEMA_PREMIUM_TARGET",
        exit_timestamp=market_stamp("2026-10-29 10:00:30").to_pydatetime())
    scanner._session_trade_candidates={"NIFTY":{"exit":exited}}
    scanner._latest_trade_candidates={}


def test_manual_entry_queue_rechecks_paper_exit_with_production_lifecycle_helper(monkeypatch):
    scanner,trade,native=queued(monkeypatch,manual=True)
    _paper_exit_during_queue_wait(scanner,trade)
    assert scanner._manual_signal_lifecycle_exited(trade)
    assert not native["exit_time"]  # The index engine has not exited this lifecycle.
    allowed,reason=lane.submission_authority(scanner,manual=True,trade=trade)
    assert not allowed and "lifecycle exited" in reason.lower()


def test_other_paper_lifecycle_exit_does_not_refuse_manual_entry(monkeypatch):
    scanner,trade,_=queued(monkeypatch,manual=True)
    _paper_exit_during_queue_wait(scanner,trade,matching=False)
    assert not scanner._manual_signal_lifecycle_exited(trade)
    assert lane.submission_authority(scanner,manual=True,trade=trade)==(True,"")


@pytest.mark.parametrize("adding",[False,True])
def test_virtual_paper_exit_does_not_override_auto_or_owned_alignment(monkeypatch,adding):
    scanner,trade,_=queued(monkeypatch,adding=adding)
    _paper_exit_during_queue_wait(scanner,trade)
    trade.broker_fill_confirmed=True
    trade.broker_quantity=150
    assert scanner._manual_signal_lifecycle_exited(trade)
    assert lane.submission_authority(scanner,manual=False,trade=trade)==(True,"")
