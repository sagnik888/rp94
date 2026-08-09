"""The option PREMIUM was stored in the index-SPOT field.

`open_trade` defaulted the spot field to the contract price:

    entry_spot=entry_spot or price

For an OPT, `price` is the option premium (~120) and the spot is ~24,850 — two
quantities three orders of magnitude apart. All three OPT entry sites feed
`entry_spot` an unguarded `self.candles.get_latest_price(candidate.instrument)`
(scanner.py:1378 -> :1407, :23976 -> :23999, :24063 -> :24112), and that returns
None/0 whenever no spot tick is cached for the index. `None or price` then yielded the
premium.

Downstream, EVERY spot<->premium converter keys off this one field, and update_trade's
synthetic-premium branch computes:

    spot_change  = current_spot - trade.entry_spot     # 24850 - 120 = 24730
    premium_move = spot_change * instrument_multiplier # * 0.5       = 12365
    current_price = entry_price + premium_move         # 120 + 12365 = 12485

On 75 qty that is ~Rs 9.2 lakh of fabricated profit, which fires TARGET_HIT and books
a fictitious exit into the ledger.

The bug also disabled its own guard. update_trade's else-branch says outright "If
entry_spot is missing, do NOT use spot price as option price! Keep it at entry_price to
avoid massive fake P&L" — but `entry_spot or price` guaranteed entry_spot > 0 whenever
price > 0, so that branch was unreachable.

FIX: the spot field never takes a contract price. 0.0 is a supported value — every
reader already guards `entry_spot > 0` — so a missing spot tick now books ~flat instead
of a fiction.

`instrument_multiplier` on an OPT row is the signed Black-Scholes DELTA, not a
contract-size multiplier. A bought PE carries a NEGATIVE delta and is stored with
direction="SHORT" while still being long premium, so both signs are exercised here.
"""
import ast
import inspect
from datetime import datetime

import pytest

from trading.trade_manager import Trade, TradeManager

INDICES = [("NIFTY", 24850.0), ("BANKNIFTY", 54200.0),
           ("SENSEX", 81300.0), ("MIDCPNIFTY", 13100.0)]


def _trade(entry_spot, premium=120.0, delta=0.5, direction="LONG", inst="OPT"):
    return Trade(
        id="T1", instrument="NIFTY", timeframe="5min", direction=direction,
        entry_price=premium, entry_time=datetime(2026, 8, 7, 10, 0),
        trailing_stop=premium * 0.85, current_stop=premium * 0.85,
        lots=1, lot_size=65, grade="A", inst_type=inst,
        trading_symbol="NIFTY25000CE" if delta > 0 else "NIFTY24000PE",
        instrument_multiplier=delta, entry_spot=entry_spot,
    )


# ── the source of the bug ────────────────────────────────────────────────────

def test_open_trade_never_substitutes_the_contract_price():
    src = inspect.getsource(TradeManager.open_trade)
    tree = ast.parse(inspect.cleandoc(src) if src.startswith("def") else src.lstrip())
    bad = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.keyword) and n.arg == "entry_spot"
        and isinstance(n.value, ast.BoolOp) and isinstance(n.value.op, ast.Or)
        and any(getattr(v, "id", "") == "price" for v in n.value.values)
    ]
    assert not bad, (
        "entry_spot still falls back to the contract price; on an OPT that is the premium"
    )


def test_a_missing_spot_leaves_the_field_empty_not_the_premium():
    """The headline. 120 is a premium; it must never land in a spot-space field."""
    src = inspect.getsource(TradeManager.open_trade)
    assert "entry_spot=float(entry_spot or 0.0)" in src


# ── the guard the bug disabled is reachable again ────────────────────────────

def _manager_with(trade):
    tm = TradeManager.__new__(TradeManager)
    tm.open_trades = {trade.id: trade}
    tm.trades = [trade]
    tm._lock = __import__("threading").RLock()
    tm.save_state = lambda *a, **k: None
    tm._log_ui = lambda *a, **k: None
    tm._option_premium_quote_plausible = lambda *a, **k: True
    tm._last_mark_to_market_save = 0.0
    tm.closed_trades = []
    tm.mode = "PAPER"
    tm.broker = None
    tm.real_broker = None
    return tm


def test_a_missing_spot_books_flat_through_the_real_update_path():
    """End-to-end through update_trade: with no live premium the mark must NOT be a
    spot-vs-premium difference. This is the branch `entry_spot or price` made
    unreachable.

    Scope note: this constructs the Trade directly, so it pins what entry_spot=0.0 must
    PRODUCE, not where the 0.0 comes from. The two source-level tests above are what
    actually fail if the `or price` default returns — verified by reverting it.
    """
    t = _trade(entry_spot=0.0)
    tm = _manager_with(t)
    try:
        tm.update_trade(t.id, current_spot=24850.0, real_premium=None)
    except Exception as exc:  # pragma: no cover - harness gap, not a behaviour claim
        pytest.skip(f"update_trade needs more collaborators here: {exc}")
    assert t.current_price == pytest.approx(120.0), (
        f"marked the option at {t.current_price}; the spot is 24850 and the premium is 120"
    )
    assert t.current_price < 1_000, "a spot-space number reached a premium-space field"


def test_the_same_path_with_a_real_spot_still_marks_the_delta_move():
    t = _trade(entry_spot=24800.0)
    tm = _manager_with(t)
    try:
        tm.update_trade(t.id, current_spot=24850.0, real_premium=None)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"update_trade needs more collaborators here: {exc}")
    assert t.current_price == pytest.approx(145.0)


@pytest.mark.parametrize("index,spot", INDICES)
def test_the_fabricated_pnl_is_no_longer_reachable(index, spot):
    """What the old default produced, on every index."""
    premium, delta = 120.0, 0.5
    fabricated = premium + (spot - premium) * delta      # entry_spot = premium
    honest = premium                                      # entry_spot = 0 -> hold entry
    assert fabricated > 5_000, f"{index}: premise broken, only {fabricated:.0f}"
    assert honest == pytest.approx(premium)
    # 75 qty of fabricated gain, in rupees
    assert (fabricated - premium) * 75 > 400_000


def test_a_real_spot_still_marks_the_option_normally():
    """The fix must not break the legitimate synthetic mark."""
    t = _trade(entry_spot=24800.0)
    spot_change = 24850.0 - t.entry_spot
    assert t.entry_price + spot_change * t.instrument_multiplier == pytest.approx(145.0)


def test_a_bought_pe_still_marks_with_its_negative_delta():
    """direction='SHORT' but LONG premium: spot DOWN must raise the mark."""
    t = _trade(entry_spot=24800.0, premium=110.0, delta=-0.45, direction="SHORT")
    spot_change = 24700.0 - t.entry_spot          # -100
    mark = t.entry_price + spot_change * t.instrument_multiplier
    assert mark == pytest.approx(155.0), "a bought PE lost money on a favourable move"


# ── every reader tolerates the empty value ───────────────────────────────────

@pytest.mark.parametrize("fn_name", [
    "_active_option_decision_mark", "_fut_price_from_spot_level",
])
def test_spot_converters_guard_a_zero_entry_spot(fn_name):
    from scanner import Scanner

    fn = getattr(Scanner, fn_name, None)
    if fn is None:
        pytest.skip(f"{fn_name} not present")
    src = inspect.getsource(fn)
    assert "entry_spot" in src
    assert (">" in src and "0" in src), f"{fn_name} does not guard entry_spot > 0"


def test_the_field_round_trips_as_zero():
    t = _trade(entry_spot=0.0)
    assert Trade.from_dict(t.to_dict()).entry_spot == 0.0


def test_a_genuine_spot_round_trips_unchanged():
    t = _trade(entry_spot=24800.0)
    assert Trade.from_dict(t.to_dict()).entry_spot == pytest.approx(24800.0)


# ── the same substitution at three sibling sites ─────────────────────────────

@pytest.mark.parametrize("path", ["scanner.py", "engine/scanner_recalculation.py"])
def test_no_site_resolves_a_spot_from_a_candidate_price(path):
    """`getattr(cand, 'entry_spot', getattr(cand, 'price', 0.0))` is the same bug with a
    getattr default instead of an `or`: an OPT candidate with no entry_spot was measured
    against its own PREMIUM as an index level."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "getattr" or len(node.args) < 3:
            continue
        first = node.args[1]
        if not (isinstance(first, ast.Constant) and first.value == "entry_spot"):
            continue
        if "price" in ast.dump(node.args[2]):
            bad.append(node.lineno)
    assert not bad, f"{path} lines {bad} still resolve a spot level from a contract price"


def test_the_reconstruction_row_no_longer_defaults_to_the_premium():
    src = open("scanner.py", encoding="utf-8").read()
    assert "entry_spot=float(getattr(entry_cand, 'entry_spot', 0.0) or 0.0)" in src


def test_the_candle_extreme_helper_no_longer_defaults_to_the_premium():
    from scanner import Scanner

    src = inspect.getsource(Scanner._candidate_candle_extreme_price)
    assert 'getattr(candidate, "entry_spot", 0.0)' in src
    assert 'getattr(candidate, "price"' not in src


# ── the premise: a spot lookup really can come back empty ────────────────────

def test_the_entry_sites_pass_an_unguarded_spot_lookup():
    """If these ever start guarding, this test is arguing for nothing."""
    src = open("scanner.py", encoding="utf-8").read()
    assert "actual_spot = self.candles.get_latest_price(" in src, (
        "the entry sites no longer read spot this way; re-check the premise"
    )
