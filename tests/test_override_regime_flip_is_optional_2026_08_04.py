"""Two things found while asking why signals were rejected, and why one bad one was not.

1. AN IP BLOCK IS NOT AN ORDER PROBLEM. Kite refuses every order when the machine's
   outbound address is not whitelisted for the app. That was logged at ERROR beside
   ordinary per-order refusals, so on 2026-08-04 three dead signals (SENSEX, BANKNIFTY,
   MIDCPNIFTY, 12:36-12:38) read as three unrelated failures rather than one blanket
   block. Nothing about the symbol, size or price can change it.

2. THE WEAKEST OVERRIDE BRANCH IS NOW OPTIONAL. _entry_reversal_relief_context is a chain
   of ALTERNATIVES -- first match wins, no conjunction. "regime flip" asks only whether
   the regime trends the same way as the signal, with no price, volume or structure test,
   and produced 653 of 1327 re-admissions across every session log (49%) against 3 from
   the strict directional-thrust branch. Default stays OFF: override-admitted signals ran
   39% wins vs 50% for clean ones (median -151), but at N=56 that is not significant
   (p=0.21), so this is opt-in rather than a behaviour change.
"""
import pytest

from trading.kite_broker import KiteBroker

IP_REFUSAL = (
    "PermissionException: IP (2405:201:8000:d071:a0bf:806c:4b13:1b61) is not allowed "
    "to place orders for this app. Update allowed IPs on the Kite developer console."
)


# ── 1. the IP block escalates once, and again when the address changes ───────

def _broker():
    return KiteBroker.__new__(KiteBroker)


def test_an_ip_block_is_escalated_as_critical(caplog):
    broker = _broker()
    import loguru

    seen = []
    sink = loguru.logger.add(lambda m: seen.append(m), level="CRITICAL")
    try:
        broker._escalate_if_ip_blocked(IP_REFUSAL)
    finally:
        loguru.logger.remove(sink)

    assert seen, "an account-wide order block was not escalated above ERROR"
    text = "".join(str(m) for m in seen)
    assert "2405:201:8000:d071:a0bf:806c:4b13:1b61" in text, "the offending address must be named"
    assert "IPv4" in text and "IPv6" in text, "the address-family trap is the actual fix"


def test_it_is_not_repeated_for_every_refused_order():
    broker = _broker()
    import loguru

    seen = []
    sink = loguru.logger.add(lambda m: seen.append(m), level="CRITICAL")
    try:
        for _ in range(5):
            broker._escalate_if_ip_blocked(IP_REFUSAL)
    finally:
        loguru.logger.remove(sink)
    assert len(seen) == 1, "one blanket block must not produce one alert per order"


def test_a_changed_address_escalates_again():
    """The usual way this appears is the egress IP moving, so a flag would swallow it."""
    broker = _broker()
    import loguru

    seen = []
    sink = loguru.logger.add(lambda m: seen.append(m), level="CRITICAL")
    try:
        broker._escalate_if_ip_blocked(IP_REFUSAL)
        broker._escalate_if_ip_blocked(IP_REFUSAL.replace("2405:201:8000", "49.36.12"))
    finally:
        loguru.logger.remove(sink)
    assert len(seen) == 2


EMPTY_LIST_REFUSAL = (
    "PermissionException: No IPs configured for this app. Add allowed IPs on the Kite "
    "developer console."
)


def test_an_empty_allow_list_is_escalated_and_names_the_address_to_add():
    """An empty list DENIES every order rather than allowing all.

    That inversion is a trap: clearing the list looks like the right move for a dynamic
    connection and is the opposite. Kite's message names no address either, so the
    operator is told to add an IP without being told which one — the alert has to supply
    the machine's own address or it is not actionable.
    """
    import loguru

    import trading.kite_broker as kb

    broker = _broker()
    original = None
    try:
        import data.kite_auth as ka

        original = ka.current_egress_ip
        ka.current_egress_ip = lambda family="v4", timeout=4.0: "49.37.32.15"
    except Exception:
        pass

    seen = []
    sink = loguru.logger.add(lambda m: seen.append(m), level="CRITICAL")
    try:
        broker._escalate_if_ip_blocked(EMPTY_LIST_REFUSAL)
    finally:
        loguru.logger.remove(sink)
        if original is not None:
            import data.kite_auth as ka

            ka.current_egress_ip = original

    text = "".join(str(m) for m in seen)
    assert seen, "an empty allow-list blocks every order and was not escalated"
    assert "49.37.32.15" in text, "the alert must name the address to paste into the console"
    assert "DENIES" in text or "denies" in text, "the inversion must be stated explicitly"


@pytest.mark.parametrize("detail", [
    "InputException: Trigger price is out of range",
    "OrderException: Insufficient funds",
    "TokenException: Incorrect api_key or access_token",
])
def test_ordinary_refusals_are_not_escalated(detail):
    """Only the account-wide block earns a CRITICAL; per-order refusals must not."""
    broker = _broker()
    import loguru

    seen = []
    sink = loguru.logger.add(lambda m: seen.append(m), level="CRITICAL")
    try:
        broker._escalate_if_ip_blocked(detail)
    finally:
        loguru.logger.remove(sink)
    assert seen == []


# ── 2. the regime-flip branch is droppable ───────────────────────────────────

def _relief(scanner_obj, settings_obj, **kw):
    # scanner.py binds get_settings at import time, so patching config.settings has no
    # effect here — the module-level name is what the function actually calls.
    import scanner as scanner_mod
    from scanner import Scanner

    original = scanner_mod.get_settings
    scanner_mod.get_settings = lambda: settings_obj
    try:
        return Scanner._entry_reversal_relief_context(scanner_obj, **kw)
    finally:
        scanner_mod.get_settings = original


class _S:
    reversal_entry_relief_enabled = True
    reversal_entry_min_confidence = 0.20
    stabilization_override_drop_regime_flip = False


def _scanner():
    from scanner import Scanner

    s = Scanner.__new__(Scanner)
    s.latest_regimes = {"NIFTY": "TRENDING_UP"}
    # Every other branch must be unavailable so the result isolates regime_flip.
    s._candle_thrust_metrics = lambda *a, **k: None
    s._late_session_reclaim_context = lambda *a, **k: None
    s._one_way_cascade_context = lambda *a, **k: None
    s._market_wide_reversal_context = lambda *a, **k: None
    s._vwap_break_context = lambda *a, **k: None
    s._structure_break_context = lambda *a, **k: None
    return s


def _args():
    return dict(instrument="NIFTY", timeframe="5min", signal_type="BUY", confidence=0.89,
                regime="TRENDING_UP", signal_timestamp=None)


def test_regime_flip_still_re_admits_by_default():
    """Default must change nothing — this is opt-in, not a behaviour change."""
    ctx = _relief(_scanner(), _S(), **_args())
    assert ctx and ctx.get("type") == "regime_flip"


def test_the_setting_drops_the_regime_flip_branch():
    class _Off(_S):
        stabilization_override_drop_regime_flip = True

    assert _relief(_scanner(), _Off(), **_args()) is None, (
        "the branch that produced 49% of all re-admissions still fired"
    )


def test_dropping_it_leaves_the_stricter_branches_working():
    """Turning it on must not disable the whole override."""
    class _Off(_S):
        stabilization_override_drop_regime_flip = True

    scanner = _scanner()
    scanner._one_way_cascade_context = lambda *a, **k: {
        "type": "one_way_cascade", "reason": "cascade confirmed"
    }
    ctx = _relief(scanner, _Off(), **_args())
    assert ctx and ctx.get("type") == "one_way_cascade"
