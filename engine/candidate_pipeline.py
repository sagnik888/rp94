"""Pure normalization, sizing and contract-selection phases for signal processing."""

from __future__ import annotations


def normalized_direction(signal_type: object) -> str:
    return "LONG" if str(signal_type or "").strip().upper() == "BUY" else "SHORT"


def select_contract_strikes(
    *,
    inst_type: str,
    signal_type: str,
    spot: float,
    strike_interval: float,
    strike_selection: str,
) -> tuple[str, float, float, list[float]]:
    option_type = "CE" if normalized_direction(signal_type) == "LONG" else "PE"
    interval = float(strike_interval or 0.0)
    atm = round(float(spot or 0.0) / interval) * interval if spot > 0 and interval > 0 else 0.0
    itm = (atm - interval) if option_type == "CE" else (atm + interval)
    if str(inst_type or "").upper() == "FUT":
        strikes = [0.0]
    elif str(strike_selection or "ATM").upper() == "ATM":
        strikes = [atm]
    elif str(strike_selection or "ATM").upper() == "ITM":
        strikes = [itm]
    else:
        strikes = [atm, itm]
    return option_type, atm, itm, strikes


def candidate_size_multipliers(
    *,
    confidence: float,
    grade: str,
    final_rr: float,
    rr_scaling_enabled: bool,
    rr_reference: float,
    rr_floor: float,
) -> tuple[float, float, float]:
    confidence_value = float(confidence or 0.0)
    quality_factor = min(
        1.0,
        confidence_value + (0.1 if str(grade or "") in {"A", "A+"} else 0.0),
    )
    if rr_scaling_enabled:
        reference = max(1e-6, float(rr_reference or 2.5))
        rr_factor = max(float(rr_floor or 0.5), min(1.0, float(final_rr) / reference))
    else:
        rr_factor = 1.0
    return quality_factor, rr_factor, quality_factor * rr_factor

