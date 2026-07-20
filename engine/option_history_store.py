"""Fast, validated storage for repeatedly loaded option candle history."""

from __future__ import annotations

import os
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger


class OptionHistoryStore:
    """Keep CSV as source of truth and use a validated binary read cache."""

    CACHE_VERSION = 1

    def __init__(
        self,
        max_memory_entries: int = 256,
        miss_path: str | Path = "data_store/option_history_misses.json",
        bundle_path: str | Path = "data_store/option_history_bundle.parquet",
    ) -> None:
        self.max_memory_entries = max(1, int(max_memory_entries))
        self._frames: OrderedDict[str, tuple[tuple[int, int], pd.DataFrame]] = OrderedDict()
        self._lock = threading.RLock()
        self.miss_path = Path(miss_path)
        self.bundle_path = Path(bundle_path)
        self.bundle_manifest_path = self.bundle_path.with_suffix(
            self.bundle_path.suffix + ".manifest.json"
        )
        self._misses: Optional[dict[str, float]] = None
        self._bundle_loaded = False
        self._bundle_signatures: dict[str, tuple[int, int]] = {}
        self._bundle_frames: dict[str, pd.DataFrame] = {}

    @staticmethod
    def _signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return int(stat.st_mtime_ns), int(stat.st_size)

    @staticmethod
    def _normalize(frame: Optional[pd.DataFrame]) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame() if frame is None else frame
        work = frame
        if not isinstance(work.index, pd.DatetimeIndex):
            work = work.copy()
            work.index = pd.to_datetime(work.index, errors="coerce")
            work = work[~work.index.isna()]
        if not work.index.is_monotonic_increasing or work.index.has_duplicates:
            work = work[~work.index.duplicated(keep="last")].sort_index()
        return work

    def _remember(
        self,
        key: str,
        signature: tuple[int, int],
        frame: pd.DataFrame,
    ) -> pd.DataFrame:
        self._frames[key] = (signature, frame)
        self._frames.move_to_end(key)
        while len(self._frames) > self.max_memory_entries:
            self._frames.popitem(last=False)
        return frame

    def load(self, csv_path: str | Path) -> pd.DataFrame:
        path = Path(csv_path)
        if not path.exists():
            return pd.DataFrame()
        key = str(path.resolve())
        signature = self._signature(path)

        with self._lock:
            cached = self._frames.get(key)
            if cached and cached[0] == signature:
                self._frames.move_to_end(key)
                return cached[1]

            self._load_bundle_if_available()
            bundled = self._bundle_frames.get(path.name)
            bundled_signature = self._bundle_signatures.get(path.name)
            if bundled is not None and bundled_signature == signature:
                return self._remember(
                    key,
                    signature,
                    self._normalize(bundled),
                )

            frame = self._normalize(
                pd.read_csv(path, index_col=0, parse_dates=True)
            )
            return self._remember(key, signature, frame)

    def _load_bundle_if_available(self) -> None:
        if self._bundle_loaded:
            return
        self._bundle_loaded = True
        if not self.bundle_path.exists() or not self.bundle_manifest_path.exists():
            return
        try:
            manifest = json.loads(
                self.bundle_manifest_path.read_text(encoding="utf-8")
            )
            if manifest.get("version") != self.CACHE_VERSION:
                return
            self._bundle_signatures = {
                str(name): tuple(signature)
                for name, signature in (manifest.get("signatures") or {}).items()
            }
            combined = pd.read_parquet(self.bundle_path, engine="pyarrow")
            contracts = combined.index.get_level_values("contract").unique()
            self._bundle_frames = {}
            for name in contracts:
                frame = combined.xs(name, level="contract")
                frame.index.name = None
                self._bundle_frames[str(name)] = frame
        except Exception as exc:
            logger.debug(f"Option history bundle ignored: {exc}")

    def ensure_bundle(
        self,
        cache_dir: str | Path = "data_store/candles",
    ) -> dict[str, float | int | bool]:
        started = time.perf_counter()
        paths = sorted(Path(cache_dir).glob("*[CP]E_*.csv"))
        signatures = {path.name: self._signature(path) for path in paths}
        if not paths:
            return {"rebuilt": False, "files": 0, "seconds": 0.0}
        with self._lock:
            self._load_bundle_if_available()
            if (
                signatures
                and signatures == self._bundle_signatures
                and len(self._bundle_frames) == len(signatures)
            ):
                return {
                    "rebuilt": False,
                    "files": len(signatures),
                    "seconds": round(time.perf_counter() - started, 3),
                }

            frames = {
                path.name: self._normalize(
                    pd.read_csv(path, index_col=0, parse_dates=True)
                )
                for path in paths
            }
            self.bundle_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.bundle_path.with_suffix(
                self.bundle_path.suffix + f".{os.getpid()}.tmp.parquet"
            )
            temp_manifest = self.bundle_manifest_path.with_suffix(
                self.bundle_manifest_path.suffix + f".{os.getpid()}.tmp"
            )
            try:
                combined = pd.concat(
                    frames,
                    names=["contract", "timestamp"],
                )
                combined.to_parquet(
                    temp_path,
                    engine="pyarrow",
                    compression=None,
                )
                temp_manifest.write_text(
                    json.dumps(
                        {
                            "version": self.CACHE_VERSION,
                            "signatures": signatures,
                        },
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                os.replace(temp_path, self.bundle_path)
                os.replace(temp_manifest, self.bundle_manifest_path)
            except Exception:
                temp_path.unlink(missing_ok=True)
                temp_manifest.unlink(missing_ok=True)
                raise
            self._bundle_loaded = True
            self._bundle_signatures = signatures
            self._bundle_frames = frames
            return {
                "rebuilt": True,
                "files": len(signatures),
                "seconds": round(time.perf_counter() - started, 3),
            }

    def invalidate(self, csv_path: str | Path) -> None:
        path = Path(csv_path)
        key = str(path.resolve())
        with self._lock:
            self._frames.pop(key, None)
            self._bundle_frames.pop(path.name, None)
            self._bundle_signatures.pop(path.name, None)

    def _load_misses(self) -> dict[str, float]:
        if self._misses is not None:
            return self._misses
        try:
            payload = json.loads(self.miss_path.read_text(encoding="utf-8"))
            self._misses = {
                str(key): float(value)
                for key, value in (payload or {}).items()
                if float(value or 0.0) > 0
            }
        except Exception:
            self._misses = {}
        return self._misses

    def _save_misses(self) -> None:
        self.miss_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.miss_path.with_suffix(
            self.miss_path.suffix + f".{os.getpid()}.tmp"
        )
        temp_path.write_text(
            json.dumps(self._misses or {}, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temp_path, self.miss_path)

    @staticmethod
    def miss_key(symbol: str, interval: str, days_back: int) -> str:
        return f"{symbol}|{interval}|{int(days_back)}"

    def has_recent_miss(
        self,
        symbol: str,
        interval: str,
        days_back: int,
        ttl_seconds: float,
    ) -> bool:
        key = self.miss_key(symbol, interval, days_back)
        with self._lock:
            misses = self._load_misses()
            missed_at = float(misses.get(key, 0.0) or 0.0)
            if missed_at <= 0:
                return False
            if time.time() - missed_at <= max(0.0, float(ttl_seconds)):
                return True
            misses.pop(key, None)
            self._save_misses()
            return False

    def record_miss(self, symbol: str, interval: str, days_back: int) -> None:
        key = self.miss_key(symbol, interval, days_back)
        with self._lock:
            self._load_misses()[key] = time.time()
            self._save_misses()

    def clear_miss(self, symbol: str, interval: str, days_back: int) -> None:
        key = self.miss_key(symbol, interval, days_back)
        with self._lock:
            misses = self._load_misses()
            if key in misses:
                misses.pop(key, None)
                self._save_misses()

    @staticmethod
    def filter_day(frame: pd.DataFrame, trade_date) -> pd.DataFrame:
        if frame is None or frame.empty or trade_date is None:
            return pd.DataFrame() if frame is None else frame
        try:
            target = pd.Timestamp(trade_date).normalize()
            index = frame.index
            if not isinstance(index, pd.DatetimeIndex):
                return pd.DataFrame()
            if index.tz is not None:
                target = target.tz_localize("Asia/Kolkata").tz_convert(index.tz)
            start = index.searchsorted(target, side="left")
            end = index.searchsorted(target + pd.Timedelta(days=1), side="left")
            return frame.iloc[start:end]
        except Exception:
            return pd.DataFrame()


_OPTION_HISTORY_STORE = OptionHistoryStore()


def get_option_history_store() -> OptionHistoryStore:
    return _OPTION_HISTORY_STORE
