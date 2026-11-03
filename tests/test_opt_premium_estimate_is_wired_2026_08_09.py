"""Two branches of the option-premium estimate that no caller could reach.

    def _estimate_opt_premium(spot_price, scanner=None, instrument="", trade_date=None)

`trade_date is not None` gates the historical-VIX average; `scanner and instrument and
trade_date is not None` gates the DTE boost. All seven call sites passed exactly two
positional arguments, so:

  * dte_boost was pinned at 1.0 -- an expiry-day option and a 30-day monthly were
    priced identically, and BANKNIFTY/MIDCPNIFTY route to the MONTHLY expiry where the
    1.35 clamp is what the boost exists for;
  * the historical branch never ran, so `elif scanner and hasattr(scanner, "data")`
    caught HISTORICAL runs too and priced a six-month-old backtest against TODAY's
    live VIX. That is lookahead bias in every backtest that touched an option.

On the LIVE site the blast radius is small and the audit's original claim was
overstated: est_premium only feeds a min()-capped stop that
_option_premium_stop_distance then clamps to a band around the REAL verified LTP, and
it never reaches lot sizing (cap_based_lots is None). It is the backtest fidelity and
the monthly-expiry indices that actually move.

`_sig_date` tolerates a signal object, a bare datetime, a bare date, a string, or
nothing at all -- returning None for the unusable cases, which reproduces exactly the
old behaviour rather than raising on a live path.
"""
import ast
import datetime as dt
import inspect
from types import SimpleNamespace

import pytest

from engine.signal_processor import _estimate_opt_premium, _sig_date

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]


class _Expiry:
    def __init__(self, dte):
        self._dte = dte

    def get_dte(self, instrument, trade_date):
        return self._dte


def _scanner(dte=1, vix_data=None, live_vix=15.0):
    return SimpleNamespace(
        expiry=_Expiry(dte),
        _vix_data=vix_data if vix_data is not None else {},
        data=SimpleNamespace(get_latest_price=lambda _s: live_vix),
    )


# ── the DTE boost is reachable ───────────────────────────────────────────────

def test_expiry_day_and_a_monthly_no_longer_price_identically():
    near = _estimate_opt_premium(24700.0, _scanner(dte=1), "NIFTY", dt.date(2026, 8, 9))
    far = _estimate_opt_premium(24700.0, _scanner(dte=30), "BANKNIFTY", dt.date(2026, 8, 9))
    assert near != pytest.approx(far), "dte_boost is still pinned at 1.0"
    assert far > near


def test_the_boost_stays_inside_its_clamp():
    """The UPPER clamp binds at 1.35. The lower one, max(0.75, ...), does not and
    cannot: `max(dte, 1.0)` floors the expression at (1/3)**0.25 = 0.7598, so 0.75 is
    unreachable for any DTE. Asserting 0.75 here would be asserting a fiction."""
    lo = _estimate_opt_premium(24700.0, _scanner(dte=0), "NIFTY", dt.date(2026, 8, 9))
    hi = _estimate_opt_premium(24700.0, _scanner(dte=9999), "NIFTY", dt.date(2026, 8, 9))
    flat = _estimate_opt_premium(24700.0, _scanner(dte=1))
    assert lo == pytest.approx(flat * (1.0 / 3.0) ** 0.25, rel=1e-6)
    assert hi == pytest.approx(flat * 1.35, rel=1e-6)
    assert flat * 0.75 < lo < flat < hi


@pytest.mark.parametrize("index", INDICES)
def test_every_index_reaches_the_boost(index):
    """Four indices are traded separately; the wiring must hold for each."""
    boosted = _estimate_opt_premium(24700.0, _scanner(dte=30), index, dt.date(2026, 8, 9))
    flat = _estimate_opt_premium(24700.0, _scanner(dte=30), index, None)
    assert boosted > flat


def test_omitting_the_date_still_behaves_exactly_as_before():
    """The old two-argument call must be bit-identical, so nothing else shifts."""
    assert (_estimate_opt_premium(24700.0, _scanner(dte=30))
            == _estimate_opt_premium(24700.0, _scanner(dte=30), "NIFTY", None))


# ── the historical VIX branch is reachable ───────────────────────────────────

def test_a_backtest_no_longer_prices_against_todays_vix():
    """Date-only estimates cannot see later March observations or today's VIX.

    An explicit decision time may use its as-of resolver value. DTE is identical
    across calls, isolating the VIX term without restoring future-day averaging.
    """
    sc = _scanner(dte=5, vix_data={"2026-03-04T10:00": 28.0, "2026-03-04T11:00": 32.0},
                  live_vix=12.0)
    observed = []
    def as_of_vix(at):
        observed.append(at)
        return 28.0 if at == dt.datetime(2026, 3, 4, 10, 1) else 0.0
    sc._get_historical_vix = as_of_vix
    march = _estimate_opt_premium(24700.0, sc, "NIFTY", dt.date(2026, 3, 4))
    unrecorded = _estimate_opt_premium(24700.0, sc, "NIFTY", dt.date(2026, 7, 1))
    known = _estimate_opt_premium(24700.0, sc, "NIFTY", dt.datetime(2026, 3, 4, 10, 1))
    assert march == unrecorded
    assert known / unrecorded == pytest.approx(28.0 / 15.0, rel=1e-6)
    assert observed[0] == dt.datetime(2026, 3, 4, 0, 0)


def test_a_dated_call_never_consults_the_live_vix_feed():
    """The whole point: in HISTORICAL mode the live feed must not be reached."""
    touched = []
    sc = SimpleNamespace(
        expiry=_Expiry(5), _vix_data={"2026-03-04T10:00": 28.0},
        data=SimpleNamespace(get_latest_price=lambda s: touched.append(s) or 99.0),
    )
    _estimate_opt_premium(24700.0, sc, "NIFTY", dt.date(2026, 3, 4))
    assert not touched, f"a backtest still read the live VIX feed: {touched}"


def test_a_day_with_no_recorded_vix_falls_back_rather_than_zeroing():
    sc = _scanner(dte=5, vix_data={"2026-03-04T10:00": 28.0}, live_vix=12.0)
    got = _estimate_opt_premium(24700.0, sc, "NIFTY", dt.date(2026, 7, 1))
    assert got > 0


# ── every call site passes the arguments ─────────────────────────────────────

def test_no_call_site_still_omits_the_date():
    src = open("engine/signal_processor.py", encoding="utf-8").read()
    tree = ast.parse(src)
    thin = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_estimate_opt_premium"
            and len(n.args) < 4]
    assert not thin, f"lines {thin} still leave dte_boost pinned at 1.0"


def test_every_call_site_passes_names_that_exist():
    """A NameError here would be strictly worse than the dead branch it replaces."""
    tree = ast.parse(open("engine/signal_processor.py", encoding="utf-8").read())
    unbound = []

    stack = []

    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(node)
            for child in ast.iter_child_nodes(node):
                walk(child)
            stack.pop()
            return
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_estimate_opt_premium" and stack):
            fn = stack[-1]
            bound = {x.id for x in ast.walk(fn)
                     if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
            bound |= {x.arg for x in ast.walk(fn) if isinstance(x, ast.arg)}
            names = {y.id for a in node.args[2:] for y in ast.walk(a)
                     if isinstance(y, ast.Name) and isinstance(y.ctx, ast.Load)}
            missing = names - bound - {"_sig_date", "getattr", "scanner"}
            if missing:
                unbound.append((node.lineno, sorted(missing)))
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    assert not unbound, f"unbound names at {unbound}"


# ── _sig_date never raises and never guesses ─────────────────────────────────

def test_a_bare_datetime_is_not_mistaken_for_a_signal():
    """datetime has a `timestamp` METHOD; reading it as an attribute yields None."""
    assert _sig_date(dt.datetime(2026, 3, 4, 10, 0)) == dt.date(2026, 3, 4)


def test_a_bare_date_passes_through():
    assert _sig_date(dt.date(2026, 3, 4)) == dt.date(2026, 3, 4)


def test_a_signal_object_is_read_from_its_timestamp():
    assert _sig_date(SimpleNamespace(timestamp=dt.datetime(2026, 3, 4))) == dt.date(2026, 3, 4)


def test_a_string_timestamp_is_parsed():
    assert _sig_date(SimpleNamespace(timestamp="2026-03-04 10:00")) == dt.date(2026, 3, 4)


@pytest.mark.parametrize("junk", [
    None, object(), SimpleNamespace(), SimpleNamespace(timestamp=None),
    SimpleNamespace(timestamp="not a date"), SimpleNamespace(timestamp=float("nan")),
])
def test_an_unusable_timestamp_returns_none_rather_than_raising(junk):
    assert _sig_date(junk) is None


def test_a_none_date_reproduces_the_old_pinned_behaviour():
    """None must mean 'as before', so an unparseable timestamp changes nothing."""
    sc = _scanner(dte=30)
    assert (_estimate_opt_premium(24700.0, sc, "NIFTY", _sig_date(SimpleNamespace()))
            == _estimate_opt_premium(24700.0, sc))


def test_a_raising_expiry_does_not_break_pricing():
    class Boom:
        def get_dte(self, *a):
            raise RuntimeError("no expiry")

    sc = SimpleNamespace(expiry=Boom(), _vix_data={},
                         data=SimpleNamespace(get_latest_price=lambda _s: 15.0))
    assert _estimate_opt_premium(24700.0, sc, "NIFTY", dt.date(2026, 8, 9)) > 0
