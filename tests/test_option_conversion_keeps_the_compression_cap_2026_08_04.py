"""Converting a candidate to options must not re-derive lots without the compression cap.

engine/position_sizing.resolve_trade_lots describes the compression cap as the FINAL
safety invariant: "a quiet locked session is never enlarged by a caller accidentally
carrying cascade/heavy flags. This intentionally overrides even a user-configured base
size until breakout confirmation."

Scanner._force_candidate_to_options is exactly such a caller — it reads
candidate.one_way_cascade_entry raw — and it re-derived lots from scratch and then
overwrote candidate.lots. It passed no compression_state, so the cap never applied:
3 lots where the sizer had said 1. On a ~Rs44.5k account with a 700 premium and the 15%
option stop that is Rs23,625 at risk (53% of the account) instead of Rs7,875 (18%).

It also collapsed a refusal into a full-size position via `max(1, sized.lots or
base_lots)`: when the sizer returns 0 lots ("one lot risk exceeds budget"), `0 or
base_lots` yields base_lots.
"""
import pytest

from config.settings import get_settings
from engine.position_sizing import resolve_trade_lots

LOT_SIZE = 75
PREMIUM = 700.0
STOP = PREMIUM * 0.85          # the 15% options stop
RISK_PER_LOT = (PREMIUM - STOP) * LOT_SIZE

COMPRESSED = [
    "FORMING", "LOCKED", "BREAKOUT_WATCH", "FAILED_BREAKOUT", "REVERSION_LOCKED",
]


def _size(compression_state, **flags):
    return resolve_trade_lots(
        settings=get_settings(),
        inst_type="OPT",
        requested_lots=1,
        lot_size=LOT_SIZE,
        entry_price=PREMIUM,
        stop_price=STOP,
        compression_state=compression_state,
        **flags,
    )


@pytest.mark.parametrize("state", COMPRESSED)
def test_a_compressed_session_caps_a_cascade_to_one_lot(state):
    assert _size(state, cascade_scale=True).lots == 1


@pytest.mark.parametrize("state", COMPRESSED)
def test_a_compressed_session_caps_a_heavy_trend_scale_to_one_lot(state):
    assert _size(state, heavy_trend_scale=True).lots == 1


def test_the_cap_is_what_makes_the_difference():
    """Guards the probe itself: without a state the cascade really does multiply."""
    assert _size("", cascade_scale=True).lots > 1


def test_the_conversion_path_passes_the_state_through():
    """The bug was a missing kwarg, so pin the call site itself.

    A behavioural test would need a live Scanner with a resolved option chain; the
    invariant that actually broke is narrow and textual: this call site must forward the
    state the candidate was sized under, and must not floor a refusal back to base lots.
    """
    import inspect

    from scanner import Scanner

    src = inspect.getsource(Scanner._force_candidate_to_options)
    call = src[src.index("resolve_trade_lots("):]
    call = call[: call.index("\n            )") + 1]

    assert "compression_state=" in call, (
        "the option-conversion sizer call dropped compression_state, so the cap that "
        "position_sizing calls its final safety invariant never applied here"
    )
    assert "sizing_compression_state" in call, (
        "compression_state must come from the state the candidate was sized under"
    )
    assert "max(1, int(sized.lots or base_lots))" not in src, (
        "a sizer refusal (0 lots) was being converted back into a full-size position"
    )


def test_the_money_at_stake_is_what_the_docstring_claims():
    """Keeps the reported numbers honest if the settings change."""
    uncapped = _size("", cascade_scale=True).lots
    capped = _size("LOCKED", cascade_scale=True).lots
    assert uncapped >= 3 and capped == 1
    assert RISK_PER_LOT == pytest.approx(7875.0)
    assert (uncapped - capped) * RISK_PER_LOT == pytest.approx(15750.0)
