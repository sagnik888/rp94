"""Tick-driven exit management, independent of entry-analysis scheduling."""

from __future__ import annotations

import copy
from datetime import datetime

from loguru import logger

from engine import s1_lane as s1_lane_policy
from engine import profit_lock_hold as profit_lock_hold_policy
from engine.market_regime import is_trending_regime, uses_tight_trailing
from trading.trade_manager import IST


class ExitCycleService:
    """Owns active-trade evaluation while TradeManager owns the trade book."""

    def __init__(self, scanner=None) -> None:
        self.scanner = scanner

    def run_tick(self, scanner=None) -> None:
        if scanner is not None:
            self.scanner = scanner
        data = getattr(self.scanner, "data", None)
        prioritize = getattr(data, "set_active_contract_tick_tokens", None)
        if callable(prioritize):
            prioritize(
                getattr(trade, "symbol_token", "")
                for trade in (getattr(getattr(self.scanner, "trades", None), "open_trades", {}) or {}).values()
                if str(getattr(trade, "status", "OPEN")).upper() == "OPEN"
            )
        self.update_active_trades(allow_blocking=False)

    def update_active_trades(self, allow_blocking: bool = True):
        scanner = self.scanner
        self = scanner  # preserve bare-host arguments used by legacy static helpers
        scanner._reset_daily_counters()
        for tid, trade in list(scanner.trades.open_trades.items()):
            if (str(getattr(scanner, "mode", "")).upper() == "HISTORICAL"
                    and not bool(getattr(trade, "is_live", False))
                    and str(tid).startswith("H_UTEMA_")
                    and getattr(trade, "source_engine", "") == "ut_ema"
                    and getattr(trade, "exit_policy_owner", "") == "UTEMA"
                    and getattr(trade, "data_provenance", "") == "BACKTEST_OBSERVED_OPTION_MINUTES"):
                # The causal replay owns this simulated row and its replay clock.
                # Wall-clock marks must neither reprice it nor consume event-loop time.
                continue
            # Per-pass authority only: a previous HOLD/acceleration must never leak
            # into a later update where the scanner could not evaluate the guard.
            early_loss_cut_decision = None
            # Skip closed / already-latched manual exits (prevents PEAK/exit spam)
            if str(getattr(trade, "status", "") or "").upper() == "CLOSED":
                continue
            # RECOVERY_REQUIRED trades await broker reconciliation / EOD flatten —
            # re-firing strategy exits on them only spams CRITICAL alerts
            # (2026-07-10: PEAK_THREAT loop at ~20/sec over stale rows).
            if bool(getattr(trade, "pending_exit", False)) or str(
                getattr(trade, "execution_status", "") or ""
            ).upper() in {"EXIT_PENDING", "PARTIAL_EXIT", "RECOVERY_REQUIRED"}:
                continue

            observed_contract_ticks = scanner._trade_contract_tick_path(trade)
            contract_stop_before_cycle = float(
                getattr(trade, "current_stop", 0.0) or 0.0
            )
            real_premium = None
            real_fut_price = None
            quote_verified = True
            is_fut_trade = getattr(trade, "inst_type", "FUT") == "FUT"
            # A futures contract quote is an executable mark in its own right.  Do not
            # discard it merely because the underlying/index spot feed is temporarily
            # absent. Conversely, no fresh contract quote and no spot means no safe
            # price for this pass.
            price = scanner.candles.get_latest_price(trade.instrument)
            if is_fut_trade:
                real_fut_price = scanner._get_live_fut_ltp_cached(
                    trade, allow_blocking=allow_blocking
                )
                if observed_contract_ticks:
                    # Exact socket path is newer than a slower fallback cache.
                    real_fut_price = float(observed_contract_ticks[-1][0])
                if (price is None or price <= 0) and not (real_fut_price and real_fut_price > 0):
                    continue
                if price is None or price <= 0:
                    price = float(real_fut_price)
            elif getattr(trade, "inst_type", "FUT") == "OPT":
                real_premium = scanner._get_live_premium_cached(trade, allow_blocking=allow_blocking)
                if observed_contract_ticks:
                    # Preserve exact newest contract mark even when a 2s REST/chain
                    # fallback exists. The full path is passed below for hidden touches.
                    real_premium = float(observed_contract_ticks[-1][0])
                if price is None or price <= 0:
                    if not (real_premium and real_premium > 0):
                        continue
                    # Contract premium remains executable while index spot is briefly
                    # unavailable. Hold underlying geometry at entry; premium-led core
                    # stop/target management continues from exact ticks.
                    price = float(
                        getattr(trade, "underlying_entry", 0.0)
                        or getattr(trade, "entry_spot", 0.0)
                        or 0.0
                    )
            elif price is None or price <= 0:
                continue
            
            # Update current_price
            trade_price = float(price)
            if is_fut_trade:
                if real_fut_price and real_fut_price > 0:
                    trade_price = float(real_fut_price)
                elif getattr(trade, "entry_spot", 0.0) > 0:
                    trade_price = scanner._fut_price_from_spot_level(trade, float(price))
                trade.current_price = trade_price
            elif getattr(trade, "inst_type", "FUT") == "OPT":
                mark, mark_source, quote_verified = scanner._active_option_decision_mark(
                    trade,
                    float(price),
                    real_premium,
                )
                trade.current_price = float(mark)
                setattr(trade, "quote_source", mark_source)
                setattr(trade, "quote_verified", bool(quote_verified))
                if real_premium is not None:
                    # Hand TradeManager the VETTED mark, not the raw LTP.
                    # _active_option_decision_mark exists to reject a stale contract
                    # quote and substitute a synthetic one, and this scanner honours it
                    # for its own stop check below. But update_trade was still being
                    # passed the raw premium, and trade_manager.py overwrites
                    # trade.current_price with it — its only guard there is an upper
                    # plausibility bound (~spot*0.12), not a staleness test, so a
                    # rejected quote sailed through and could trigger EARLY_LOSS_CUT or
                    # TRAILING_STOP at a price this function had already refused.
                    # Both directions cost money: a stale low stops out a winner, a
                    # stale high suppresses a stop that should fire.
                    real_premium = float(mark)

            opt_val = trade.current_price if getattr(trade, "inst_type", "FUT") == "OPT" else trade_price
            observed_prices = [float(item[0]) for item in observed_contract_ticks]
            if observed_prices:
                observed_favorable_price = (
                    max(observed_prices)
                    if getattr(trade, "inst_type", "FUT") == "OPT"
                    or str(getattr(trade, "direction", "LONG") or "LONG").upper() == "LONG"
                    else min(observed_prices)
                )
            else:
                observed_favorable_price = float(opt_val or 0.0)

            # Define conversion helpers for this trade
            def prem_to_spot(prem_val):
                if getattr(trade, "inst_type", "FUT") != "OPT":
                    return prem_val
                mult = abs(getattr(trade, "instrument_multiplier", 0.5)) or 0.5
                spot_dist = (trade.entry_price - prem_val) / mult
                if trade.direction == "LONG":
                    return trade.entry_spot - spot_dist
                else:
                    return trade.entry_spot + spot_dist

            def spot_to_prem(spot_val):
                if getattr(trade, "inst_type", "FUT") != "OPT":
                    return spot_val
                mult = getattr(trade, "instrument_multiplier", 0.5)
                spot_change = spot_val - trade.entry_spot
                return trade.entry_price + (spot_change * mult)

            regime = scanner._get_trade_regime(trade)
            latest_time = (
                datetime.now(IST).replace(tzinfo=None)
                if getattr(self, "mode", "") != "HISTORICAL"
                else scanner.candles.get_max_timestamp()
            )

            if (
                str(getattr(trade, "inst_type", "FUT") or "FUT").upper() == "OPT"
                and not bool(quote_verified)
            ):
                # A delta/theta reconstruction keeps the dashboard legible while the
                # contract feed is missing, but it is not an executable option mark.
                # Stop this pass before any intelligence, runner, peak-P&L, profit-lock,
                # or strategy-exit state can be mutated from that synthetic number.
                # TradeManager still gets the heartbeat so broker-side protection and
                # persistence health continue to be monitored. The independent hard-EOD
                # sweep runs before this method, so forced risk exits remain available.
                scanner.trades.update_trade(
                    tid,
                    price,
                    float(getattr(trade, "current_stop", 0.0) or 0.0),
                    current_time=latest_time,
                    real_premium=float(opt_val or 0.0),
                    quote_verified=False,
                    allow_broker_protection=allow_blocking,
                    observed_contract_ticks=observed_contract_ticks,
                )
                scanner._commit_trade_contract_tick_path(trade)
                continue

            # Live unrealized for distress reversion + intel exit voting.
            try:
                live_unreal = float(
                    getattr(trade, "unrealized_pnl", None)
                    if getattr(trade, "unrealized_pnl", None) is not None
                    else getattr(trade, "pnl", 0.0) or 0.0
                )
            except Exception:
                live_unreal = 0.0
            if live_unreal == 0.0:
                try:
                    entry_px = float(getattr(trade, "entry_price", 0.0) or 0.0)
                    mark_px = float(opt_val or 0.0)
                    qty = float(
                        getattr(trade, "quantity", None)
                        or getattr(trade, "qty", None)
                        or getattr(trade, "lots", 1)
                        or 1
                    )
                    lot_mult = float(getattr(trade, "lot_size", 1) or 1)
                    if entry_px > 0 and mark_px > 0:
                        # Options are always BOUGHT, so a premium RISE is profit for a
                        # PE as much as a CE. Reading direction alone reported a winning
                        # PE as a loser, and live_unreal feeds the distress-reversion arm
                        # and the is_losing flag on the OI/PCR reversal vote.
                        signed = (mark_px - entry_px) if (
                            str(getattr(trade, "inst_type", "")).upper() == "OPT"
                            or str(getattr(trade, "direction", "")).upper() == "LONG"
                        ) else (entry_px - mark_px)
                        # OPT qty often already includes lot; keep simple Rs estimate.
                        live_unreal = signed * abs(qty) * (lot_mult if abs(qty) < 50 else 1.0)
                except Exception:
                    pass

            # Frozen exit owner (stamped at admission), not the admission switch.
            if s1_lane_policy.is_managed_lane(trade):
                # Lane: its own exits only. S1: index SL1 with the T1 lock, the S1 engine's
                # EXIT for this signal, the profit-lock giveback) plus the premium guard held
                # as the hard stop. The other engines' guards, locks and floors below do not
                # apply to an S1 trade. Exit AUTHORITY is unchanged: Manual mode only alerts
                # through _close_and_record, and a live position is never closed while the
                # scanner runs HISTORICAL.
                s1_result = (
                    getattr(getattr(self, "mtf", None), "_result_cache", {}) or {}
                ).get(f"{trade.instrument}_{getattr(trade, 'timeframe', '')}")
                if s1_lane_policy.is_utema(trade):
                    from engine.ut_ema_lane import result_for_monitor
                    s1_result = result_for_monitor(scanner, trade)
                managed_lane = s1_lane_policy.managed_lane(trade)
                s1_bookkeeping = (getattr(trade, "s1_best_spot", None),
                                  getattr(trade, "s1_t1_locked", False),
                                  getattr(trade, "s1_peak_premium", None),
                                  getattr(trade, "uts_confirmed", False),
                                  getattr(trade, "uts_repaint_exit_required", False))
                s12_bookkeeping = (
                    copy.deepcopy(tuple(getattr(trade, key, None) for key in s1_lane_policy.SESSION_STATE_FIELDS))
                    if managed_lane in {"D1", "D2", "UTEMA"} else None
                )
                s1_decision = s1_lane_policy.exit_decision(
                    trade,
                    float(price or 0.0),
                    float(opt_val or 0.0),
                    now=latest_time,
                    engine_exit=s1_lane_policy.engine_exit_for(trade, s1_result, now=latest_time),
                    trend=s1_lane_policy.closed_bar_trend(trade, s1_result),
                    trend_evidence=s1_lane_policy.closed_bar_trend_evidence(
                        trade, s1_result, now=latest_time
                    ),
                    regime=regime,
                    market=s1_lane_policy.closed_bar_state(trade, s1_result),
                    observed_contract_ticks=observed_contract_ticks,
                )
                from engine import uts_lane
                if s1_lane_policy.managed_lane(trade) == uts_lane.LANE_TAG and not s1_decision.exit and scanner.auto_mode and quote_verified:
                    try:
                        proposed = uts_lane.extension_stop(trade, float(opt_val or 0.0),
                            result=s1_result, context=scanner._uts_extension_context(trade), now=latest_time)
                        old_stop = float(getattr(trade, "current_stop", 0.0) or 0.0)
                        touched = any(float(tick[0]) <= old_stop for tick in (observed_contract_ticks or []))
                        if proposed > 0 and not touched and not (getattr(trade, "is_live", False) and scanner.mode == "HISTORICAL"):
                            scanner.trades.request_uts_stop_extension(trade, proposed,
                                quote_verified=quote_verified, now=latest_time)
                    except (AttributeError, TypeError, ValueError):
                        pass  # Missing current evidence keeps the original tighter stop.
                if s1_decision.target_premium > 0:
                    lowered = s1_lane_policy.nearer_target(
                        float(getattr(trade, "target", 0.0) or 0.0),
                        s1_decision.target_premium,
                        1 if getattr(trade, "inst_type", "OPT") == "OPT"
                        else (1 if str(trade.direction).upper() == "LONG" else -1),
                    )
                    if lowered != float(getattr(trade, "target", 0.0) or 0.0):
                        trade.target = lowered
                        scanner.log_event(
                            f"S1 target for {trade.instrument} {getattr(trade, 'timeframe', '')} "
                            f"lowered to {lowered:.2f} (0.7R): the move has gone quiet",
                            "trade",
                        )
                # While the S1 trend is with the trade the T3 target does not close it;
                # the premium trail and the profit lock do.
                trade.runner_mode = bool(s1_decision.runner)
                if s1_bookkeeping != (getattr(trade, "s1_best_spot", None),
                                      getattr(trade, "s1_t1_locked", False),
                                      getattr(trade, "s1_peak_premium", None),
                                      getattr(trade, "uts_confirmed", False),
                                      getattr(trade, "uts_repaint_exit_required", False)):
                    mark_dirty = getattr(scanner.trades, "_mark_dirty", None)
                    if callable(mark_dirty):
                        mark_dirty()
                if s12_bookkeeping is not None and s12_bookkeeping != tuple(
                    getattr(trade, key, None) for key in s1_lane_policy.SESSION_STATE_FIELDS
                ):
                    mark_dirty = getattr(scanner.trades, "_mark_dirty", None)
                    if callable(mark_dirty):
                        mark_dirty()
                s1_is_opt = getattr(trade, "inst_type", "FUT") == "OPT"
                s1_tm_stop = float(getattr(trade, "current_stop", 0.0) or 0.0)
                if s1_is_opt and s1_decision.index_stop > 0 and managed_lane not in {"D1", "D2", "UTEMA"}:
                    # Ratchet only: the T1 lock lifts the premium stop above entry.
                    s1_tm_stop = max(s1_tm_stop, float(spot_to_prem(s1_decision.index_stop)))
                if s1_is_opt and s1_decision.premium_stop > 0:
                    # The broker stop trails the premium peak by the guard %.
                    s1_tm_stop = max(s1_tm_stop, float(s1_decision.premium_stop))
                if not s1_is_opt and managed_lane in {"D1", "D2"} and s1_decision.index_stop > 0:
                    # Preserve the entry futures/index basis as the engine ratchets its
                    # native index stop; TradeManager then updates exchange protection.
                    basis = float(getattr(trade, "entry_price", 0.0) or 0.0) - float(
                        getattr(trade, "entry_spot", 0.0) or 0.0
                    )
                    native_contract_stop = float(s1_decision.index_stop) + basis
                    if str(getattr(trade, "direction", "LONG") or "LONG").upper() == "LONG":
                        s1_tm_stop = max(s1_tm_stop, native_contract_stop)
                    elif s1_tm_stop <= 0 or native_contract_stop < s1_tm_stop:
                        s1_tm_stop = native_contract_stop
                s1_trigger = s1_decision.reason if s1_decision.exit else ""
                s1_target = 0.0 if s1_decision.runner else float(getattr(trade, "target", 0.0) or 0.0)
                if not s1_trigger and s1_is_opt:
                    if float(opt_val or 0.0) <= s1_tm_stop:
                        s1_trigger = "TRAILING_STOP"
                    elif s1_target > 0 and float(opt_val or 0.0) >= s1_target:
                        s1_trigger = "TARGET_HIT"
                if getattr(trade, "is_live", False) and scanner.mode == "HISTORICAL":
                    if s1_trigger:
                        marker = f"S1:{s1_trigger}:{latest_time}"
                        if getattr(trade, "_s1_live_alert_marker", "") != marker:
                            setattr(trade, "_s1_live_alert_marker", marker)
                            lane_label = f"S1+S2 Dual {managed_lane}" if managed_lane in {"D1", "D2"} else "S1"
                            msg = (
                                f"🚨 ALERT: Live {lane_label} trade {trade.instrument} requires EXIT "
                                f"({s1_trigger}) at {float(opt_val or 0.0):.2f}!"
                            )
                            logger.warning(msg)
                            if scanner.on_notification:
                                scanner.on_notification(msg, "warning")
                    if (s1_is_opt or managed_lane in {"D1", "D2"}) and s1_tm_stop != float(
                        getattr(trade, "current_stop", 0.0) or 0.0
                    ):
                        trade.current_stop = s1_tm_stop
                    continue
                if s1_decision.exit or (s1_trigger and not scanner.auto_mode):
                    lane_label = f"S1+S2 Dual {managed_lane}" if managed_lane in {"D1", "D2"} else "S1"
                    scanner.log_event(
                        f"{lane_label} exit for {trade.instrument} {getattr(trade, 'timeframe', '')}: "
                        f"{s1_trigger} ({s1_decision.detail})",
                        "trade",
                    )
                    scanner._close_and_record(tid, opt_val, s1_trigger, exit_time=latest_time)
                    scanner._commit_trade_contract_tick_path(trade)
                    continue
                if not scanner.auto_mode:
                    if (s1_is_opt or managed_lane in {"D1", "D2"}) and s1_tm_stop != float(
                        getattr(trade, "current_stop", 0.0) or 0.0
                    ):
                        trade.current_stop = s1_tm_stop
                    scanner._commit_trade_contract_tick_path(trade)
                    continue
                scanner.trades.update_trade(
                    tid,
                    price,
                    s1_tm_stop,
                    current_time=latest_time,
                    real_premium=real_premium,
                    real_fut_price=real_fut_price,
                    quote_verified=quote_verified,
                    allow_broker_protection=allow_blocking,
                    observed_contract_ticks=observed_contract_ticks,
                    # The EARLY_LOSS_CUT backstop is another engine's guard; S1's premium
                    # guard is the hard stop. Stop/target and broker protection still run.
                    early_loss_cut_decision=False,
                )
                scanner._commit_trade_contract_tick_path(trade)
                if tid not in scanner.trades.open_trades:
                    scanner._record_trade_manager_close(tid, trade, price, latest_time)
                continue

            fast_exit, fast_exit_reason = scanner._fifteen_min_one_min_support_exit_decision(
                trade,
                float(price or 0.0),
                latest_time,
                current_mark=float(opt_val or 0.0),
                current_pnl_rs=live_unreal,
                peak_pnl_rs=float(getattr(trade, "peak_pnl", 0.0) or 0.0),
                observed_contract_ticks=observed_contract_ticks,
            )
            if fast_exit:
                scanner.log_event(
                    f"Early 15m exit for {trade.instrument}: {fast_exit_reason}",
                    "trade",
                )
                scanner._close_and_record(
                    tid,
                    opt_val,
                    "TF15_FAST_REVERSAL",
                    exit_time=latest_time,
                )
                continue

            # Open-trade distress: underwater + reverse/fast opposite → reversion window.
            try:
                scanner._maybe_arm_open_distress_reversion(
                    trade,
                    unrealized_pnl=live_unreal,
                    latest_time=latest_time,
                )
            except Exception:
                pass

            # ── Intelligence-Based Early Exit (Patch) ──
            from config.settings import get_settings
            settings = get_settings()
            if getattr(settings, "ut_intel_early_exit", False):
                # allow_blocking=False means "you are on the 100ms scanner event loop".
                # It was threaded to the premium fetch and stopped there, so this call
                # went on recomputing the full intelligence bundle -- measured at 48ms
                # with a 40-strike chain -- inside a 100ms budget, every time the cache
                # expired. Read the cache on the fast path and let the next unhurried
                # pass do the work; an early exit that waits one cycle is not worse than
                # a scanner loop that misses its cadence.
                intel_result = (
                    (
                        # Keep the cache probe explicit at this boundary. The
                        # blocking reader below may still recompute the bundle;
                        # this probe is non-authoritative and makes the split
                        # auditable in source and traces.
                        scanner._cached_timeframe_intel(
                            str(getattr(trade, "instrument", "") or "").upper(),
                            str(getattr(trade, "timeframe", "5min") or "5min").lower(),
                            allow_fallback=False,
                        ),
                        scanner._get_trade_intel(trade),
                    )[1]
                    if allow_blocking
                    else (
                        scanner._fresh_exact_trade_intel(
                            trade,
                            evaluation_time=latest_time,
                            allow_blocking=False,
                        )
                        if scanner._cached_timeframe_intel(
                            str(getattr(trade, "instrument", "") or "").upper(),
                            str(getattr(trade, "timeframe", "5min") or "5min").lower(),
                            allow_fallback=False,
                        )
                        is not None
                        else None
                    )
                )
                if intel_result is None and not allow_blocking:
                    scanner._diag_inc("intel", "fast_path_stale_skipped")
                if intel_result is not None and not allow_blocking:
                    # Keep an explicit age guard at the 100ms boundary as well as in
                    # the exact-timeframe reader.  This protects the close decision if
                    # a future refactor supplies a cached bundle through another seam.
                    try:
                        max_age = float(
                            getattr(get_settings(), "loss_guard_intel_max_age_seconds", 90.0)
                            or 90.0
                        )
                    except (TypeError, ValueError):
                        max_age = 90.0
                    age = scanner._intel_age_seconds(
                        intel_result,
                        str(getattr(trade, "instrument", "") or "").upper(),
                        latest_time,
                    )
                    if age is not None and age > max_age:
                        scanner._diag_inc("intel", "fast_path_stale_skipped")
                        intel_result = None
                if intel_result:
                    # is_losing uses live unrealized for 2-vote OI/PCR (gap2).
                    # unrealized_pnl is NET of estimated charges, so it opens at
                    # minus the round trip — see _position_is_underwater.
                    reversal_reason = scanner._intel_reversal_reason(
                        trade.direction,
                        intel_result,
                        is_losing=scanner._position_is_underwater(trade, live_unreal),
                    )
                    if reversal_reason:
                        fresh, stale_text = scanner._intel_reversal_is_new_evidence(
                            trade, trade.direction, intel_result
                        )
                        if not fresh:
                            marker = f"{latest_time:%H:%M}:noflip"
                            if getattr(trade, "_last_stale_intel_exit", "") != marker:
                                setattr(trade, "_last_stale_intel_exit", marker)
                                scanner.log_event(
                                    f"Held {trade.instrument} {trade.timeframe} through OI/PCR flip; "
                                    f"not a reversal ({stale_text}).",
                                    "info",
                                )
                            reversal_reason = None
                    if reversal_reason and scanner._intel_exit_blocked_by_trend(trade, regime):
                        marker = f"{latest_time:%H:%M}:trendskip"
                        if getattr(trade, "_last_trend_skip_intel_exit", "") != marker:
                            setattr(trade, "_last_trend_skip_intel_exit", marker)
                            scanner.log_event(
                                f"Held {trade.instrument} {trade.timeframe} through contrarian OI/PCR flip; "
                                f"regime {regime} aligned with {trade.direction}.",
                                "info",
                            )
                        reversal_reason = None
                    if reversal_reason:
                        defer_exit, defer_text = scanner._should_defer_option_intel_exit(
                            trade,
                            opt_val,
                            latest_time,
                            reversal_reason,
                            regime=regime,
                        )
                        if defer_exit:
                            scanner.log_event(
                                f"Deferred intelligence exit for {trade.instrument} {trade.timeframe}: {reversal_reason}; {defer_text}.",
                                "info",
                            )
                        else:
                            logger.info(f"🚨 OI/PCR Reversal Exit for {trade.instrument} {trade.direction}: {reversal_reason}")
                            scanner._close_and_record(tid, opt_val, "OI_PCR_REVERSAL", exit_time=latest_time)
                            continue
                    agg = intel_result.get("aggregate", {})
                    score = agg.get("score", 0.0)

                    if trade.direction == "LONG" and score < -40.0:
                        reason = f"Intelligence flip score {score:.1f}"
                        defer_exit, defer_text = scanner._should_defer_option_intel_exit(
                            trade,
                            opt_val,
                            latest_time,
                            reason,
                            regime=regime,
                        )
                        if defer_exit:
                            scanner.log_event(
                                f"Deferred intelligence exit for {trade.instrument} {trade.timeframe}: {reason}; {defer_text}.",
                                "info",
                            )
                        else:
                            logger.info(f"🚨 Intelligence Early Exit for {trade.instrument} LONG: Score={score}")
                            scanner._close_and_record(tid, opt_val, "INTEL_FLIP", exit_time=latest_time)
                            continue
                    elif trade.direction == "SHORT" and score > 40.0:
                        reason = f"Intelligence flip score {score:.1f}"
                        defer_exit, defer_text = scanner._should_defer_option_intel_exit(
                            trade,
                            opt_val,
                            latest_time,
                            reason,
                            regime=regime,
                        )
                        if defer_exit:
                            scanner.log_event(
                                f"Deferred intelligence exit for {trade.instrument} {trade.timeframe}: {reason}; {defer_text}.",
                                "info",
                            )
                        else:
                            logger.info(f"🚨 Intelligence Early Exit for {trade.instrument} SHORT: Score={score}")
                            scanner._close_and_record(tid, opt_val, "INTEL_FLIP", exit_time=latest_time)
                            continue

            # ── Smart Trailing Patch ──
            if getattr(settings, "ut_smart_trailing", False):
                key_tf = f"{trade.instrument}_{trade.timeframe}"
            else:
                key_tf = f"{trade.instrument}_1min"

            engine_tf = scanner.mtf.engines.get(key_tf)
            latest_time = scanner.candles.get_max_timestamp()
            if not engine_tf:
                logger.warning(f"Active trade guard running without UT engine state for {trade.instrument}; trailing skipped, P&L lock still active.")

            state_tf = engine_tf.get_state(key_tf) if engine_tf else None
            raw_ts = state_tf.trailing_stop if state_tf else 0.0

            # Initialize new_stop as spot index level
            if getattr(trade, "inst_type", "FUT") == "OPT":
                new_stop = prem_to_spot(trade.current_stop)
            else:
                new_stop = trade.current_stop

            # ── Adaptive Logic based on Market Regime ──
            # 1. TRENDING (Normal ATR Trailing)
            if is_trending_regime(regime):
                if raw_ts > 0:
                    trade_raw_ts = scanner._fut_price_from_spot_level(trade, raw_ts) if is_fut_trade else raw_ts
                    # Only move stop in favorable direction
                    if trade.direction == "LONG":
                        new_stop = max(new_stop, trade_raw_ts)
                    else:
                        new_stop = min(new_stop, trade_raw_ts)

            # 2. Configured tight-trailing regimes (VOLATILE is deliberately excluded;
            # its measured giveback profile uses the normal guard stack instead).
            elif uses_tight_trailing(regime):
                df_1m = scanner.candles.get_candles(trade.instrument, "1min")
                if df_1m is not None and len(df_1m) > 2:
                    last_low = df_1m['low'].iloc[-2]
                    last_high = df_1m['high'].iloc[-2]

                    if trade.direction == "LONG":
                        # Trail by last candle's low (Tight)
                        trade_last_low = scanner._fut_price_from_spot_level(trade, last_low) if is_fut_trade else last_low
                        new_stop = max(new_stop, trade_last_low)
                    else:
                        # Trail by last candle's high (Tight)
                        trade_last_high = scanner._fut_price_from_spot_level(trade, last_high) if is_fut_trade else last_high
                        new_stop = min(new_stop, trade_last_high)

            # 3. PROFIT PROTECTOR (Lock-in at 1:1 RR)
            # Use entry_spot for options so we compare Spot with Spot
            ref_entry = trade.entry_spot if trade.inst_type == "OPT" else trade.entry_price
            risk_dist = abs(ref_entry - prem_to_spot(trade.trailing_stop)) if trade.inst_type == "OPT" else abs(ref_entry - trade.trailing_stop)
            ref_current = price if trade.inst_type == "OPT" else trade_price
            current_profit_pts = (ref_current - ref_entry) if trade.direction == "LONG" else (ref_entry - ref_current)
            premium_profit_pts = (
                float(trade.current_price or 0.0) - float(trade.entry_price or 0.0)
                if trade.inst_type == "OPT"
                else current_profit_pts
            )
            cascade_ctx = scanner._trade_one_way_cascade_context(trade, latest_time)
            cascade_runner = bool(cascade_ctx and premium_profit_pts > 0)
            if cascade_runner:
                setattr(trade, "one_way_cascade_entry", True)
                setattr(trade, "one_way_cascade_context", dict(cascade_ctx or {}))
                setattr(trade, "scalp_lock_mode", False)
                if not bool(getattr(trade, "runner_mode", False)):
                    trade.runner_mode = True
                    setattr(trade, "stop_provenance", "runner")
                    setattr(trade, "runner_activation_timestamp", latest_time)
                    setattr(
                        trade,
                        "runner_activation_reason",
                        str(cascade_ctx.get("reason", "broad directional cascade")),
                    )
                    trade.original_target_distance = abs(float(trade.target or 0.0) - float(trade.entry_price or 0.0))
                    trade.target = 0
                    marker = f"{latest_time:%H:%M}:{trade.instrument}:{trade.timeframe}"
                    if getattr(trade, "_last_one_way_runner_log", "") != marker:
                        setattr(trade, "_last_one_way_runner_log", marker)
                        scanner.log_event(
                            f"One-way cascade runner enabled for {trade.instrument} {trade.timeframe}: "
                            f"{cascade_ctx.get('reason', 'broad directional cascade')}.",
                            "trade",
                        )

            if current_profit_pts > risk_dist:
                buffer = ref_entry * 0.0005 # Tighten buffer (0.05%)
                old_stop = new_stop
                if trade.direction == "LONG":
                    new_stop = max(new_stop, ref_entry + buffer)
                else:
                    new_stop = min(new_stop, ref_entry - buffer)

                if new_stop != old_stop:
                    scanner.log_event(f"🛡️ Profit Protector: Locking BE for {trade.instrument}", "trade")

            # ── 3b. ADAPTIVE HOLD (adaptive_hold_enabled; OFF by default) ──
            # Cuts a bought option that never turned green, then trails it wide
            # instead of taking profit early. While it holds, the profit-taking exits
            # below stand down; loss protection and the 15:15 force exit do not.
            adaptive_active = False
            adaptive = scanner._adaptive_hold_step(
                trade, latest_time=latest_time, premium=float(opt_val or 0.0)
            )
            if adaptive.action == "EXIT":
                logger.warning(f"{adaptive.reason} for {trade.instrument}: {adaptive.detail}")
                scanner._close_and_record(tid, float(opt_val), adaptive.reason, exit_time=latest_time)
                scanner.log_event(
                    f"Adaptive hold exit: {trade.instrument} {trade.timeframe} ({adaptive.detail})",
                    "trade",
                )
                continue
            if adaptive.action == "HOLD":
                adaptive_active = True
                # HOLD owns new discretionary tightening in this pass. Combining
                # it with the 1m/ATR/BE proposal above defeated the wider trail.
                # Keep every already-active premium stop (including broker risk
                # protection); only discard proposals not yet applied this cycle.
                new_stop = prem_to_spot(max(
                    float(getattr(trade, "current_stop", 0.0) or 0.0),
                    float(adaptive.stop),
                ))

            # ── 4. TARGET TRAILING & RUNNER MODE (T1 -> T2 -> T3) ──
            if not adaptive_active and (trade.target > 0 or getattr(trade, 'runner_mode', False)):
                rr_settings = get_settings()
                runner_unlock_ratio = min(1.0, max(0.50, float(getattr(rr_settings, "dynamic_rr_runner_unlock_ratio", 0.95) or 0.95)))
                runner_lock_pct = scanner._dynamic_runner_lock_pct(
                    float(getattr(rr_settings, "dynamic_rr_runner_lock_pct", 0.90) or 0.90),
                    regime,
                )
                if cascade_runner and isinstance(cascade_ctx, dict):
                    runner_lock_pct = min(
                        0.98,
                        max(0.50, float(cascade_ctx.get("runner_lock_pct", runner_lock_pct) or runner_lock_pct)),
                    )
                # Use entry_spot for options so we compare Spot with Spot
                ref_entry = trade.entry_spot if trade.inst_type == "OPT" else trade.entry_price

                # Check if T1 was already hit and we are in runner mode
                is_runner = getattr(trade, 'runner_mode', False)

                if not is_runner and trade.target > 0:
                    # Logic to activate Runner Mode at T1 (or 90% of T1)
                    if trade.inst_type == "OPT":
                        target_distance = abs(trade.target - trade.entry_price)
                        threshold_t1 = target_distance * runner_unlock_ratio
                        current_move = max(
                            0.0,
                            float(trade.current_price or 0.0) - float(trade.entry_price or 0.0),
                            float(observed_favorable_price or 0.0) - float(trade.entry_price or 0.0),
                        )
                        is_correct_direction = current_move > 0.0
                    else:
                        target_distance = abs(trade.target - ref_entry)
                        threshold_t1 = target_distance * runner_unlock_ratio
                        target_current = price if trade.inst_type == "OPT" else trade_price
                        current_move = max(
                            0.0,
                            (max(float(target_current), observed_favorable_price) - ref_entry)
                            if trade.direction == "LONG"
                            else (ref_entry - min(float(target_current), observed_favorable_price)),
                        )
                        is_correct_direction = current_move > 0.0

                    reached_t1 = is_correct_direction and current_move >= threshold_t1
                    # Big-move ride: engage the runner EARLY (before target) for a proven
                    # winner in a strong trend so a big move is not cut at the fixed target.
                    # Small gains never qualify (see _big_move_expected).
                    big_move_ok = False
                    big_reason = ""
                    if (
                        is_correct_direction
                        and not reached_t1
                        and scanner._setting_enabled(
                            getattr(get_settings(), "trend_runner_engage_before_target_enabled", True), True
                        )
                    ):
                        big_move_ok, big_reason = scanner._big_move_expected(
                            trade,
                            gain_r=scanner._trade_gain_in_r(trade, current_move),
                            regime=regime,
                        )
                    if reached_t1 or big_move_ok:
                        trade.runner_mode = True
                        setattr(trade, "stop_provenance", "runner")
                        setattr(trade, "runner_activation_timestamp", latest_time)
                        setattr(
                            trade,
                            "runner_activation_reason",
                            "TARGET 1 HIT" if reached_t1 else str(big_reason or "big-move ride"),
                        )
                        trade.original_target_distance = target_distance
                        trade.target = 0 # Remove hard target to let it run to T2/T3
                        lock_label = f"{runner_lock_pct * 100:.0f}%"
                        note = "TARGET 1 HIT" if reached_t1 else f"big-move ride ({big_reason})"
                        scanner.log_event(f"🚀 {note} for {trade.instrument}. Entering Runner Mode ({lock_label} Gain Lock).", "trade")
                        logger.success(f"🔥 Runner Mode Active: {trade.instrument}. Hard target removed, locking {lock_label} gains.")

                # ── Runner Mode Trailing (90% Gain Lock) ──
                if getattr(trade, 'runner_mode', False):
                    if trade.inst_type == "OPT":
                        current_gain_pts = max(
                            0.0,
                            trade.current_price - trade.entry_price,
                            observed_favorable_price - trade.entry_price,
                        )
                    else:
                        current_gain_pts = max(
                            0.0,
                            (max(trade_price, observed_favorable_price) - trade.entry_price)
                            if trade.direction == "LONG"
                            else (trade.entry_price - min(trade_price, observed_favorable_price)),
                        )

                    if cascade_runner:
                        locked_gain_pts = current_gain_pts * runner_lock_pct
                    else:
                        # Fixed trailing distance logic based on original target profit magnitude
                        orig_target_dist = getattr(trade, 'original_target_distance', 0)
                        if orig_target_dist <= 0:
                            # Old rows have no frozen runner distance. Recover only
                            # contract-price geometry, never an index target or a
                            # mutable profit stop, and freeze the recovered value.
                            premium_target = float(getattr(trade, "premium_target", 0.0) or 0.0)
                            if trade.inst_type == "OPT" and premium_target > trade.entry_price:
                                orig_target_dist = premium_target - trade.entry_price
                            else:
                                initial_stop = float(getattr(trade, "initial_stop", 0.0) or trade.trailing_stop)
                                actual_rr = float(getattr(trade, "entry_policy_actual_rr", 0.0) or 0.0)
                                if actual_rr <= 0:
                                    actual_rr = float(trade.rr_ratio or 0.0)
                                orig_target_dist = abs(trade.entry_price - initial_stop) * actual_rr
                            trade.original_target_distance = orig_target_dist

                        trail_dist = orig_target_dist * (1.0 - runner_lock_pct)

                        # Lock the peak gain minus the fixed trail distance
                        locked_gain_pts = max(0, current_gain_pts - trail_dist)

                    if trade.inst_type == "OPT":
                        multiplier = abs(trade.instrument_multiplier) or 0.5
                        # Convert to spot: Spot = Entry Spot + (Gain / Multiplier) if Call, else Entry Spot - (Gain / Multiplier)
                        spot_gain = locked_gain_pts / multiplier
                        new_runner_stop = (trade.entry_spot + spot_gain) if trade.direction == "LONG" else (trade.entry_spot - spot_gain)
                    else:
                        new_runner_stop = (trade.entry_price + locked_gain_pts) if trade.direction == "LONG" else (trade.entry_price - locked_gain_pts)

                    # Update stop if the new locked gain is higher (trailing)
                    if trade.direction == "LONG":
                        new_stop = max(new_stop, new_runner_stop)
                    else:
                        new_stop = min(new_stop, new_runner_stop)
                    setattr(trade, "runner_stop", float(new_runner_stop))
                    setattr(
                        trade,
                        "runner_stop_basis",
                        "INDEX_SPOT"
                        if str(getattr(trade, "inst_type", "") or "").upper() == "OPT"
                        else "FUTURES_CONTRACT",
                    )

            # ── 5. SMART PROFIT LOCK & REVERSAL GUARD ──
            # Calculate absolute PnL in Rupees for institutional exit rules
            current_pnl_rs = 0.0
            entry_val = trade.entry_price
            # The CONFIRMED fill, not the intended size. `quantity` is lots * lot_size;
            # on a partial fill the broker confirms less and pnl_quantity carries that.
            # Its own docstring cites the case: 65 intended / 30 filled reported -860
            # against a true -440. This qty feeds current_pnl_rs, candle_extreme_pnl,
            # peak_pnl and initial_risk_live, so an overstatement arms the rupee loss
            # band (Rs.1,000/lot) and even the Rs.3,000 catastrophe force-exit on money
            # that was never at risk. Falls back to the intended size pre-fill.
            qty = getattr(trade, "pnl_quantity", None) or getattr(
                trade, "quantity", getattr(trade, "qty", 0)
            )
            inst_mult = getattr(trade, 'instrument_multiplier', 1.0)
            if trade.inst_type == "OPT":
                current_pnl_rs = (trade.current_price - entry_val) * qty
                current_gain_pct = (current_pnl_rs / (entry_val * qty)) * 100.0 if entry_val > 0 and qty > 0 else 0
            else:
                current_pnl_rs = (trade_price - entry_val) * qty if trade.direction == "LONG" else (entry_val - trade_price) * qty
                current_pnl_rs *= inst_mult
                current_gain_pct = (current_pnl_rs / (entry_val * qty * inst_mult)) * 100.0 if entry_val > 0 and qty > 0 and inst_mult > 0 else 0

            candle_extreme_price = scanner._trade_candle_extreme_price(trade, latest_time)
            if candle_extreme_price > 0:
                if trade.inst_type == "OPT":
                    # Delta projection is context, not an executable option quote. IV,
                    # theta, gamma and basis can make the contract miss this estimate.
                    # Keep it separately for diagnostics; never arm a real profit exit
                    # from money the option was not observed to have earned.
                    prem_extreme = float(candle_extreme_price)
                    prem_extreme = scanner._sanitize_option_mark(
                        trade,
                        prem_extreme,
                        float(price),
                        "intrabar peak reconstruction",
                    )
                    projected_peak_pnl = (prem_extreme - entry_val) * qty
                    setattr(
                        trade,
                        "projected_peak_pnl",
                        max(
                            float(getattr(trade, "projected_peak_pnl", 0.0) or 0.0),
                            float(projected_peak_pnl or 0.0),
                        ),
                    )
                    candle_extreme_pnl = 0.0
                else:
                    candle_extreme_pnl = (
                        (candle_extreme_price - entry_val) * qty
                        if trade.direction == "LONG"
                        else (entry_val - candle_extreme_price) * qty
                    )
                    candle_extreme_pnl *= inst_mult
            else:
                candle_extreme_pnl = 0.0

            tick_extreme_pnl = 0.0
            if observed_prices and qty > 0:
                if trade.inst_type == "OPT":
                    tick_extreme_pnl = (observed_favorable_price - entry_val) * qty
                elif trade.direction == "LONG":
                    tick_extreme_pnl = (
                        observed_favorable_price - entry_val
                    ) * qty * inst_mult
                else:
                    tick_extreme_pnl = (
                        entry_val - observed_favorable_price
                    ) * qty * inst_mult

            # Update executable peak PnL only from observed instrument prices. Futures
            # candle extremes are contract-space observations; option index-to-premium
            # projections stay in projected_peak_pnl above.
            inst_mult_peak = getattr(trade, 'instrument_multiplier', 1.0) if getattr(trade, 'inst_type', 'FUT') != 'OPT' else 1.0
            prior_peak_pnl = float(getattr(trade, 'peak_pnl', 0.0) or 0.0)
            trade.peak_pnl, peak_provenance = scanner._resolve_executable_peak_pnl(
                trade,
                prior_peak_pnl=prior_peak_pnl,
                current_pnl_rs=current_pnl_rs,
                candle_extreme_pnl=candle_extreme_pnl,
                tick_extreme_pnl=tick_extreme_pnl,
                quote_verified=bool(quote_verified),
                has_contract_ticks=bool(observed_prices),
            )
            if trade.peak_pnl > prior_peak_pnl:
                setattr(trade, "peak_pnl_provenance", peak_provenance)
            peak_gain_pct = (trade.peak_pnl / (entry_val * qty * inst_mult_peak)) * 100.0 if entry_val > 0 and qty > 0 and inst_mult_peak > 0 else 0

            lock_dir = "LONG" if trade.inst_type == "OPT" else trade.direction
            scanner._ensure_trade_like_initial_stop(trade)
            initial_risk_live = scanner._trade_like_initial_risk_rs(
                trade,
                qty=int(qty or 0),
                inst_mult=inst_mult_peak,
            )
            pre_profit_lock_stop = float(new_stop or 0.0)
            lock_stop, lock_reason, lock_ratio = scanner._profit_lock_floor(
                entry_val, lock_dir, int(qty or 0), trade.peak_pnl,
                bool(getattr(trade, "scalp_lock_mode", False)),
                inst_mult=inst_mult_peak, initial_risk_rs=initial_risk_live,
                timeframe=getattr(trade, "timeframe", ""),
                now=latest_time,
                trade_like=trade,
            )
            if adaptive_active:
                lock_stop, lock_reason = 0.0, ""
            if lock_reason and cascade_runner and current_pnl_rs > 0.0:
                lock_stop = 0.0
                lock_reason = ""
                marker = f"{latest_time:%H:%M}:{trade.peak_pnl:.0f}:{current_pnl_rs:.0f}"
                if getattr(trade, "_last_one_way_profit_lock_defer_log", "") != marker:
                    setattr(trade, "_last_one_way_profit_lock_defer_log", marker)
                    scanner.log_event(
                        f"Deferred profit lock for {trade.instrument} {trade.timeframe}: "
                        f"{cascade_ctx.get('reason', 'one-way cascade still aligned')}.",
                        "info",
                    )
            if lock_reason == "MAJOR_WIN_GUARD" and trade.inst_type == "FUT":
                should_exit_major, guard_context = scanner._should_exit_major_win_guard(
                    trade,
                    float(trade_price),
                    trade.peak_pnl,
                    current_pnl_rs,
                    latest_time,
                )
                if not should_exit_major:
                    lock_stop = 0.0
                    lock_reason = ""
                    scanner.log_event(
                        f"Deferred major-win lock for {trade.instrument} {trade.timeframe}: {guard_context}.",
                        "info",
                    )
            
            lock_stop_spot = prem_to_spot(lock_stop) if lock_stop > 0 else 0.0
            if lock_stop_spot > 0:
                if trade.direction == "LONG":
                    new_stop = max(new_stop, lock_stop_spot)
                else:
                    new_stop = min(new_stop, lock_stop_spot)

            # One HOLD contract for manual, broker and replay (engine/profit_lock_hold.py).
            # Breathing room and the lock floor live in premium space for an option; the
            # deferral restores the stop this pass started from (pre_profit_lock_stop,
            # kept in this path's own space so nothing round-trips through a conversion).
            option_lock_space = trade.inst_type == "OPT"
            proposed_lock_stop = spot_to_prem(new_stop) if option_lock_space else float(new_stop or 0.0)

            def _log_profit_lock_deferral(reason_name: str, why: str) -> None:
                marker = f"{latest_time:%H:%M}:{trade.peak_pnl:.0f}:{current_pnl_rs:.0f}"
                if getattr(trade, "_last_trend_profit_lock_defer_log", "") != marker:
                    setattr(trade, "_last_trend_profit_lock_defer_log", marker)
                    scanner.log_event(
                        f"Deferred {reason_name} for {trade.instrument} {trade.timeframe}: {why}.",
                        "info",
                    )

            def _broker_stop_hit(stop_value: float) -> bool:
                return (trade.direction == "LONG" and trade_price <= stop_value) or (
                    trade.direction == "SHORT" and trade_price >= stop_value
                )

            lock_hold = profit_lock_hold_policy.resolve(
                scanner,
                trade,
                lock_reason=lock_reason,
                lock_stop=lock_stop,
                proposed_stop=proposed_lock_stop,
                pre_lock_stop=(
                    spot_to_prem(pre_profit_lock_stop) if option_lock_space else pre_profit_lock_stop
                ),
                # CE and PE are both bought premium. A delta-projected index stop cannot
                # prove a premium floor breach, and a tighter structural trail must
                # retain its own TRAILING_STOP authority: only a verified mark at or
                # below the lock floor is a lock hit.
                mark=float(opt_val) if option_lock_space else float(trade_price),
                mark_verified=bool(quote_verified),
                option_space=option_lock_space,
                current_pnl_rs=current_pnl_rs,
                peak_pnl_rs=trade.peak_pnl,
                latest_time=latest_time,
                regime=regime,
                apply_breathing=option_lock_space,
                non_option_hit=_broker_stop_hit,
                on_defer=_log_profit_lock_deferral,
            )
            profit_lock_breathing_clamped = lock_hold.breathing_clamped
            profit_lock_deferred = lock_hold.trend_deferred
            effective_lock_stop = lock_hold.effective_lock_stop
            if lock_hold.breathing_clamped:
                scanner.log_event(
                    f"Option breathing room kept live {trade.instrument} {trade.timeframe} stop at {lock_hold.breathing_stop:.2f} "
                    f"(floor {lock_hold.breathing_floor:.2f}) instead of {proposed_lock_stop:.2f}.",
                    "info",
                )
                new_stop = prem_to_spot(lock_hold.breathing_stop)
            if lock_hold.restored:
                new_stop = pre_profit_lock_stop
            lock_reason = lock_hold.lock_reason
            lock_stop = lock_hold.lock_stop

            if lock_reason and lock_hold.hit:
                # A crossed floor is a trigger, not an observed executable fill.
                exit_val = float(opt_val) if trade.inst_type == "OPT" else new_stop
                if trade.inst_type == "OPT":
                    locked_pnl = (exit_val - entry_val) * qty
                else:
                    locked_pnl = (exit_val - entry_val) * qty if trade.direction == "LONG" else (entry_val - exit_val) * qty
                logger.warning(
                    f"Profit Lock Stop Hit for {trade.instrument}: Peak Rs.{trade.peak_pnl:.0f}, "
                    f"Locked {lock_ratio:.0%} Rs.{locked_pnl:.0f}"
                )
                scanner._close_and_record(tid, exit_val, lock_reason, exit_time=latest_time)
                continue

            # Rule 1: R-aware smart protection. The old hard-coded Rs.1000
            # threshold clipped option winners even when configured risk was
            # several thousand rupees.
            smart_min_peak_live = scanner._profit_lock_min_peak(
                bool(getattr(trade, "scalp_lock_mode", False)),
                initial_risk_live,
                getattr(trade, "timeframe", ""),
                latest_time,
                trade_like=trade,
            )

            def handle_live_recovery_watch(proposed_reason: str) -> str:
                action, recovery_ctx = scanner._loss_recovery_watch_decision(
                    trade,
                    current_price=float(opt_val),
                    current_pnl_rs=current_pnl_rs,
                    peak_pnl=trade.peak_pnl,
                    initial_risk_rs=initial_risk_live,
                    latest_time=latest_time,
                    regime=regime,
                    proposed_exit_reason=proposed_reason,
                )
                if action == "HOLD":
                    marker = f"{latest_time:%H:%M}:{current_pnl_rs:.0f}:{proposed_reason}"
                    if getattr(trade, "_last_loss_recovery_hold_log", "") != marker:
                        setattr(trade, "_last_loss_recovery_hold_log", marker)
                        scanner.log_event(
                            f"Recovery watch holding live {trade.instrument} {trade.timeframe}: {recovery_ctx}.",
                            "info",
                        )
                    return "HOLD"
                if action == "EXIT":
                    logger.warning(f"🚨 RECOVERY_WATCH_EXIT for {trade.instrument}: {recovery_ctx}")
                    scanner._close_and_record(tid, opt_val, "RECOVERY_WATCH_EXIT", exit_time=latest_time)
                    scanner.log_event(
                        f"🛟 Recovery watch exit: {trade.instrument} @ ₹{current_pnl_rs:.0f} ({recovery_ctx})",
                        "trade",
                    )
                    return "EXIT"
                return "NONE"

            # Early loss-guard (symmetric FUT/OPT): base 0.8R; accelerates to
            # 0.55R / 0.35R when intel + price action confirm a BIG reversal
            # (not a small pullback). Mirrors profit-lock on the downside.
            try:
                # trade.peak_pnl, deliberately. On a bare Trade it does track
                # unrealized_pnl, which is NET -- but this lane does not read a bare
                # Trade. Forty lines up, unconditionally, in this same method:
                #
                #   scanner.py:29429
                #   trade.peak_pnl = max(trade.peak_pnl, current_pnl_rs, candle_extreme_pnl)
                #
                # current_pnl_rs there is THIS gross number, so by the time the rule
                # reads peak_pnl it is already gross AND already carries the intrabar
                # extreme. The unit mismatch that a separate gross high-water mark was
                # introduced to fix does not exist on this path.
                #
                # That separate mark cost two things. It never saw candle_extreme_pnl,
                # so a peak reached between two samples was invisible (sampled marks
                # 40/90/120/100/60 against a real intrabar high of Rs.500 gave a peak of
                # 120, and the rule stayed silent at Rs.60 where it used to fire). And
                # peak_gross_pnl is absent from Trade.to_dict/from_dict and from the
                # sqlite persisted set, so after a restart it reset to the CURRENT mark
                # -- and a number can never be Rs.150 above itself, so the rule could
                # never arm again for that position. main.py restarts several times a
                # session, so that was not a corner case.
                #
                # The arm/floor were fitted by replaying pnl_path, which is the gross
                # series peak_pnl tracks. Feeding the rule anything lower feeds it a
                # series the calibration never saw. A floor crossing may still precede
                # a later recovery, so lowering the arm is not assumed risk-free.
                cap_verdict = scanner._peak_giveback_cap_exit(
                    trade,
                    current_pnl_rs=current_pnl_rs,
                    peak_pnl_rs=float(getattr(trade, "peak_pnl", 0.0) or 0.0),
                    evaluated_at=latest_time,
                )
                cap_exit, cap_reason = cap_verdict
                # The method hosts two rules; log the one that actually fired.
                # The fallback is deliberately NOT either rule's name: a verdict that
                # fired without naming itself is a bug, and the log should say that
                # rather than blame whichever rule happens to be spelled here.
                cap_rule = getattr(cap_verdict, "rule", "") or "PROFIT_FLOOR_EXIT"
                if cap_exit and not adaptive_active:
                    recovery_action = handle_live_recovery_watch(cap_rule)
                    if recovery_action not in {"HOLD", "EXIT"}:
                        exit_val = float(opt_val if trade.inst_type == "OPT" else trade_price)
                        label = (
                            "Breakeven stop" if cap_rule == "BREAKEVEN_STOP"
                            else "Give-back cap"
                        )
                        logger.warning(
                            f"{label} for {trade.instrument}: {cap_reason} "
                            f"(PnL Rs.{current_pnl_rs:.0f})"
                        )
                        scanner._close_and_record(
                            tid, exit_val, cap_rule, exit_time=latest_time
                        )
                        scanner.log_event(
                            f"{label}: {trade.instrument} closed @ "
                            f"Rs.{current_pnl_rs:.0f} ({cap_reason})",
                            "trade",
                        )
                    continue

                should_cut, cut_detail, cut_r = scanner._loss_guard_decision(
                    trade,
                    current_pnl_rs=current_pnl_rs,
                    initial_risk_rs=initial_risk_live,
                    latest_time=latest_time,
                    regime=regime,
                    current_price=float(opt_val if trade.inst_type == "OPT" else trade_price),
                )
                early_loss_cut_decision = bool(should_cut)
                if should_cut and current_pnl_rs < 0.0:
                    recovery_action = handle_live_recovery_watch("EARLY_LOSS_CUT")
                    if recovery_action in {"HOLD", "EXIT"}:
                        continue
                    exit_val = float(opt_val if trade.inst_type == "OPT" else trade_price)
                    logger.warning(
                        f"Early Loss Cut for {trade.instrument}: {cut_detail} "
                        f"(PnL ₹{current_pnl_rs:.0f}, thr {cut_r:.2f}R)"
                    )
                    scanner._close_and_record(tid, exit_val, "EARLY_LOSS_CUT", exit_time=latest_time)
                    scanner.log_event(
                        f"Loss Guard: {trade.instrument} cut @ ₹{current_pnl_rs:.0f} ({cut_detail})",
                        "trade",
                    )
                    continue
            except Exception as exc:
                logger.debug(f"Early loss cut check skipped for {trade.instrument}: {exc}")

            # Rupee loss ladder: OPT from -1000, FUT from -1500 (product arms).
            try:
                live_px = float(opt_val if trade.inst_type == "OPT" else trade_price)
                band_exit, band_reason, band_ctx = scanner._rupee_loss_band_guard_decision(
                    trade,
                    current_pnl_rs=current_pnl_rs,
                    latest_time=latest_time,
                    regime=regime,
                    current_price=live_px,
                )
                if band_exit and current_pnl_rs < 0.0:
                    recovery_action = handle_live_recovery_watch(band_reason)
                    if recovery_action in {"HOLD", "EXIT"}:
                        continue
                    logger.warning(f"Rupee loss band exit for {trade.instrument}: {band_ctx}")
                    scanner._close_and_record(tid, live_px, band_reason, exit_time=latest_time)
                    scanner.log_event(
                        f"Loss Band: {trade.instrument} cut @ ₹{current_pnl_rs:.0f} ({band_ctx})",
                        "trade",
                    )
                    continue
            except Exception as exc:
                logger.debug(f"Live rupee loss-band skipped for {trade.instrument}: {exc}")

            threat_exit, threat_reason, threat_ctx = scanner._peak_threat_exit_decision(
                trade,
                current_price=float(opt_val if trade.inst_type == "OPT" else trade_price),
                current_pnl_rs=current_pnl_rs,
                peak_pnl=trade.peak_pnl,
                initial_risk_rs=initial_risk_live,
                latest_time=latest_time,
                regime=regime,
                inst_type=getattr(trade, "inst_type", ""),
                option_type=getattr(trade, "option_type", ""),
                direction=getattr(trade, "direction", ""),
            )
            if threat_exit and not (adaptive_active and threat_reason == "PEAK_THREAT_EXIT"):
                exit_val = float(opt_val if trade.inst_type == "OPT" else trade_price)
                # Log only if not already latched (manual mode one-shot)
                if not bool(getattr(trade, "pending_exit", False)):
                    logger.warning(f"🚨 {threat_reason} for {trade.instrument}: {threat_ctx}")
                    scanner.log_event(
                        f"💰 {threat_reason}: {trade.instrument} @ ₹{current_pnl_rs:.0f} ({threat_ctx})",
                        "trade",
                    )
                scanner._close_and_record(tid, exit_val, threat_reason, exit_time=latest_time)
                continue

            ladder_peak_live = type(scanner)._profit_ladder_pnl(self, trade, trade.peak_pnl, qty)
            if not adaptive_active and scanner._smart_profit_retention_eligible(
                peak_pnl=trade.peak_pnl,
                minimum_peak=smart_min_peak_live,
                ladder_peak=ladder_peak_live,
                breathing_clamped=profit_lock_breathing_clamped,
                trend_deferred=profit_lock_deferred,
            ):
                # Profit fell below the ladder's own retention, OR the trade earned more
                # than the keep-floor and gave it back. The floor is collapse protection,
                # never a cap on a trade still climbing through it — see
                # _smart_lock_floor_collapse (2026-08-12: auto mode carried the same
                # arm-below-floor defect that cut four signal-mode winners at their peak).
                # The retention comes from _smart_lock_backstop_ratio so auto and signal
                # mode cannot disagree about when a fading winner is done.
                backstop_ratio_live = scanner._smart_lock_backstop_ratio(
                    lock_ratio, bool(getattr(trade, "scalp_lock_mode", False))
                )
                if current_pnl_rs < (
                    trade.peak_pnl * backstop_ratio_live
                ) or scanner._smart_lock_floor_collapse(
                    trade.peak_pnl, current_pnl_rs, backstop_ratio_live,
                    floor_rs=scanner._profit_lock_floor_rs(trade),
                ):
                    if current_pnl_rs < 0.0:
                        recovery_action = handle_live_recovery_watch("SMART_PROFIT_LOCK")
                        if recovery_action in {"HOLD", "EXIT"}:
                            continue
                    logger.warning(f"🚨 Smart Profit Lock Hit for {trade.instrument}: Peak ₹{trade.peak_pnl:.0f}, Current ₹{current_pnl_rs:.0f}")
                    scanner._close_and_record(tid, opt_val if trade.inst_type == "OPT" else (lock_stop if lock_stop > 0 else opt_val), "SMART_PROFIT_LOCK", exit_time=latest_time)
                    scanner.log_event(f"💰 Profit Locked: {trade.instrument} (Retention Guard Hit @ ₹{current_pnl_rs:.0f})", "trade")
                    continue

            # Fix 1/3: sub-1R give-back guard + live regime-reversal profit-taker.
            gb_exit, gb_reason, gb_ctx = scanner._peak_giveback_or_reversal_exit(
                trade,
                current_pnl_rs=current_pnl_rs,
                peak_pnl=trade.peak_pnl,
                initial_risk_rs=initial_risk_live,
                smart_min_peak=smart_min_peak_live,
                latest_time=latest_time,
                regime=regime,
                inst_type=getattr(trade, "inst_type", ""),
                option_type=getattr(trade, "option_type", ""),
                direction=getattr(trade, "direction", ""),
            )
            if gb_exit and not adaptive_active:
                if current_pnl_rs < 0.0:
                    recovery_action = handle_live_recovery_watch(gb_reason)
                    if recovery_action in {"HOLD", "EXIT"}:
                        continue
                logger.warning(f"🚨 {gb_reason} for {trade.instrument}: {gb_ctx}")
                scanner._close_and_record(tid, opt_val, gb_reason, exit_time=latest_time)
                scanner.log_event(f"💰 {gb_reason}: {trade.instrument} @ ₹{current_pnl_rs:.0f} ({gb_ctx})", "trade")
                continue

            eod_loss_exit, eod_loss_ctx = scanner._eod_loss_minimize_exit(
                trade,
                current_pnl_rs=current_pnl_rs,
                initial_risk_rs=initial_risk_live,
                latest_time=latest_time,
                regime=regime,
            )
            if eod_loss_exit:
                logger.warning(f"🚨 EOD_LOSS_MINIMIZE for {trade.instrument}: {eod_loss_ctx}")
                scanner._close_and_record(tid, opt_val, "EOD_LOSS_MINIMIZE", exit_time=latest_time)
                scanner.log_event(
                    f"🕒 EOD loss minimized: {trade.instrument} @ ₹{current_pnl_rs:.0f} ({eod_loss_ctx})",
                    "trade",
                )
                continue

            # Rule 2: Low-Gain Protection (If gain was > 10% but reversed)
            if peak_gain_pct >= 10.0 and not adaptive_active:
                # If gain drops below 40% of peak. The old `or current_gain_pct < 2.0`
                # "BE floor" was removed 2026-08-30 as unreachable: the enclosing gate is
                # peak_gain_pct >= 10.0, so peak_gain_pct * 0.40 >= 4.0 and anything under
                # 2.0 is already under 4.0. Verified over 39.8M pairs -- it never once
                # decided on its own. Behaviour-identical; the rule no longer advertises a
                # breakeven floor it never had.
                if current_gain_pct < (peak_gain_pct * 0.40):
                    logger.warning(f"🚨 Low-Gain Protection Hit for {trade.instrument}: Peak {peak_gain_pct:.1f}%, Current {current_gain_pct:.1f}%")
                    scanner._close_and_record(tid, opt_val, "LOW_GAIN_PROTECT", exit_time=latest_time)
                    continue

            # Rule 3: Major Win Protection. The rung is sized to the contract -- a flat
            # Rs.3,000 had never once fired on NIFTY or BANKNIFTY. See
            # _major_win_guard_arm_rs.
            if not adaptive_active and type(scanner)._profit_ladder_pnl(self, trade, trade.peak_pnl, qty) >= scanner._major_win_guard_ladder_arm_rs(trade, qty):
                if current_pnl_rs < (trade.peak_pnl * 0.75):
                    should_exit, guard_context = (
                        scanner._should_exit_major_win_guard(
                            trade,
                            float(trade_price),
                            trade.peak_pnl,
                            current_pnl_rs,
                            latest_time,
                        )
                        if str(getattr(trade, "inst_type", "FUT") or "FUT").upper() == "FUT"
                        else (True, "option major-win guard")
                    )
                    if should_exit:
                        logger.warning(
                            f"Major Win Guard for {trade.instrument}: Peak Rs.{trade.peak_pnl:.0f} "
                            f"-> Exit @ Rs.{current_pnl_rs:.0f}; {guard_context}"
                        )
                        scanner._close_and_record(
                            tid,
                            opt_val if trade.inst_type == "OPT" else (lock_stop if lock_stop > 0 else opt_val),
                            "MAJOR_WIN_GUARD",
                            exit_time=latest_time,
                        )
                        continue
                    scanner.log_event(
                        f"Deferred major-win exit for {trade.instrument} {trade.timeframe}: {guard_context}.",
                        "info",
                    )

            # Rule 4: Risk- and volatility-normalized stagnation protection.
            should_stagnate, stagnation_context = scanner._stagnation_exit_decision(
                trade,
                current_pnl_rs,
                trade.peak_pnl,
                latest_time,
                regime=regime,
            )
            if should_stagnate and not adaptive_active:
                logger.info(f"Stagnation Exit: {trade.instrument}; {stagnation_context}")
                scanner._close_and_record(tid, opt_val, "STAGNATION_EXIT", exit_time=latest_time)
                continue

            if False and trade.entry_time and current_pnl_rs >= 300.0:
                time_elapsed = (latest_time - trade.entry_time.replace(tzinfo=None)).total_seconds() / 60.0 if latest_time else 0
                # Exit if held > 20m and current profit is < 85% of peak (stalled)
                stagnation_minutes = max(
                    1.0,
                    float(getattr(settings, "trade_stagnation_minutes", 20.0) or 20.0),
                )
                if time_elapsed >= stagnation_minutes and current_pnl_rs < (trade.peak_pnl * 0.85):
                    logger.info(f"⏳ Stagnation Exit: {trade.instrument} held {time_elapsed:.0f}m, PnL ₹{current_pnl_rs:.0f}")
                    scanner._close_and_record(tid, opt_val, "STAGNATION_EXIT", exit_time=latest_time)
                    continue

            latest_time = scanner.candles.get_max_timestamp()

            # ── Live Trade Preservation Patch ──
            if getattr(trade, 'is_live', False) and scanner.mode == "HISTORICAL":
                triggered = False
                reason = ""
                if trade.inst_type == "OPT":
                    if trade.current_price <= trade.current_stop:
                        triggered = True
                        reason = "TRAILING_STOP"
                    elif trade.target > 0 and trade.current_price >= trade.target:
                        triggered = True
                        reason = "TARGET_HIT"
                else:
                    stop_check_price = trade_price
                    if trade.direction == "LONG":
                        if stop_check_price <= new_stop:
                            triggered = True
                            reason = "TRAILING_STOP"
                        elif stop_check_price >= trade.target and trade.target > 0:
                            triggered = True
                            reason = "TARGET_HIT"
                    else:
                        if stop_check_price >= new_stop:
                            triggered = True
                            reason = "TRAILING_STOP"
                        elif stop_check_price <= trade.target and trade.target > 0:
                            triggered = True
                            reason = "TARGET_HIT"

                if triggered:
                    msg = f"🚨 ALERT: Live Trade {trade.instrument} requires EXIT ({reason}) at {trade.current_price:.2f}!"
                    logger.warning(msg)
                    if scanner.on_notification:
                        scanner.on_notification(msg, "warning")

                # Update price and stop in memory so UI shows it, but do NOT close it!
                if trade.inst_type == "OPT":
                    # `new_stop` is SPOT space; trade.current_stop is PREMIUM space (it is
                    # compared against trade.current_price just above). Assigning new_stop
                    # here would store ~24,800 in a field tested against a ~150 premium, so
                    # `150 <= 24800` would fire a spurious TRAILING_STOP alert on every
                    # option, every tick. The old `pass` avoided that but froze the stop at
                    # whatever it held when the mode flipped, so a runner gave back its
                    # whole gain with the dashboard still showing the entry stop.
                    #
                    # Convert, then ratchet one way only. A bought option is always long
                    # premium, so the stop may only rise -- and spot_to_prem divides through
                    # entry_spot, which is a field this codebase has already had corrupted,
                    # so a monotonic guard keeps a bad conversion from LOOSENING a live stop.
                    try:
                        prem_stop = float(spot_to_prem(new_stop))
                    except Exception:
                        prem_stop = 0.0
                    if prem_stop > float(trade.current_stop or 0.0):
                        trade.current_stop = prem_stop
                else:
                    trade.current_stop = new_stop
                continue

            if not scanner.auto_mode:
                manual_trigger = ""
                manual_exit_price = float(opt_val)
                manual_exit_time = latest_time
                contract_touch = scanner._first_contract_exit_touch(
                    trade,
                    observed_contract_ticks,
                    stop=contract_stop_before_cycle,
                    target=float(getattr(trade, "target", 0.0) or 0.0),
                )
                if contract_touch is not None:
                    manual_trigger, manual_exit_price, manual_exit_time = contract_touch
                if trade.inst_type == "OPT":
                    manual_option_stop = float(spot_to_prem(new_stop) or 0.0)
                    if not manual_trigger and trade.current_price <= manual_option_stop:
                        manual_trigger = "TRAILING_STOP"
                    elif not manual_trigger and trade.target > 0 and trade.current_price >= trade.target:
                        manual_trigger = "TARGET_HIT"
                elif trade.direction == "LONG":
                    if not manual_trigger and trade_price <= new_stop:
                        manual_trigger = "TRAILING_STOP"
                    elif not manual_trigger and trade.target > 0 and trade_price >= trade.target:
                        manual_trigger = "TARGET_HIT"
                else:
                    if not manual_trigger and trade_price >= new_stop:
                        manual_trigger = "TRAILING_STOP"
                    elif not manual_trigger and trade.target > 0 and trade_price <= trade.target:
                        manual_trigger = "TARGET_HIT"
                if manual_trigger:
                    recovery_action = handle_live_recovery_watch(manual_trigger)
                    if recovery_action in {"HOLD", "EXIT"}:
                        continue
                    marker = f"{manual_trigger}:{latest_time:%Y-%m-%dT%H:%M}"
                    if getattr(trade, "_manual_exit_alert_marker", "") != marker:
                        setattr(trade, "_manual_exit_alert_marker", marker)
                        scanner._close_and_record(
                            tid,
                            manual_exit_price,
                            manual_trigger,
                            exit_time=manual_exit_time,
                        )
                scanner._commit_trade_contract_tick_path(trade)
                continue

            auto_stop_trigger = False
            if trade.inst_type == "OPT":
                auto_stop_trigger = trade.current_price <= trade.current_stop
            elif trade.direction == "LONG":
                auto_stop_trigger = trade_price <= new_stop
            else:
                auto_stop_trigger = trade_price >= new_stop
            if auto_stop_trigger:
                recovery_action = handle_live_recovery_watch("TRAILING_STOP")
                if recovery_action in {"HOLD", "EXIT"}:
                    continue

            # Audit #9: pass PREMIUM stop for OPT so TradeManager does not re-map via delta.
            tm_stop = spot_to_prem(new_stop) if trade.inst_type == "OPT" else new_stop
            if trade.inst_type == "OPT" and not quote_verified:
                tm_stop = float(getattr(trade, "current_stop", 0.0) or 0.0)
            scanner.trades.update_trade(
                tid,
                price,
                tm_stop,
                current_time=latest_time,
                real_premium=real_premium,
                real_fut_price=real_fut_price,
                quote_verified=quote_verified,
                allow_broker_protection=allow_blocking,
                observed_contract_ticks=observed_contract_ticks,
                early_loss_cut_decision=early_loss_cut_decision,
            )
            scanner._commit_trade_contract_tick_path(trade)
            if tid not in scanner.trades.open_trades:
                scanner._record_trade_manager_close(tid, trade, price, latest_time)
