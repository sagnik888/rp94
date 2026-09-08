"""Regression coverage for the immutable 1.5R fresh-entry boundary."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import config.settings as settings_module
import scanner as scanner_module
from engine.entry_policy import (
    MODEL_SOURCE,
    MODEL_VERSION,
    SUPPORTED_ENTRY_INSTRUMENTS,
    ExecutionGeometry,
    ExpectedMoveForecast,
    build_directional_mfe_forecast,
    effective_expected_move_floor_pct,
    effective_underlying_stop_cap_pct,
    evaluate_final_entry_policy,
)
from engine.signal_processor import TradeCandidate
from intelligence.rtracker import RegimeTracker
from scanner import Scanner
from trading.trade_manager import IST, Trade, TradeManager


def _forecast(now: datetime, **changes) -> ExpectedMoveForecast:
    values = dict(
        instrument="NIFTY",
        direction="LONG",
        timeframe="5min",
        horizon_minutes=30,
        horizon_bars=6,
        expected_favorable_move_pct=0.25,
        q10_pct=0.12,
        q25_pct=0.25,
        q50_pct=0.40,
        uncertainty_pct=0.22,
        probability_meeting_floor=0.72,
        floor_pct=0.20,
        sample_count=120,
        generated_at=now.isoformat(),
        data_as_of=(now - timedelta(seconds=30)).isoformat(),
        model_version=MODEL_VERSION,
        source=MODEL_SOURCE,
        available=True,
        reason="",
    )
    values.update(changes)
    return ExpectedMoveForecast(**values)


def _geometry(now: datetime, **changes) -> ExecutionGeometry:
    values = dict(
        instrument="NIFTY",
        direction="LONG",
        inst_type="OPT",
        underlying_entry=25_000.0,
        underlying_stop=24_975.0,
        underlying_target=25_050.0,
        premium_entry=100.0,
        premium_stop=96.0,
        premium_target=106.0,
        contract_entry=100.0,
        contract_stop=96.0,
        contract_target=106.0,
        observed_at=now.isoformat(),
        source="signal_processor.dual_geometry|auto_pre_submit",
        verified=True,
        conversion_ratio=0.5,
    )
    values.update(changes)
    return ExecutionGeometry(**values)


def _verdict(geometry, forecast, now, *, fresh_exposure=True):
    return evaluate_final_entry_policy(
        geometry,
        forecast,
        min_rr=1.5,
        expected_move_floor_pct=0.20,
        max_underlying_stop_pct=0.15,
        max_forecast_age_seconds=90.0,
        max_geometry_age_seconds=10.0,
        checked_at=now,
        phase="test",
        supported_instruments=SUPPORTED_ENTRY_INSTRUMENTS,
        fresh_exposure=fresh_exposure,
    )


def test_exactly_one_point_five_r_is_allowed_and_metadata_rr_is_irrelevant():
    now = datetime(2026, 8, 27, 10, 0)
    verdict = _verdict(_geometry(now), _forecast(now), now)
    assert verdict.allowed is True
    assert verdict.actual_rr == pytest.approx(1.5)


def test_one_point_four_nine_nine_r_is_rejected():
    now = datetime(2026, 8, 27, 10, 0)
    geometry = _geometry(now, premium_target=105.996, contract_target=105.996)
    verdict = _verdict(geometry, _forecast(now), now)
    assert verdict.allowed is False
    assert dict(verdict.checks)["execution_rr"] is False


def test_underlying_stop_cap_is_strict_at_the_fourth_decimal_place():
    now = datetime(2026, 8, 27, 10, 0)
    exact = _geometry(now, underlying_stop=25_000.0 * (1.0 - 0.0015))
    over = _geometry(now, underlying_stop=25_000.0 * (1.0 - 0.0015004))

    exact_verdict = _verdict(exact, _forecast(now), now)
    over_verdict = _verdict(over, _forecast(now), now)

    assert exact_verdict.allowed is True
    assert dict(exact_verdict.checks)["underlying_stop_cap"] is True
    assert over_verdict.allowed is False
    assert dict(over_verdict.checks)["underlying_stop_cap"] is False


def test_option_cross_geometry_requires_a_bounded_conversion_ratio():
    now = datetime(2026, 8, 27, 10, 0)
    verdict = _verdict(_geometry(now, conversion_ratio=0.0), _forecast(now), now)
    assert verdict.allowed is False
    assert dict(verdict.checks)["option_cross_geometry"] is False


def test_option_cross_geometry_rejects_premium_units_that_are_too_large_for_delta():
    now = datetime(2026, 8, 27, 10, 0)
    # A 25-point underlying risk with a 0.5 delta cannot justify a 99-point
    # premium risk.  This catches premium values accidentally written into
    # the underlying/spot geometry.
    verdict = _verdict(
        _geometry(
            now,
            premium_stop=1.0,
            contract_stop=1.0,
            conversion_ratio=0.5,
        ),
        _forecast(now),
        now,
    )
    assert verdict.allowed is False
    assert dict(verdict.checks)["option_cross_geometry"] is False


def test_hard_floor_and_stop_bounds_cannot_be_weakened_by_configuration():
    """The PROPERTY is the point and it is unchanged: configuration can only tighten.
    The numbers moved 2026-08-30 because the originals were unreachable -- see
    tests/test_entry_floor_and_major_win_are_reachable_2026_08_30.py."""
    assert effective_expected_move_floor_pct("NIFTY", 0.001) == pytest.approx(0.023)
    assert effective_expected_move_floor_pct("MIDCPNIFTY", 0.001) == pytest.approx(0.031)
    # and a stricter configured value still wins over the hard floor
    assert effective_expected_move_floor_pct("NIFTY", 0.40) == pytest.approx(0.40)
    assert effective_underlying_stop_cap_pct("NIFTY", 1.0) == pytest.approx(0.15)
    assert effective_underlying_stop_cap_pct("MIDCPNIFTY", 1.0) == pytest.approx(0.15)


@pytest.mark.parametrize(
    "lane",
    [
        "ordinary",
        "volume_spike",
        "shock",
        "reversion",
        "cascade",
        "late_session_reclaim",
        "missed_entry",
        "weak_market",
        "15min_choppy",
        "raw_union",
        "quad_union",
        "instrument_conversion",
        "manual_execution",
    ],
)
def test_no_fresh_entry_lane_can_bypass_the_one_point_five_r_floor(lane):
    now = datetime(2026, 8, 27, 10, 0)
    geometry = _geometry(now, premium_target=105.996, contract_target=105.996)

    verdict = evaluate_final_entry_policy(
        geometry,
        _forecast(now),
        min_rr=1.5,
        expected_move_floor_pct=0.20,
        max_underlying_stop_pct=0.15,
        max_forecast_age_seconds=90.0,
        max_geometry_age_seconds=10.0,
        checked_at=now,
        phase="post_lane_mutation",
        lane=lane,
        supported_instruments=SUPPORTED_ENTRY_INSTRUMENTS,
    )

    assert verdict.lane == lane
    assert verdict.allowed is False
    assert dict(verdict.checks)["execution_rr"] is False


def test_configuration_cannot_lower_the_immutable_one_point_five_r_floor():
    now = datetime(2026, 8, 27, 10, 0)
    geometry = _geometry(
        now,
        premium_target=105.6,
        contract_target=105.6,
    )
    verdict = evaluate_final_entry_policy(
        geometry,
        _forecast(now),
        min_rr=1.0,
        expected_move_floor_pct=0.20,
        max_underlying_stop_pct=0.15,
        max_forecast_age_seconds=90.0,
        max_geometry_age_seconds=10.0,
        checked_at=now,
        phase="test",
        supported_instruments=SUPPORTED_ENTRY_INSTRUMENTS,
    )
    assert verdict.actual_rr == pytest.approx(1.4)
    assert verdict.min_rr == pytest.approx(1.5)
    assert verdict.allowed is False
    assert settings_module.Settings(_env_file=None, entry_policy_min_rr=1.0).entry_policy_min_rr == 1.5


def test_policy_without_an_explicit_supported_universe_fails_closed():
    now = datetime(2026, 8, 27, 10, 0)
    verdict = evaluate_final_entry_policy(
        _geometry(now),
        _forecast(now),
        min_rr=1.5,
        expected_move_floor_pct=0.20,
        max_underlying_stop_pct=0.15,
        max_forecast_age_seconds=90.0,
        max_geometry_age_seconds=10.0,
        checked_at=now,
        phase="test",
    )
    assert verdict.allowed is False
    assert dict(verdict.checks)["supported_instrument"] is False


def test_malformed_geometry_and_policy_limits_reject_instead_of_crashing():
    now = datetime(2026, 8, 27, 10, 0)
    geometry = _geometry(
        now,
        underlying_entry="not-a-number",
        contract_entry="not-a-number",
    )
    verdict = evaluate_final_entry_policy(
        geometry,
        _forecast(now),
        min_rr=float("nan"),
        expected_move_floor_pct=float("nan"),
        max_underlying_stop_pct=float("nan"),
        max_forecast_age_seconds=float("nan"),
        max_geometry_age_seconds=float("nan"),
        checked_at=now,
        phase="test",
        supported_instruments=SUPPORTED_ENTRY_INSTRUMENTS,
    )
    assert verdict.allowed is False
    assert dict(verdict.checks)["policy_parameters"] is False
    assert dict(verdict.checks)["finite_verified_geometry"] is False


@pytest.mark.parametrize(
    ("geometry", "forecast", "failed_check"),
    [
        (
            lambda now: _geometry(now, premium_stop=95.0),
            lambda now: _forecast(now),
            "premium_contract_consistency",
        ),
        (
            lambda now: _geometry(now, source="external.payload|auto_pre_submit"),
            lambda now: _forecast(now),
            "geometry_source",
        ),
        (
            lambda now: _geometry(now, schema="legacy.mixed_geometry.v0"),
            lambda now: _forecast(now),
            "finite_verified_geometry",
        ),
        (
            lambda now: _geometry(now, direction="SIDEWAYS"),
            lambda now: _forecast(now, direction="SIDEWAYS"),
            "execution_identity",
        ),
        (
            lambda now: _geometry(now, inst_type="CFD"),
            lambda now: _forecast(now),
            "execution_identity",
        ),
        (
            lambda now: _geometry(now - timedelta(seconds=11)),
            lambda now: _forecast(now),
            "geometry_fresh",
        ),
        (
            lambda now: _geometry(now),
            lambda now: _forecast(now, uncertainty_pct=float("nan")),
            "forecast_evidence",
        ),
        (
            lambda now: _geometry(now),
            lambda now: _forecast(now, generated_at=(now + timedelta(seconds=5)).isoformat()),
            "forecast_fresh",
        ),
        (
            lambda now: _geometry(now),
            lambda now: _forecast(
                now,
                timeframe="15min",
                data_as_of=(now - timedelta(minutes=4)).isoformat(),
            ),
            "forecast_fresh",
        ),
    ],
)
def test_missing_stale_inconsistent_or_nonfinite_evidence_fails_closed(
    geometry, forecast, failed_check
):
    now = datetime(2026, 8, 27, 10, 0)
    verdict = _verdict(geometry(now), forecast(now), now)
    assert verdict.allowed is False
    assert dict(verdict.checks)[failed_check] is False


def test_real_forecast_requires_a_transport_observation_clock():
    now = datetime(2026, 8, 27, 12, 0)
    idx = pd.date_range(now - timedelta(minutes=500), periods=100, freq="5min")
    frame = pd.DataFrame(
        {
            "close": [100.0] * len(idx),
            "high": [100.3] * len(idx),
            "low": [99.7] * len(idx),
        },
        index=idx,
    )
    forecast = build_directional_mfe_forecast(
        frame,
        instrument="NIFTY",
        direction="LONG",
        timeframe="5min",
        horizon_minutes=15,
        floor_pct=0.15,
        as_of=now,
        data_observed_at=None,
        min_samples=5,
        require_observation_clock=True,
    )
    assert forecast.available is False
    assert "observation clock is required" in forecast.reason


def test_kite_history_forecast_fails_closed_when_its_rest_health_is_stale(
    monkeypatch,
):
    now = datetime(2026, 8, 27, 12, 0)
    frame = pd.DataFrame(
        {
            "close": [100.0],
            "high": [100.3],
            "low": [99.7],
            "source": ["kite_history"],
        },
        index=pd.DatetimeIndex([now - timedelta(minutes=5)]),
    )
    captured = {}

    def fake_forecast(*args, **kwargs):
        captured.update(kwargs)
        return _forecast(now)

    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.candles = SimpleNamespace(
        get_candles=lambda instrument, timeframe: frame,
        latest_candle_receipt=lambda instrument, timeframe: now,
    )
    scanner.data = SimpleNamespace(
        kite=SimpleNamespace(
            historical_rest_status=lambda: {"kite_historical_rest_fresh": False}
        )
    )
    monkeypatch.setattr(scanner_module, "build_directional_mfe_forecast", fake_forecast)
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            entry_policy_forecast_horizon_minutes_by_timeframe={"5min": 15},
            entry_policy_forecast_quantile=0.25,
            entry_policy_forecast_min_samples=5,
            entry_policy_forecast_max_samples=100,
            entry_policy_forecast_max_age_seconds=90.0,
        ),
    )

    scanner._expected_move_forecast_for_entry(
        SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG"),
        checked_at=now,
        floor_pct=0.15,
    )
    assert captured["require_observation_clock"] is True
    assert captured["data_observed_at"] is None


def test_finnifty_is_outside_the_entry_universe_even_if_geometry_is_good():
    now = datetime(2026, 8, 27, 10, 0)
    verdict = _verdict(
        _geometry(now, instrument="FINNIFTY"),
        _forecast(now, instrument="FINNIFTY"),
        now,
    )
    assert verdict.allowed is False
    assert dict(verdict.checks)["supported_instrument"] is False


def test_explicit_active_universe_is_not_replaced_by_capability_defaults():
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["SENSEX"]
    assert scanner._fresh_entry_universe() == ("SENSEX",)
    scanner.active_indices = []
    assert scanner._fresh_entry_universe() == ()


def test_cascade_add_refuses_an_index_removed_after_signal_creation():
    scanner = Scanner.__new__(Scanner)
    scanner.active_indices = ["SENSEX"]
    scanner.trades = SimpleNamespace(open_trades={})
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
    )
    allowed, reason = scanner._offer_cascade_add(candidate)
    assert allowed is False
    assert "active entry universe" in reason


def test_settings_strip_finnifty_from_an_environment_override():
    settings = settings_module.Settings(
        _env_file=None,
        active_indices=["NIFTY", "FINNIFTY", "SENSEX", "NIFTY"],
    )
    assert settings.active_indices == ["NIFTY", "SENSEX"]


def test_recovery_of_existing_broker_exposure_is_not_a_fresh_entry():
    now = datetime(2026, 8, 27, 10, 0)
    missing = _geometry(
        now,
        underlying_entry=0.0,
        underlying_stop=0.0,
        underlying_target=0.0,
        premium_entry=0.0,
        premium_stop=0.0,
        premium_target=0.0,
        contract_entry=0.0,
        contract_stop=0.0,
        contract_target=0.0,
        verified=False,
        source="",
    )
    unavailable = _forecast(now, available=False, reason="not needed for recovery")
    verdict = _verdict(missing, unavailable, now, fresh_exposure=False)
    assert verdict.allowed is True
    assert verdict.lane == "recovery"


def test_recovery_identity_survives_trade_serialization_and_restart():
    trade = Trade(
        id="T-recovery",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(IST),
        trailing_stop=96.0,
        current_stop=96.0,
        lots=1,
        lot_size=65,
        grade="A",
        is_recovery=True,
    )

    restored = Trade.from_dict(trade.to_dict())

    assert restored.is_recovery is True


@pytest.mark.parametrize(
    ("phase", "expected_fresh"),
    [
        ("actual_broker_fill", False),
        ("broker_queue", False),
        ("scale_in_pre_submit", True),
        ("scale_in_actual_fill", True),
    ],
)
def test_recovered_trade_exemption_never_leaks_into_a_later_scale_in(
    phase, expected_fresh
):
    scanner = Scanner.__new__(Scanner)
    seen = {}

    def verdict(holder, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(allowed=True, reason="")

    scanner._final_entry_policy_verdict = verdict
    trade = SimpleNamespace(is_recovery=True)

    allowed, reason = scanner._entry_policy_allows(trade, phase=phase)

    assert allowed is True, reason
    assert seen["fresh_exposure"] is expected_fresh


def _trend_frame(start: str, periods: int, *, down: bool = False) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="1min")
    step = -0.10 if down else 0.10
    close = 100.0 + np.arange(periods) * step
    return pd.DataFrame(
        {
            "open": close - (step / 2.0),
            "high": close + 0.50,
            "low": close - 0.50,
            "close": close,
        },
        index=index,
    )


def test_directional_mfe_forecast_has_an_explicit_horizon_version_and_uncertainty():
    frame = _trend_frame("2026-08-27 09:15", 100)
    as_of = frame.index[-1].to_pydatetime() + timedelta(minutes=1)
    forecast = build_directional_mfe_forecast(
        frame,
        instrument="NIFTY",
        direction="LONG",
        timeframe="1min",
        horizon_minutes=15,
        floor_pct=0.20,
        as_of=as_of,
        min_samples=30,
    )
    assert forecast.available is True
    assert forecast.horizon_bars == 15
    assert forecast.model_version == MODEL_VERSION
    assert forecast.source == MODEL_SOURCE
    assert forecast.expected_favorable_move_pct == forecast.q25_pct
    assert forecast.uncertainty_pct >= 0.0


def test_shipped_expected_move_floors_are_reachable_values():
    """These were 0.15 / 0.15 / 0.15 / 0.25 and were called "permissive". They were not:
    compared against the 25th percentile of the forward move, every one of them sat
    ABOVE the largest rolling q25 the stored history has ever produced, so the gate could
    not pass once on any index. It went live on 2026-08-28 and the book took zero trades
    that session against 5 / 10 / 17 / 14 on the four before it.

    Recalibrated to the 10th percentile of each index's own rolling q25 on the binding
    5-minute series. Reachability is asserted separately, against the observed maxima, in
    tests/test_entry_floor_and_major_win_are_reachable_2026_08_30.py.
    """
    settings = settings_module.Settings(_env_file=None)
    assert settings.entry_policy_expected_move_floor_pct_by_index == {
        "NIFTY": 0.023,
        "BANKNIFTY": 0.029,
        "SENSEX": 0.022,
        "MIDCPNIFTY": 0.031,
    }


def test_fresh_quote_clock_cannot_make_an_old_candle_frame_fresh():
    frame = _trend_frame("2026-08-27 09:15", 100)
    as_of = datetime(2026, 8, 27, 12, 0)
    forecast = build_directional_mfe_forecast(
        frame,
        instrument="NIFTY",
        direction="LONG",
        timeframe="1min",
        horizon_minutes=15,
        floor_pct=0.15,
        as_of=as_of,
        data_observed_at=as_of,
        min_samples=1,
        max_source_age_seconds=90.0,
    )
    assert forecast.available is False
    assert "stale" in forecast.reason


def test_forecast_does_not_treat_an_overnight_gap_as_a_fifteen_minute_move():
    frame = pd.concat(
        [
            _trend_frame("2026-08-26 15:00", 20),
            _trend_frame("2026-08-27 09:15", 20) * 1.10,
        ]
    )
    as_of = frame.index[-1].to_pydatetime() + timedelta(minutes=1)
    forecast = build_directional_mfe_forecast(
        frame,
        instrument="NIFTY",
        direction="LONG",
        timeframe="1min",
        horizon_minutes=15,
        floor_pct=0.20,
        as_of=as_of,
        min_samples=1,
    )
    assert forecast.sample_count == 10


def test_unfinished_current_bar_never_labels_a_historical_forecast_sample():
    frame = _trend_frame("2026-08-27 09:15", 50)
    live = build_directional_mfe_forecast(
        frame,
        instrument="NIFTY",
        direction="LONG",
        timeframe="1min",
        horizon_minutes=15,
        floor_pct=0.20,
        as_of=frame.index[-1].to_pydatetime() + timedelta(seconds=30),
        min_samples=1,
    )
    closed = build_directional_mfe_forecast(
        frame,
        instrument="NIFTY",
        direction="LONG",
        timeframe="1min",
        horizon_minutes=15,
        floor_pct=0.20,
        as_of=frame.index[-1].to_pydatetime() + timedelta(minutes=1),
        min_samples=1,
    )
    assert live.sample_count == closed.sample_count - 1


@pytest.mark.parametrize(
    "changes",
    [
        {"horizon_minutes": 0},
        {"horizon_minutes": "bad"},
        {"quantile": float("nan")},
        {"quantile": 0.9},
        {"min_samples": 0},
        {"max_samples": 0},
    ],
)
def test_invalid_forecast_configuration_fails_closed_without_throwing(changes):
    frame = _trend_frame("2026-08-27 09:15", 50)
    arguments = dict(
        instrument="NIFTY",
        direction="LONG",
        timeframe="1min",
        horizon_minutes=15,
        floor_pct=0.20,
        as_of=frame.index[-1].to_pydatetime() + timedelta(minutes=1),
        quantile=0.25,
        min_samples=1,
        max_samples=480,
    )
    arguments.update(changes)

    forecast = build_directional_mfe_forecast(frame, **arguments)

    assert forecast.available is False
    assert "configuration" in forecast.reason


def test_option_repricing_preserves_one_point_five_r_at_the_live_ask():
    scanner = Scanner.__new__(Scanner)
    scanner._get_live_premium_cached = lambda candidate: 120.0
    scanner._get_live_fut_ltp_cached = lambda candidate: 0.0
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    scanner._configured_option_sl_pct = lambda: 15.0
    scanner.expiry = SimpleNamespace(in_gamma_window=lambda *args, **kwargs: False)
    scanner._session_squareoff_clock = lambda: None
    candidate = SimpleNamespace(
        inst_type="OPT",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        trading_symbol="NIFTY25000CE",
        price=100.0,
        stop=96.0,
        target=106.0,
        multiplier=0.5,
        rr=1.5,
        option_type="CE",
        volume_spike_entry=False,
        late_session_reclaim_entry=False,
        one_way_cascade_entry=False,
        missed_signal_reentry=False,
        is_reversion_scalp=False,
    )
    entry, stop, target = scanner._execution_levels(candidate)
    assert (entry, stop, target) == pytest.approx((120.0, 115.2, 127.2))
    assert (target - entry) / (entry - stop) == pytest.approx(1.5)


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_real_option_execution_uses_the_marketable_ask_for_both_signal_directions(
    direction,
):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_live_premium_cached = lambda candidate: 999.0
    scanner.log_signal_decision_once = lambda *args, **kwargs: None
    scanner._configured_option_sl_pct = lambda: 15.0
    scanner.expiry = SimpleNamespace(in_gamma_window=lambda *args, **kwargs: False)
    scanner._session_squareoff_clock = lambda: None
    candidate = SimpleNamespace(
        inst_type="OPT",
        instrument="NIFTY",
        timeframe="5min",
        direction=direction,
        trading_symbol="NIFTY25000CE" if direction == "LONG" else "NIFTY25000PE",
        price=100.0,
        stop=96.0,
        target=106.0,
        multiplier=0.5,
        rr=1.5,
        option_type="CE" if direction == "LONG" else "PE",
        volume_spike_entry=False,
        late_session_reclaim_entry=False,
        one_way_cascade_entry=False,
        missed_signal_reentry=False,
        is_reversion_scalp=False,
    )

    levels = scanner._execution_levels(
        candidate,
        execution_touch_quote={
            "bid": 119.0,
            "ask": 120.0,
            "age_seconds": 0.1,
            "usable": True,
        },
    )

    assert levels == pytest.approx((120.0, 115.2, 127.2))


@pytest.mark.parametrize(
    ("direction", "stop", "target", "expected"),
    [
        ("LONG", 24_975.0, 25_050.0, (25_002.0, 24_977.0, 25_052.0)),
        ("SHORT", 25_025.0, 24_950.0, (24_998.0, 25_023.0, 24_948.0)),
    ],
)
def test_real_futures_execution_uses_the_directional_marketable_touch(
    direction, stop, target, expected
):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_live_fut_ltp_cached = lambda candidate: 99_999.0
    candidate = SimpleNamespace(
        inst_type="FUT",
        direction=direction,
        price=25_000.0,
        stop=stop,
        target=target,
    )

    levels = scanner._execution_levels(
        candidate,
        execution_touch_quote={
            "bid": 24_998.0,
            "ask": 25_002.0,
            "age_seconds": 0.1,
            "usable": True,
        },
    )

    assert levels == pytest.approx(expected)


@pytest.mark.parametrize(
    "quote",
    [
        {"bid": 99.0, "ask": 100.0, "age_seconds": 99.0, "usable": True},
        {"bid": 99.0, "ask": 100.0, "usable": True},
        {"bid": 101.0, "ask": 100.0, "age_seconds": 0.1, "usable": True},
    ],
)
def test_real_execution_levels_fail_closed_on_unusable_touch_even_with_an_ltp(quote):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._get_live_fut_ltp_cached = lambda candidate: 25_000.0
    candidate = SimpleNamespace(
        inst_type="FUT",
        direction="LONG",
        price=25_000.0,
        stop=24_975.0,
        target=25_050.0,
    )

    assert scanner._execution_levels(
        candidate,
        execution_touch_quote=quote,
    ) == (0.0, 0.0, 0.0)


def test_projected_scale_in_policy_does_not_mutate_parent_geometry(monkeypatch):
    now = datetime(2026, 8, 27, 10, 0)
    policy_settings = SimpleNamespace(
        entry_policy_enabled=True,
        entry_policy_min_rr=1.5,
        entry_policy_expected_move_floor_pct_by_index={"NIFTY": 0.20},
        entry_policy_underlying_stop_max_pct_by_index={"NIFTY": 0.15},
        entry_policy_forecast_max_age_seconds=90.0,
        entry_policy_geometry_max_age_seconds=10.0,
    )
    monkeypatch.setattr(scanner_module, "get_settings", lambda: policy_settings)
    scanner = Scanner.__new__(Scanner)
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_030.0)
    scanner._entry_policy_clock = lambda holder, phase: now
    scanner._expected_move_forecast_for_entry = (
        lambda holder, checked_at, floor_pct: _forecast(now)
    )
    parent = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="OPT",
        market_regime="TRENDING_UP",
        source_lane="cascade",
        underlying_entry=25_000.0,
        underlying_stop=24_975.0,
        underlying_target=25_050.0,
        entry_spot=25_000.0,
        spot_stop=24_975.0,
        spot_target=25_050.0,
        premium_entry=100.0,
        premium_stop=96.0,
        premium_target=106.0,
        geometry_observed_at=now.isoformat(),
        geometry_source="signal_processor.dual_geometry|auto_pre_submit",
        geometry_verified=True,
        _entry_policy_prior_quantity=100,
        _entry_policy_add_quantity=50,
    )
    before = (
        parent.underlying_entry,
        parent.underlying_stop,
        parent.underlying_target,
        parent.premium_entry,
        parent.premium_stop,
        parent.premium_target,
    )
    verdict = scanner._final_entry_policy_verdict(
        parent,
        execution_levels=(104.0, 100.0, 110.0),
        phase="scale_in_pre_submit",
    )
    after = (
        parent.underlying_entry,
        parent.underlying_stop,
        parent.underlying_target,
        parent.premium_entry,
        parent.premium_stop,
        parent.premium_target,
    )
    assert verdict.allowed is True
    assert after == before
    assert parent.projected_entry_policy_verdict["allowed"] is True


def test_broker_queue_refreshes_underlying_geometry_instead_of_reusing_a_stale_stamp(
    monkeypatch,
):
    now = datetime(2026, 8, 27, 10, 0)
    policy_settings = SimpleNamespace(
        entry_policy_enabled=True,
        entry_policy_min_rr=1.5,
        entry_policy_expected_move_floor_pct_by_index={"NIFTY": 0.20},
        entry_policy_underlying_stop_max_pct_by_index={"NIFTY": 0.15},
        entry_policy_forecast_max_age_seconds=90.0,
        entry_policy_geometry_max_age_seconds=10.0,
    )
    monkeypatch.setattr(scanner_module, "get_settings", lambda: policy_settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_030.0)
    scanner.data = SimpleNamespace(
        get_cached_index_quote_snapshot=lambda instrument: {
            "spot_price": 25_030.0,
            "quote_age_seconds": 0.0,
            "is_fresh": True,
            "source": "kite_ws",
            "source_role": "primary",
        }
    )
    scanner._entry_policy_clock = lambda holder, phase: now
    scanner._expected_move_forecast_for_entry = (
        lambda holder, checked_at, floor_pct: _forecast(now)
    )
    trade = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="OPT",
        market_regime="TRENDING_UP",
        source_lane="ordinary",
        entry_price=100.0,
        current_stop=96.0,
        target=106.0,
        underlying_entry=25_000.0,
        underlying_stop=24_975.0,
        underlying_target=25_050.0,
        geometry_observed_at=(now - timedelta(seconds=30)).isoformat(),
        geometry_source="signal_processor.dual_geometry|auto_pre_submit",
        geometry_verified=True,
        geometry_schema="dual_price_geometry.v1",
    )

    verdict = scanner._final_entry_policy_verdict(trade, phase="broker_queue")

    assert verdict.allowed is True
    assert trade.underlying_entry == pytest.approx(25_030.0)
    assert trade.geometry_observed_at == now.isoformat()


def test_material_primary_ltp_rebase_clamps_ranging_risk_to_fresh_spot(monkeypatch):
    """A >0.015% move between two valid observations must use fresh Kite geometry."""
    now = datetime(2026, 8, 27, 10, 0)
    old_spot = 24_101.45
    fresh_kite_spot = 24_094.15
    assert abs(old_spot - fresh_kite_spot) / old_spot * 100.0 > 0.015

    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            entry_policy_underlying_stop_max_pct_by_index={"NIFTY": 0.15}
        ),
    )
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.data = SimpleNamespace(
        get_cached_index_quote_snapshot=lambda instrument: {
            "spot_price": fresh_kite_spot,
            "quote_age_seconds": 0.0,
            "is_fresh": True,
            "source": "kite_ws",
            "source_role": "primary",
        }
    )
    candidate = TradeCandidate(
        instrument="NIFTY",
        direction="LONG",
        price=100.0,
        stop=96.0,
        target=106.0,
        lots=1,
        lot_size=65,
        grade="B+",
        confidence=0.60,
        timeframe="5min",
        inst_type="OPT",
        option_type="CE",
        atm_strike=24_100.0,
        multiplier=0.5,
        trading_symbol="NIFTYTESTCE",
        symbol_token="TOKEN-NIFTY",
        underlying_entry=old_spot,
        underlying_stop=old_spot - (old_spot * 0.001),
        underlying_target=old_spot + 48.0,
        geometry_observed_at=(now - timedelta(seconds=2)).isoformat(),
        geometry_source="signal_processor.dual_geometry",
        geometry_verified=True,
    )
    candidate.market_regime = "RANGING"

    geometry = scanner._entry_execution_geometry(
        candidate,
        (candidate.price, candidate.stop, candidate.target),
        phase="auto_pre_submit",
        observed_at=now,
    )

    assert geometry.underlying_entry == pytest.approx(fresh_kite_spot)
    rebased_risk = geometry.underlying_entry - geometry.underlying_stop
    assert rebased_risk <= 24.09415 + 1e-8
    assert rebased_risk == pytest.approx(24.09415, abs=1e-8)
    assert "underlying:kite_ws" in geometry.source


def test_real_broker_queue_does_not_restamp_a_stale_underlying_quote(monkeypatch):
    now = datetime(2026, 8, 27, 10, 0)
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            entry_policy_enabled=True,
            entry_policy_min_rr=1.5,
            entry_policy_expected_move_floor_pct_by_index={"NIFTY": 0.20},
            entry_policy_underlying_stop_max_pct_by_index={"NIFTY": 0.15},
            entry_policy_forecast_max_age_seconds=90.0,
            entry_policy_geometry_max_age_seconds=10.0,
        ),
    )
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.data = SimpleNamespace(
        get_cached_index_quote_snapshot=lambda instrument: {
            "spot_price": 25_030.0,
            "quote_age_seconds": 30.0,
            "is_fresh": False,
            "source": "kite_ws",
            "source_role": "primary",
        }
    )
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 25_030.0)
    scanner._entry_policy_clock = lambda holder, phase: now
    scanner._expected_move_forecast_for_entry = (
        lambda holder, checked_at, floor_pct: _forecast(now)
    )
    trade = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="OPT",
        market_regime="TRENDING_UP",
        source_lane="ordinary",
        entry_price=100.0,
        current_stop=96.0,
        target=106.0,
        underlying_entry=25_000.0,
        underlying_stop=24_975.0,
        underlying_target=25_050.0,
        geometry_observed_at=(now - timedelta(seconds=1)).isoformat(),
        geometry_source="signal_processor.dual_geometry|auto_pre_submit",
        geometry_verified=True,
        geometry_schema="dual_price_geometry.v1",
    )

    verdict = scanner._final_entry_policy_verdict(trade, phase="broker_queue")

    assert verdict.allowed is False
    assert dict(verdict.checks)["finite_verified_geometry"] is False
    assert trade.geometry_observed_at == (now - timedelta(seconds=1)).isoformat()


def test_real_entry_geometry_refuses_fresh_fyers_failover_authority():
    now = datetime(2026, 8, 27, 10, 0)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.data = SimpleNamespace(
        get_cached_index_quote_snapshot=lambda instrument: {
            "spot_price": 24_094.15,
            "quote_age_seconds": 0.01,
            "is_fresh": True,
            "source": "fyers_ws",
            "source_role": "failover",
        }
    )

    assert scanner._entry_policy_live_index_quote("NIFTY", now) == (0.0, "", "")


def test_broker_queue_reprices_the_contract_touch_and_hands_it_to_submission():
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner._candidate_execution_touch_quote = lambda trade: {
        "bid": 25_029.0,
        "ask": 25_031.0,
        "age_seconds": 0.1,
        "usable": True,
        "source": "kite_depth",
    }
    seen = {}

    def policy(holder, *, execution_levels, phase):
        seen["levels"] = execution_levels
        seen["phase"] = phase
        return True, ""

    scanner._entry_policy_allows = policy
    trade = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="FUT",
        trading_symbol="NIFTYTESTFUT",
        symbol_token="TOKEN-NIFTY",
        entry_price=25_000.0,
        current_price=25_000.0,
        current_stop=24_975.0,
        trailing_stop=24_975.0,
        initial_stop=24_975.0,
        target=25_050.0,
    )

    allowed, reason = scanner._queued_fresh_entry_policy_guard(trade)

    assert allowed is True, reason
    assert seen == {
        "levels": pytest.approx((25_031.0, 25_006.0, 25_081.0)),
        "phase": "broker_queue",
    }
    assert trade._entry_policy_submission_levels == pytest.approx(
        (25_031.0, 25_006.0, 25_081.0)
    )
    assert trade._entry_policy_submission_touch_quote["ask"] == 25_031.0
    assert (trade.entry_price, trade.current_stop, trade.target) == pytest.approx(
        (25_031.0, 25_006.0, 25_081.0)
    )


def test_scanner_policy_rejects_malformed_holder_geometry_without_throwing(monkeypatch):
    now = datetime(2026, 8, 27, 10, 0)
    policy_settings = SimpleNamespace(
        entry_policy_enabled=True,
        entry_policy_min_rr=1.5,
        entry_policy_expected_move_floor_pct_by_index={"NIFTY": 0.20},
        entry_policy_underlying_stop_max_pct_by_index={"NIFTY": 0.15},
        entry_policy_forecast_max_age_seconds=90.0,
        entry_policy_geometry_max_age_seconds=10.0,
    )
    monkeypatch.setattr(scanner_module, "get_settings", lambda: policy_settings)
    scanner = Scanner.__new__(Scanner)
    scanner.mode = "REAL"
    scanner.candles = SimpleNamespace(get_latest_price=lambda instrument: 0.0)
    scanner._entry_policy_clock = lambda holder, phase: now
    scanner._expected_move_forecast_for_entry = (
        lambda holder, checked_at, floor_pct: _forecast(now)
    )
    holder = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        inst_type="OPT",
        market_regime="TRENDING_UP",
        source_lane="ordinary",
        entry_price="bad-entry",
        current_stop=96.0,
        target=106.0,
        underlying_entry="bad-spot",
        underlying_stop=24_975.0,
        underlying_target=25_050.0,
        geometry_observed_at=now.isoformat(),
        geometry_source="signal_processor.dual_geometry",
        geometry_verified=True,
        geometry_schema="dual_price_geometry.v1",
    )

    verdict = scanner._final_entry_policy_verdict(holder, phase="broker_queue")

    assert verdict.allowed is False
    assert dict(verdict.checks)["finite_verified_geometry"] is False


def test_actual_fill_tightens_the_stop_to_restore_one_point_five_r(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(entry_policy_min_rr=1.5),
    )
    manager = TradeManager.__new__(TradeManager)
    seen = {}

    def guard(trade):
        seen["levels"] = trade._entry_policy_execution_levels
        seen["phase"] = trade._entry_policy_phase
        return True, ""

    manager.fresh_entry_policy_guard = guard
    trade = Trade(
        id="T1",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=102.0,
        entry_time=datetime.now(IST),
        trailing_stop=96.0,
        current_stop=96.0,
        target=106.0,
        lots=1,
        lot_size=65,
        grade="A",
        inst_type="OPT",
    )
    allowed, reason = manager._actual_fill_entry_policy_verdict(trade)
    assert allowed is True, reason
    assert trade.current_stop == pytest.approx(102.0 - (4.0 / 1.5))
    assert seen["levels"] == pytest.approx((102.0, trade.current_stop, 106.0))
    assert seen["phase"] == "actual_broker_fill"


def test_actual_fill_policy_fails_closed_when_broker_fill_price_is_unverified():
    manager = TradeManager.__new__(TradeManager)
    manager.fresh_entry_policy_guard = lambda trade: (True, "")
    trade = Trade(
        id="T-unverified",
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        entry_price=100.0,
        entry_time=datetime.now(IST),
        trailing_stop=96.0,
        current_stop=96.0,
        target=106.0,
        lots=1,
        lot_size=65,
        grade="A",
        inst_type="OPT",
    )
    trade._entry_policy_actual_fill_verified = False

    allowed, reason = manager._actual_fill_entry_policy_verdict(trade)

    assert allowed is False
    assert "unavailable or unverified" in reason


def test_fresh_fifteen_minute_trade_does_not_exit_on_local_one_minute_break(monkeypatch):
    monkeypatch.setattr(
        scanner_module,
        "get_settings",
        lambda: SimpleNamespace(
            fifteen_min_one_min_support_exit_enabled=True,
            fifteen_min_one_min_support_min_hold_seconds=0.0,
        ),
    )
    scanner = Scanner.__new__(Scanner)
    scanner._profit_guard_reversal_context = lambda *args, **kwargs: {
        "has_context": True,
        "one_min_context": {"has_context": True, "reason": "large opposing move"},
        "one_min_emergency": True,
        "fast_reversal": False,
        "trend_supportive": False,
        "pullback_class": "structure_break",
        "reason": "15m support failed with 1m confirmation",
    }
    trade = SimpleNamespace(
        timeframe="15min",
        entry_time=datetime.now(IST) - timedelta(seconds=1),
    )
    allowed, reason = scanner._fifteen_min_one_min_support_exit_decision(
        trade, 25_000.0, datetime.now(IST)
    )
    assert allowed is False
    assert "fresh trade" in reason


def test_rtracker_volume_records_availability_source_and_basis(tmp_path):
    tracker = RegimeTracker(tmp_path)
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        signal_timestamp=datetime(2026, 8, 27, 10, 0),
        market_intelligence_context={
            "volume": {
                "signal": "BULLISH",
                "available": True,
                "source": "fyers_cash_index",
                "basis": "cash_constituent_proxy",
                "volume_ratio": 1.8,
            }
        },
    )
    event = tracker._event_from_candidate(
        candidate,
        decision="REJECTED",
        reason="test",
        source="test",
        extra=None,
    )
    assert event["volume"] == {
        "signal": "BULLISH",
        "available": True,
        "source": "fyers_cash_index",
        "basis": "cash_constituent_proxy",
        "unit": "",
        "comparable": None,
        "unavailable_reason": "",
        "volume_ratio": 1.8,
        "current_volume": None,
        "buy_sell_ratio": None,
    }


def test_rtracker_volume_string_false_is_not_recorded_as_available(tmp_path):
    tracker = RegimeTracker(tmp_path)
    candidate = SimpleNamespace(
        instrument="NIFTY",
        timeframe="5min",
        direction="LONG",
        signal_timestamp=datetime(2026, 8, 27, 10, 0),
        market_intelligence_context={
            "volume": {
                "available": "false",
                "source": "unavailable",
                "basis": "none",
            }
        },
    )

    event = tracker._event_from_candidate(
        candidate,
        decision="REJECTED",
        reason="test",
        source="test",
        extra=None,
    )

    assert event["volume"]["available"] is False
