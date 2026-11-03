"""Causal ATM-option replay for the native UT/EMA shared-position lane.

Pine owns its index events. The option position also has its own premium stop
and selected RR target. Missing observed contract candles never become modeled
premiums or favorable fills. Replay writes only to a HISTORICAL TradeManager.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import time, timedelta

import pandas as pd

from engine.ut_ema_engine import ENGINE_KEY, LANE, market_stamp, minutes


class ReplayDataGap(ValueError):
    """The available observations cannot support a complete trade path."""


def _observed_contract_context(frame, symbol, strike, option_type, entry_time, trade_day):
    """Derive expiry risk from the exact observed contract, never today's master."""
    from engine.expiry_manager import ArchivedExpiryContext

    attrs = dict(getattr(frame, "attrs", {}) or {})
    if attrs.get("contract_verified") is not True or not attrs.get("contract_expiry"):
        raise ReplayDataGap("Observed option contract expiry is unverified or unavailable")
    if str(attrs.get("contract_symbol") or "") != str(symbol):
        raise ReplayDataGap("Observed option contract symbol differs from resolved contract")
    if str(attrs.get("contract_option_type") or "").upper() != str(option_type).upper():
        raise ReplayDataGap("Observed option type differs from requested ATM contract")
    try:
        observed_strike = float(attrs.get("contract_strike"))
    except (ValueError, TypeError):
        raise ReplayDataGap("Observed option ATM strike is unverified")
    if not math.isfinite(observed_strike) or abs(observed_strike-float(strike)) > 1e-6:
        raise ReplayDataGap("Observed option strike differs from requested ATM contract")
    try:
        expiry = market_stamp(attrs["contract_expiry"])
    except (ValueError, TypeError):
        raise ReplayDataGap("Observed option contract expiry is invalid")
    if pd.isna(expiry):
        raise ReplayDataGap("Observed option contract expiry is invalid")
    if expiry.date() < trade_day:
        raise ReplayDataGap("Observed option contract expired before the replay session")
    context = ArchivedExpiryContext().resolve(
        "", entry_time, contract_expiry=expiry, contract_verified=True)
    if not context.get("available") or context.get("days_to_expiry") is None:
        raise ReplayDataGap("Observed option contract expiry cannot establish entry DTE")
    return {**context, "contract_symbol": str(symbol), "contract_strike": observed_strike,
            "contract_option_type": str(option_type).upper(), "contract_verified": True}


def _next_session_open(stamp, holidays, special_days):
    day = stamp.date() + timedelta(days=1)
    while day.isoformat() not in special_days and (day.weekday() >= 5 or day.isoformat() in holidays):
        day += timedelta(days=1)
    return pd.Timestamp.combine(day, time(9, 15))


def _alignment_levels(average, frozen_risk, previous_stop, previous_target, rr):
    stop = max(previous_stop, round(math.ceil(average * (1 - frozen_risk) / .05 - 1e-9) * .05, 2))
    target = average + (average - stop) * rr if stop < average else previous_target
    return stop, target


def _frame(frame):
    if frame is None or frame.empty:
        raise ReplayDataGap("Observed ATM option minute candles unavailable")
    if not {"open", "high", "low", "close"}.issubset(frame.columns):
        raise ReplayDataGap("Option minute OHLC columns unavailable")
    out = frame.copy()
    out.index = pd.DatetimeIndex([market_stamp(t) for t in out.index])
    return out[~out.index.duplicated(keep="last")].sort_index()


def _valid_prices(row):
    values = tuple(float(row[k]) for k in ("open", "high", "low", "close"))
    op, high, low, close = values
    if not all(math.isfinite(v) and v > 0 for v in values):
        raise ReplayDataGap("Option OHLC contains an unavailable or invalid price")
    if not low <= min(op, close) <= max(op, close) <= high:
        raise ReplayDataGap("Option OHLC range is inconsistent")
    return values


def simulate_record(record, frame, lots, rr, delta, horizon):
    """Replay one shared lifecycle, using only prices observable at each event.

    Minute timestamps are left edges. An open is observable at that timestamp;
    high/low/close become observable one minute later. No full candle is consumed
    when a native exit occurs at its open. Overnight gaps are allowed, intraday
    missing minutes are not. The original mapped risk percentage stays frozen
    across the one alignment add, matching live accounting.
    """
    from data.market_calendar import load_nse_holidays, load_nse_special_trading_days
    from engine.ut_ema_lane import premium_levels

    bars = _frame(frame)
    entry_time, horizon = market_stamp(record["entry_time"]), market_stamp(horizon)
    if entry_time > horizon or entry_time not in bars.index:
        raise ReplayDataGap("Exact entry-minute option open unavailable")
    entry = float(bars.loc[entry_time, "open"])
    if not math.isfinite(entry) or entry <= 0:
        raise ReplayDataGap("Exact entry-minute option open is invalid")
    _, stop, target = premium_levels(entry, abs(float(record["entry_spot"]) - float(record["initial_stop"])), delta, rr)
    distance = entry - stop
    if distance <= 0 or lots < 1 or not math.isfinite(rr) or rr <= 0:
        raise ReplayDataGap("Native premium geometry unavailable")
    risk_pct = distance / entry
    average, current_lots = entry, int(lots)
    holidays, special_days = load_nse_holidays(), load_nse_special_trading_days()
    native_exit = market_stamp(record["exit_time"]) if record.get("exit_time") else None
    native_exit = native_exit if native_exit is not None and native_exit <= horizon else None
    add_time = market_stamp(record["alignment_time"]) if record.get("alignment_lot") and record.get("alignment_time") else None
    added, path, trace, buys = False, [], [], [{"time": entry_time.isoformat(), "price": entry, "lots": int(lots)}]
    previous, mark, mark_time = None, entry, entry_time

    def finish(price, when, reason):
        return {"entry_price": average, "initial_entry_price": entry, "entry_time": entry_time,
                "lots": current_lots, "stop": stop, "target": target,
                "risk_pct": risk_pct * 100, "alignment_added": added,
                "buys": buys, "price_path": path, "exit_trace": trace,
                "exit_price": float(price), "exit_time": when, "reason": reason,
                "closed": bool(reason), "mark_price": float(price), "mark_time": when}

    for stamp, row in bars.loc[(bars.index >= entry_time) & (bars.index <= horizon)].iterrows():
        if previous is not None:
            expected = previous + pd.Timedelta(minutes=1)
            if stamp.date() != previous.date():
                if previous.time() != time(15, 29):
                    raise ReplayDataGap(f"Option session tail missing after {previous.isoformat()}")
                expected = _next_session_open(previous, holidays, special_days)
            if stamp != expected:
                raise ReplayDataGap(f"Option minute missing after {previous.isoformat()}")
        op = float(row["open"])
        if not math.isfinite(op) or op <= 0:
            raise ReplayDataGap("Option minute open is invalid")
        # The current open is the first executable quote after an overnight or
        # minute gap. Protection takes precedence over another alignment order.
        if op <= stop:
            trace.append({"time": stamp.isoformat(), "reason": "UTEMA_PREMIUM_STOP", "price": op, "basis": "minute_open"})
            return finish(op, stamp, "UTEMA_PREMIUM_STOP")
        if native_exit is not None and stamp >= native_exit:
            if stamp != native_exit:
                raise ReplayDataGap("Exact native-exit option open unavailable")
            reason = "UTEMA_" + str(record.get("exit_reason") or "NATIVE_EXIT").replace(" ", "_")
            trace.append({"time": stamp.isoformat(), "reason": reason, "price": op, "basis": "native_event_minute_open"})
            return finish(op, stamp, reason)
        if op >= target:
            trace.append({"time": stamp.isoformat(), "reason": "UTEMA_PREMIUM_TARGET", "price": op, "basis": "minute_open"})
            return finish(op, stamp, "UTEMA_PREMIUM_TARGET")
        if add_time is not None and not added and stamp >= add_time:
            if stamp != add_time:
                raise ReplayDataGap("Exact alignment-minute option open unavailable")
            average = (average * current_lots + op) / (current_lots + 1)
            current_lots += 1
            added = True
            stop, target = _alignment_levels(average, risk_pct, stop, target, rr)
            buys.append({"time": stamp.isoformat(), "price": op, "lots": 1})
        mark, mark_time = op, stamp
        # A bar beginning at the horizon has no observed high/low/close yet.
        observed = stamp + pd.Timedelta(minutes=1)
        if observed > horizon:
            break
        op, high, low, close = _valid_prices(row)
        path.append({"time": stamp.isoformat(), "open": op, "high": high, "low": low, "close": close,
                     "observed_at": observed.isoformat()})
        if low <= stop:
            trace.append({"time": observed.isoformat(), "reason": "UTEMA_PREMIUM_STOP", "price": stop,
                          "basis": "minute_range_stop_first"})
            return finish(stop, observed, "UTEMA_PREMIUM_STOP")
        if high >= target:
            trace.append({"time": observed.isoformat(), "reason": "UTEMA_PREMIUM_TARGET", "price": target,
                          "basis": "minute_range"})
            return finish(target, observed, "UTEMA_PREMIUM_TARGET")
        mark, mark_time, previous = close, observed, stamp

    if native_exit is not None:
        # A terminal closed minute can quote the same boundary as a native
        # observation even when the next minute is absent. Never use it later.
        if mark_time != native_exit:
            raise ReplayDataGap("Native exit has no contemporaneous option observation")
        reason = "UTEMA_" + str(record.get("exit_reason") or "NATIVE_EXIT").replace(" ", "_")
        trace.append({"time": native_exit.isoformat(), "reason": reason, "price": mark, "basis": "terminal_minute_close"})
        return finish(mark, native_exit, reason)
    if mark_time < horizon:
        if mark_time.time() < time(15, 30) or _next_session_open(mark_time, holidays, special_days) <= horizon:
            raise ReplayDataGap("Option observations end before replay horizon")
    return finish(mark, mark_time, "")


def _dates_and_horizon(scanner, instrument, timeframe, result):
    frame = scanner.candles.get_candles(instrument, timeframe)
    if frame is None or frame.empty:
        return [], None
    index = pd.DatetimeIndex([market_stamp(t) for t in frame.index])
    cutoff = scanner.historical_session_cutoff_date() if callable(getattr(scanner, "historical_session_cutoff_date", None)) else market_stamp().date()
    dates = sorted({t.date() for t in index if t.date() <= cutoff})
    if not dates:
        return [], None
    dates = dates[-max(1, int(getattr(scanner, "backtest_days", 1) or 1)):]
    source_end = market_stamp((result.get("state") or {}).get("bar_time") or index[-1]) + pd.Timedelta(minutes=minutes(timeframe))
    eligible = index[index.date <= cutoff]
    horizon = min(source_end, eligible[-1] + pd.Timedelta(minutes=minutes(timeframe)))
    return dates, horizon


def _report(scanner, instrument, timeframe, identity, reason):
    rows = getattr(scanner, "_utema_replay_coverage", None)
    if not isinstance(rows, dict):
        scanner._utema_replay_coverage = rows = {}
    rows[(instrument, timeframe, identity)] = str(reason)


def _simulate_cached(processor, native, frame, lots, rr, delta, horizon):
    """Reuse a pure replay only when prices, native events and calendar match."""
    from data.market_calendar import load_nse_holidays, load_nse_special_trading_days
    digest = hashlib.sha256(pd.util.hash_pandas_object(
        frame[["open", "high", "low", "close"]], index=True).values.tobytes()).hexdigest()
    key = (json.dumps(native, sort_keys=True, allow_nan=False), digest, int(lots), float(rr),
           float(delta), market_stamp(horizon), frozenset(load_nse_holidays()),
           frozenset(load_nse_special_trading_days()))
    cache = processor.__dict__.setdefault("_utema_simulation_cache", {})
    if key not in cache:
        result = simulate_record(native, frame, lots, rr, delta, horizon)
        if len(cache) >= 128:
            cache.clear()
        cache[key] = copy.deepcopy(result)
    return copy.deepcopy(cache[key])


def _authorized(scanner, manager):
    if str(getattr(scanner, "mode", "")).upper() != "HISTORICAL" or str(getattr(manager, "mode", "")).upper() != "HISTORICAL":
        return False
    guard = getattr(scanner, "allows_historical_trade_creation", None)
    return not callable(guard) or bool(guard())


def _prune_scope(scanner, manager, instrument, timeframe, dates, proven_ids):
    """Remove only this replay's rows in the recalculated session window."""
    def obsolete(trade):
        if not (str(getattr(trade, "id", "")).startswith("H_UTEMA_")
                and getattr(trade, "instrument", "") == instrument
                and getattr(trade, "timeframe", "") == timeframe
                and getattr(trade, "source_engine", "") == ENGINE_KEY
                and getattr(trade, "source_lane", "") == LANE
                and getattr(trade, "exit_policy_owner", "") == LANE
                and getattr(trade, "data_provenance", "") == "BACKTEST_OBSERVED_OPTION_MINUTES"):
            return False
        try:
            return market_stamp(trade.entry_time).date() in dates and trade.id not in proven_ids
        except (ValueError, TypeError):
            return False
    with manager._lock:
        if not _authorized(scanner, manager):
            return
        old_count = len(manager.closed_trades) + len(manager.open_trades)
        manager.closed_trades[:] = [t for t in manager.closed_trades if not obsolete(t)]
        for identity, trade in list(manager.open_trades.items()):
            if obsolete(trade):
                manager.open_trades.pop(identity, None)
        if old_count != len(manager.closed_trades) + len(manager.open_trades):
            marker = getattr(manager, "_mark_dirty", None)
            if callable(marker):
                marker()


def replay_candidates(processor, instrument, mtf_result, cfg, lots, lot_size, settings):
    """Populate observed historical rows; never return candidates for live orders."""
    from config.settings import timeframe_entry_timeframes
    from engine.trade_accounting import estimate_trade_charges
    from engine.ut_ema_lane import persist, selected_rr
    from trading.trade_manager import Trade

    scanner = processor.scanner
    manager = getattr(scanner, "trades", None)
    if not _authorized(scanner, manager):
        return []
    allowed = timeframe_entry_timeframes(getattr(settings, "ut_timeframe_entry_policy", "INCLUDE_5MIN"))
    for timeframe in ("5min", "15min"):
        if timeframe not in allowed:
            continue
        result = getattr(mtf_result, "results_" + timeframe, None) or {}
        if result.get("source_engine") != ENGINE_KEY:
            continue
        dates, horizon = _dates_and_horizon(scanner, instrument, timeframe, result)
        if not dates or horizon is None:
            continue
        coverage = getattr(scanner, "_utema_replay_coverage", {})
        for key in list(coverage):
            if key[:2] == (instrument, timeframe):
                coverage.pop(key, None)
        proven_ids = set()
        records = result.get("utema_records") or {}
        for identity, native in records.items():
            when = market_stamp(native["entry_time"])
            if when.date() not in dates or when > horizon:
                continue
            trade_id = "H_UTEMA_" + hashlib.sha256(str(identity).encode()).hexdigest()[:24]
            spot, side = float(native["entry_spot"]), int(native["side"])
            interval = float(cfg.get("strike_interval", 50) or 50)
            strike = round(spot / interval) * interval
            option_type = "CE" if side > 0 else "PE"
            end = min(market_stamp(native["exit_time"]), horizon) if native.get("exit_time") else horizon
            frames, symbol, token, contract_context = [], "", "", None
            try:
                for day in (d for d in dates if when.date() <= d <= end.date()):
                    frame, day_symbol, day_token = processor._load_option_candles(
                        scanner, instrument, strike, option_type, "1min", day,
                        now_dt=end.to_pydatetime())
                    if frame is None or frame.empty or not day_symbol:
                        raise ReplayDataGap(f"Observed ATM contract history unavailable for {day}")
                    if symbol and day_symbol != symbol:
                        raise ReplayDataGap("Contract changed during the shared position")
                    day_context = _observed_contract_context(
                        frame, day_symbol, strike, option_type, when, day)
                    if contract_context is not None and day_context != contract_context:
                        raise ReplayDataGap("Observed contract identity or expiry changed during the shared position")
                    contract_context = day_context
                    symbol, token = day_symbol, day_token or token
                    frames.append(frame)
                if not frames:
                    raise ReplayDataGap("Observed ATM contract history unavailable")
                dte = float(contract_context["days_to_expiry"])
                delta = abs(float(processor._option_signed_delta(spot, strike, option_type, {}, dte, .5)))
                if not math.isfinite(delta) or not 0 < delta <= 1:
                    raise ReplayDataGap("Observed ATM option delta is unavailable")
                rr = selected_rr(settings, timeframe)
                replay = _simulate_cached(processor, native, pd.concat(frames), int(lots), rr, delta, end)
            except (ReplayDataGap, ValueError, TypeError, KeyError) as exc:
                _report(scanner, instrument, timeframe, identity, exc)
                continue
            quantity = replay["lots"] * int(lot_size)
            charges = estimate_trade_charges(replay["entry_price"], replay["exit_price"], quantity, "OPT", settings,
                                            instrument=instrument)
            if replay["alignment_added"]:
                # One additional buy order, with the existing flat round-trip
                # estimate split evenly between its two order legs.
                charges = round(charges + float(getattr(settings, "opt_cost", 80.) or 80.) / 2, 2)
            initial_risk = (replay["initial_entry_price"] * replay["risk_pct"] / 100) * int(lots) * int(lot_size)
            target_spot = spot + side * abs(spot - float(native["initial_stop"])) * 2
            recorded_charges = charges if replay["closed"] else 0.
            recorded_pnl = round((replay["exit_price"] - replay["entry_price"]) * quantity - recorded_charges, 2)
            proof = {"guard": "replay_input", "state": "VERIFIED_PRICE_PATH", "complete_path": True,
                     "contract_verified": True, "contract_expiry": contract_context["contract_expiry"],
                     "days_to_expiry": dte, "observed_until": replay["mark_time"].isoformat(),
                     "source_engine": ENGINE_KEY, "entry_price": replay["entry_price"],
                     "exit_price": replay["exit_price"] if replay["closed"] else 0.,
                     "lots": replay["lots"], "lot_size": int(lot_size),
                     "charges": recorded_charges, "pnl": recorded_pnl}
            proof["input_sha256"] = hashlib.sha256(json.dumps({
                "contract": contract_context, "buys": replay["buys"],
                "price_path": replay["price_path"], "exit_trace": replay["exit_trace"]},
                sort_keys=True, allow_nan=False).encode()).hexdigest()
            replay["exit_trace"].append(proof)
            trade = Trade(id=trade_id, instrument=instrument, timeframe=timeframe,
                direction="LONG" if side > 0 else "SHORT", entry_price=replay["entry_price"],
                entry_time=when.tz_localize("Asia/Kolkata").to_pydatetime(), trailing_stop=replay["stop"],
                current_stop=replay["stop"], initial_stop=replay["stop"], lots=replay["lots"], lot_size=int(lot_size),
                grade="NATIVE (Hist)", confidence=1., inst_type="OPT", option_type=option_type,
                atm_strike=strike, rr_ratio=rr, target=replay["target"], trading_symbol=symbol, symbol_token=token,
                source_engine=ENGINE_KEY, source_engines=["ut", "ema"], source_lane=LANE, exit_policy_owner=LANE,
                lifecycle_id=identity, data_provenance="BACKTEST_OBSERVED_OPTION_MINUTES", current_price=replay["mark_price"],
                signal_time=when.tz_localize("Asia/Kolkata").to_pydatetime(),
                entry_spot=spot, spot_stop=float(native["initial_stop"]), spot_target=target_spot,
                underlying_entry=spot, underlying_stop=float(native["initial_stop"]), underlying_target=target_spot,
                premium_entry=replay["entry_price"], premium_stop=replay["stop"], premium_target=replay["target"],
                geometry_verified=True, geometry_source="observed_option_1min", geometry_observed_at=when.isoformat(),
                instrument_multiplier=delta if side > 0 else -delta,
                status="CLOSED" if replay["closed"] else "OPEN", execution_status="CLOSED" if replay["closed"] else "OPEN",
                exit_price=replay["exit_price"] if replay["closed"] else 0.,
                exit_time=replay["exit_time"].tz_localize("Asia/Kolkata").to_pydatetime() if replay["closed"] else None,
                exit_reason=replay["reason"], charges=recorded_charges, pnl=recorded_pnl,
                scale_in_count=int(replay["alignment_added"]), scale_in_lots=int(replay["alignment_added"]),
                frozen_initial_risk_rs=initial_risk, replay_price_path=replay["price_path"], replay_exit_trace=replay["exit_trace"])
            trade.utema_state = {key: copy.deepcopy(native[key]) for key in (
                "lifecycle_id", "side", "entry_engine", "entry_spot", "entry_time", "entry_bar", "initial_stop")}
            underlying_stop = float(native["initial_stop"])
            for update in native.get("stop_updates", []):
                if market_stamp(update["time"]) <= replay["mark_time"]:
                    value = float(update["stop"])
                    underlying_stop = max(underlying_stop, value) if side > 0 else min(underlying_stop, value)
            trade.utema_state["stop"] = underlying_stop
            trade.utema_state.update(premium_risk_pct=replay["risk_pct"], option_delta=delta, rr=rr,
                alignment_added=replay["alignment_added"], replay_buys=replay["buys"],
                replay_observed_until=replay["mark_time"].isoformat(),
                option_contract_evidence=contract_context, option_dte_at_entry=dte,
                option_delta_source="estimated_black_scholes_iv_15pct_verified_contract_expiry")
            persist(trade)
            # Mutate only the simulated ledger. Never call open/close/scale broker
            # methods, even if scanner mode changes while history is loading.
            if not _authorized(scanner, manager):
                return []
            with manager._lock:
                if not _authorized(scanner, manager):
                    return []
                existing = manager.open_trades.get(trade_id)
                if existing is None:
                    existing = next((row for row in manager.closed_trades if row.id == trade_id), None)
                if existing is not None and existing.to_dict() == trade.to_dict():
                    proven_ids.add(trade_id)
                    _report(scanner, instrument, timeframe, identity,
                            "observed_closed" if replay["closed"] else "observed_open")
                    continue
                manager.open_trades.pop(trade_id, None)
                # Recalculate after settings or candle corrections. Stable IDs
                # prevent duplicates without freezing an obsolete replay result.
                manager.closed_trades[:] = [t for t in manager.closed_trades if t.id != trade_id]
                if replay["closed"]:
                    manager.closed_trades.append(trade)
                else:
                    manager.open_trades[trade_id] = trade
                marker = getattr(manager, "_mark_dirty", None)
                if callable(marker):
                    marker()
            proven_ids.add(trade_id)
            _report(scanner, instrument, timeframe, identity, "observed_closed" if replay["closed"] else "observed_open")
        _prune_scope(scanner, manager, instrument, timeframe, dates, proven_ids)
    return []
