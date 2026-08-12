"""One entry cost Rs.10,402 -- ten times the entire two-week book.

From rtracker, 2026-07-28:

    signal_timestamp  09:10:00       <- before the 09:15 open
    entry_timestamp   09:15:34.516   entry mark 131.70
    exit_timestamp    09:15:34.556   exit       52.30    <- 40 milliseconds later
    NIFTY 24000PE  2 lots x 65   exit_reason EARLY_LOSS_CUT   pnl -10,402

An option cannot round-trip -60% in 40ms of real fills. The mark was the previous
close, which is what an option's LTP still reads until its first trade of the day
prints; the first live tick then repriced the position and EARLY_LOSS_CUT booked the
gap. `_option_premium_quote_plausible` could not see it -- that function bounds
premiums only from ABOVE (spot*0.12 ~ Rs.2,880 on NIFTY), because its job is catching
an index price leaking into premium space, not a stale-but-in-range mark.

Two independent nets, because neither subsumes the other: the signal-freshness check
needs no quote and so still covers the open, while the mark check catches a stale mark
at any hour. Across the 12-session window the freshness net blocks exactly 2 trades,
both losers, together -Rs.10,866 -- so it costs no winning trade in the measured book.
"""
from datetime import datetime, time as dtime

import pytest
import pytz

from trading.trade_manager import TradeManager

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _pinned_settings():
    """The settings singleton leaks between test files in this suite."""
    from config.settings import get_settings

    s = get_settings()
    keys = (
        "entry_preopen_signal_guard_enabled",
        "entry_mark_touch_validation_enabled",
        "entry_mark_max_ask_multiple",
        "entry_mark_min_bid_multiple",
        "real_mode_live_start",
    )
    saved = {k: getattr(s, k, None) for k in keys}
    setattr(s, "entry_preopen_signal_guard_enabled", True)
    setattr(s, "entry_mark_touch_validation_enabled", True)
    setattr(s, "entry_mark_max_ask_multiple", 1.50)
    setattr(s, "entry_mark_min_bid_multiple", 0.50)
    setattr(s, "real_mode_live_start", "09:15:00")
    yield
    for k, v in saved.items():
        if v is not None:
            setattr(s, k, v)


def _tm():
    return TradeManager.__new__(TradeManager)


def _admit(**kw):
    kw.setdefault("inst_type", "OPT")
    kw.setdefault("price", 100.0)
    kw.setdefault("signal_time", None)
    kw.setdefault("entry_time", None)
    kw.setdefault("touch_quote", None)
    kw.setdefault("trading_symbol", "NIFTY28JUL2624000PE")
    kw.setdefault("is_recovery", False)
    return TradeManager._entry_mark_admission(_tm(), **kw)


# ── (a) the signal predates the open ─────────────────────────────────────────

def test_the_2026_07_28_trade_is_refused():
    ok, why = _admit(
        signal_time=datetime(2026, 7, 28, 9, 10, 0),
        entry_time=datetime(2026, 7, 28, 9, 15, 34, 516738),
        price=131.70,
    )
    assert ok is False
    assert "predates" in why


def test_the_other_preopen_trade_in_the_window_is_refused():
    """2026-07-31 MIDCPNIFTY, signal 09:00:00, entry 09:16:04, -Rs.464."""
    ok, _ = _admit(
        signal_time=datetime(2026, 7, 31, 9, 0, 0),
        entry_time=datetime(2026, 7, 31, 9, 16, 4),
    )
    assert ok is False


@pytest.mark.parametrize("sig_h,sig_m", [(9, 15), (9, 20), (11, 30), (14, 5), (15, 10)])
def test_every_in_session_signal_is_admitted(sig_h, sig_m):
    """The guard must not touch a single ordinary intraday entry."""
    ok, why = _admit(
        signal_time=datetime(2026, 7, 28, sig_h, sig_m, 0),
        entry_time=datetime(2026, 7, 28, sig_h, sig_m, 30),
    )
    assert ok is True, why


def test_a_preopen_signal_entered_preopen_is_untouched():
    """The guard is about acting AT the open on pre-open data, not about pre-open
    bookkeeping — a paper/premarket entry stamped before the open is left alone."""
    ok, _ = _admit(
        signal_time=datetime(2026, 7, 28, 9, 5, 0),
        entry_time=datetime(2026, 7, 28, 9, 10, 0),
    )
    assert ok is True


def test_a_previous_day_signal_is_not_caught_by_this_net():
    """Scoped to same-day deliberately; overnight carry is a different guard's job and
    a date-blind rule would refuse every legitimate morning re-entry."""
    ok, _ = _admit(
        signal_time=datetime(2026, 7, 27, 9, 10, 0),
        entry_time=datetime(2026, 7, 28, 9, 15, 34),
    )
    assert ok is True


def test_tz_aware_and_naive_stamps_both_work():
    ok, _ = _admit(
        signal_time=IST.localize(datetime(2026, 7, 28, 9, 10, 0)),
        entry_time=IST.localize(datetime(2026, 7, 28, 9, 15, 34)),
    )
    assert ok is False


def test_the_open_time_follows_the_setting():
    from config.settings import get_settings

    setattr(get_settings(), "real_mode_live_start", "10:00:00")
    ok, _ = _admit(
        signal_time=datetime(2026, 7, 28, 9, 30, 0),
        entry_time=datetime(2026, 7, 28, 10, 5, 0),
    )
    assert ok is False, "the guard ignored a reconfigured session open"


# ── (b) the mark disagrees with the live book ────────────────────────────────

def _q(bid, ask, usable=True):
    return {"bid": bid, "ask": ask, "usable": usable, "source": "ws", "age_seconds": 0.3}


def test_the_stale_mark_is_refused_against_the_live_book():
    """131.70 against a real market of 52.30 is 2.5x the offer."""
    ok, why = _admit(price=131.70, touch_quote=_q(52.10, 52.50))
    assert ok is False
    assert "stale mark" in why


@pytest.mark.parametrize("mark", [52.50, 55.0, 60.0, 70.0, 78.0])
def test_normal_spread_noise_is_admitted(mark):
    """Anything inside 1.5x the offer trades as before."""
    ok, why = _admit(price=mark, touch_quote=_q(52.10, 52.50))
    assert ok is True, why


def test_a_mark_far_below_the_bid_is_also_refused():
    ok, why = _admit(price=20.0, touch_quote=_q(52.10, 52.50))
    assert ok is False and "stale mark" in why


def test_an_unusable_quote_does_not_refuse():
    """No opinion is not evidence of a bad mark — that is what net (a) is for."""
    ok, _ = _admit(price=131.70, touch_quote=_q(52.10, 52.50, usable=False))
    assert ok is True


@pytest.mark.parametrize("quote", [None, {}, {"usable": True}, {"bid": 0, "ask": 0, "usable": True},
                                   {"bid": 60.0, "ask": 50.0, "usable": True}])
def test_a_malformed_or_crossed_quote_never_blocks_trading(quote):
    ok, _ = _admit(price=131.70, touch_quote=quote)
    assert ok is True


def test_futures_are_not_mark_validated():
    """The touch quote is built for OPT only; FUT carries no bid/ask here."""
    ok, _ = _admit(inst_type="FUT", price=131.70, touch_quote=_q(52.10, 52.50))
    assert ok is True


# ── scope and switches ───────────────────────────────────────────────────────

def test_recovery_entries_bypass_both_nets():
    """Matches _entry_risk_admission: refusing a recovery strands live exposure."""
    ok, _ = _admit(
        signal_time=datetime(2026, 7, 28, 9, 10, 0),
        entry_time=datetime(2026, 7, 28, 9, 15, 34),
        price=131.70, touch_quote=_q(52.10, 52.50), is_recovery=True,
    )
    assert ok is True


def test_each_net_can_be_switched_off_independently():
    from config.settings import get_settings

    s = get_settings()
    setattr(s, "entry_preopen_signal_guard_enabled", False)
    ok, _ = _admit(
        signal_time=datetime(2026, 7, 28, 9, 10, 0),
        entry_time=datetime(2026, 7, 28, 9, 15, 34), price=131.70,
    )
    assert ok is True, "pre-open net ignored its toggle"

    setattr(s, "entry_preopen_signal_guard_enabled", True)
    setattr(s, "entry_mark_touch_validation_enabled", False)
    ok, _ = _admit(price=131.70, touch_quote=_q(52.10, 52.50))
    assert ok is True, "mark net ignored its toggle"


def test_the_nets_are_independent():
    """A stale mark with an in-session signal is still refused, and a pre-open signal
    with a healthy mark is still refused. Neither net hides behind the other."""
    ok, _ = _admit(
        signal_time=datetime(2026, 7, 28, 11, 0, 0),
        entry_time=datetime(2026, 7, 28, 11, 0, 5),
        price=131.70, touch_quote=_q(52.10, 52.50),
    )
    assert ok is False, "mark net did not fire on an in-session signal"

    ok, _ = _admit(
        signal_time=datetime(2026, 7, 28, 9, 10, 0),
        entry_time=datetime(2026, 7, 28, 9, 15, 34),
        price=52.30, touch_quote=_q(52.10, 52.50),
    )
    assert ok is False, "freshness net did not fire on a healthy mark"


# ── the guard is actually reached by every entry ─────────────────────────────

def test_open_trade_calls_the_admission():
    import inspect

    src = inspect.getsource(TradeManager.open_trade)
    assert "_entry_mark_admission(" in src, "open_trade no longer validates the mark"
    idx = src.index("_entry_mark_admission(")
    assert "return None" in src[idx:idx + 700], "a refused mark does not stop the entry"
    # Ahead of the broker call, or the order is already gone.
    assert idx < src.index("Trade("), "mark validation runs after the trade is built"


def test_the_plausibility_check_still_cannot_catch_this():
    """If _option_premium_quote_plausible ever gains a live-book cross-check this guard
    becomes belt-and-braces rather than the only defense — worth knowing."""
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner._option_premium_quote_plausible)
    assert "bid" not in src and "ask" not in src
