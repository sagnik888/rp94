"""The exit-confirmation layer, ported back from restore_point_51.

Comparing the three builds on the post-entry->exit chain showed the current one had lost
a whole guard family that the older, better-performing build carried:

    concept                              current   rp46-v8   restore_point_51
    exit confirmation (2 observations)         0         8                 92
    loss-exit patience window                  0         0                 20
    hold exit on unverified mark               0         0                  6
    contract quote-ready gate                  0         0                  4
    loss-threat confirmation                   0         0                 36

restore_point_51's loss arm would not cut a position until ALL of this held:

  * score >= peak_threat_exit_loss_min_score (6)                    — current had this
  * >= peak_threat_exit_loss_min_domains distinct evidence domains,
    with "price" mandatory among them                               — MISSING
  * the SAME domain signature observed twice, at least
    peak_threat_exit_loss_confirm_seconds apart on the MARKET clock  — MISSING
  * a minimum adverse amount                                        — MISSING
  * the latch cleared on every path that stops short of an exit     — MISSING

This ports the domain requirement, the two-observation confirmation and the
clear-on-lapse latch. The adverse floor is the round-trip cost floor added the same day.

Why the domain rule matters more than the score: on 2026-08-10 nine exits reached the
score of 6 entirely from intel, aggregate, order-flow, volume and trend votes. Cutting a
live position is a PRICE decision; a pile of soft intel agreeing is not a price break.
A score of 8 with no price evidence is now refused outright.

Why the market clock: scanner cycles run several times against one market timestamp, so
counting CALLS would manufacture persistence. rp51's own comment says exactly this, and
the port keeps the `observation_ts <= last_at` guard that enforces it.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from scanner import Scanner

ENTRY = datetime(2026, 8, 10, 11, 20, 53)
INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


def _scanner(price_adverse=True, intel_adverse=True, live_adverse=False, fast=False):
    s = Scanner.__new__(Scanner)
    s._setting_enabled = lambda v, d=True: bool(v) if v is not None else d
    s._trade_direction_view = lambda t, **k: "LONG"
    s.candles = SimpleNamespace(get_latest_price=lambda i: 24550.0)
    s._eod_protect_active = lambda t: False
    s._profit_guard_reversal_context = lambda *a, **k: {
        "has_context": True, "fast_reversal": fast, "one_min_emergency": False,
        "trend_supportive": not price_adverse,
        "trend_votes": 0 if price_adverse else 2,
        "reason": "trend votes 0/3" if price_adverse else "supportive"}
    s._live_direction_context = lambda *a, **k: {
        "adverse": live_adverse, "supportive": False, "live_regime": "X",
        "cross_opp": 2, "cross_peers": 3}
    sig = "BEARISH" if intel_adverse else "NEUTRAL"
    s._trade_like_market_intelligence_context = lambda t: {
        "aggregate": {"signal": sig},
        "order_flow": {"signal": sig, "ratio": 0.6 if intel_adverse else 1.0},
        "volume": {"signal": sig}, "oi": {}, "pcr": {}}
    s._regime_conviction_vector_from_context = lambda t, c: {}
    # 0.0, not -0.16: a whisper-negative reading trips the `aligned_intel <= -0.15`
    # branch and creates a whole fresh_intel DOMAIN, which would make 'price only'
    # untestable. Noted as a residual weakness of the domain model, not a stub hack.
    s._trade_like_signed_intelligence = lambda t, c: -0.6 if intel_adverse else 0.0
    s._view_aligned_intel_score = lambda v, x: x
    s._intel_signal_opposes_view = lambda g, v: intel_adverse
    s._intel_signal_aligns_view = lambda g, v: False
    return s


def _trade(index="NIFTY", entry=232.85, lot=65):
    return SimpleNamespace(
        instrument=index, timeframe="5min", inst_type="OPT", direction="LONG",
        option_type="CE", volume_ratio=0.9, price_action_verdict="",
        price_action_score=0.0, entry_price=entry, entry_time=ENTRY,
        lots=1, lot_size=lot)


def _look(s, trade, secs, pnl=-400.0):
    return Scanner._peak_threat_exit_decision(
        s, trade, current_price=232.0, current_pnl_rs=pnl, peak_pnl=0.0,
        initial_risk_rs=3000.0, latest_time=ENTRY + timedelta(seconds=secs),
        regime="RANGING", inst_type="OPT", option_type="CE", direction="LONG")


# ── price evidence is mandatory ──────────────────────────────────────────────

def test_a_high_score_with_no_price_evidence_is_refused():
    """The headline. Score 8 — above the threshold of 6 — and still no exit, because
    every point came from intel. This is the exact shape of the 2026-08-10 churn."""
    s, t = _scanner(price_adverse=False, intel_adverse=True), _trade()
    for secs in (300, 305, 310):
        ok, _r, ctx = _look(s, t, secs)
        assert ok is False, ctx
        assert "no price evidence" in ctx, ctx


@pytest.mark.parametrize("index", INDICES)
def test_price_plus_one_other_domain_is_enough_on_every_index(index):
    s, t = _scanner(price_adverse=True, intel_adverse=True), _trade(index)
    assert _look(s, t, 300)[0] is False          # 1/2
    assert _look(s, t, 306)[0] is True           # confirmed


def test_price_alone_does_not_reach_the_domain_floor():
    """One domain is one opinion. min_domains is 2."""
    s, t = _scanner(price_adverse=True, intel_adverse=False), _trade()
    ok, _r, ctx = _look(s, t, 300)
    assert ok is False
    assert "domains 1/2" in ctx, ctx


def test_price_and_live_market_alone_are_two_domains_but_not_enough_score():
    """Domains and score are independent gates. price(+2) + live_market(+2) + weak
    volume(+1) = 5, one short of the 6 threshold, so this correctly refuses on SCORE."""
    s, t = _scanner(price_adverse=True, intel_adverse=False, live_adverse=True), _trade()
    ok, _r, ctx = _look(s, t, 300)
    assert ok is False
    assert "domains 2/2" in ctx and "score 5/6" in ctx, ctx


def test_live_market_can_be_the_corroborating_domain_on_a_real_break():
    """A fast reversal is +3 on price, so price + live_market clears both gates with no
    intel involvement at all."""
    s, t = _scanner(price_adverse=True, intel_adverse=False, live_adverse=True, fast=True), _trade()
    assert _look(s, t, 300)[0] is False
    ok, _r, ctx = _look(s, t, 306)
    assert ok is True, ctx
    assert "live_market" in ctx and "price" in ctx


# ── two observations, on a clock that moved ──────────────────────────────────

def test_one_observation_never_exits():
    s, t = _scanner(), _trade()
    ok, _r, ctx = _look(s, t, 300)
    assert ok is False
    assert "confirmation 1/2" in ctx


def test_a_repeated_market_timestamp_cannot_manufacture_persistence():
    """Scanner cycles run several times against one timestamp. Counting calls would
    turn that into false confirmation — rp51's comment names this trap explicitly."""
    s, t = _scanner(), _trade()
    for _ in range(6):
        ok, _r, ctx = _look(s, t, 300)
        assert ok is False, ctx
    assert "0.0s/3.0s" in ctx, ctx


def test_the_gap_must_exceed_the_configured_seconds():
    s, t = _scanner(), _trade()
    _look(s, t, 300)
    ok, _r, ctx = _look(s, t, 302)          # only 2s of a 3s requirement
    assert ok is False
    assert "confirming 2.0s/3.0s" in ctx


def test_a_changed_signature_restarts_rather_than_completes():
    """Different evidence is not the same evidence seen twice."""
    s_both, t = _scanner(price_adverse=True, intel_adverse=True), _trade()
    _look(s_both, t, 300)                              # signature {price, fresh_intel}
    s_price_only = _scanner(price_adverse=True, intel_adverse=False)
    ok, _r, ctx = _look(s_price_only, t, 310)
    assert ok is False
    # falls back to the domain floor, and the old latch must not carry it over
    assert "domains 1/2" in ctx, ctx


# ── the latch clears whenever the case lapses ────────────────────────────────

def test_the_latch_clears_when_the_trade_goes_green():
    s, t = _scanner(), _trade()
    _look(s, t, 300)
    assert getattr(t, "_loss_threat_confirmation", None) is not None
    _look(s, t, 302, pnl=+500.0)             # back above water
    assert getattr(t, "_loss_threat_confirmation", None) is None


def test_the_latch_clears_when_the_loss_falls_under_the_cost_floor():
    s, t = _scanner(), _trade()
    _look(s, t, 300)
    _look(s, t, 302, pnl=-5.0)               # inside the round trip
    assert getattr(t, "_loss_threat_confirmation", None) is None


def test_the_latch_clears_when_the_score_collapses():
    s, t = _scanner(), _trade()
    _look(s, t, 300)
    calm = _scanner(price_adverse=False, intel_adverse=False)
    _look(calm, t, 310)
    assert getattr(t, "_loss_threat_confirmation", None) is None


def test_a_cleared_latch_cannot_complete_an_old_confirmation():
    """The real hazard: a first sighting from minutes ago completing against an
    unrelated second one."""
    s, t = _scanner(), _trade()
    _look(s, t, 300)                          # 1/2
    _look(_scanner(price_adverse=False, intel_adverse=False), t, 310)   # lapses
    ok, _r, ctx = _look(s, t, 900)            # long after
    assert ok is False, "a stale confirmation completed"
    assert "confirmation 1/2" in ctx


def test_the_clear_helper_tolerates_a_read_only_object():
    class Frozen:
        __slots__ = ()

    Scanner._clear_loss_threat_confirmation(Frozen())   # must not raise


# ── the settings are real and tunable ────────────────────────────────────────

def test_the_new_settings_resolve():
    from config.settings import get_settings

    s = get_settings()
    assert int(getattr(s, "peak_threat_exit_loss_min_domains")) >= 1
    assert float(getattr(s, "peak_threat_exit_loss_confirm_seconds")) >= 0.0


def test_confirmation_can_be_disabled_without_disabling_the_guard():
    """confirm_seconds=0 restores single-observation behaviour for anyone who wants it."""
    from config.settings import get_settings

    st = get_settings()
    saved = st.peak_threat_exit_loss_confirm_seconds
    st.peak_threat_exit_loss_confirm_seconds = 0.0
    try:
        s, t = _scanner(), _trade()
        ok, _r, ctx = _look(s, t, 300)
        assert ok is True, ctx
    finally:
        st.peak_threat_exit_loss_confirm_seconds = saved


def test_the_domain_requirement_is_wired_to_its_setting():
    import inspect

    src = inspect.getsource(Scanner._peak_threat_exit_decision)
    assert "peak_threat_exit_loss_min_domains" in src
    assert '"price" in evidence_domains' in src
