"""The explosive-spike tier must stay above the range tier that gates it.

power_move_confirmed uses two range multiples:

    power_move_range_atr_mult        ordinary range expansion, a GATE
    power_move_spike_range_atr_mult  explosive spike, WAIVES 15m alignment

    range_ok     = range_atr_ratio >= range_mult
    strong_spike = range_atr_ratio >= spike_mult
    align_ok     = trend_15m_agrees or (spike_bypass and strong_spike and ...)

The bypass exists because a genuine spike LEADS the 15-minute bar, so demanding
15m agreement would make post-2:59pm spikes structurally uncatchable. That
reasoning only holds while "explosive" is a HIGHER bar than "expanded".

Code ships 2.0 and 2.5. On 2026-08-13 the operator's .env set the spike multiple
to 2.0 — equal to the range gate — so every candidate clearing range_ok also
counted as an explosive spike, align_ok was always satisfied through the bypass,
and the 15-minute alignment requirement became unreachable for power moves. Two
independent numbers, nothing enforcing the ordering between them.

The spike multiple is now DERIVED: whatever the range gate is set to, the spike
tier keeps the separation the code defaults express (2.5 / 2.0 = 1.25x). An
operator raising the range gate carries the spike tier with it, and neither can
be configured into the other.
"""
import pytest

from engine.signal_quality_policy import power_move_confirmed, power_move_spike_threshold


class _S:
    """Only the knobs power_move_confirmed reads."""

    def __init__(self, **kw):
        self.power_move_override_enabled = True
        self.power_move_volume_mult = 2.5
        self.power_move_range_atr_mult = 2.0
        self.power_move_spike_range_atr_mult = 2.5
        self.power_move_close_location = 0.70
        self.power_move_spike_bypass_15m = True
        self.momentum_override_threshold = 25.0
        for k, v in kw.items():
            setattr(self, k, v)


# ── the tier cannot be configured into the gate ──────────────────────────────

def test_a_collapsed_spike_tier_is_lifted_above_the_range_gate():
    """The exact 2026-08-13 .env shape: spike 2.0 against a range gate of 2.0."""
    settings = _S(power_move_range_atr_mult=2.0, power_move_spike_range_atr_mult=2.0)

    assert power_move_spike_threshold(settings) > 2.0


def test_the_separation_follows_the_range_gate():
    """Raise the gate and the spike tier rises with it — not a fixed constant."""
    low = power_move_spike_threshold(_S(power_move_range_atr_mult=2.0,
                                        power_move_spike_range_atr_mult=2.0))
    high = power_move_spike_threshold(_S(power_move_range_atr_mult=3.0,
                                         power_move_spike_range_atr_mult=3.0))

    assert high > low


def test_the_code_defaults_are_preserved_untouched():
    """2.5 already sits above 2.0; nothing is invented for a sane configuration."""
    assert power_move_spike_threshold(_S()) == pytest.approx(2.5)


def test_an_operator_may_still_raise_the_spike_tier():
    """Only the collapse is corrected; a deliberately higher bar is honoured."""
    settings = _S(power_move_range_atr_mult=2.0, power_move_spike_range_atr_mult=4.0)

    assert power_move_spike_threshold(settings) == pytest.approx(4.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, None, "junk"])
def test_an_unusable_spike_setting_falls_back_to_the_derived_tier(bad):
    settings = _S(power_move_range_atr_mult=2.0, power_move_spike_range_atr_mult=bad)

    assert power_move_spike_threshold(settings) > 2.0


# ── the behaviour that was lost ──────────────────────────────────────────────

def _pm(settings, **kw):
    base = dict(volume_ratio=6.0, range_atr_ratio=3.0, close_location=0.88,
                adx_value=30.0, intel_score=0.3, trend_15m_agrees=True,
                settings=settings)
    base.update(kw)
    return power_move_confirmed(**base)


def test_a_sub_spike_still_needs_15m_even_with_the_tiers_configured_equal():
    """2.2x cleared the 2.0 range gate and, with the tiers equal, also counted as
    explosive — waiving the alignment the sub-spike case exists to require."""
    settings = _S(power_move_range_atr_mult=2.0, power_move_spike_range_atr_mult=2.0)

    assert _pm(settings, trend_15m_agrees=False, range_atr_ratio=2.2) is False


def test_a_genuine_explosive_spike_still_bypasses_15m():
    settings = _S(power_move_range_atr_mult=2.0, power_move_spike_range_atr_mult=2.0)

    assert _pm(settings, trend_15m_agrees=False, range_atr_ratio=3.0) is True


def test_15m_agreement_still_admits_a_sub_spike():
    settings = _S(power_move_range_atr_mult=2.0, power_move_spike_range_atr_mult=2.0)

    assert _pm(settings, trend_15m_agrees=True, range_atr_ratio=2.2) is True
