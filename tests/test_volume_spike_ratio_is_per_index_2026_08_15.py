"""A flat volume-ratio floor is not one filter; it is four different ones.

Measured over 24 sessions (2026-07-14..08-14, 1,650 five-minute bars per index)
against the profiler's own time-of-day baseline — same clock slot, prior days,
last 10, non-zero preferred — the live 2.0x floor sits somewhere completely
different in each index's distribution:

    index        median   p90    p95    p99  | flat 2.0x fires on
    NIFTY          0.94   1.42   1.63   2.03 |   1.3% of bars
    BANKNIFTY      0.83   1.70   2.14   3.76 |   6.2% of bars
    MIDCPNIFTY     0.88   1.70   2.13   4.07 |   6.3% of bars
    SENSEX         0.74   2.35   3.71   9.85 |  12.4% of bars

The spike lane opens nearly ten times more often on SENSEX than on NIFTY from a
single setting. SENSEX is the thinnest of the four by absolute volume — median
137k a bar against NIFTY's 3.5m — and thin books print lumpily, so its ratios
scatter far wider.

The defaults are each index's own 95th percentile, which puts the lane on the top
5.0% of bars — except NIFTY, held at the live 2.0 rather than its p95 of 1.63,
because nothing here should make this lane MORE active. SENSEX tightens from 12.4%
of bars to 5%; nothing is loosened.

WHAT THIS DOES NOT DO
Equalising selectivity does not manufacture signal. Over the same 24 sessions the
forward-move lift at >=2.5x, verified across 1, 3 and 6 bar horizons, was:

    BANKNIFTY   1.22x / 1.59x / 1.64x   (61 samples)  - a real edge
    MIDCPNIFTY  0.97x / 1.19x / 1.05x   (60 samples)  - inconsistent
    SENSEX      1.02x / 1.03x / 1.07x  (153 samples)  - essentially none
    NIFTY       too few to measure       (8 samples)

Tested in the OTHER direction — starting from the moves rather than the volume —
it is worth nothing anywhere. Median volume ratio of the top 1% of forward moves:
NIFTY 1.01, BANKNIFTY 0.92, SENSEX 0.74, MIDCPNIFTY 0.73 — at or BELOW average.
Sweeping 0.8x to 2.5x, big-move retention over all-bar retention sits at ~1.00
throughout, and degrades at the top: MIDCPNIFTY 0.19 at 2.0x and 0.00 at 2.5x,
NIFTY 0.00 at 2.5x, SENSEX under 1.00 everywhere.

So this lane is kept selective because it has no measured edge, not because volume
is informative. Volume never blocks a normal entry here — the stabilization thrust
only SHORTENS a wait and is OR'd with range expansion — so a tight bar costs no
setups. This corrects an earlier audit note that read SENSEX's low
logged ratios as thin participation suppressing its entries: SENSEX in fact clears
the spike bar more often than any other index, and on the weakest evidence.
"""
import pytest

from config.settings import get_settings
from engine.signal_quality_policy import volume_spike_min_ratio


class _S:
    """Settings stub — the resolver must not require a full Settings model."""

    def __init__(self, base=2.0, per_index=None):
        self.volume_spike_entry_min_volume_ratio = base
        if per_index is not None:
            self.volume_spike_min_ratio_by_index = per_index


# ── per-index resolution ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("NIFTY", 2.00),
    ("BANKNIFTY", 2.14),
    ("MIDCPNIFTY", 2.13),
    ("SENSEX", 3.71),
])
def test_each_index_resolves_to_its_own_measured_floor(name, expected):
    assert volume_spike_min_ratio(get_settings(), name) == pytest.approx(expected)


def test_sensex_is_the_highest_bar_because_its_ratios_scatter_widest():
    s = get_settings()

    assert volume_spike_min_ratio(s, "SENSEX") > volume_spike_min_ratio(s, "BANKNIFTY")
    assert volume_spike_min_ratio(s, "SENSEX") > volume_spike_min_ratio(s, "NIFTY")


def test_nifty_is_held_at_the_live_flat_value_not_its_own_p95():
    """Its p95 is 1.63, which would take the lane from 1.3% of bars to 5%. With no
    measured edge, nothing here may make this lane MORE active."""
    assert volume_spike_min_ratio(get_settings(), "NIFTY") == pytest.approx(2.00)


def test_no_index_is_loosened_below_the_live_flat_floor():
    """SENSEX tightens (12.4% -> 5% of bars); nothing goes the other way."""
    s = get_settings()

    for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"):
        assert volume_spike_min_ratio(s, name) >= 2.0 - 1e-9


def test_the_case_of_the_name_does_not_matter():
    assert volume_spike_min_ratio(get_settings(), "sensex") == pytest.approx(3.71)
    assert volume_spike_min_ratio(get_settings(), " SenSex ") == pytest.approx(3.71)


# ── falling back to the flat setting ─────────────────────────────────────────

def test_an_unknown_instrument_uses_the_flat_floor():
    assert volume_spike_min_ratio(_S(base=2.0), "GOLDBEES") == pytest.approx(2.0)


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_no_instrument_uses_the_flat_floor(blank):
    assert volume_spike_min_ratio(_S(base=2.0), blank) == pytest.approx(2.0)


def test_an_empty_map_restores_the_previous_behaviour():
    """The escape hatch: nothing per-index, one number everywhere, as before."""
    s = _S(base=2.0, per_index={})

    for name in ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"):
        assert volume_spike_min_ratio(s, name) == pytest.approx(2.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, None, "junk"])
def test_an_unusable_per_index_value_falls_back(bad):
    s = _S(base=2.0, per_index={"NIFTY": bad})

    assert volume_spike_min_ratio(s, "NIFTY") == pytest.approx(2.0)


def test_a_missing_map_attribute_does_not_raise():
    """Settings stubs in other tests will not carry the new field."""
    assert volume_spike_min_ratio(_S(base=2.0), "NIFTY") == pytest.approx(2.0)


# ── the qualifier actually uses it ───────────────────────────────────────────

def _confirm(instrument, volume_ratio):
    from engine.signal_quality_policy import volume_spike_entry_confirmed

    return volume_spike_entry_confirmed(
        timeframe="5min",
        volume_ratio=volume_ratio,
        range_atr_ratio=99.0,      # generous: isolate the volume floor
        close_location=1.0,
        adx_value=40.0,
        intel_score=90.0,
        power_move=True,
        settings=get_settings(),
        signal_type="BUY",
        instrument=instrument,
    )


def test_a_ratio_between_the_two_floors_separates_the_indices():
    """2.5x clears NIFTY's 2.00 bar and fails SENSEX's 3.71 — the whole point."""
    assert _confirm("NIFTY", 2.5) is True
    assert _confirm("SENSEX", 2.5) is False


def test_sensex_still_qualifies_above_its_own_bar():
    assert _confirm("SENSEX", 4.0) is True


def test_nifty_below_its_own_bar_is_refused():
    assert _confirm("NIFTY", 1.9) is False
