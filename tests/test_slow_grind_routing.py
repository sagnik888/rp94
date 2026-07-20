"""AUTO instrument routing: slow-grind trends route to futures (no theta);
futures-blocked indices never receive an AUTO FUT decision."""

from config.settings import get_settings
from scanner import Scanner


def _auto_scanner():
    scanner = Scanner.__new__(Scanner)
    scanner.inst_pref = "AUTO"
    scanner.mode = "REAL"
    return scanner


def _route(scanner, **overrides):
    kwargs = dict(
        instrument="NIFTY",
        grade="B+",
        regime="TRENDING_UP",
        intel_score=0.30,
        signal_score=0.30,
        confidence=0.55,
        adx_value=22.0,
        atr_value=60.0,  # 0.25% of 24000 — compressed per-bar range
        price=24000.0,
        signal_time=None,
        iv_percentile=50.0,
        timeframe="5min",
    )
    kwargs.update(overrides)
    return scanner._resolve_instrument_type(**kwargs)


def test_slow_grind_trend_routes_to_futures():
    # Real trend (ADX 22) without thrust and with compressed ATR%: an option
    # bleeds theta faster than delta earns; the router must pick futures.
    assert _route(_auto_scanner()) == "FUT"


def test_slow_grind_routing_respects_toggle():
    settings = get_settings()
    previous = getattr(settings, "slow_grind_futures_routing_enabled", True)
    settings.slow_grind_futures_routing_enabled = False
    try:
        # With the feature off, the lean catch-all sends it to options.
        assert _route(_auto_scanner()) == "OPT"
    finally:
        settings.slow_grind_futures_routing_enabled = previous


def test_blocked_index_never_gets_auto_futures():
    # MIDCPNIFTY futures are rejected at the FUT stop gate downstream, so the
    # AUTO router must convert every FUT decision to options for it.
    assert _route(_auto_scanner(), instrument="MIDCPNIFTY") == "OPT"
    # High spot volatility is a hard FUT rule for other indices...
    assert _route(_auto_scanner(), atr_value=200.0) == "FUT"
    # ...but still must not produce FUT for a blocked index.
    assert _route(_auto_scanner(), instrument="MIDCPNIFTY", atr_value=200.0) == "OPT"


def test_strong_momentum_still_routes_to_options():
    # Thrusty A-grade trend keeps the existing option routing (gamma earns).
    assert (
        _route(
            _auto_scanner(),
            grade="A",
            confidence=0.90,
            signal_score=0.55,
            intel_score=0.70,
            adx_value=35.0,
        )
        == "OPT"
    )


def test_choppy_lean_routing_unchanged():
    # Choppy regimes keep defined-risk options under lean routing.
    assert _route(_auto_scanner(), regime="RANGING", adx_value=15.0) == "OPT"
