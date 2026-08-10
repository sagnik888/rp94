"""1min could not trade because its surge test was cut for a measurement it never has.

Traded volume is structurally absent at the live right edge of a cash index (13 of the
last 20 1min bars carry volume 0; 5min 1/20, 15min 0/20). The previous commit stopped
that reading as a fabricated 0.00x, but abstaining alone still leaves 1min unable to
enter — the lane needs a surge measure that EXISTS in real time.

tick_count is the only one. The trap is reusing the traded threshold on it:

    traded volume is heavily tailed  -> a z-score of 5 STILL admits 4.7% of 1min bars
    tick counts are near-Poisson     -> the same z admits ~nothing

so a 2.2x floor cut for volume would leave 1min exactly as dead as the zero-numerator
bug did. That is this codebase's recurring failure — a threshold applied to a quantity
it was never cut for.

PERCENTILE RANK is uniform for any continuous distribution, so one threshold means the
same thing on either measurement: "among the busiest N% of recent minutes". Calibrated
on the real 1min candles rather than guessed:

    ratio >= 2.2   admits 4.73% of measurable traded-volume bars
    rank  >= 97    admits 4.98%          <- matched strictness, basis-independent

and the shipped lookback makes that exact: 20 prior bars with rank>=97 is "strictly
busiest of the last 21", i.e. ~4.76%.

TRADED readings keep the ratio test untouched, so 5min and 15min are unaffected.
"""
import numpy as np
import pandas as pd
import pytest

from config.settings import get_settings
from engine.signal_quality_policy import volume_spike_entry_confirmed
from scanner import Scanner


@pytest.fixture(autouse=True)
def _pinned():
    """get_settings() is a shared singleton other tests mutate, so anything this file
    depends on is pinned and restored — otherwise these pass alone and fail in a full
    run, which is how this suite has been bitten before."""
    s = get_settings()
    keys = ("surge_tick_proxy_enabled", "surge_tick_percentile_floor",
            "one_min_scalping_enabled", "one_min_scalping_high_burst_only",
            "volume_spike_entry_enabled", "volume_spike_entry_min_volume_ratio",
            "volume_spike_entry_min_range_atr_ratio",
            "volume_spike_entry_min_close_location",
            "volume_spike_entry_strong_1min_adx_threshold",
            "volume_spike_depth_confirm_enabled")
    saved = {k: getattr(s, k, None) for k in keys}
    s.surge_tick_proxy_enabled = True
    s.surge_tick_percentile_floor = 97.0
    s.one_min_scalping_enabled = True
    s.one_min_scalping_high_burst_only = True
    s.volume_spike_entry_enabled = True
    s.volume_spike_entry_min_volume_ratio = 2.2
    s.volume_spike_entry_min_range_atr_ratio = 1.5
    s.volume_spike_entry_min_close_location = 0.65
    s.volume_spike_entry_strong_1min_adx_threshold = 25.0
    yield s
    for k, v in saved.items():
        if v is not None:
            setattr(s, k, v)


def _frame(*, bars=130, base=100, last_ticks=100, last_vol=None, prior_vol=0.0, expand=2.5):
    idx = pd.date_range("2026-08-11 09:15", periods=bars, freq="min")
    rs = np.random.RandomState(3)
    hi = [101.0] * (bars - 1); lo = [99.0] * (bars - 1); cl = [100.0] * (bars - 1)
    hi.append(99.0 + 2.0 * expand); lo.append(99.0); cl.append(99.0 + 2.0 * expand * 0.95)
    vol = [prior_vol] * (bars - 1) + [last_vol if last_vol is not None else prior_vol]
    tk = list(rs.poisson(base, bars - 1)) + [last_ticks]
    return pd.DataFrame(
        {"open": cl, "high": hi, "low": lo, "close": cl, "volume": vol, "tick_count": tk},
        index=idx,
    )


def _metrics(df, tf="1min"):
    sc = Scanner.__new__(Scanner)
    sc.candles = type("C", (), {"get_candles": staticmethod(lambda i, t: df)})()
    return Scanner._candle_thrust_metrics(sc, "NIFTY", tf, "BUY")


def _confirm(m, tf="1min", adx=30.0):
    return bool(volume_spike_entry_confirmed(
        timeframe=tf, volume_ratio=m["volume_ratio"],
        range_atr_ratio=m["range_atr_ratio"], close_location=m["close_location"],
        adx_value=adx, intel_score=0.0, power_move=False, settings=get_settings(),
        signal_type="BUY", depth_usable=False, ofr=0.0,
        volume_basis=m["volume_basis"], surge_percentile=m["surge_percentile"],
    ))


# ── 1min can now enter, on a measure that exists in real time ────────────────

def test_a_genuine_tick_surge_confirms_a_1min_entry():
    m = _metrics(_frame(last_ticks=220))
    assert m is not None and m["volume_basis"] == "TICK_ACTIVITY"
    assert _confirm(m) is True, "1min still cannot enter on any tick surge"


@pytest.mark.parametrize("ticks", [95, 100, 110])
def test_ordinary_tick_activity_does_not_confirm(ticks):
    m = _metrics(_frame(last_ticks=ticks))
    assert m is not None
    assert _confirm(m) is False, f"{ticks} ticks (pct {m['surge_percentile']}) confirmed"


def test_the_floor_is_what_decides(_pinned):
    m = _metrics(_frame(last_ticks=110))          # ~85th percentile
    assert _confirm(m) is False
    _pinned.surge_tick_percentile_floor = 80.0
    assert _confirm(m) is True


# ── the traded path must be untouched (5min / 15min) ─────────────────────────

@pytest.mark.parametrize("vol,expected", [(1000.0, False), (2000.0, False), (3000.0, True)])
def test_traded_volume_still_uses_the_ratio_floor(vol, expected):
    m = _metrics(_frame(prior_vol=1000.0, last_vol=vol), tf="5min")
    assert m is not None and m["volume_basis"] == "TRADED"
    assert _confirm(m, tf="5min") is expected


def test_a_traded_reading_is_never_judged_on_percentile():
    """2.0x is below the 2.2x floor but sits at the top of its window; if the percentile
    rule leaked into the traded path it would wrongly confirm."""
    m = _metrics(_frame(prior_vol=1000.0, last_vol=2000.0), tf="5min")
    assert m["surge_percentile"] >= 97.0
    assert _confirm(m, tf="5min") is False


# ── everything the gate already enforced still applies ───────────────────────

def test_high_burst_only_still_refuses_weak_context():
    m = _metrics(_frame(last_ticks=220))
    assert _confirm(m, adx=15.0) is False, "a 1min scalp entered without strong context"


def test_range_expansion_is_still_required():
    m = _metrics(_frame(last_ticks=220, expand=1.0))    # no expansion
    assert m is not None
    assert _confirm(m) is False


def test_it_abstains_when_neither_basis_is_measurable():
    df = _frame(last_ticks=220)
    df = df.drop(columns=["tick_count"])
    assert _metrics(df) is None


def test_the_proxy_can_be_switched_off(_pinned):
    _pinned.surge_tick_proxy_enabled = False
    assert _metrics(_frame(last_ticks=220)) is None


# ── the calibration this rests on ────────────────────────────────────────────

def test_the_floor_is_the_calibrated_value():
    assert float(get_settings().surge_tick_percentile_floor) == pytest.approx(97.0)


def test_percentile_rank_is_uniform_and_so_basis_independent():
    """The property the whole fix rests on: for ANY continuous series, the fraction of
    bars above the Nth percentile of their own window is ~(100-N)%. Volume and ticks
    have very different shapes; this does not care."""
    rs = np.random.RandomState(1)
    for draw in (lambda n: rs.lognormal(0, 1.4, n),      # heavy tailed, like volume
                 lambda n: rs.poisson(120, n).astype(float)):  # near-Poisson, like ticks
        a = draw(20000)
        hits = 0
        for i in range(120, len(a), 7):
            w = a[i - 120:i]
            if 100.0 * float((w < a[i]).mean()) >= 97.0:
                hits += 1
        rate = 100.0 * hits / len(range(120, len(a), 7))
        assert 1.5 <= rate <= 6.0, f"rank>=97 admitted {rate:.2f}% — not basis-independent"
