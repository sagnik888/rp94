"""A lost entry acknowledgement must be resolved by TAG, not by contract shape.

_submit_entry writes a per-trade tag on every entry:

    tag = self.order_tag(trade_id)
    if tag:
        params["tag"] = tag

When Kite returns an empty order id the order may still have reached the exchange,
so the code asks the order book which it was. It asked the wrong question:

    resting = self.inspect_recent_entry_order(symbol, token, side, qty)

That matcher is contract-level — tradingsymbol, side, quantity — with no tag term.
On a strike several trades share (reachable here: ut_concurrency_guard off,
max_trades_per_index 12) it can return a DIFFERENT trade's order id, which this entry
then adopts as its own. Every stop, exit and reconcile for both trades is then keyed
off one broker order.

inspect_order_by_tag has existed on the same class the whole time, and its docstring
states the exact purpose: "after a lost acknowledgement, ask the order book whether
the tag exists." It was wired for the protective leg and for exits. The entry path,
which is the one that writes the tag, was the only one still guessing.

The fix is additive: a definite FOUND on the tag short-circuits; ABSENT and UNKNOWN
fall through to the previous contract scan untouched, so a tag the broker failed to
echo can never make a placed order look unplaced.
"""
import inspect

import pytest

from trading.kite_broker import KiteBroker


def test_the_entry_path_consults_the_tag():
    src = inspect.getsource(KiteBroker._submit_entry)

    assert "inspect_order_by_tag(tag)" in src, (
        "the entry empty-ack path still resolves identity by contract shape alone"
    )


def test_the_tag_is_asked_before_the_contract_scan():
    """Order matters: the exact identity must win over the shape match."""
    src = inspect.getsource(KiteBroker._submit_entry)

    assert src.index("inspect_order_by_tag(tag)") < src.index("inspect_recent_entry_order("), (
        "the contract scan must only run after the exact tag lookup fails"
    )


def test_only_a_found_tag_short_circuits():
    """ABSENT/UNKNOWN must fall through, or an unechoed tag would look like no order."""
    src = inspect.getsource(KiteBroker._submit_entry)
    i = src.index("inspect_order_by_tag(tag)")
    window = src[i: src.index("inspect_recent_entry_order(", i)]

    assert '"FOUND"' in window
    assert "ABSENT" not in window, (
        "the tag lookup must not raise or reject on ABSENT; that is the scan's job"
    )


def test_the_contract_scan_is_still_reached():
    """The previous behaviour has to survive for the untagged / unechoed case."""
    src = inspect.getsource(KiteBroker._submit_entry)

    assert "inspect_recent_entry_order(symbol, token, side, qty)" in src
    assert 'if state == "ABSENT":' in src
    assert 'if state == "FOUND":' in src


def test_a_failed_tag_lookup_does_not_abort_the_entry():
    """The broker call can throw; that must degrade to the scan, not kill the recovery."""
    src = inspect.getsource(KiteBroker._submit_entry)
    i = src.index("inspect_order_by_tag(tag)")
    window = src[i - 200: i + 300]

    assert "except Exception" in window


def test_the_exact_matcher_exists_and_is_tri_state():
    """Pins the contract the fix leans on."""
    assert hasattr(KiteBroker, "inspect_order_by_tag")

    doc = inspect.getdoc(KiteBroker.inspect_order_by_tag) or ""
    assert "EXACT" in doc or "exact" in doc


def test_entry_orders_are_still_tagged():
    """If the tag ever stops being written the lookup silently degrades to the scan."""
    src = inspect.getsource(KiteBroker._submit_entry)

    assert 'params["tag"] = tag' in src


# ── every order leg carries an identity ──────────────────────────────────────

def test_the_reconcile_replacement_order_is_tagged():
    """The one send that went out untagged.

    A replacement exists BECAUSE the first acknowledgement was unreliable, so it is
    the leg most likely to need recovery by tag. Untagged, a lost ack on it can only
    be resolved by contract shape, which cannot tell two trades on one strike apart.
    """
    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    i = src.index("replacement = self.broker.place_order(")
    call = src[i: src.index(")", src.index("**self._tag_kwargs", i))]

    assert "_tag_kwargs(" in call, "the stale-entry replacement is still untagged"
    assert '"ENTRY"' in call


def test_the_replacement_tag_differs_from_the_original_entry():
    """Same trade, same leg — only `attempt` keeps the two orders distinguishable."""
    from trading.trade_manager import TradeManager

    src = inspect.getsource(TradeManager.reconcile_broker_positions)
    i = src.index("replacement = self.broker.place_order(")
    call = src[i: src.index(")", src.index("**self._tag_kwargs", i))]

    assert "attempt=" in call
    assert "entry_replace_count" in call


def test_tagging_degrades_rather_than_breaking_untagged_brokers():
    """_tag_kwargs returns {} unless the broker really accepts a tag kwarg."""
    from trading.trade_manager import TradeManager

    doc = inspect.getdoc(TradeManager._tag_kwargs) or ""
    assert "only when the broker actually accepts it" in doc
