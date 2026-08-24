"""ADX remembers; the Efficiency Ratio observes. The regime must hear both.

2026-08-19, the clean demonstration. MIDCPNIFTY made a genuine move 09:15-10:30 and the
engine caught it — the session's ONLY winner, +Rs.2,806. The tape then died: from 10:30
to 13:41 it held a 41-point range (0.28%) with roughly 90 one-minute direction flips.
Kaufman ER across that window ran 0.002-0.039 on the four indices; BANKNIFTY travelled
204 points to arrive 3.75 higher.

ADX did not notice, because ADX is an EMA-smoothed memory. On MIDCPNIFTY 5m it still
read 51-58, describing the move that had already finished. Every regime decision keys
on that number:

    adx >= 20  ->  TRENDING_UP
    adx >= 20  ->  a 60-bar band touch becomes BREAKOUT_UP at confidence 70
    adx >= 35  ->  the confirmed-range-day guard steps aside
    adx >= 20  ->  the ER promotion is skipped, so ER is never consulted at all

The last line is the structural defect: ER could only ever make the regime MORE
directional. There was no path by which a low ER made it less. So the engine entered
MIDCPNIFTY 14850CE five times at A+ and 93-96% confidence for -Rs.1,900, with ER at
those entries measuring 0.070, 0.135, 0.097, 0.013 and 0.201.

Measured over every recorded lifecycle (312 positions, 25 sessions, 2026-07-08..08-19),
refusing entries whose own-timeframe ER sat below the floor:

    floor 0.10  +Rs. 7,425      floor 0.25  +Rs. 7,231
    floor 0.15  +Rs.12,934      floor 0.30  +Rs. 6,118
    floor 0.20  +Rs. 8,653      floor 0.35  -Rs. 2,058

0.15 ships: 14 sessions better, 7 worse, worst single session -Rs.2,084, and the blocked
population splits 18 wins / 46 losses against 105 / 143 for the kept one.

The floor is deliberately LOW and must stay that way. Winner and loser ER distributions
overlap heavily (medians 0.285 vs 0.242), so ER is NOT a classifier and using it as one
destroys the edge — by 0.35 the sweep is already negative. It is a tail filter: its job
is to name tape that is provably going nowhere, and nothing more.
"""
import numpy as np
import pandas as pd
import pytest

from config.settings import Settings, get_settings
from engine.market_regime import MarketRegime
from intelligence.regime_detector import RegimeDetector


# ── shipped values ───────────────────────────────────────────────────────────

def test_the_veto_ships_off_pending_a_deliberate_enable():
    """Measured positive (+Rs.13,338 over 25 sessions) but NOT armed yet.

    On 2026-08-19 the dashboard went to unknown data shortly after this landed. The
    cause turned out to be an unrelated, long-standing candle-cache timezone bug (all
    18 occurrences sit within 16-501s of a restart, including three restarts that
    predate this change) — but that was established afterwards, not at the time, and
    the safe move mid-session was to make this inert rather than argue about it.

    It stays off until the tz fix has run through a restart and someone turns this on
    deliberately. Flip this assertion and the default together, never the default alone.
    """
    assert Settings.model_fields["regime_er_veto_enabled"].default is False


def test_the_floor_is_the_measured_one_and_is_low():
    floor = Settings.model_fields["regime_er_veto_floor"].default
    assert floor == 0.15
    # Guard the reasoning, not just the number: above the loser median (0.242) the
    # sweep turns negative, because ER stops being a tail filter and starts being
    # used as a classifier it is not good enough to be.
    assert floor < 0.24, "a floor at or above the loser median destroys the measured edge"


def test_the_range_day_adx_escape_ships_off_with_the_veto():
    """Armed or disarmed together — half of this change is not a coherent state."""
    assert Settings.model_fields["regime_range_day_adx_override_needs_er"].default is False
    assert (
        Settings.model_fields["regime_range_day_adx_override_needs_er"].default
        == Settings.model_fields["regime_er_veto_enabled"].default
    ), "the veto and its range-day counterpart must ship in the same state"


# ── the veto itself ──────────────────────────────────────────────────────────

def _guard(regime, adx, er, **over):
    # The behaviour of the rule is tested independently of whether it ships armed;
    # the shipped default has its own test above.
    over.setdefault("regime_range_day_adx_override_needs_er", True)
    over.setdefault("regime_er_veto_enabled", True)
    st = get_settings()
    saved = {k: getattr(st, k) for k in over}
    for k, v in over.items():
        setattr(st, k, v)
    try:
        return RegimeDetector._range_day_guarded_regime(
            RegimeDetector.__new__(RegimeDetector),
            settings=st,
            session_context={"day_type": "RANGE_DAY", "confidence": 0.9},
            regime=regime,
            adx_val=adx,
            er_value=er,
        )
    finally:
        for k, v in saved.items():
            setattr(st, k, v)


def test_a_strong_adx_no_longer_escapes_the_range_day_guard_on_its_own():
    """The 2026-08-19 MIDCPNIFTY case: ADX 55 inside a 41-point box, ER 0.07."""
    assert _guard(MarketRegime.TRENDING_UP.value, adx=55.0, er=0.07) is True


def test_a_strong_adx_still_escapes_when_the_tape_is_really_travelling():
    """The 2026-07-24 audit case this escape was built for must survive."""
    assert _guard(MarketRegime.TRENDING_UP.value, adx=55.0, er=0.62) is False


def test_an_unmeasurable_er_keeps_the_old_escape_behaviour():
    """Absence of evidence is not evidence of chop — notably at the open."""
    assert _guard(MarketRegime.TRENDING_UP.value, adx=55.0, er=float("nan")) is False


def test_the_er_requirement_is_toggleable():
    assert _guard(
        MarketRegime.TRENDING_UP.value, adx=55.0, er=0.07,
        regime_range_day_adx_override_needs_er=False,
    ) is False


def test_a_ranging_label_is_not_this_guards_business():
    assert _guard(MarketRegime.RANGING.value, adx=55.0, er=0.07) is False


# ── end to end through detect() ──────────────────────────────────────────────

def _series(prices):
    idx = pd.date_range("2026-08-19 09:15", periods=len(prices), freq="5min")
    p = np.asarray(prices, dtype=float)
    return pd.DataFrame(
        {"open": p, "high": p * 1.0006, "low": p * 0.9994, "close": p,
         "volume": np.full(len(p), 1000.0)},
        index=idx,
    )


def _dead_tape(n=120, base=14850.0):
    """A 41-point box that goes nowhere — 2026-08-19 MIDCPNIFTY after 10:30."""
    rng = np.random.default_rng(7)
    return _series(base + rng.uniform(-20, 20, n))


def _real_trend(n=120, base=14850.0):
    return _series(base + np.arange(n) * 3.0)


def _detect(df):
    d = RegimeDetector()
    return d.detect(df, instrument="MIDCPNIFTY", timeframe="5min")


def test_a_dead_tape_does_not_come_back_trending_or_breaking_out():
    out = _detect(_dead_tape())
    assert out["regime"] not in {
        MarketRegime.TRENDING_UP.value, MarketRegime.TRENDING_DOWN.value,
        MarketRegime.BREAKOUT_UP.value, MarketRegime.BREAKOUT_DOWN.value,
    }, f"a 40-point random box was labelled {out['regime']} (ER {out.get('kaufman_er')})"


def test_a_real_trend_is_still_a_trend():
    """The veto must not cost the system genuine directional moves — that is the
    failure mode the range-day ADX escape was added for in the first place."""
    out = _detect(_real_trend())
    assert out["regime"] in {
        MarketRegime.TRENDING_UP.value, MarketRegime.BREAKOUT_UP.value,
    }, f"a clean 360-point advance was labelled {out['regime']}"


def test_the_veto_is_reported_not_silent():
    """A demotion that money decisions read must be visible in telemetry."""
    out = _detect(_dead_tape())
    assert "er_veto" in out and "er_veto_reason" in out
    if out["er_veto"]:
        assert "er_veto_" in out["er_veto_reason"]
        assert "adx" in out["er_veto_reason"]


def test_turning_the_veto_off_restores_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(get_settings(), "regime_er_veto_enabled", False)
    out = _detect(_dead_tape())
    assert out["er_veto"] is False


def test_kaufman_er_is_still_reported():
    out = _detect(_dead_tape())
    assert out.get("kaufman_er") is not None
