"""Owner-independent reservations for bought option positions and manual signals."""
from __future__ import annotations

import math
from typing import Any


def same_contract(left: Any, right: Any) -> bool:
    """Use the resolved contract, including expiry, rather than timeframe or owner."""
    if any(str(getattr(row, "inst_type", "") or "").upper() != "OPT" for row in (left, right)):
        return False
    symbols = [str(getattr(row, "trading_symbol", "") or "").strip().upper() for row in (left, right)]
    exchanges = [str(getattr(row, "exchange", "") or "").strip().upper() for row in (left, right)]
    if all(exchanges) and exchanges[0] != exchanges[1]:
        return False
    if all(symbols):
        return symbols[0] == symbols[1]
    tokens = [str(getattr(row, "symbol_token", "") or "").strip() for row in (left, right)]
    return bool(all(tokens) and tokens[0] == tokens[1])


def stop_reservation(row: Any, cost: float) -> float:
    """Reserve the actual premium stop plus costs; PE delta is not a rupee multiplier."""
    entry = float(getattr(row, "entry_price", 0.0) or getattr(row, "price", 0.0) or 0.0)
    stop = float(getattr(row, "current_stop", 0.0) or getattr(row, "stop", 0.0)
                 or getattr(row, "trailing_stop", 0.0) or 0.0)
    quantity = max(float(getattr(row, "broker_quantity", 0.0) or 0.0),
                   float(getattr(row, "lots", 0.0) or 0.0) * float(getattr(row, "lot_size", 0.0) or 0.0))
    charges = max(cost, float(getattr(row, "round_trip_cost", 0.0) or 0.0),
                  float(getattr(row, "charges", 0.0) or 0.0))
    if not all(math.isfinite(x) for x in (entry, stop, quantity, charges)) or min(entry, stop, quantity) <= 0:
        raise ValueError("option premium/stop/quantity unavailable")
    return max(0.0, entry - stop) * quantity + charges


def decide(candidate: Any, active: list, *, realized_net: float, loss_budget: float,
           cost: float, forbid_same_contract: bool, max_trade_loss: float = 0.0) -> dict:
    """Inputs are already deduplicated across the broker and signal ledgers."""
    evidence = {"schema": "option_exposure.v1", "active_positions": len(active),
                "realized_net": realized_net, "loss_budget": loss_budget,
                "allowed": False, "reason": ""}
    if forbid_same_contract and any(same_contract(candidate, row) for row in active):
        evidence["reason"] = "same option contract already active across owners/timeframes"
        return evidence
    try:
        if not all(math.isfinite(x) for x in (realized_net, loss_budget, cost, max_trade_loss)):
            raise ValueError("non-finite option loss budget")
        proposed = stop_reservation(candidate, cost)
        reserved = sum(stop_reservation(row, cost) for row in active)
        evidence.update(proposed_stop_risk=proposed, existing_stop_risk=reserved,
                        projected_net=realized_net - reserved - proposed)
        if max_trade_loss > 0 and proposed > max_trade_loss:
            evidence["reason"] = "option entry exceeds configured per-trade loss cap including costs"
        elif loss_budget > 0 and evidence["projected_net"] <= -loss_budget:
            evidence["reason"] = "shared option loss budget exhausted by realized losses and reserved stops"
        else:
            evidence["allowed"] = True
    except (TypeError, ValueError, OverflowError) as exc:
        evidence["reason"] = f"option exposure cannot be verified: {exc}"
    return evidence
