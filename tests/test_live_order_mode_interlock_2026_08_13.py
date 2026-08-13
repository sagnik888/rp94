"""No broker order may leave a non-REAL TradeManager.

Both broker-order paths guarded only on wiring:

    trade_manager.py:4106  if self.broker and trading_symbol and symbol_token
                              and not is_warmup and not is_ghost:      # ENTRY
    trade_manager.py:4852  if self.broker and trade.symbol_token
                              and not getattr(trade, 'is_ghost', False):  # EXIT

Neither asked what mode it was in, while lines 802, 1412, 3778 and 4692 all do.
The only thing standing between a HISTORICAL-mode manager and a live order was
that the factory does not attach a broker outside REAL — wiring, not a lock. A
broker injected by a test, a hot-swap, or a future wiring change reaches the
order path with nothing mode-aware in the way.

trading_mode is Literal["HISTORICAL", "REAL"], so there is no third mode that
legitimately needs broker orders: the interlock cannot cost a real fill.

Defense in depth. The factory remains the primary control; this is a second lock
on the same door, sited where the order is actually sent.
"""
import inspect

import pytest

from trading.trade_manager import TradeManager


def _mgr(mode):
    mgr = TradeManager.__new__(TradeManager)
    mgr.mode = mode
    return mgr


def test_real_mode_may_send_orders():
    assert _mgr("REAL")._live_orders_permitted() is True


@pytest.mark.parametrize("mode", ["HISTORICAL", "historical", "PAPER", "", None, "BACKTEST"])
def test_no_other_mode_may_send_orders(mode):
    assert _mgr(mode)._live_orders_permitted() is False


def test_a_manager_with_no_mode_attribute_refuses():
    """Absent is not permission — the safe default for an order gate."""
    mgr = TradeManager.__new__(TradeManager)

    assert mgr._live_orders_permitted() is False


def test_both_broker_order_paths_are_gated():
    """Pinned at source: a future order path must carry the interlock too.

    Matched across the whole condition rather than one line, so reformatting the
    guard does not silently retire this check.
    """
    lines = inspect.getsource(TradeManager).splitlines()
    sites = [i for i, ln in enumerate(lines) if "self.broker and" in ln]

    assert len(sites) >= 2, "expected the entry and exit broker-order sites"
    for i in sites:
        window = "\n".join(lines[max(0, i - 2): i + 1])
        assert "_live_orders_permitted()" in window, (
            f"broker-order path not mode-gated: {lines[i].strip()}"
        )
