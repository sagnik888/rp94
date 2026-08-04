"""Orders must leave on an address that can actually be whitelisted.

2026-08-04: three signals died 12:36-12:38 with

    PermissionException: IP (2405:201:8000:d071:a0bf:806c:4b13:1b61) is not allowed
    to place orders for this app.

Measured on the live machine that day: public IPv4 49.37.32.15, public IPv6
2405:201:8000:d071:..., and socket.getaddrinfo("api.kite.trade") returns IPv6 FIRST — so
orders left on the IPv6 address while the operator had whitelisted their IPv4.

An IPv6 egress is normally a rotating privacy address, so whitelisting it is not a durable
fix; pinning Kite to IPv4 leaves exactly one address to manage. Reads are unaffected by
the allowed-IP list, which is why every health check passed while no order could be placed
— a read-only probe certifies nothing about placement.

The equivalent pre-flight existed for AngelOne (check_angelone_registered_ip) and was
dropped when that broker was retired; its orphaned comment still sits in config/settings.py
above the Fyers block. This is that guard, ported.
"""
import socket

import pytest

import data.kite_auth as kite_auth


@pytest.fixture(autouse=True)
def _restore_getaddrinfo():
    original = socket.getaddrinfo
    installed = kite_auth._IPV4_PIN_INSTALLED
    yield
    socket.getaddrinfo = original
    kite_auth._IPV4_PIN_INSTALLED = installed


def _install():
    kite_auth._IPV4_PIN_INSTALLED = False
    kite_auth.pin_kite_to_ipv4(True)


def test_kite_hostnames_resolve_ipv4_only():
    seen = {}

    def _fake(host, port, family=0, type=0, proto=0, flags=0):
        seen["family"] = family
        return [(socket.AF_INET, 0, 0, "", ("104.16.34.50", port))]

    kite_auth._ORIGINAL_GETADDRINFO = _fake
    _install()
    socket.getaddrinfo("api.kite.trade", 443)
    assert seen["family"] == socket.AF_INET, (
        "Kite still resolves to IPv6 first, so orders leave on an address that cannot "
        "be whitelisted durably"
    )


def test_other_hosts_are_left_alone():
    """Fyers, Yahoo and everything else must keep dual-stack resolution."""
    seen = {}

    def _fake(host, port, family=0, type=0, proto=0, flags=0):
        seen[host] = family
        return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", port))]

    kite_auth._ORIGINAL_GETADDRINFO = _fake
    _install()
    socket.getaddrinfo("api.fyers.in", 443)
    socket.getaddrinfo("query1.finance.yahoo.com", 443)
    assert seen["api.fyers.in"] == 0
    assert seen["query1.finance.yahoo.com"] == 0


def test_pinning_is_idempotent():
    """Re-running must not wrap the wrapper and recurse."""
    calls = []

    def _fake(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        return [(socket.AF_INET, 0, 0, "", ("104.16.34.50", port))]

    kite_auth._ORIGINAL_GETADDRINFO = _fake
    _install()
    kite_auth.pin_kite_to_ipv4(True)
    kite_auth.pin_kite_to_ipv4(True)
    socket.getaddrinfo("api.kite.trade", 443)
    assert len(calls) == 1


def test_the_pin_can_be_disabled():
    kite_auth._IPV4_PIN_INSTALLED = False
    before = socket.getaddrinfo
    kite_auth.pin_kite_to_ipv4(False)
    assert socket.getaddrinfo is before


# ── the readiness warning ────────────────────────────────────────────────────

def _readiness(registered, live_ip):
    import engine.safe_mode as sm

    class _S:
        kite_api_key = "k"
        kite_api_secret = "s"
        kite_registered_ip = registered
        entry_funds_warning_floor = 0.0

    original = kite_auth.current_egress_ip
    kite_auth.current_egress_ip = lambda family="v4", timeout=4.0: live_ip
    try:
        return sm.validate_real_mode_readiness(_S())
    finally:
        kite_auth.current_egress_ip = original


def test_a_drifted_ip_is_warned_before_a_signal_dies():
    result = _readiness("49.37.34.13", "49.37.32.15")
    assert any("49.37.32.15" in w and "49.37.34.13" in w for w in result["warnings"]), (
        "the whitelist was stale and the readiness gate said nothing"
    )


def test_a_matching_ip_is_silent():
    result = _readiness("49.37.32.15", "49.37.32.15")
    assert not any("whitelisted for the Kite app" in w for w in result["warnings"])


def test_a_blank_setting_skips_the_check():
    """Blank is correct when the console's IP list is empty — a dynamic connection."""
    result = _readiness("", "49.37.32.15")
    assert not any("whitelisted for the Kite app" in w for w in result["warnings"])


def test_an_unavailable_lookup_does_not_invent_a_warning():
    """No network is 'unknown', not 'drifted'."""
    result = _readiness("49.37.34.13", "")
    assert not any("whitelisted for the Kite app" in w for w in result["warnings"])
