"""Realized-slippage ledger.

Measures actual broker fills against the signal/decision prices that produced
them and maintains an EWMA of relative adverse slippage per instrument type and
per instrument. Once enough real samples exist, the charge model
(engine/trade_accounting.py) uses these measured values instead of the static
settings assumptions — so simulated P&L converges to the account's actual
execution quality instead of a guess.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from loguru import logger

DEFAULT_STATS_PATH = "data_store/slippage_stats.json"
# EWMA weight of the newest fill. 0.2 ≈ the last ~10 fills dominate, so the
# estimate tracks current market conditions without whipsawing on one bad fill.
EWMA_ALPHA = 0.2


class SlippageTracker:
    def __init__(self, path: str = DEFAULT_STATS_PATH, alpha: float = EWMA_ALPHA) -> None:
        self._path = Path(path)
        self._alpha = float(alpha)
        self._lock = threading.Lock()
        self._stats: Dict[str, Dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._stats = {
                        str(key): dict(value)
                        for key, value in raw.items()
                        if isinstance(value, dict)
                    }
        except Exception as exc:
            logger.warning(f"Slippage stats load failed ({self._path}): {exc}")
            self._stats = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._stats, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug(f"Slippage stats save skipped: {exc}")

    @staticmethod
    def _keys(inst_type: str, instrument: str = "") -> list:
        """Aggregate key first, per-instrument key second (most specific last)."""
        inst_type = str(inst_type or "FUT").upper()
        keys = [inst_type]
        instrument = str(instrument or "").strip().upper()
        if instrument:
            keys.append(f"{inst_type}|{instrument}")
        return keys

    def record(
        self,
        *,
        inst_type: str,
        instrument: str = "",
        reference_price: float,
        adverse_points: float,
        leg: str = "ENTRY",
    ) -> None:
        """Record one real fill. adverse_points is signed: positive = filled worse
        than the reference price, negative = price improvement."""
        try:
            ref = abs(float(reference_price or 0.0))
            if ref <= 0:
                return
            frac = float(adverse_points) / ref
        except (TypeError, ValueError):
            return
        # A fill more than 5% away from the reference is a data error (wrong quote,
        # stale signal), not slippage — recording it would poison the EWMA.
        if abs(frac) > 0.05:
            logger.warning(
                f"Slippage sample rejected as implausible: {leg} {inst_type} {instrument} "
                f"ref={ref:.2f} adverse={float(adverse_points):.2f} ({frac * 100:.2f}%)"
            )
            return
        with self._lock:
            for key in self._keys(inst_type, instrument):
                row = self._stats.setdefault(key, {"ewma_frac": 0.0, "count": 0})
                count = int(row.get("count", 0) or 0)
                if count <= 0:
                    row["ewma_frac"] = frac
                else:
                    row["ewma_frac"] = self._alpha * frac + (1.0 - self._alpha) * float(row.get("ewma_frac", 0.0) or 0.0)
                row["count"] = count + 1
                row["last_leg"] = str(leg or "")
                row["updated_at"] = datetime.now().isoformat()
            self._save()

    def calibrated_fraction(
        self,
        inst_type: str,
        instrument: str = "",
        min_samples: int = 5,
    ) -> Optional[float]:
        """Measured per-leg slippage as a fraction of price, or None if not enough
        real samples yet. Prefers the per-instrument estimate over the aggregate."""
        min_samples = max(1, int(min_samples or 1))
        with self._lock:
            for key in reversed(self._keys(inst_type, instrument)):
                row = self._stats.get(key)
                if row and int(row.get("count", 0) or 0) >= min_samples:
                    try:
                        return float(row.get("ewma_frac", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        continue
        return None

    def snapshot(self) -> Dict[str, Dict]:
        with self._lock:
            return {key: dict(value) for key, value in self._stats.items()}


_tracker: Optional[SlippageTracker] = None
_tracker_lock = threading.Lock()


def get_slippage_tracker() -> SlippageTracker:
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = SlippageTracker()
        return _tracker
