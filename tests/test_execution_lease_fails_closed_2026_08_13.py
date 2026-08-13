import pytest
from trading.kite_broker import KiteBroker

def _b(lease):
    b = KiteBroker.__new__(KiteBroker); b._execution_lease = lease; return b

def test_no_account_id_means_no_establishable_lease():
    assert _b(None).execution_lease_established() is False

def test_a_configured_account_establishes_one():
    assert _b(object()).execution_lease_established() is True

def test_the_permissive_accessors_are_unchanged_for_non_real_use():
    """Test fixtures build brokers without an account id; they must keep working."""
    assert _b(None).acquire_execution_lease() is True

def test_real_startup_requires_both():
    import inspect, main
    src = inspect.getsource(main)
    assert "execution_lease_established()" in src
    i = src.index("execution_lease_established()")
    assert "acquire_execution_lease()" in src[i:i+200]
