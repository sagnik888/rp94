"""Confidence → win-probability calibration (isotonic, dependency-free).

R5: the engine's `confidence` is a de-saturated logistic score, but it is NOT
a probability until it is fitted to realized outcomes. This module fits a
monotonic (isotonic, via Pool-Adjacent-Violators) mapping from raw confidence
to empirical win-rate using `data_store/outcome_calibration.csv`.

SAFETY — this stays consistent with the existing ADVISORY_ONLY guardrail in
`intelligence.outcome_calibration`:
  * It refuses to return a calibrator unless the dataset is genuinely ready
    (>= min_samples AND both win and loss classes are present). A degenerate
    set (e.g. all-wins) yields None, so calibration becomes a safe no-op.
  * Live application is gated behind `ut_confidence_calibration_enabled`
    (default False). Nothing changes live until a human turns it on AFTER an
    offline validation, exactly as the validation plan requires.
"""

from __future__ import annotations

import csv
import math
from bisect import bisect_left
from datetime import datetime
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


def _isotonic_pava(x: Sequence[float], y: Sequence[float]) -> Tuple[List[float], List[float]]:
    """Pool-Adjacent-Violators isotonic regression on (x sorted asc, y in {0,1}).

    Returns (thresholds, fitted_values) describing a non-decreasing step curve.
    """
    pairs = sorted(zip(x, y), key=lambda p: p[0])
    # Equal x values are one observation group before enforcing monotonicity.
    groups: List[List[float]] = []
    for xi, yi in pairs:
        if groups and groups[-1][2] == float(xi):
            groups[-1][0] += float(yi)
            groups[-1][1] += 1
        else:
            groups.append([float(yi), 1.0, float(xi)])
    # Each block: [sum_y, count, x_right].
    blocks: List[List[float]] = []
    for group in groups:
        blocks.append(group)
        # Merge while previous mean > current mean (enforce non-decreasing)
        while len(blocks) >= 2 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            s2, c2, _ = blocks.pop()
            blocks[-1][0] += s2
            blocks[-1][1] += c2
            blocks[-1][2] = max(blocks[-1][2], _)
    thresholds = [b[2] for b in blocks]
    values = [b[0] / b[1] for b in blocks]
    return thresholds, values


class ConfidenceCalibrator:
    """Monotonic confidence→probability mapping fitted from realized outcomes."""

    def __init__(self, thresholds: List[float], values: List[float], n: int):
        self._thresholds = thresholds
        self._values = values
        self.n = n

    def transform(self, confidence: float) -> float:
        c = float(confidence or 0.0)
        # Thresholds are block right endpoints, including every pooled point.
        if not self._values:
            return max(0.0, min(1.0, c))
        index = min(bisect_left(self._thresholds, c), len(self._values) - 1)
        out = self._values[index]
        return max(0.0, min(1.0, out))


_CACHE: dict = {}
_CACHE_TTL = 300.0


class CalibratedConfidence(float):
    """Float-compatible result carrying proof that a fitted segment was applied."""
    calibration_applied = True


def _decision_time(value):
    """Normalize recorded decision/outcome clocks to naive exchange time."""
    from zoneinfo import ZoneInfo
    try:
        ts = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return ts.astimezone(ZoneInfo('Asia/Kolkata')).replace(tzinfo=None) if ts.tzinfo else ts
    except (TypeError, ValueError):
        return None


def load_calibrator(
    path: str = "data_store/outcome_calibration.csv",
    *,
    min_samples: int = 200,
    instrument: str = "",
    timeframe: str = "",
    regime: str = "",
    as_of=None,
) -> Optional[ConfidenceCalibrator]:
    """Fit (and cache) a calibrator, or return None if the data is not ready.

    Ready = at least `min_samples` rows AND both win (pnl>0) and loss (pnl<=0)
    classes present. A degenerate dataset returns None (calibration is a no-op).
    """
    now = time.monotonic()
    cutoff = _decision_time(as_of) if as_of is not None else None
    if as_of is not None and cutoff is None:
        return None
    p = Path(path)
    signature = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
    instrument_parts = str(instrument or "").split()
    segment = (
        str(Path(path).resolve()), int(min_samples),
        instrument_parts[0].upper() if instrument_parts else "",
        str(timeframe or "").lower(), str(regime or "").upper(),
        cutoff, signature,
    )
    cached = _CACHE.get(segment)
    if cached and (now - cached["at"]) < _CACHE_TTL:
        return cached["calibrator"]

    confidences: List[float] = []
    outcomes: List[float] = []
    if p.exists() and p.stat().st_size > 0:
        try:
            with p.open("r", newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if cutoff is not None:
                        outcome_time = _decision_time(row.get('exit_time'))
                        if outcome_time is None or outcome_time >= cutoff:
                            continue
                    row_instrument = str(row.get("instrument") or "").split()
                    if segment[2] and (not row_instrument or row_instrument[0].upper() != segment[2]):
                        continue
                    if segment[3] and str(row.get("timeframe") or "").lower() != segment[3]:
                        continue
                    if segment[4] and str(row.get("regime") or "").upper() != segment[4]:
                        continue
                    try:
                        conf = float(row.get("confidence") or "")
                        pnl = float(row.get("pnl") or "")
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(conf) or not 0.0 <= conf <= 1.0 or not math.isfinite(pnl):
                        continue
                    confidences.append(conf)
                    outcomes.append(1.0 if pnl > 0 else 0.0)
        except Exception:
            confidences, outcomes = [], []

    wins = sum(1 for o in outcomes if o > 0)
    losses = len(outcomes) - wins
    ready = len(outcomes) >= int(min_samples) and wins > 0 and losses > 0
    if not ready:
        _CACHE[segment] = {"calibrator": None, "at": now}
        return None

    thresholds, values = _isotonic_pava(confidences, outcomes)
    calibrator = ConfidenceCalibrator(thresholds, values, len(outcomes))
    _CACHE[segment] = {"calibrator": calibrator, "at": now}
    return calibrator


def calibrate(
    confidence: float,
    settings=None,
    path: str = "data_store/outcome_calibration.csv",
    *,
    instrument: str = "",
    timeframe: str = "",
    regime: str = "",
    as_of=None,
) -> float:
    """Return calibrated confidence if enabled AND data is ready, else the input.

    Default-OFF: `ut_confidence_calibration_enabled` must be truthy. Even when
    enabled, returns the raw confidence unchanged until the dataset is ready.
    """
    try:
        collection_enabled = bool(getattr(settings, "ut_confidence_calibration_enabled", False))
        live_apply = bool(
            getattr(settings, "ut_confidence_calibration_live_apply_enabled", False)
        )
        if not (collection_enabled and live_apply):
            return float(confidence or 0.0)
        # Live decisions require an adequately sampled exact index/TF/regime cell.
        # A global curve is not silently substituted for a sparse segment.
        if not instrument or not timeframe or not regime:
            return float(confidence or 0.0)
        global_min = int(getattr(settings, "ut_confidence_calibration_min_samples", 200) or 200)
        if load_calibrator(path, min_samples=global_min, as_of=as_of) is None:
            return float(confidence or 0.0)
        segment_min = int(
            getattr(settings, "ut_confidence_calibration_min_segment_samples", 50) or 50
        )
        cal = load_calibrator(
            path,
            min_samples=segment_min,
            instrument=instrument,
            timeframe=timeframe,
            regime=regime,
            as_of=as_of,
        )
        if cal is None:
            return float(confidence or 0.0)
        return CalibratedConfidence(cal.transform(float(confidence or 0.0)))
    except Exception:
        return float(confidence or 0.0)
