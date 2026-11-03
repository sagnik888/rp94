"""A signal against a trend label whose session extreme is stale is not counter-trend.

2026-09-17: BANKNIFTY topped at 09:35 (+270) and fell to -222 by 11:08. At 10:25-10:45
its 5min regime still read TRENDING_UP, so the SHORT signals graded C at 8-13% and the
price-action, session-trend and census vetoes all refused them as counter-trend.
Measured over 22 sessions, signals against a trend label whose own extreme was at least
30 minutes old averaged +0.77 ATR at 30 minutes (59% positive, 13/21 days); the same
staleness filter adds nothing to ranging or aligned signals.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import get_settings
from engine.quality_verdict import QualityInputs, evaluate_quality
from scanner import Scanner


DECISION = datetime(2026, 9, 17, 10, 30, 20)


@pytest.fixture
def route_on(monkeypatch):
    """Exercise the mechanism with the route forced on.

    The route SHIPS OFF since 2026-09-18 (see test_the_route_ships_off below), so every
    test of its internals has to turn it on explicitly. monkeypatch restores the singleton
    afterwards; a bare assignment to get_settings() would leak into the rest of the suite.
    """
    monkeypatch.setattr(get_settings(), "stale_regime_reversal_enabled", True, raising=False)


def test_the_route_ships_off():
    """One live session, three trades, three losses, -Rs.2,478.

    All three came through route=stale_regime_reversal on MIDCPNIFTY 15min with
    confirmation_count 0 and every leg false: C grade at 19%/19%/32%, volume 0.43-0.64x
    against a 1.2x floor, source_count 1, market_shift_core_votes 0, price action ADVERSE
    on two, against a MEASURED ADX of 32-36 pointing the other way. The staleness test is
    a one-way latch — MIDCPNIFTY set its high at 09:24, so "extreme older than 30 minutes"
    held from 09:54 to the close — and all three shorts landed within 6 points of the
    session low, with price higher 15 minutes later every time.
    """
    from config.settings import Settings

    assert Settings().stale_regime_reversal_enabled is False


def _session_frame(high_at: datetime, *, start=datetime(2026, 9, 17, 9, 15), end=DECISION):
    """Flat 1-minute bars with a single session high at ``high_at``."""
    index = pd.date_range(start=start, end=end.replace(second=0), freq="1min")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
        index=index,
    )
    frame.loc[pd.Timestamp(high_at), "high"] = 150.0
    return frame


def _scanner(frame, mode="REAL"):
    scanner = Scanner.__new__(Scanner)
    scanner.mode = mode
    scanner.candles = SimpleNamespace(get_candles=lambda *_args: frame)
    return scanner


def _context(scanner, **overrides):
    kwargs = dict(
        instrument="BANKNIFTY",
        timeframe="5min",
        signal_type="SELL",
        signal_timestamp=datetime(2026, 9, 17, 10, 25),
        regime="TRENDING_UP",
        as_of=DECISION,
    )
    kwargs.update(overrides)
    return Scanner._stale_regime_reversal_context(
        scanner,
        kwargs.pop("instrument"),
        kwargs.pop("timeframe"),
        kwargs.pop("signal_type"),
        kwargs.pop("signal_timestamp"),
        **kwargs,
    )


def test_short_against_an_uptrend_whose_high_is_stale_is_flagged(route_on):
    ctx = _context(_scanner(_session_frame(datetime(2026, 9, 17, 9, 35))))

    assert ctx is not None
    assert ctx["type"] == "stale_regime_reversal"
    assert ctx["enabled"] is True
    assert ctx["extreme_side"] == "high"
    assert ctx["minutes_since_extreme"] == pytest.approx(55.0)


def test_a_fresh_high_is_not_stale(route_on):
    assert _context(_scanner(_session_frame(datetime(2026, 9, 17, 10, 11)))) is None


def test_long_against_a_downtrend_uses_the_session_low(route_on):
    frame = _session_frame(datetime(2026, 9, 17, 9, 35))
    frame["high"] = 101.0
    frame.loc[pd.Timestamp(datetime(2026, 9, 17, 9, 40)), "low"] = 50.0

    ctx = _context(_scanner(frame), signal_type="BUY", regime="BREAKOUT_DOWN")

    assert ctx is not None and ctx["extreme_side"] == "low"
    assert ctx["minutes_since_extreme"] == pytest.approx(50.0)


@pytest.mark.parametrize(
    "signal_type, regime",
    [
        ("SELL", "RANGING"),
        ("SELL", "VOLATILE"),
        ("SELL", "TRENDING_DOWN"),  # aligned, not a reversal
        ("BUY", "TRENDING_UP"),     # aligned, not a reversal
    ],
)
def test_only_signals_against_a_trend_label_qualify(route_on, signal_type, regime):
    frame = _session_frame(datetime(2026, 9, 17, 9, 35))
    assert _context(_scanner(frame), signal_type=signal_type, regime=regime) is None


def test_one_minute_signals_are_excluded(route_on):
    frame = _session_frame(datetime(2026, 9, 17, 9, 35))
    assert _context(_scanner(frame), timeframe="1min") is None


def test_previous_session_extreme_is_ignored(route_on):
    """The candle store has no session break: yesterday's high must not look stale."""
    yesterday = pd.DataFrame(
        {"open": 100.0, "high": 500.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
        index=pd.date_range(datetime(2026, 9, 16, 15, 20), periods=10, freq="1min"),
    )
    today = _session_frame(datetime(2026, 9, 17, 10, 15))
    frame = pd.concat([yesterday, today])

    assert _context(_scanner(frame)) is None


def test_the_forming_minute_does_not_count_as_the_extreme(route_on):
    """Only bars that closed before the decision clock are read."""
    frame = _session_frame(datetime(2026, 9, 17, 9, 35))
    # A bar stamped at the decision minute is still forming; its high is not observed.
    frame.loc[pd.Timestamp(DECISION.replace(second=0)), "high"] = 999.0

    ctx = _context(_scanner(frame))

    assert ctx is not None
    assert ctx["minutes_since_extreme"] == pytest.approx(55.0)


def test_historical_mode_decides_at_the_signal_bar_close(route_on):
    frame = _session_frame(datetime(2026, 9, 17, 9, 50), end=datetime(2026, 9, 17, 11, 0))
    scanner = _scanner(frame, mode="HISTORICAL")

    # 5min bar opened 10:15 closes 10:20: 30 minutes after the 09:50 high.
    ctx = _context(scanner, as_of=None, signal_timestamp=datetime(2026, 9, 17, 10, 15))
    assert ctx is not None and ctx["minutes_since_extreme"] == pytest.approx(30.0)

    # 5min bar opened 10:10 closes 10:15: only 25 minutes.
    assert _context(scanner, as_of=None, signal_timestamp=datetime(2026, 9, 17, 10, 10)) is None


def test_setting_disables_detection(monkeypatch):
    monkeypatch.setattr(get_settings(), "stale_regime_reversal_enabled", False, raising=False)
    frame = _session_frame(datetime(2026, 9, 17, 9, 35))

    assert _context(_scanner(frame)) is None


def test_threshold_setting_is_honoured(route_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "stale_regime_reversal_min_minutes", 60.0, raising=False)
    frame = _session_frame(datetime(2026, 9, 17, 9, 35))

    assert _context(_scanner(frame)) is None


def _inputs(**overrides):
    values = dict(
        timeframe="5min",
        inst_type="OPT",
        confidence=0.08,
        grade="C",
        regime="TRENDING_UP",
        adx_value=22.5,
        stale_regime_reversal=True,
        direction="SHORT",
    )
    values.update(overrides)
    return QualityInputs(**values)


def test_quality_verdict_admits_a_stale_regime_reversal_below_bplus(route_on):
    verdict = evaluate_quality(_inputs(), get_settings())

    assert verdict.allowed
    assert verdict.route == "stale_regime_reversal"
    assert verdict.reason_code == "STALE_REGIME_REVERSAL"
    assert verdict.checks["stale_regime_reversal"] is True


def test_quality_verdict_without_the_flag_keeps_the_bplus_floor(route_on):
    verdict = evaluate_quality(_inputs(stale_regime_reversal=False), get_settings())

    assert not verdict.allowed
    assert verdict.reason_code == "BELOW_BPLUS_ELIGIBILITY"


def test_quality_verdict_route_is_limited_to_5min_and_15min(route_on):
    assert evaluate_quality(_inputs(timeframe="15min"), get_settings()).allowed
    verdict = evaluate_quality(_inputs(timeframe="1min"), get_settings())
    assert not verdict.allowed


def test_quality_verdict_route_respects_the_setting(monkeypatch):
    monkeypatch.setattr(get_settings(), "stale_regime_reversal_enabled", False, raising=False)

    verdict = evaluate_quality(_inputs(), get_settings())

    assert not verdict.allowed
    assert verdict.reason_code == "BELOW_BPLUS_ELIGIBILITY"


def test_grade_integrity_still_rejects_before_the_route():
    verdict = evaluate_quality(_inputs(grade="A+"), get_settings())

    assert not verdict.allowed
    assert verdict.reason_code == "GRADE_CONFIDENCE_MISMATCH"
