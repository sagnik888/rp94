"""Signal admission and order-execution orchestration."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

from loguru import logger

from engine.scanner_modes import scanner_mode, configured_mode, COMBINED_MODES, scope_suffix, same_owner, native_result, native_policy
from config.settings import get_settings
from engine import s1_lane as s1_lane_policy
from engine.boundary_failures import ExecutionBoundaryFailure
from engine.notification_manager import get_notification_manager
from engine.signal_processor import SignalProcessor, TradeCandidate
from trading.trade_manager import IST


class ExecutionService:
    """Owns candidate stabilization, admission, and execution coordination."""

    def __init__(self, scanner) -> None:
        self.scanner = scanner

    async def coordinate_and_execute(self, candidates: List[TradeCandidate], is_warmup: bool = False):
        """
        Coordinate every matured signal through independent eligibility checks.
        Same-time signals are never pre-dropped by a cross-index quota.
        """
        scanner = self.scanner
        self = scanner  # preserve bare-host arguments used by legacy static helpers
        if configured_mode(get_settings()) == "ut_ema":
            from engine.ut_ema_lane import coordinate
            return await coordinate(scanner, candidates, is_warmup=is_warmup)
        from engine.decision_record import ensure_decision_record

        for rank, candidate in enumerate(list(candidates or []), start=1):
            ensure_decision_record(
                candidate,
                rank=rank,
                decided_at=datetime.now(IST),
                alternatives=[item for item in list(candidates or []) if item is not candidate],
            )
        accepted_candidates: List[TradeCandidate] = []
        signal_fire_mode = scanner._signal_fire_mode()
        intrabar_enabled = signal_fire_mode == "INTRACANDLE"

        if scanner.mode != "HISTORICAL" and not is_warmup:
            settings = get_settings()
            if not scanner._live_clock_is_sane():
                scanner._pending_live_signals.clear()
                return accepted_candidates
            session_now = scanner._session_squareoff_clock()
            force_exit_time = scanner._configured_session_time("ut_force_exit_time", "15:25")
            final_entry_time = scanner._configured_session_time("ut_5min_no_entry_after", "15:15")
            if session_now.time() >= force_exit_time:
                if scanner.scan_count % 25 == 0:
                    logger.info(
                        f"Execution Gate: hard square-off window active after {force_exit_time.strftime('%H:%M')} IST."
                    )
                scanner._pending_live_signals.clear()
                return accepted_candidates

            if session_now.time() >= final_entry_time:
                pending_candidates = [
                    item.get("candidate")
                    for item in scanner._pending_live_signals.values()
                    if isinstance(item, dict) and item.get("candidate") is not None
                ]
                cutoff_eligible = any(
                    scanner._entry_cutoff_allows_candidate(candidate, session_now)
                    for candidate in list(candidates) + pending_candidates
                )
                if not cutoff_eligible:
                    if scanner.scan_count % 25 == 0:
                        logger.info(
                            f"Execution Gate: no eligible candle closed by "
                            f"{final_entry_time.strftime('%H:%M')} IST."
                        )
                    scanner._pending_live_signals.clear()
                    return accepted_candidates
        if scanner.mode != "HISTORICAL" and not is_warmup:
            source_now = scanner._session_squareoff_clock()
            timing_eligible = []
            for candidate in candidates:
                source_is_intrabar = scanner._candidate_source_is_intrabar(candidate, source_now)
                entered_intracandle = (
                    intrabar_enabled
                    and getattr(candidate, "action", "ENTRY") == "ENTRY"
                    and source_is_intrabar
                )
                setattr(candidate, "signal_fire_mode", signal_fire_mode)
                setattr(candidate, "entered_intracandle", entered_intracandle)
                if not intrabar_enabled and source_is_intrabar:
                    scanner.log_signal_decision_once(
                        f"{candidate.instrument}|{candidate.timeframe}|{getattr(candidate, 'signal_timestamp', '')}|closed-mode",
                        f"Signal waiting: {candidate.instrument} {candidate.timeframe} deferred until candle close.",
                        "trade",
                    )
                    continue
                # CANDLE_CLOSE only. In INTRACANDLE every candidate meets the entry cutoff
                # inside the stabilization block below, but that whole block is skipped in
                # closed-candle mode, leaving just the wave-level any() above — which is
                # not per candidate and only runs once the clock is already past
                # ut_5min_no_entry_after. A bar that closed inside the window but is
                # processed after it therefore entered unchecked.
                # Confined to this branch on purpose: applying the cutoff this early in
                # INTRACANDLE would reject candidates the buffer is still entitled to
                # mature, which is a live-path behaviour change this does not want.
                if not intrabar_enabled and not scanner._entry_cutoff_allows_candidate(
                    candidate, source_now
                ):
                    scanner.log_signal_decision_once(
                        f"{candidate.instrument}|{candidate.timeframe}|"
                        f"{getattr(candidate, 'signal_timestamp', '')}|closed-cutoff",
                        f"Entry cutoff: {candidate.instrument} {candidate.timeframe} "
                        f"candle closed past the no-entry policy.",
                        "trade",
                    )
                    continue
                timing_eligible.append(candidate)
            candidates = timing_eligible
            if not intrabar_enabled:
                scanner._pending_live_signals.clear()

        # ── Live Anti-Repaint Stabilization Buffer ──
        if scanner.mode != "HISTORICAL" and not is_warmup and intrabar_enabled:
            now = datetime.now(IST).replace(tzinfo=None)
            matured_candidates = []
            pending_changed = False

            # Step 1: Add new candidates to the buffer if not already present
            for c in candidates:
                if getattr(c, "action", "ENTRY") != "ENTRY":
                    continue
                # A signal whose SOURCE BAR HAS CLOSED does not belong in an anti-repaint
                # buffer. The buffer exists to let a forming bar's arrow settle; a final
                # bar's arrow cannot repaint, so the wait buys nothing and costs 30s of
                # drift before intelligence and the filters even see it.
                #
                # Worse, it was then run through the repaint gate below, which re-reads
                # the CURRENT latest signal. Once the cache rolls to the next bar the old
                # identity is gone, so a fully-confirmed closed-bar signal got discarded
                # as "arrow vanished" — a rejection reason that cannot logically apply to
                # a bar that is already final.
                #
                # entered_intracandle is computed for every candidate a few lines above
                # and, until now, read by nothing in this block.
                if not bool(getattr(c, "entered_intracandle", True)):
                    # Exempt from the anti-repaint WAIT — not from the session's entry
                    # policy. BOTH cutoff checks live further down this function: the
                    # pre-buffer one below, and the matured one inside the pending loop.
                    # `continue` skipped both, so a closed 15min bar could open a fresh
                    # index-option position after ut_no_entry_after with minutes left to
                    # the 15:25 force exit. The wave-level check above is an any() across
                    # all candidates, so it does not gate this one.
                    limit_time = (
                        scanner._configured_session_time("ut_no_entry_after", "15:00")
                        if c.timeframe == "15min"
                        else scanner._configured_session_time("ut_5min_no_entry_after", "15:15")
                    )
                    if not scanner._entry_cutoff_allows_candidate(c, session_now):
                        logger.info(
                            f"🚫 [LIVE GATE] Discarding closed-candle {c.instrument} "
                            f"{c.timeframe} {c.direction} due to entry policy ({limit_time})"
                        )
                        scanner._rtracker_record_candidate(
                            c,
                            decision="REJECTED",
                            reason=str(getattr(c, "_entry_cutoff_reason", "") or f"Entry cutoff policy {limit_time}"),
                            source=str(getattr(c, "_entry_cutoff_source", "") or "entry_cutoff_gate"),
                        )
                        continue
                    # A closed-bar arrow printing the OTHER way must still evict a stale
                    # opposite buffer. The repaint gate cannot do it: the cached signals
                    # list holds the whole session's flip history, so after a CROSS-BAR
                    # reversal the older arrow is still in that list and the stale side
                    # reads PRESENT. It then matures into a second bought leg — a long CE
                    # and a long PE on one chart, two premiums, theta against both.
                    sig_ts = getattr(c, "signal_timestamp", None)
                    if sig_ts is None:
                        cached_res = scanner._cached_results.get(f"{c.instrument}_{c.timeframe}")
                        if cached_res and cached_res.get("signals"):
                            target_type = "BUY" if c.direction == "LONG" else "SELL"
                            matching_sigs = [
                                s for s in cached_res["signals"] if s.signal_type == target_type
                            ]
                            if matching_sigs:
                                sig_ts = matching_sigs[-1].timestamp
                    scanner._evict_superseded_opposite_buffer(c, sig_ts)
                    # A UTS flip buffered just before the close is now final. Remove
                    # that same identity so it cannot also mature in step 2 below.
                    if str(getattr(c, 'source_engine', '')).lower() == 'ut_smart':
                        same_key = f"{c.instrument}_{c.timeframe}_{c.direction}" + scope_suffix(c, get_settings())
                        existing = scanner._pending_live_signals.get(same_key)
                        if existing and scanner._one_min_timestamp(existing.get('sig_timestamp')) == scanner._one_min_timestamp(sig_ts):
                            scanner._pending_live_signals.pop(same_key, None)
                            getattr(scanner, '_signal_preflight_cache', {}).pop(same_key, None)
                            pending_changed = True
                    matured_candidates.append(c)
                    scanner._diag_inc("stabilization", "closed_candle_direct")
                    scanner.log_signal_decision_once(
                        f"{c.instrument}|{c.timeframe}|{getattr(c, 'signal_timestamp', '')}|closed-direct",
                        f"{c.instrument} {c.timeframe} {c.direction}: source candle already "
                        "closed, going straight to analysis without the stabilization wait.",
                        "trade",
                    )
                    continue
                # Timeframe-Specific entry gate check
                c_time = session_now.time()
                limit_time = (
                    scanner._configured_session_time("ut_no_entry_after", "15:00")
                    if c.timeframe == "15min"
                    else scanner._configured_session_time("ut_5min_no_entry_after", "15:15")
                )
                if not scanner._entry_cutoff_allows_candidate(c, session_now):
                    logger.info(f"🚫 [LIVE GATE] Skipping buffering for {c.instrument} {c.timeframe} {c.direction} at {c_time} due to entry policy ({limit_time})")
                    continue

                key = f"{c.instrument}_{c.timeframe}_{c.direction}" + scope_suffix(c, get_settings())
                if key not in scanner._pending_live_signals:
                    # Track the exact intrabar UT1 signal. This lets realtime
                    # signals form and repaint, but only mature if the same
                    # signal is still present after the stabilization delay.
                    sig_timestamp = getattr(c, "signal_timestamp", None)
                    cached_key = f"{c.instrument}_{c.timeframe}"
                    cached_res = scanner._cached_results.get(cached_key)
                    if sig_timestamp is None and cached_res and cached_res.get("signals"):
                        target_type = "BUY" if c.direction == "LONG" else "SELL"
                        matching_sigs = [s for s in cached_res["signals"] if s.signal_type == target_type]
                        if matching_sigs:
                            sig_timestamp = matching_sigs[-1].timestamp

                    # A cache hit from the scan that CREATED the candidate is not an
                    # independent stabilization observation.  Force the next valid MTF
                    # pass to recompute even if the candle hash is unchanged, and bind
                    # this buffer to that post-buffer result timestamp.
                    result_required_after_epoch = time.time()
                    require_fresh = getattr(
                        getattr(self, "mtf", None), "require_fresh_result", None
                    )
                    if callable(require_fresh):
                        try:
                            require_fresh(
                                c.instrument,
                                c.timeframe,
                                newer_than_epoch=result_required_after_epoch,
                            )
                        except Exception as exc:
                            logger.debug(
                                f"Could not arm MTF freshness for {key}: {exc}"
                            )

                    stable_seconds, min_stable_scans, adaptive_stability = scanner._live_signal_stabilization_requirements(c)
                    scanner._pending_live_signals[key] = {
                        "timestamp": now,
                        "candidate": c,
                        "sig_timestamp": sig_timestamp,
                        "result_required_after_epoch": result_required_after_epoch,
                        "stable_scans": 0,
                        # The announced wait is what this buffer honours. It used to be
                        # recomputed on every maturity pass from the CURRENT regime, so a
                        # signal logged "for 30s" was silently held to 60s when the regime
                        # turned risky mid-buffer.
                        "announced_seconds": float(stable_seconds),
                        "announced_scans": int(min_stable_scans),
                        "announced_adaptive": bool(adaptive_stability),
                    }
                    pending_changed = True
                    scanner._diag_inc("stabilization", "buffered")
                    scanner._evict_superseded_opposite_buffer(c, sig_timestamp)
                    scan_note = f" and {min_stable_scans} stable scans" if adaptive_stability and min_stable_scans > 1 else ""
                    logger.info(f"[STABILIZATION] Buffering {c.direction} signal for {c.instrument} {c.timeframe} for {stable_seconds:.0f}s{scan_note}...")
                    scanner.log_event(f"Buffering {c.direction} signal for {c.instrument} {c.timeframe} for {stable_seconds:.0f}s{scan_note}...", "system")
                else:
                    # A LATER arrow on the same instrument/timeframe/direction used to be
                    # dropped in silence while the first snapshot kept waiting, so what
                    # executed carried the older price, levels and intelligence. Carry the
                    # newer candidate forward; the running wait is NOT restarted, because
                    # the stabilization window is about the signal persisting, not about
                    # which snapshot of it is newest.
                    scanner._refresh_pending_live_signal(key, c, now)

            if pending_changed:
                # A signal may mature before the normal 15-second checkpoint.
                # Persist immediately so a restart resumes this exact timer.
                scanner._save_warm_memory(force=True)

            # Step 2: Check which buffered signals have matured (held for >= 20 seconds)
            keys_to_remove = []
            for key, data in list(scanner._pending_live_signals.items()):
                c = data["candidate"]
                sig_timestamp = data.get("sig_timestamp")
                elapsed = (now - data["timestamp"]).total_seconds()
                tf_minutes = {"1min": 1, "5min": 5, "15min": 15}.get(str(getattr(c, "timeframe", "") or ""), 5)
                stable_seconds, min_stable_scans, adaptive_stability = scanner._live_stabilization_requirements_for_pending(c, data)
                max_pending_seconds = (tf_minutes * 60.0) + 90.0
                if sig_timestamp:
                    sig_time_check = sig_timestamp
                    if getattr(sig_time_check, "tzinfo", None) is not None:
                        sig_time_check = sig_time_check.astimezone(IST).replace(tzinfo=None)
                    clock_check = session_now
                    if getattr(clock_check, "tzinfo", None) is not None:
                        clock_check = clock_check.astimezone(IST).replace(tzinfo=None)
                    if sig_time_check > (clock_check + timedelta(seconds=5)):
                        logger.warning(
                            f"[STABILIZATION] Signal for {key} has future timestamp {sig_time_check} "
                            f"relative to scanner clock {clock_check}. Discarding."
                        )
                        scanner.log_event(f"Future-timestamp signal discarded: {key}", "warning")
                        scanner._diag_inc("stabilization", "discarded_future")
                        scanner._forget_signal_display_rows(c)
                        keys_to_remove.append(key)
                        dedup_key = f"{c.instrument}_{c.timeframe}" + scope_suffix(c, get_settings())
                        scanner._last_live_signal_candle_time.pop(dedup_key, None)
                        continue

                if elapsed > max_pending_seconds:
                    logger.warning(
                        f"[STABILIZATION] Signal for {key} expired after {elapsed:.1f}s "
                        "without a stable scan cadence. Discarding stale candidate."
                    )
                    scanner.log_event(f"Stale buffered signal discarded: {key}", "warning")
                    scanner._diag_inc("stabilization", "discarded_stale")
                    scanner._forget_signal_display_rows(c)
                    keys_to_remove.append(key)
                    dedup_key = f"{c.instrument}_{c.timeframe}" + scope_suffix(c, get_settings())
                    scanner._last_live_signal_candle_time.pop(dedup_key, None)
                    continue

                cached_key = f"{c.instrument}_{c.timeframe}"
                if data.get("recovered") and cached_key not in scanner._cached_results:
                    # Startup may restore the checkpoint before the first fresh
                    # indicator pass. Wait for that pass instead of declaring
                    # the restored arrow repainted from an empty cache.
                    scanner._diag_inc("stabilization", "waiting_fresh_scan_after_restart")
                    continue

                result_required_after_epoch = data.get("result_required_after_epoch")
                repaint_status = scanner._repaint_signal_status(
                    c,
                    sig_timestamp,
                    result_newer_than_epoch=result_required_after_epoch,
                )
                if repaint_status == "UNKNOWN":
                    # Fail closed without manufacturing a repaint verdict: no stable
                    # scan is earned until the exact timeframe has successfully
                    # recomputed after buffering.  Keep the force-recompute request
                    # armed after a transient missing/short/error input.
                    require_fresh = getattr(
                        getattr(self, "mtf", None), "require_fresh_result", None
                    )
                    if callable(require_fresh) and result_required_after_epoch is not None:
                        try:
                            require_fresh(
                                c.instrument,
                                c.timeframe,
                                newer_than_epoch=float(result_required_after_epoch),
                            )
                        except Exception:
                            pass
                    scanner._diag_inc("stabilization", "waiting_fresh_timeframe_result")
                    continue
                still_present = repaint_status == "PRESENT"

                if not still_present:
                    override_ctx = scanner._stabilization_repaint_override_context(c, sig_timestamp)
                    if override_ctx:
                        setattr(c, "stabilization_momentum_override", True)
                        try:
                            c.reasons = list(c.reasons or []) + [
                                f"Repaint override: {override_ctx.get('reason', override_ctx.get('type', 'confirmed momentum'))}"
                            ]
                        except Exception:
                            pass
                        scanner._diag_inc("stabilization", "momentum_override")
                        logger.info(
                            f"[STABILIZATION] Re-admitting {c.instrument} {c.timeframe} {c.direction}: "
                            f"arrow flickered but {override_ctx.get('reason', 'momentum confirmed')}."
                        )
                        scanner.log_event(
                            f"Signal re-admitted: {c.instrument} {c.timeframe} {c.direction} "
                            f"after repaint flicker; {override_ctx.get('reason', 'momentum confirmed')}.",
                            "trade",
                        )
                    else:
                        flipped = repaint_status == "FLIPPED"
                        scanner._diag_inc(
                            "stabilization",
                            "discarded_flipped" if flipped else "discarded_repaint",
                        )
                        what = (
                            f"UT1 arrow FLIPPED to the opposite side after {elapsed:.1f}s"
                            if flipped
                            else f"UT1 arrow vanished after {elapsed:.1f}s"
                        )
                        logger.warning(
                            f"[STABILIZATION] Rejected {c.instrument} {c.timeframe} {c.direction}: {what}."
                        )
                        scanner.log_event(
                            (
                                f"Signal rejected: {c.instrument} {c.timeframe} {c.direction} "
                                f"flipped to the opposite side during the {stable_seconds:.0f}s "
                                "stabilization window; the momentum override does not apply to a reversal."
                            )
                            if flipped
                            else (
                                f"Signal rejected: {c.instrument} {c.timeframe} {c.direction} "
                                f"repainted during the {stable_seconds:.0f}s stabilization window."
                            ),
                            "warning",
                        )
                        scanner._rtracker_record_candidate(
                            c,
                            decision="REJECTED",
                            reason=(
                                f"Flipped to the opposite side during {stable_seconds:.0f}s "
                                "stabilization window"
                                if flipped
                                else f"Repainted during {stable_seconds:.0f}s stabilization window"
                            ),
                            source="stabilization_repaint_gate",
                        )
                        scanner._forget_signal_display_rows(c)
                        # The indicator has withdrawn this arrow. Withdraw it from the
                        # re-entry pool too, or the missed-signal path re-admits the same
                        # discarded signal within seconds.
                        scanner._drop_missed_reentry_seed(
                            c,
                            reason=(
                                "arrow flipped to the opposite side"
                                if flipped
                                else "arrow vanished during stabilization"
                            ),
                        )
                        if not flipped and s1_lane_policy.is_s1(c):
                            # S1 re-enters a repainted signal only when the S1 score comes
                            # back as strong (see _s1_missed_reentry_support).
                            scanner._remember_missed_reentry_seed(
                                c,
                                reason="S1 arrow repainted during stabilization",
                                source="s1_repaint",
                            )
                        keys_to_remove.append(key)
                        dedup_key = f"{c.instrument}_{c.timeframe}" + scope_suffix(c, get_settings())
                        identity = f"{sig_timestamp.replace(tzinfo=None).isoformat()}|{getattr(c, 'source_signal_type', '')}" if sig_timestamp else ""
                        SignalProcessor._forget_live_signal_identity(
                            self,
                            dedup_key,
                            sig_timestamp,
                            identity,
                        )
                        if str(getattr(c, 'source_engine', '')).lower() == 'ut_smart':
                            SignalProcessor._forget_live_signal_identity(
                                self, dedup_key, sig_timestamp, f'{identity}|ut_smart')
                        if configured_mode(get_settings()) in COMBINED_MODES:
                            SignalProcessor._forget_live_signal_identity(
                                self, dedup_key, sig_timestamp,
                                identity + "|" + str(getattr(c, "source_engine", "") or ""))
                        scanner._candidate_process_cache.pop(c.instrument, None)
                        raw_key = (
                            f"{c.instrument}|{c.timeframe}|{getattr(c, 'source_signal_type', '')}|"
                            f"{sig_timestamp.replace(tzinfo=None).isoformat(timespec='seconds')}"
                            if sig_timestamp
                            else ""
                        )
                        if raw_key:
                            getattr(self, "_raw_utbot_activity_keys", set()).discard(raw_key)
                            if str(getattr(c, 'source_engine', '')).lower() == 'ut_smart':
                                getattr(self, '_raw_utbot_activity_keys', set()).discard(f'{raw_key}|ut_smart')
                        continue

                data["stable_scans"] = int(data.get("stable_scans") or 0) + 1
                stable_scans = int(data.get("stable_scans") or 0)

                if data.pop("recovered", False):
                    logger.info(
                        f"[STABILIZATION] Resumed {c.instrument} {c.timeframe} {c.direction} "
                        f"after restart with {elapsed:.1f}s already elapsed."
                    )
                    scanner.log_event(
                        f"Resumed signal buffer after restart: {c.instrument} {c.timeframe} "
                        f"{c.direction}, {elapsed:.1f}s already stable.",
                        "system",
                    )

                if not scanner._stabilization_ready(
                    elapsed,
                    stable_seconds,
                    stable_scans,
                    min_stable_scans,
                    adaptive_stability,
                ):
                    scanner._maybe_prepare_stabilization_preflight(
                        key,
                        c,
                        elapsed,
                        stable_seconds,
                    )
                    scanner._diag_inc("stabilization", "waiting_stability")
                    continue

                from engine import uts_lane
                if uts_lane.is_uts(c):
                    # A result computed early in the buffer can remain "fresh" for
                    # 30 seconds. It cannot prove that the arrow survived the full
                    # 6/20-second wait, even if repeatedly read on later scan cycles.
                    result = native_result(c, scanner._cached_results.get(cached_key)) or {}
                    observed = (result.get("uts_evaluation") or {}).get("observed_at")
                    if observed is not None:
                        try:
                            complete_window = (
                                uts_lane._market_time(observed)
                                >= uts_lane._market_time(data["timestamp"])
                                + timedelta(seconds=stable_seconds)
                            )
                        except (TypeError, ValueError):
                            complete_window = False
                        if not complete_window:
                            require_fresh = getattr(getattr(scanner, "mtf", None), "require_fresh_result", None)
                            if callable(require_fresh):
                                require_fresh(c.instrument, c.timeframe)
                            scanner._diag_inc("stabilization", "waiting_buffer_end_observation")
                            continue

                c_time = session_now.time()
                limit_time = (
                    scanner._configured_session_time("ut_no_entry_after", "15:00")
                    if c.timeframe == "15min"
                    else scanner._configured_session_time("ut_5min_no_entry_after", "15:15")
                )
                if not scanner._entry_cutoff_allows_candidate(c, session_now):
                    logger.info(f"🚫 [LIVE GATE] Discarding matured signal for {key} due to entry policy ({limit_time})")
                    scanner._rtracker_record_candidate(
                        c,
                        decision="REJECTED",
                        reason=str(getattr(c, "_entry_cutoff_reason", "") or f"Entry cutoff policy {limit_time}"),
                        source=str(getattr(c, "_entry_cutoff_source", "") or "entry_cutoff_gate"),
                    )
                    keys_to_remove.append(key)
                    dedup_key = f"{c.instrument}_{c.timeframe}" + scope_suffix(c, get_settings())
                    scanner._last_live_signal_candle_time.pop(dedup_key, None)
                    continue

                logger.success(f"✅ [STABILIZATION] Signal for {key} matured after {elapsed:.1f}s. Proceeding to execute.")
                scanner._apply_stabilization_preflight(key, c)
                scanner._diag_inc("stabilization", "matured")
                # The whole point of the wait is that more evidence arrives during it.
                # Re-score the intelligence and regime pillars on what exists now, so
                # the live gate judges the signal on current evidence rather than on the
                # grade it was born with 20-60 seconds ago.
                scanner._regrade_matured_candidate(c)
                # Record the SURVIVING side too. Rejections were already written here,
                # so rtracker held only the deaths and no survival rate was computable
                # from it — on 2026-08-11 the log showed 48 maturations against 9
                # ACCEPTED rows, because most matured signals are refused further down
                # and never reach an acceptance record. STABILIZED is its own decision
                # so it cannot be mistaken for an entry in the funnel arithmetic.
                if scanner._setting_enabled(
                    getattr(get_settings(), "stabilization_rtracker_logging_enabled", True), True
                ):
                    scanner._rtracker_record_candidate(
                        c,
                        decision="STABILIZED",
                        reason=(
                            f"Survived the {stable_seconds:.0f}s stabilization window "
                            f"in {elapsed:.1f}s"
                        ),
                        source="stabilization_gate",
                        extra={
                            "stabilization": {
                                "window_seconds": round(float(stable_seconds or 0.0), 1),
                                "elapsed_seconds": round(float(elapsed or 0.0), 1),
                                "momentum_override": bool(
                                    getattr(c, "stabilization_momentum_override", False)
                                ),
                                "cascade_latched": bool(
                                    getattr(c, "_stabilization_cascade_latch", None)
                                ),
                            }
                        },
                    )
                scanner.log_event(
                    f"UT1 {c.direction} {c.instrument} {c.timeframe} stayed valid for {elapsed:.1f}s. Running filters.",
                    "trade",
                )
                matured_candidates.append(c)
                keys_to_remove.append(key)
                if sig_timestamp:
                    dedup_key = f"{c.instrument}_{c.timeframe}" + scope_suffix(c, get_settings())
                    scanner._last_signal_time[dedup_key] = max(scanner._last_signal_time.get(dedup_key, datetime.min), sig_timestamp)

            for key in keys_to_remove:
                scanner._pending_live_signals.pop(key, None)
                getattr(self, "_signal_preflight_cache", {}).pop(key, None)
            if keys_to_remove:
                # Prevent a rejected or matured buffer from being resurrected
                # if another restart happens before the periodic checkpoint.
                scanner._save_warm_memory(force=True)

            candidates = matured_candidates
            if not candidates:
                return accepted_candidates

        # â”€â”€ Institutional Execution Gate â”€â”€
        if not scanner.data.is_market_open() and not is_warmup:
            # Don't place new trades if market is closed (except in Historical mode)
            if scanner.mode != "HISTORICAL":
                if scanner.scan_count % 100 == 0:
                    logger.debug("â³ Execution Gate: Market is closed. Analysis only.")
                return accepted_candidates

        # Same-wave heavy-trend votes: register peers before mixed-book / sizing
        # so 2/4 align does not lag one scan cycle (R3).
        try:
            scanner._ingest_wave_heavy_trend_votes(list(candidates or []))
        except Exception:
            pass

        # Multi-engine / multi-TF / multi-index wave:
        # collapse best TF per index, assign at most 1× FUT (1 lot), rest OPT.
        try:
            candidates = scanner._apply_multi_signal_mixed_book(list(candidates or []))
        except Exception as mix_exc:
            logger.warning(f"Multi-signal mixed book skipped: {mix_exc}")

        # Final product lock: even single-index paths must honor FUT/OPT-only.
        try:
            candidates = scanner._apply_strict_instrument_pref_to_entries(list(candidates or []))
        except Exception as pref_exc:
            # Fail closed: this is the terminal product lock — never execute an
            # un-enforced wave. Under a FUT/OPT lock, drop every ENTRY whose product
            # does not already match the lock rather than passing the pre-gate list
            # through to open_trade. Exits/non-ENTRY rows are preserved.
            logger.warning(f"Strict instrument preference pass raised; failing closed: {pref_exc}")
            _lock = scanner._strict_instrument_pref()
            if _lock in {"FUT", "OPT"}:
                def _matches_lock(c):
                    if str(getattr(c, "action", "ENTRY") or "ENTRY").upper() != "ENTRY":
                        return True
                    it = str(getattr(c, "inst_type", "") or "").upper()
                    if it in {"CE", "PE"}:
                        it = "OPT"
                    return it == _lock
                candidates = [c for c in (candidates or []) if _matches_lock(c)]

        # OPT contract safety net: an ENTRY claiming OPT must carry a resolved
        # option contract (rare dual-TF clones can flip a FUT winner's sibling
        # to OPT with futures fields still attached).
        try:
            candidates = scanner._resolve_unbuilt_opt_entries(list(candidates or []))
        except Exception as _opt_net_exc:
            failure = ExecutionBoundaryFailure(
                "option contract resolution failed", cause=_opt_net_exc, retryable=True
            )
            logger.error(f"Execution abstained: {failure}: {_opt_net_exc}")
            for candidate in list(candidates or []):
                scanner._update_candidate_decision_record(
                    candidate,
                    "BLOCK",
                    str(failure),
                    "execution_boundary",
                )
            candidates = []

        # R4: arm global position ceiling for cascade multi-TF batch
        try:
            cascade_wave = any(
                bool(getattr(c, "one_way_cascade_entry", False))
                for c in (candidates or [])
                if getattr(c, "action", "ENTRY") == "ENTRY"
            )
            arm = getattr(getattr(self, "trades", None), "arm_cascade_wave_capacity", None)
            if callable(arm):
                arm(cascade_wave)
        except Exception:
            pass

        # One global queue prevents LONG from consuming shared capacity before a
        # higher-quality SHORT. Exits retain absolute priority.
        by_dir = {"GLOBAL": list(candidates)}

        for direction, signals in by_dir.items():
            if not signals: continue

            signals.sort(key=scanner._candidate_execution_priority, reverse=True)
            scanner._prepare_candidate_decision_records(signals)
            open_count = len(scanner.trades.open_trades)

            # â•â•â• CROSS-INSTRUMENT CORRELATION GUARD â•â•â•
            # Rules:
            # 1. Max 2 concurrent trades per index.
            # 2. Every separate index receives its own complete eligibility pass.

            for i, sig in enumerate(signals):
                if scanner._absorb_same_direction_session_refresh(sig):
                    scanner._record_absorbed_session_signal(sig)
                    continue
                total_open = len(scanner.trades.open_trades)

                signal_type = "BUY" if str(getattr(sig, "direction", "") or "").upper() == "LONG" else "SELL"
                signal_time_for_reversal = getattr(sig, "signal_timestamp", None)
                one_way_cascade = scanner._one_way_cascade_context(
                    sig.instrument,
                    sig.timeframe,
                    signal_type,
                    signal_time_for_reversal,
                )
                if one_way_cascade:
                    setattr(sig, "one_way_cascade_entry", True)
                    setattr(sig, "one_way_cascade_context", one_way_cascade)
                    try:
                        sig.reasons = list(sig.reasons or []) + [
                            (
                                f"{one_way_cascade.get('reason', 'one-way cascade')}: "
                                f"{', '.join(one_way_cascade.get('indices', []))}"
                            )
                        ]
                    except Exception:
                        pass
                market_wide_reversal = scanner._market_wide_reversal_context(
                    sig.instrument,
                    sig.timeframe,
                    signal_type,
                    signal_time_for_reversal,
                )
                if market_wide_reversal:
                    setattr(sig, "market_wide_reversal_entry", True)
                    setattr(sig, "market_wide_reversal_context", market_wide_reversal)
                    try:
                        sig.reasons = list(sig.reasons or []) + [
                            (
                                f"{market_wide_reversal.get('reason', 'market-wide reversal')}: "
                                f"{', '.join(market_wide_reversal.get('indices', []))}"
                            )
                        ]
                    except Exception:
                        pass
                market_ltp_shock = scanner._evaluate_market_ltp_shock(
                    sig.instrument,
                    signal_type,
                    signal_time_for_reversal,
                    timeframe=str(getattr(sig, "timeframe", "") or ""),
                    setup_confidence=float(getattr(sig, "confidence", 0.0) or 0.0),
                    setup_adx=float(getattr(sig, "adx_value", 0.0) or 0.0),
                    setup_intel_score=float(getattr(sig, "intelligence_score", 0.0) or 0.0),
                    trend_15m_agrees=bool(getattr(sig, "trend_15m_agrees", False)),
                )
                if market_ltp_shock:
                    setattr(sig, "market_ltp_shock_entry", True)
                    setattr(sig, "market_ltp_shock_context", market_ltp_shock)
                    try:
                        sig.reasons = list(sig.reasons or []) + [
                            (
                                f"{market_ltp_shock.get('reason', 'market LTP shock')}: "
                                f"{', '.join(market_ltp_shock.get('indices', []))}"
                            )
                        ]
                    except Exception:
                        pass

                # Only an explicit A/A+ operator preference is checked here. AUTO/B/B+
                # flow to the canonical evidence verdict below.
                grade_allowed, grade_preference_reason = (
                    (True, "")
                    if s1_lane_policy.is_lane(sig) or str(getattr(sig, "action", "ENTRY")).upper() == "EXIT"
                    else scanner._passes_candidate_grade_preference(sig)
                )
                if not grade_allowed:
                    reason_str = ", ".join(sig.reasons) if sig.reasons else "Low confidence"
                    rejection_reason = grade_preference_reason
                    scanner.log_event(f"🚫 Signal Rejected: {sig.instrument} {sig.direction} ({sig.grade}) - {rejection_reason} | Reasons: {reason_str}", "trade")
                    logger.info(f"🚫 Signal Rejected: {sig.instrument} {sig.direction} ({sig.grade}) - {rejection_reason} | Reasons: {reason_str}")
                    scanner._rtracker_record_candidate(
                        sig,
                        decision="REJECTED",
                        reason=rejection_reason,
                        source="grade_preference_gate",
                    )
                    scanner._remember_missed_reentry_seed(
                        sig,
                        reason=rejection_reason,
                        source="grade_preference_gate",
                    )
                    continue

                signal_time = getattr(sig, "signal_timestamp", None) or datetime.now(IST).replace(tzinfo=None)
                if getattr(signal_time, "tzinfo", None) is not None:
                    signal_time = signal_time.astimezone(IST).replace(tzinfo=None)
                allowed, limit_reason = scanner._can_trade_instrument(
                    sig.instrument, signal_time,
                    direction=str(getattr(sig, "direction", "") or ""),
                    adx_value=float(getattr(sig, "adx_value", 0.0) or 0.0),
                    s1_lane=s1_lane_policy.lane_tag(sig),
                )
                if not allowed:
                    scanner.log_signal_decision_once(
                        f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{signal_time.isoformat()}|trade-limit",
                        f"Signal rejected: {sig.instrument} {sig.direction} - {limit_reason}",
                        "trade",
                    )
                    scanner._rtracker_record_candidate(
                        sig,
                        decision="REJECTED",
                        reason=limit_reason,
                        source="trade_limit_gate",
                    )
                    continue
                if str(limit_reason or "").startswith("stop-out re-entry:"):
                    setattr(sig, "stop_out_reentry", True)

                if not scanner._passes_live_trade_ready_gate(sig):
                    ts = getattr(sig, "signal_timestamp", None)
                    ts_key = ts.isoformat(timespec="seconds") if ts else ""
                    rejection_reason = str(
                        getattr(sig, "live_gate_rejection_reason", "") or
                        "live readiness gate returned False without a stamped sub-gate reason"
                    )
                    rejection_source = str(
                        getattr(sig, "live_gate_rejection_source", "") or
                        "live_ready_gate_unstamped"
                    )
                    scanner.log_signal_decision_once(
                        f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{ts_key}|live-ready",
                        f"Signal rejected: {sig.instrument} {sig.direction} {sig.timeframe} - {rejection_reason}",
                        "trade",
                    )
                    scanner._rtracker_record_candidate(
                        sig,
                        decision="REJECTED",
                        reason=rejection_reason,
                        source=rejection_source,
                    )
                    scanner._remember_missed_reentry_seed(
                        sig,
                        reason=rejection_reason,
                        source=rejection_source,
                    )
                    continue

                if not scanner._candidate_quote_ready_for_lifecycle(sig):
                    ts = getattr(sig, "signal_timestamp", None)
                    ts_key = ts.isoformat(timespec="seconds") if ts else ""
                    scanner.log_signal_decision_once(
                        f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{ts_key}|quote-unverified",
                        (
                            f"Signal rejected: {sig.instrument} {sig.direction} {sig.timeframe} - "
                            "quote unavailable; waiting for verified option premium before lifecycle acceptance"
                        ),
                        "trade",
                    )
                    sig.status = "QUOTE UNAVAILABLE"
                    scanner._rtracker_record_candidate(
                        sig,
                        decision="REJECTED",
                        reason="Quote unavailable before lifecycle acceptance",
                        source="quote_ready_gate",
                    )
                    continue

                # Per-Instrument Safety (Cross-TF Complementary Rule)
                quality_inst_type_before_concurrency = str(
                    getattr(sig, "inst_type", "") or ""
                ).upper()
                if not scanner._prepare_candidate_for_concurrency(sig):
                    # A duplicate on a chart that is already working is not noise to throw
                    # away — it is the cascade confirming itscanner. Before dropping it, offer
                    # it to the running trade as one more lot. scale_in_trade owns every
                    # rule about whether that is allowed (already in profit, still with
                    # trend, add cap, lot ceiling, rupee risk budget) and refuses with a
                    # reason when it is not; this only decides WHEN to ask.
                    #
                    # MANUAL MODE MUST NOT REACH THE BROKER. This call site sits 51 lines
                    # ABOVE the `if not scanner.auto_mode` gate that stops every ordinary
                    # entry, and nothing below it re-checks: trade_manager.py contains no
                    # reference to auto_mode at all, and scale_in_trade branches only on
                    # mode == "REAL" before placing a live BUY on the running contract.
                    # An operator with auto mode off, expecting to approve each entry from
                    # the toast, would have had a lot added to a live position with no
                    # approval. Inert today only because scale_in_enabled is False -- which
                    # is exactly the switch this feature exists to be turned on by.
                    if bool(getattr(sig, "option_exposure_blocked", False)):
                        added, add_note = False, "shared option exposure refusal cannot become a scale-in"
                    elif not scanner.auto_mode and not is_warmup:
                        scanner.log_signal_decision_once(
                            f"{sig.instrument}|{sig.timeframe}|{sig.direction}|manual-cascade",
                            f"{sig.instrument} {sig.timeframe} {sig.direction}: cascade add "
                            "available but manual mode is on — approve it yourscanner.",
                            "trade",
                        )
                        added, add_note = False, "manual mode: cascade add not auto-executed"
                    else:
                        added, add_note = scanner._offer_cascade_add(sig)
                    if added:
                        continue
                    ts = getattr(sig, "signal_timestamp", None)
                    ts_key = ts.isoformat(timespec="seconds") if ts else ""
                    if add_note:
                        scanner.log_signal_decision_once(
                            f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{ts_key}|cascade-add",
                            f"{sig.instrument} {sig.timeframe} {sig.direction}: cascade add "
                            f"declined ({add_note})",
                            "trade",
                        )
                    block_kind = str(
                        getattr(sig, "concurrency_block_kind", "active trade lifecycle")
                        or "active trade lifecycle"
                    )
                    block_reason = str(
                        getattr(sig, "concurrency_block_reason", "concurrency limit")
                        or "concurrency limit"
                    )
                    reject_detail = f"{block_kind}: {block_reason}"
                    scanner.log_signal_decision_once(
                        f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{ts_key}|concurrency",
                        f"Signal rejected: {sig.instrument} {sig.direction} {sig.timeframe} - {reject_detail}",
                        "trade",
                    )
                    scanner._rtracker_record_candidate(
                        sig,
                        decision="REJECTED",
                        reason=f"Concurrency: {reject_detail}",
                        source="concurrency_gate",
                    )
                    continue

                quality_inst_type_after_concurrency = str(
                    getattr(sig, "inst_type", "") or ""
                ).upper()
                if (
                    quality_inst_type_after_concurrency != quality_inst_type_before_concurrency
                    and not s1_lane_policy.is_s1(sig) and str(getattr(sig, "action", "ENTRY")).upper() == "ENTRY"
                ):
                    # Concurrency may turn a FUT signal into its OPT complement (or the
                    # reverse).  The first verdict describes the pre-route product, so it
                    # cannot be carried into persistence or broker submission unchanged.
                    routed_quality = scanner._current_quality_verdict_for_candidate(
                        sig,
                        lane="LIVE_POST_ROUTE",
                    )
                    if not routed_quality.allowed:
                        rejection_reason = (
                            f"Post-route quality blocked {quality_inst_type_before_concurrency}→"
                            f"{quality_inst_type_after_concurrency}: {routed_quality.reason} "
                            f"[{routed_quality.reason_code}]"
                        )
                        setattr(sig, "live_gate_rejection_reason", rejection_reason)
                        setattr(sig, "live_gate_rejection_source", "post_route_quality_gate")
                        scanner._rtracker_record_candidate(
                            sig,
                            decision="REJECTED",
                            reason=rejection_reason,
                            source="post_route_quality_gate",
                        )
                        scanner._remember_missed_reentry_seed(
                            sig,
                            reason=rejection_reason,
                            source="post_route_quality_gate",
                        )
                        continue
                    if not scanner._candidate_quote_ready_for_lifecycle(sig):
                        sig.status = "QUOTE UNAVAILABLE"
                        scanner._rtracker_record_candidate(
                            sig,
                            decision="REJECTED",
                            reason="Quote unavailable after concurrency product conversion",
                            source="post_route_quote_gate",
                        )
                        continue

                # Per-bucket concurrent capacity: normal 4x2, rare burst 4x3 only
                # with all-index 95%+/volume/ADX confirmation.
                if not scanner._bucket_capacity_allows(sig):
                    bucket = scanner._correlated_index_bucket(sig.instrument)
                    ts = getattr(sig, "signal_timestamp", None)
                    ts_key = ts.isoformat(timespec="seconds") if ts else ""
                    scanner.log_signal_decision_once(
                        f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{ts_key}|bucket-capacity",
                        f"Signal rejected: {sig.instrument} {sig.direction} {sig.timeframe} - "
                        f"{bucket} bucket at capacity ({scanner._active_bucket_cap(bucket)} trades)",
                        "trade",
                    )
                    scanner._rtracker_record_candidate(
                        sig,
                        decision="REJECTED",
                        reason=f"{bucket} bucket at capacity",
                        source="bucket_capacity_gate",
                    )
                    continue

                # Every index receives an independent pass; only same-index overlap is restricted.
                can_take = scanner._passes_correlated_index_guard(sig, sig.direction)

                if can_take:
                    scanner._ensure_trade_like_initial_stop(sig)
                    # All lane/conversion mutations are complete. Calculate the first
                    # immutable verdict from executable prices now; do it again at the
                    # broker worker because quotes and resources can change while queued.
                    policy_touch = (
                        scanner._candidate_execution_touch_quote(sig)
                        if str(getattr(self, "mode", "") or "").upper() == "REAL"
                        else None
                    )
                    policy_levels = scanner._execution_levels(
                        sig,
                        execution_touch_quote=policy_touch,
                    )
                    policy_ok, policy_reason = scanner._entry_policy_allows(
                        sig,
                        execution_levels=policy_levels,
                        phase="post_lane_final",
                    )
                    if not policy_ok:
                        sig.status = "NO ENTRY - FINAL POLICY"
                        scanner._rtracker_record_candidate(
                            sig,
                            decision="REJECTED",
                            reason=policy_reason,
                            source="final_entry_policy",
                        )
                        scanner._remember_missed_reentry_seed(
                            sig,
                            reason=policy_reason,
                            source="final_entry_policy",
                        )
                        continue
                    exposure = scanner._option_exposure_decision(sig, execution_levels=policy_levels)
                    if not exposure["allowed"]:
                        sig.status = "NO ENTRY - SHARED OPTION EXPOSURE"
                        scanner._rtracker_record_candidate(sig, decision="REJECTED",
                            reason=exposure["reason"], source="final_option_exposure")
                        continue
                    # Regime/trend market-context gate for every lane (2026-10-08 user):
                    # shadow-recorded by default, MIDCPNIFTY 15m peer rule enforced.
                    context_ok, context_reason = scanner._entry_context_gate_allows(sig)
                    if not context_ok:
                        sig.status = "NO ENTRY - MARKET CONTEXT"
                        scanner._rtracker_record_candidate(sig, decision="REJECTED",
                            reason=context_reason, source="entry_context_gate")
                        continue
                    # â”€â”€â”€ Manual Signal Gate â”€â”€â”€
                    # S1 stays signal-only under Auto (2026-10-08 audit: S1 lost under every
                    # replayed exit policy); its rows still record as manual signals.
                    signal_only = bool(scanner.auto_mode) and s1_lane_policy.signal_only(sig)
                    if (not scanner.auto_mode or signal_only) and not is_warmup:
                        # The auto path reaches open_trade, which reprices via
                        # _execution_levels and validates the mark. This one does not, so
                        # the same question is asked here: is the price we are about to
                        # record still real? See _candidate_entry_mark_refusal.
                        stale_mark = scanner._candidate_entry_mark_refusal(sig)
                        if stale_mark:
                            sig.status = "NO ENTRY - Stale entry mark"
                            scanner.log_signal_decision_once(
                                f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{ts_key}|stale-mark",
                                f"Signal rejected: {sig.instrument} {sig.direction} "
                                f"{sig.timeframe} - {stale_mark}",
                                "trade",
                            )
                            scanner._rtracker_record_candidate(
                                sig,
                                decision="REJECTED",
                                reason=f"Stale entry mark: {stale_mark}",
                                source="entry_mark_gate",
                            )
                            scanner._remember_trade_candidates([sig])
                            continue
                        scanner._close_superseded_session_entries([sig])
                        scanner._latest_trade_candidates[sig.instrument] = [sig]
                        setattr(sig, "accepted_by_gate", True)
                        # This lane intentionally does not submit an order.  Preserve
                        # the distinction so a later tracked/repaint exit can never be
                        # mistaken for a broker-filled trade by a consumer of the
                        # session-candidate ledger.
                        setattr(sig, "execution_status", "SIGNAL_ONLY")
                        setattr(sig, "execution_state", "NOT_PLACED")
                        setattr(sig, "broker_fill_confirmed", False)
                        scanner._mark_candidate_accepted_time(sig, datetime.now(IST))
                        accepted_candidates.append(sig)
                        scanner._record_accepted_entry(sig)
                        scanner._remember_trade_candidates([sig])
                        await scanner._publish_trade_payload_now()
                        if i == 0: # Only notify for the primary candidate
                            logger.info(f"📣 MANUAL SIGNAL: {sig.direction} {sig.instrument} @ {sig.price} (Auto Mode: Manual)")
                            scanner.log_event(f"🎯 Manual {sig.direction} Signal: {sig.instrument} @ {sig.price:.2f}", "trade")
                            scanner._notify(f"🎯 MANUAL SIGNAL: {sig.direction} {sig.instrument} @ {sig.price:.2f}. Auto-execution disabled.", "info")
                            
                            try:
                                nm = get_notification_manager(asyncio.get_running_loop())
                                execution_loop = asyncio.get_running_loop()
                                
                                def make_executor(candidate_sig, is_ghost=False):
                                    def do_execute():
                                        # Instrument-lock parity with execute_manual_signal (1320):
                                        # this deferred click may fire after the operator switched
                                        # the FUT/OPT preference, so re-enforce the lock at execution
                                        # time. Ghosts are virtual and never reach the broker.
                                        if not is_ghost:
                                            pref_ok, pref_why = scanner._enforce_strict_instrument_pref_on_candidate(candidate_sig)
                                            if not pref_ok:
                                                candidate_sig.status = f"BLOCKED - {scanner._strict_instrument_pref()} PREF ({pref_why})"
                                                return
                                            quality_ok, quality_why, quality_source = scanner._current_manual_admission_decision(
                                                candidate_sig,
                                                lane="MANUAL_TOAST_SUBMIT",
                                            )
                                            if not quality_ok:
                                                candidate_sig.status = "BLOCKED - CURRENT QUALITY POLICY"
                                                setattr(candidate_sig, "accepted_by_gate", False)
                                                scanner._rtracker_record_candidate(
                                                    candidate_sig,
                                                    decision="REJECTED",
                                                    reason=quality_why,
                                                    source=quality_source,
                                                )
                                                return
                                        execution_touch_quote = scanner._candidate_execution_touch_quote(candidate_sig)
                                        exec_price, exec_stop, exec_target = scanner._execution_levels(
                                            candidate_sig,
                                            execution_touch_quote=execution_touch_quote,
                                        )
                                        if exec_price <= 0:
                                            candidate_sig.status = "QUOTE UNAVAILABLE"
                                            return
                                        policy_ok, policy_reason = scanner._entry_policy_allows(
                                            candidate_sig,
                                            execution_levels=(exec_price, exec_stop, exec_target),
                                            phase="manual_toast_submit",
                                        )
                                        if not policy_ok:
                                            candidate_sig.status = "BLOCKED - FINAL ENTRY POLICY"
                                            scanner._rtracker_record_candidate(
                                                candidate_sig,
                                                decision="REJECTED",
                                                reason=policy_reason,
                                                source="manual_final_entry_policy",
                                            )
                                            return
                                        if not is_ghost:
                                            exposure = scanner._option_exposure_decision(
                                                candidate_sig, execution_levels=(exec_price, exec_stop, exec_target))
                                            if not exposure["allowed"]:
                                                scanner._rtracker_record_candidate(candidate_sig, decision="REJECTED",
                                                    reason=exposure["reason"], source="manual_option_exposure")
                                                return
                                        actual_spot = scanner.candles.get_latest_price(candidate_sig.instrument)
                                        
                                        def on_execution_result(executed_trade, accepted, candidate=candidate_sig):
                                            asyncio.run_coroutine_threadsafe(
                                                scanner._handle_broker_entry_result(candidate, executed_trade, accepted),
                                                execution_loop,
                                            )

                                        entry_refusal_outcome: Dict[str, Any] = {}
                                        trade = scanner.trades.open_trade(
                                            instrument=candidate_sig.instrument, timeframe=candidate_sig.timeframe, direction=candidate_sig.direction,
                                            price=exec_price, trailing_stop=exec_stop,
                                            lots=candidate_sig.lots, lot_size=candidate_sig.lot_size, grade=candidate_sig.grade,
                                            atm_strike=candidate_sig.atm_strike, option_type=candidate_sig.option_type,
                                            target=exec_target,
                                            rr_ratio=candidate_sig.rr,
                                            confidence=candidate_sig.confidence,
                                            instrument_multiplier=candidate_sig.multiplier,
                                            trading_symbol=candidate_sig.trading_symbol,
                                            symbol_token=candidate_sig.symbol_token,
                                            inst_type=candidate_sig.inst_type,
                                            market_regime=str(getattr(candidate_sig, "market_regime", "") or ""),
                                            data_provenance=str(getattr(candidate_sig, "data_provenance", "") or ""),
                                            price_action_verdict=str(getattr(candidate_sig, "price_action_verdict", "") or ""),
                                            entry_intelligence_score=float(getattr(candidate_sig, "intelligence_score", 0.0) or 0.0),
                                            strategy_revision=str(getattr(candidate_sig, "strategy_revision", "") or ""),
                                            strategy_direction_mode=str(getattr(candidate_sig, "strategy_direction_mode", "") or ""),
                                            entry_momentum_urgency=scanner._entry_momentum_urgency(candidate_sig),
                                            exec_type="M",
                                            entry_spot=float(getattr(candidate_sig, "underlying_entry", 0.0) or actual_spot or 0.0),
                                            spot_stop=float(getattr(candidate_sig, "underlying_stop", 0.0) or 0.0),
                                            spot_target=float(getattr(candidate_sig, "underlying_target", 0.0) or 0.0),
                                            underlying_entry=float(getattr(candidate_sig, "underlying_entry", 0.0) or actual_spot or 0.0),
                                            underlying_stop=float(getattr(candidate_sig, "underlying_stop", 0.0) or 0.0),
                                            underlying_target=float(getattr(candidate_sig, "underlying_target", 0.0) or 0.0),
                                            premium_entry=float(getattr(candidate_sig, "premium_entry", 0.0) or 0.0),
                                            premium_stop=float(getattr(candidate_sig, "premium_stop", 0.0) or 0.0),
                                            premium_target=float(getattr(candidate_sig, "premium_target", 0.0) or 0.0),
                                            geometry_observed_at=str(getattr(candidate_sig, "geometry_observed_at", "") or ""),
                                            geometry_source=str(getattr(candidate_sig, "geometry_source", "") or ""),
                                            geometry_verified=bool(getattr(candidate_sig, "geometry_verified", False)),
                                            expected_move_forecast=dict(getattr(candidate_sig, "expected_move_forecast", {}) or {}),
                                            entry_policy_verdict=dict(getattr(candidate_sig, "entry_policy_verdict", {}) or {}),
                                            quality_verdict=dict(getattr(candidate_sig, "quality_verdict", {}) or {}),
                                            decision_record=dict(getattr(candidate_sig, "decision_record", {}) or {}),
                                            is_explosive_bypass=bool(getattr(candidate_sig, "is_explosive_bypass", False)),
                                            is_reversion_scalp=bool(getattr(candidate_sig, "is_reversion_scalp", False)),
                                            is_recovery=False,
                                            is_ghost=is_ghost,
                                            entry_time=datetime.now(IST),
                                            signal_time=getattr(candidate_sig, "signal_timestamp", None),
                                            signal_fire_mode=getattr(candidate_sig, "signal_fire_mode", signal_fire_mode),
                                            entered_intracandle=bool(getattr(candidate_sig, "entered_intracandle", False)),
                                            market_wide_reversal_entry=bool(getattr(candidate_sig, "market_wide_reversal_entry", False)),
                                            volume_spike_entry=bool(getattr(candidate_sig, "volume_spike_entry", False)),
                                            power_move=bool(getattr(candidate_sig, "power_move", False)),
                                            source_engine=str(getattr(candidate_sig, "source_engine", "") or ""),
                                            source_engines=list(getattr(candidate_sig, "source_engines", []) or []),
                                            source_lane=str(getattr(candidate_sig, "source_lane", "") or ""),
                                            quad_union_entry=bool(getattr(candidate_sig, "quad_union_entry", False)),
                                            raw_union_entry=bool(getattr(candidate_sig, "raw_union_entry", False)),
                                            initial_stop=exec_stop,
                                            scalp_lock_mode=bool(getattr(candidate_sig, "scalp_lock_mode", False)),
                                            execution_touch_quote=execution_touch_quote,
                                            on_execution_result=on_execution_result,
                                            lifecycle_id=scanner._candidate_lifecycle_key(candidate_sig),
                                            entry_refusal_outcome=entry_refusal_outcome,
                                        )
                                        if trade:
                                            trade.is_live = (scanner.mode == "REAL")
                                            type(scanner)._carry_s1_trade_attrs(trade, candidate_sig)
                                            candidate_sig.status = "ORDER PENDING" if not is_ghost else "GHOST WATCH"
                                            asyncio.create_task(scanner._publish_trade_payload_now())
                                        else:
                                            scanner._apply_trade_manager_entry_refusal(
                                                candidate_sig, entry_refusal_outcome
                                            )
                                            asyncio.create_task(scanner._publish_trade_payload_now())
                                            
                                    return do_execute

                                def make_watcher(candidate_sig):
                                    def do_watch():
                                        candidate_sig.status = "WATCHING"
                                        setattr(candidate_sig, "watch_only", True)
                                        scanner._remember_trade_candidates([candidate_sig])
                                        scanner.log_event(
                                            f"Watching {candidate_sig.instrument} {candidate_sig.direction} "
                                            f"{candidate_sig.timeframe} without broker execution.",
                                            "trade",
                                        )
                                        asyncio.create_task(scanner._publish_trade_payload_now())

                                    return do_watch
                                
                                nm.send_trade_notification(
                                    signal_type="BUY" if sig.inst_type == "OPT" else ("BUY" if sig.direction == "LONG" else "SELL"),
                                    instrument=sig.instrument,
                                    entry_price=sig.price,
                                    target_price=sig.target,
                                    sl_price=sig.stop,
                                    rr_ratio=sig.rr,
                                    grade=sig.grade,
                                    confidence=getattr(sig, 'confidence', 0.0) * 100,
                                    timeframe=sig.timeframe,
                                    on_execute=make_executor(sig, is_ghost=False),
                                    on_watch=make_watcher(sig)
                                )
                            except Exception as e:
                                logger.error(f"Failed to dispatch toast notification: {e}")
                                
                        continue # Don't take the trade in manual mode

                    # TradeManager.open_trade is the single broker execution path.
                    actual_spot = scanner.candles.get_latest_price(sig.instrument)
                    
                    sig_ts = getattr(sig, "signal_timestamp", None)
                    sig_ts_str = sig_ts.strftime('%H:%M:%S') if sig_ts else "--:--:--"
                    exec_ts_str = datetime.now(IST).strftime('%H:%M:%S')
                    logger.info(f"⏱️ Exact Execution Trigger: Signal={sig_ts_str} + 20s Buffer + Latency -> Entry={exec_ts_str}")
                    
                    # Resolve entries in the traded contract's price space.
                    execution_touch_quote = scanner._candidate_execution_touch_quote(sig)
                    exec_price, exec_stop, exec_target = scanner._execution_levels(
                        sig,
                        execution_touch_quote=execution_touch_quote,
                    )
                    if exec_price <= 0:
                        sig.status = "QUOTE UNAVAILABLE"
                        scanner.log_event(
                            f"Signal blocked: {sig.instrument} {sig.trading_symbol} has no verified live premium",
                            "warning",
                        )
                        scanner._rtracker_record_candidate(
                            sig,
                            decision="REJECTED",
                            reason="No verified live premium at execution",
                            source="execution_quote_gate",
                        )
                        continue
                    policy_ok, policy_reason = scanner._entry_policy_allows(
                        sig,
                        execution_levels=(exec_price, exec_stop, exec_target),
                        phase="auto_pre_submit",
                    )
                    if not policy_ok:
                        sig.status = "BLOCKED - FINAL ENTRY POLICY"
                        scanner._rtracker_record_candidate(
                            sig,
                            decision="REJECTED",
                            reason=policy_reason,
                            source="auto_final_entry_policy",
                        )
                        continue
                    exposure = scanner._option_exposure_decision(
                        sig, execution_levels=(exec_price, exec_stop, exec_target))
                    if not exposure["allowed"]:
                        sig.status = "BLOCKED - SHARED OPTION EXPOSURE"
                        scanner._rtracker_record_candidate(sig, decision="REJECTED",
                            reason=exposure["reason"], source="auto_option_exposure")
                        continue
                    spot_stop = float(getattr(sig, "underlying_stop", 0.0) or 0.0)
                    spot_target = float(getattr(sig, "underlying_target", 0.0) or 0.0)

                    execution_loop = asyncio.get_running_loop()

                    def on_execution_result(executed_trade, accepted, candidate=sig):
                        asyncio.run_coroutine_threadsafe(
                            scanner._handle_broker_entry_result(candidate, executed_trade, accepted),
                            execution_loop,
                        )

                    # Final, fresh check at the broker-execution boundary. Earlier
                    # readiness checks may be seconds old after buffering, quote refresh,
                    # and concurrency work; memory pressure can cross the line meanwhile.
                    resource_blockers = type(scanner)._auto_entry_resource_blockers(self)
                    if resource_blockers:
                        reason = "; ".join(resource_blockers)
                        sig.status = "BLOCKED - HOST RESOURCE PRESSURE"
                        scanner.log_event(
                            f"Auto entry blocked at execution boundary for {sig.instrument}: {reason}",
                            "warning",
                        )
                        scanner._rtracker_record_candidate(
                            sig,
                            decision="REJECTED",
                            reason=reason,
                            source="final_resource_pressure_gate",
                        )
                        continue

                    entry_refusal_outcome: Dict[str, Any] = {}
                    trade = scanner.trades.open_trade(
                        instrument=sig.instrument, timeframe=sig.timeframe, direction=sig.direction,
                        price=exec_price, trailing_stop=exec_stop,
                        lots=sig.lots, lot_size=sig.lot_size, grade=sig.grade,
                        atm_strike=sig.atm_strike, option_type=sig.option_type,
                        target=exec_target,
                        rr_ratio=sig.rr,
                        confidence=sig.confidence,
                        instrument_multiplier=sig.multiplier,
                        trading_symbol=sig.trading_symbol,
                        symbol_token=sig.symbol_token,
                        inst_type=sig.inst_type,
                        market_regime=str(getattr(sig, "market_regime", "") or ""),
                        data_provenance=str(getattr(sig, "data_provenance", "") or ""),
                        price_action_verdict=str(getattr(sig, "price_action_verdict", "") or ""),
                        entry_intelligence_score=float(getattr(sig, "intelligence_score", 0.0) or 0.0),
                        strategy_revision=str(getattr(sig, "strategy_revision", "") or ""),
                        strategy_direction_mode=str(getattr(sig, "strategy_direction_mode", "") or ""),
                        entry_momentum_urgency=scanner._entry_momentum_urgency(sig),
                        exec_type="A",
                        entry_spot=float(getattr(sig, "underlying_entry", 0.0) or actual_spot or 0.0),
                        spot_stop=spot_stop,
                        spot_target=spot_target,
                        underlying_entry=float(getattr(sig, "underlying_entry", 0.0) or actual_spot or 0.0),
                        underlying_stop=spot_stop,
                        underlying_target=spot_target,
                        premium_entry=float(getattr(sig, "premium_entry", 0.0) or 0.0),
                        premium_stop=float(getattr(sig, "premium_stop", 0.0) or 0.0),
                        premium_target=float(getattr(sig, "premium_target", 0.0) or 0.0),
                        geometry_observed_at=str(getattr(sig, "geometry_observed_at", "") or ""),
                        geometry_source=str(getattr(sig, "geometry_source", "") or ""),
                        geometry_verified=bool(getattr(sig, "geometry_verified", False)),
                        expected_move_forecast=dict(getattr(sig, "expected_move_forecast", {}) or {}),
                        entry_policy_verdict=dict(getattr(sig, "entry_policy_verdict", {}) or {}),
                        quality_verdict=dict(getattr(sig, "quality_verdict", {}) or {}),
                        decision_record=dict(getattr(sig, "decision_record", {}) or {}),
                        is_reversion_scalp=bool(getattr(sig, "is_reversion_scalp", False)),
                        # A fresh entry is NEVER a recovery. This passed is_warmup, which
                        # only means the scanner is still loading data — unrelated. When
                        # true, can_open_trade skips BOTH the orphan block and the
                        # max-concurrent-positions cap, and the trade is written
                        # execution_status="OPEN" with pending_execution=False, i.e.
                        # already established rather than awaiting a fill. Auto Mode is
                        # not persisted, so an operator re-enabling it seconds after a
                        # restart lands squarely inside _background_initial_setup.
                        # The other two open_trade call sites both pass False.
                        is_recovery=False,
                        entry_time=datetime.now(IST),
                        signal_time=getattr(sig, "signal_timestamp", None),
                        signal_fire_mode=getattr(sig, "signal_fire_mode", signal_fire_mode),
                        entered_intracandle=bool(getattr(sig, "entered_intracandle", False)),
                        market_wide_reversal_entry=bool(getattr(sig, "market_wide_reversal_entry", False)),
                        volume_spike_entry=bool(getattr(sig, "volume_spike_entry", False)),
                        power_move=bool(getattr(sig, "power_move", False)),
                        source_engine=str(getattr(sig, "source_engine", "") or ""),
                        source_engines=list(getattr(sig, "source_engines", []) or []),
                        source_lane=str(getattr(sig, "source_lane", "") or ""),
                        quad_union_entry=bool(getattr(sig, "quad_union_entry", False)),
                        raw_union_entry=bool(getattr(sig, "raw_union_entry", False)),
                        initial_stop=exec_stop,
                        scalp_lock_mode=bool(getattr(sig, "scalp_lock_mode", False)),
                        execution_touch_quote=execution_touch_quote,
                        on_execution_result=on_execution_result,
                        lifecycle_id=scanner._candidate_lifecycle_key(sig),
                        entry_submission_guard=scanner._queued_auto_entry_submission_guard,
                        entry_refusal_outcome=entry_refusal_outcome,
                    )

                    if trade:
                        trade.is_live = (scanner.mode == "REAL")
                        type(scanner)._carry_s1_trade_attrs(trade, sig)
                        setattr(sig, "accepted_by_gate", False)
                        scanner._mark_candidate_accepted_time(sig, getattr(trade, "entry_time", None))
                        sig.status = "ORDER PENDING"
                        await scanner._publish_trade_payload_now()
                        open_count += 1
                        continue
                    else:
                        ts = getattr(sig, "signal_timestamp", None)
                        ts_key = ts.isoformat(timespec="seconds") if ts else ""
                        rejection_reason = scanner._apply_trade_manager_entry_refusal(
                            sig, entry_refusal_outcome
                        )
                        scanner.log_signal_decision_once(
                            f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{ts_key}|trade-manager",
                            f"Signal rejected: {sig.instrument} {sig.direction} "
                            f"{sig.timeframe} - {rejection_reason}",
                            "trade",
                        )
                else:
                    ts = getattr(sig, "signal_timestamp", None)
                    ts_key = ts.isoformat(timespec="seconds") if ts else ""
                    scanner.log_signal_decision_once(
                        f"{sig.instrument}|{sig.timeframe}|{sig.direction}|{ts_key}|correlation",
                        f"Signal rejected: {sig.instrument} {sig.direction} {sig.timeframe} - correlated-index exposure limit reached",
                        "trade",
                    )
                    scanner._rtracker_record_candidate(
                        sig,
                        decision="REJECTED",
                        reason="Correlated-index exposure limit reached",
                        source="correlation_gate",
                    )
                    logger.info(
                        f"Correlation Guard rejected {sig.instrument} {sig.direction} "
                        f"(Current {sig.direction} Exposure: {open_count})"
                    )

    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
    # CHART BUILDER — uses CACHED results (zero re-processing)
    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
        scanner._finalize_rejected_alternatives(signals)
        return accepted_candidates
