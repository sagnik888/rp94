"""UTS extensions require the exchange's actual stop, before and after a write."""
from datetime import datetime
from types import SimpleNamespace as NS
import threading

import pytest

from trading.trade_manager import TradeManager


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr('config.settings.get_settings',
                        lambda: NS(uts_lane_enabled=True, options_sl_pct=15.))


def case(*, acknowledgement=True, exchange_applies=True):
    trade = NS(id='uts-exchange',source_lane='UTS',instrument='NIFTY',timeframe='5min',
        direction='LONG',inst_type='OPT',entry_price=100.,initial_stop=90.,
        current_stop=90.,trailing_stop=90.,current_price=92.,pending_exit=False,
        is_recovery=False,is_ghost=False,protective_order_id='stop-1',
        protective_trigger=90.,protective_quantity=65,trading_symbol='NIFTY-CE',
        symbol_token='fake',quality_verdict={'policy_version':'uts_native.v1',
                                            'checks':{'uts_premium_cap_pct':10.}})
    order = dict(status='TRIGGER PENDING',orderid='stop-1',tradingsymbol='NIFTY-CE',
                 transactiontype='SELL',quantity=65,triggerprice=90.,filledshares=0)
    manager = TradeManager.__new__(TradeManager)
    manager.mode,manager.product_type,manager.options_sl_pct='REAL','MIS',15.
    manager._lock=threading.RLock()
    manager._protection_lock=threading.RLock()
    manager.open_trades={trade.id:trade}
    manager._extension_budget_permitted=True
    manager.uts_stop_extension_guard=lambda *args: manager._extension_budget_permitted
    manager._transactable_quantity=lambda *args: 65
    manager._mark_dirty=lambda: None
    manager.nudge_position_sync=lambda: None
    manager._mark_recovery=lambda t,reason: setattr(t,'execution_status','RECOVERY_REQUIRED')
    writes=[]
    def modify(*args):
        writes.append(args)
        if exchange_applies:
            order['triggerprice']=args[5]
        return acknowledgement
    manager.broker=NS(get_order_status=lambda _:dict(order),modify_protective_stop=modify)
    manager.request_uts_stop_extension(trade,85.,quote_verified=True,now=datetime.now())
    return manager,trade,order,writes


@pytest.mark.parametrize('change', ['missing_trigger','tighter','profit_stop','looser',
    'nan_trigger','wrong_order','wrong_contract','wrong_side','wrong_quantity','partial_fill'])
def test_extension_refuses_unverified_or_different_exchange_stop(change):
    manager,trade,order,writes=case()
    if change=='missing_trigger': order.pop('triggerprice')
    if change=='tighter': order['triggerprice']=91.
    if change=='profit_stop': order['triggerprice']=110.
    if change=='looser': order['triggerprice']=89.
    if change=='nan_trigger': order['triggerprice']=float('nan')
    if change=='wrong_order': order['orderid']='another-order'
    if change=='wrong_contract': order['tradingsymbol']='BANKNIFTY-CE'
    if change=='wrong_side': order['transactiontype']='BUY'
    if change=='wrong_quantity': order['quantity']=130
    if change=='partial_fill': order['filledshares']=1
    assert not manager._apply_uts_stop_extension(trade,65)
    assert writes==[] and trade.current_stop==90.
    assert not trade.quality_verdict['checks'].get('uts_stop_extension_attempted')


def test_another_trade_owning_the_stop_blocks_extension():
    manager,trade,_,writes=case()
    other=NS(id='other-trade',protective_order_id='stop-1')
    manager.open_trades[other.id]=other
    assert not manager._apply_uts_stop_extension(trade,65)
    assert writes==[] and trade.current_stop==90.


def test_native_kite_fields_confirm_exchange_extension():
    manager,trade,order,writes=case()
    order['trigger_price']=order.pop('triggerprice')
    order['order_id']=order.pop('orderid')
    order['transaction_type']=order.pop('transactiontype')
    def modify(*args):
        writes.append(args)
        order['trigger_price']=args[5]
        return True
    manager.broker.modify_protective_stop=modify
    assert manager._apply_uts_stop_extension(trade,65)
    assert trade.current_stop==trade.protective_trigger==85.
    assert trade.initial_stop==90.
    assert trade.quality_verdict['checks']['uts_stop_extended']


def test_http_acknowledgement_without_exchange_change_does_not_widen_local_stop():
    manager,trade,_,writes=case(exchange_applies=False)
    assert not manager._apply_uts_stop_extension(trade,65)
    assert trade.current_stop==90. and trade.initial_stop==90.
    assert trade.execution_status=='RECOVERY_REQUIRED'
    assert trade.protective_trigger==85.  # Conservative bound, pending restoration.
    assert trade.quality_verdict['checks']['uts_stop_extension_attempted']
    assert len(writes)==1
    assert not manager.request_uts_stop_extension(trade,85.,quote_verified=True,now=datetime.now())


@pytest.mark.parametrize('failure',['read_error','unknown_after','false_ack','raised_write'])
def test_uncertain_write_retains_local_stop_and_restores_possible_broker_widening(failure):
    manager,trade,order,writes=case(acknowledgement=failure!='false_ack')
    original_read=manager.broker.get_order_status
    reads=[]
    def read(oid):
        reads.append(oid)
        if len(reads)>1 and failure=='read_error': raise RuntimeError('fake outage')
        if len(reads)>1 and failure=='unknown_after': return None
        return original_read(oid)
    manager.broker.get_order_status=read
    if failure=='raised_write':
        def ambiguous(*args):
            writes.append(args)
            order['triggerprice']=args[5]
            if args[5]==85.: raise RuntimeError('fake timeout after write')
            return True
        manager.broker.modify_protective_stop=ambiguous
    assert not manager._apply_uts_stop_extension(trade,65)
    assert trade.current_stop==90. and trade.protective_trigger==85.
    assert trade.quality_verdict['checks']['uts_stop_extension_attempted']
    # Normal protection knows the broker might now carry85, so it restores90.
    manager.broker.get_order_status=original_read
    def restore(*args):
        writes.append(args)
        order['triggerprice']=args[5]
        return True
    manager.broker.modify_protective_stop=restore
    assert manager.ensure_broker_protection(trade,65)
    assert writes[-1][5]==90. and trade.protective_trigger==90.
    assert not trade.quality_verdict['checks'].get('uts_stop_extension_uncertain')


@pytest.mark.parametrize('race',['profit_lock','stop_touch','pending_exit','unverified','budget','quantity','expired'])
def test_permission_changes_during_broker_write_cannot_commit_wider_stop(race):
    manager,trade,order,writes=case()
    request=trade._uts_extension_request
    original_modify=manager.broker.modify_protective_stop
    def modify(*args):
        result=original_modify(*args)
        if race=='profit_lock': trade.current_stop=114.
        if race=='stop_touch': trade._uts_stop_touch_pending=True
        if race=='pending_exit': trade.pending_exit=True
        if race=='unverified': trade.quote_verified=False
        if race=='budget': manager._extension_budget_permitted=False
        if race=='quantity': manager._transactable_quantity=lambda *args:30
        if race=='expired': request['at']-=3
        return result
    manager.broker.modify_protective_stop=modify
    assert not manager._apply_uts_stop_extension(trade,65)
    assert trade.current_stop==(114. if race=='profit_lock' else 90.)
    assert trade.protective_trigger==85. and trade.execution_status=='RECOVERY_REQUIRED'
    assert trade.quality_verdict['checks']['uts_stop_extension_attempted']
    assert not trade.quality_verdict['checks'].get('uts_stop_extended')


def test_possible_exchange_widening_reserves_full_risk_during_recovery():
    from engine.risk_manager import RiskManager
    manager,trade,_,_=case(acknowledgement=False)
    assert not manager._apply_uts_stop_extension(trade,65)
    trade.lots,trade.lot_size,trade.instrument_multiplier=1,65,.5
    risk=RiskManager.__new__(RiskManager)
    risk.options_sl_pct,risk.futures_sl_pct,risk.max_trade_loss_abs=10.,.5,900.
    # Local90 is retained, but the broker may already hold85. A new configured10%
    # or rupee cap cannot erase the975 rupees of actual possible stop exposure.
    assert risk._projected_stop_risk(trade)==975.
    trade.protective_trigger=90.
    assert risk._projected_stop_risk(trade)==650.
    trade.protective_trigger=float('nan')
    assert risk._projected_stop_risk(trade) is None


def test_uncertainty_risk_exception_does_not_change_other_scanners():
    from engine.risk_manager import RiskManager
    manager,trade,_,_=case(acknowledgement=False)
    assert not manager._apply_uts_stop_extension(trade,65)
    trade.lots,trade.lot_size,trade.instrument_multiplier=1,65,.5
    risk=RiskManager.__new__(RiskManager)
    risk.options_sl_pct,risk.futures_sl_pct,risk.max_trade_loss_abs=10.,.5,900.
    trade.source_lane='S1'
    assert risk._projected_stop_risk(trade)==650.


@pytest.mark.parametrize('failure',['stale_ack','read_error','wrong_quantity','triggered'])
def test_uncertain_restoration_requires_actual_exchange_stop_before_releasing_risk(failure):
    from engine.risk_manager import RiskManager
    manager,trade,order,writes=case(acknowledgement=False)
    assert not manager._apply_uts_stop_extension(trade,65)
    trade.lots,trade.lot_size,trade.instrument_multiplier=1,65,.5
    risk=RiskManager.__new__(RiskManager)
    risk.options_sl_pct,risk.futures_sl_pct,risk.max_trade_loss_abs=15.,.5,0.
    original_read=manager.broker.get_order_status
    def unconfirmed_restore(*args):
        writes.append(args)
        if failure!='stale_ack': order['triggerprice']=args[5]
        if failure=='wrong_quantity': order['quantity']=130
        if failure=='triggered': order['status']='OPEN'
        if failure=='read_error':
            def unavailable(_): raise RuntimeError('fake read outage')
            manager.broker.get_order_status=unavailable
        return True
    manager.broker.modify_protective_stop=unconfirmed_restore
    assert not manager.ensure_broker_protection(trade,65)
    assert trade.current_stop==90. and trade.protective_trigger==85.
    assert trade.quality_verdict['checks']['uts_stop_extension_uncertain']
    assert risk._projected_stop_risk(trade)==975.
    # A subsequent fresh, complete confirmation can release the extra reservation.
    manager._last_protective_sync.clear()
    manager._last_protective_status_poll.clear()
    order.update(quantity=65,status='TRIGGER PENDING')
    manager.broker.get_order_status=original_read
    def confirmed_restore(*args):
        writes.append(args)
        order['triggerprice']=args[5]
        return True
    manager.broker.modify_protective_stop=confirmed_restore
    assert manager.ensure_broker_protection(trade,65)
    assert trade.protective_trigger==90.
    assert not trade.quality_verdict['checks'].get('uts_stop_extension_uncertain')
    assert risk._projected_stop_risk(trade)==650.
