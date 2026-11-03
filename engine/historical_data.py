"""Strictly offline data boundary for historical simulation.

Historical decisions may read persisted candles and timestamped archives only. Live
provider methods are deliberately absent from this interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


class HistoricalDataUnavailable(RuntimeError):
    """Required point-in-time evidence is unavailable; caller must abstain."""


@dataclass(frozen=True)
class OfflineCandleLoad:
    frames: dict[tuple[str, str], pd.DataFrame]
    missing: tuple[str, ...]


class HistoricalDataGateway:
    """Reads replay inputs without owning or calling a network-capable provider."""

    def __init__(self, cache_root: str | Path = "data_store/candles") -> None:
        self.cache_root = Path(cache_root)

    def candle_path(self, instrument: str, timeframe: str) -> Path:
        return self.cache_root / f"{str(instrument).upper()}_{timeframe}.csv"

    def load_candles(
        self,
        instruments: Iterable[str],
        timeframes: Iterable[str],
    ) -> OfflineCandleLoad:
        frames: dict[tuple[str, str], pd.DataFrame] = {}
        missing: list[str] = []
        for instrument in instruments:
            name = str(instrument).upper()
            for timeframe in timeframes:
                tf = str(timeframe)
                path = self.candle_path(name, tf)
                if not path.is_file():
                    missing.append(f"candle:{name}:{tf}")
                    continue
                try:
                    frame = pd.read_csv(path, index_col=0, parse_dates=True)
                except Exception as exc:
                    missing.append(f"candle:{name}:{tf}:{type(exc).__name__}")
                    continue
                if frame is None or frame.empty:
                    missing.append(f"candle:{name}:{tf}:empty")
                    continue
                frames[(name, tf)] = frame.sort_index()
        return OfflineCandleLoad(frames=frames, missing=tuple(sorted(missing)))


def unavailable_chain_quality(reason: str = "recorded option chain unavailable") -> dict[str, Any]:
    return {
        "source": "recorded_unavailable",
        "intelligence_usable": False,
        "historical_abstention": True,
        "reason": str(reason),
    }


def unavailable_depth(reason: str = "recorded order-book depth unavailable") -> dict[str, Any]:
    return {
        "ofr": 1.0,
        "source": "recorded_unavailable",
        "usable": False,
        "historical_abstention": True,
        "reason": str(reason),
    }
