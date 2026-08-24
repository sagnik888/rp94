"""The winner-side min-age guard must survive the candle clock, like the loss side does.

scanner.py:28022-28026 sets `latest_time` to the WALL clock in live mode, and then
scanner.py:28217 unconditionally overwrites it with `self.candles.get_max_timestamp()`
— the CANDLE clock — before every exit decision in the active-trade loop.

For an intracandle entry the fill is stamped after the bar it fired inside, so
`_trade_like_age_seconds(trade_like, candle_clock)` sees a clock BEHIND the entry and
correctly refuses to invent an age (scanner.py:14047-14049 returns None on age < 0).

Both loss-side holds already recover from that by re-asking the wall clock:

    scanner.py:14226  (LOSS_THREAT min-hold)
    scanner.py:16598  (EARLY_LOSS_CUT min-hold)

    if held is None and str(getattr(self, "mode", "") or "").upper() != "HISTORICAL":
        held = self._trade_like_age_seconds(trade_like, datetime.now(IST))

The winner-side guard at scanner.py:14150 does not, so it silently no-ops in the live
lane on exactly the rows it exists to protect — the seconds-old position whose "peak"
is an opening synthetic mark. The asymmetry runs the wrong way: losses are held back
15-60s while winners may be cut instantly.

test_peak_threat_winner_min_age_2026_08_13.py passes `latest_time=now`, a
wall-clock-consistent stamp, so it never reaches this path.

An age unknowable from BOTH clocks must still not block — the
never-conclude-from-absence rule is unchanged.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from scanner import IST, Scanner


def _sc(mode="REAL"):
    sc = Scanner.__new__(Scanner)
    sc.mode = mode
    sc._clear_loss_threat_confirmation = lambda *_a, **_k: None
    return sc


def _winner(entry_time):
    """A winner comfortably past the peak arm and retaining it — the profit branch."""
    return SimpleNamespace(
        instrument="BANKNIFTY", timeframe="5min", inst_type="OPT",
        option_type="CE", direction="LONG", lots=1, lot_size=30,
        entry_time=entry_time,
        grade="A", runner_mode=False, volume_ratio=0.0,
    )


def _decide(sc, trade, latest_time):
    return sc._peak_threat_exit_decision(
        trade,
        current_price=742.0,
        current_pnl_rs=1300.0,
        peak_pnl=1400.0,     # retention 93%, comfortably past min_retention
        initial_risk_rs=820.0,
        latest_time=latest_time,
        regime="VOLATILE",
        inst_type="OPT",
        option_type="CE",
        direction="LONG",
    )


def _intracandle(age_seconds):
    """A fill `age_seconds` old by the wall clock, on a bar stamped before it.

    Both stamps hang off the real clock so the fallback measures a real elapsed
    time; the candle stamp sits a minute behind the fill, which is what an
    intracandle entry looks like to the active-trade loop.
    """
    entry = datetime.now(IST).replace(tzinfo=None) - timedelta(seconds=age_seconds)
    candle_clock = entry - timedelta(seconds=60)
    return entry, candle_clock


def test_the_candle_clock_does_not_disarm_the_winner_hold():
    """The bug: age is None off the candle clock, so the guard never runs."""
    entry, candle_clock = _intracandle(5)
    sc = _sc()

    # The premise: genuinely unknowable from the candle clock alone.
    assert Scanner._trade_like_age_seconds(_winner(entry), candle_clock) is None

    fired, _reason, detail = _decide(sc, _winner(entry), candle_clock)

    assert fired is False
    assert "post-entry tape" in detail


def test_a_winner_old_by_the_wall_clock_is_still_judged_normally():
    """The fallback must not block a position that really is old enough.

    600s clears the 45s base minimum and the 120s opening-window one alike, so the
    assertion does not depend on what time of day the suite runs.
    """
    entry, candle_clock = _intracandle(600)
    sc = _sc()

    _fired, _reason, detail = _decide(sc, _winner(entry), candle_clock)

    assert "post-entry tape" not in detail


def test_a_replay_keeps_the_candle_answer():
    """In HISTORICAL "now" is meaningless — the loss-side guards say so too."""
    entry, candle_clock = _intracandle(5)
    sc = _sc(mode="HISTORICAL")

    _fired, _reason, detail = _decide(sc, _winner(entry), candle_clock)

    assert "post-entry tape" not in detail


def test_an_age_unknowable_from_both_clocks_still_never_blocks():
    """No entry stamp at all: neither clock can answer, and absence must not block."""
    _entry, candle_clock = _intracandle(5)
    sc = _sc()

    _fired, _reason, detail = _decide(sc, _winner(None), candle_clock)

    assert "post-entry tape" not in detail


def test_the_toggle_still_restores_the_previous_behaviour(monkeypatch):
    """The fallback lives inside the existing switch, not around it."""
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "peak_threat_exit_min_age_enabled", False)
    entry, candle_clock = _intracandle(5)
    sc = _sc()

    _fired, _reason, detail = _decide(sc, _winner(entry), candle_clock)

    assert "post-entry tape" not in detail
