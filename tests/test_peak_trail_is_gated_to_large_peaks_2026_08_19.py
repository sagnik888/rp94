"""A percentage trail is only safe once the peak is big enough to percentage.

Fix 04 as originally proposed — "trail a percentage of the running peak" — does not
survive measurement, and this test exists partly to record that so it is not proposed
again. Over 60 recorded pnl_paths at arm 150 / floor 80:

    no trail              Rs.6,687   helped 15  hurt  4  worst -Rs.226
    trail 50% (ungated)   Rs.5,375   helped 16  hurt  8  worst -Rs.1,806
    trail 40% (ungated)   Rs.4,423   helped 19  hurt  8  worst -Rs.1,806
    trail 25% (ungated)   Rs.3,413   helped 18  hurt 14  worst -Rs.1,806

Tighter is monotonically worse. That is the give-back cap's failure reproducing: the
rule takes money off trades that were still going up. The original evidence for the
proposal came from three trades that had all ended NEGATIVE — a sample on which a trail
cannot lose, because there was nothing left to clip.

Gated above a Rs.600 peak it stops clipping and turns mildly positive (Rs.6,986, hurt
and worst-case unchanged), but the entire gain is too thin to certify a live default.
The trail therefore ships OFF while this test pins the conservative gate and opt-in
behavior.
"""
from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from scanner import Scanner


def _sc():
    sc = Scanner.__new__(Scanner)
    sc.mode = "LIVE"
    return sc


def _t():
    return SimpleNamespace(instrument="NIFTY", inst_type="OPT", timeframe="5min")


def _fire(peak, now):
    return Scanner._peak_giveback_cap_exit(
        _sc(), _t(), current_pnl_rs=now, peak_pnl_rs=peak
    )


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "peak_giveback_cap_enabled", False)
    monkeypatch.setattr(st, "breakeven_stop_enabled", True)
    monkeypatch.setattr(st, "breakeven_stop_arm_rs", 150.0)
    monkeypatch.setattr(st, "breakeven_stop_floor_rs", 80.0)


def _trail_on(monkeypatch, gate=600.0, give=0.40):
    st = get_settings()
    monkeypatch.setattr(st, "peak_trail_enabled", True)
    monkeypatch.setattr(st, "peak_trail_above_rs", gate)
    monkeypatch.setattr(st, "peak_trail_giveback_pct", give)


# ── defaults ─────────────────────────────────────────────────────────────────

def test_the_trail_ships_off_pending_held_out_validation():
    assert Settings.model_fields["peak_trail_enabled"].default is False


def test_the_shipped_gate_and_giveback_are_conservative():
    """The opt-in trail retains the conservative Rs.600 gate and 40% give-back."""
    assert Settings.model_fields["peak_trail_above_rs"].default == 600.0
    assert Settings.model_fields["peak_trail_giveback_pct"].default == 0.40


def test_with_the_trail_off_nothing_changes(monkeypatch):
    """A Rs.2,000 peak back at Rs.500 is untouched: well above the floor, no trail.

    Turns the trail off explicitly rather than leaning on the default, which is what it
    meant all along — the default moved on 2026-08-24 and this read as a regression.
    """
    monkeypatch.setattr(get_settings(), "peak_trail_enabled", False)
    assert _fire(peak=2000.0, now=500.0)[0] is False


# ── the gate is the whole point ──────────────────────────────────────────────

def test_below_the_gate_a_small_peak_is_not_percentaged(monkeypatch):
    """40% of a Rs.300 peak is Rs.120 — inside the spread. The flat floor owns this."""
    _trail_on(monkeypatch)
    assert _fire(peak=300.0, now=180.0)[0] is False   # would fire on an ungated 40%
    assert _fire(peak=300.0, now=80.0)[0] is True     # the floor still works
    assert _fire(peak=300.0, now=80.0).rule == "BREAKEVEN_STOP"


def test_above_the_gate_the_trail_takes_over(monkeypatch):
    _trail_on(monkeypatch)
    # Rs.1,000 peak -> trail sits at Rs.600.
    assert _fire(peak=1000.0, now=650.0)[0] is False
    v = _fire(peak=1000.0, now=590.0)
    assert v[0] is True
    assert v.rule == "PEAK_TRAIL"
    assert "600" in v[1]


def test_the_trail_never_sits_below_the_floor(monkeypatch):
    """A trail that resolved under the floor would be a downgrade, not an upgrade."""
    _trail_on(monkeypatch, gate=100.0, give=0.99)
    v = _fire(peak=200.0, now=80.0)
    assert v[0] is True
    assert v.rule == "BREAKEVEN_STOP", "floor must win when it is the higher level"


def test_the_trail_still_cannot_fire_below_the_arm(monkeypatch):
    _trail_on(monkeypatch, gate=100.0, give=0.40)
    assert _fire(peak=140.0, now=-500.0)[0] is False


def test_a_zero_on_either_knob_is_a_clean_off_switch(monkeypatch):
    for gate, give in ((0.0, 0.40), (600.0, 0.0)):
        _trail_on(monkeypatch, gate=gate, give=give)
        assert _fire(peak=1000.0, now=590.0)[0] is False


# ── the measured claim, on the real corpus ───────────────────────────────────

def test_an_ungated_trail_is_worse_than_no_trail_on_recorded_paths(monkeypatch):
    """The refutation, asserted rather than asserted-in-prose. If a future change makes
    an ungated tight trail profitable, this test fails and the settings block is stale.
    """
    import glob, json

    files = sorted(glob.glob("data_store/rtracker/events/*.jsonl"))
    if not files:
        pytest.skip("no tracker data present")
    positions = {}
    for f in files:
        for line in open(f, encoding="utf-8"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("decision") != "EXIT" or not e.get("pnl_path"):
                continue
            s = e["pnl_path"]
            if not isinstance(s, list) or len(s) < 3:
                continue
            positions.setdefault(
                (e.get("instrument"), e.get("entry_timestamp"), e.get("timeframe")),
                {"pnl": float(e.get("pnl") or 0.0),
                 "path": [(float(a), float(b)) for a, b in s]},
            )
    if len(positions) < 20:
        pytest.skip(f"only {len(positions)} pathed positions")

    st, sc, tr = get_settings(), _sc(), _t()

    def replay():
        total = 0.0
        for p in positions.values():
            peak, booked = 0.0, None
            for _s, pnl in p["path"]:
                peak = max(peak, pnl)
                if Scanner._peak_giveback_cap_exit(
                    sc, tr, current_pnl_rs=pnl, peak_pnl_rs=peak
                )[0]:
                    booked = pnl
                    break
            total += p["pnl"] if booked is None else booked
        return total

    monkeypatch.setattr(st, "peak_trail_enabled", False)
    no_trail = replay()

    monkeypatch.setattr(st, "peak_trail_enabled", True)
    monkeypatch.setattr(st, "peak_trail_above_rs", 1.0)      # effectively ungated
    monkeypatch.setattr(st, "peak_trail_giveback_pct", 0.25)
    ungated = replay()

    monkeypatch.setattr(st, "peak_trail_above_rs", 600.0)
    monkeypatch.setattr(st, "peak_trail_giveback_pct", 0.40)
    gated = replay()

    assert ungated < no_trail, (
        f"an ungated 25% trail booked Rs.{ungated:,.0f} vs Rs.{no_trail:,.0f} with no "
        f"trail — if this passes now, the settings block's reasoning is out of date"
    )
    assert gated >= no_trail, (
        f"the gated trail booked Rs.{gated:,.0f} vs Rs.{no_trail:,.0f} with no trail"
    )
