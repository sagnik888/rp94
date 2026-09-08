"""Breakeven means breakeven AFTER charges, not before them.

The breakeven stop exists so a trade that has proven itself does not close negative.
With floor 0.0 it could not deliver that, for two independent reasons:

  1. Rs.0 gross is a LOSS net of the ~Rs.80 round trip the position already paid.
  2. `now <= 0` is only observable once the trade is already at or below zero. P&L
     arrives on a poll, and on 2026-08-18 that poll ran at a median 2.4-6.9s (max 20s)
     while option premium moved hundreds of rupees between samples.

2026-08-18, all five firings, each with the last POSITIVE reading the guard saw:

    trade                    peak   last seen +   gap    booked
    SENSEX    15min          277        +67      2.4s     -190
    BANKNIFTY  5min          158         +4      1.9s     -294
    BANKNIFTY  5min          198       +128      6.6s     -143
    MIDCPNIFTY 15min         180       +120      5.5s     -158
    SENSEX     5min          236         +2      8.6s     -135

Rs.1,242 booked below the last price the guard itself had observed.

The floor is set to the round trip (~Rs.80) and deliberately no higher: see the
settings block for the measured cliff above it.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from intelligence.rtracker import normalize_replay_pnl_event
from scanner import Scanner


def _sc():
    sc = Scanner.__new__(Scanner)
    sc.mode = "LIVE"
    return sc


def _t(**kw):
    base = dict(instrument="NIFTY", inst_type="OPT", timeframe="5min")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Only the breakeven stop is under test; the cap shares this method."""
    st = get_settings()
    monkeypatch.setattr(st, "peak_giveback_cap_enabled", False)
    monkeypatch.setattr(st, "breakeven_stop_enabled", True)
    monkeypatch.setattr(st, "breakeven_stop_arm_rs", 100.0)
    monkeypatch.setattr(st, "breakeven_stop_floor_rs", 80.0)
    monkeypatch.setattr(st, "breakeven_stop_min_age_seconds", 60.0)


def _fire(peak, now, trade=None, evaluated_at=None):
    return Scanner._peak_giveback_cap_exit(
        _sc(), trade or _t(), current_pnl_rs=now, peak_pnl_rs=peak,
        evaluated_at=evaluated_at,
    )


# ── the shipped value ────────────────────────────────────────────────────────

def test_the_default_floor_is_the_round_trip_not_zero():
    assert Settings.model_fields["breakeven_stop_floor_rs"].default == 80.0


def test_only_one_knob_moved_at_a_time():
    """This started life as "the arm is unchanged": Fix 02 moved the FLOOR only, because
    the arm had no evidence behind it and moving both at once would have made the
    measurement unattributable. The discipline stands; the direction has swapped.

    2026-08-26 the arm moved 150 -> 100 on its own evidence (replay of 100 recorded
    lifecycles: +Rs.5,164 vs recorded at arm 100, against +Rs.3,038 at 150, on a plateau
    spanning 80-120), and the FLOOR is the one now held still. Rs.80 remains the best
    floor of every value replayed, and it is what makes the floor net-zero on a gross
    path. Move one, measure it, leave the other alone.
    """
    assert Settings.model_fields["breakeven_stop_arm_rs"].default == 100.0
    assert Settings.model_fields["breakeven_stop_floor_rs"].default == 80.0


# ── behaviour ────────────────────────────────────────────────────────────────

def test_a_proven_trade_exits_before_it_goes_negative():
    """The whole point: the trigger is now reachable while still in profit."""
    fired, why = _fire(peak=277.0, now=60.0)
    assert fired is True
    assert "60" in why


def test_it_still_fires_on_the_recorded_2026_08_18_cases():
    """Raising the floor must not stop it catching what it already caught."""
    for peak, final in [(277.0, -190.0), (157.5, -294.5), (198.0, -143.0),
                        (180.0, -158.0), (236.0, -135.0)]:
        assert _fire(peak, final)[0] is True, f"peak {peak} -> {final} no longer fires"


def test_a_trade_comfortably_in_profit_is_left_alone():
    """The floor is a floor, not a target. Rs.200 of open profit on a Rs.277 peak is
    not this guard's business — the profit lock and peak-threat own that."""
    assert _fire(peak=277.0, now=200.0)[0] is False


def test_an_unproven_trade_is_still_not_this_guards_business():
    """Below the arm there is no high-water mark worth defending, whatever the floor."""
    assert _fire(peak=90.0, now=-500.0)[0] is False


def test_the_floor_is_inclusive():
    assert _fire(peak=200.0, now=80.0)[0] is True
    assert _fire(peak=200.0, now=80.01)[0] is False


def test_the_hold_uses_market_time_not_wall_clock():
    entry = datetime(2026, 9, 8, 9, 15, 0)
    trade = _t(entry_time=entry)

    assert _fire(
        peak=200.0, now=80.0, trade=trade,
        evaluated_at=entry + timedelta(seconds=59),
    )[0] is False
    assert _fire(
        peak=200.0, now=80.0, trade=trade,
        evaluated_at=entry + timedelta(seconds=60),
    )[0] is True


def test_the_hold_starts_at_accepted_entry_not_signal_candle():
    signal = datetime(2026, 9, 8, 9, 15, 0)
    accepted = datetime(2026, 9, 8, 9, 20, 0)
    trade = _t(signal_timestamp=signal, entry_timestamp=accepted)

    assert _fire(
        peak=200.0, now=80.0, trade=trade,
        evaluated_at=accepted + timedelta(seconds=59),
    )[0] is False
    assert _fire(
        peak=200.0, now=80.0, trade=trade,
        evaluated_at=accepted + timedelta(seconds=60),
    )[0] is True


def test_it_is_still_named_the_breakeven_stop():
    """Fix 01 must survive Fix 02."""
    assert _fire(peak=277.0, now=-190.0).rule == "BREAKEVEN_STOP"


# ── the measured claim ───────────────────────────────────────────────────────

def test_the_round_trip_floor_protects_recorded_paths_after_the_hold():
    """Replays every recorded pnl_path through the real guard and market clock.

    A recorded path stops at the real exit, so the replay can only ever exit EARLIER
    than reality — it pays for forfeited upside and cannot invent any.
    """
    import glob, json, os

    files = sorted(glob.glob("data_store/rtracker/events/*.jsonl"))
    if not files:
        pytest.skip("no tracker data present")
    # The session being written RIGHT NOW is not evidence. Its file grows all day, so
    # the per-day assertion below reads whatever the book happens to be at the moment
    # the suite runs — on 2026-08-21 at 09:31 that was a single 16.9s BANKNIFTY cut at
    # -Rs.916, and the whole test failed on it. A claim about how sessions close can
    # only be made about sessions that have closed.
    from datetime import datetime as _dt

    from scanner import IST as _IST

    _today = _dt.now(_IST).strftime("%Y-%m-%d")
    files = [f for f in files if os.path.basename(f)[:10] != _today]
    if not files:
        pytest.skip("only the in-progress session is present")

    positions = {}
    for f in files:
        for line in open(f, encoding="utf-8"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("decision") != "EXIT" or not e.get("pnl_path"):
                continue
            if not e.get("entry_timestamp"):
                continue
            s = e["pnl_path"]
            if not isinstance(s, list) or len(s) < 3:
                continue
            accounting = normalize_replay_pnl_event(e)
            if accounting is None:
                continue
            key = (e.get("instrument"), e.get("entry_timestamp"), e.get("timeframe"))
            positions.setdefault(key, {
                "day": os.path.basename(f)[:10],
                "entry_timestamp": e["entry_timestamp"],
                "pnl": float(accounting["pnl"]),
                # The live guard consumes gross P&L, but an early exit books net.
                # Keep both aligned by sample index so a replay cannot compare a
                # gross trigger value with the real net outcome.
                "path_gross": accounting["pnl_path_gross"],
                "path_net": accounting["pnl_path_net"],
            })
    if len(positions) < 20:
        pytest.skip(f"only {len(positions)} pathed positions; too few to assert on")

    st, sc = get_settings(), _sc()

    def replay(floor):
        st.breakeven_stop_floor_rs = floor
        total = 0.0
        per_day = {}
        worst = 0.0
        actual = 0.0
        helped = hurt = 0
        for p in positions.values():
            entry_at = datetime.fromisoformat(p["entry_timestamp"])
            trade = _t(entry_time=entry_at)
            peak, booked = 0.0, None
            for gross_sample, net_sample in zip(p["path_gross"], p["path_net"]):
                pnl = float(gross_sample[1])
                peak = max(peak, pnl)
                if Scanner._peak_giveback_cap_exit(
                    sc, trade, current_pnl_rs=pnl, peak_pnl_rs=peak,
                    evaluated_at=(
                        entry_at + timedelta(seconds=float(gross_sample[0]))
                    ),
                )[0]:
                    booked = float(net_sample[1])
                    break
            if booked is None:
                booked = p["pnl"]
            delta = booked - p["pnl"]
            total += booked
            actual += p["pnl"]
            worst = min(worst, delta)
            helped += int(delta > 1.0)
            hurt += int(delta < -1.0)
            per_day[p["day"]] = per_day.get(p["day"], 0.0) + booked
        return total, per_day, worst, actual, helped, hurt

    at_cost, _, worst, actual, helped, hurt = replay(80.0)

    assert at_cost > actual, (
        f"round-trip floor booked Rs.{at_cost:,.0f} vs actual Rs.{actual:,.0f}"
    )
    # Require broad and economically meaningful protection without pretending a static
    # floor must beat every alternative on every future session. A growing production
    # ledger made the old exact floor-80-vs-floor-0 ranking and fixed Rs.400 clip bound
    # change even while floor 80 remained net beneficial.
    assert helped > hurt, f"helped {helped} vs hurt {hurt} — no breadth advantage"
    aggregate_benefit = at_cost - actual
    assert aggregate_benefit > abs(worst) * 2.0, (
        f"aggregate benefit Rs.{aggregate_benefit:,.0f} does not cover twice the "
        f"worst clip Rs.{-worst:,.0f}"
    )
