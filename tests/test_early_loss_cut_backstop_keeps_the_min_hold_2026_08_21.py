"""The EARLY_LOSS_CUT backstop must honour the same minimum hold as the scanner path.

`_early_loss_cut_hit` (trading/trade_manager.py:5700) is pure arithmetic — it reads no
clock, no peak and no cost. It has two callers:

    scanner.py:16658            inside _loss_guard_decision, which since 2026-08-16
                                carries a 15s minimum hold (scanner.py:16589-16601),
                                a give-back deferral and a cost floor.

    trading/trade_manager.py:5639   inside update_trade, with none of them.

update_trade is called from the monitor loop at scanner.py:28956 and closes the trade
immediately at :5640, so the unguarded caller PRE-EMPTS the guarded one. The three
guards added on 2026-08-16 therefore do not bind in the live lane at all.

test_early_loss_cut_guards_2026_08_16.py exercises Scanner._loss_guard_decision only,
so it cannot see this path.

The hold is deliberately short (15s, not LOSS_THREAT's 60s): per that test's own
docstring this exit "exists to be fast, so LOSS_THREAT_EXIT's 60s would disable it,
not guard it". The R thresholds and the hard stop are untouched here — worst case per
trade is exactly what it was.

Never-conclude-from-absence is preserved: an unknown entry stamp does not block, and
a replay keeps the old behaviour because "now" is meaningless there.
"""
import datetime as dt
from types import SimpleNamespace

import pytz

from config.settings import get_settings
from trading.trade_manager import TradeManager

IST = pytz.timezone("Asia/Kolkata")


def _mgr(mode="REAL"):
    m = TradeManager.__new__(TradeManager)
    m.mode = mode
    return m


def _trade(age_seconds):
    entry = (
        None
        if age_seconds is None
        else datetime_now() - dt.timedelta(seconds=age_seconds)
    )
    return SimpleNamespace(
        instrument="BANKNIFTY", inst_type="OPT", direction="LONG",
        entry_time=entry, entry_filled_at=None,
    )


def datetime_now():
    return dt.datetime.now(IST)


def test_a_seconds_old_loser_is_not_cut_by_the_backstop():
    """p10 of the measured cuts was 2.0s; this is the path that produced them."""
    assert _mgr()._early_loss_cut_min_hold_met(_trade(3)) is False


def test_a_loser_past_the_hold_is_cut_normally():
    assert _mgr()._early_loss_cut_min_hold_met(_trade(60)) is True


def test_the_boundary_is_the_configured_hold():
    min_hold = float(getattr(get_settings(), "early_loss_cut_min_hold_seconds", 15.0))
    assert _mgr()._early_loss_cut_min_hold_met(_trade(min_hold - 2)) is False
    assert _mgr()._early_loss_cut_min_hold_met(_trade(min_hold + 2)) is True


def test_a_replay_keeps_the_old_behaviour():
    """In HISTORICAL "now" is meaningless — the scanner-side hold says so too."""
    assert _mgr(mode="HISTORICAL")._early_loss_cut_min_hold_met(_trade(3)) is True


def test_an_unknown_entry_stamp_never_blocks():
    """Refusing on absence would disarm the backstop for any row without a stamp."""
    assert _mgr()._early_loss_cut_min_hold_met(_trade(None)) is True


def test_a_clock_behind_the_entry_never_blocks():
    """Direction-safety: an inconsistent clock may not manufacture a young age."""
    assert _mgr()._early_loss_cut_min_hold_met(_trade(-120)) is True


def test_disabling_the_hold_restores_the_previous_behaviour(monkeypatch):
    monkeypatch.setattr(get_settings(), "early_loss_cut_min_hold_seconds", 0.0)
    assert _mgr()._early_loss_cut_min_hold_met(_trade(1)) is True


def test_the_backstop_call_site_consults_the_hold():
    """The helper is worthless if update_trade does not ask it.

    A source check is weak on its own, but the runtime behaviour above is already
    covered; this pins that the guard is wired into the caller that pre-empts the
    scanner path, which is the whole point of the fix.
    """
    import inspect

    src = inspect.getsource(TradeManager.update_trade)
    assert "_early_loss_cut_hit" in src, "the backstop moved; re-point this test"
    hold_at = src.find("_early_loss_cut_min_hold_met")
    cut_at = src.find("_early_loss_cut_hit")
    assert hold_at != -1, "update_trade does not consult the minimum hold"
    # The hold must be asked FIRST, so a young position short-circuits before the
    # arithmetic predicate can close it.
    assert hold_at < cut_at
