"""The give-back cap: why the book kept 27% of the profit it touched.

Across all 288 stored positions:
    77% went green at some point            -> entry selection works
    aggregate peak reached  Rs.179,599
    actually booked         Rs. 48,687      -> 27% captured
    105 went green then closed NEGATIVE, booking -Rs.27,585

The cause is calibration, not signal quality. Peak distribution of green trades is
p25 Rs.216 / median Rs.454 / p75 Rs.875, while every profit guard arms above that:
profit lock Rs.250, giveback Rs.500, peak-threat Rs.750. The strongest guard therefore
sees 26% of trades, and 61 green-then-red trades peaked below even the lowest arm, so no
profit protection could arm at all (-Rs.15,102).

This guard is ABSOLUTE, not another retention fraction, because premium noise is roughly
absolute. Retention is simultaneously too loose on a small peak (38% of Rs.200 = Rs.76)
and far too generous on a large one (62% of Rs.1,194 = Rs.740 handed back).

Really it is a loss-limiter anchored to the high-water mark instead of to entry: on the
measured sessions it turns -Rs.428 / -477 / -534 into roughly -Rs.50.

Validated by replaying recorded pnl_path samples through this very method (49 positions,
4 sessions): Rs.694 -> Rs.6,569, helped 23, hurt 3, worst single hurt -Rs.238, positive
in all four sessions. It is not free — a trade that peaked Rs.1,179 books Rs.896 here
against Rs.961 actual, and one breached the cap off a running peak of Rs.335 and then ran
to Rs.670. 23 against 3 is the trade being made.
"""
import glob
import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from intelligence.rtracker import normalize_replay_pnl_event
from scanner import IST, Scanner


@pytest.fixture(autouse=True)
def _cap_on(monkeypatch):
    """The cap ships DISABLED as of 2026-08-18 — it nets more than the breakeven stop but
    earns it by exiting trades that are still in profit, which is the operator's actual
    complaint. Its logic is retained and toggleable, so these tests enable it explicitly.
    They also disable the breakeven stop, which now shares this method and would
    otherwise fire first and mask what is under test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "peak_giveback_cap_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_enabled", False)


def _sc():
    sc = Scanner.__new__(Scanner)
    sc.mode = "LIVE"
    return sc


def _t(**kw):
    base = dict(instrument="NIFTY", inst_type="OPT", timeframe="5min")
    base.update(kw)
    return SimpleNamespace(**base)


def _fire(peak, now_pnl):
    return Scanner._peak_giveback_cap_exit(
        _sc(), _t(), current_pnl_rs=now_pnl, peak_pnl_rs=peak
    )


# ── the rule ─────────────────────────────────────────────────────────────────

def test_a_giveback_past_the_allowance_fires():
    """Rs.823 peak: allowance is max(200, 25% of 823) = Rs.206, so Rs.213 given back
    fires. This is today's SENSEX 15min trade, which peaked Rs.823 and booked Rs.230."""
    fired, why = _fire(peak=823.0, now_pnl=610.0)
    assert fired is True
    assert "gave back" in why and "823" in why


def test_a_giveback_inside_the_allowance_holds():
    """Rs.199 of give-back on a Rs.823 peak is inside the Rs.206 allowance."""
    fired, _ = _fire(peak=823.0, now_pnl=624.0)
    assert fired is False


def test_the_cap_converts_a_small_peak_loss_into_a_small_loss():
    """The real mechanism. MIDCPNIFTY peaked Rs.192 and booked -Rs.428; capped, it exits
    around -Rs.8. This is loss limiting anchored to the peak, not profit taking."""
    fired, _ = _fire(peak=192.0, now_pnl=-8.0)
    assert fired is True
    fired_early, _ = _fire(peak=192.0, now_pnl=100.0)
    assert fired_early is False


def test_a_peak_below_the_arm_never_fires():
    """Nothing worth defending yet. A Rs.99 peak is noise on an option premium."""
    fired, _ = _fire(peak=99.0, now_pnl=-500.0)
    assert fired is False


def test_the_arm_boundary_is_inclusive():
    assert _fire(peak=100.0, now_pnl=-100.0)[0] is True
    assert _fire(peak=99.99, now_pnl=-100.0)[0] is False


def test_the_allowance_is_a_hybrid_not_a_flat_cap():
    """A flat Rs.200 was the first attempt and the existing suite killed it: on a
    Rs.4,859 peak it fires on a 4% pullback and pre-empts the major-win guard, whose job
    is to let a large winner breathe. The replay set topped out near Rs.1,194 of peak, so
    the large-peak regime was outside the validated range entirely.

    allowance = max(cap_rs, cap_pct x peak)
    """
    settings = get_settings()
    cap = float(settings.peak_giveback_cap_rs)
    pct = float(settings.peak_giveback_cap_pct)
    for peak in (150.0, 400.0, 800.0):
        assert max(cap, peak * pct) == cap, "the absolute term must bind at small peaks"
    for peak in (2000.0, 4859.0):
        assert max(cap, peak * pct) > cap, "the pct term must bind at large peaks"


def test_a_large_peak_gets_a_proportional_allowance():
    """Rs.1,200 peak -> max(200, 25% of 1,200) = Rs.300 allowed."""
    assert _fire(peak=1200.0, now_pnl=1200.0 - 250.0)[0] is False
    assert _fire(peak=1200.0, now_pnl=1200.0 - 350.0)[0] is True


def test_above_the_ceiling_the_guard_steps_aside_entirely():
    """The replay set's peaks run median Rs.247 / p75 Rs.526 / p90 Rs.1,038 / max
    Rs.2,401, so evidence is dense below ~Rs.1,000 and only two positions sit above
    Rs.1,500. The major-win guard and the trend lock are built and tested for the
    large-winner regime and deliberately hold a big runner through a deep pullback — a
    Rs.4,000 peak handing back Rs.1,250 is a case they exist to tolerate, and the suite
    caught a flat cap pre-empting exactly that."""
    ceiling = float(get_settings().peak_giveback_cap_max_peak_rs)
    assert ceiling >= 1038.0, "the ceiling must sit above the dense part of the evidence"
    assert _fire(peak=ceiling + 1.0, now_pnl=-5000.0)[0] is False
    assert _fire(peak=4000.0, now_pnl=4000.0 - 1250.0)[0] is False
    # and just inside the ceiling it still works
    assert _fire(peak=ceiling - 1.0, now_pnl=0.0)[0] is True


def test_the_ceiling_is_toggleable(monkeypatch):
    monkeypatch.setattr(get_settings(), "peak_giveback_cap_max_peak_rs", 0.0)
    assert _fire(peak=9999.0, now_pnl=0.0)[0] is True


def test_a_mid_peak_winner_still_keeps_most_of_it():
    """Rs.1,194 peak -> 25% = Rs.298 allowed, so it books ~Rs.896. A 38% retention
    ladder would instead have handed back Rs.740 of it."""
    assert _fire(peak=1194.0, now_pnl=1000.0)[0] is False
    assert _fire(peak=1194.0, now_pnl=890.0)[0] is True


def test_the_pct_term_can_be_disabled_leaving_the_flat_cap(monkeypatch):
    """With pct off, a Rs.1,200 peak reverts to the flat Rs.200 allowance."""
    assert _fire(peak=1200.0, now_pnl=1200.0 - 250.0)[0] is False
    monkeypatch.setattr(get_settings(), "peak_giveback_cap_pct", 0.0)
    assert _fire(peak=1200.0, now_pnl=1200.0 - 250.0)[0] is True


def test_a_trade_that_never_went_green_is_not_this_guards_business():
    """The loss guard and the hard stop own that case."""
    assert _fire(peak=0.0, now_pnl=-900.0)[0] is False


# ── switches ─────────────────────────────────────────────────────────────────

def test_the_guard_is_toggleable(monkeypatch):
    monkeypatch.setattr(get_settings(), "peak_giveback_cap_enabled", False)
    assert _fire(peak=823.0, now_pnl=100.0)[0] is False


@pytest.mark.parametrize("field", ["peak_giveback_cap_arm_rs", "peak_giveback_cap_rs"])
def test_zero_on_either_number_is_a_clean_off_switch(monkeypatch, field):
    monkeypatch.setattr(get_settings(), field, 0.0)
    assert _fire(peak=823.0, now_pnl=100.0)[0] is False


def test_the_shipped_defaults_are_the_validated_ones():
    """Rs.200 is a floor, not a starting point: at Rs.150 the measured hurt count went
    from 3 to 9, worst-case damage to -Rs.706, and one session flipped negative."""
    f = Settings.model_fields
    # Ships OFF: it exits trades that are still in profit (a Rs.800 peak at +Rs.600
    # trips it) and hurt 3 of 49 replayed positions. The breakeven stop covers the
    # green-then-red bucket with zero hurt instead.
    assert f["peak_giveback_cap_enabled"].default is False
    assert f["peak_giveback_cap_arm_rs"].default == 100.0
    assert f["peak_giveback_cap_rs"].default == 200.0
    assert f["peak_giveback_cap_pct"].default == 0.25


def test_bad_settings_values_disarm_rather_than_crash(monkeypatch):
    monkeypatch.setattr(get_settings(), "peak_giveback_cap_rs", "not a number")
    assert _fire(peak=823.0, now_pnl=100.0)[0] is False


# ── wiring ───────────────────────────────────────────────────────────────────

def test_it_runs_before_the_loss_guard_in_both_monitor_paths():
    """Ordering is the whole point. The loss guard is what actually cut the measured
    green-then-red trades, at 0.43-0.55R; once it has cut there is no peak left to
    defend, so this must be evaluated first — in BOTH paths, which have historically
    disagreed about exit order."""
    import inspect

    src = inspect.getsource(Scanner)
    caps = [i for i in range(len(src)) if src.startswith("self._peak_giveback_cap_exit(", i)]
    guards = [i for i in range(len(src)) if src.startswith("self._loss_guard_decision(", i)]
    assert len(caps) == 2, f"expected 2 cap call sites, found {len(caps)}"
    assert len(guards) == 2, f"expected 2 loss-guard call sites, found {len(guards)}"
    for cap_at, guard_at in zip(caps, guards):
        assert cap_at < guard_at, "the cap is evaluated after the loss guard"


def test_the_exit_reason_is_not_registered_as_a_stop_out():
    """A cap exit is frequently PROFITABLE (a Rs.823 peak books ~Rs.620), so counting it
    as a stop-out would wrongly suppress re-entry on the instrument."""
    assert "PEAK_GIVEBACK_CAP" not in Scanner.STOP_OUT_EXIT_REASONS


def test_the_setting_reaches_the_dashboard():
    """Three registrations are required on this project or the field is silently
    read-only: the field set, the boolean set, and explicit name forwarding."""
    import pathlib

    src = pathlib.Path("dashboard/server.py").read_text(encoding="utf-8")
    assert '"peak_giveback_cap_enabled",' in src
    assert '"peak_giveback_cap_arm_rs",' in src
    assert '"peak_giveback_cap_rs",' in src
    assert 'peak_giveback_cap_rs=msg.get("peak_giveback_cap_rs")' in src


# ── the measured claim, re-checked against the real recorded paths ───────────

def test_replaying_recorded_paths_beats_what_actually_happened():
    """The claim that justifies shipping this, asserted rather than asserted-in-prose.
    Walks each recorded pnl_path through the REAL guard and compares to the booked P&L.

    The replay can only ever exit EARLIER than reality, since a path stops at the real
    exit — so it correctly pays for forfeited upside and cannot invent any.
    """
    files = sorted(glob.glob("data_store/rtracker/events/*.jsonl"))
    if not files:
        pytest.skip("no tracker data present")
    # The session being written RIGHT NOW is not evidence. Its file grows all day, so
    # the per-day assertion below reads whatever the book happens to be at the moment
    # the suite runs — on 2026-08-21 at 09:31 that was a single 16.9s BANKNIFTY cut at
    # -Rs.916, and the whole test failed on it. A claim about how sessions close can
    # only be made about sessions that have closed.
    today = datetime.now(IST).strftime("%Y-%m-%d")
    files = [f for f in files if os.path.basename(f)[:10] != today]
    if not files:
        pytest.skip("only the in-progress session is present")
    positions = {}
    for path_file in files:
        day = os.path.basename(path_file)[:10]
        for line in open(path_file, encoding="utf-8"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("decision") != "EXIT" or not e.get("pnl_path"):
                continue
            samples = e["pnl_path"]
            if not isinstance(samples, list) or len(samples) < 3:
                continue
            accounting = normalize_replay_pnl_event(e)
            if accounting is None:
                continue
            key = (e.get("instrument"), e.get("entry_timestamp"), e.get("timeframe"))
            positions.setdefault(key, {
                "day": day,
                "pnl": float(accounting["pnl"]),
                "path_gross": accounting["pnl_path_gross"],
                "path_net": accounting["pnl_path_net"],
            })
    if len(positions) < 20:
        pytest.skip(f"only {len(positions)} pathed positions; too few to assert on")

    sc, trade = _sc(), _t()
    actual = capped = 0.0
    per_day_capped = {}
    per_day_actual = {}
    helped = hurt = 0
    worst = 0.0
    for pos in positions.values():
        peak = 0.0
        booked = None
        for gross_sample, net_sample in zip(pos["path_gross"], pos["path_net"]):
            pnl = float(gross_sample[1])
            peak = max(peak, pnl)
            fired, _ = Scanner._peak_giveback_cap_exit(
                sc, trade, current_pnl_rs=pnl, peak_pnl_rs=peak
            )
            if fired:
                booked = float(net_sample[1])
                break
        if booked is None:
            booked = pos["pnl"]
        delta = booked - pos["pnl"]
        actual += pos["pnl"]
        capped += booked
        per_day_capped[pos["day"]] = per_day_capped.get(pos["day"], 0.0) + booked
        per_day_actual[pos["day"]] = per_day_actual.get(pos["day"], 0.0) + pos["pnl"]
        worst = min(worst, delta)
        if delta > 1:
            helped += 1
        elif delta < -1:
            hurt += 1

    assert capped > actual, f"the cap booked Rs.{capped:,.0f} vs actual Rs.{actual:,.0f}"
    assert helped > hurt * 2, f"helped {helped} vs hurt {hurt} — the edge is too thin"
    assert worst > -500.0, f"a single position lost Rs.{-worst:,.0f} to the cap"
    # A peak-protection rule cannot make a day positive when its largest losers never
    # reach the arm.  Require improvement across days instead: this rejects a result
    # carried by one exceptional session without asserting an impossible absolute P&L.
    day_deltas = {
        day: per_day_capped[day] - per_day_actual.get(day, 0.0)
        for day in per_day_capped
    }
    improved_days = sum(delta > 1.0 for delta in day_deltas.values())
    degraded_days = sum(delta < -1.0 for delta in day_deltas.values())
    assert improved_days > degraded_days, day_deltas
