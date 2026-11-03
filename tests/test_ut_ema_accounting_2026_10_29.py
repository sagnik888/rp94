"""Observed native replay economics survive dashboard normalization."""
import copy
from types import SimpleNamespace
import pytest
from engine.trade_row_adapter import reconcile_legacy_trade_row


def row():
    values=dict(instrument="NIFTY",timeframe="5min",direction="SHORT",inst_type="OPT",
        entry_price=100.,exit_price=103.,current_price=103.,lots=3,lot_size=75,
        charges=124.5,pnl=550.5,status="CLOSED",grade="NATIVE (Hist)",
        trailing_stop=98.,target=105.,source_engine="ut_ema",source_lane="UTEMA",exit_policy_owner="UTEMA",
        data_provenance="BACKTEST_OBSERVED_OPTION_MINUTES",exit_reason="UTEMA_EMA_T2")
    proof={key:values[key] for key in ("entry_price","exit_price","lots","lot_size","charges","pnl")}
    proof.update(guard="replay_input",state="VERIFIED_PRICE_PATH",complete_path=True,input_sha256="a"*64,source_engine="ut_ema")
    values["replay_exit_trace"]=[proof]
    return values


def test_verified_native_replay_keeps_observed_fill_and_recorded_costs(monkeypatch):
    monkeypatch.setattr("engine.trade_row_adapter.get_settings",lambda:SimpleNamespace(opt_cost=80.,fut_cost=200.))
    result=reconcile_legacy_trade_row(row())
    assert result["charges"]==124.5 and result["pnl"]==550.5
    assert result["exit_price"]==103. and result["exit_reason"]=="UTEMA_EMA_T2"


@pytest.mark.parametrize("field,value",[("source_engine","utbot"),("exit_policy_owner","GENERIC")])
def test_native_tag_alone_cannot_claim_verified_replay(monkeypatch,field,value):
    monkeypatch.setattr("engine.trade_row_adapter.get_settings",lambda:SimpleNamespace(opt_cost=80.,fut_cost=200.))
    value_row=row();value_row[field]=value
    assert reconcile_legacy_trade_row(value_row)["pnl"]!=550.5


@pytest.mark.parametrize("changes",[{"input_sha256":"bad"},{"complete_path":False},{"exit_price":110.},
    {"charges":-1.},{"pnl":999.},{"lot_size":1}])
def test_mismatched_economics_or_incomplete_proof_cannot_freeze_pnl(monkeypatch,changes):
    monkeypatch.setattr("engine.trade_row_adapter.get_settings",lambda:SimpleNamespace(opt_cost=80.,fut_cost=200.))
    value=row();value["replay_exit_trace"][0].update(changes)
    assert reconcile_legacy_trade_row(value)["pnl"]!=550.5
