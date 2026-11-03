"""One live-data truth for forecasts and exceptional entries.

This module intentionally does not score direction.  It answers only whether the
data needed to *act* on a fast or A+ setup is present right now.  Forecasts can
still describe the tape when it is not, but must be marked advisory.
"""

from __future__ import annotations

from typing import Any, Mapping


VERSION = "entry_data_quality.v1"

# Completed cash bars from TODAY's session needed before they can stand in for a
# forming-minute reading.  The candle store runs straight through the overnight gap,
# so without this a 09:16 check is satisfied by the previous session's close.
MIN_SESSION_CASH_BARS = 5


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def evaluate_entry_data_quality(
    *,
    live: bool,
    special_entry: bool,
    one_minute_volume: Mapping[str, Any] | None = None,
    chain_quality: Mapping[str, Any] | None = None,
    option_contract: bool = False,
    strike_oi_available: bool = False,
    strike_volume_available: bool = False,
    strike_liquidity_available: bool = False,
    scan_block_reason: str = "",
    accept_completed_cash_bars: bool = False,
    accept_futures_minute: bool = False,
) -> dict[str, Any]:
    """Return the entry/advisory verdict for one current market observation.

    Ordinary live entries retain their existing policy path when data is merely
    thin.  A+ and fast routes are different: they claim exceptional evidence, so
    they must have fresh comparable 1-minute cash volume and, for options, a
    measured tradeable contract.  A scan timeout blocks every new entry because
    a stale pass must never be allowed to make a decision.

    ``accept_completed_cash_bars`` lets fresh COMPLETED Fyers cash bars satisfy the
    volume requirement.  Fyers publishes no cumulative volume for an index on its
    websocket or quote endpoint (verified 2026-09-17: ``volume=0`` for all four), so a
    forming-minute reading exists only when a history poll happens to land inside the
    same minute.  Requiring it blocked graded A+ entries at random -- BANKNIFTY 5min
    SHORT at 11:00 that day, with every completed bar present.  Shock and volume-spike
    routes still need the forming minute, because that minute is their evidence.

    ``accept_futures_minute`` (2026-10-09) lets the near-month Kite futures minute
    series stand in while the Fyers cash bars have not arrived: completed futures
    minutes for completed-bar entries, a reliable forming futures minute for the
    shock/spike routes. Contracts are compared only with contracts.
    """
    volume = _mapping(one_minute_volume)
    chain = _mapping(chain_quality)
    # ``_prediction_volume_context`` publishes the explicit ``volume_*`` keys.
    # Accept the equivalent compact keys too, because signal adapters may already
    # have normalised the same observation to ``available/reliable/comparable``.
    # Absence still means unavailable; this is compatibility, never a fallback.
    volume_available = volume.get("volume_available", volume.get("available", False))
    volume_reliable = volume.get("volume_reliable", volume.get("reliable", False))
    volume_comparable = volume.get("volume_comparable", volume.get("comparable", True))
    intrabar_cash = bool(volume_available and volume_reliable and volume_comparable)
    try:
        session_cash_bars = int(volume.get("completed_cash_session_bars", 0) or 0)
    except (TypeError, ValueError):
        session_cash_bars = 0
    completed_cash = bool(
        accept_completed_cash_bars
        and volume.get("completed_cash_volume_fresh", False) is True
        and session_cash_bars >= MIN_SESSION_CASH_BARS
    )
    futures_minute = _mapping(volume.get("futures_minute_volume"))
    try:
        futures_completed = int(futures_minute.get("completed_samples", 0) or 0)
    except (TypeError, ValueError):
        futures_completed = 0
    futures_volume = bool(
        accept_futures_minute
        and not (intrabar_cash or completed_cash)
        and (
            futures_minute.get("reliable") is True
            if not accept_completed_cash_bars
            else (futures_minute.get("available") is True and futures_completed >= MIN_SESSION_CASH_BARS)
        )
    )
    checks = {
        "live": bool(live),
        "special_entry": bool(special_entry),
        "one_minute_cash_volume": bool(intrabar_cash or completed_cash),
        "one_minute_cash_volume_basis": (
            "intrabar" if intrabar_cash else ("completed_bars" if completed_cash else "")
        ),
        "one_minute_futures_volume": futures_volume,
        "one_minute_volume_basis": (
            "intrabar" if intrabar_cash else "completed_bars" if completed_cash
            else "futures_minute" if futures_volume else ""
        ),
        "completed_cash_session_bars": session_cash_bars,
        "chain_authoritative": bool(
            chain.get("intelligence_usable")
            and not chain.get("stale", False)
            and not chain.get("fallback", False)
        ),
        "strike_oi_available": bool(strike_oi_available),
        "strike_volume_available": bool(strike_volume_available),
        "strike_liquidity_available": bool(strike_liquidity_available),
        "scan_block_reason": str(scan_block_reason or ""),
    }

    if not live:
        return {
            "version": VERSION,
            "allowed": True,
            "advisory_only": False,
            "reason": "historical/replay lane",
            "reason_code": "HISTORICAL",
            "checks": checks,
        }
    if checks["scan_block_reason"]:
        return {
            "version": VERSION,
            "allowed": False,
            "advisory_only": True,
            "reason": checks["scan_block_reason"],
            "reason_code": "STALE_SCAN",
            "checks": checks,
        }
    if not special_entry:
        return {
            "version": VERSION,
            "allowed": True,
            "advisory_only": False,
            "reason": "ordinary entry; exceptional-data requirement not invoked",
            "reason_code": "ORDINARY",
            "checks": checks,
        }
    if not (checks["one_minute_cash_volume"] or checks["one_minute_futures_volume"]):
        return {
            "version": VERSION,
            "allowed": False,
            "advisory_only": True,
            "reason": "fresh comparable 1-minute cash volume is unavailable",
            "reason_code": "ONE_MINUTE_VOLUME_UNAVAILABLE",
            "checks": checks,
        }
    if option_contract and not checks["chain_authoritative"]:
        return {
            "version": VERSION,
            "allowed": False,
            "advisory_only": True,
            "reason": "authoritative current option-chain intelligence is unavailable",
            "reason_code": "OPTION_CHAIN_UNAVAILABLE",
            "checks": checks,
        }
    if option_contract and not (
        checks["strike_oi_available"]
        and checks["strike_volume_available"]
        and checks["strike_liquidity_available"]
    ):
        return {
            "version": VERSION,
            "allowed": False,
            "advisory_only": True,
            "reason": "selected option contract lacks measured OI, volume, or liquidity",
            "reason_code": "OPTION_CONTRACT_EVIDENCE_UNAVAILABLE",
            "checks": checks,
        }
    return {
        "version": VERSION,
        "allowed": True,
        "advisory_only": False,
        "reason": "required live evidence is present",
        "reason_code": "READY",
        "checks": checks,
    }
