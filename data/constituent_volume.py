"""Effective-dated, complete-interval constituent share volume.

Broker stock candles are the reconciliation authority. Streaming cumulative totals
are provisional telemetry: sampled packets cannot certify exact minute boundaries.
Missing constituents never become zero-volume observations.
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import numpy as np

CASH_VOLUME_BASES = frozenset({"fyers_index", "constituent_shares"})
INDEX_VENUES = {"NIFTY": "NSE", "BANKNIFTY": "NSE", "MIDCPNIFTY": "NSE", "SENSEX": "BSE"}

# Cash constituents stop producing 1-minute candles before the index does. Queried
# from Kite's historical endpoint on 2026-10-30: NSE equities return 360 bars
# (09:15-15:14, the 15:14 slot closing at 15:15) while the index returns 375
# (09:15-15:29), the extra 15 bars being a frozen closing print with zero volume.
# No constituent can certify those bars, so certification must never require them.
CONSTITUENT_SESSION_CLOSE = (15, 15)


@dataclass(frozen=True)
class Membership:
    index: str
    effective_from: date
    effective_until: date  # Exclusive; an explicit boundary prevents indefinite stale membership.
    version: str
    source: str
    members: tuple[tuple[str, str], ...]


class ConstituentVolumeService:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.memberships: tuple[Membership, ...] = ()
        self.last_error = "membership_not_loaded"
        self._live: dict = {}
        self.load()

    def load(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                raise ValueError("unsupported_membership_schema")
            records = []
            for raw in payload["memberships"]:
                index = str(raw["index"]).upper()
                venue = INDEX_VENUES[index]
                source = str(raw["source"])
                host = (urlparse(source).hostname or "").lower()
                if not any(host == domain or host.endswith("." + domain) for domain in
                           ("niftyindices.com", "nseindia.com", "bseindia.com", "bseindices.com")):
                    raise ValueError("membership_source_not_authoritative")
                members = tuple((str(m["exchange"]).upper(), str(m["symbol"]).upper()) for m in raw["members"])
                if not members or len(set(members)) != len(members):
                    raise ValueError("membership_empty_or_duplicate")
                if len(members) != int(raw["expected_count"]):
                    raise ValueError("membership_count_mismatch")
                if any(exchange != venue or not symbol for exchange, symbol in members):
                    raise ValueError("membership_venue_mismatch")
                start, end = date.fromisoformat(raw["effective_from"]), date.fromisoformat(raw["effective_until"])
                if start >= end or not str(raw["version"]).strip():
                    raise ValueError("membership_invalid_validity")
                records.append(Membership(index, start, end, str(raw["version"]), source, members))
            for i, current in enumerate(records):
                if any(current.index == prior.index and current.effective_from < prior.effective_until
                       and prior.effective_from < current.effective_until for prior in records[:i]):
                    raise ValueError("membership_overlapping_versions")
            if not records:
                raise ValueError("membership_empty")
        except (OSError, ValueError, KeyError, TypeError):
            with self._lock:
                self.memberships = ()
                self.last_error = "membership_invalid_or_missing"
            return False
        with self._lock:
            self.memberships = tuple(records)
            self.last_error = ""
        return True

    def membership(self, index: str, day: date) -> Membership | None:
        with self._lock:
            return next((m for m in self.memberships if m.index == index and
                         m.effective_from <= day < m.effective_until), None)

    def instruments(self, start: date, end: date) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(sorted({key for m in self.memberships if m.effective_from <= end
                                 and start < m.effective_until for key in m.members}))

    @staticmethod
    def local_frame(frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        result.index = pd.DatetimeIndex(pd.to_datetime(result.index))
        if result.index.tz is not None:
            result.index = result.index.tz_convert("Asia/Kolkata").tz_localize(None)
        return result[~result.index.duplicated(keep="last")].sort_index()

    def aggregate(self, index: str, bars: pd.DatetimeIndex, stock_frames: dict,
                  provider: str, *, now=None) -> pd.DataFrame:
        """Sum only complete 1-minute intervals from one coherent provider.

        Explicit finite zero is valid. Missing/negative values, an unfinished bar,
        or an absent member makes the whole index interval unavailable.
        """
        bars = pd.DatetimeIndex(bars)
        if bars.tz is not None:
            bars = bars.tz_convert("Asia/Kolkata").tz_localize(None)
        reference = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Kolkata")
        if reference.tzinfo is not None:
            reference = reference.tz_convert("Asia/Kolkata").tz_localize(None)
        normalized = {key: self.local_frame(frame) for key, frame in stock_frames.items()
                      if isinstance(frame, pd.DataFrame) and not frame.empty and "volume" in frame}
        if not len(bars):
            return pd.DataFrame(index=bars)
        # Align each stock once. Per-cell .at lookups across stocks x minutes held
        # the GIL for seconds and stalled the unrelated live scanner workers.
        totals = np.zeros(len(bars), dtype=float)
        observed = np.zeros(len(bars), dtype=int)
        expected = np.zeros(len(bars), dtype=int)
        versions = np.full(len(bars), "", dtype=object)
        eligible = np.asarray((bars == bars.floor("min")) &
                              (bars+pd.Timedelta(minutes=1) <= reference))
        dates = bars.date
        groups = {}
        for day in set(dates):
            membership = self.membership(index, day)
            if membership is not None:
                groups.setdefault(membership, []).extend(np.flatnonzero(dates == day))
        aligned = {}
        for membership, locations in groups.items():
            positions = np.asarray(locations, dtype=int)
            expected[positions] = len(membership.members)
            versions[positions] = membership.version
            for key in membership.members:
                frame = normalized.get(key)
                if frame is None:
                    continue
                if key not in aligned:
                    volume = pd.to_numeric(frame["volume"], errors="coerce").reindex(bars).to_numpy(dtype=float, na_value=np.nan)
                    valid = np.isfinite(volume) & (volume >= 0) & eligible
                    if "volume_complete" in frame:
                        valid &= frame["volume_complete"].eq(True).astype("boolean").reindex(bars).to_numpy(dtype=bool, na_value=False)
                    aligned[key] = volume, valid
                volume, valid = aligned[key]
                use = valid[positions]
                totals[positions] += np.where(use, volume[positions], 0.)
                observed[positions] += use
        complete = (expected > 0) & (observed == expected)
        return pd.DataFrame(dict(volume=np.where(complete, totals, np.nan),
            volume_complete=complete, volume_basis=np.where(complete, "constituent_shares", "UNAVAILABLE"),
            volume_provider=provider, volume_members_observed=observed,
            volume_members_expected=expected, membership_version=versions), index=bars)

    def observe(self, exchange: str, symbol: str, volume: float, exchange_at: float,
                received_at: float, provider: str) -> bool:
        """Store sampled cumulative activity without certifying a candle interval."""
        if not all(math.isfinite(float(v)) for v in (volume, exchange_at, received_at)):
            return False
        if volume < 0 or not 0 <= received_at - exchange_at <= 20:
            return False
        day = pd.Timestamp(exchange_at, unit="s", tz="UTC").tz_convert("Asia/Kolkata").date()
        key = (provider, exchange, symbol)
        with self._lock:
            prior = self._live.get(key)
            if prior and prior["day"] == day and (exchange_at < prior["exchange_at"] or volume < prior["volume"]):
                return False
            self._live[key] = dict(day=day, volume=volume, exchange_at=exchange_at, received_at=received_at)
        return True

    def activity(self, index: str, *, now: float, provider: str = "angel_ws") -> dict:
        day = pd.Timestamp(now, unit="s", tz="UTC").tz_convert("Asia/Kolkata").date()
        membership = self.membership(index, day)
        with self._lock:
            rows = [self._live.get((provider, *key)) for key in membership.members] if membership else []
            usable = [row for row in rows if row and row["day"] == day and 0 <= now-row["exchange_at"] <= 20]
            return {"volume": sum(row["volume"] for row in usable) if rows and len(usable) == len(rows) else None,
                    "members_observed": len(usable), "members_expected": len(rows),
                    "volume_complete": False, "provisional": True,
                    "volume_basis": "constituent_shares", "provider": provider}

    def health(self, day: date | None = None) -> dict:
        day = day or pd.Timestamp.now(tz="Asia/Kolkata").date()
        return {"membership_available": {index: self.membership(index, day) is not None for index in INDEX_VENUES},
                "last_error": self.last_error, "live_interval_certified": False}
