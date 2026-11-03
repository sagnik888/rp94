"""Contract-keyed cumulative observations and comparable rolling interval features.

No time interpolation or fabricated bars. A window is unavailable when the sampling
cadence cannot resolve it. Restart/provider/contract/counter changes require warmup.
"""
from bisect import bisect_left, bisect_right
from collections import deque
from datetime import datetime, timezone, timedelta
import json
import math
from numbers import Real
from pathlib import Path
import threading
import time
import numpy as np


class DerivativeParticipation:
    def __init__(self, archive_dir=None):
        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._rows = {}
        self._metadata = {}
        self._archive_dir = Path(archive_dir) if archive_dir is not None else None
        self._pending = deque(maxlen=20000)
        self.archive_dropped_events = 0
        self._last_queued_at = {}
        self._archive_cache = {}
        # day -> instrument -> {"ats": [...], "events": [...], "sorted": bool}: the same
        # observation dicts as _archive_cache, ordered by time for windowed reads.
        self._archive_obs = {}
        # day -> earliest ``at`` the index still holds; None means the whole session.
        self._archive_floor = {}
        self._clock = time.time
        self._chains = deque(maxlen=1200)
        self._last_prune_stamp = None

    @staticmethod
    def _session(timestamp):
        return datetime.fromtimestamp(float(timestamp), timezone(timedelta(hours=5, minutes=30))).date().isoformat()

    def _queue(self, event):
        if len(self._pending) == self._pending.maxlen:
            self.archive_dropped_events += 1
        self._pending.append(event)

    def _queue_observation(self, key, row, metadata):
        if self._archive_dir is not None and row[0] > self._last_queued_at.get(key, 0):
            self._queue({"kind": "observation", "at": row[0], "key": key,
                         "row": row, "metadata": metadata})
            self._last_queued_at[key] = row[0]

    @classmethod
    def _json_safe(cls, value):
        if isinstance(value, dict):
            return {str(k): cls._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_safe(v) for v in value]
        if isinstance(value, Real) and not math.isfinite(value):
            return None
        return value

    def observe(self, instrument, family, provider, contract, timestamp, volume,
                *, oi=0.0, price=0.0, clock="exchange", unit="broker_reported_quantity",
                expiry="", option_type="", strike=0.0):
        try:
            values = [float(timestamp), float(volume), float(oi), float(price)]
        except (TypeError, ValueError, OverflowError):
            return
        if family not in {"futures", "options"} or not contract or not provider or not all(math.isfinite(v) for v in values):
            return
        stamp, quantity, interest, mark = values
        if stamp <= 0 or quantity < 0:
            return
        metadata = {"expiry": str(expiry or ""), "option_type": str(option_type or "").upper(),
                    "strike": float(strike or 0.0)}
        key = (instrument, family, provider, contract, clock, unit,
               metadata["expiry"], metadata["option_type"], metadata["strike"])
        with self._lock:
            rows = self._rows.setdefault(key, deque(maxlen=2400))
            if rows and stamp <= rows[-1][0]:
                return
            # Indian sessions, with no dependence on host timezone.
            if rows and (int((stamp+19800)//86400) != int((rows[-1][0]+19800)//86400)
                         or quantity < rows[-1][1]):
                rows.clear()
            observation = (stamp, quantity, interest, mark)
            # Keep the latest actual observation within each five-second bucket.
            # This preserves exchange clocks/cumulative totals without fabricating
            # evenly spaced values, and gives 2400 slots more than 15m coverage
            # even when a contract emits hundreds of ticks per second.
            if rows and int(stamp//5) == int(rows[-1][0]//5):
                rows[-1] = observation
            else:
                if rows:
                    self._queue_observation(key, rows[-1], self._metadata.get(key, metadata))
                rows.append(observation)
            self._metadata[key] = metadata
            # Retain one session and cap inactive contract keys.  The sweep visits
            # every contract key, and this runs on the broker tick thread for every
            # option tick; a key only expires after a day, so once a minute suffices.
            last_prune = self._last_prune_stamp
            if last_prune is not None and 0 <= stamp - last_prune < 60:
                return
            self._last_prune_stamp = stamp
            for old_key in list(self._rows):
                if self._rows[old_key] and stamp-self._rows[old_key][-1][0] > 86400:
                    del self._rows[old_key]
                    self._metadata.pop(old_key, None)
                    self._last_queued_at.pop(old_key, None)

    def record_option_chain(self, instrument, chain, timestamp, quality=None):
        """Record an actual response, including its original identity/quality."""
        if chain is None or chain.empty:
            return
        event = {"kind": "chain", "at": float(timestamp), "instrument": instrument,
                 "rows": chain.to_dict("records"), "attrs": dict(chain.attrs),
                 "quality": dict(quality or {})}
        with self._lock:
            self._chains.append(event)
            if self._archive_dir is not None:
                self._queue(event)

    def flush(self):
        """Persist only from background quote workers, never the hot tick path."""
        if self._archive_dir is None:
            return
        with self._flush_lock:
            self._flush_pending()

    def _flush_pending(self):
        with self._lock:
            for key, rows in self._rows.items():
                if rows:
                    self._queue_observation(key, rows[-1], self._metadata.get(key, {}))
            pending = list(self._pending)
            self._pending.clear()
        if not pending:
            return
        try:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
            by_day = {}
            for event in pending:
                by_day.setdefault(self._session(event["at"]), []).append(event)
            # A separate writer lock serializes appends. Disk latency never holds
            # the observation lock used by the tick/price path.
            for day, events in by_day.items():
                with (self._archive_dir / (day + ".jsonl")).open("a", encoding="utf-8") as handle:
                    for event in events:
                        handle.write(json.dumps(self._json_safe(event), default=str, allow_nan=False) + "\n")
                with self._lock:
                    if day in self._archive_cache:
                        self._archive_cache[day].extend(events)
                    if day in self._archive_obs:
                        self._index_observations(day, events)
                        self._prune_live_index(day)
        except (OSError, TypeError, ValueError):
            with self._lock:
                combined = pending + list(self._pending)
                self.archive_dropped_events += max(0, len(combined)-self._pending.maxlen)
                self._pending = deque(combined, maxlen=self._pending.maxlen)
            raise

    # Live reads only ever look back ~34 minutes (15min timeframe). Keep an hour of
    # observations in memory; replay reads (as_of far from the wall clock) still load
    # the whole session. The full 2026-09-17 archive held ~2 GB after a 14:11 restart.
    LIVE_RETENTION_SECONDS = 3600.0
    LIVE_AS_OF_TOLERANCE_SECONDS = 900.0
    # A flush archives each contract's latest row, so file order trails time order by
    # at most a flush interval; stop reading backwards well past that.
    TAIL_READ_MARGIN_SECONDS = 900.0
    TAIL_READ_CHUNK_BYTES = 4 * 1024 * 1024

    def _load_archive_day(self, day):
        """Read one session's archive into the cache (caller holds ``_flush_lock``)."""
        if day in self._archive_cache:
            return
        events = []
        path = self._archive_dir / (day + ".jsonl")
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                        if self._session(item["at"]) == day:
                            events.append(item)
                    except (ValueError, KeyError, TypeError):
                        continue  # A truncated last write cannot create evidence.
        if len(self._archive_cache) >= 2:
            evicted = next(iter(self._archive_cache))
            self._archive_cache.pop(evicted)
            self._archive_obs.pop(evicted, None)
            self._archive_floor.pop(evicted, None)
        self._archive_cache[day] = events
        self._archive_obs[day] = {}
        self._archive_floor[day] = None
        self._index_observations(day, events)

    def _load_archive_tail(self, day, floor):
        """Index only observations at or after ``floor``, reading the file backwards."""
        path = self._archive_dir / (day + ".jsonl")
        chunks = []
        if path.exists():
            with path.open("rb") as handle:
                handle.seek(0, 2)
                position = handle.tell()
                carry = b""
                while position > 0:
                    size = min(self.TAIL_READ_CHUNK_BYTES, position)
                    position -= size
                    handle.seek(position)
                    lines = (handle.read(size) + carry).split(b"\n")
                    # The first piece may be the tail of a line that starts earlier.
                    carry = lines.pop(0) if position > 0 else b""
                    found, newest = [], None
                    for raw in lines:
                        if not raw.strip():
                            continue
                        try:
                            item = json.loads(raw)
                            at = float(item["at"])
                            if self._session(at) != day:
                                continue
                        except (ValueError, KeyError, TypeError):
                            continue  # A truncated last write cannot create evidence.
                        newest = at if newest is None else max(newest, at)
                        if at >= floor:
                            found.append(item)
                    chunks.append(found)
                    if newest is not None and newest < floor - self.TAIL_READ_MARGIN_SECONDS:
                        break
        events = [item for found in reversed(chunks) for item in found]
        if len(self._archive_obs) >= 3:
            evicted = next(iter(self._archive_obs))
            self._archive_obs.pop(evicted, None)
            self._archive_floor.pop(evicted, None)
            self._archive_cache.pop(evicted, None)
        self._archive_obs[day] = {}
        self._archive_floor[day] = float(floor)
        self._index_observations(day, events)

    def _ensure_observation_index(self, day, as_of, cutoff):
        """Make the index able to answer ``cutoff..as_of`` (caller holds ``_flush_lock``)."""
        floor = self._archive_floor.get(day, None) if day in self._archive_obs else None
        if day in self._archive_obs and (floor is None or float(cutoff) >= floor):
            return
        live = abs(float(as_of) - float(self._clock())) <= self.LIVE_AS_OF_TOLERANCE_SECONDS
        if day not in self._archive_obs and live:
            self._load_archive_tail(day, float(as_of) - self.LIVE_RETENTION_SECONDS)
            if float(cutoff) >= self._archive_floor[day]:
                return
        # A replay read, or a live read older than the retained hour: the whole session.
        self._archive_cache.pop(day, None)
        self._archive_obs.pop(day, None)
        self._archive_floor.pop(day, None)
        self._load_archive_day(day)

    def _prune_live_index(self, day):
        """Drop observations older than the live retention window from a tail index."""
        floor = self._archive_floor.get(day)
        index = self._archive_obs.get(day)
        if floor is None or not index:
            return
        newest = max((bucket["ats"][-1] for bucket in index.values() if bucket["ats"]), default=None)
        if newest is None:
            return
        target = newest - self.LIVE_RETENTION_SECONDS
        # Prune in steps so a flush does not do this work every time.
        if target < floor + 300.0:
            return
        for bucket in index.values():
            cut = bisect_left(bucket["ats"], target)
            if cut:
                del bucket["ats"][:cut], bucket["seqs"][:cut], bucket["events"][:cut]
        self._archive_floor[day] = target

    def _index_observations(self, day, events):
        """Merge observation events into the per-instrument time index for ``day``.

        The index stays sorted by (at, seq) at all times. A flush archives each
        contract's latest row, so a batch routinely carries timestamps slightly older
        than the newest indexed one; flagging the whole bucket for a re-sort on every
        such flush re-sorted ~500k entries per read and cost 43% of GIL time live
        (2026-09-17 13:58). Only the tail from the batch's oldest timestamp is re-sorted.
        """
        index = self._archive_obs.setdefault(day, {})
        batches = {}
        for event in events:
            try:
                if event.get("kind") != "observation":
                    continue
                instrument = event["key"][0]
                at = float(event["at"])
            except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                continue
            bucket = index.setdefault(
                instrument, {"ats": [], "seqs": [], "events": [], "next_seq": 0}
            )
            batches.setdefault(instrument, []).append((at, bucket["next_seq"], event))
            bucket["next_seq"] += 1
        for instrument, batch in batches.items():
            bucket = index[instrument]
            ats, seqs, stored = bucket["ats"], bucket["seqs"], bucket["events"]
            start = bisect_right(ats, min(at for at, _, _ in batch))
            merged = sorted(
                list(zip(ats[start:], seqs[start:], stored[start:])) + batch,
                key=lambda item: (item[0], item[1]),
            )
            del ats[start:], seqs[start:], stored[start:]
            ats.extend(at for at, _, _ in merged)
            seqs.extend(seq for _, seq, _ in merged)
            stored.extend(event for _, _, event in merged)

    def _archived(self, as_of):
        if self._archive_dir is None:
            return []
        day = self._session(as_of)
        # Readers share the disk lock with flush, never the live-observation lock.
        with self._flush_lock:
            self._load_archive_day(day)
            return list(self._archive_cache[day])

    def _archived_observations(self, instrument, as_of, cutoff):
        """Archived observations for one instrument with ``cutoff <= at <= as_of``.

        features() used to walk the whole day's archive on every call. The archive
        grows all session (258 MB by 13:47 on 2026-09-17), and features() runs per
        instrument and timeframe on every scan, so live scans slowed as the day went
        on: 30% of all GIL time after a restart. The window it needs is ~35 minutes.
        """
        if self._archive_dir is None:
            return []
        day = self._session(as_of)
        with self._flush_lock:
            self._ensure_observation_index(day, as_of, cutoff)
            bucket = (self._archive_obs.get(day) or {}).get(instrument)
            if not bucket:
                return []
            ats = bucket["ats"]
            lo, hi = bisect_left(ats, float(cutoff)), bisect_right(ats, float(as_of))
            # Hand the window back in archive order: features() lists contracts in the
            # order their first in-window observation was archived.
            events = bucket["events"][lo:hi]
            # Sort the numeric archive ordinals in native code. A Python key
            # callback per observation monopolized the GIL as this window grew.
            order = np.argsort(np.asarray(bucket["seqs"][lo:hi]), kind="stable")
            return np.take(np.asarray(events, dtype=object), order).tolist()

    def recorded_option_chain(self, instrument, as_of, expiry="", max_age=120.0):
        """As-of-only replay; no current response or later-day fallback."""
        import pandas as pd
        events = self._archived(as_of)
        with self._lock:
            events += list(self._chains)
        matches = [e for e in events if e.get("kind") == "chain" and e.get("instrument") == instrument
                   and 0 <= float(as_of)-float(e["at"]) <= max_age
                   and self._session(e["at"]) == self._session(as_of)
                   and (not expiry or str(e.get("attrs", {}).get("expiry")) == str(expiry))]
        if not matches:
            return pd.DataFrame(), {"intelligence_usable": False, "source": "recorded_unavailable",
                                    "reason": "No recorded option chain at or before analysis time"}
        event = max(matches, key=lambda e: e["at"])
        chain = pd.DataFrame(event["rows"])
        chain.attrs.update(event["attrs"])
        quality = dict(event["quality"])
        quality.update(age_seconds=float(as_of)-float(event["at"]), recorded_at=event["at"],
                       replay_source="recorded_option_chain")
        return chain, quality

    def features(self, instrument, timeframe, as_of):
        seconds = {"1min":60, "5min":300, "15min":900}[timeframe]
        as_of = float(as_of)
        max_gap = min(120.0, seconds / 2)
        cutoff = as_of-2*seconds-2*max_gap
        result = {"instrument":instrument, "timeframe":timeframe, "as_of":as_of, "futures":[], "options":[]}
        with self._lock:
            # Row stamps strictly increase within a deque (observe() refuses a stamp at
            # or before the last one, and clears on a new day or counter reset), so
            # walking back from the newest row until the cutoff yields exactly the rows
            # the window filter below would keep -- without copying all 2400 under the
            # lock the tick thread takes on every option tick.
            snapshots = {}
            for key, rows in self._rows.items():
                if key[0] != instrument:
                    continue
                recent = []
                for row in reversed(rows):
                    if row[0] < cutoff:
                        break
                    recent.append(row)
                recent.reverse()
                snapshots[key] = recent
            metadata = dict(self._metadata)
        # Only in-window observations can reach the output. Metadata for keys outside
        # the window was never read: each key's metadata is part of the key itself.
        for event in self._archived_observations(instrument, as_of, cutoff):
            key = tuple(event["key"])
            snapshots.setdefault(key, []).append(tuple(event["row"]))
            metadata.setdefault(key, event.get("metadata", {}))
        for key, rows in snapshots.items():
            rows = sorted(set(tuple(r) for r in rows if cutoff <= r[0] <= as_of
                              and int((r[0]+19800)//86400) == int((as_of+19800)//86400)))
            # A cumulative-counter reset is a new measurement segment, including replay.
            for i in range(len(rows)-1, 0, -1):
                if rows[i][1] < rows[i-1][1]:
                    rows = rows[i:]
                    break
            _, family, provider, contract, clock, unit = key[:6]
            item = {"contract":contract,"provider":provider,"clock":clock,"unit":unit,
                    "instrument":instrument, "family":family, **metadata.get(key, {}),
                    "timeframe":timeframe,"available":False,"entry_eligible":False,
                    "reason":"insufficient_interval_coverage"}
            if not rows or as_of-rows[-1][0] > max_gap:
                item["reason"] = "stale_observation"
            else:
                end = rows[-1]
                earlier = [r for r in rows if r[0] <= end[0]-seconds]
                if earlier:
                    start = earlier[-1]
                    window = [r for r in rows if start[0] <= r[0] <= end[0]]
                    span = end[0]-start[0]
                    gaps = [b[0]-a[0] for a,b in zip(window,window[1:])]
                    if span <= seconds+max_gap and max(gaps,default=0) <= max_gap:
                        item.update(available=True, entry_eligible=clock == "exchange", reason="",
                                    start_at=start[0], end_at=end[0], coverage_seconds=span,
                                    volume_change=end[1]-start[1], oi_change=end[2]-start[2],
                                    price_change=end[3]-start[3], price=end[3],
                                    oi=end[2], start_price=start[3],
                                    max_observation_gap_seconds=max(gaps, default=0),
                                    volume_per_second=(end[1]-start[1])/span)
                        prior = [r for r in rows if r[0] <= start[0]-seconds]
                        if prior:
                            p = prior[-1]
                            prior_rows = [r for r in rows if p[0] <= r[0] <= start[0]]
                            prior_gaps = [b[0]-a[0] for a,b in zip(prior_rows,prior_rows[1:])]
                            if start[0]-p[0] <= seconds+max_gap and max(prior_gaps,default=0) <= max_gap:
                                rate = (start[1]-p[1])/(start[0]-p[0])
                                item['relative_volume_ratio'] = item['volume_per_second']/rate if rate > 0 else None
            result[family].append(item)
        return result
