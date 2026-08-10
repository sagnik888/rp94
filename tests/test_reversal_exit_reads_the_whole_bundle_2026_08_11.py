"""The reversal exit judged a live trade on 3 pillars while holding 8.

`_intel_reversal_reason` read PCR, OI and order flow. The same bundle already carries
volume, regime, session context and a divergence alert — it simply never looked. Same
shape as `readiness`, `components_analyzed`, `metric_kind` and `effective_volume_ratio`:
computed, published, read by nobody.

WIDENING IT NAIVELY IS WRONG IN BOTH DIRECTIONS, which is why this is not just "add
three voters":

  * keep min_votes at 2/3 and add three more ways to reach it -> every exit gets easier
    and winners get cut;
  * scale min_votes to 4/6 -> a LOSING trade can no longer reach it, because the added
    pillars are usually NEUTRAL, so the loss-guard reversal stops firing and losers are
    held longer.

Both are the failure this codebase keeps repeating: a threshold inherited across a change
in what it measures. So corroboration may only move the bar one vote, and only the safe
way:

    losing  + extras CONFIRM -> exit one vote SOONER   (cut the loser faster)
    winning + extras OPPOSE  -> demand one vote MORE   (protect the runner)
    anything else            -> unchanged

divergence_alert is a BOOLEAN trap flag (price rising while order flow distributes), not
a direction, so it counts toward CONFIRM only: "price and flow disagree" is a reason to
doubt a position, never a reason to hold it.
"""
from types import SimpleNamespace

import pytest

from scanner import Scanner


@pytest.fixture(autouse=True)
def _pinned():
    from config.settings import get_settings

    s = get_settings()
    saved = (
        getattr(s, "intel_reversal_corroboration_enabled", True),
        getattr(s, "intel_reversal_corroboration_votes", 2),
        getattr(s, "loss_guard_oi_pcr_votes_losing", 2),
        getattr(s, "loss_guard_oi_pcr_votes_winning", 3),
    )
    yield s
    (s.intel_reversal_corroboration_enabled, s.intel_reversal_corroboration_votes,
     s.loss_guard_oi_pcr_votes_losing, s.loss_guard_oi_pcr_votes_winning) = saved


def _sc():
    sc = Scanner.__new__(Scanner)
    sc._setting_enabled = lambda v, d=False: bool(v)
    return sc


def _bundle(*, pcr="NEUTRAL", oi="NEUTRAL", flow="NEUTRAL", flow_ratio=1.0,
            volume="NEUTRAL", regime="", divergence=False):
    return {
        "pcr": {"contrarian_signal": pcr},
        "oi": {"signal": oi},
        "order_flow": {"signal": flow, "ratio": flow_ratio},
        "volume": {"signal": volume},
        "regime": {"smoothed_regime": regime},
        "divergence_alert": divergence,
    }


def _reason(sc, direction, bundle, **kw):
    return Scanner._intel_reversal_reason(sc, direction, bundle, **kw)


# ── the 3-pillar behaviour must be preserved exactly when extras are neutral ──

def test_two_core_votes_still_exits_a_loser():
    r = _reason(_sc(), "LONG", _bundle(pcr="BEARISH", oi="BEARISH"), is_losing=True)
    assert r and "bearish votes=2/2" in r


def test_two_core_votes_does_not_exit_a_winner():
    assert _reason(_sc(), "LONG", _bundle(pcr="BEARISH", oi="BEARISH"), is_losing=False) is None


def test_three_core_votes_exits_a_winner():
    r = _reason(_sc(), "LONG",
                _bundle(pcr="BEARISH", oi="BEARISH", flow="BEARISH"), is_losing=False)
    assert r and "votes=3/3" in r


def test_no_votes_never_exits():
    assert _reason(_sc(), "LONG", _bundle(), is_losing=True) is None


# ── corroboration accelerates a LOSING exit, by exactly one vote ──────────────

def test_a_corroborated_loser_exits_one_vote_sooner():
    b = _bundle(pcr="BEARISH", volume="BEARISH_VOLUME", regime="TRENDING_DOWN")
    r = _reason(_sc(), "LONG", b, is_losing=True)
    assert r and "votes=1/1" in r and "corroborated" in r


def test_the_divergence_flag_counts_toward_corroboration():
    b = _bundle(pcr="BEARISH", volume="BEARISH_VOLUME", divergence=True)
    r = _reason(_sc(), "LONG", b, is_losing=True)
    assert r and "corroborated" in r and "divergence" in r


def test_one_corroborating_pillar_is_not_enough():
    b = _bundle(pcr="BEARISH", regime="TRENDING_DOWN")
    assert _reason(_sc(), "LONG", b, is_losing=True) is None


def test_corroboration_never_fires_an_exit_with_zero_core_votes():
    """The core pillars remain necessary; extras only move the bar."""
    b = _bundle(volume="BEARISH_VOLUME", regime="TRENDING_DOWN", divergence=True)
    assert _reason(_sc(), "LONG", b, is_losing=True) is None


# ── opposition PROTECTS a winner, by exactly one vote ────────────────────────

def test_a_contested_winner_needs_one_more_vote():
    b = _bundle(pcr="BEARISH", oi="BEARISH", flow="BEARISH",
                volume="BULLISH_VOLUME", regime="TRENDING_UP")
    assert _reason(_sc(), "LONG", b, is_losing=False) is None, (
        "a 3-vote reversal against a strongly bullish tape still cut the winner"
    )


def test_opposition_does_not_protect_a_loser():
    """A losing trade must never be held open by contrary extras."""
    b = _bundle(pcr="BEARISH", oi="BEARISH",
                volume="BULLISH_VOLUME", regime="TRENDING_UP")
    r = _reason(_sc(), "LONG", b, is_losing=True)
    assert r is not None, "opposition delayed a losing exit"


# ── SHORT side mirrors ───────────────────────────────────────────────────────

def test_short_side_corroboration():
    b = _bundle(pcr="BULLISH", volume="BULLISH_VOLUME", regime="TRENDING_UP")
    r = _reason(_sc(), "SHORT", b, is_losing=True)
    assert r and "bullish" in r and "corroborated" in r


def test_short_side_opposition_protects_a_winner():
    b = _bundle(pcr="BULLISH", oi="BULLISH", flow="BULLISH",
                volume="BEARISH_VOLUME", regime="TRENDING_DOWN")
    assert _reason(_sc(), "SHORT", b, is_losing=False) is None


# ── the switch, and robustness ───────────────────────────────────────────────

def test_it_can_be_disabled_back_to_pure_three_pillar(_pinned):
    _pinned.intel_reversal_corroboration_enabled = False
    b = _bundle(pcr="BEARISH", volume="BEARISH_VOLUME", regime="TRENDING_DOWN")
    assert _reason(_sc(), "LONG", b, is_losing=True) is None


@pytest.mark.parametrize("bad", [None, {}, {"volume": None}, {"regime": "notadict"},
                                 {"volume": {}, "regime": {}, "divergence_alert": None}])
def test_a_malformed_bundle_does_not_raise(bad):
    sc = _sc()
    assert Scanner._intel_reversal_corroboration(sc, "LONG", bad) is not None


def test_corroboration_counts_are_bounded():
    sc = _sc()
    b = _bundle(volume="BEARISH_VOLUME", regime="TRENDING_DOWN", divergence=True)
    confirm, oppose, detail = Scanner._intel_reversal_corroboration(sc, "LONG", b)
    assert confirm == 3 and oppose == 0
    assert set(detail.split("+")) == {"vol", "regime", "divergence"}


def test_an_unknown_regime_label_abstains():
    sc = _sc()
    b = _bundle(regime="RANGING")
    confirm, oppose, _ = Scanner._intel_reversal_corroboration(sc, "LONG", b)
    assert confirm == 0 and oppose == 0


def test_the_settings_resolve():
    from config.settings import get_settings

    s = get_settings()
    assert isinstance(getattr(s, "intel_reversal_corroboration_enabled"), bool)
    assert int(getattr(s, "intel_reversal_corroboration_votes")) >= 1
