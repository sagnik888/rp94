"""Exception-route intelligence floors were inherited across a scale change.

The aggregate normaliser changed denominator twice:

    restore_point_51   final_score = (score / components) * 100      <- pillar COUNT
    rp46-2.1           averaged    =  score / components             <- pillar COUNT
    current            averaged    =  score / max(mass, 1.30)        <- pillar WEIGHT MASS

Dividing a WEIGHTED sum by a plain COUNT is dimensionally wrong, so the current form is
the correct one — and it is the only one whose output is interpretable, because it reads
as "fraction of a fully-informed max-conviction read" on a fixed 0..1 scale.

But the four min_intel_score thresholds are byte-identical in all three builds. They were
never re-cut, so the same market evidence now clears them far more easily. Measured on
2026-08-11 over 824 real aggregator reads across the stored NIFTY/BANKNIFTY/SENSEX/
MIDCPNIFTY 5min and 15min candles:

    threshold   admitted     1 in N
       0.20       39.1%        2.6      <- was the floor for two routes
       0.25       24.4%        4.1      <- was the floor for the third
       0.35       14.8%        6.8      <- chosen
       0.40        7.8%       12.9
       0.50        2.4%       41.2
       0.70        0.0%      never      <- rare_burst: unreachable

Two of every five reads clearing an EXCEPTION gate is not an exception. 0.35 lands at
roughly 1 in 7. It was not raised further because that measurement is the thin-evidence
regime (no option chain: evidence_mass averaged 0.80 of a possible 2.02, so the 1.30
FULL_EVIDENCE_MASS floor bound on every read) — 0.40/0.50 could silence these routes
outright in a condition this sample cannot see.

rare_burst is deliberately LEFT at 0.70 even though it is provably unreachable. Lowering
it would switch on a dormant speculative entry route, which is the opposite of filtering.
Its being dead is recorded here so nobody "fixes" it by accident.
"""
import pytest

from config.settings import get_settings

TIGHTENED = (
    "missed_signal_reentry_min_intel_score",
    "fifteen_min_choppy_scalp_min_intel_score",
    "midcap_15min_risky_stabilization_min_intel_score",
)


@pytest.mark.parametrize("key", TIGHTENED)
def test_the_exception_floors_are_at_the_measured_level(key):
    assert float(getattr(get_settings(), key)) == pytest.approx(0.35)


@pytest.mark.parametrize("key", TIGHTENED)
def test_they_are_meaningfully_above_the_median_read(key):
    """Median |score_norm| measured at 0.185. A floor at or below that admits half of
    everything and is not a filter at all."""
    assert float(getattr(get_settings(), key)) > 0.185 * 1.5


@pytest.mark.parametrize("key", TIGHTENED)
def test_they_are_not_pushed_into_the_unreachable_band(key):
    """p99 of the measured distribution was 0.554 and the max 0.646. A floor above that
    silences the route the way rare_burst is silenced."""
    assert float(getattr(get_settings(), key)) <= 0.55


def test_rare_burst_is_left_dead_on_purpose():
    """Unreachable in the measured regime (0/824 reads). Left alone: lowering it would
    ENABLE a speculative route rather than filter one."""
    assert float(getattr(get_settings(), "rare_burst_min_intel_score")) >= 0.70


def test_the_dotenv_and_dataclass_agree():
    """.env overrides the dataclass in this project, so a change to one and not the other
    is silently inert — the exact trap that made these three look tuned when they were not.
    """
    import os
    import re

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        pytest.skip(".env not present in this checkout")
    env = open(path, encoding="utf-8", errors="replace").read()
    for key in TIGHTENED:
        m = re.search(rf"^{key.upper()}\s*=\s*'?([0-9.]+)'?", env, re.M)
        if not m:
            continue
        assert float(m.group(1)) == pytest.approx(float(getattr(get_settings(), key))), (
            f"{key}: .env and settings disagree; the dataclass value would be ignored"
        )


# ── the normaliser these floors are calibrated against ───────────────────────

def test_the_scale_is_still_fraction_of_full_evidence():
    """If the denominator ever reverts to a pillar COUNT, every number above is wrong
    again and these floors must be recut."""
    import inspect

    import intelligence.intelligence_aggregator as agg

    src = inspect.getsource(agg)
    assert "score / max(max_possible, FULL_EVIDENCE_MASS)" in src.replace("\n", " ").replace("  ", " ") or (
        "max(max_possible, FULL_EVIDENCE_MASS)" in src
    ), "the aggregate normaliser changed; recalibrate the exception floors"
    assert "FULL_EVIDENCE_MASS = 1.30" in src


def test_the_canonical_scale_is_bounded():
    """The floors assume a 0..1 contract."""
    from engine.intelligence_score import aggregate_intelligence_score

    for raw in (-99.0, -1.5, 0.0, 1.5, 99.0):
        v = aggregate_intelligence_score({"score_norm": raw})
        assert -1.0 <= v <= 1.0
