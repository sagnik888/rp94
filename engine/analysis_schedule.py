"""Pure scheduling policy for expensive scanner entry analysis."""

from __future__ import annotations

import math
import time


def quote_generation(received_at: float, min_interval_seconds: float) -> float | int:
    """Coalesce quote noise while preserving deterministic candle/feature events."""
    stamp = max(0.0, float(received_at or 0.0))
    interval = max(0.0, float(min_interval_seconds or 0.0))
    if interval <= 0.0:
        return round(stamp, 3)
    return int(math.floor(stamp / interval))


def analysis_due(
    *,
    event_driven: bool,
    cycle_is_warmup: bool,
    initial_setup_completed: bool,
    has_pending_signals: bool,
    signature: tuple,
    previous_signature: tuple | None,
    requested_reason: str = "",
    previous_analysis_at: float = 0.0,
    intracandle_min_interval_seconds: float = 0.0,
    now_monotonic: float | None = None,
) -> tuple[bool, str]:
    if not event_driven:
        return True, "event gate disabled"
    if cycle_is_warmup or not initial_setup_completed:
        return True, "startup/warmup"
    if has_pending_signals:
        return True, "stabilization pending"
    if signature != previous_signature:
        quote_only_change = bool(
            previous_signature
            and len(signature) == len(previous_signature)
            and signature[:-1] == previous_signature[:-1]
        )
        if quote_only_change and previous_analysis_at > 0:
            now = time.monotonic() if now_monotonic is None else float(now_monotonic)
            interval = max(0.0, float(intracandle_min_interval_seconds or 0.0))
            if now - float(previous_analysis_at) < interval:
                return False, "intracandle analysis throttled"
        return True, requested_reason or "input changed"
    return False, "inputs unchanged"
