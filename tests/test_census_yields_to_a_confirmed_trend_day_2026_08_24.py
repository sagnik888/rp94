"""A confirmed trend day outranks the session's own signal census.

`directional_census_block` counts the directions THIS SCANNER emitted
(`scanner._dir_census[signal_direction] += 1`, engine/signal_processor.py:5406). It
never looks at price. When one side dominates, the other side must clear an elevated
confidence floor.

2026-08-24 shows why that is not safe on its own. Every index fell — BANKNIFTY -582,
SENSEX -428, NIFTY -141 — and the scanner correctly generated 53 SHORT signals against
45 LONG. But the morning had skewed long, so the afternoon census read 18L/9S and the
system refused shorts as "counter-bias" while simultaneously holding:

    14:40  BANKNIFTY SHORT conf 0.42   day_type='TREND_DAY_DOWN' bias='BEARISH' conf=0.92
           -> "Counter-session-bias SHORT (session 18L/9S) needs >= 60%, got 42%"
    14:45  BANKNIFTY SHORT conf 0.38   day_type='TREND_DAY_DOWN' bias='BEARISH' conf=0.92
    15:05  BANKNIFTY SHORT conf 0.59   day_type='TREND_DAY_DOWN' bias='BEARISH' conf=0.92
    15:10  BANKNIFTY SHORT conf 0.59   day_type='TREND_DAY_DOWN' bias='BEARISH' conf=0.90

It believed it was a bearish trend day at 92% confidence AND that selling was the
counter-bias side. Those cannot both be right.

This is the mirror of a case the file already handles. session_trend_guard_census_yield
(engine/signal_processor.py:111) exists because on 2026-07-08 a stale TREND_DAY_UP LABEL
vetoed six midcap SELLs while the census was already 5L/10S bearish. There the census was
fresh and the label stale; here the label is fresh and the CENSUS is stale. Same failure,
opposite direction, so the guard yields the same way.

Deliberately narrow: only a CONFIRMED trend day (TREND_DAY_UP / TREND_DAY_DOWN) whose
direction AGREES with the signal waives the penalty, and only above a confidence floor on
the day-type itself. RANGE_DAY and UNDECIDED change nothing — on 2026-08-24 the 15:00
MIDCPNIFTY short carried day_type='RANGE_DAY' and stays blocked.
"""
import pytest
import inspect

from config.settings import Settings, get_settings
from engine.signal_processor import SignalProcessor, directional_census_block


LONG_HEAVY = {"LONG": 18, "SHORT": 9}


def _block(direction, conf, day_type="", day_conf=0.92, settings=None):
    s = settings or get_settings()
    return directional_census_block(
        s,
        LONG_HEAVY,
        signal_direction=direction,
        confidence=conf,
        grade_floor=0.55,
        day_type=day_type,
        day_type_confidence=day_conf,
    )


def test_the_2026_08_24_short_is_no_longer_penalised():
    """day_type TREND_DAY_DOWN at 0.92 agrees with SHORT -> census yields."""
    assert _block("SHORT", 0.42, day_type="TREND_DAY_DOWN") == ""
    assert _block("SHORT", 0.59, day_type="TREND_DAY_DOWN") == ""


def test_without_a_trend_day_the_penalty_still_applies():
    """The guard is not removed, only outranked. This is the pre-fix behaviour."""
    msg = _block("SHORT", 0.42, day_type="")
    assert "Counter-session-bias" in msg
    assert "18L/9S" in msg


def test_a_range_day_does_not_waive_it():
    """2026-08-24 15:00 MIDCPNIFTY carried RANGE_DAY and must stay blocked."""
    assert "Counter-session-bias" in _block("SHORT", 0.52, day_type="RANGE_DAY")
    assert "Counter-session-bias" in _block("SHORT", 0.52, day_type="UNDECIDED")


def test_a_trend_day_pointing_the_OTHER_way_does_not_waive_it():
    """A bullish trend day must not excuse a counter-bias SHORT."""
    assert "Counter-session-bias" in _block("SHORT", 0.42, day_type="TREND_DAY_UP")


def test_a_low_confidence_day_label_does_not_waive_it():
    """A stale/unsure label must not be able to switch the guard off."""
    assert "Counter-session-bias" in _block(
        "SHORT", 0.42, day_type="TREND_DAY_DOWN", day_conf=0.30
    )


def test_the_dominant_side_is_unaffected():
    """LONG is the dominant side here; it was never penalised and still is not."""
    assert _block("LONG", 0.10, day_type="") == ""


def test_the_toggle_restores_the_previous_behaviour(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "directional_census_trend_day_yield_enabled", False)
    assert "Counter-session-bias" in _block(
        "SHORT", 0.42, day_type="TREND_DAY_DOWN", settings=s
    )


def test_it_ships_enabled():
    assert Settings().directional_census_trend_day_yield_enabled is True


def test_historical_census_receives_the_same_day_type_context_as_live():
    """The replay call must not silently omit the live waiver context."""
    src = inspect.getsource(SignalProcessor.process_best_signal)
    assert "day_type=_hist_day_type" in src
    assert "day_type_confidence=_hist_day_conf" in src

    live = _block("SHORT", 0.42, day_type="TREND_DAY_DOWN", day_conf=0.92)
    replay = directional_census_block(
        get_settings(),
        LONG_HEAVY,
        signal_direction="SHORT",
        confidence=0.42,
        grade_floor=0.55,
        day_type="TREND_DAY_DOWN",
        day_type_confidence=0.92,
    )
    assert replay == live == ""
