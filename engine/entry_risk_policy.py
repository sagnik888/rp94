"""Prospective entry-risk admission: would the NEXT entry make the book unsafe?

RiskManager.check_circuit_breaker protects a book that is already open — it measures what
is at risk now and halts when that breaches a limit. This module answers the earlier
question: if this candidate were accepted, would the book's total risk AT ITS CONFIGURED
STOPS exceed what the account can absorb?

The distinction matters once several positions run at once. The breaker trips after the
exposure exists; this refuses it beforehand. On a single-position account the two nearly
coincide, which is why this was not needed at three lots — it earns its keep when multiple
lots run across multiple indices and cascade scaling is enabled.

Deliberately pure: no broker, no settings, no I/O. Every entry surface can share one
decision, and a test can exercise a complete multi-position wave without a live session.

Fail-CLOSED throughout. A candidate whose risk cannot be computed, an open position whose
risk cannot be computed, or a missing limit all REFUSE the entry. That is the opposite of
the margin preflight, which fails open — and the asymmetry is deliberate. Margin asks "can
the broker fund this", where a quiet endpoint must not halt trading; this asks "would this
overrun the loss budget", where an unanswerable question means the budget cannot be
proven safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


def _value(item: Any, name: str, default: Any = None) -> Any:
    """Read from a Trade, a TradeCandidate or a plain dict alike."""
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _positive_float(value: Any) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return result if result > 0.0 else 0.0


def normalized_instrument_type(item: Any) -> str:
    value = str(_value(item, "inst_type", "OPT") or "OPT").upper()
    return "FUT" if value == "FUT" else "OPT"


def projected_stop_risk_rs(
    item: Any,
    *,
    entry_price: float = 0.0,
    slippage_bps: float = 0.0,
) -> Optional[float]:
    """Maximum loss from entry to the ACTUAL protective stop, in rupees.

    None means this position cannot be risked safely — a missing or invalid entry or
    stop. A stop already beyond breakeven carries no price risk, but modelled exit
    slippage is still counted.

    Options are treated as long premium whatever the signal direction says, because that
    is what this system trades: a bearish view is a bought PE, never a written CE.
    """
    entry = (
        _positive_float(entry_price)
        or _positive_float(_value(item, "entry_price", 0.0))
        or _positive_float(_value(item, "price", 0.0))
    )
    stop = _positive_float(_value(item, "current_stop", 0.0))
    if stop <= 0.0:
        stop = _positive_float(_value(item, "trailing_stop", 0.0))
    if stop <= 0.0:
        stop = _positive_float(_value(item, "stop", 0.0))
    if stop <= 0.0:
        stop = _positive_float(_value(item, "initial_stop", 0.0))
    if entry <= 0.0 or stop <= 0.0:
        return None

    try:
        lots = max(1, int(_value(item, "lots", 1) or 1))
        lot_size = max(1, int(_value(item, "lot_size", 1) or 1))
    except (TypeError, ValueError):
        return None
    quantity = lots * lot_size
    inst_type = normalized_instrument_type(item)

    if inst_type == "OPT":
        adverse_distance = max(0.0, entry - stop)
        multiplier = 1.0
    else:
        direction = str(_value(item, "direction", "LONG") or "LONG").upper()
        adverse_distance = (
            max(0.0, stop - entry) if direction == "SHORT" else max(0.0, entry - stop)
        )
        multiplier = _positive_float(_value(item, "multiplier", 1.0)) or 1.0

    price_risk = adverse_distance * quantity * multiplier
    slip = (
        entry * quantity * multiplier * max(0.0, float(slippage_bps or 0.0)) / 10_000.0
    )
    return max(0.0, price_risk + slip)


@dataclass(frozen=True)
class EntryRiskDecision:
    allowed: bool
    reason: str
    candidate_risk_rs: float
    current_segment_risk_rs: float
    projected_segment_risk_rs: float
    segment_limit_rs: float
    current_total_risk_rs: float
    projected_total_risk_rs: float
    global_limit_rs: float


def evaluate_entry_risk(
    candidate: Any,
    positions: Iterable[Any],
    *,
    fut_limit_rs: float,
    opt_limit_rs: float,
    global_limit_rs: float,
    max_trade_loss_abs: float = 0.0,
    slippage_bps: float = 0.0,
    entry_price: float = 0.0,
    realized_loss_by_segment: Optional[Mapping[str, float]] = None,
) -> EntryRiskDecision:
    """Fail-closed prospective stop-risk decision for one new entry.

    Realized losses are folded in per segment so the budget shrinks as the day goes
    against you — otherwise the last entry of a losing session is admitted against the
    same headroom as the first.
    """
    candidate_type = normalized_instrument_type(candidate)
    segment_limits = {
        "FUT": max(0.0, float(fut_limit_rs or 0.0)),
        "OPT": max(0.0, float(opt_limit_rs or 0.0)),
    }
    global_limit = max(0.0, float(global_limit_rs or 0.0))
    realized = {
        "FUT": max(0.0, float((realized_loss_by_segment or {}).get("FUT", 0.0) or 0.0)),
        "OPT": max(0.0, float((realized_loss_by_segment or {}).get("OPT", 0.0) or 0.0)),
    }

    risk = projected_stop_risk_rs(
        candidate, entry_price=entry_price, slippage_bps=slippage_bps
    )
    current = {"FUT": realized["FUT"], "OPT": realized["OPT"]}
    invalid_positions = 0
    for position in positions or []:
        position_risk = projected_stop_risk_rs(position, slippage_bps=slippage_bps)
        if position_risk is None:
            invalid_positions += 1
            continue
        current[normalized_instrument_type(position)] += position_risk

    current_segment = current[candidate_type]
    current_total = current["FUT"] + current["OPT"]
    segment_limit = segment_limits[candidate_type]
    candidate_risk = float(risk or 0.0)
    projected_segment = current_segment + candidate_risk
    projected_total = current_total + candidate_risk

    def decision(allowed: bool, reason: str) -> EntryRiskDecision:
        return EntryRiskDecision(
            allowed=allowed,
            reason=reason,
            candidate_risk_rs=candidate_risk,
            current_segment_risk_rs=current_segment,
            projected_segment_risk_rs=projected_segment,
            segment_limit_rs=segment_limit,
            current_total_risk_rs=current_total,
            projected_total_risk_rs=projected_total,
            global_limit_rs=global_limit,
        )

    if risk is None:
        return decision(False, "candidate stop risk unavailable")
    if invalid_positions:
        # One unmeasurable open position makes the whole book's risk unknown, and an
        # unknown book cannot be proven to have room for another entry.
        return decision(
            False, f"{invalid_positions} open position risk value(s) unavailable"
        )
    absolute_cap = max(0.0, float(max_trade_loss_abs or 0.0))
    if absolute_cap > 0.0 and candidate_risk > absolute_cap:
        return decision(
            False,
            f"candidate stop risk Rs.{candidate_risk:.0f} exceeds per-trade cap "
            f"Rs.{absolute_cap:.0f}",
        )
    if segment_limit <= 0.0:
        return decision(False, f"{candidate_type} projected-risk limit unavailable")
    if projected_segment > segment_limit + 1e-9:
        return decision(
            False,
            f"{candidate_type} projected stop risk Rs.{projected_segment:.0f} exceeds "
            f"Rs.{segment_limit:.0f}",
        )
    if global_limit <= 0.0:
        return decision(False, "global projected-risk limit unavailable")
    if projected_total > global_limit + 1e-9:
        return decision(
            False,
            f"global projected stop risk Rs.{projected_total:.0f} exceeds "
            f"Rs.{global_limit:.0f}",
        )
    return decision(True, "projected stop risk within segment/global limits")
