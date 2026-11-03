"""Early-entry build-up detector (2026-10-09, user). Shadow only: records, never trades.

The closed-bar engines confirm a move after its first bar. The user asked for entries
"while the move is about to begin with volume, PCR and OI building up". This module
watches the inputs that lead the bar close and records the moments they line up, so the
idea can be judged on real outcomes before anything trades on it
(scripts/evaluate_buildup_shadow.py).

Inputs, all already collected by the live system (no extra broker calls):
  * the near-month futures quote the index-volume worker polls every ~15s: price,
    cumulative volume and open interest;
  * every option-chain refresh: put/call open interest, giving PCR.

An event needs a price impulse (the futures price moved at least
`buildup_impulse_min_pct` in `buildup_window_seconds`, and at least
`buildup_impulse_baseline_mult` times its usual move once a baseline exists) plus at
least `buildup_min_legs` of:
  * volume: futures volume over the window >= `buildup_volume_ratio` x its usual window;
  * OI: futures open interest rising >= `buildup_oi_min_pct` (fresh positions in the
    direction of the move: long build-up up, short build-up down);
  * PCR: put/call OI ratio moving with the trade by >= `buildup_pcr_min_change`
    (rising for LONG as puts are written, falling for SHORT).
Peer indices moving the same way are recorded, not required.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from loguru import logger

LOG_PATH = Path("data_store") / "buildup_shadow.jsonl"
_MAX_FUTURES_SAMPLES = 600   # ~2.5h at the 15s poll
_MAX_CHAIN_SAMPLES = 120


def _g(settings, name: str, default: float) -> float:
    try:
        value = float(getattr(settings, name, default))
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _flag(settings, name: str, default: bool = True) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default if value is None else value)


def chain_pcr(chain) -> Tuple[float, float, float]:
    """(PCR, total put OI, total call OI) from a wide (call_oi/put_oi) or long (type/oi) chain."""
    try:
        cols = set(getattr(chain, "columns", []))
        if {"call_oi", "put_oi"} <= cols:
            calls = float(chain["call_oi"].fillna(0).astype(float).sum())
            puts = float(chain["put_oi"].fillna(0).astype(float).sum())
        elif {"type", "oi"} <= cols:
            kind = chain["type"].astype(str).str.upper()
            calls = float(chain.loc[kind == "CE", "oi"].fillna(0).astype(float).sum())
            puts = float(chain.loc[kind == "PE", "oi"].fillna(0).astype(float).sum())
        else:
            return 0.0, 0.0, 0.0
    except Exception:
        return 0.0, 0.0, 0.0
    return (puts / calls if calls > 0 else 0.0), puts, calls


class BuildupShadow:
    def __init__(self, settings_getter: Optional[Callable[[], Any]] = None,
                 clock: Callable[[], float] = time.time, log_path: Optional[Path] = None):
        if settings_getter is None:
            from config.settings import get_settings
            settings_getter = get_settings
        self._settings = settings_getter
        self._clock = clock
        self.log_path = log_path or LOG_PATH
        self._lock = threading.Lock()
        self._futures: Dict[str, Deque[Tuple[float, float, float, float]]] = {}
        self._chain: Dict[str, Deque[Tuple[float, float]]] = {}
        self._last_event: Dict[Tuple[str, str], float] = {}
        self._latest_move: Dict[str, Tuple[float, float]] = {}

    # ── inputs ────────────────────────────────────────────────────────────────
    def observe_chain(self, index: str, chain, at: Optional[float] = None) -> None:
        if not _flag(self._settings(), "buildup_shadow_enabled", True):
            return
        pcr, puts, calls = chain_pcr(chain)
        if pcr <= 0:
            return
        with self._lock:
            self._chain.setdefault(str(index).upper(), deque(maxlen=_MAX_CHAIN_SAMPLES)).append(
                (float(at or self._clock()), pcr))

    def observe_futures(self, index: str, at: float, price: float, cumulative_volume: float,
                        oi: float) -> Optional[Dict[str, Any]]:
        """Record one futures quote and return an event when the build-up lines up."""
        settings = self._settings()
        if not _flag(settings, "buildup_shadow_enabled", True):
            return None
        try:
            at, price, volume, oi = float(at), float(price), float(cumulative_volume), float(oi or 0.0)
        except (TypeError, ValueError):
            return None
        if at <= 0 or price <= 0 or volume <= 0:
            return None
        name = str(index).upper()
        with self._lock:
            samples = self._futures.setdefault(name, deque(maxlen=_MAX_FUTURES_SAMPLES))
            if samples and (at <= samples[-1][0] or volume < samples[-1][2]):
                if volume < samples[-1][2]:
                    samples.clear()   # contract roll or a new session
                else:
                    return None
            samples.append((at, price, volume, oi))
            legs = self._legs(name, settings)
            if legs is None:
                return None
            self._latest_move[name] = (at, legs["move_pct"])
            legs["peers_aligned"] = sorted(
                peer for peer, (peer_at, move) in self._latest_move.items()
                if peer != name and at - peer_at <= 60 and move * legs["move_pct"] > 0
                and abs(move) >= 0.5 * _g(settings, "buildup_impulse_min_pct", 0.08))
            if not legs["impulse"] or legs["legs_true"] < int(_g(settings, "buildup_min_legs", 2)):
                return None
            key = (name, legs["direction"])
            cooldown = _g(settings, "buildup_cooldown_seconds", 600.0)
            if at - self._last_event.get(key, -1e18) < cooldown:
                return None
            self._last_event[key] = at
        event = {"index": name, "at": datetime.fromtimestamp(at).isoformat(timespec="seconds"),
                 "epoch": round(at, 3), "price": price, **legs}
        self._emit(event)
        return event

    # ── detection ─────────────────────────────────────────────────────────────
    @staticmethod
    def _at_or_before(samples, when: float):
        best = None
        for sample in samples:
            if sample[0] <= when:
                best = sample
            else:
                break
        return best

    def _legs(self, name: str, settings) -> Optional[Dict[str, Any]]:
        samples = self._futures.get(name)
        if not samples or len(samples) < 3:
            return None
        window = _g(settings, "buildup_window_seconds", 180.0)
        now_at, price, volume, oi = samples[-1]
        past = self._at_or_before(samples, now_at - window)
        if past is None or now_at - past[0] > 2 * window:
            return None
        move = (price - past[1]) / past[1] * 100.0
        window_volume = volume - past[2]
        direction = "LONG" if move > 0 else "SHORT"
        # Usual window move and volume, from non-overlapping windows in the baseline span.
        span = _g(settings, "buildup_baseline_minutes", 60.0) * 60.0
        moves, volumes = [], []
        cursor = past[0]
        while cursor - window >= now_at - span:
            a = self._at_or_before(samples, cursor - window)
            b = self._at_or_before(samples, cursor)
            if a is None or b is None or b[0] - a[0] <= 0 or b[0] - a[0] > 2 * window:
                break
            moves.append(abs(b[1] - a[1]) / a[1] * 100.0)
            volumes.append(max(0.0, b[2] - a[2]))
            cursor -= window
        min_baseline = int(_g(settings, "buildup_min_baseline_windows", 5))
        has_baseline = len(moves) >= min_baseline
        usual_move = median(moves) if has_baseline else 0.0
        usual_volume = median(volumes) if has_baseline else 0.0
        impulse = abs(move) >= _g(settings, "buildup_impulse_min_pct", 0.08) and (
            not has_baseline or usual_move <= 0
            or abs(move) >= _g(settings, "buildup_impulse_baseline_mult", 2.0) * usual_move)
        volume_ratio = window_volume / usual_volume if usual_volume > 0 else 0.0
        volume_leg = has_baseline and volume_ratio >= _g(settings, "buildup_volume_ratio", 1.5)
        oi_window = _g(settings, "buildup_oi_window_seconds", 300.0)
        oi_past = self._at_or_before(samples, now_at - oi_window)
        oi_change = ((oi - oi_past[3]) / oi_past[3] * 100.0
                     if oi_past is not None and oi_past[3] > 0 and oi > 0 else 0.0)
        oi_min = _g(settings, "buildup_oi_min_pct", 0.05)
        oi_type = ("long_buildup" if move > 0 else "short_buildup") if oi_change >= oi_min else (
            ("short_covering" if move > 0 else "long_unwinding") if oi_change <= -oi_min else "flat")
        oi_leg = oi_change >= oi_min
        chain = self._chain.get(name) or deque()
        pcr_now = chain[-1][1] if chain else 0.0
        pcr_past = self._at_or_before(chain, (chain[-1][0] if chain else now_at)
                                      - _g(settings, "buildup_pcr_window_seconds", 300.0)) if chain else None
        pcr_change = pcr_now - pcr_past[1] if pcr_past is not None and pcr_now > 0 else 0.0
        pcr_fresh = bool(chain) and now_at - chain[-1][0] <= _g(settings, "buildup_pcr_max_age_seconds", 240.0)
        pcr_min = _g(settings, "buildup_pcr_min_change", 0.02)
        pcr_leg = pcr_fresh and (pcr_change >= pcr_min if move > 0 else pcr_change <= -pcr_min)
        legs_true = int(volume_leg) + int(oi_leg) + int(pcr_leg)
        return {
            "direction": direction, "move_pct": round(move, 4), "window_seconds": window,
            "impulse": bool(impulse), "baseline_windows": len(moves), "usual_move_pct": round(usual_move, 4),
            "volume_ratio": round(volume_ratio, 3), "volume_leg": bool(volume_leg),
            "oi_change_pct": round(oi_change, 4), "oi_type": oi_type, "oi_leg": bool(oi_leg),
            "pcr": round(pcr_now, 4), "pcr_change": round(pcr_change, 4), "pcr_leg": bool(pcr_leg),
            "legs_true": legs_true,
        }

    # ── output ────────────────────────────────────────────────────────────────
    def _emit(self, event: Dict[str, Any]) -> None:
        legs = ", ".join(name for name, on in (("volume", event["volume_leg"]), ("OI", event["oi_leg"]),
                                               ("PCR", event["pcr_leg"])) if on)
        logger.info(
            f"[BUILDUP SHADOW] {event['index']} {event['direction']}: {event['move_pct']:+.2f}% in "
            f"{event['window_seconds']:.0f}s, volume {event['volume_ratio']:.2f}x, OI {event['oi_change_pct']:+.2f}% "
            f"({event['oi_type']}), PCR {event['pcr']:.2f} ({event['pcr_change']:+.3f}); legs: {legs}; "
            f"peers {len(event['peers_aligned'])}/3"
        )
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, default=str) + "\n")
        except Exception as exc:
            logger.debug(f"Build-up shadow archive failed: {exc}")
