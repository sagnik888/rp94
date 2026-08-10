"""Three findings from the 28-point audit, two of them narrower than reported and one wider.

1. A GHOST ROW ABSORBED THE LIVE CASCADE ADD.
   `_offer_cascade_add` matched open trades on (base index, timeframe, direction) with no
   is_ghost filter and RETURNED on its first match. Reconstructed session-ledger rows are
   inserted into open_trades with status OPEN at startup, and dict order puts them ahead
   of anything opened later — so a matching ghost consumed the single cascade offer and
   the live position behind it was never asked, while the log reported a successful add.

   Every sibling guard already filters ghosts (_opposite_direction_stack_block,
   _is_live_book_trade, _bucket_open_count, _effective_max_positions); this loop was the
   one that did not.

   Deliberately NOT fixed by moving the return out of the loop so it tries every match:
   adding to two trades from one duplicate signal is worse than adding to none, and an
   existing test pins first-match-only.

2. THE "ONCE PER DAY" STOP-OUT RE-ENTRY CAP WAS ONCE PER PROCESS LIFETIME.
   The report said the allowance is consumed by the eligibility check before any order is
   placed. True — but the larger half is that `_stop_out_reentries` was never cleared by
   `_reset_daily_counters`, which resets _trades_today, _losses_today, _last_exit_time,
   _counted_entry_keys and _counted_outcome_signatures but not this dict. A process kept
   alive across sessions had stop-out re-entry permanently disabled on any index that
   used it once, despite the docstring promising a per-day cap.

3. KITE ORDER CAPABILITY IS LATCHED AT CONNECT.
   `provider.is_connected` is a plain attribute assigned at connect()/rebind_kite() and
   never revoked, so a token invalidated mid-session leaves it True forever while every
   order is refused.

   NOT folded into `kite_health`, which is what the report implies. kite_health feeds
   all_brokers_unavailable, which gates REAL mode — tightening a trading gate on an
   observability defect risks refusing to trade through a transient auth blip, and the
   existing tests document "the stream being quiet must never read as broker down" as
   deliberate. The live value is published alongside instead.
"""
import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

from scanner import Scanner

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


# ── 1. the cascade add skips ghosts ──────────────────────────────────────────

def _offer_scanner(open_trades, adder):
    s = Scanner.__new__(Scanner)
    s.trades = SimpleNamespace(open_trades=open_trades, scale_in_trade=adder)
    s.log_event = lambda *a, **k: None
    s._diag_inc = lambda *a, **k: None
    return s


def _row(tid, ghost, instrument="NIFTY", tf="5min", direction="LONG"):
    return SimpleNamespace(
        instrument=instrument, timeframe=tf, direction=direction, is_ghost=ghost,
        current_price=120.0, entry_price=118.0, trading_symbol=f"SYM{tid}",
    )


def _cand(instrument="NIFTY", tf="5min", direction="LONG"):
    return SimpleNamespace(instrument=instrument, timeframe=tf, direction=direction)


@pytest.mark.parametrize("index", INDICES)
def test_a_ghost_ahead_of_the_live_row_does_not_absorb_the_add(index):
    """The headline: the live position must get the lot, on every index."""
    asked = []
    trades = {"G1": _row("G1", True, index), "T1": _row("T1", False, index)}
    s = _offer_scanner(trades, lambda tid, *a, **k: (asked.append(tid), (True, ""))[1])
    ok, _ = Scanner._offer_cascade_add(s, _cand(index))
    assert ok is True
    assert asked == ["T1"], f"the ghost consumed the offer: {asked}"


def test_a_ghost_only_book_offers_nothing():
    asked = []
    s = _offer_scanner({"G1": _row("G1", True)},
                       lambda tid, *a, **k: (asked.append(tid), (True, ""))[1])
    ok, _ = Scanner._offer_cascade_add(s, _cand())
    assert asked == [], "a reconstructed row was offered a real lot"
    assert ok is False


def test_a_live_book_is_unaffected():
    asked = []
    s = _offer_scanner({"T1": _row("T1", False)},
                       lambda tid, *a, **k: (asked.append(tid), (True, ""))[1])
    assert Scanner._offer_cascade_add(s, _cand())[0] is True
    assert asked == ["T1"]


def test_only_the_first_live_match_is_offered():
    """Adding to two trades from one duplicate signal is worse than adding to none."""
    asked = []
    trades = {"T1": _row("T1", False), "T2": _row("T2", False)}
    s = _offer_scanner(trades, lambda tid, *a, **k: (asked.append(tid), (True, ""))[1])
    Scanner._offer_cascade_add(s, _cand())
    assert asked == ["T1"]


def test_a_row_with_no_is_ghost_attribute_is_treated_as_live():
    asked = []
    row = SimpleNamespace(instrument="NIFTY", timeframe="5min", direction="LONG",
                          current_price=120.0, entry_price=118.0, trading_symbol="S")
    s = _offer_scanner({"T1": row},
                       lambda tid, *a, **k: (asked.append(tid), (True, ""))[1])
    Scanner._offer_cascade_add(s, _cand())
    assert asked == ["T1"]


def test_the_guard_is_present_in_source():
    src = inspect.getsource(Scanner._offer_cascade_add)
    assert 'getattr(trade, "is_ghost", False)' in src


# ── 2. the re-entry cap is per day ───────────────────────────────────────────

def test_the_daily_reset_clears_the_stop_out_allowance():
    src = inspect.getsource(Scanner._reset_daily_counters)
    assert "self._stop_out_reentries = {}" in src, (
        "the 'once per index per DAY' cap is still once per process lifetime"
    )


def test_the_reset_clears_it_alongside_the_other_daily_counters():
    """If the other counters move, this one must move with them."""
    src = inspect.getsource(Scanner._reset_daily_counters)
    for sibling in ("_trades_today", "_losses_today", "_counted_entry_keys"):
        assert f"self.{sibling}" in src
    assert src.index("self._stop_out_reentries") > src.index("self._trades_today")


def test_the_docstring_still_promises_a_daily_cap():
    """The premise. If the intent changes to per-process, the reset is wrong."""
    doc = inspect.getdoc(Scanner._stop_out_reentry_allowed) or ""
    assert "daily" in doc.lower() or "per day" in doc.lower(), doc[:200]


# ── 3. order capability is published without moving a trading gate ───────────

def _provider(is_connected, session_live):
    return SimpleNamespace(
        _ws_cache={}, _ltp_cache={}, _last_angel_ws_tick=0.0, _last_fyers_ws_tick=0.0,
        _fyers_auth_required=False, _fyers_auth_reason="", _fyers_refresh_disabled=False,
        _yahoo_request_count=0, _yahoo_last_used_at=None,
        is_connected=is_connected, fyers=None, is_market_open=lambda: True,
        kite=SimpleNamespace(is_connected=session_live),
    )


def test_a_dead_session_is_visible_even_though_the_latch_says_connected():
    from data.market_source_health import build_source_health

    h = build_source_health(_provider(True, False))
    assert h["kite"] is True, "the latched gate must not move"
    assert h["kite_session_live"] is False, "a dead token is still invisible"


def test_a_live_session_reports_both_true():
    from data.market_source_health import build_source_health

    h = build_source_health(_provider(True, True))
    assert h["kite"] is True and h["kite_session_live"] is True


def test_a_provider_without_a_kite_attribute_does_not_raise():
    from data.market_source_health import build_source_health

    p = _provider(True, True)
    del p.kite
    h = build_source_health(p)
    assert h["kite_session_live"] is False
    assert h["kite"] is True


def test_the_real_mode_gate_was_not_tightened():
    """kite_health feeds all_brokers_unavailable, which blocks REAL mode. A dead token
    must not silently start refusing to trade — that decision belongs to the operator
    and to main.py's RuntimeWatchdog, which polls the authoritative auth flag."""
    import data.market_source_health as mod

    src = inspect.getsource(mod)
    assert "kite_health = bool(provider.is_connected)" in src
