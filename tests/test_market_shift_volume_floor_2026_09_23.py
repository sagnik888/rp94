"""The market-shift override must look at measured participation, and no gate may invert.

The `confirmed_market_shift` route waives the regime/ADX/confirmation fence. Live
2026-09-09..09-18 (broker-filled rows only): 12 trades through it, 3 winners, -Rs.2,447,
mostly B+ at 62-68% in RANGING with ADX 12-22 — the exact shape that fence refuses. The
2026-09-18 09:17 SENSEX loser carried `volume_available=false` and intelligence 0.0. The
parallel RP50 build gates the same market-wide vote with a 1.05x volume floor; this ports
that floor to the route only.

The second half of this file is a direction audit. Every admission comparison in the policy
must be monotone: raising confidence, ADX or volume can turn a refusal into an admission,
never the reverse. An inverted `<`/`>` anywhere in the chain shows up here as a flip.
"""
import pytest

from config.settings import Settings, get_settings
from engine.quality_verdict import QualityInputs, evaluate_quality, policy_configuration
from engine.signal_quality_policy import confidence_to_grade


@pytest.fixture(autouse=True)
def _route_on(monkeypatch):
    """The floor lives inside the confirmed_market_shift route, which ships OFF since
    2026-09-23. These tests exercise the floor, so the route is switched on (monkeypatch
    restores the singleton)."""
    monkeypatch.setattr(get_settings(), "quality_policy_market_shift_route_enabled", True, raising=False)


def _inputs(**overrides):
    values = dict(
        timeframe="5min", inst_type="OPT", confidence=0.63, regime="RANGING",
        adx_value=14.9, direction="LONG",
        market_shift_confirmed=True, market_shift_core_votes=3, market_shift_total_votes=4,
        volume_available=True, volume_basis="TRADED", volume_ratio=1.40,
    )
    values.update(overrides)
    # The grade must track the confidence, or the grade-consistency route refuses and a
    # confidence sweep reads as an inversion that is not there.
    values.setdefault("grade", confidence_to_grade(values["confidence"]))
    return QualityInputs(**values)


# ── the floor ─────────────────────────────────────────────────────────────────


def test_the_route_still_admits_a_shift_backed_by_volume():
    verdict = evaluate_quality(_inputs(), get_settings())

    assert verdict.allowed and verdict.route == "confirmed_market_shift"


def test_unmeasured_volume_cannot_ride_the_override():
    """The live 09-18 09:17 shape: market-wide vote, B+ 63%, no volume measurement."""
    verdict = evaluate_quality(
        _inputs(volume_available=False, volume_basis="UNAVAILABLE", volume_ratio=0.0),
        get_settings(),
    )

    assert not verdict.allowed
    assert verdict.reason_code == "MARKET_SHIFT_VOLUME_UNCONFIRMED"
    assert "market_shift_volume" in verdict.checks["binding_legs"]


def test_a_tick_proxy_is_not_measured_volume():
    verdict = evaluate_quality(
        _inputs(volume_basis="INDEX_TICK_ACTIVITY", volume_ratio=3.0), get_settings()
    )

    assert not verdict.allowed and verdict.reason_code == "MARKET_SHIFT_VOLUME_UNCONFIRMED"


@pytest.mark.parametrize("ratio, allowed", [(0.43, False), (0.64, False), (1.04, False), (1.05, True), (2.0, True)])
def test_the_floor_binds_at_the_configured_ratio(ratio, allowed):
    verdict = evaluate_quality(_inputs(volume_ratio=ratio), get_settings())

    assert verdict.allowed is allowed
    if allowed:
        assert verdict.route == "confirmed_market_shift"


def test_the_floor_is_the_rp50_value():
    assert policy_configuration(get_settings())["market_shift_min_volume_ratio"] == 1.05


def test_the_floor_does_not_touch_ordinary_routes():
    """A setup that clears the normal fence is unaffected by the market-shift floor."""
    strong = _inputs(
        market_shift_confirmed=False, market_shift_core_votes=0, market_shift_total_votes=0,
        confidence=0.80, regime="TRENDING_UP", adx_value=30.0, trend_15m_agrees=True,
        impulse_confirmed=True, volume_ratio=0.40,
    )

    verdict = evaluate_quality(strong, get_settings())

    assert verdict.route != "market_shift_volume"


# ── the entry-timing revert ───────────────────────────────────────────────────


def test_ut_smart_entries_can_use_forming_candles():
    """UT Smart re-enabled stabilized forming-bar signals on 2026-09-26."""
    defaults = {name: Settings.model_fields[name].default for name in
                ('ut_signal_mode', 'live_intrabar_signal_entries')}
    settings = Settings(_env_file=None, **defaults)

    assert str(settings.ut_signal_mode).lower() == "realtime"
    assert bool(settings.live_intrabar_signal_entries) is True


# ── direction audit: no admission comparison may be inverted ──────────────────


def _allowed(confidence, adx, volume_ratio, **extra):
    return evaluate_quality(
        _inputs(confidence=confidence, adx_value=adx, volume_ratio=volume_ratio, **extra),
        get_settings(),
    ).allowed


@pytest.mark.parametrize("regime", ["RANGING", "TRENDING_UP", "VOLATILE", "UNKNOWN"])
@pytest.mark.parametrize("shift", [True, False])
def test_raising_confidence_never_turns_an_admission_into_a_refusal(regime, shift):
    seen_allowed = False
    for step in range(20):
        confidence = 0.30 + step * 0.035
        allowed = _allowed(
            confidence, 26.0, 1.40, regime=regime,
            market_shift_confirmed=shift,
            market_shift_core_votes=3 if shift else 0,
            market_shift_total_votes=4 if shift else 0,
            trend_15m_agrees=True, impulse_confirmed=True,
        )
        if allowed:
            seen_allowed = True
        elif seen_allowed:
            pytest.fail(f"confidence {confidence:.3f} refused after a lower one was admitted ({regime}, shift={shift})")


@pytest.mark.parametrize("regime", ["RANGING", "TRENDING_UP", "UNKNOWN"])
def test_raising_adx_never_turns_an_admission_into_a_refusal(regime):
    seen_allowed = False
    for step in range(20):
        adx = 5.0 + step * 2.5
        allowed = _allowed(0.86, adx, 1.40, regime=regime, trend_15m_agrees=True, impulse_confirmed=True)
        if allowed:
            seen_allowed = True
        elif seen_allowed:
            pytest.fail(f"ADX {adx:.1f} refused after a lower one was admitted ({regime})")


def test_raising_volume_never_turns_an_admission_into_a_refusal():
    seen_allowed = False
    for step in range(20):
        ratio = 0.10 * step
        allowed = _allowed(0.63, 14.9, ratio)
        if allowed:
            seen_allowed = True
        elif seen_allowed:
            pytest.fail(f"volume ratio {ratio:.2f}x refused after a lower one was admitted")


def test_grade_ranks_run_the_right_way():
    from engine.signal_quality_policy import grade_rank

    order = ["C", "B", "B+", "A", "A+"]
    ranks = [grade_rank(g) for g in order]

    assert ranks == sorted(ranks), f"grade ranking is not ascending: {dict(zip(order, ranks))}"


def test_confidence_maps_to_grades_in_the_right_direction():
    grades = [confidence_to_grade(c / 100.0) for c in range(10, 100, 5)]
    ranks = [__import__("engine.signal_quality_policy", fromlist=["grade_rank"]).grade_rank(g) for g in grades]

    assert ranks == sorted(ranks), f"higher confidence produced a lower grade: {list(zip(range(10, 100, 5), grades))}"
