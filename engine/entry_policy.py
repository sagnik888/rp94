"""Fail-closed fresh-entry policy and directional expected-move forecast.

The rest of the strategy is free to propose and mutate candidates.  This module owns
the final, immutable answer to a narrower question: may *these measured execution
levels* create fresh exposure now?  It deliberately does not read ``candidate.rr``;
RR is recomputed from the prices that would actually be submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil, isfinite
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


MODEL_VERSION = "directional_mfe_quantile_v1"
MODEL_SOURCE = "historical_candles.directional_mfe"
GEOMETRY_SCHEMA = "dual_price_geometry.v1"
MIN_EXECUTION_RR = 1.5
# Immutable baseline safety bounds.  Operators may make admission stricter, but a
# runtime/config reload must never lower the expected-move floor or widen the
# approved underlying stop ceiling.
HARD_EXPECTED_MOVE_FLOORS = {
    "NIFTY": 0.15,
    "BANKNIFTY": 0.15,
    "SENSEX": 0.15,
    "MIDCPNIFTY": 0.25,
}
HARD_UNDERLYING_STOP_CAPS = {
    "NIFTY": 0.15,
    "BANKNIFTY": 0.15,
    "SENSEX": 0.15,
    "MIDCPNIFTY": 0.15,
}
GEOMETRY_SOURCE_ROOTS = frozenset(
    {
        "signal_processor.dual_geometry",
        "verified_quote_repair",
        "missed_reentry.futures",
        "missed_reentry.option_delta",
        "concurrency_conversion.futures",
        "concurrency_conversion.option_delta",
        "instrument_conversion.futures",
        "instrument_conversion.option_preserved_underlying",
        "instrument_conversion.option_delta",
    }
)
SUPPORTED_ENTRY_INSTRUMENTS = (
    "NIFTY",
    "BANKNIFTY",
    "SENSEX",
    "MIDCPNIFTY",
)


def effective_expected_move_floor_pct(instrument: str, configured: Any) -> float:
    """Resolve a floor so configuration can only tighten admission."""
    try:
        value = float(configured)
    except (TypeError, ValueError):
        value = 0.0
    if not isfinite(value) or value <= 0:
        return 0.0
    hard = HARD_EXPECTED_MOVE_FLOORS.get(str(instrument or "").split()[0].upper())
    return max(float(hard), value) if hard is not None else value


def effective_underlying_stop_cap_pct(instrument: str, configured: Any) -> float:
    """Resolve a stop ceiling so configuration can only tighten risk."""
    try:
        value = float(configured)
    except (TypeError, ValueError):
        value = 0.0
    if not isfinite(value) or value <= 0:
        return 0.0
    hard = HARD_UNDERLYING_STOP_CAPS.get(str(instrument or "").split()[0].upper())
    return min(float(hard), value) if hard is not None else value


def _finite(value: Any) -> bool:
    try:
        return isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if isfinite(result) else float(default)


def _as_naive_timestamp(value: Any) -> Optional[pd.Timestamp]:
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts


def _iso(value: Any) -> str:
    ts = _as_naive_timestamp(value)
    return ts.isoformat() if ts is not None else ""


@dataclass(frozen=True)
class ExecutionGeometry:
    """Both price spaces for one prospective execution.

    ``premium_*`` is the traded option-contract space and stays zero for futures.
    ``underlying_*`` is always the index/spot space.  Legacy ``spot_*`` aliases may
    mirror the latter at integration boundaries, but are never an input here.
    """

    instrument: str
    direction: str
    inst_type: str
    underlying_entry: float
    underlying_stop: float
    underlying_target: float
    premium_entry: float = 0.0
    premium_stop: float = 0.0
    premium_target: float = 0.0
    contract_entry: float = 0.0
    contract_stop: float = 0.0
    contract_target: float = 0.0
    observed_at: str = ""
    source: str = ""
    verified: bool = False
    schema: str = GEOMETRY_SCHEMA
    # Absolute option delta (or futures conversion ratio 1.0) used to relate the
    # underlying and traded-contract spaces.  It is evidence, not a sizing input.
    conversion_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "direction": self.direction,
            "inst_type": self.inst_type,
            "underlying_entry": self.underlying_entry,
            "underlying_stop": self.underlying_stop,
            "underlying_target": self.underlying_target,
            "premium_entry": self.premium_entry,
            "premium_stop": self.premium_stop,
            "premium_target": self.premium_target,
            "contract_entry": self.contract_entry,
            "contract_stop": self.contract_stop,
            "contract_target": self.contract_target,
            "observed_at": self.observed_at,
            "source": self.source,
            "verified": self.verified,
            "schema": self.schema,
            "conversion_ratio": self.conversion_ratio,
        }


@dataclass(frozen=True)
class ExpectedMoveForecast:
    instrument: str
    direction: str
    timeframe: str
    horizon_minutes: int
    horizon_bars: int
    expected_favorable_move_pct: float
    q10_pct: float
    q25_pct: float
    q50_pct: float
    uncertainty_pct: float
    probability_meeting_floor: float
    floor_pct: float
    sample_count: int
    generated_at: str
    data_as_of: str
    quantile: float = 0.25
    model_version: str = MODEL_VERSION
    source: str = MODEL_SOURCE
    available: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "direction": self.direction,
            "timeframe": self.timeframe,
            "horizon_minutes": self.horizon_minutes,
            "horizon_bars": self.horizon_bars,
            "expected_favorable_move_pct": self.expected_favorable_move_pct,
            "q10_pct": self.q10_pct,
            "q25_pct": self.q25_pct,
            "q50_pct": self.q50_pct,
            "uncertainty_pct": self.uncertainty_pct,
            "probability_meeting_floor": self.probability_meeting_floor,
            "floor_pct": self.floor_pct,
            "sample_count": self.sample_count,
            "generated_at": self.generated_at,
            "data_as_of": self.data_as_of,
            "quantile": self.quantile,
            "model_version": self.model_version,
            "source": self.source,
            "available": self.available,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "ExpectedMoveForecast":
        data = value if isinstance(value, Mapping) else {}
        return cls(
            instrument=str(data.get("instrument") or "").upper(),
            direction=str(data.get("direction") or "").upper(),
            timeframe=str(data.get("timeframe") or ""),
            horizon_minutes=int(data.get("horizon_minutes") or 0),
            horizon_bars=int(data.get("horizon_bars") or 0),
            expected_favorable_move_pct=float(data.get("expected_favorable_move_pct") or 0.0),
            q10_pct=float(data.get("q10_pct") or 0.0),
            q25_pct=float(data.get("q25_pct") or 0.0),
            q50_pct=float(data.get("q50_pct") or 0.0),
            uncertainty_pct=float(data.get("uncertainty_pct") or 0.0),
            probability_meeting_floor=float(data.get("probability_meeting_floor") or 0.0),
            floor_pct=float(data.get("floor_pct") or 0.0),
            sample_count=int(data.get("sample_count") or 0),
            generated_at=str(data.get("generated_at") or ""),
            data_as_of=str(data.get("data_as_of") or ""),
            quantile=float(data.get("quantile") or 0.25),
            model_version=str(data.get("model_version") or ""),
            source=str(data.get("source") or ""),
            available=bool(data.get("available", False)),
            reason=str(data.get("reason") or ""),
        )


@dataclass(frozen=True)
class EntryPolicyVerdict:
    allowed: bool
    phase: str
    checked_at: str
    actual_rr: float
    underlying_stop_pct: float
    expected_move_pct: float
    expected_move_floor_pct: float
    min_rr: float
    max_underlying_stop_pct: float
    reasons: Tuple[str, ...]
    checks: Tuple[Tuple[str, bool], ...]
    lane: str = ""
    policy_version: str = "final_entry_policy.v1"

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "phase": self.phase,
            "checked_at": self.checked_at,
            "actual_rr": self.actual_rr,
            "underlying_stop_pct": self.underlying_stop_pct,
            "expected_move_pct": self.expected_move_pct,
            "expected_move_floor_pct": self.expected_move_floor_pct,
            "min_rr": self.min_rr,
            "max_underlying_stop_pct": self.max_underlying_stop_pct,
            "reasons": list(self.reasons),
            "checks": {key: passed for key, passed in self.checks},
            "lane": self.lane,
            "policy_version": self.policy_version,
        }


def actual_execution_rr(geometry: ExecutionGeometry) -> float:
    """RR in the traded contract's price space, never from metadata."""
    inst_type = str(geometry.inst_type or "").upper()
    direction = str(geometry.direction or "").upper()
    try:
        entry = float(geometry.contract_entry or 0.0)
        stop = float(geometry.contract_stop or 0.0)
        target = float(geometry.contract_target or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not all(_finite(v) for v in (entry, stop, target)) or entry <= 0:
        return 0.0
    if inst_type == "OPT":
        risk, reward = entry - stop, target - entry
    elif direction == "SHORT":
        risk, reward = stop - entry, entry - target
    else:
        risk, reward = entry - stop, target - entry
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def build_directional_mfe_forecast(
    candle_df: Optional[pd.DataFrame],
    *,
    instrument: str,
    direction: str,
    timeframe: str,
    horizon_minutes: int,
    floor_pct: float,
    as_of: Any,
    data_observed_at: Any = None,
    quantile: float = 0.25,
    min_samples: int = 30,
    max_samples: int = 480,
    max_source_age_seconds: float = 90.0,
    require_observation_clock: bool = False,
) -> ExpectedMoveForecast:
    """Forecast directional MFE from fully-observed historical forward windows.

    The label for a LONG is the greatest future high above the starting close; for a
    SHORT it is the greatest future low below it.  The current move is never used as
    the forecast, and the last ``horizon_bars`` rows cannot label themselves because
    their future is not complete.  q25 is intentionally conservative.
    """
    tf = str(timeframe or "5min").lower()
    tf_minutes = {"1min": 1, "5min": 5, "15min": 15}.get(tf, 0)
    try:
        horizon = int(horizon_minutes or 0)
        minimum_samples = int(min_samples or 0)
        maximum_samples = int(max_samples or 0)
        selected_quantile = float(quantile)
    except (TypeError, ValueError, OverflowError):
        horizon = minimum_samples = maximum_samples = 0
        selected_quantile = float("nan")
    horizon_bars = int(ceil(horizon / tf_minutes)) if tf_minutes and horizon > 0 else 0
    base = dict(
        instrument=str(instrument or "").upper(),
        direction=str(direction or "").upper(),
        timeframe=tf,
        horizon_minutes=horizon,
        horizon_bars=horizon_bars,
        expected_favorable_move_pct=0.0,
        q10_pct=0.0,
        q25_pct=0.0,
        q50_pct=0.0,
        uncertainty_pct=0.0,
        probability_meeting_floor=0.0,
        floor_pct=_safe_float(floor_pct),
        sample_count=0,
        generated_at=_iso(as_of),
        data_as_of="",
    )
    forecast_config_ok = bool(
        tf_minutes > 0
        and horizon > 0
        and horizon_bars > 0
        and minimum_samples > 0
        and maximum_samples > 0
        and _finite(selected_quantile)
        and 0.05 <= selected_quantile <= 0.50
        and _finite(max_source_age_seconds)
        and float(max_source_age_seconds) >= 0.0
    )
    if not forecast_config_ok:
        return ExpectedMoveForecast(**base, reason="invalid forecast horizon/configuration")
    if candle_df is None or not isinstance(candle_df, pd.DataFrame) or candle_df.empty:
        return ExpectedMoveForecast(**base, reason="candle history unavailable")
    needed = {"close", "high", "low"}
    if not needed.issubset(set(candle_df.columns)):
        return ExpectedMoveForecast(**base, reason="OHLC source missing close/high/low")

    work = candle_df.copy()
    if not isinstance(work.index, pd.DatetimeIndex):
        if "timestamp" not in work.columns:
            return ExpectedMoveForecast(**base, reason="candle timestamps unavailable")
        work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
        work = work.dropna(subset=["timestamp"]).set_index("timestamp")
    work = work.sort_index()
    if getattr(work.index, "tz", None) is not None:
        work.index = work.index.tz_convert("Asia/Kolkata").tz_localize(None)
    cutoff = _as_naive_timestamp(as_of)
    if cutoff is None:
        return ExpectedMoveForecast(**base, reason="forecast clock unavailable")
    work = work[work.index <= cutoff]
    for column in needed:
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.replace([np.inf, -np.inf], np.nan).dropna(subset=list(needed))
    work = work[(work["close"] > 0) & (work["high"] > 0) & (work["low"] > 0)]
    if not work.empty:
        # Candle timestamps identify bar STARTS, not when the source last updated.
        # Keep those clocks separate: otherwise a 15m bar is automatically called
        # fresh for a full 900 seconds even if its feed died on the opening tick.
        last_start = _as_naive_timestamp(work.index[-1])
        source_observed = _as_naive_timestamp(data_observed_at)
        if source_observed is None and require_observation_clock:
            return ExpectedMoveForecast(
                **base,
                reason="candle source observation clock is required but unavailable",
            )
        if source_observed is None and last_start is not None:
            bar_end = last_start + pd.Timedelta(minutes=tf_minutes)
            # Standalone/historical callers without a transport clock get the most
            # conservative clock justified by the bars themselves. An unfinished bar
            # proves only its start; a completed bar proves its close.
            source_observed = bar_end if cutoff >= bar_end else last_start
        if (
            source_observed is None
            or source_observed > cutoff + pd.Timedelta(seconds=1)
            or (last_start is not None and source_observed < last_start)
        ):
            return ExpectedMoveForecast(
                **base,
                reason="candle source observation clock is unavailable or inconsistent",
            )
        base["data_as_of"] = _iso(source_observed)
        # A fresh quote clock does not prove that the candle frame was refreshed.
        # Compare the source observation with the end of the newest bar so an old
        # candle set cannot masquerade as fresh merely because the index quote is live.
        # An in-progress newest bar is considered current when its start is before the
        # observation clock; completed bars must have closed recently.
        bar_end = last_start + pd.Timedelta(minutes=tf_minutes)
        if source_observed >= bar_end:
            source_gap_seconds = max(0.0, (source_observed - bar_end).total_seconds())
        else:
            source_gap_seconds = 0.0
        if source_gap_seconds > float(max_source_age_seconds) + 1e-9:
            return ExpectedMoveForecast(
                **base,
                reason=(
                    "candle frame is stale: newest completed bar is "
                    f"{source_gap_seconds:.1f}s behind the source clock"
                ),
            )
        if last_start is not None and cutoff < last_start + pd.Timedelta(minutes=tf_minutes):
            work = work.iloc[:-1]
    if len(work) <= horizon_bars:
        return ExpectedMoveForecast(**base, reason="insufficient completed forecast windows")

    closes = work["close"].to_numpy(dtype=float)
    highs = work["high"].to_numpy(dtype=float)
    lows = work["low"].to_numpy(dtype=float)
    start = max(0, len(work) - horizon_bars - maximum_samples)
    samples: list[float] = []
    wants_short = str(direction or "").upper() == "SHORT"
    for idx in range(start, len(work) - horizon_bars):
        entry = closes[idx]
        future = slice(idx + 1, idx + 1 + horizon_bars)
        # Adjacent rows across the close/next open are not a 15/30/60-minute horizon.
        # Exclude windows that cross a session date or contain a data gap; otherwise an
        # overnight gap is mislabeled as intraday MFE and inflates the forecast.
        window_index = work.index[idx : idx + horizon_bars + 1]
        if any(ts.date() != window_index[0].date() for ts in window_index[1:]):
            continue
        gaps = window_index.to_series().diff().dropna().dt.total_seconds()
        if not gaps.empty and bool((gaps > (tf_minutes * 60.0 * 1.5)).any()):
            continue
        move = (
            (entry - float(np.min(lows[future]))) / entry * 100.0
            if wants_short
            else (float(np.max(highs[future])) - entry) / entry * 100.0
        )
        if isfinite(move):
            samples.append(max(0.0, move))
    base["sample_count"] = len(samples)
    if len(samples) < minimum_samples:
        return ExpectedMoveForecast(
            **base,
            reason=f"insufficient samples ({len(samples)}<{minimum_samples})",
        )

    values = np.asarray(samples, dtype=float)
    q10 = float(np.quantile(values, 0.10))
    q25 = float(np.quantile(values, 0.25))
    selected = float(np.quantile(values, selected_quantile))
    q50 = float(np.quantile(values, 0.50))
    q75 = float(np.quantile(values, 0.75))
    return ExpectedMoveForecast(
        **{
            **base,
            "expected_favorable_move_pct": selected,
            "q10_pct": q10,
            "q25_pct": q25,
            "q50_pct": q50,
            "uncertainty_pct": max(0.0, q75 - q25),
            "probability_meeting_floor": float(np.mean(values >= float(floor_pct or 0.0))),
            "quantile": selected_quantile,
        },
        available=True,
        reason="",
    )


def evaluate_final_entry_policy(
    geometry: ExecutionGeometry,
    forecast: ExpectedMoveForecast,
    *,
    min_rr: float,
    expected_move_floor_pct: float,
    max_underlying_stop_pct: float,
    max_forecast_age_seconds: float,
    max_geometry_age_seconds: float,
    checked_at: Any,
    phase: str,
    lane: str = "",
    supported_instruments: Optional[Sequence[str]] = None,
    fresh_exposure: bool = True,
) -> EntryPolicyVerdict:
    """Return the one frozen entry-policy decision for measured execution levels."""
    now = _as_naive_timestamp(checked_at)
    instrument = str(geometry.instrument or "").split()[0].upper()
    configured_min_rr = float(min_rr) if _finite(min_rr) else MIN_EXECUTION_RR
    resolved_min_rr = max(MIN_EXECUTION_RR, configured_min_rr)
    resolved_expected_floor = effective_expected_move_floor_pct(
        instrument, expected_move_floor_pct
    )
    resolved_stop_cap = effective_underlying_stop_cap_pct(
        instrument, max_underlying_stop_pct
    )
    resolved_forecast_age = _safe_float(max_forecast_age_seconds, -1.0)
    resolved_geometry_age = _safe_float(max_geometry_age_seconds, -1.0)
    checks: list[tuple[str, bool]] = []
    reasons: list[str] = []

    if not fresh_exposure:
        return EntryPolicyVerdict(
            allowed=True,
            phase=str(phase or "recovery"),
            checked_at=_iso(checked_at),
            actual_rr=0.0,
            underlying_stop_pct=0.0,
            expected_move_pct=0.0,
            expected_move_floor_pct=resolved_expected_floor,
            min_rr=resolved_min_rr,
            max_underlying_stop_pct=resolved_stop_cap,
            reasons=("existing broker exposure recovery; no fresh entry",),
            checks=(("fresh_exposure", False),),
            lane=str(lane or "recovery"),
        )

    policy_parameters_ok = bool(
        _finite(min_rr)
        and _finite(expected_move_floor_pct)
        and resolved_expected_floor > 0
        and _finite(max_underlying_stop_pct)
        and resolved_stop_cap > 0
        and _finite(max_forecast_age_seconds)
        and resolved_forecast_age >= 0
        and _finite(max_geometry_age_seconds)
        and resolved_geometry_age >= 0
    )
    checks.append(("policy_parameters", policy_parameters_ok))
    if not policy_parameters_ok:
        reasons.append("entry-policy limits are missing, non-finite, or non-positive")

    # A final safety evaluator must be given the approved universe explicitly. An
    # omitted/empty universe is not permission to admit arbitrary symbols; production
    # passes SUPPORTED_ENTRY_INSTRUMENTS and direct callers fail closed.
    supported = {str(item or "").upper() for item in (supported_instruments or ())}
    universe_ok = bool(supported and instrument and instrument in supported)
    checks.append(("supported_instrument", universe_ok))
    if not universe_ok:
        reasons.append(f"{instrument or 'unknown instrument'} is outside the approved entry universe")

    direction = str(geometry.direction or "").upper()
    inst_type = str(geometry.inst_type or "").upper()
    execution_identity_ok = direction in {"LONG", "SHORT"} and inst_type in {"OPT", "FUT"}
    checks.append(("execution_identity", execution_identity_ok))
    if not execution_identity_ok:
        reasons.append("execution direction or instrument type is unsupported")

    geometry_values = (
        geometry.underlying_entry,
        geometry.underlying_stop,
        geometry.underlying_target,
        geometry.contract_entry,
        geometry.contract_stop,
        geometry.contract_target,
    )
    finite_geometry = bool(
        geometry.verified
        and geometry.schema == GEOMETRY_SCHEMA
        and geometry.source
        and all(_finite(v) for v in geometry_values)
    )
    checks.append(("finite_verified_geometry", finite_geometry))
    if not finite_geometry:
        reasons.append("execution geometry is missing, non-finite, unverified, or unsourced")

    geometry_source_root = str(geometry.source or "").split("|", 1)[0]
    geometry_source_ok = geometry_source_root in GEOMETRY_SOURCE_ROOTS
    checks.append(("geometry_source", geometry_source_ok))
    if not geometry_source_ok:
        reasons.append("execution geometry provenance is not an approved internal source")

    geometry_fresh = False
    if finite_geometry and now is not None:
        observed = _as_naive_timestamp(geometry.observed_at)
        if observed is not None:
            geometry_age = (now - observed).total_seconds()
            geometry_fresh = (
                -1.0 <= geometry_age <= resolved_geometry_age
            )
    checks.append(("geometry_fresh", geometry_fresh))
    if not geometry_fresh:
        reasons.append("execution geometry is stale or has no valid source clock")

    ue, us, ut = (
        _safe_float(geometry.underlying_entry),
        _safe_float(geometry.underlying_stop),
        _safe_float(geometry.underlying_target),
    )
    underlying_ordered = bool(
        ue > 0
        and ((direction == "SHORT" and ut < ue < us) or (direction != "SHORT" and us < ue < ut))
    )
    checks.append(("underlying_directional_order", underlying_ordered))
    if not underlying_ordered:
        reasons.append("underlying entry/stop/target are not directionally ordered")

    premium_consistent = True
    cross_geometry_ok = True
    if inst_type == "OPT":
        pe, ps, pt = (
            geometry.premium_entry,
            geometry.premium_stop,
            geometry.premium_target,
        )
        ce, cs, ct = (
            geometry.contract_entry,
            geometry.contract_stop,
            geometry.contract_target,
        )
        premium_consistent = bool(
            all(_finite(v) for v in (pe, ps, pt, ce, cs, ct))
            and float(ps) < float(pe) < float(pt)
            and all(
                abs(float(left) - float(right)) <= 1e-7
                for left, right in (
                    (pe, ce),
                    (ps, cs),
                    (pt, ct),
                )
            )
        )
    checks.append(("premium_contract_consistency", premium_consistent))
    if not premium_consistent:
        reasons.append("premium and submitted contract geometry disagree")
    if inst_type == "OPT":
        # Delta is the explicit bridge between the two price spaces. A bounded
        # tolerance is intentional: delta/gamma and bid/ask movement make an exact
        # equality false, but a missing/absurd bridge must never pass. The check is
        # primarily a unit-mismatch guard (e.g. premium values written into spot_stop).
        try:
            ratio = float(getattr(geometry, "conversion_ratio", 0.0) or 0.0)
            underlying_risk = abs(float(geometry.underlying_entry) - float(geometry.underlying_stop))
            underlying_reward = abs(float(geometry.underlying_target) - float(geometry.underlying_entry))
            premium_risk = abs(float(geometry.premium_entry) - float(geometry.premium_stop))
            premium_reward = abs(float(geometry.premium_target) - float(geometry.premium_entry))
            cross_geometry_ok = bool(
                _finite(ratio)
                and 0.0 < ratio <= 1.5
                and _finite(underlying_risk)
                and _finite(underlying_reward)
                and _finite(premium_risk)
                and _finite(premium_reward)
                and premium_risk <= max(0.05, underlying_risk * ratio * 4.0) + 1e-9
                and premium_reward <= max(0.05, underlying_reward * ratio * 4.0) + 1e-9
            )
        except (TypeError, ValueError, OverflowError):
            cross_geometry_ok = False
        checks.append(("option_cross_geometry", cross_geometry_ok))
        if not cross_geometry_ok:
            reasons.append("option premium/underlying geometry has no valid delta bridge")

    actual_rr = actual_execution_rr(geometry)
    rr_ok = bool(_finite(actual_rr) and actual_rr + 1e-9 >= resolved_min_rr)
    checks.append(("execution_rr", rr_ok))
    if not rr_ok:
        reasons.append(
            f"actual execution RR {actual_rr:.4f} is below {resolved_min_rr:.2f}R"
        )

    stop_pct = abs(ue - us) / ue * 100.0 if ue > 0 and _finite(us) else float("inf")
    stop_ok = bool(
        _finite(stop_pct)
        and stop_pct > 0
        and stop_pct <= resolved_stop_cap + 1e-9
    )
    checks.append(("underlying_stop_cap", stop_ok))
    if not stop_ok:
        reasons.append(
            f"underlying stop {stop_pct:.4f}% exceeds approved "
            f"{resolved_stop_cap:.4f}% ceiling"
        )

    forecast_numbers = (
        forecast.expected_favorable_move_pct,
        forecast.q10_pct,
        forecast.q25_pct,
        forecast.q50_pct,
        forecast.uncertainty_pct,
        forecast.probability_meeting_floor,
        forecast.floor_pct,
        forecast.quantile,
    )
    forecast_core = bool(
        forecast.available
        and forecast.model_version == MODEL_VERSION
        and forecast.source == MODEL_SOURCE
        and forecast.instrument == instrument
        and forecast.direction == direction
        and forecast.sample_count > 0
        and forecast.horizon_minutes > 0
        and all(_finite(value) for value in forecast_numbers)
        and 0.0 <= float(forecast.probability_meeting_floor) <= 1.0
        and 0.05 <= float(forecast.quantile) <= 0.50
        and abs(float(forecast.floor_pct) - resolved_expected_floor) <= 1e-9
        and bool(forecast.generated_at)
        and bool(forecast.data_as_of)
    )
    checks.append(("forecast_evidence", forecast_core))
    if not forecast_core:
        reasons.append(f"expected-move evidence unavailable or incorrectly sourced ({forecast.reason or 'unknown'})")

    forecast_fresh = False
    if forecast_core and now is not None:
        data_ts = _as_naive_timestamp(forecast.data_as_of)
        generated_ts = _as_naive_timestamp(forecast.generated_at)
        if data_ts is not None and generated_ts is not None:
            data_age = (now - data_ts).total_seconds()
            generated_age = (now - generated_ts).total_seconds()
            limit = resolved_forecast_age
            forecast_fresh = (
                -1.0 <= data_age <= limit
                and -1.0 <= generated_age <= limit
            )
    checks.append(("forecast_fresh", forecast_fresh))
    if not forecast_fresh:
        reasons.append("expected-move evidence is stale or has no valid clock")

    expected_pct = _safe_float(forecast.expected_favorable_move_pct)
    expected_ok = bool(
        forecast_core
        and _finite(expected_pct)
        and expected_pct + 1e-9 >= resolved_expected_floor
    )
    checks.append(("expected_move_floor", expected_ok))
    if not expected_ok:
        reasons.append(
            f"directional expected favorable move {expected_pct:.4f}% is below "
            f"{resolved_expected_floor:.4f}% floor"
        )

    return EntryPolicyVerdict(
        allowed=all(passed for _, passed in checks),
        phase=str(phase or "entry"),
        checked_at=_iso(checked_at),
        actual_rr=float(actual_rr if _finite(actual_rr) else 0.0),
        underlying_stop_pct=float(stop_pct if _finite(stop_pct) else 0.0),
        expected_move_pct=expected_pct,
        expected_move_floor_pct=resolved_expected_floor,
        min_rr=resolved_min_rr,
        max_underlying_stop_pct=resolved_stop_cap,
        reasons=tuple(reasons),
        checks=tuple(checks),
        lane=str(lane or ""),
    )


__all__ = [
    "GEOMETRY_SCHEMA",
    "GEOMETRY_SOURCE_ROOTS",
    "MIN_EXECUTION_RR",
    "HARD_EXPECTED_MOVE_FLOORS",
    "HARD_UNDERLYING_STOP_CAPS",
    "MODEL_SOURCE",
    "MODEL_VERSION",
    "SUPPORTED_ENTRY_INSTRUMENTS",
    "EntryPolicyVerdict",
    "ExecutionGeometry",
    "ExpectedMoveForecast",
    "actual_execution_rr",
    "build_directional_mfe_forecast",
    "effective_expected_move_floor_pct",
    "effective_underlying_stop_cap_pct",
    "evaluate_final_entry_policy",
]
