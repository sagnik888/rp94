"""ATM vs ITM: break a near-tie on tradeability, never override a better setup.

option_strike_selection is "BOTH", so an ATM and an ITM candidate are scored on every
option entry and `results_list.sort(key=score)[0]` wins. Nothing in that score
describes how tradeable the contract is, so two setups of near-identical quality were
separated by whatever the score rounded to and the thinner strike won as often as not.
Delta is already computed per strike, so pricing was correct; only the choice between
the two candidates was unranked.

The data needed was already being fetched: the chain carries call_oi / call_volume /
put_oi / put_volume PER STRIKE. The ranking reads the CACHED chain
(allow_fetch=False), so it costs no broker call and adds no latency to an entry.

Two deliberate constraints:
  * TIE-BREAK ONLY. Liquidity may decide only within option_strike_liquidity_score_band,
    so a materially better setup always wins.
  * BOTH OI AND VOLUME must be better by option_strike_liquidity_min_advantage. A
    weighted blend would need a weight nobody can justify; requiring both is scale-free
    and means "more standing interest AND more activity today".

Absent liquidity is treated as NO OPINION, not as illiquid — an unreadable chain must
never move the selection.
"""
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import Settings
from engine.signal_processor import SignalProcessor


S = SignalProcessor


def _chain(rows):
    """A chain shaped like the real one: per-strike OI and volume for both sides."""
    return pd.DataFrame(rows)


CHAIN = _chain(
    [
        {"strike": 24300.0, "call_oi": 100_000, "call_volume": 50_000,
         "put_oi": 80_000, "put_volume": 40_000},
        {"strike": 24250.0, "call_oi": 10_000, "call_volume": 2_000,
         "put_oi": 500_000, "put_volume": 250_000},
    ]
)


def _cand(score, oi, vol, symbol="X"):
    return SimpleNamespace(score=score, strike_oi=oi, strike_volume=vol, trading_symbol=symbol)


def _settings(**over):
    base = {
        "option_strike_liquidity_ranking_enabled": True,
        "option_strike_liquidity_score_band": 0.05,
        "option_strike_liquidity_min_advantage": 1.25,
    }
    base.update(over)
    return SimpleNamespace(**base)


# ── reading the chain ────────────────────────────────────────────────────────

def test_call_liquidity_comes_from_the_call_columns():
    assert S._strike_liquidity(CHAIN, 24300.0, "CE") == (100_000.0, 50_000.0)


def test_put_liquidity_comes_from_the_put_columns():
    assert S._strike_liquidity(CHAIN, 24250.0, "PE") == (500_000.0, 250_000.0)


def test_strikes_are_matched_on_proximity_not_equality():
    """Strikes arrive as floats from the broker."""
    assert S._strike_liquidity(CHAIN, 24300.2, "CE") == (100_000.0, 50_000.0)


@pytest.mark.parametrize("bad", [None, 99999.0])
def test_an_unknown_strike_reads_as_no_opinion(bad):
    assert S._strike_liquidity(CHAIN, bad, "CE") == (0.0, 0.0)


def test_an_empty_or_missing_chain_reads_as_no_opinion():
    assert S._strike_liquidity(None, 24300.0, "CE") == (0.0, 0.0)
    assert S._strike_liquidity(pd.DataFrame(), 24300.0, "CE") == (0.0, 0.0)


def test_a_chain_without_the_liquidity_columns_reads_as_no_opinion():
    assert S._strike_liquidity(_chain([{"strike": 24300.0}]), 24300.0, "CE") == (0.0, 0.0)


def test_nan_liquidity_reads_as_no_opinion():
    chain = _chain([{"strike": 24300.0, "call_oi": float("nan"),
                     "call_volume": 1.0, "put_oi": 0, "put_volume": 0}])

    assert S._strike_liquidity(chain, 24300.0, "CE") == (0.0, 0.0)


# ── the tie-break ────────────────────────────────────────────────────────────

def test_a_near_tie_goes_to_the_more_liquid_strike():
    ranked = [_cand(0.80, 10_000, 2_000, "thin"), _cand(0.78, 500_000, 250_000, "deep")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "deep"


def test_a_materially_better_setup_still_wins():
    """Outside the band, liquidity has no say at all."""
    ranked = [_cand(0.90, 10_000, 2_000, "thin"), _cand(0.50, 500_000, 250_000, "deep")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "thin"


def test_a_marginal_liquidity_edge_does_not_displace_the_leader():
    """Must clear min_advantage on both, not merely be larger."""
    ranked = [_cand(0.80, 100_000, 50_000, "lead"), _cand(0.79, 110_000, 55_000, "barely")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "lead"


def test_better_open_interest_alone_is_not_enough():
    ranked = [_cand(0.80, 100_000, 50_000, "lead"), _cand(0.79, 500_000, 50_000, "oi_only")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "lead"


def test_better_volume_alone_is_not_enough():
    ranked = [_cand(0.80, 100_000, 50_000, "lead"), _cand(0.79, 100_000, 250_000, "vol_only")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "lead"


def test_the_boundary_of_the_advantage_is_inclusive():
    ranked = [_cand(0.80, 100_000, 50_000, "lead"), _cand(0.79, 125_000, 62_500, "edge")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "edge"


# ── absent data must not move anything ───────────────────────────────────────

def test_a_challenger_with_no_liquidity_data_cannot_displace():
    ranked = [_cand(0.80, 100_000, 50_000, "lead"), _cand(0.79, 0, 0, "unknown")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "lead"


def test_a_leader_with_no_liquidity_data_is_not_displaced_either():
    """No opinion is not a verdict against the incumbent."""
    ranked = [_cand(0.80, 0, 0, "lead"), _cand(0.79, 500_000, 250_000, "deep")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "lead"


# ── switches and degenerate input ────────────────────────────────────────────

def test_the_ranking_can_be_turned_off():
    ranked = [_cand(0.80, 10_000, 2_000, "thin"), _cand(0.78, 500_000, 250_000, "deep")]
    off = _settings(option_strike_liquidity_ranking_enabled=False)

    assert S._prefer_liquid_strike(ranked, off).trading_symbol == "thin"


@pytest.mark.parametrize("band", [0.0, -1.0])
def test_a_zero_band_disables_the_tiebreak(band):
    ranked = [_cand(0.80, 10_000, 2_000, "thin"), _cand(0.78, 500_000, 250_000, "deep")]

    assert S._prefer_liquid_strike(
        ranked, _settings(option_strike_liquidity_score_band=band)
    ).trading_symbol == "thin"


def test_an_advantage_of_one_or_less_is_refused():
    """A ratio <= 1 would let any larger number win, which is not an advantage."""
    ranked = [_cand(0.80, 100_000, 50_000, "lead"), _cand(0.79, 100_001, 50_001, "noise")]

    assert S._prefer_liquid_strike(
        ranked, _settings(option_strike_liquidity_min_advantage=1.0)
    ).trading_symbol == "lead"


def test_a_single_candidate_is_returned_unchanged():
    only = _cand(0.80, 1, 1, "solo")

    assert S._prefer_liquid_strike([only], _settings()) is only


def test_an_empty_ranking_returns_none():
    assert S._prefer_liquid_strike([], _settings()) is None


def test_unscored_candidates_do_not_crash_the_tiebreak():
    ranked = [_cand(0.80, 100_000, 50_000, "lead"), _cand("junk", 500_000, 250_000, "bad")]

    assert S._prefer_liquid_strike(ranked, _settings()).trading_symbol == "lead"


# ── wiring and configurability ───────────────────────────────────────────────

def test_every_knob_is_a_setting():
    """The operator's standing rule: adjustable, never hardcoded."""
    for name in (
        "option_strike_liquidity_ranking_enabled",
        "option_strike_liquidity_score_band",
        "option_strike_liquidity_min_advantage",
    ):
        assert name in Settings.model_fields


def test_the_selection_uses_the_tiebreak_and_reads_only_cached_chain():
    import inspect

    src = inspect.getsource(SignalProcessor.process_best_signal)

    assert "_prefer_liquid_strike(results_list, settings)" in src
    assert "allow_fetch=False" in src, "the ranking must never trigger a broker fetch"
    assert "winner = results_list[0]" not in src
