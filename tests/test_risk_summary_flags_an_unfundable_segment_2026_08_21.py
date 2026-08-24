"""A segment budget too small to pay one round trip must not report a clean bill.

Live .env on 2026-08-21 carries CAPITAL_FUT='1.0' with RISK_FUT_PCT='1.5', so
config/settings.py:2618 computes:

    fut_budget = 1.0 * 1.5 / 100.0 = Rs.0.015

One and a half paise. Every futures entry is refused on budget wherever the
entry-risk gate is consulted, and the same figure is the futures circuit-breaker
threshold. Yet `risk_configuration_summary()` returned:

    ok = True   issues = []   warnings = []

because its only capital test is `<= 0` (config/settings.py:2621) and 1.0 passes,
and `capital_fut + capital_opt > capital_total` is 500001 vs 500002, which passes
too. A validator that certifies a segment nobody can trade is worse than no
validator, because the dashboard reports it as healthy.

This does NOT decide whether CAPITAL_FUT=1.0 is a typo or a deliberate way to ban
futures — that is an operator call. It only insists the condition be *visible*.
It stays a warning rather than an issue for the same reason: `ok` gates startup
surfaces, and a deliberately disabled segment must not break the run.
"""
from config.settings import Settings


def _s(**kw):
    """A FRESH Settings, never the singleton — mutating that one leaks across tests."""
    base = dict(
        capital_total=500002.0, capital_opt=500000.0, capital_fut=1.0,
        risk_opt_pct=7.0, risk_fut_pct=1.5,
        opt_cost=80.0, fut_cost=200.0,
        max_trade_loss_abs=0.0, auto_risk_sizing_enabled=False,
    )
    base.update(kw)
    return Settings(**base)


def _warnings(s):
    return s.risk_configuration_summary()["warnings"]


def test_the_live_futures_budget_is_reported():
    """Rs.0.015 against a Rs.200 futures round trip."""
    warned = _warnings(_s())

    assert any("FUT" in w and "budget" in w.lower() for w in warned), warned


def test_the_warning_names_the_segment_and_the_number():
    warned = " | ".join(_warnings(_s()))

    assert "0.02" in warned or "0.015" in warned or "0.0" in warned
    assert "200" in warned


def test_a_fundable_segment_is_not_warned_about():
    """The options segment funds Rs.35,000 against a Rs.80 round trip."""
    warned = _warnings(_s())

    assert not any("OPT" in w and "budget" in w.lower() for w in warned), warned


def test_both_segments_healthy_stays_silent():
    warned = _warnings(_s(capital_fut=100000.0, capital_total=700000.0))

    assert not any("round trip" in w for w in warned), warned


def test_the_boundary_is_one_round_trip():
    """Exactly one round trip is fundable; a rupee under it is not.

    fut_budget = capital_fut * 1.5 / 100, so Rs.200 needs capital_fut = 13,333.33.
    capital_total is lifted so the segment-vs-total check stays out of the way.
    """
    fundable = _warnings(_s(capital_fut=13334.0, capital_total=700000.0))
    short = _warnings(_s(capital_fut=13000.0, capital_total=700000.0))

    assert not any("FUT" in w and "round trip" in w for w in fundable), fundable
    assert any("FUT" in w and "round trip" in w for w in short), short


def test_an_unfundable_segment_is_a_warning_not_an_issue():
    """`ok` gates startup surfaces; a deliberately disabled segment must not break it."""
    summary = _s().risk_configuration_summary()

    assert summary["ok"] is True
    assert summary["issues"] == []
    assert summary["warnings"]


def test_a_nonpositive_capital_is_still_an_issue():
    """The existing hard check is untouched."""
    summary = _s(capital_fut=0.0).risk_configuration_summary()

    assert summary["ok"] is False
    assert any("positive" in i for i in summary["issues"])
