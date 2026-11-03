"""Read-only pre-candle movement classifier.

This module observes the still-forming 5min/15min structure using the already
available 1min feed and market-intelligence snapshot. It deliberately returns a
diagnostic state only; entry and exit code must opt in separately before using
these states for decisions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from engine.intelligence_score import aggregate_intelligence_score, normalize_intelligence_score
from engine.entry_data_quality import evaluate_entry_data_quality


PULLBACK_NOISE = "PULLBACK_NOISE"
EARLY_CONTINUATION = "EARLY_CONTINUATION"
EARLY_REVERSAL_WARNING = "EARLY_REVERSAL_WARNING"
LIKELY_5M_FLIP = "LIKELY_5M_FLIP"
LIKELY_15M_FLIP = "LIKELY_15M_FLIP"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except Exception:
        return default
    if pd.isna(result):
        return default
    return result


def _iso_ts(value: Any) -> str:
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            return value.isoformat()
    except Exception:
        pass
    return str(value or "")


def _direction_from_ohlc(row: Mapping[str, Any]) -> str:
    open_px = _safe_float(row.get("open"))
    close_px = _safe_float(row.get("close"))
    if close_px > open_px:
        return "LONG"
    if close_px < open_px:
        return "SHORT"
    return "FLAT"


def _direction_from_rows(rows: Optional[pd.DataFrame]) -> str:
    if rows is None or rows.empty:
        return "FLAT"
    try:
        open_px = _safe_float(rows["open"].iloc[0])
        close_px = _safe_float(rows["close"].iloc[-1])
    except Exception:
        return "FLAT"
    if close_px > open_px:
        return "LONG"
    if close_px < open_px:
        return "SHORT"
    return "FLAT"


def _close_location(row: Mapping[str, Any], direction: str) -> float:
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    close_px = _safe_float(row.get("close"))
    rng = high - low
    if rng <= 0:
        return 0.5
    if direction == "SHORT":
        return max(0.0, min(1.0, (high - close_px) / rng))
    return max(0.0, min(1.0, (close_px - low) / rng))


def _timeframe_minutes(timeframe: str) -> int:
    return 15 if str(timeframe).lower() == "15min" else 5


def _slice_from(df: Optional[pd.DataFrame], start: Any) -> pd.DataFrame:
    if df is None or df.empty or start is None:
        return pd.DataFrame()
    try:
        idx = df.index
        # Sorted index: a binary search replaces the boolean mask, which on pyarrow-backed
        # string columns cost a full take per call (2% of per-index scan wall time).
        if isinstance(idx, pd.DatetimeIndex) and idx.is_monotonic_increasing:
            try:
                return df.iloc[int(idx.searchsorted(start, side="left")):].copy()
            except (TypeError, ValueError):
                pass
        return df[df.index >= start].copy()
    except Exception:
        return pd.DataFrame()


def _progress_fraction(internal_1m: pd.DataFrame, timeframe: str) -> float:
    minutes = _timeframe_minutes(timeframe)
    count = len(internal_1m) if internal_1m is not None else 0
    if count <= 0:
        return 0.0
    return max(1.0 / minutes, min(1.0, count / float(minutes)))


def _timeframe_metrics(
    candle_tf: Optional[pd.DataFrame],
    direction: str,
    progress: float,
) -> Dict[str, float]:
    if candle_tf is None or candle_tf.empty or len(candle_tf) < 4:
        return {
            "volume_ratio": 0.0,
            "projected_volume_ratio": 0.0,
            "range_atr_ratio": 0.0,
            "close_location": 0.5,
        }
    last = candle_tf.iloc[-1]
    prior = candle_tf.iloc[:-1].tail(20)
    prior_vol = pd.to_numeric(prior.get("volume"), errors="coerce").fillna(0)
    prior_nonzero = prior_vol[prior_vol > 0]
    avg_vol = _safe_float(prior_nonzero.mean() if len(prior_nonzero) else prior_vol.mean())
    current_vol = _safe_float(last.get("volume"))
    raw_volume_ratio = current_vol / avg_vol if avg_vol > 0 else 0.0
    projected_volume_ratio = raw_volume_ratio / max(progress, 0.2) if raw_volume_ratio > 0 else 0.0

    high = _safe_float(last.get("high"))
    low = _safe_float(last.get("low"))
    rng = max(0.0, high - low)
    prior_ranges = (
        pd.to_numeric(prior.get("high"), errors="coerce")
        - pd.to_numeric(prior.get("low"), errors="coerce")
    ).abs()
    atr = _safe_float(prior_ranges.tail(14).mean())
    return {
        "volume_ratio": round(raw_volume_ratio, 4),
        "projected_volume_ratio": round(projected_volume_ratio, 4),
        "range_atr_ratio": round((rng / atr) if atr > 0 else 0.0, 4),
        "close_location": round(_close_location(last, direction), 4),
    }


def _one_min_metrics(
    candle_1min: Optional[pd.DataFrame],
    direction: str,
    volume_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    volume_context = volume_context or {}
    if candle_1min is None or candle_1min.empty or len(candle_1min) < 6:
        tick_count = _safe_float(volume_context.get("tick_count_current_minute"))
        return {
            "volume_ratio": 0.0,
            "range_atr_ratio": 0.0,
            "close_location": 0.5,
            "volume_available": bool(volume_context.get("volume_available")),
            "volume_reliable": bool(volume_context.get("volume_reliable")),
            "volume_unavailable": not bool(volume_context.get("volume_available")),
            "volume_source": str(volume_context.get("volume_source") or volume_context.get("source") or ""),
            "volume_key": str(volume_context.get("volume_key") or ""),
            "tick_count": int(tick_count),
            "completed_cash_volume_fresh": bool(
                volume_context.get("completed_cash_volume_fresh")
            ),
            "futures_volume_reliable": bool(
                volume_context.get("futures_minute_volume_reliable")
            ),
            "futures_volume_ratio": _safe_float(
                volume_context.get("futures_minute_volume_ratio")
            ),
        }
    recent = candle_1min.tail(21)
    last = recent.iloc[-1]
    prior = recent.iloc[:-1]
    prior_vol = pd.to_numeric(prior.get("volume"), errors="coerce").fillna(0)
    prior_nonzero = prior_vol[prior_vol > 0]
    avg_vol = _safe_float(prior_nonzero.mean() if len(prior_nonzero) else prior_vol.mean())
    current_vol = _safe_float(last.get("volume"))
    frame_measured = bool(avg_vol > 0 and current_vol > 0)
    context_intrabar = _safe_float(volume_context.get("intrabar_cash_volume"))
    used_context_intrabar = bool(current_vol <= 0.0 and context_intrabar > 0.0)
    if used_context_intrabar:
        current_vol = context_intrabar
    high = _safe_float(last.get("high"))
    low = _safe_float(last.get("low"))
    rng = max(0.0, high - low)
    prior_ranges = (
        pd.to_numeric(prior.get("high"), errors="coerce")
        - pd.to_numeric(prior.get("low"), errors="coerce")
    ).abs()
    atr = _safe_float(prior_ranges.tail(14).mean())
    tick_count = _safe_float(last.get("tick_count")) if "tick_count" in last.index else 0.0
    if tick_count <= 0:
        tick_count = _safe_float(volume_context.get("tick_count_current_minute"))
    provider_available = bool(volume_context.get("volume_available"))
    volume_available = bool(
        frame_measured
        or (used_context_intrabar and avg_vol > 0 and provider_available)
    )
    volume_reliable = bool(
        frame_measured
        or (
            used_context_intrabar
            and volume_available
            and volume_context.get("volume_reliable")
        )
    )
    volume_ratio = (current_vol / avg_vol) if avg_vol > 0 else 0.0
    if used_context_intrabar and volume_ratio > 0.0:
        progress = max(
            0.2,
            min(
                1.0,
                _safe_float(volume_context.get("intrabar_cash_progress")) or 0.2,
            ),
        )
        volume_ratio /= progress
    return {
        "volume_ratio": round(volume_ratio, 4),
        "range_atr_ratio": round((rng / atr) if atr > 0 else 0.0, 4),
        "close_location": round(_close_location(last, direction), 4),
        "volume_available": volume_available,
        "volume_reliable": volume_reliable,
        "volume_unavailable": not volume_available,
        "volume_source": str(volume_context.get("volume_source") or volume_context.get("source") or ""),
        "volume_key": str(volume_context.get("volume_key") or ""),
        "tick_count": int(tick_count),
        "completed_cash_volume_fresh": bool(
            volume_context.get("completed_cash_volume_fresh")
        ),
        "futures_volume_reliable": bool(
            volume_context.get("futures_minute_volume_reliable")
        ),
        "futures_volume_ratio": _safe_float(
            volume_context.get("futures_minute_volume_ratio")
        ),
    }


def _vote_direction(
    *,
    tf_direction: str,
    one_min_direction: str,
    partial_5min_direction: str,
    state_1min: Mapping[str, Any],
) -> str:
    votes = {"LONG": 0.0, "SHORT": 0.0}
    if tf_direction in votes:
        votes[tf_direction] += 2.0
    if one_min_direction in votes:
        votes[one_min_direction] += 1.5
    if partial_5min_direction in votes:
        votes[partial_5min_direction] += 1.0
    state_label = str(state_1min.get("position_label") or "").upper()
    if state_label in votes:
        votes[state_label] += 0.5
    if abs(votes["LONG"] - votes["SHORT"]) < 1.0:
        return "FLAT"
    return "LONG" if votes["LONG"] > votes["SHORT"] else "SHORT"


def _signal_direction(value: Any) -> str:
    text = str(value or "").upper()
    if "BULL" in text or "BUY" in text or text == "LONG":
        return "LONG"
    if "BEAR" in text or "SELL" in text or text == "SHORT":
        return "SHORT"
    return "FLAT"


def _signed_intel_direction(score: float) -> str:
    if score >= 0.15:
        return "LONG"
    if score <= -0.15:
        return "SHORT"
    return "FLAT"


def _core_agreement(direction: str, core_states: Mapping[str, Any]) -> Dict[str, Any]:
    states: Dict[str, str] = {}
    agree = 0
    total = 0
    for instrument, state in (core_states or {}).items():
        label = str((state or {}).get("position_label") or "").upper()
        if label not in {"LONG", "SHORT"}:
            continue
        states[str(instrument).upper()] = label
        total += 1
        if label == direction:
            agree += 1
    return {"agree": agree, "total": total, "states": states}


def build_pre_candle_prediction(
    *,
    instrument: str,
    timeframe: str,
    candle_tf: Optional[pd.DataFrame],
    candle_1min: Optional[pd.DataFrame],
    candle_5min: Optional[pd.DataFrame] = None,
    mtf_states: Optional[Mapping[str, Mapping[str, Any]]] = None,
    intel_result: Optional[Mapping[str, Any]] = None,
    core_states: Optional[Mapping[str, Any]] = None,
    trade_context: Optional[Mapping[str, Any]] = None,
    option_context: Optional[Mapping[str, Any]] = None,
    volume_context: Optional[Mapping[str, Any]] = None,
    data_quality: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify the current forming 5min/15min candle from lower-TF evidence."""
    tf = str(timeframe or "5min").lower()
    if tf not in {"5min", "15min"}:
        tf = "5min"
    if candle_tf is None or candle_tf.empty:
        return {
            "instrument": str(instrument or "").upper(),
            "timeframe": tf,
            "state": PULLBACK_NOISE,
            "direction": "FLAT",
            "confidence": 0.0,
            "reason": "no timeframe candle data",
            "evidence": {},
        }

    mtf_states = mtf_states or {}
    intel_result = intel_result or {}
    start = candle_tf.index[-1]
    internal_1m = _slice_from(candle_1min, start)
    partial_5m = _slice_from(candle_5min, start) if tf == "15min" else pd.DataFrame()
    tf_direction = _direction_from_ohlc(candle_tf.iloc[-1])
    one_min_direction = _direction_from_rows(internal_1m.tail(3) if not internal_1m.empty else candle_1min.tail(3) if candle_1min is not None and not candle_1min.empty else None)
    partial_5min_direction = _direction_from_rows(partial_5m) if tf == "15min" else "FLAT"
    direction = _vote_direction(
        tf_direction=tf_direction,
        one_min_direction=one_min_direction,
        partial_5min_direction=partial_5min_direction,
        state_1min=mtf_states.get("1min", {}) or {},
    )

    progress = _progress_fraction(internal_1m, tf)
    if direction == "FLAT":
        progress = progress or 0.0

    tf_metrics = _timeframe_metrics(candle_tf, direction if direction != "FLAT" else tf_direction, progress)
    volume_context = volume_context or {}
    one_metrics = _one_min_metrics(
        candle_1min,
        direction if direction != "FLAT" else one_min_direction,
        volume_context,
    )
    state_tf = mtf_states.get(tf, {}) or {}
    adx = _safe_float(state_tf.get("adx_value"))
    intel_score = aggregate_intelligence_score(
        (intel_result.get("aggregate") or {}) if isinstance(intel_result, Mapping) else None
    )
    order_flow_signal = _signal_direction((intel_result.get("order_flow") or {}).get("signal") if isinstance(intel_result, Mapping) else "")
    pcr_signal = _signal_direction((intel_result.get("pcr") or {}).get("signal") if isinstance(intel_result, Mapping) else "")
    oi_signal = _signal_direction((intel_result.get("oi") or {}).get("signal") if isinstance(intel_result, Mapping) else "")
    intel_direction = _signed_intel_direction(intel_score)
    core = _core_agreement(direction, core_states or {})

    score = 0.0
    reasons = []
    if direction != "FLAT":
        score += 0.10
    if one_metrics["volume_available"] and one_metrics["volume_ratio"] >= 2.0:
        score += 0.16
        reasons.append(f"1m volume {one_metrics['volume_ratio']:.1f}x")
    elif one_metrics["volume_available"] and one_metrics["volume_ratio"] >= 1.5:
        score += 0.08
        reasons.append(f"1m volume {one_metrics['volume_ratio']:.1f}x")
    elif one_metrics["volume_unavailable"]:
        cash_note = "live 1m cash volume unavailable"
        if one_metrics["completed_cash_volume_fresh"]:
            cash_note += "; completed cash bars available"
        reasons.append(cash_note)
        futures_ratio = one_metrics["futures_volume_ratio"]
        if one_metrics["futures_volume_reliable"] and futures_ratio >= 2.0:
            score += 0.12
            reasons.append(f"futures 1m volume {futures_ratio:.1f}x own baseline")
        elif one_metrics["futures_volume_reliable"] and futures_ratio >= 1.5:
            score += 0.06
            reasons.append(f"futures 1m volume {futures_ratio:.1f}x own baseline")
        elif one_metrics["tick_count"] >= 8:
            score += 0.08
            reasons.append(f"tick activity {one_metrics['tick_count']}/min")
        elif one_metrics["tick_count"] >= 4:
            score += 0.04
            reasons.append(f"tick activity {one_metrics['tick_count']}/min")
        if one_metrics["range_atr_ratio"] >= 1.2 and one_metrics["close_location"] >= 0.65:
            score += 0.08
            reasons.append("1m price-action burst proxy")
    if one_metrics["range_atr_ratio"] >= 1.2:
        score += 0.12
        reasons.append(f"1m range {one_metrics['range_atr_ratio']:.1f}x")
    if one_metrics["close_location"] >= 0.65:
        score += 0.10
        reasons.append(f"1m close {one_metrics['close_location']:.0%}")
    if tf_metrics["projected_volume_ratio"] >= 1.5:
        score += 0.12
        reasons.append(f"{tf} projected volume {tf_metrics['projected_volume_ratio']:.1f}x")
    if tf_metrics["range_atr_ratio"] >= (0.55 if tf == "15min" else 0.80):
        score += 0.10
        reasons.append(f"{tf} range {tf_metrics['range_atr_ratio']:.1f}x")
    if tf_metrics["close_location"] >= 0.62:
        score += 0.08
        reasons.append(f"{tf} close {tf_metrics['close_location']:.0%}")
    if tf == "15min" and partial_5min_direction == direction and partial_5min_direction != "FLAT":
        score += 0.08
        reasons.append("partial 5m agrees")
    if adx >= 25:
        score += 0.10
        reasons.append(f"ADX {adx:.1f}")
    elif adx >= 18:
        score += 0.05
        reasons.append(f"ADX {adx:.1f}")
    if intel_direction == direction and abs(intel_score) >= 0.70:
        score += 0.14
        reasons.append(f"strong intel {intel_score:.2f}")
    elif intel_direction == direction and abs(intel_score) >= 0.35:
        score += 0.08
        reasons.append(f"intel {intel_score:.2f}")
    for label, value in (("order-flow", order_flow_signal), ("PCR", pcr_signal), ("OI", oi_signal)):
        if value == direction:
            score += 0.04
            reasons.append(f"{label} agrees")
    order_flow_payload = intel_result.get("order_flow") if isinstance(intel_result, Mapping) else {}
    options_flow = (
        order_flow_payload.get("options_flow")
        if isinstance(order_flow_payload, Mapping)
        else {}
    )
    if isinstance(options_flow, Mapping):
        activity_ratio = _safe_float(options_flow.get("ce_pe_activity_ratio"), 1.0)
        if (direction == "LONG" and activity_ratio >= 1.15) or (direction == "SHORT" and activity_ratio <= 0.85):
            score += 0.05
            reasons.append("option activity agrees")
    if core["agree"] >= 3:
        score += 0.12
        reasons.append("core 3/3 agrees")
    elif core["agree"] >= 2:
        score += 0.08
        reasons.append("core 2/3 agrees")

    trade_direction = str((trade_context or {}).get("direction") or "").upper()
    trade_pnl = _safe_float((trade_context or {}).get("pnl"))
    peak_pnl = _safe_float((trade_context or {}).get("peak_pnl"))
    option_accel = _safe_float((option_context or {}).get("acceleration_points_per_min"))
    if trade_direction in {"LONG", "SHORT"} and option_accel:
        favorable = direction == trade_direction and option_accel > 0
        adverse = direction != trade_direction and option_accel < 0
        if favorable or adverse:
            score += 0.05
            reasons.append("option premium acceleration")

    confidence = round(max(0.0, min(1.0, score)), 4)
    if direction == "FLAT" or confidence < 0.35:
        state = PULLBACK_NOISE
    elif trade_direction in {"LONG", "SHORT"}:
        same_direction = direction == trade_direction
        protected_profit_threat = peak_pnl > 0 and trade_pnl < max(0.0, peak_pnl * 0.55)
        if same_direction:
            state = EARLY_CONTINUATION if confidence >= 0.50 else PULLBACK_NOISE
        elif confidence >= 0.62 or protected_profit_threat:
            state = EARLY_REVERSAL_WARNING
        else:
            state = PULLBACK_NOISE
    elif confidence >= 0.62:
        state = LIKELY_15M_FLIP if tf == "15min" else LIKELY_5M_FLIP
    elif confidence >= 0.50:
        state = EARLY_CONTINUATION
    else:
        state = PULLBACK_NOISE

    supplied_quality = data_quality if isinstance(data_quality, Mapping) else {}
    # A pre-candle read remains useful for an operator, but proxy/tick evidence must
    # never be visually indistinguishable from an entry-ready forecast.
    if not supplied_quality:
        supplied_quality = evaluate_entry_data_quality(
            live=True,
            special_entry=True,
            one_minute_volume=volume_context,
            chain_quality=(intel_result.get("data_quality") if isinstance(intel_result, Mapping) else {}),
        )
    # Forming-candle output is intentionally never executable.  With incomplete
    # evidence this communicates the concrete blocker; with healthy evidence it
    # still tells the operator to wait for the completed-candidate gate.
    advisory_only = True
    advisory_reason = str(supplied_quality.get("reason") or "")
    if not bool(supplied_quality.get("allowed", False)):
        reasons.append(f"advisory only: {advisory_reason or 'live entry evidence unavailable'}")
    else:
        reasons.append("advisory only: forming-candle forecast; await final entry gate")

    return {
        "instrument": str(instrument or "").upper(),
        "timeframe": tf,
        "state": state,
        "direction": direction,
        "confidence": confidence,
        "forming_start": _iso_ts(start),
        "progress": round(progress, 4),
        "reason": "; ".join(reasons) if reasons else "no decisive lower-timeframe evidence",
        "entry_eligible": False,
        "advisory_only": advisory_only,
        "data_quality": dict(supplied_quality),
        "evidence": {
            "tf_direction": tf_direction,
            "one_min_direction": one_min_direction,
            "partial_5min_direction": partial_5min_direction,
            "one_min": one_metrics,
            "timeframe": tf_metrics,
            "adx": round(adx, 4),
            "intel_score": round(float(intel_score or 0.0), 4),
            "order_flow_signal": order_flow_signal,
            "pcr_signal": pcr_signal,
            "oi_signal": oi_signal,
            "core_agreement": core,
            "trade_context": dict(trade_context or {}),
            "option_context": dict(option_context or {}),
            "volume_context": dict(volume_context or {}),
        },
        "read_only": True,
    }
