"""An account-level refusal must be reported as "nothing can trade", not "one order failed".

From 2026-07-31 14:51 AngelOne refused every order with

    {'success': False, 'errorCode': 'AG7002',
     'message': '49.37.34.211 is not a registered IP, please check your registered IP.'}

The system logged a generic per-order failure and kept taking signals until the close.
Nothing told the operator that the account itself was shut out, or which IP to register.
Note the offending IP is only ever visible in the broker's own message — the SDK's
X-ClientPublicIP header is a hardcoded constant (SmartApi/smartConnect.py sets it in a
`finally:` block, so it overwrites the real lookup unconditionally) and reported a
completely different address in the same log line.
"""
import threading

import pytest

from trading.trade_manager import TradeManager

AG7002 = (
    "[AG7002] 49.37.34.211 is not a registered IP, please check your registered IP."
)


def _manager():
    m = TradeManager.__new__(TradeManager)
    m.ui_logger = None
    m.alerts = []
    m.on_notification = lambda msg, kind="info": m.alerts.append(msg)
    m._lock = threading.RLock()
    m._log_ui = lambda *_a, **_kw: None
    return m


def test_unregistered_ip_names_the_ip_and_where_to_fix_it():
    m = _manager()
    m._alert_blocking_broker_condition(AG7002)

    assert len(m.alerts) == 1
    alert = m.alerts[0]
    assert "TRADING BLOCKED" in alert
    assert "49.37.34.211" in alert, "the operator cannot act without the actual IP"
    # Was "smartapi.angelone.in" until 2026-08-07. AngelOne is retired, so that sent the
    # operator to a console they no longer use at the exact moment every order is being
    # refused. The address family and empty-list points are carried over from the
    # Kite-side alert in kite_broker, because both have bitten this account.
    assert "developers.kite.trade" in alert
    assert "smartapi.angelone.in" not in alert
    assert "IPv6" in alert, "a whitelisted IPv4 does not cover an IPv6 egress"
    assert "EMPTY" in alert, "an empty allowed-IP list denies every order"


def test_repeat_failures_do_not_spam_the_operator():
    m = _manager()
    for _ in range(25):
        m._alert_blocking_broker_condition(AG7002)
    assert len(m.alerts) == 1


def test_expired_session_is_escalated_too():
    m = _manager()
    m._alert_blocking_broker_condition("[AB1010] Invalid session, please re-login")
    assert len(m.alerts) == 1
    assert "TRADING BLOCKED" in m.alerts[0]
    assert "re-login" in m.alerts[0].lower()


def test_an_ordinary_order_failure_is_not_escalated():
    """Only account-level conditions; a normal rejection must stay a normal rejection."""
    m = _manager()
    m._alert_blocking_broker_condition("RMS: order price is outside the day range")
    m._alert_blocking_broker_condition("Broker rejected order")
    m._alert_blocking_broker_condition("")
    assert m.alerts == []
