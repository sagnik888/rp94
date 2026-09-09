"""Advisory outcome-calibration dataset builder.

This stores compact rows for later analysis only. It must not change live
trading decisions until a separate validation step proves an edge.
"""

from __future__ import annotations

import csv
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List


CALIBRATION_FIELDS = [
    "trade_id",
    "instrument",
    "timeframe",
    "inst_type",
    "direction",
    "grade",
    "confidence",
    "entry_time",
    "exit_time",
    "pnl",
    "mfe",
    "mae",
    "exit_reason",
    "regime",
    "aggregate_score",
    "volume_signal",
    "volume_ratio",
    "pcr",
    "oi_signal",
    "ofr",
    "iv_percentile",
    "day_type",
    "session_bias",
    "hourly_anchor",
    "breadth_bias",
    "top_down_adjustment",
    "session_delta_ratio",
    "vix_change_pct",
]


_OUTCOME_FILE_LOCK = threading.RLock()


class OutcomeCalibrationStore:
    """Append-only compact CSV store for post-trade research."""

    def __init__(self, path: str = "data_store/outcome_calibration.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validation_cache: Dict[str, Any] = {}
        self._validation_cache_at = 0.0

    def build_row(self, trade: Any, intel: Dict[str, Any] | None = None) -> Dict[str, Any]:
        intel = intel or {}
        pcr = intel.get("pcr", {}) if isinstance(intel.get("pcr"), dict) else {}
        oi = intel.get("oi", {}) if isinstance(intel.get("oi"), dict) else {}
        flow = intel.get("order_flow", {}) if isinstance(intel.get("order_flow"), dict) else {}
        greeks = intel.get("greeks", {}) if isinstance(intel.get("greeks"), dict) else {}
        regime = intel.get("regime", {}) if isinstance(intel.get("regime"), dict) else {}
        volume = intel.get("volume", {}) if isinstance(intel.get("volume"), dict) else {}
        aggregate = intel.get("aggregate", {}) if isinstance(intel.get("aggregate"), dict) else {}
        session_context = intel.get("session_context", {}) if isinstance(intel.get("session_context"), dict) else {}
        session_delta = (
            session_context.get("session_delta", {})
            if isinstance(session_context.get("session_delta"), dict)
            else {}
        )
        anchor = intel.get("anchor_regime", {}) if isinstance(intel.get("anchor_regime"), dict) else {}
        breadth = intel.get("breadth_context", {}) if isinstance(intel.get("breadth_context"), dict) else {}
        # Accepted-trade snapshots are intentionally compact/flat; full intelligence
        # snapshots are nested. Support both without inventing absent evidence.
        flat = intel
        regime_value = regime.get("regime", "") or flat.get("regime_label", "") or getattr(trade, "market_regime", "")
        aggregate_value = aggregate.get("score", "")
        if aggregate_value == "":
            aggregate_value = flat.get("aggregate_score", "")
        volume_ratio_value = volume.get("volume_ratio", "")
        if volume_ratio_value == "":
            volume_ratio_value = flat.get("volume_ratio", "")
        pcr_value = pcr.get("primary_pcr", pcr.get("pcr_oi", pcr.get("pcr", "")))
        if pcr_value == "":
            pcr_value = flat.get("pcr", "")
        oi_value = oi.get("signal", "") or flat.get("oi_signal", "")
        ofr_value = flow.get("ratio", flow.get("buy_sell_ratio", ""))
        if ofr_value == "":
            ofr_value = flat.get("order_ratio", "")
        iv_value = greeks.get("iv_percentile", "")
        if iv_value == "":
            iv_value = flat.get("iv_percentile", "")
        return {
            "trade_id": getattr(trade, "id", ""),
            "instrument": getattr(trade, "instrument", ""),
            "timeframe": getattr(trade, "timeframe", ""),
            "inst_type": getattr(trade, "inst_type", ""),
            "direction": getattr(trade, "direction", ""),
            "grade": getattr(trade, "grade", ""),
            "confidence": float(getattr(trade, "confidence", 0.0) or 0.0),
            "entry_time": getattr(getattr(trade, "entry_time", None), "isoformat", lambda: "")(),
            "exit_time": getattr(getattr(trade, "exit_time", None), "isoformat", lambda: "")(),
            "pnl": float(getattr(trade, "pnl", 0.0) or 0.0),
            "mfe": float(getattr(trade, "peak_pnl", 0.0) or 0.0),
            "mae": float(getattr(trade, "max_drawdown", 0.0) or 0.0),
            "exit_reason": getattr(trade, "exit_reason", ""),
            "regime": regime_value,
            "aggregate_score": aggregate_value,
            "volume_signal": volume.get("signal", ""),
            "volume_ratio": volume_ratio_value,
            "pcr": pcr_value,
            "oi_signal": oi_value,
            "ofr": ofr_value,
            "iv_percentile": iv_value,
            "day_type": session_context.get("day_type", ""),
            "session_bias": session_context.get("bias", ""),
            "hourly_anchor": anchor.get("regime", ""),
            "breadth_bias": breadth.get("bias", ""),
            "top_down_adjustment": aggregate.get("top_down_adjustment", ""),
            "session_delta_ratio": session_delta.get("delta_ratio", ""),
            "vix_change_pct": session_context.get("vix_change_pct", ""),
        }

    def append_rows(self, rows: Iterable[Dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with _OUTCOME_FILE_LOCK:
            write_header = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CALIBRATION_FIELDS, extrasaction="ignore")
                if write_header:
                    writer.writeheader()
                for row in rows:
                    writer.writerow(row)
        return len(rows)

    def compact_from_trades(self, trades: Iterable[Any], intel_by_instrument: Dict[str, Any] | None = None) -> int:
        intel_by_instrument = intel_by_instrument or {}
        rows: List[Dict[str, Any]] = []
        for trade in trades:
            rows.append(self.build_row(trade, intel_by_instrument.get(getattr(trade, "instrument", ""))))
        return self.append_rows(rows)

    def validation_status(
        self,
        min_samples: int = 200,
        min_samples_per_instrument: int = 30,
    ) -> Dict[str, Any]:
        """Describe readiness for an offline validation study; never tune live logic."""
        if time.monotonic() - self._validation_cache_at < 30.0 and self._validation_cache:
            return dict(self._validation_cache)
        rows: List[Dict[str, Any]] = []
        with _OUTCOME_FILE_LOCK:
            if self.path.exists() and self.path.stat().st_size > 0:
                try:
                    with self.path.open("r", newline="", encoding="utf-8") as handle:
                        rows = list(csv.DictReader(handle))
                except (OSError, csv.Error):
                    rows = []
        unique_rows = {}
        for row in rows:
            key = str(row.get("trade_id") or "").strip()
            if key:
                unique_rows[key] = row
        rows = list(unique_rows.values())
        required_context = ("regime", "pcr", "oi_signal", "ofr", "iv_percentile")
        complete_context = sum(
            1
            for row in rows
            if all(str(row.get(field) or "").strip() for field in required_context)
        )
        instrument_counts = Counter(
            str(row.get("instrument") or "").strip() for row in rows if row.get("instrument")
        )
        eligible_instruments = sorted(
            instrument
            for instrument, count in instrument_counts.items()
            if count >= min_samples_per_instrument
        )
        sample_ready = len(rows) >= min_samples
        context_coverage = complete_context / len(rows) if rows else 0.0
        coverage_ready = context_coverage >= 0.90
        instrument_ready = bool(instrument_counts) and len(eligible_instruments) == len(instrument_counts)
        ready = sample_ready and coverage_ready and instrument_ready
        status = {
            "mode": "ADVISORY_ONLY",
            "state": "READY_FOR_OFFLINE_VALIDATION" if ready else "COLLECTING_EVIDENCE",
            "live_adjustments_enabled": False,
            "self_learning_claim_allowed": False,
            "samples": len(rows),
            "minimum_samples": min_samples,
            "context_complete_samples": complete_context,
            "context_coverage_pct": round(context_coverage * 100.0, 1),
            "minimum_context_coverage_pct": 90.0,
            "instrument_counts": dict(instrument_counts),
            "minimum_samples_per_instrument": min_samples_per_instrument,
            "eligible_instruments": eligible_instruments,
            "validation_plan": [
                "Collect the minimum sample and feature-coverage thresholds.",
                "Freeze a candidate calibration rule before inspecting the holdout set.",
                "Use chronological walk-forward splits with costs and slippage.",
                "Require improvement in expectancy and drawdown without degrading tail loss.",
                "Run shadow-only for at least 20 sessions before any live promotion.",
                "Promote only through an explicit reviewed configuration change.",
            ],
        }
        self._validation_cache = status
        self._validation_cache_at = time.monotonic()
        return dict(status)
