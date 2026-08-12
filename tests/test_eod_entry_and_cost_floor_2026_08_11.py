"""Four trades on 2026-08-11 opened and closed in a fraction of a second.

    15:09:36.598 -> .788  NIFTY 24500PE   133.00 -> 40.65   -Rs.6,083  (190 ms)
    15:09:36.749 -> .870  MIDCPNIFTY      163.00 -> 162.15  -Rs.  182  (121 ms)
    15:09:41.752 -> .814  SENSEX          315.45 -> 313.20  -Rs.  125  ( 62 ms)
    15:11:22     -> .     BANKNIFTY       473.00 -> 474.20  -Rs.   44  (171 ms)

Three separate defects, one per file section.

1. STALE MARK (NIFTY). Spot was 24453.65, so a same-day-expiry 24500 PE is worth its
   intrinsic Rs.46 — the true price was 40.65 and the recorded 133.00 implied Rs.87 of
   time value on an option 20 minutes from expiry. The mark was ~2.5 hours stale; the
   same contract legitimately printed 133 at 12:25. The real-order path reprices through
   _execution_levels and validates in open_trade, but the MANUAL path did neither.

2. ENTRY/EOD CONTRADICTION (all four). ut_5min_no_entry_after admitted entries until
   15:14 while EOD protection engaged at ut_force_exit_time minus
   eod_protect_window_minutes — 14:55 at the shipped values. For ~20 minutes the system
   opened positions into a regime whose own logic closed them on the next tick. Across
   the stored book a 14:55 cutoff removes 13 trades worth -Rs.8,864 (9 losers, 4
   winners).

3. PROTECTING LESS THAN THE SPREAD (BANKNIFTY). PROFIT_GIVEBACK_GUARD banked a Rs.36
   gross peak and booked -Rs.44, because the round trip costs Rs.80. Inside the EOD
   window the arm collapses to eod_protect_min_peak_rs (Rs.1), so any blip armed an exit
   that could not come out ahead. The LOSS side of peak-threat has refused on exactly
   this reasoning since it was written; the profit side had no equivalent.
"""
from datetime import datetime, time as dtime
from types import SimpleNamespace

import pytest
import pytz

from scanner import Scanner

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _pinned_settings():
    from config.settings import Settings, get_settings

    s = get_settings()
    keys = (
        "candidate_entry_mark_validation_enabled",
        "candidate_entry_mark_max_divergence_pct",
        "candidate_entry_mark_min_gap_rs",
        "block_entries_during_eod_protect",
        "eod_protect_enabled",
        "eod_protect_window_minutes",
        "eod_protect_min_peak_rs",
        "eod_protect_arm_r_multiple",
        "profit_protect_cost_floor_enabled",
        "profit_protect_min_cost_multiple",
        "opt_cost",
        "ut_force_exit_time",
        "ut_5min_no_entry_after",
        "ut_no_entry_after",
        "profit_giveback_min_peak_rs",
        "profit_giveback_arm_r_multiple",
    )
    saved = {k: getattr(s, k, None) for k in keys}
    for k in keys:
        if k in Settings.model_fields:
            setattr(s, k, Settings.model_fields[k].default)
    yield
    for k, v in saved.items():
        setattr(s, k, v)


# ── 1. the stale entry mark ──────────────────────────────────────────────────

def _cand(mark, live, lots=1, lot_size=65, inst="OPT"):
    sc = Scanner.__new__(Scanner)
    sc._get_live_premium_cached = lambda t, allow_blocking=True: live
    c = SimpleNamespace(
        inst_type=inst, price=mark, lots=lots, lot_size=lot_size,
        trading_symbol="NIFTY2681124500PE", symbol_token="10499074", instrument="NIFTY",
    )
    return sc, c


def test_the_2026_08_11_stale_mark_is_refused():
    sc, c = _cand(133.00, 40.65)
    why = sc._candidate_entry_mark_refusal(c)
    assert why, "the -Rs.6,083 mark was accepted again"
    assert "69%" in why and "40.65" in why


@pytest.mark.parametrize("mark,live,note", [
    (133.0, 130.0, "2% drift"),
    (133.0, 120.0, "10% drift"),
    (100.0, 80.0, "20% drift, just inside the band"),
    (212.0, 208.65, "a real NIFTY trade from the same session"),
    (473.0, 474.2, "the BANKNIFTY row — mark was fine, the exit was the problem"),
])
def test_ordinary_drift_is_admitted(mark, live, note):
    sc, c = _cand(mark, live)
    assert sc._candidate_entry_mark_refusal(c) == "", note


def test_a_cheap_contract_is_not_judged_on_percentage_alone():
    """A Rs.5 premium ticking to Rs.3.50 is 30% but Rs.97 of exposure. Percentage alone
    would refuse ordinary movement on every cheap contract."""
    sc, c = _cand(5.0, 3.5)
    assert sc._candidate_entry_mark_refusal(c) == ""


def test_a_cheap_contract_at_size_is_still_caught():
    """The rupee floor is about EXPOSURE, not premium — the same 30% on 20 lots is real."""
    sc, c = _cand(5.0, 3.5, lots=20, lot_size=65)
    assert sc._candidate_entry_mark_refusal(c) != ""


@pytest.mark.parametrize("live", [0.0, None, -1.0])
def test_a_missing_quote_never_blocks_trading(live):
    """No opinion is not evidence of a bad mark; refusing here would stop trading on
    every feed hiccup."""
    sc, c = _cand(133.0, live)
    assert sc._candidate_entry_mark_refusal(c) == ""


def test_a_quote_lookup_that_raises_never_blocks_trading():
    sc, c = _cand(133.0, 40.65)

    def boom(_t, allow_blocking=True):
        raise RuntimeError("feed down")

    sc._get_live_premium_cached = boom
    assert sc._candidate_entry_mark_refusal(c) == ""


def test_the_quote_is_fetched_without_blocking_the_scan_loop():
    """This runs inside the scan cycle; a blocking HTTPS call here would stall it."""
    seen = {}
    sc, c = _cand(133.0, 40.65)

    def spy(_t, allow_blocking=True):
        seen["allow_blocking"] = allow_blocking
        return 40.65

    sc._get_live_premium_cached = spy
    sc._candidate_entry_mark_refusal(c)
    assert seen.get("allow_blocking") is False


def test_futures_are_not_premium_validated():
    sc, c = _cand(133.0, 40.65, inst="FUT")
    assert sc._candidate_entry_mark_refusal(c) == ""


def test_the_toggle_switches_it_off():
    from config.settings import get_settings

    setattr(get_settings(), "candidate_entry_mark_validation_enabled", False)
    sc, c = _cand(133.00, 40.65)
    assert sc._candidate_entry_mark_refusal(c) == ""


def test_the_manual_gate_actually_calls_it():
    """The whole point: the manual path bypassed open_trade, so the guard has to be
    reachable from there or it protects nothing."""
    import inspect

    src = inspect.getsource(Scanner)
    assert "_candidate_entry_mark_refusal(sig)" in src
    # Asserted by ORDER, not a character window — a fixed slice passes or fails on
    # unrelated edits and truncated this very assertion on the first run.
    idx = src.index("_candidate_entry_mark_refusal(sig)")
    stamped = src.index('setattr(sig, "accepted_by_gate", True)')
    assert idx < stamped, "the mark is validated after the candidate is already accepted"
    bail = src.find("continue", idx)
    assert 0 < bail < stamped, "a stale mark does not stop the acceptance"
    assert 0 < src.find("entry_mark_gate", idx) < stamped, (
        "the refusal is not recorded for the operator"
    )


# ── 2. entries into the EOD window ───────────────────────────────────────────

OPERATOR_SCHEDULE = {          # set by the operator on 2026-08-11
    "15min": dtime(14, 55),
    "5min": dtime(15, 1),
    "1min": dtime(15, 1),
    "force_exit": dtime(15, 15),
}


@pytest.mark.parametrize("tf,expected", [
    ("15min", OPERATOR_SCHEDULE["15min"]),
    ("5min", OPERATOR_SCHEDULE["5min"]),
    ("1min", OPERATOR_SCHEDULE["1min"]),
])
def test_the_operator_schedule_resolves(tf, expected):
    from config.settings import get_settings
    from engine.signal_processor import _entry_cutoff

    assert _entry_cutoff(get_settings(), tf)[1] == expected


def test_protection_engages_exactly_when_the_last_entry_is_allowed():
    """The invariant the whole fix rests on: no position can be opened inside the window
    that closes it. eod_protect_window_minutes is derived from the schedule (force exit
    15:15 minus 14 = 15:01 = the 5min cutoff), so changing one time without the other
    re-opens the overlap this file exists to close."""
    from config.settings import get_settings
    from engine.signal_processor import _entry_cutoff, _eod_protect_entry_cutoff

    s = get_settings()
    eod = _eod_protect_entry_cutoff(s)
    assert eod == OPERATOR_SCHEDULE["5min"]
    for tf in ("15min", "5min", "1min"):
        assert _entry_cutoff(s, tf)[1] <= eod, f"{tf} entries are admitted inside the window"


def test_the_bypass_lane_respects_the_schedule():
    """A power move is the one lane that skips ut_no_entry_after entirely, so its own
    cutoff is the only thing bounding it. At 15:15 it would have admitted an entry at
    the exact moment of force-exit."""
    from config.settings import get_settings

    raw = str(getattr(get_settings(), "power_move_late_hard_cutoff", "15:15"))
    assert datetime.strptime(raw, "%H:%M").time() <= OPERATOR_SCHEDULE["5min"]


def test_the_expiry_day_exit_still_precedes_force_exit():
    """It exists to close expiring contracts on their own schedule rather than in the
    final-minutes crunch; at 15:20 against a 15:15 force-exit that ordering inverted."""
    from config.settings import get_settings

    raw = str(getattr(get_settings(), "expiry_day_late_window_time_exit", "15:10"))
    assert datetime.strptime(raw, "%H:%M").time() < OPERATOR_SCHEDULE["force_exit"]


@pytest.mark.parametrize("hhmm,blocked", [
    ("14:30", False), ("14:55", False), ("15:00", False),
    ("15:01", True), ("15:09", True), ("15:11", True), ("15:14", True),
])
def test_entries_inside_the_eod_window_are_blocked(hhmm, blocked):
    """15:09 and 15:11 are the four trades this file is named for."""
    from config.settings import get_settings
    from engine.signal_processor import _entry_cutoff

    _, cut = _entry_cutoff(get_settings(), "5min")
    t = datetime.strptime(hhmm, "%H:%M").time()
    assert (t >= cut) is blocked


def test_the_cutoff_never_moves_later_than_configured():
    """It is a ceiling: a cutoff already earlier than EOD protection stays put."""
    from config.settings import get_settings
    from engine.signal_processor import _entry_cutoff

    s = get_settings()
    setattr(s, "ut_5min_no_entry_after", "14:00")
    _, cut = _entry_cutoff(s, "5min")
    assert cut == dtime(14, 0)


def test_the_gate_can_be_switched_off():
    """Off, the configured cutoff stands on its own — which is what it already equals
    under the operator schedule, so the cap is a guard rail rather than an override."""
    from config.settings import Settings, get_settings
    from engine.signal_processor import _entry_cutoff

    s = get_settings()
    setattr(s, "block_entries_during_eod_protect", False)
    _, cut = _entry_cutoff(s, "5min")
    raw = str(Settings.model_fields["ut_5min_no_entry_after"].default)
    assert cut == datetime.strptime(raw, "%H:%M").time()


def test_eod_protection_disabled_leaves_the_cutoff_alone():
    from config.settings import Settings, get_settings
    from engine.signal_processor import _entry_cutoff

    s = get_settings()
    setattr(s, "eod_protect_enabled", False)
    _, cut = _entry_cutoff(s, "5min")
    raw = str(Settings.model_fields["ut_5min_no_entry_after"].default)
    assert cut == datetime.strptime(raw, "%H:%M").time()


def test_a_later_configured_cutoff_is_still_capped():
    """The guard rail must survive someone widening the entry window again."""
    from config.settings import get_settings
    from engine.signal_processor import _entry_cutoff

    s = get_settings()
    setattr(s, "ut_5min_no_entry_after", "15:14")
    _, cut = _entry_cutoff(s, "5min")
    assert cut == OPERATOR_SCHEDULE["5min"], "a widened cutoff escaped the EOD cap"


@pytest.mark.parametrize("label", ["15:25", "15:25:00", "nonsense", ""])
def test_a_malformed_force_exit_time_never_crashes_the_gate(label):
    from config.settings import get_settings
    from engine.signal_processor import _entry_cutoff

    s = get_settings()
    setattr(s, "ut_force_exit_time", label)
    _, cut = _entry_cutoff(s, "5min")
    assert isinstance(cut, dtime)


# ── 3. protecting less than the round trip ───────────────────────────────────

def _sc_trade():
    sc = Scanner.__new__(Scanner)
    sc.latest_regimes = {}
    t = SimpleNamespace(
        instrument="BANKNIFTY 57400 PE", inst_type="OPT", market_regime="RANGING",
    )
    return sc, t


EOD = IST.localize(datetime(2026, 8, 11, 15, 11, 0))
MIDDAY = IST.localize(datetime(2026, 8, 11, 12, 0, 0))


def test_the_banknifty_peak_no_longer_arms_the_giveback_guard():
    """Rs.36 of gross peak against a Rs.80 round trip."""
    sc, t = _sc_trade()
    arm = sc._profit_giveback_arm(1000.0, now=EOD, trade_like=t)
    assert arm >= 80.0
    assert 36.0 < arm, "a Rs.36 peak still arms a protective exit"


def test_the_eod_collapse_is_floored_at_the_round_trip():
    """Without the floor the EOD arm is eod_protect_min_peak_rs — Rs.1."""
    from config.settings import get_settings

    sc, t = _sc_trade()
    setattr(get_settings(), "profit_protect_cost_floor_enabled", False)
    assert sc._profit_giveback_arm(1000.0, now=EOD, trade_like=t) == pytest.approx(1.0)


def test_the_profit_lock_arm_gets_the_same_floor():
    sc, t = _sc_trade()
    assert sc._profit_lock_min_peak(False, 1000.0, "5min", EOD, trade_like=t) >= 80.0


def test_midday_arms_are_untouched():
    """The floor may only ever RAISE an arm, and Rs.80 sits far below both."""
    sc, t = _sc_trade()
    assert sc._profit_giveback_arm(1000.0, now=MIDDAY, trade_like=t) == pytest.approx(500.0)
    assert sc._profit_lock_min_peak(False, 1000.0, "5min", MIDDAY, trade_like=t) == pytest.approx(250.0)


def test_the_floor_follows_the_configured_cost():
    from config.settings import get_settings

    sc, t = _sc_trade()
    setattr(get_settings(), "opt_cost", 250.0)
    assert sc._profit_giveback_arm(1000.0, now=EOD, trade_like=t) == pytest.approx(250.0)


def test_the_floor_can_be_switched_off_and_scaled():
    from config.settings import get_settings

    sc, t = _sc_trade()
    setattr(get_settings(), "profit_protect_min_cost_multiple", 2.0)
    assert sc._profit_giveback_arm(1000.0, now=EOD, trade_like=t) == pytest.approx(160.0)
    setattr(get_settings(), "profit_protect_min_cost_multiple", 0.0)
    assert sc._profit_giveback_arm(1000.0, now=EOD, trade_like=t) == pytest.approx(1.0)


def test_an_unknown_position_cannot_lower_an_arm():
    """trade_like=None must return 0.0 from the floor, never a value that raises it
    unexpectedly or one that drops an arm."""
    sc, _ = _sc_trade()
    assert sc._profit_protect_cost_floor_rs(None) >= 0.0
    assert sc._profit_giveback_arm(1000.0, now=MIDDAY) == pytest.approx(500.0)


def test_the_loss_side_already_had_this_and_still_does():
    """peak_threat_exit_loss_min_cost_multiple is the precedent this mirrors; if it ever
    disappears the two sides have drifted apart again."""
    import inspect

    src = inspect.getsource(Scanner)
    assert "peak_threat_exit_loss_min_cost_multiple" in src
    assert "_round_trip_cost_rs" in src
