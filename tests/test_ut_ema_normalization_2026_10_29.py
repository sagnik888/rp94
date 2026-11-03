"""Native persistence must not reapply the generic admission/exit policy."""
import copy
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from engine.candidate_normalization import normalize_loaded_exit_candidate


def row(**changes):
    values=dict(price=100.,stop=85.,target=137.5,rr=2.5,inst_type="OPT",direction="LONG",
        action="ENTRY",source_engine="ut_ema",source_lane="UTEMA",exit_policy_owner="UTEMA",
        utema_state={"premium_risk_pct":15.,"rr":2.5},initial_stop=85.,premium_stop=85.,premium_target=137.5,
        current_price=100.,pnl=0.,exit_price=0.,exit_reason="",reasons=[],
        entry_timestamp=datetime(2026,10,29,10),exit_timestamp=datetime(2026,10,29,10,3),
        exit_quote_verified=True)
    values.update(changes)
    return NS(**values)


def normalize(value,cap=5.,net=None):
    return normalize_loaded_exit_candidate(value,futures_sl_pct=.1,options_sl_pct=cap,
        candidate_exit_resolution=Mock(side_effect=AssertionError("generic exit interpretation")),
        candidate_net_pnl=net or (lambda c,px:(px-c.price)*75-80.))


@pytest.mark.parametrize("cap",[1.,5.,10.,15.,30.])
def test_repeated_save_load_never_changes_native_stop_target_or_frozen_state(cap):
    value=row()
    before=copy.deepcopy(vars(value))
    assert not normalize(value,cap)
    assert not normalize(value,cap)
    assert vars(value)==before


def test_global_rr_and_runner_flags_do_not_erase_native_target():
    value=row(rr=.1,runner_mode=True)
    assert not normalize(value)
    assert value.stop==85. and value.target==137.5


def test_protected_alignment_stop_remains_above_average_entry():
    value=row(price=80.,stop=85.,target=137.5)
    assert not normalize(value)
    assert (value.stop,value.target)==(85.,137.5)


@pytest.mark.parametrize("reason,observed",[("UTEMA_UT_STOP",83.),("STOP_HIT",83.),
    ("UTEMA_EMA_T2",139.),("TARGET_HIT",139.),("UTEMA_EMA_RSI-50",95.)])
def test_native_observed_exit_keeps_slippage_and_original_reason(reason,observed):
    value=row(action="EXIT",exit_reason=reason,exit_price=observed,current_price=100.,pnl=0.)
    assert normalize(value)
    assert value.current_price==value.exit_price==observed
    assert value.exit_reason==reason
    assert value.pnl==(observed-100.)*75-80.
    assert (value.stop,value.target,value.initial_stop)==(85.,137.5,85.)
    assert not normalize(value)


@pytest.mark.parametrize("changes",[{"exit_quote_verified":False},{"exit_quote_verified":None},
    {"exit_price":float("nan")},{"exit_price":0.},{"exit_timestamp":None},
    {"exit_timestamp":datetime(2026,10,29,9,59)}, {"entry_timestamp":None,"signal_timestamp":None}])
def test_missing_quote_or_chronology_never_invents_a_native_exit(changes):
    values=dict(action="EXIT",exit_reason="UTEMA_UT_STOP",exit_price=83.,current_price=100.,pnl=0.)
    values.update(changes)
    value=row(**values)
    net=Mock(side_effect=AssertionError("unproven mark used"))
    assert not normalize(value,net=net)
    assert value.current_price==100. and value.pnl==0.
    assert (value.stop,value.target)==(85.,137.5)
    net.assert_not_called()


@pytest.mark.parametrize("changes",[{"price":float("inf")},{"stop":float("nan")},
    {"target":0.},{"price":"invalid"}])
def test_invalid_geometry_is_not_repaired_into_new_native_authority(changes):
    value=row(**changes)
    net=Mock(side_effect=AssertionError("invalid geometry used"))
    assert not normalize(value,net=net)
    net.assert_not_called()


def test_quote_invalid_native_row_cannot_retain_fictional_pnl():
    value=row(action="EXIT",exit_reason="QUOTE_INVALID",exit_quote_verified=False,pnl=200.)
    assert normalize(value)
    assert value.pnl==0. and (value.stop,value.target)==(85.,137.5)


def test_explicit_generic_owner_retains_generic_cap_despite_source_tag():
    value=row(exit_policy_owner="GENERIC")
    assert normalize(value)
    assert value.stop==95. and value.target==112.5


def test_frozen_native_owner_survives_lost_source_tags():
    value=row(source_engine="",source_lane="")
    assert not normalize(value)
    assert value.stop==85. and value.target==137.5
