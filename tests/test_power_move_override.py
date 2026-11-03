"""
Late-session power-move override.

A data-confirmed big move (volume surge + range expansion + directional close +
momentum & 15m alignment) may bypass the grade floor (any time) and the 15:00
no-entry cutoff (after 15:00, up to the hard cutoff), entering in scalp-lock mode.

Validated against the shape of the real 2026-06-19 15:00 reversal candles
(e.g. NIFTY: range ~138 vs ~30 prior, volume ~6x, close ~88% of range).
"""

from datetime import datetime, time as dtime
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner import Scanner
from config.settings import get_settings
from engine.signal_processor import TradeCandidate
from engine.signal_quality_policy import (
    power_move_confirmed,
    volume_spike_entry_confirmed,
)


@pytest.fixture
def settings():
    s = get_settings()
    s.power_move_override_enabled = True
    s.power_move_volume_mult = 2.5
    s.power_move_range_atr_mult = 2.0
    # Pinned like the rest: this fixture reads the live singleton, so without it
    # the operator's .env leaks in and the test measures their config rather than
    # this code. POWER_MOVE_SPIKE_RANGE_ATR_MULT='2.0' in .env against a 2.5 code
    # default made a 2.2x range count as an explosive spike and waive the 15m
    # alignment the sub-spike case exists to require.
    s.power_move_spike_range_atr_mult = 2.5
    s.power_move_close_location = 0.70
    s.power_move_volume_lookback = 20
    s.momentum_override_threshold = 25.0
    s.volume_spike_entry_enabled = True
    s.one_min_scalping_enabled = True
    # Legacy eased floor tests use high_burst_only=False (min of floors).
    # Continuous 1M SPIKE path defaults high_burst_only=True in production.
    s.one_min_scalping_high_burst_only = False
    s.volume_spike_entry_min_volume_ratio = 2.5
    s.volume_spike_entry_strong_1min_min_volume_ratio = 2.0
    s.volume_spike_entry_strong_1min_adx_threshold = 25.0
    s.volume_spike_entry_strong_1min_intel_threshold = 0.70
    s.volume_spike_entry_min_range_atr_ratio = 1.5
    s.volume_spike_entry_min_close_location = 0.65
    s.volume_spike_entry_options_sl_pct = 5.0
    s.volume_spike_entry_min_rr = 2.5
    s.volume_spike_entry_1min_scalp_rr = 1.5
    s.volume_spike_entry_stabilization_seconds = 5.0
    return s


# ── pure detector: all 4 factors required ───────────────────────────
def test_power_move_requires_all_four_factors(settings):
    def pm(**kw):
        base = dict(volume_ratio=6.0, range_atr_ratio=3.0, close_location=0.88,
                    adx_value=30.0, intel_score=0.3, trend_15m_agrees=True, settings=settings)
        base.update(kw)
        return power_move_confirmed(**base)

    assert pm() is True                              # all confirm
    assert pm(volume_ratio=1.5) is False             # no volume surge
    assert pm(range_atr_ratio=1.2) is False          # no range expansion
    assert pm(close_location=0.5) is False           # doji / no conviction
    # Momentum is spike-aware: ADX>=20 OR intel>=0.70 OR volume>=3.5x.
    assert pm(adx_value=15.0, intel_score=0.3, volume_ratio=2.6) is False  # no momentum (vol<3.5, adx<20)
    assert pm(adx_value=15.0, intel_score=0.3, volume_ratio=4.0) is True   # momentum via strong volume
    assert pm(adx_value=22.0, intel_score=0.3, volume_ratio=2.6) is True   # momentum via ADX>=20
    assert pm(adx_value=15.0, intel_score=0.85, volume_ratio=2.6) is True  # momentum via strong intel
    # 15m alignment: a genuine EXPLOSIVE spike (range>=2.5x ATR + hard close +
    # momentum) bypasses 15m agreement — a spike LEADS the 15m bar, so requiring
    # 15m agreement would make post-2:59pm spikes structurally un-catchable.
    assert pm(trend_15m_agrees=False) is True              # explosive spike bypasses 15m
    assert pm(trend_15m_agrees=False, range_atr_ratio=2.2) is False  # sub-spike still needs 15m


def test_power_move_disabled_flag(settings):
    settings.power_move_override_enabled = False
    try:
        assert power_move_confirmed(volume_ratio=9.0, range_atr_ratio=5.0, close_location=0.95,
                                    adx_value=40.0, intel_score=0.9, trend_15m_agrees=True,
                                    settings=settings) is False
    finally:
        settings.power_move_override_enabled = True


def test_volume_spike_entry_thresholds_are_exact_and_timeframe_scoped(settings):
    def spike(**kw):
        base = dict(
            timeframe="5min",
            volume_ratio=2.5,
            range_atr_ratio=1.5,
            close_location=0.65,
            settings=settings,
        )
        base.update(kw)
        return volume_spike_entry_confirmed(**base)

    assert spike() is True
    assert spike(timeframe="1min") is True
    assert spike(timeframe="15min") is False
    assert spike(volume_ratio=2.49) is False
    assert spike(range_atr_ratio=1.49) is False
    assert spike(close_location=0.64) is False


def test_strong_one_minute_spike_can_use_two_x_volume_floor(settings):
    def spike(**kw):
        base = dict(
            timeframe="1min",
            volume_ratio=2.0,
            range_atr_ratio=1.5,
            close_location=0.65,
            adx_value=25.0,
            intel_score=0.0,
            settings=settings,
        )
        base.update(kw)
        return volume_spike_entry_confirmed(**base)

    assert spike() is True
    assert spike(volume_ratio=1.99) is False
    assert spike(adx_value=24.0, intel_score=0.0, power_move=False) is False
    assert spike(adx_value=0.0, intel_score=0.70) is True
    assert spike(adx_value=0.0, intel_score=0.0, power_move=True) is True
    assert spike(timeframe="5min", adx_value=40.0) is False
    assert spike(range_atr_ratio=1.49) is False
    assert spike(close_location=0.64) is False


def test_volume_spike_entry_disabled_flag(settings):
    settings.volume_spike_entry_enabled = False
    try:
        assert volume_spike_entry_confirmed(
            timeframe="5min",
            volume_ratio=4.0,
            range_atr_ratio=4.0,
            close_location=0.9,
            settings=settings,
        ) is False
    finally:
        settings.volume_spike_entry_enabled = True


# ── scanner detector: metric extraction from candles ────────────────
def _df(breakout: bool):
    rows = []
    base = 23900.0
    ts = pd.date_range("2026-06-19 11:00", periods=25, freq="15min")
    for t in ts:
        rows.append({"open": base, "high": base + 15, "low": base - 15, "close": base + 5, "volume": 15_000_000})
    if breakout:
        # NIFTY-like 15:00 reversal: ~138pt range, ~6x volume, close ~88% of range.
        rows.append({"open": 23910.5, "high": 24042.65, "low": 23904.45, "close": 24026.95, "volume": 92_930_201})
    else:
        rows.append({"open": base, "high": base + 16, "low": base - 14, "close": base + 1, "volume": 15_500_000})
    idx = pd.date_range("2026-06-19 11:00", periods=len(rows), freq="15min")
    return pd.DataFrame(rows, index=idx)


def _scanner(df):
    st = SimpleNamespace(
        candles=SimpleNamespace(get_candles=lambda inst, tf: df),
        _setting_enabled=lambda v, d: bool(v) if isinstance(v, bool) else d,
        log_event=lambda *a, **k: None,
    )
    st._detect_power_move = Scanner._detect_power_move.__get__(st, Scanner)
    st._candle_thrust_metrics = Scanner._candle_thrust_metrics.__get__(st, Scanner)
    st._detect_volume_spike_entry = Scanner._detect_volume_spike_entry.__get__(st, Scanner)
    st._power_move_late_cutoff_ok = Scanner._power_move_late_cutoff_ok.__get__(st, Scanner)
    return st


def test_detect_power_move_on_breakout_candle(settings):
    st = _scanner(_df(breakout=True))
    assert st._detect_power_move("NIFTY", "15min", "BUY", adx_value=30.0,
                                 intel_score=0.3, trend_15m_agrees=True) is True


def test_detect_power_move_rejects_normal_candle(settings):
    st = _scanner(_df(breakout=False))
    assert st._detect_power_move("NIFTY", "15min", "BUY", adx_value=30.0,
                                 intel_score=0.3, trend_15m_agrees=True) is False


def test_detect_power_move_spike_bypasses_alignment(settings):
    # An explosive breakout spike (big range + hard close + momentum) bypasses 15m
    # alignment, because a sudden spike leads the 15m bar (post-2:59pm spike capture).
    st = _scanner(_df(breakout=True))
    assert st._detect_power_move("NIFTY", "15min", "BUY", adx_value=30.0,
                                 intel_score=0.3, trend_15m_agrees=False) is True


def test_detect_power_move_as_of_no_future_leak(settings):
    # HISTORICAL parity: a full df with a breakout candle followed by a normal candle.
    # as_of=breakout time must detect (slicing); evaluating the latest (normal) must not
    # — proving no future-data leak in backtest reconstruction.
    df = _df(breakout=True)
    next_ts = df.index[-1] + pd.Timedelta(minutes=15)
    normal_after = pd.DataFrame(
        [{"open": 24042.0, "high": 24050.0, "low": 24035.0, "close": 24040.0, "volume": 15_000_000}],
        index=[next_ts],
    )
    st = _scanner(pd.concat([df, normal_after]))
    breakout_ts = df.index[-1]
    assert st._detect_power_move("NIFTY", "15min", "BUY", adx_value=30.0, intel_score=0.0,
                                 trend_15m_agrees=True, as_of=breakout_ts) is True
    assert st._detect_power_move("NIFTY", "15min", "BUY", adx_value=30.0, intel_score=0.0,
                                 trend_15m_agrees=True) is False  # latest candle is normal


def test_detect_power_move_fires_on_low_adx_volume_spike(settings):
    # Today's MIDCPNIFTY case: ADX only ~12 but a ~6x volume / 4x range spike.
    # Spike-aware momentum (volume >= 3.5x) must still confirm it.
    st = _scanner(_df(breakout=True))
    assert st._detect_power_move("MIDCPNIFTY", "15min", "BUY", adx_value=12.0,
                                 intel_score=0.0, trend_15m_agrees=True) is True


def _five_min_spike_df(volume_ratio: float, range_ratio: float, close_location: float):
    prior_volume = 10_000.0
    prior_range = 10.0
    rows = [
        {
            "open": 25_000.0,
            "high": 25_005.0,
            "low": 24_995.0,
            "close": 25_001.0,
            "volume": prior_volume,
        }
        for _ in range(20)
    ]
    low = 24_950.0
    candle_range = prior_range * range_ratio
    high = low + candle_range
    close = high - (candle_range * close_location)
    rows.append(
        {
            "open": high - (candle_range * 0.2),
            "high": high,
            "low": low,
            "close": close,
            "volume": prior_volume * volume_ratio,
        }
    )
    return pd.DataFrame(
        rows,
        index=pd.date_range("2026-06-23 13:20", periods=len(rows), freq="5min"),
    )


@pytest.mark.parametrize(
    ("instrument", "volume_ratio", "range_ratio", "close_location"),
    [
        ("NIFTY", 3.62, 4.68, 0.83),
        ("BANKNIFTY", 3.65, 2.92, 0.76),
    ],
)
def test_real_june_23_sell_spikes_qualify_without_15min_alignment(
    settings,
    instrument,
    volume_ratio,
    range_ratio,
    close_location,
):
    st = _scanner(_five_min_spike_df(volume_ratio, range_ratio, close_location))
    metrics = st._detect_volume_spike_entry(instrument, "5min", "SELL")

    assert metrics is not None
    assert metrics["volume_ratio"] == pytest.approx(volume_ratio)
    assert metrics["range_atr_ratio"] == pytest.approx(range_ratio)
    assert metrics["close_location"] == pytest.approx(close_location)


def test_one_min_scalping_toggle_blocks_only_one_min_spike_entries(settings):
    """1M SPIKE OFF blocks continuous 1min spike detector; 5min still works.
    POST-WIN SNAPBACK admits 1min via reversion window (separate path)."""
    settings.one_min_scalping_enabled = False
    try:
        st = _scanner(_five_min_spike_df(3.5, 2.0, 0.80))

        assert st._detect_volume_spike_entry("NIFTY", "1min", "SELL", adx_value=40.0) is None
        assert st._detect_volume_spike_entry("NIFTY", "5min", "SELL", adx_value=40.0) is not None
    finally:
        settings.one_min_scalping_enabled = True


def test_high_burst_only_blocks_weak_1min_without_strong_context(settings):
    settings.one_min_scalping_high_burst_only = True
    settings.volume_spike_entry_strong_1min_min_volume_ratio = 2.5
    try:
        # Weak 1min: no ADX/intel/power → reject even at 2.5x volume.
        assert volume_spike_entry_confirmed(
            timeframe="1min",
            volume_ratio=2.5,
            range_atr_ratio=1.5,
            close_location=0.65,
            adx_value=10.0,
            intel_score=0.0,
            power_move=False,
            settings=settings,
        ) is False
        # Strong context + volume at floor 2.5 → accept.
        assert volume_spike_entry_confirmed(
            timeframe="1min",
            volume_ratio=2.5,
            range_atr_ratio=1.5,
            close_location=0.65,
            adx_value=28.0,
            intel_score=0.0,
            power_move=False,
            settings=settings,
        ) is True
        # Strong context but volume below floor → reject.
        assert volume_spike_entry_confirmed(
            timeframe="1min",
            volume_ratio=2.4,
            range_atr_ratio=1.5,
            close_location=0.65,
            adx_value=28.0,
            settings=settings,
        ) is False
    finally:
        settings.one_min_scalping_high_burst_only = False


def test_volume_spike_option_execution_uses_tight_candidate_stop(settings):
    candidate = TradeCandidate(
        instrument="NIFTY",
        direction="SHORT",
        price=100.0,
        stop=85.0,
        target=130.0,
        lots=1,
        lot_size=75,
        grade="B+",
        confidence=0.66,
        timeframe="5min",
        inst_type="OPT",
        option_type="PE",
        atm_strike=25_000.0,
        multiplier=1.0,
        trading_symbol="NIFTY25JUN25000PE",
        symbol_token="123",
        rr=2.5,
        volume_spike_entry=True,
        tight_stop_pct=5.0,
    )
    st = SimpleNamespace(
        trades=SimpleNamespace(options_sl_pct=15.0),
        _get_live_premium_cached=lambda candidate: 100.0,
        log_signal_decision_once=lambda *args, **kwargs: None,
    )
    st._execution_levels = Scanner._execution_levels.__get__(st, Scanner)
    st._candidate_market_regime = Scanner._candidate_market_regime.__get__(st, Scanner)

    entry, stop, target = st._execution_levels(candidate)

    assert entry == pytest.approx(100.0)
    assert stop == pytest.approx(95.0)
    assert target == pytest.approx(112.5)


def test_volume_spike_gets_fast_but_stable_live_confirmation(settings):
    candidate = TradeCandidate(
        instrument="NIFTY",
        direction="SHORT",
        price=25_000.0,
        stop=25_012.5,
        target=24_968.75,
        lots=1,
        lot_size=75,
        grade="B+",
        confidence=0.66,
        timeframe="5min",
        inst_type="FUT",
        option_type="",
        atm_strike=0.0,
        multiplier=1.0,
        trading_symbol="NIFTY25JUNFUT",
        symbol_token="123",
        rr=2.5,
        volume_spike_entry=True,
    )
    st = SimpleNamespace(latest_regimes={"NIFTY": "VOLATILE"}, _timeframe_regimes={})
    st._timeframe_intel_key = Scanner._timeframe_intel_key
    st._get_trade_regime = Scanner._get_trade_regime.__get__(st, Scanner)
    st._live_signal_stabilization_requirements = (
        Scanner._live_signal_stabilization_requirements.__get__(st, Scanner)
    )

    assert st._live_signal_stabilization_requirements(candidate) == (5.0, 2, True)


# ── hard late cutoff ────────────────────────────────────────────────
def test_late_hard_cutoff(settings):
    settings.power_move_late_hard_cutoff = "15:15"
    st = _scanner(_df(breakout=True))
    assert st._power_move_late_cutoff_ok(datetime(2026, 6, 19, 15, 5)) is True
    assert st._power_move_late_cutoff_ok(datetime(2026, 6, 19, 15, 14)) is True
    assert st._power_move_late_cutoff_ok(datetime(2026, 6, 19, 15, 20)) is False
