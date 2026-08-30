"""Regime tracker (rtracker) shadow evidence store.

Rtracker records regime evidence and signal outcomes for later calibration. It
does not feed back into live decisions; all writes are append-only study data.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger

from intelligence.entry_policy_evaluation import evaluate_entry_shadow_walk_forward

try:
    from trading.trade_manager import IST
except Exception:  # pragma: no cover - import fallback for isolated scripts
    IST = None


VECTOR_KEYS = (
    "trend_up_prob",
    "trend_down_prob",
    "range_prob",
    "breakout_prob",
    "reversal_prob",
    "volatility_expansion_prob",
    "breadth_stress_prob",
)


# P&L units are part of the archive contract.  The scanner's exit guards consume a
# gross, pre-charge path, while the outcome stored in ``pnl`` is net of the round trip.
# Keeping the names explicit prevents an offline replay from comparing a gross early
# exit with a net booked result (which overstates every replayed exit by the charges).
PNL_BASIS_NET = "NET_AFTER_ROUND_TRIP_COST"
PNL_BASIS_GROSS = "GROSS_BEFORE_ROUND_TRIP_COST"


#: Serialises the event APPEND only. The scanner event loop takes this lock
#: synchronously (_rtracker_record_candidate, called from inside _scan_cycle), so
#: anything held across it stalls the 100ms exit monitor. Hold it for the write and
#: nothing else -- never across a full-archive read or a report build.
_RTRACKER_FILE_LOCK = threading.RLock()

#: Serialises report generation against itself. Deliberately a DIFFERENT lock: the
#: reports live in their own directory, the 30-minute job is the only writer, and the
#: scan loop must never queue behind it.
_RTRACKER_REPORT_LOCK = threading.RLock()


class RegimeTracker:
    """Append-only regime evidence and weekly report generator."""

    def __init__(
        self,
        root_dir: str | Path = "data_store/rtracker",
        *,
        enabled: bool = True,
        max_weeks: int = 8,
        dedupe_size: int = 4096,
        retention_days: int = 0,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.events_dir = self.root_dir / "events"
        self.reports_dir = self.root_dir / "reports"
        self.enabled = bool(enabled)
        self.max_weeks = max(1, int(max_weeks or 8))
        # 0 = keep every day-file forever, which is the behaviour this store shipped
        # with. `max_weeks` only bounds the READ window; it never deleted anything, so
        # the archive grew unbounded (~1.4 MB/day, 83 MB after 61 days). Pruning is
        # opt-in because it destroys recorded evidence.
        self.retention_days = max(0, int(retention_days or 0))
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=max(128, int(dedupe_size or 4096)))
        # Ids already on disk, per day-file, recovered before the first write to
        # that day. _seen alone is per-instance and starts empty, so every process
        # start reopened the day for rewriting: the 2026-08-12 archive holds 482
        # rows over 307 distinct ids — 59 ids written more than once, one of them
        # seven times — across six process starts. Counts drawn from that file feed
        # tuning decisions, so the duplication is not a reporting cosmetic.
        self._day_ids: Dict[str, set[str]] = {}

    @classmethod
    def from_settings(cls, settings: Any) -> "RegimeTracker":
        return cls(
            root_dir=str(getattr(settings, "rtracker_data_dir", "data_store/rtracker") or "data_store/rtracker"),
            enabled=_setting_enabled(getattr(settings, "rtracker_enabled", True), True),
            max_weeks=int(getattr(settings, "rtracker_report_weeks", 8) or 8),
            retention_days=int(getattr(settings, "rtracker_retention_days", 0) or 0),
        )

    def prune_events(self, *, retention_days: Optional[int] = None,
                     as_of: Optional[datetime] = None) -> Dict[str, Any]:
        """Delete day-files older than the retention window.

        No-op when retention_days is 0 (the default). Refuses to prune inside the
        report read window, so a misconfigured retention can never delete evidence
        the 8-week summary still reports on.
        """
        days = self.retention_days if retention_days is None else max(0, int(retention_days or 0))
        if days <= 0 or not self.events_dir.exists():
            return {"pruned": 0, "bytes": 0, "files": []}
        as_of_dt = as_of or _now()
        floor_days = max(days, self.max_weeks * 7)
        cutoff = as_of_dt.date() - timedelta(days=floor_days)
        removed, freed = [], 0
        with _RTRACKER_FILE_LOCK:
            for path in sorted(self.events_dir.glob("*.jsonl")):
                try:
                    if date.fromisoformat(path.stem) >= cutoff:
                        continue
                    size = path.stat().st_size
                    path.unlink()
                except (ValueError, OSError):
                    continue
                removed.append(path.name)
                freed += size
        if removed:
            logger.info(
                f"rtracker pruned {len(removed)} day-file(s) older than {cutoff.isoformat()} "
                f"({freed / 1e6:.1f} MB)"
            )
        return {"pruned": len(removed), "bytes": freed, "files": removed}

    def record_candidate(
        self,
        candidate: Any,
        *,
        decision: str = "",
        reason: str = "",
        source: str = "scanner",
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if candidate is None:
            return False
        event = self._event_from_candidate(
            candidate,
            decision=decision,
            reason=reason,
            source=source,
            extra=extra,
        )
        return self.record_event(event)

    def record_event(self, event: Dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        try:
            event = dict(event or {})
            if not event:
                return False
            ts = _parse_dt(event.get("timestamp")) or _now()
            event["timestamp"] = ts.isoformat()
            event["date"] = event.get("date") or ts.date().isoformat()
            event.setdefault("schema", "rtracker.v1")
            # Lifecycle identity is deliberately additive.  Existing readers can
            # continue to use ``id``/``signal_id`` while new readers can group every
            # funnel row belonging to one materialised trade.  EXIT rows are terminal
            # outcomes, so their id is derived from the lifecycle rather than from the
            # exit timestamp/reason.  This makes the first terminal outcome immutable
            # across repeated calls and process restarts (the durable day-id recovery
            # below then suppresses the same canonical id).
            if not _first_non_empty(event.get("lifecycle_id"), event.get("trade_id")):
                event["lifecycle_id"] = self._lifecycle_id(event)
            else:
                event["lifecycle_id"] = _first_non_empty(
                    event.get("lifecycle_id"), event.get("trade_id")
                )
            is_exit = str(event.get("decision") or "").upper() == "EXIT"
            if is_exit:
                event_id = self._terminal_event_id(str(event["lifecycle_id"]))
            else:
                event_id = str(event.get("id") or self._event_id(event))
            event["id"] = event_id
            # Identity of the SIGNAL, independent of which gate spoke about it, so a
            # day's funnel can be counted per signal rather than per row without
            # re-deriving the key from every consumer.
            event.setdefault("signal_id", self._signal_id(event))
            with _RTRACKER_FILE_LOCK:
                self.events_dir.mkdir(parents=True, exist_ok=True)
                path = self.events_dir / f"{event['date']}.jsonl"
                day_ids = self._day_ids_for(str(event["date"]), path)
                if event_id in self._seen or event_id in day_ids:
                    return False
                self._remember(event_id)
                day_ids.add(event_id)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(_json_safe(event), separators=(",", ":"), sort_keys=True) + "\n")
            return True
        except Exception as exc:
            logger.debug(f"rtracker record skipped: {exc}")
            return False

    def generate_weekly_reports(
        self,
        *,
        weeks: Optional[int] = None,
        as_of: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        weeks = max(1, min(int(weeks or self.max_weeks), 52))
        as_of_dt = as_of or _now()
        pruned = self.prune_events(as_of=as_of_dt)
        events = self.load_events(weeks=weeks, as_of=as_of_dt)
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for event in events:
            event_date = _parse_date(event.get("date")) or (_parse_dt(event.get("timestamp")) or as_of_dt).date()
            iso = event_date.isocalendar()
            grouped[f"{iso.year}-W{iso.week:02d}"].append(event)

        # Build every summary OUTSIDE any lock -- _weekly_summary and
        # _multi_week_summary re-walk all 44k events, and none of that touches shared
        # state. Only the writes are serialised, and against the report lock rather
        # than the append lock the scan loop uses.
        rendered = []
        for week_key in sorted(grouped, reverse=True):
            summary = self._weekly_summary(week_key, grouped[week_key])
            rendered.append((week_key, summary, self._weekly_markdown(summary)))
        eight_week = self._multi_week_summary(events, weeks)
        eight_week_md = self._multi_week_markdown(eight_week)
        # Executable entry-policy evidence is evaluated alongside the normal weekly
        # study, but remains advisory. Build it outside both locks: the evaluator is
        # pure and may scan the archive several times for the configured horizons.
        entry_policy_expectancy = {
            "schema": "entry-policy-expectancy.v1",
            "generated_at": as_of_dt.isoformat(),
            "weeks_requested": weeks,
            "mode": "ADVISORY_ONLY",
            "live_settings_changed": False,
            "horizons": {
                str(horizon): evaluate_entry_shadow_walk_forward(
                    events,
                    horizon_minutes=horizon,
                    min_train_sessions=5,
                    test_sessions=1,
                )
                for horizon in (15, 30, 60)
            },
        }

        with _RTRACKER_REPORT_LOCK:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            week_reports = []
            for week_key, summary, markdown in rendered:
                json_path = self.reports_dir / f"{week_key}.json"
                md_path = self.reports_dir / f"{week_key}.md"
                json_path.write_text(json.dumps(_json_safe(summary), indent=2, sort_keys=True), encoding="utf-8")
                md_path.write_text(markdown, encoding="utf-8")
                week_reports.append(
                    {
                        "week": week_key,
                        "events": summary["summary"]["events"],
                        "json": str(json_path),
                        "markdown": str(md_path),
                    }
                )

            summary_path = self.reports_dir / f"rtracker_{weeks}_week_summary.md"
            summary_json = self.reports_dir / f"rtracker_{weeks}_week_summary.json"
            summary_path.write_text(eight_week_md, encoding="utf-8")
            summary_json.write_text(json.dumps(_json_safe(eight_week), indent=2, sort_keys=True), encoding="utf-8")
            expectancy_json = self.reports_dir / "entry_policy_expectancy.json"
            expectancy_json.write_text(
                json.dumps(_json_safe(entry_policy_expectancy), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return {
            "weeks_requested": weeks,
            "events": len(events),
            "pruned_day_files": pruned.get("pruned", 0),
            "weekly_reports": week_reports,
            "summary_markdown": str(summary_path),
            "summary_json": str(summary_json),
            "entry_policy_expectancy_json": str(expectancy_json),
        }

    def load_events(self, *, weeks: int, as_of: Optional[datetime] = None) -> List[Dict[str, Any]]:
        as_of_dt = as_of or _now()
        start_day = as_of_dt.date() - timedelta(days=max(0, int(weeks or 1) * 7 - 1))
        events: List[Dict[str, Any]] = []
        if not self.events_dir.exists():
            return events
        # Snapshot the file list under the lock, then parse OUTSIDE it. Reading the
        # archive took ~1s against 78MB / 44k events, and holding the append lock for
        # that long stalls the scanner event loop every 30 minutes. Closed day-files
        # are immutable; only today's is appended to, and a torn trailing line is
        # already skipped by the JSONDecodeError guard below.
        with _RTRACKER_FILE_LOCK:
            candidates = sorted(self.events_dir.glob("*.jsonl"))
        for path in candidates:
            try:
                path_day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if path_day < start_day or path_day > as_of_dt.date():
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict):
                            events.append(payload)
            except OSError:
                # A day-file pruned or rotated between the snapshot and the read.
                continue
        events.sort(key=lambda item: str(item.get("timestamp") or ""))
        return events

    def _event_from_candidate(
        self,
        candidate: Any,
        *,
        decision: str,
        reason: str,
        source: str,
        extra: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        signal_ts = (
            _candidate_value(candidate, "signal_timestamp")
            or _candidate_value(candidate, "source_signal_timestamp")
            or _candidate_value(candidate, "signal_time")
        )
        entry_ts = (
            _candidate_value(candidate, "entry_timestamp")
            or _candidate_value(candidate, "entry_time")
            or signal_ts
        )
        exit_ts = (
            _candidate_value(candidate, "exit_timestamp")
            or _candidate_value(candidate, "exit_time")
        )
        event_ts = _parse_dt(exit_ts) or _parse_dt(entry_ts) or _parse_dt(signal_ts) or _now()
        direction = str(_candidate_value(candidate, "direction", "") or "").upper()
        signal_type = str(_candidate_value(candidate, "source_signal_type", "") or "")
        if not signal_type:
            signal_type = "BUY" if direction == "LONG" else ("SELL" if direction == "SHORT" else "")
        status = str(_candidate_value(candidate, "status", "") or "")
        action = str(_candidate_value(candidate, "action", "ENTRY") or "ENTRY").upper()
        final_decision = str(decision or "").upper()
        if not final_decision:
            if action == "EXIT":
                final_decision = "EXIT"
            elif action == "NO_ENTRY" or "REJECT" in status.upper() or "NO ENTRY" in status.upper():
                final_decision = "REJECTED"
            elif _truthy(_candidate_value(candidate, "accepted_by_gate", False)):
                final_decision = "ACCEPTED"
            else:
                final_decision = "CANDIDATE"

        # A candidate can pass the grading gate and THEN be refused further down — the
        # live-readiness gate is the common one. Until 2026-08-18 such a row was still
        # written as ACCEPTED, because accepted_by_gate had already been set and nothing
        # downstream revisited the label. Four rows in the stored book say
        # "Live trade readiness gate blocked it" while claiming ACCEPTED.
        #
        # It is a small count but it corrupts the one number every analysis starts from:
        # 2026-08-17 read as "12 accepted, 11 exits", and that phantom gap was chased as
        # a missing exit when in fact no twelfth trade was ever taken. An accepted count
        # that includes refusals also inflates any acceptance rate computed from it.
        if final_decision == "ACCEPTED":
            blocked_markers = ("blocked it", "gate blocked", "readiness gate")
            haystack = " ".join(
                str(part) for part in (
                    _candidate_value(candidate, "reasons", "") or "",
                    _candidate_value(candidate, "reject_reason", "") or "",
                    reason or "",
                )
            ).lower()
            if any(marker in haystack for marker in blocked_markers):
                final_decision = "REJECTED"

        ctx = _candidate_value(candidate, "market_intelligence_context", {}) or {}
        if not isinstance(ctx, dict):
            ctx = {}
        regime_ctx = ctx.get("regime", {}) if isinstance(ctx.get("regime"), dict) else {}
        session_ctx = ctx.get("session_context", {}) if isinstance(ctx.get("session_context"), dict) else {}
        breadth_ctx = ctx.get("breadth_context", {}) if isinstance(ctx.get("breadth_context"), dict) else {}
        volume_ctx = ctx.get("volume", {}) if isinstance(ctx.get("volume"), dict) else {}
        aggregate_ctx = ctx.get("aggregate", {}) if isinstance(ctx.get("aggregate"), dict) else {}
        market_wide_ctx = _candidate_value(candidate, "market_wide_reversal_context", {}) or {}
        if not isinstance(market_wide_ctx, dict):
            market_wide_ctx = {}

        rejection_reason = reason or _first_non_empty(
            _candidate_value(candidate, "exit_reason", ""),
            _last_reason(_candidate_value(candidate, "reasons", [])),
            status,
        )
        conversion_ratio = _measured_candidate_float(
            candidate,
            "geometry_conversion_ratio",
        )
        if conversion_ratio is None:
            # Candidates created before dual geometry was stamped still expose the
            # contract conversion through multiplier; retain that measured value
            # rather than dropping the option cross-geometry dimension on replay.
            conversion_ratio = _measured_candidate_float(candidate, "multiplier")
        vector = dict(regime_ctx.get("conviction_vector", {}) or {})
        event = {
            "timestamp": event_ts.isoformat(),
            "instrument": str(_candidate_value(candidate, "instrument", "") or "").split()[0],
            "timeframe": str(_candidate_value(candidate, "timeframe", "") or ""),
            "direction": direction,
            "signal_type": signal_type,
            "decision": final_decision,
            "status": status,
            "reason": str(rejection_reason or ""),
            "source": source,
            "source_lane": str(_candidate_value(candidate, "source_lane", "") or ""),
            "grade": str(_candidate_value(candidate, "grade", "") or ""),
            "confidence": _safe_float(_candidate_value(candidate, "confidence", 0.0)),
            "price": _safe_float(
                _candidate_value(
                    candidate,
                    "price",
                    _candidate_value(candidate, "entry_price", 0.0),
                )
            ),
            "stop": _safe_float(
                _candidate_value(
                    candidate,
                    "stop",
                    _candidate_value(candidate, "current_stop", 0.0),
                )
            ),
            "target": _safe_float(_candidate_value(candidate, "target", 0.0)),
            "current_price": _safe_float(_candidate_value(candidate, "current_price", 0.0)),
            "pnl": _safe_float(_candidate_value(candidate, "pnl", 0.0)),
            "peak_pnl": _safe_float(_candidate_value(candidate, "peak_pnl", 0.0)),
            "max_drawdown": _safe_float(_candidate_value(candidate, "max_drawdown", 0.0)),
            # (seconds_since_entry, pnl) samples. peak_pnl and max_drawdown are maxima,
            # so they cannot say whether the trough came before or after the peak — and
            # that ordering is the whole question when sizing a trail. Empty for trades
            # opened before 2026-08-11 and whenever pnl_path_logging_enabled is off.
            "pnl_path": _pnl_path_rows(_candidate_value(candidate, "pnl_path", None)),
            # The intel state at the moment of acceptance: volume ratio, PCR, cumulative
            # OI, order flow, breadth, VWAP position, greeks and VIX percentile. The
            # engine computed all of it and the record kept almost none — volume_ratio
            # survived on 2% of accepted rows and PCR/OI on none — so "does the intel
            # predict which trades travel?" was unanswerable from this file.
            "entry_intel": _entry_intel_block(_candidate_value(candidate, "entry_intel", None)),
            # The readings the gate judged THIS row on, for every decision — not
            # only accepted ones. Bounded and truncated by the same block helper.
            "decision_intel": _entry_intel_block(_candidate_value(candidate, "decision_intel", None)),
            "exit_price": _safe_float(_candidate_value(candidate, "exit_price", 0.0)),
            "exit_reason": str(_candidate_value(candidate, "exit_reason", "") or ""),
            "signal_timestamp": _iso_or_empty(signal_ts),
            "entry_timestamp": _iso_or_empty(entry_ts),
            "exit_timestamp": _iso_or_empty(exit_ts),
            # Traded contract identity (2026-07-24): without this an offline replay can
            # only GUESS the contract by premium proximity, which mismatched ~60% of
            # trades (nearest stored strike differed 15-77%). Recording it makes every
            # future session exactly replayable against its real premium candles.
            # Which intelligence context actually graded this candidate. The regime block
            # below records the candidate's own context, which is NOT necessarily the one
            # grading used — without this the per-timeframe grading flag cannot be A/B'd
            # from archived sessions.
            "grading": {
                "timeframe": str(_candidate_value(candidate, "graded_timeframe", "") or ""),
                # Falls back to the position's own intelligence_score. `graded_intel_score`
                # is set on the CANDIDATE at grading time; an EXIT event is built from the
                # Trade, which never carries that attribute, so the default fired and every
                # exit recorded intel_score 0.0 — including the 13 of 14 lifecycles on
                # 2026-08-19 whose ACCEPTED event held the real value (-0.3935 .. +0.2667).
                #
                # Decisions were never affected: the ACCEPTED record is what grading used
                # and it was always correct. But an archive that says "intelligence was
                # zero" on every exit is worse than one that says nothing, because it
                # invites exactly the wrong conclusion — that the 30%-weight intelligence
                # pillar is dead. It is not; reading exits alone made it look that way.
                "intel_score": _safe_float(
                    _candidate_value(
                        candidate,
                        "graded_intel_score",
                        _candidate_value(candidate, "intelligence_score", 0.0),
                    )
                ),
                "regime": str(_candidate_value(candidate, "graded_regime", "") or ""),
                # What each pillar actually contributed. The three collapse into one
                # confidence before anything is recorded, so without this a rejected B+
                # and a taken A+ are indistinguishable beyond the score itself.
                "pillars": _candidate_value(candidate, "grade_pillars", {}) or {},
            },
            # Strategy/data and quote provenance are separate from the score.  This
            # makes outcomes attributable to the exact deployed logic and prevents
            # an inherited entry quote flag from being mistaken for EXIT evidence.
            "strategy": {
                "revision": str(_candidate_value(candidate, "strategy_revision", "") or ""),
                "direction_mode": str(
                    _candidate_value(candidate, "strategy_direction_mode", "") or ""
                ),
                "data_provenance": str(
                    _candidate_value(candidate, "data_provenance", "") or ""
                ),
            },
            "quote": {
                "decision_verified": _optional_bool(candidate, "quote_verified"),
                "decision_source": str(
                    _candidate_value(candidate, "quote_source", "") or ""
                ),
                "entry_verified": _optional_bool(candidate, "entry_quote_verified"),
                "entry_source": str(
                    _candidate_value(candidate, "entry_quote_source", "") or ""
                ),
                # Only explicit exit-time fields qualify.  quote_verified belongs
                # to the decision/current holder and is intentionally not reused.
                "exit_verified": _optional_bool(candidate, "exit_quote_verified"),
                "exit_source": str(
                    _candidate_value(candidate, "exit_quote_source", "") or ""
                ),
                "execution_bid": _measured_candidate_float(candidate, "execution_bid_price"),
                "execution_ask": _measured_candidate_float(candidate, "execution_ask_price"),
                "execution_touch_source": str(
                    _candidate_value(candidate, "execution_touch_source", "") or ""
                ),
                "execution_touch_age_seconds": _measured_candidate_float(
                    candidate, "execution_touch_age_seconds"
                ),
                "execution_touch_usable": _optional_bool(
                    candidate, "execution_touch_usable"
                ),
                "execution_touch_reason": str(
                    _candidate_value(candidate, "execution_touch_reason", "") or ""
                ),
                "actual_fill_price": _measured_candidate_float(
                    candidate, "actual_fill_price"
                ),
            },
            "contract": {
                "trading_symbol": str(_candidate_value(candidate, "trading_symbol", "") or ""),
                "symbol_token": str(_candidate_value(candidate, "symbol_token", "") or ""),
                "inst_type": str(_candidate_value(candidate, "inst_type", "") or ""),
                "option_type": str(_candidate_value(candidate, "option_type", "") or ""),
                "atm_strike": _safe_float(_candidate_value(candidate, "atm_strike", 0.0)),
                "lots": _safe_int(_candidate_value(candidate, "lots", 0)),
                "lot_size": _safe_int(_candidate_value(candidate, "lot_size", 0)),
            },
            "geometry": {
                "schema": str(_candidate_value(candidate, "geometry_schema", "") or ""),
                "verified": _optional_bool(candidate, "geometry_verified"),
                "source": str(_candidate_value(candidate, "geometry_source", "") or ""),
                "observed_at": str(_candidate_value(candidate, "geometry_observed_at", "") or ""),
                "underlying_entry": _measured_candidate_float(candidate, "underlying_entry"),
                "underlying_stop": _measured_candidate_float(candidate, "underlying_stop"),
                "underlying_target": _measured_candidate_float(candidate, "underlying_target"),
                "premium_entry": _measured_candidate_float(candidate, "premium_entry"),
                "premium_stop": _measured_candidate_float(candidate, "premium_stop"),
                "premium_target": _measured_candidate_float(candidate, "premium_target"),
                "conversion_ratio": conversion_ratio,
            },
            "expected_move": (
                dict(_candidate_value(candidate, "expected_move_forecast", {}) or {})
                if isinstance(_candidate_value(candidate, "expected_move_forecast", {}), dict)
                and bool(_candidate_value(candidate, "expected_move_forecast", {}))
                else {
                    "expected_favorable_move_pct": _measured_candidate_float(candidate, "expected_move_pct"),
                    "floor_pct": _measured_candidate_float(candidate, "expected_move_floor_pct"),
                    "uncertainty_pct": _measured_candidate_float(candidate, "expected_move_uncertainty_pct"),
                    "probability_meeting_floor": _measured_candidate_float(candidate, "expected_move_probability"),
                    "sample_count": _measured_candidate_float(candidate, "expected_move_sample_count"),
                    "horizon_minutes": _measured_candidate_float(candidate, "expected_move_horizon_minutes"),
                    "generated_at": str(_candidate_value(candidate, "expected_move_generated_at", "") or ""),
                    "data_as_of": str(_candidate_value(candidate, "expected_move_data_as_of", "") or ""),
                    "model_version": str(_candidate_value(candidate, "expected_move_model_version", "") or ""),
                    "source": str(_candidate_value(candidate, "expected_move_source", "") or ""),
                }
            ),
            "entry_policy": (
                dict(_candidate_value(candidate, "entry_policy_verdict", {}) or {})
                if isinstance(_candidate_value(candidate, "entry_policy_verdict", {}), dict)
                and bool(_candidate_value(candidate, "entry_policy_verdict", {}))
                else {
                    "allowed": _optional_bool(candidate, "entry_policy_allowed"),
                    "phase": str(_candidate_value(candidate, "entry_policy_phase", "") or ""),
                    "checked_at": str(_candidate_value(candidate, "entry_policy_checked_at", "") or ""),
                    "reason": str(_candidate_value(candidate, "entry_policy_reason", "") or ""),
                    "actual_rr": _measured_candidate_float(candidate, "entry_policy_actual_rr"),
                    "underlying_stop_pct": _measured_candidate_float(candidate, "entry_policy_underlying_stop_pct"),
                    "policy_version": str(_candidate_value(candidate, "entry_policy_version", "") or ""),
                }
            ),
            "regime": {
                "raw_regime": str(regime_ctx.get("raw_regime") or ""),
                "guarded_regime": str(regime_ctx.get("guarded_regime") or ""),
                "smoothed_regime": str(regime_ctx.get("smoothed_regime") or regime_ctx.get("regime") or _candidate_value(candidate, "market_regime", "") or ""),
                "hard_label": str(regime_ctx.get("regime") or _candidate_value(candidate, "market_regime", "") or ""),
                "confidence": _measured_float(regime_ctx, "confidence"),
                "conviction_label": str(regime_ctx.get("conviction_label") or ""),
                "conviction_summary": str(regime_ctx.get("conviction_summary") or ""),
                "conviction_vector": {key: _safe_float(vector.get(key, 0.0)) for key in VECTOR_KEYS},
                "adx": _safe_float(regime_ctx.get("adx", _candidate_value(candidate, "adx_value", 0.0))),
                "kaufman_er": _measured_float(regime_ctx, "kaufman_er"),
                "atr_pct": _measured_float(regime_ctx, "atr_pct"),
                "bb_width": _measured_float(regime_ctx, "bb_width"),
                "range_day_trend_guard": bool(regime_ctx.get("range_day_trend_guard", False)),
                "range_day_trend_guard_reason": str(regime_ctx.get("range_day_trend_guard_reason") or ""),
                # Index SPOT at decision time (2026-08-17). Nothing on an event recorded
                # the underlying level, so the day's actual index range could not be
                # measured from the tracker at all — a post-session review could only
                # read back the engine's OWN regime labels, never check them against what
                # the index really did. Four indices move differently on the same day, so
                # this has to be per event rather than a session summary.
                "spot": _safe_float(
                    regime_ctx.get("spot",
                        _candidate_value(candidate, "spot_price",
                            _candidate_value(candidate, "index_spot", 0.0)))
                ),
            },
            "session": {
                "day_type": str(session_ctx.get("day_type") or ""),
                "bias": str(session_ctx.get("bias") or ""),
                "confidence": _safe_float(session_ctx.get("confidence", 0.0)),
                "vwap_cross_count": _safe_int(session_ctx.get("vwap_cross_count", 0)),
                "close_above_vwap_pct": _safe_float(session_ctx.get("close_above_vwap_pct", 0.0)),
            },
            "volume": {
                "signal": str(volume_ctx.get("signal") or ""),
                "available": (
                    bool(_truthy(volume_ctx.get("available")))
                    if volume_ctx.get("available") is not None
                    else (
                        bool(_truthy(volume_ctx.get("volume_available")))
                        if volume_ctx.get("volume_available") is not None
                        else _optional_bool(candidate, "volume_available")
                    )
                ),
                "source": str(
                    volume_ctx.get("source")
                    or volume_ctx.get("volume_source")
                    or _candidate_value(candidate, "volume_source", "")
                    or ""
                ),
                "basis": str(
                    volume_ctx.get("basis")
                    or volume_ctx.get("volume_basis")
                    or volume_ctx.get("metric_kind")
                    or _candidate_value(candidate, "volume_basis", "")
                    or ""
                ),
                "unit": str(
                    volume_ctx.get("unit")
                    or volume_ctx.get("volume_unit")
                    or _candidate_value(candidate, "volume_unit", "")
                    or ""
                ),
                "comparable": (
                    bool(_truthy(volume_ctx.get("comparable")))
                    if volume_ctx.get("comparable") is not None
                    else (
                        bool(_truthy(volume_ctx.get("volume_comparable")))
                        if volume_ctx.get("volume_comparable") is not None
                        else _optional_bool(candidate, "volume_comparable")
                    )
                ),
                "unavailable_reason": str(
                    volume_ctx.get("unavailable_reason")
                    or volume_ctx.get("volume_unavailable_reason")
                    or _candidate_value(candidate, "volume_unavailable_reason", "")
                    or ""
                ),
                "volume_ratio": _measured_float(volume_ctx, "volume_ratio", _candidate_value(candidate, "volume_ratio", None)),
                "current_volume": _measured_float(volume_ctx, "current_volume"),
                "buy_sell_ratio": _measured_float(volume_ctx, "buy_sell_ratio"),
            },
            "breadth": {
                "bias": str(breadth_ctx.get("bias") or ""),
                "aligned_count": _measured_int(breadth_ctx, "aligned_count"),
            },
            "market_wide_reversal": {
                "entry": bool(_candidate_value(candidate, "market_wide_reversal_entry", False)),
                "core_votes": _safe_int(market_wide_ctx.get("core_votes", 0)),
                "total_votes": _safe_int(market_wide_ctx.get("total_votes", 0)),
                "reason": str(market_wide_ctx.get("reason") or ""),
            },
            "aggregate": {
                # Legacy -100..+100 when the aggregate carries it, and the canonical
                # -1..+1 candidate attribute otherwise -- two scales under one key, so
                # the archive was not comparable across rows. Record the canonical
                # value under its own key; `score` keeps its shape for old readers.
                "score": _safe_float(aggregate_ctx.get("score", _candidate_value(candidate, "intelligence_score", 0.0))),
                # Absent stays absent: 0.0 is a legitimate aggregate score, so a
                # fabricated one is indistinguishable from a genuinely neutral book.
                "score_norm": (
                    _measured_float(aggregate_ctx, "score_norm")
                    if aggregate_ctx.get("score_norm") is not None
                    else (
                        _safe_float(aggregate_ctx.get("score")) / 100.0
                        if aggregate_ctx.get("score") is not None
                        else _measured_float(
                            {}, "", _candidate_value(candidate, "intelligence_score", None)
                        )
                    )
                ),
                "signal": str(aggregate_ctx.get("signal") or ""),
                "regime_score_multiplier": _measured_float(aggregate_ctx, "regime_score_multiplier"),
            },
            "flags": {
                "accepted_by_gate": bool(_candidate_value(candidate, "accepted_by_gate", False)),
                "volume_spike_entry": bool(_candidate_value(candidate, "volume_spike_entry", False)),
                "late_session_reclaim_entry": bool(_candidate_value(candidate, "late_session_reclaim_entry", False)),
                "fifteen_min_choppy_scalp_entry": bool(_candidate_value(candidate, "fifteen_min_choppy_scalp_entry", False)),
                "scalp_lock_mode": bool(_candidate_value(candidate, "scalp_lock_mode", False)),
                "power_move": bool(_candidate_value(candidate, "power_move", False)),
                "risk_geometry_source": str(
                    _candidate_value(candidate, "risk_geometry_source", "") or ""
                ),
            },
        }
        # Prefer an explicitly assigned lifecycle on the candidate.  ``trade_id`` is
        # accepted as a compatibility bridge for Trade-like objects until callers can
        # populate lifecycle_id directly.  The fallback is deterministic and uses the
        # entry/signal clock plus the event identity fields, never the exit clock.
        explicit_lifecycle = _first_non_empty(
            _candidate_value(candidate, "lifecycle_id", ""),
            _candidate_value(candidate, "trade_id", ""),
        )
        event["lifecycle_id"] = explicit_lifecycle or self._lifecycle_id(event)
        if final_decision == "EXIT":
            # Additive fields keep rtracker.v1 readers compatible while making every
            # newly-written exit self-describing.  TradeCandidate paths are gross;
            # Trade paths are net.  The discriminator below uses the fields each holder
            # actually exposes and honours an explicit producer override.
            event.update(
                {
                    "pnl_basis": _candidate_pnl_basis(candidate),
                    "pnl_path_basis": _candidate_pnl_path_basis(candidate),
                    "round_trip_cost": _candidate_round_trip_cost(candidate),
                }
            )
            exit_provenance = _exit_provenance(candidate)
            if exit_provenance:
                event["exit_provenance"] = exit_provenance
        if extra:
            event["extra"] = _compact(extra)
        return event

    def _remember(self, event_id: str) -> None:
        if len(self._seen_order) == self._seen_order.maxlen:
            old = self._seen_order.popleft()
            self._seen.discard(old)
        self._seen.add(event_id)
        self._seen_order.append(event_id)

    def _day_ids_for(self, day: str, path: Path) -> set[str]:
        """Ids already recorded for a day, recovered from its file exactly once.

        Best effort: a damaged archive must never silence the tracker, so an
        unreadable line or file yields an empty set and recording continues.
        """
        known = self._day_ids.get(day)
        if known is not None:
            return known
        known = set()
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row_id = json.loads(line).get("id")
                        except Exception:
                            continue
                        if row_id:
                            known.add(str(row_id))
        except Exception as exc:
            logger.debug(f"rtracker day-id recovery skipped for {day}: {exc}")
        # Only the current and previous day stay resident; older files are never
        # appended to again.
        if len(self._day_ids) >= 2:
            self._day_ids.pop(sorted(self._day_ids)[0], None)
        self._day_ids[day] = known
        return known

    @staticmethod
    def _signal_id(event: Dict[str, Any]) -> str:
        """Stable identity for the SIGNAL a row is about.

        Keyed on the signal's own clock, not the row's, so every gate that speaks
        about one signal shares an id. Falls back to the row timestamp when a
        signal clock is absent: identity is then unknowable, and recording twice is
        the safe error where merging two real signals is not.
        """
        key = "|".join(
            str(event.get(name, "") or "")
            for name in (
                "date",
                "instrument",
                "timeframe",
                "direction",
                "signal_timestamp",
            )
        )
        if not str(event.get("signal_timestamp") or ""):
            key = f"{key}|{event.get('timestamp', '')}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    def _event_id(self, event: Dict[str, Any]) -> str:
        key = "|".join(
            str(event.get(name, "") or "")
            for name in (
                "date",
                "timestamp",
                "instrument",
                "timeframe",
                "direction",
                "decision",
                "status",
                "reason",
                "source",
            )
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    @staticmethod
    def _lifecycle_id(event: Dict[str, Any]) -> str:
        """Return the stable identity shared by a signal's lifecycle rows.

        Explicit lifecycle/trade ids are preserved by ``record_event`` callers.  For
        legacy candidates and direct event producers, the fallback is deterministic
        from the event date, entry/signal clock, and market identity.  If no entry or
        signal clock exists, the normalised row timestamp is included as a last-resort
        guard so unrelated ad-hoc events are not merged accidentally.
        """
        explicit = _first_non_empty(event.get("lifecycle_id"), event.get("trade_id"))
        if explicit:
            return explicit
        date_text = str(event.get("date") or "")
        clock = _first_non_empty(
            _iso_or_empty(event.get("entry_timestamp")),
            _iso_or_empty(event.get("signal_timestamp")),
        )
        if not clock:
            clock = _iso_or_empty(event.get("timestamp")) or str(event.get("timestamp") or "")
        contract = event.get("contract") if isinstance(event.get("contract"), dict) else {}
        key = "|".join(
            (
                date_text,
                clock,
                str(event.get("instrument", "") or ""),
                str(event.get("timeframe", "") or ""),
                str(event.get("direction", "") or ""),
                # Multiple accepted lanes/contracts can legitimately share a source
                # candle.  They are separate positions and must not lose one terminal
                # outcome merely because the older coarse audit key grouped them.
                str(event.get("source_lane", "") or ""),
                str((contract or {}).get("trading_symbol", "") or ""),
            )
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rtracker-lifecycle|{key}"))

    @staticmethod
    def _terminal_event_id(lifecycle_id: str) -> str:
        """Stable canonical id for the one terminal EXIT outcome of a lifecycle."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rtracker-terminal|{lifecycle_id}|EXIT"))

    def _weekly_summary(self, week_key: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_instrument: Dict[str, Dict[str, Any]] = defaultdict(_blank_stats)
        regime_counts = Counter()
        conviction_counts = Counter()
        reason_counts = Counter()
        decision_counts = Counter()
        vector_totals = {key: 0.0 for key in VECTOR_KEYS}
        vector_count = 0
        pnl_total = 0.0
        pnl_count = 0
        wins = 0

        for event in events:
            inst = str(event.get("instrument") or "UNKNOWN")
            decision = str(event.get("decision") or "UNKNOWN").upper()
            decision_counts[decision] += 1
            stats = by_instrument[inst]
            stats["events"] += 1
            stats["decisions"][decision] += 1

            regime = event.get("regime", {}) if isinstance(event.get("regime"), dict) else {}
            label = str(regime.get("smoothed_regime") or regime.get("hard_label") or "UNKNOWN")
            regime_counts[label] += 1
            stats["regimes"][label] += 1
            conviction_label = str(regime.get("conviction_label") or "UNKNOWN")
            conviction_counts[conviction_label] += 1
            stats["convictions"][conviction_label] += 1
            vector = regime.get("conviction_vector", {}) if isinstance(regime.get("conviction_vector"), dict) else {}
            if vector:
                vector_count += 1
                for key in VECTOR_KEYS:
                    value = _safe_float(vector.get(key, 0.0))
                    vector_totals[key] += value
                    stats["vector_totals"][key] += value
                stats["vector_count"] += 1

            reason = str(event.get("reason") or "")
            if decision in {"REJECTED", "NO_ENTRY", "MISSED"} and reason:
                reason_counts[reason] += 1
                stats["reject_reasons"][reason] += 1

            pnl = _safe_float(event.get("pnl", 0.0))
            if decision in {"EXIT", "CLOSED", "OUTCOME"} or abs(pnl) > 0:
                pnl_total += pnl
                pnl_count += 1
                wins += int(pnl > 0)
                stats["pnl_total"] += pnl
                stats["pnl_count"] += 1
                stats["wins"] += int(pnl > 0)

        instrument_payload = {}
        for inst, stats in by_instrument.items():
            count = max(1, int(stats["vector_count"]))
            instrument_payload[inst] = {
                "events": stats["events"],
                "decisions": dict(stats["decisions"]),
                "top_regimes": dict(stats["regimes"].most_common(5)),
                "top_convictions": dict(stats["convictions"].most_common(5)),
                "avg_vector": {key: round(stats["vector_totals"][key] / count, 3) for key in VECTOR_KEYS},
                "pnl_total": round(stats["pnl_total"], 2),
                "outcomes": stats["pnl_count"],
                "win_rate": round(stats["wins"] / stats["pnl_count"], 3) if stats["pnl_count"] else None,
                "top_reject_reasons": dict(stats["reject_reasons"].most_common(5)),
            }

        avg_vector = {
            key: round(vector_totals[key] / max(1, vector_count), 3)
            for key in VECTOR_KEYS
        }
        notes = self._behavior_notes(avg_vector, regime_counts, conviction_counts)
        return {
            "week": week_key,
            "generated_at": _now().isoformat(),
            "summary": {
                "events": len(events),
                "decisions": dict(decision_counts),
                "top_regimes": dict(regime_counts.most_common(8)),
                "top_convictions": dict(conviction_counts.most_common(8)),
                "avg_vector": avg_vector,
                "pnl_total": round(pnl_total, 2),
                "outcomes": pnl_count,
                "win_rate": round(wins / pnl_count, 3) if pnl_count else None,
                "top_reject_reasons": dict(reason_counts.most_common(10)),
                "market_behavior_notes": notes,
            },
            "by_instrument": instrument_payload,
        }

    def _multi_week_summary(self, events: List[Dict[str, Any]], weeks: int) -> Dict[str, Any]:
        summary = self._weekly_summary(f"last-{weeks}-weeks", events)
        summary["window"] = f"Last {weeks} week(s)"
        summary["purpose"] = (
            "Shadow study of how NIFTY, BANKNIFTY, SENSEX and MIDCPNIFTY regimes "
            "behave before tuning live gates."
        )
        return summary

    @staticmethod
    def _behavior_notes(avg_vector: Dict[str, float], regime_counts: Counter, conviction_counts: Counter) -> List[str]:
        notes = []
        trend = max(avg_vector.get("trend_up_prob", 0.0), avg_vector.get("trend_down_prob", 0.0))
        range_prob = avg_vector.get("range_prob", 0.0)
        vol = avg_vector.get("volatility_expansion_prob", 0.0)
        breadth = avg_vector.get("breadth_stress_prob", 0.0)
        if range_prob >= trend + 0.12:
            notes.append("Range/chop evidence dominated trend evidence in this sample.")
        elif trend >= range_prob + 0.12:
            notes.append("Directional trend evidence dominated range evidence in this sample.")
        else:
            notes.append("Trend and range evidence were mixed; avoid tuning from a single label.")
        if vol >= 0.45:
            notes.append("Volatility expansion was material; fixed baseline stops should be reviewed against outcomes.")
        if breadth >= 0.35:
            notes.append("Breadth stress/divergence was visible; cross-index confirmation deserves weight in tuning.")
        if regime_counts:
            notes.append(f"Most common hard regime: {regime_counts.most_common(1)[0][0]}.")
        if conviction_counts:
            notes.append(f"Most common conviction leader: {conviction_counts.most_common(1)[0][0]}.")
        return notes

    @staticmethod
    def _weekly_markdown(report: Dict[str, Any]) -> str:
        summary = report.get("summary", {})
        lines = [
            f"# RTracker Weekly Regime Study - {report.get('week', '')}",
            "",
            f"- Events: {summary.get('events', 0)}",
            f"- Decisions: {summary.get('decisions', {})}",
            f"- Outcomes: {summary.get('outcomes', 0)}, win rate: {summary.get('win_rate')}",
            f"- PnL total: Rs.{summary.get('pnl_total', 0)}",
            f"- Average vector: {summary.get('avg_vector', {})}",
            "",
            "## Market Behavior Notes",
        ]
        for note in summary.get("market_behavior_notes", []) or []:
            lines.append(f"- {note}")
        lines.extend(["", "## By Instrument"])
        for inst, stats in (report.get("by_instrument", {}) or {}).items():
            lines.extend(
                [
                    f"### {inst}",
                    f"- Events: {stats.get('events', 0)}",
                    f"- Decisions: {stats.get('decisions', {})}",
                    f"- Top regimes: {stats.get('top_regimes', {})}",
                    f"- Top convictions: {stats.get('top_convictions', {})}",
                    f"- Avg vector: {stats.get('avg_vector', {})}",
                    f"- Outcomes: {stats.get('outcomes', 0)}, win rate: {stats.get('win_rate')}, PnL Rs.{stats.get('pnl_total', 0)}",
                    f"- Top reject reasons: {stats.get('top_reject_reasons', {})}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _multi_week_markdown(report: Dict[str, Any]) -> str:
        lines = [
            f"# RTracker {report.get('window', '')} Regime Study",
            "",
            report.get("purpose", ""),
            "",
            RegimeTracker._weekly_markdown(report).replace("# RTracker Weekly Regime Study - ", "## Aggregate - ", 1),
        ]
        return "\n".join(lines).rstrip() + "\n"


def _blank_stats() -> Dict[str, Any]:
    return {
        "events": 0,
        "decisions": Counter(),
        "regimes": Counter(),
        "convictions": Counter(),
        "reject_reasons": Counter(),
        "vector_totals": {key: 0.0 for key in VECTOR_KEYS},
        "vector_count": 0,
        "pnl_total": 0.0,
        "pnl_count": 0,
        "wins": 0,
    }


def _setting_enabled(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(value)


def _now() -> datetime:
    if IST is not None:
        return datetime.now(IST).replace(tzinfo=None)
    return datetime.now()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except Exception:
            return None
    if getattr(parsed, "tzinfo", None) is not None and IST is not None:
        parsed = parsed.astimezone(IST).replace(tzinfo=None)
    return parsed


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _candidate_value(candidate: Any, name: str, default: Any = None) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _measured_int(mapping: Any, key: str) -> Optional[int]:
    """Integer counterpart of _measured_float. Absent stays absent."""
    value = _measured_float(mapping, key)
    return None if value is None else int(value)


def _measured_float(mapping: Any, key: str, fallback: Any = None) -> Optional[float]:
    """A metric's value, or None when it was never measured.

    Absent is not zero. `_safe_float(ctx.get(key, 0.0))` writes a 0.0 nobody
    measured, and every threshold in this system is argued from this archive: on
    2026-08-12 the four worst trades recorded `TRENDING_UP, adx 47-54,
    confidence 0.0`, which reads as "traded on no conviction" — but
    RegimeDetector's floor is 40 and a trending label computes min(95, adx+30),
    so the real values were 77-84. The zero was invented here, because
    `hard_label` has a candidate fallback and `confidence` does not.

    A genuinely measured 0.0 still records as 0.0; only the missing key is absent.
    """
    value = mapping.get(key) if isinstance(mapping, dict) and key in mapping else None
    if value is None:
        # A caller-supplied fallback is a MEASUREMENT from elsewhere (the candidate
        # carries its own reading), not a default standing in for one. None still
        # means nobody measured it.
        value = fallback
    if value is None:
        return None
    return _number_or_none(value)


def _entry_intel_block(value: Any) -> dict:
    """Normalise the entry-time intel snapshot into JSON-safe scalars.

    Tolerant like everything else in this writer: a malformed field costs that field,
    never the record. Bounded so a runaway payload cannot bloat the event log.
    """
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for key, raw in value.items():
        if len(out) >= 40:
            break
        name = str(key)[:48]
        if isinstance(raw, bool):
            out[name] = raw
        elif isinstance(raw, (int, float)):
            try:
                f = float(raw)
            except (TypeError, ValueError):
                continue
            if f == f and abs(f) != float("inf"):
                out[name] = round(f, 6)
        elif isinstance(raw, str):
            out[name] = raw[:32]
    return out


def _pnl_path_rows(value: Any) -> list:
    """Normalise a trade's pnl_path into JSON-safe [seconds, pnl] pairs.

    Tolerant by design: this runs inside the event writer, and a malformed sample must
    cost that sample rather than the whole record.
    """
    rows = []
    for row in (value or []):
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            rows.append([round(float(row[0]), 2), round(float(row[1]), 2)])
        except (TypeError, ValueError):
            continue
    return rows


def _candidate_has(candidate: Any, name: str) -> bool:
    if isinstance(candidate, dict):
        return name in candidate
    return hasattr(candidate, name)


def _optional_bool(candidate: Any, name: str) -> Optional[bool]:
    """Return a recorded boolean without turning absence into False."""
    if not _candidate_has(candidate, name):
        return None
    value = _candidate_value(candidate, name, None)
    if value is None:
        return None
    return bool(_truthy(value))


def _measured_candidate_float(candidate: Any, name: str) -> Optional[float]:
    """Return a finite candidate measurement, preserving missing as None."""
    if not _candidate_has(candidate, name):
        return None
    return _number_or_none(_candidate_value(candidate, name, None))


def _exit_provenance(candidate: Any) -> dict:
    """Return optional runner/stop provenance without inventing missing state."""
    runner_present = _candidate_has(candidate, "runner_active") or _candidate_has(candidate, "runner_mode")
    stop_present = _candidate_has(candidate, "stop_provenance")
    activation_present = _candidate_has(candidate, "runner_activation_timestamp")
    locked_stop_present = _candidate_has(candidate, "runner_stop_at_exit") or _candidate_has(candidate, "runner_stop")
    basis_present = _candidate_has(candidate, "runner_stop_basis")
    if not any((runner_present, stop_present, activation_present, locked_stop_present, basis_present)):
        return {}

    active_value = _candidate_value(candidate, "runner_active", None)
    if active_value is None:
        active_value = _candidate_value(candidate, "runner_mode", None)
    provenance = {
        "runner_active": None if active_value is None else bool(_truthy(active_value)),
        "stop_provenance": str(_candidate_value(candidate, "stop_provenance", "") or ""),
    }
    if basis_present:
        provenance["runner_stop_basis"] = str(
            _candidate_value(candidate, "runner_stop_basis", "") or ""
        )
    if activation_present:
        provenance["runner_activation_timestamp"] = _iso_or_empty(
            _candidate_value(candidate, "runner_activation_timestamp", None)
        )
    if locked_stop_present:
        provenance["runner_stop_at_exit"] = _safe_float(
            _candidate_value(
                candidate,
                "runner_stop_at_exit",
                _candidate_value(candidate, "runner_stop", 0.0),
            )
        )
    return provenance


def _number_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    return number


def _canonical_pnl_basis(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip().upper()
    aliases = {
        "NET": PNL_BASIS_NET,
        "NET_AFTER_COST": PNL_BASIS_NET,
        "NET_AFTER_CHARGES": PNL_BASIS_NET,
        PNL_BASIS_NET: PNL_BASIS_NET,
        "GROSS": PNL_BASIS_GROSS,
        "GROSS_BEFORE_COST": PNL_BASIS_GROSS,
        "GROSS_BEFORE_CHARGES": PNL_BASIS_GROSS,
        PNL_BASIS_GROSS: PNL_BASIS_GROSS,
    }
    return aliases.get(text, default)


def _candidate_pnl_basis(candidate: Any) -> str:
    explicit = _canonical_pnl_basis(_candidate_value(candidate, "pnl_basis", None))
    # Both objects currently recorded on an EXIT hold net booked P&L in ``pnl``:
    # TradeCandidate uses Scanner._candidate_net_pnl and Trade uses its accounting
    # finalizer.  An explicit producer field wins if another source is added later.
    return explicit or PNL_BASIS_NET


def _candidate_pnl_path_basis(candidate: Any) -> str:
    explicit = _canonical_pnl_basis(
        _candidate_value(candidate, "pnl_path_basis", None)
    )
    if explicit:
        return explicit
    # TradeCandidate exposes ``price`` and is sampled from _candidate_gross_pnl.
    # Trade exposes ``entry_price`` (not ``price``) and sample_pnl_path reads its net
    # ``pnl`` attribute.  RTracker historically records TradeCandidate, so gross is the
    # safe legacy fallback if a minimal test double exposes neither discriminator.
    if _candidate_has(candidate, "price"):
        return PNL_BASIS_GROSS
    if _candidate_has(candidate, "entry_price"):
        return PNL_BASIS_NET
    return PNL_BASIS_GROSS


def _candidate_quantity(candidate: Any) -> float:
    lots = _number_or_none(_candidate_value(candidate, "lots", None))
    lot_size = _number_or_none(_candidate_value(candidate, "lot_size", None))
    if lots is not None and lot_size is not None and lots > 0 and lot_size > 0:
        return lots * lot_size
    for name in ("broker_quantity", "quantity", "qty"):
        quantity = _number_or_none(_candidate_value(candidate, name, None))
        if quantity is not None and quantity > 0:
            return quantity
    return 0.0


def _gross_outcome_pnl(
    *,
    entry_price: Any,
    exit_price: Any,
    quantity: Any,
    inst_type: Any,
    direction: Any,
    multiplier: Any = 1.0,
) -> Optional[float]:
    entry = _number_or_none(entry_price)
    exit_ = _number_or_none(exit_price)
    qty = _number_or_none(quantity)
    mult = _number_or_none(multiplier)
    if entry is None or exit_ is None or qty is None or entry <= 0 or exit_ <= 0 or qty <= 0:
        return None
    mult = mult if mult is not None and mult > 0 else 1.0
    kind = str(inst_type or "").upper()
    side = str(direction or "").upper()
    # Index options in this engine are premium-long for both CE and PE.  Direction is
    # the underlying view, not an instruction to short the option premium.
    if kind == "OPT" or side in {"LONG", "BUY"}:
        return (exit_ - entry) * qty * (1.0 if kind == "OPT" else mult)
    if side in {"SHORT", "SELL"}:
        return (entry - exit_) * qty * mult
    return None


def _candidate_round_trip_cost(candidate: Any) -> Optional[float]:
    explicit = _number_or_none(_candidate_value(candidate, "round_trip_cost", None))
    if explicit is not None and explicit >= 0:
        return round(explicit, 2)
    charges = _number_or_none(_candidate_value(candidate, "charges", None))
    if charges is not None and charges > 0:
        return round(charges, 2)

    entry = _candidate_value(
        candidate,
        "price",
        _candidate_value(candidate, "entry_price", None),
    )
    exit_ = _candidate_value(
        candidate,
        "exit_price",
        _candidate_value(candidate, "current_price", None),
    )
    kind = str(_candidate_value(candidate, "inst_type", "") or "").upper()
    multiplier = (
        _candidate_value(candidate, "multiplier", 1.0)
        if kind != "OPT"
        else 1.0
    )
    if kind != "OPT" and not _candidate_has(candidate, "multiplier"):
        multiplier = _candidate_value(candidate, "instrument_multiplier", 1.0)
    gross = _gross_outcome_pnl(
        entry_price=entry,
        exit_price=exit_,
        quantity=_candidate_quantity(candidate),
        inst_type=kind,
        direction=_candidate_value(candidate, "direction", ""),
        multiplier=multiplier,
    )
    booked = _number_or_none(_candidate_value(candidate, "pnl", None))
    if gross is None or booked is None:
        return None
    cost = gross - booked
    # A few paise of serialization/rounding disagreement is not a negative fee.
    if cost < -0.01:
        return None
    return round(max(0.0, cost), 2)


def _event_round_trip_cost(event: Dict[str, Any]) -> Optional[float]:
    explicit = _number_or_none(event.get("round_trip_cost"))
    if explicit is not None and explicit >= 0:
        return round(explicit, 2)
    charges = _number_or_none(event.get("charges"))
    if charges is not None and charges >= 0:
        return round(charges, 2)

    # Legacy rtracker.v1 exits omitted the cost but retained enough option-contract
    # identity to recover it exactly: gross terminal P&L minus the stored net outcome.
    # Do not attempt this when a producer explicitly says its stored outcome is gross.
    pnl_basis = _canonical_pnl_basis(event.get("pnl_basis"), default=PNL_BASIS_NET)
    if pnl_basis != PNL_BASIS_NET:
        return None
    contract = event.get("contract") if isinstance(event.get("contract"), dict) else {}
    lots = _number_or_none(contract.get("lots"))
    lot_size = _number_or_none(contract.get("lot_size"))
    quantity = (
        lots * lot_size
        if lots is not None and lot_size is not None and lots > 0 and lot_size > 0
        else _number_or_none(contract.get("quantity"))
    )
    gross = _gross_outcome_pnl(
        entry_price=event.get("price", event.get("entry_price")),
        exit_price=event.get("exit_price") or event.get("current_price"),
        quantity=quantity,
        inst_type=contract.get("inst_type"),
        direction=event.get("direction"),
        multiplier=contract.get("multiplier", 1.0),
    )
    booked = _number_or_none(event.get("pnl"))
    if gross is None or booked is None:
        return None
    cost = gross - booked
    if cost < -0.01:
        return None
    return round(max(0.0, cost), 2)


def normalize_replay_pnl_event(event: Any) -> Optional[Dict[str, Any]]:
    """Return one tracker exit on consistent gross-decision/net-booking bases.

    Exit guards in Scanner consume gross P&L, so ``pnl_path_gross`` is the path to feed
    them.  If a guard fires at sample *i*, ``pnl_path_net[i]`` is what that exit would
    have booked after the same round-trip cost as the real outcome.  ``pnl`` is the
    real outcome normalized to net.

    New events carry explicit bases and cost.  Legacy rtracker.v1 exits are supported by
    inferring their cost from contract quantity and entry/exit prices.  If the units
    cannot be established, return ``None`` instead of silently mixing them.
    """
    if not isinstance(event, dict):
        return None
    path = _pnl_path_rows(event.get("pnl_path"))
    booked = _number_or_none(event.get("pnl"))
    if not path or booked is None:
        return None

    pnl_basis = _canonical_pnl_basis(
        event.get("pnl_basis"), default=PNL_BASIS_NET
    )
    path_basis = _canonical_pnl_basis(
        event.get("pnl_path_basis"), default=PNL_BASIS_GROSS
    )
    if not pnl_basis or not path_basis:
        return None
    cost = _event_round_trip_cost(event)
    # Replaying a gross-threshold guard always needs both views of the path.  Refuse an
    # event whose round trip cannot be known rather than treating an absent fee as zero.
    if cost is None:
        return None

    booked_net = booked - cost if pnl_basis == PNL_BASIS_GROSS else booked
    gross_path = []
    net_path = []
    for seconds, pnl in path:
        if path_basis == PNL_BASIS_GROSS:
            gross, net = pnl, pnl - cost
        else:
            gross, net = pnl + cost, pnl
        gross_path.append([seconds, round(gross, 2)])
        net_path.append([seconds, round(net, 2)])
    return {
        "pnl": round(booked_net, 2),
        "pnl_path_gross": gross_path,
        "pnl_path_net": net_path,
        "round_trip_cost": round(cost, 2),
        "pnl_basis": PNL_BASIS_NET,
        "pnl_path_gross_basis": PNL_BASIS_GROSS,
        "pnl_path_net_basis": PNL_BASIS_NET,
    }


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    return _setting_enabled(value, False)


def _iso_or_empty(value: Any) -> str:
    parsed = _parse_dt(value)
    return parsed.isoformat() if parsed is not None else ""


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _last_reason(value: Any) -> str:
    if isinstance(value, (list, tuple)) and value:
        return str(value[-1] or "")
    return str(value or "")


def _compact(value: Any, max_items: int = 20) -> Any:
    if isinstance(value, dict):
        return {str(k): _compact(v, max_items) for k, v in list(value.items())[:max_items]}
    if isinstance(value, list):
        return [_compact(v, max_items) for v in value[:max_items]]
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value
