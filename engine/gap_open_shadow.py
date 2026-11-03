"""Gap-open shadow rule (2026-10-09 user). Records, never admits or refuses.

On a gap that holds, the previous session's swing structure describes a market that
has already been repriced. On 2026-10-09 every opening LONG after a gap-up was refused
as "Price-action ADVERSE [structure=DOWN ...]" while all four indices trended up.

When the signal bar starts within the first `gap_open_window_minutes` of the session,
the instrument gapped at least `gap_open_min_gap_pct` and is holding the gap, and at
least `gap_open_min_aligned` of the indices gapped and hold the same way, this module
recomputes the price-action score without the swing-structure term (+/-0.40) and
records whether the verdict would have changed. Verdicts go to the candidate's price
action context, the log and data_store/gap_open_shadow.jsonl for later evaluation.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd
from loguru import logger

SESSION_OPEN = "09:15"
STRUCTURE_WEIGHT = 0.40
DEFAULT_INDICES = ("NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY")
LOG_PATH = Path("data_store") / "gap_open_shadow.jsonl"


def _g(settings, name: str, default: float) -> float:
    try:
        value = float(getattr(settings, name, default))
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _naive(value) -> Optional[pd.Timestamp]:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if ts is pd.NaT:
        return None
    return ts.tz_convert("Asia/Kolkata").tz_localize(None) if ts.tzinfo is not None else ts


def gap_state(frame: Optional[pd.DataFrame], day, *, max_fill: float = 0.5) -> Optional[Dict[str, Any]]:
    """Gap of `day`'s first bar against the previous session's last close, and whether it holds.

    A gap holds while less than `max_fill` of it has been given back by the latest close.
    """
    if frame is None or frame.empty or not {"open", "close"} <= set(frame.columns):
        return None
    index = pd.DatetimeIndex([_naive(t) for t in frame.index])
    frame = frame.set_axis(index).sort_index()
    day = pd.Timestamp(day).normalize()
    session = frame.loc[(frame.index >= day + pd.Timedelta(hours=9, minutes=15)) & (frame.index < day + pd.Timedelta(days=1))]
    before = frame.loc[frame.index < day]
    if session.empty or before.empty:
        return None
    prev_close = float(before["close"].iloc[-1])
    first_open = float(session["open"].iloc[0])
    last = float(session["close"].iloc[-1])
    if prev_close <= 0:
        return None
    gap = first_open - prev_close
    gap_pct = gap / prev_close * 100.0
    retained = (last - prev_close) / gap if gap else 0.0
    return {"prev_close": prev_close, "open": first_open, "last": last,
            "gap_pct": round(gap_pct, 4), "direction": (gap > 0) - (gap < 0),
            "retained": round(retained, 4), "holding": bool(gap and retained >= 1.0 - max_fill)}


def evaluate(instrument: str, direction: str, signal_time, frames: Dict[str, pd.DataFrame], settings,
             pa_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Shadow verdict for one candidate; `active` is False outside the rule's conditions."""
    ts = _naive(signal_time)
    result: Dict[str, Any] = {"active": False, "would_change": False, "reason": ""}
    if ts is None:
        result["reason"] = "no signal time"
        return result
    window = _g(settings, "gap_open_window_minutes", 5.0)
    min_gap = _g(settings, "gap_open_min_gap_pct", 0.8)
    min_aligned = int(_g(settings, "gap_open_min_aligned", 3))
    day = ts.normalize()
    opened = day + pd.Timedelta(hours=9, minutes=15)
    result.update(window_minutes=window, min_gap_pct=min_gap, min_aligned=min_aligned,
                  signal_time=ts.isoformat())
    if not (opened <= ts < opened + pd.Timedelta(minutes=window)):
        result["reason"] = "signal bar outside the opening window"
        return result
    states = {name: gap_state(frame, day) for name, frame in (frames or {}).items()}
    own = states.get(str(instrument).upper())
    if not own:
        result["reason"] = "no gap state for the instrument"
        return result
    qualifies = lambda st: bool(st and abs(st["gap_pct"]) >= min_gap and st["holding"])
    aligned = sorted(name for name, st in states.items()
                     if qualifies(st) and st["direction"] == own["direction"])
    side = 1 if str(direction).upper() in {"LONG", "BUY"} else -1
    result.update(gap_pct=own["gap_pct"], gap_holding=own["holding"], gap_direction=own["direction"],
                  aligned_indices=aligned, with_gap=side == own["direction"],
                  states={k: v for k, v in states.items() if v})
    if not qualifies(own) or len(aligned) < min_aligned:
        result["reason"] = "gap too small, not holding, or too few indices aligned"
        return result
    result["active"] = True
    if pa_ctx:
        structure = str(pa_ctx.get("structure") or "RANGE").upper()
        term = 0.0
        if structure in {"UP", "DOWN"}:
            term = STRUCTURE_WEIGHT if (structure == "UP") == (side > 0) else -STRUCTURE_WEIGHT
        score = float(pa_ctx.get("score") or 0.0)
        adverse_at = float(pa_ctx.get("adverse_threshold", -0.5))
        shadow_score = round(score - term, 3)
        shadow_verdict = ("ADVERSE" if shadow_score <= adverse_at
                          else "FAVORABLE" if shadow_score >= 0.40 else "NEUTRAL")
        result.update(structure=structure, structure_term=term, price_action_score=score,
                      price_action_verdict=pa_ctx.get("verdict"), shadow_score=shadow_score,
                      shadow_verdict=shadow_verdict,
                      would_change=bool(result["with_gap"] and pa_ctx.get("verdict") == "ADVERSE"
                                        and shadow_verdict != "ADVERSE"))
    return result


def record(scanner, instrument: str, timeframe: str, signal, settings, pa_ctx: Optional[Dict[str, Any]],
           *, indices: Optional[Iterable[str]] = None) -> Optional[Dict[str, Any]]:
    """Evaluate, stamp the price-action context, log and archive. Never raises."""
    try:
        if not bool(getattr(settings, "gap_open_shadow_enabled", True)):
            return None
        names = list(indices or getattr(scanner, "active_indices", None) or DEFAULT_INDICES)
        candles = getattr(scanner, "candles", None)
        frames = {}
        for name in names:
            try:
                frames[str(name).upper()] = candles.get_candles(name, "1min")
            except Exception:
                continue
        direction = str(getattr(signal, "direction", "") or getattr(signal, "signal_type", "") or "")
        signal_time = getattr(signal, "timestamp", None) or getattr(signal, "signal_timestamp", None)
        verdict = evaluate(instrument, direction, signal_time, frames, settings, pa_ctx)
        if not verdict.get("active"):
            return verdict
        if isinstance(pa_ctx, dict):
            pa_ctx["gap_open_shadow"] = verdict
        logger.info(
            f"[GAP-OPEN SHADOW] {instrument} {timeframe} {direction}: gap {verdict['gap_pct']:+.2f}% holding, "
            f"aligned {len(verdict['aligned_indices'])}; price action {verdict.get('price_action_verdict')} "
            f"{verdict.get('price_action_score')} -> {verdict.get('shadow_verdict')} {verdict.get('shadow_score')} "
            f"without prior-session structure (would_change={verdict.get('would_change')})"
        )
        row = {"instrument": instrument, "timeframe": timeframe, "direction": direction,
               "mode": str(getattr(scanner, "mode", "") or ""), **verdict}
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, default=str) + "\n")
        except Exception as exc:
            logger.debug(f"Gap-open shadow archive failed: {exc}")
        return verdict
    except Exception as exc:
        logger.debug(f"Gap-open shadow failed: {exc}")
        return None
