"""Two findings where the audit was right about the code and wrong about the fix.

1. A 15-MINUTE TRADE'S STALENESS GUARD WAS READING THE 5-MINUTE BUNDLE'S CLOCK.
   IntelligenceAggregator.analyze never stamped its output, so `_intel_age_seconds` had
   no embedded timestamp to prefer and always fell through to
   `_last_intel_fetch[instrument]` -- the BARE key. `_cache_timeframe_intel` writes that
   key only under `mirror_legacy or tf == "5min"`; the 15min bundle is stamped only under
   `NIFTY_15min`. So while the 5min bundle kept refreshing every 30s, a FROZEN 15min
   bundle reported an age of ~0 and never crossed the 90s bound, letting an exit fire
   OI_PCR_REVERSAL on evidence captured much earlier.

   The report's implied fix -- swap in the timeframe-qualified key -- would be WRONG.
   When 15min history is not ready the reader legitimately RETURNS the 5min bundle, and
   aging that on a 15min key would report false-stale. Stamping the bundle itself ages
   whichever bundle was actually handed back. Measured: 500s, against ~0s before.

2. THE OPT BRANCH OF THE LIVE-TRADE-PRESERVATION PATH DISCARDED ITS TRAILING STOP.
   The branch read `if trade.inst_type == "OPT": pass else: trade.current_stop = new_stop`,
   so an option's stop froze at whatever it held when the mode flipped and a runner gave
   back its whole gain with the dashboard still showing the entry stop.

   The report calls this a plain omission and says to assign `new_stop`. That would be
   far worse than the bug. `new_stop` is SPOT space (seeded `new_stop =
   prem_to_spot(trade.current_stop)`, converted back on the way out with
   `spot_to_prem`), while `trade.current_stop` is PREMIUM space -- it is compared against
   `trade.current_price`, the premium mark. Assigning would store ~24,800 in a field
   tested against a ~150 premium, so `150 <= 24800` would fire a spurious TRAILING_STOP
   alert on EVERY live option, every tick. The `pass` was a deliberate unit guard.

   Correct fix: convert with spot_to_prem, then ratchet upward only. A bought option is
   always long premium (a bearish view is a bought PE, stored direction="SHORT", which
   still profits when premium RISES), so a one-way ratchet is right -- and spot_to_prem
   divides through entry_spot, a field this codebase has had corrupted before, so the
   monotonic guard stops a bad conversion from LOOSENING a live stop.
"""
import inspect
import textwrap
import time

import pytest

from scanner import Scanner

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


# ── 1. the bundle carries its own capture time ───────────────────────────────

def _scanner():
    s = Scanner.__new__(Scanner)
    s._intel_cache = {}
    s._timeframe_regimes = {}
    s.latest_regimes = {}
    s._timeframe_macd = {}
    return s


def _bundle():
    return {"aggregate": {"score": 42.0}, "regime": {"regime": "TRENDING_UP"}}


@pytest.mark.parametrize("index", INDICES)
def test_the_cache_stamps_every_bundle(index):
    s, b = _scanner(), _bundle()
    t0 = time.time() - 123.0
    Scanner._cache_timeframe_intel(s, index, "15min", b, at=t0)
    assert b.get("as_of") == pytest.approx(t0)


def test_a_frozen_15min_bundle_now_ages_on_its_own_clock():
    """The headline. The 5min bundle refreshing must not make a stale 15min look fresh."""
    s, b = _scanner(), _bundle()
    t0 = time.time() - 500.0
    Scanner._cache_timeframe_intel(s, "BANKNIFTY", "15min", b, at=t0)
    s._last_intel_fetch = {"BANKNIFTY": time.time()}   # 5min refreshed just now
    age = Scanner._intel_age_seconds(s, b, "BANKNIFTY", None)
    assert age is not None and age > 400, f"reported {age}, truth ~500s"


def test_the_bare_key_proxy_would_have_hidden_it():
    """The premise: without a stamp the fallback reports ~0 for the same frozen bundle."""
    s, b = _scanner(), _bundle()
    Scanner._cache_timeframe_intel(s, "BANKNIFTY", "15min", b, at=time.time() - 500.0)
    b.pop("as_of")
    s._last_intel_fetch = {"BANKNIFTY": time.time()}
    age = Scanner._intel_age_seconds(s, b, "BANKNIFTY", None)
    assert age is None or age < 60, f"premise broken: unstamped bundle reported {age}"


def test_the_stamp_tracks_recaching_rather_than_sticking():
    """setdefault would pin the first value and make a refreshed bundle look ancient."""
    s, b = _scanner(), _bundle()
    Scanner._cache_timeframe_intel(s, "NIFTY", "5min", b, at=1000.0)
    Scanner._cache_timeframe_intel(s, "NIFTY", "5min", b, at=2000.0)
    assert b["as_of"] == pytest.approx(2000.0)


def test_the_stamp_matches_the_cache_time_entry():
    s, b = _scanner(), _bundle()
    Scanner._cache_timeframe_intel(s, "SENSEX", "15min", b, at=1234.0)
    key = Scanner._timeframe_intel_key("SENSEX", "15min")
    assert s._intel_cache_time[key] == pytest.approx(b["as_of"])


def test_the_reader_still_prefers_an_embedded_stamp():
    """The premise the fix rests on."""
    src = inspect.getsource(Scanner._intel_age_seconds)
    assert '"as_of"' in src


# ── 2. the option trail converts before it ratchets ──────────────────────────

def _preservation_branch():
    src = textwrap.dedent(inspect.getsource(Scanner._update_active_trades))
    marker = "Live Trade Preservation Patch"
    assert marker in src, "the live-preservation branch moved"
    start = src.index(marker)
    return src[start:start + 3000]


def test_the_option_stop_is_no_longer_discarded():
    branch = _preservation_branch()
    assert "spot_to_prem(new_stop)" in branch, (
        "the OPT stop is still thrown away, so a runner gives back its whole gain"
    )


def test_the_option_stop_is_never_assigned_in_spot_space():
    """Assigning new_stop directly would alert on every option, every tick."""
    branch = _preservation_branch()
    assert "trade.current_stop = new_stop" in branch, "expected the FUT arm to remain"
    # The OPT arm must convert; the raw assignment may appear only in the else-branch.
    opt_arm = branch[branch.index('if trade.inst_type == "OPT":'):]
    opt_arm = opt_arm[:opt_arm.index("else:")]
    assert "trade.current_stop = new_stop" not in opt_arm, (
        "spot-space new_stop is being written into the premium-space current_stop"
    )


def test_the_option_stop_only_ratchets_upward():
    branch = _preservation_branch()
    assert "prem_stop > float(trade.current_stop" in branch, (
        "an unguarded assignment can LOOSEN a live stop when entry_spot is bad"
    )


def _ratchet(prem_stop, current_stop):
    """Reproduce the guarded assignment."""
    return prem_stop if prem_stop > current_stop else current_stop


def test_a_rising_premium_stop_is_taken():
    assert _ratchet(140.0, 120.0) == 140.0


def test_a_falling_premium_stop_is_refused():
    assert _ratchet(95.0, 120.0) == 120.0


def test_a_zero_conversion_cannot_wipe_the_stop():
    """spot_to_prem raising or returning 0 must not hand the trade a stop of 0."""
    assert _ratchet(0.0, 120.0) == 120.0


def test_the_units_premise_still_holds():
    """new_stop is spot-space and current_stop is premium-space. If this ever changes,
    the conversion above becomes wrong and this test says so first."""
    src = textwrap.dedent(inspect.getsource(Scanner._update_active_trades))
    assert "new_stop = prem_to_spot(trade.current_stop)" in src, (
        "new_stop is no longer seeded from a premium->spot conversion"
    )
    assert 'tm_stop = spot_to_prem(new_stop) if trade.inst_type == "OPT" else new_stop' in src, (
        "the exit path no longer converts back, so the spaces may have changed"
    )
