"""S1 lane: how a trade (or a manual/paper signal row) from the S1 engine is admitted
and managed.

The user's S1 script already selects its signals with five gates, so an S1 signal skips the
quality/grading/setup SELECTION stack other engines use, and none of the special-entry
lanes (LTP shock, volume spike, power move, cascade, reclaim, snapback) re-tag it. Every
SAFETY control stays: daily-loss and segment breakers, position/bucket/capital caps, quote
and data checks, Auto/Manual authority, EOD squareoff, broker-side stop protection. S1 has
its own entry cutoffs (15m: 14:50, 5m/1m: 15:10) and the 15:15 hard exit.

Exits for an S1 trade or row (all other engines' guards, locks and floors are skipped):
    1. Index SL1: the S1 dynamic stop (0.10-0.25% of the index) from the entry price;
       moved to entry + 0.7R once T1 prints.
    2. S1 EXIT: the engine's own exit for this signal (TradingView's EXIT label) once
       its bar has closed.
    3. Trend against: while the trade has not yet earned its lock, a closed bar whose S1
       scores show a clear trend AGAINST the trade cuts it early.
    4. Profit lock: once the premium gain reaches lock_arm_r x the premium risk, exit on a
       giveback from the peak gain that depends on the S1 trend: 25% with the trade,
       10% neutral, 0% against.
    5. Premium stop (the broker stop): starts at the premium guard -- 5/10/15% by
       timeframe, one step tighter after 14:30, one step wider at score >= 80, capped at
       10% in a clear trend regime and 15% in choppy/volatile/unknown ones -- and then
       trails the premium peak by the same guard, one step wider while the trend is with
       the trade and one step tighter when it turns against.
    6. Runner: while the S1 trend is with the trade, the T3 target does not close it;
       the trail and the profit lock do. Opposite signal and EOD stay as they are.
"""
from __future__ import annotations

import math
import copy

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

CLEAR_TREND_REGIMES = frozenset({"TRENDING_UP", "TRENDING_DOWN", "TRENDING", "STRONG_TREND", "TREND"})

# Session rows carry dynamic native fields outside TradeCandidate's dataclass.  A
# restart or EXIT copy must preserve their protection and the original seed evidence.
SESSION_STATE_FIELDS = (
    "s1_lane", "s1_score", "s1_bull_score", "s1_bear_score", "s1_setup",
    "s1_sl_pct", "s1_sl1", "s1_targets", "s1_chart_entry", "s1_chart_signal_close",
    "s1_chart_sl1", "s1_chart_sl2", "s1_chart_targets", "s1_guard_pct",
    "s1_weak_target", "s1_best_spot", "s1_t1_locked", "s1_peak_premium",
    "s1_rel_volume", "s1_atr_expansion", "s1_exit_reason", "s1_exit_time", "s1_exit_price",
    "source_signal_price", "missed_signal_reentry", "missed_signal_reentry_context",
    "missed_reentry_seed_reason", "missed_reentry_recorded_at", "tight_stop_pct",
    "lot_sizing_mode", "accepted_decision_record",
    "pnl_basis", "pnl_path_basis", "peak_pnl_basis", "max_drawdown_basis",
    "peak_pnl_gross", "max_drawdown_gross", "peak_pnl_net", "max_drawdown_net",
    "round_trip_cost",
    "exit_policy_owner", "exit_decision_record", "option_exposure_verdict",
    "s12_lane", "s12_trade_id", "s12_event_id", "s12_score", "s12_setup", "s12_entry_spot",
    "s12_stop", "s12_stop_initial", "s12_stop_pct", "s12_r_ladder", "s12_t1", "s12_t2", "s12_t3",
    "s12_t1_hit", "s12_t2_hit", "s12_t3_hit", "s12_premium_cap_pct",
    "s12_exit_reason", "s12_exit_time", "s12_exit_price",
    "utema_state",
)
_SESSION_BOOL_FIELDS = frozenset({"s1_lane", "s1_weak_target", "s1_t1_locked", "missed_signal_reentry",
                                 "s12_t1_hit", "s12_t2_hit", "s12_t3_hit"})
_SESSION_TIME_FIELDS = frozenset({"s1_exit_time", "missed_reentry_recorded_at", "s12_exit_time"})
_SESSION_DICT_FIELDS = frozenset({
    "accepted_decision_record", "missed_signal_reentry_context", "exit_decision_record", "option_exposure_verdict",
    "utema_state",
})
_SESSION_TEXT_FIELDS = frozenset({
    "s1_setup", "s1_exit_reason", "missed_reentry_seed_reason", "lot_sizing_mode",
    "exit_policy_owner", "s12_lane", "s12_trade_id", "s12_event_id", "s12_setup", "s12_exit_reason",
})
_SESSION_TARGET_FIELDS = frozenset({"s1_targets", "s1_chart_targets", "s12_r_ladder"})
_SESSION_BASIS_FIELDS = frozenset({"pnl_basis", "pnl_path_basis", "peak_pnl_basis", "max_drawdown_basis"})


def _session_json_value(value):
    """Copy JSON-compatible state; reject invalid diagnostics per field, not per row."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite session state")
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_session_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _session_json_value(item) for key, item in value.items()}
    raise TypeError("unsupported session state")


def snapshot_session_state(candidate: Any) -> dict:
    result = {}
    for name in SESSION_STATE_FIELDS:
        if not hasattr(candidate, name):
            continue
        try:
            result[name] = _session_json_value(getattr(candidate, name))
        except (TypeError, ValueError):
            continue
    return result


def restore_session_state(candidate: Any, payload: dict) -> None:
    """Restore only present, valid fields; legacy rows gain no invented native proof."""
    for name in SESSION_STATE_FIELDS:
        if name not in payload:
            continue
        try:
            value = _session_json_value(payload[name])
            if name in _SESSION_BOOL_FIELDS and not isinstance(value, bool):
                continue
            if name in _SESSION_DICT_FIELDS and not isinstance(value, dict):
                continue
            if name in _SESSION_TEXT_FIELDS and value is not None and not isinstance(value, str):
                continue
            if name == EXIT_OWNER_FIELD and str(value or "").upper() not in EXIT_OWNERS:
                continue
            if name in _SESSION_TARGET_FIELDS and value is not None:
                if not isinstance(value, list) or any(
                    isinstance(item, bool) or not isinstance(item, (int, float)) or item <= 0
                    for item in value
                ):
                    continue
            if name in _SESSION_BASIS_FIELDS and value not in {
                "NET_AFTER_ROUND_TRIP_COST", "GROSS_BEFORE_ROUND_TRIP_COST", "NET", "GROSS"
            }:
                continue
            if name in _SESSION_TIME_FIELDS and value is not None:
                value = datetime.fromisoformat(str(value))
            if name not in (_SESSION_BOOL_FIELDS | _SESSION_TIME_FIELDS | _SESSION_DICT_FIELDS
                            | _SESSION_TEXT_FIELDS | _SESSION_TARGET_FIELDS | _SESSION_BASIS_FIELDS):
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    continue
            if name in {"s1_best_spot", "s1_peak_premium"} and value is not None and value <= 0:
                continue
            setattr(candidate, name, copy.deepcopy(value))
        except (TypeError, ValueError):
            continue


def _settings(settings=None):
    if settings is not None:
        return settings
    try:
        from config.settings import get_settings

        return get_settings()
    except Exception:
        return None


def _g(s, name: str, default: float) -> float:
    try:
        value = getattr(s, name, default) if s is not None else default
        return float(default if value is None else value)
    except (TypeError, ValueError):
        return float(default)


def lane_enabled(settings=None) -> bool:
    s = _settings(settings)
    return bool(getattr(s, "s1_lane_enabled", True)) if s is not None else True


def is_s12(obj: Any) -> bool:
    """True for either native S1+S2 lane, including rows restored without source_engine."""
    if obj is None:
        return False
    source = str(getattr(obj, "source_engine", "") or "").strip().lower()
    lane = str(getattr(obj, "source_lane", "") or getattr(obj, "s12_lane", "") or "").strip().upper()
    return source == "s1s2_dual" and lane in {"D1", "D2"} or lane in {"D1", "D2"}


def is_s1(obj: Any, settings=None) -> bool:
    """True for an S1 signal, candidate, session row or trade while the lane is switched on.

    A trade restored from SQLite has lost source_engine (not a persisted column), so the
    persisted source_lane "S1" is accepted too.
    """
    if obj is None or not lane_enabled(settings):
        return False
    if str(getattr(obj, "source_engine", "") or "").strip().lower() == "s1":
        return True
    if str(getattr(obj, "source_lane", "") or "").strip().upper() == "S1":
        return True
    sources: Iterable = getattr(obj, "source_engines", None) or ()
    try:
        return [str(x).strip().lower() for x in sources] == ["s1"]
    except TypeError:
        return False


def is_utema(obj: Any) -> bool:
    """Native UT/EMA identity also survives SQLite losing source_engine."""
    return obj is not None and (
        str(getattr(obj, "source_engine", "") or "").strip().lower() == "ut_ema"
        or str(getattr(obj, "source_lane", "") or "").strip().upper() == "UTEMA"
        or str(getattr(obj, "exit_policy_owner", "") or "").strip().upper() == "UTEMA"
    )


def is_lane(obj: Any, settings=None) -> bool:
    """An own-lane engine's signal, candidate, row or trade: S1, UTS, D1, D2 or UTEMA.

    Both engines select their own entries and own their exits, so the other engines'
    selection stack and exit guards skip them; the values that differ (stop cap, RR floor,
    premium guard, cutoffs, exits) are looked up per lane.
    """
    if is_utema(obj) or is_s1(obj, settings):
        return True
    from engine import uts_lane

    return uts_lane.is_uts(obj, settings) or is_s12(obj)


def lane_tag(obj: Any, settings=None) -> str:
    """"S1", "UTS", "D1", "D2", "UTEMA" or "" -- truthy exactly when is_lane() is."""
    if is_utema(obj):
        return "UTEMA"
    if is_s1(obj, settings):
        return "S1"
    from engine import uts_lane

    if is_s12(obj):
        from engine import s12_lane

        return s12_lane.lane_of(obj)
    if uts_lane.is_uts(obj, settings):
        return uts_lane.LANE_TAG
    return ""


# Exit-policy ownership, frozen when a position is admitted.
#
# is_s1/is_uts answer "may this lane ADMIT a new trade?" and so follow the dashboard
# switches. Exit management used the same answer, so switching a lane off mid-position
# re-routed an open S1 row onto the generic QP stack (and an open UTS trade onto the S1
# policy inside exit_decision) without anything about the trade having changed (guard
# audit 2026-10-06). The owner is stamped once, persisted with the row/trade, and read
# back by every management path regardless of the switches; the switches keep governing
# admission only. "GENERIC" records that the trade was admitted outside both lanes.
EXIT_OWNER_FIELD = "exit_policy_owner"
EXIT_OWNER_GENERIC = "GENERIC"
EXIT_OWNERS = frozenset({"S1", "UTS", "D1", "D2", "UTEMA", EXIT_OWNER_GENERIC})


def frozen_exit_owner(obj: Any) -> str:
    """The persisted owner, or "" for a legacy row/trade that predates the stamp."""
    value = str(getattr(obj, EXIT_OWNER_FIELD, "") or "").strip().upper()
    return value if value in EXIT_OWNERS else ""


def stamp_exit_owner(obj: Any, settings=None) -> str:
    """Freeze the exit-policy owner on ``obj`` from the admission-time lane switches.

    Idempotent: an existing stamp is never overwritten, so re-scans, EXIT copies and
    restarts keep the owner the position was admitted with.
    """
    if obj is None:
        return ""
    owner = frozen_exit_owner(obj)
    if owner:
        return owner
    owner = lane_tag(obj, settings) or EXIT_OWNER_GENERIC
    try:
        setattr(obj, EXIT_OWNER_FIELD, owner)
    except Exception:
        return owner
    return owner


def managed_lane(obj: Any, settings=None) -> str:
    """Frozen native lane, or "" for an OPEN position's generic exit management.

    Uses the frozen owner. A legacy position without one is stamped from the current
    switches on its first management pass, so it is frozen from then on.
    """
    owner = stamp_exit_owner(obj, settings)
    return "" if owner in {"", EXIT_OWNER_GENERIC} else owner


def is_managed_lane(obj: Any, settings=None) -> bool:
    return bool(managed_lane(obj, settings))


def lane_sl_max_pct(obj: Any, settings=None) -> float:
    """Index stop ceiling (% of spot) the final policy applies to a lane entry."""
    s = _settings(settings)
    if lane_tag(obj, s) == "UTEMA":
        return .21  # The native 0.2% stop rounds outwards to the index tick.
    if lane_tag(obj, s) == "UTS":
        from engine import uts_lane

        return uts_lane.sl_max_pct(s)
    if lane_tag(obj, s) in {"D1", "D2"}:
        from engine import s12_lane

        return s12_lane.sl_max_pct(s)
    return _g(s, "s1_sl_max_pct", 0.25)


def _flag(s, name: str, default: bool) -> bool:
    value = getattr(s, name, default) if s is not None else default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default if value is None else value)


def signal_only(obj: Any, settings=None) -> bool:
    """True when an S1 signal must not become an Auto order (s1_lane_signal_only)."""
    s = _settings(settings)
    tag = lane_tag(obj, s)
    if tag == "UTEMA":
        return False
    if tag in {"D1", "D2"}:
        from engine import s12_lane

        return s12_lane.signal_only(s)
    return tag == "S1" and _flag(s, "s1_lane_signal_only", True)


def lane_min_entry_rr(obj: Any, settings=None) -> float:
    s = _settings(settings)
    if lane_tag(obj, s) == "UTEMA":
        return 0.0
    if lane_tag(obj, s) == "UTS":
        from engine import uts_lane

        return uts_lane.min_entry_rr(s)
    if lane_tag(obj, s) in {"D1", "D2"}:
        from engine import s12_lane

        return s12_lane.min_entry_rr(s)
    floor = min_entry_rr(s)
    if _flag(s, "s1_lane_rr_floor_at_target", True):
        # 2026-10-08 audit AC-07: a 0.5R floor admitted geometry below the 1R target the
        # trade is managed to (0.7R on a weak move), which cannot pay after costs. The
        # floor is the base target; 0.02R absorbs tick rounding of rebuilt levels.
        floor = max(floor, target_rr(s, weak=False) - 0.02)
    return floor


def lane_entry_cutoff(tag: str, timeframe: str, settings=None) -> str:
    """"HH:MM" last new entry for a lane signal: the system-wide last entry (2026-09-26
    user: 15m 14:50, 5m 15:10, 1m 15:14) for both lanes."""
    from engine import entry_window

    return entry_window.last_entry(timeframe, settings)


def lane_entry_allowed(signal_time, timeframe: str, settings=None, now=None) -> bool:
    """May a lane signal still open a trade? Entry time = ``now`` live, bar close in replay."""
    from engine import entry_window

    return entry_window.entry_allowed(signal_time, timeframe, settings, now=now)


def lane_intrabar_wait_seconds(obj: Any, settings=None) -> float:
    s = _settings(settings)
    if lane_tag(obj, s) == "UTS":
        from engine import uts_lane
        return uts_lane.intrabar_wait_seconds(obj, s)
    if lane_tag(obj, s) in {"D1", "D2", "UTEMA"}:
        return 0.0
    one_minute = str(getattr(obj, "timeframe", "") or "") == "1min"
    name = "s1_lane_intrabar_wait_seconds_1min" if one_minute else "s1_lane_intrabar_wait_seconds"
    default = 5.0 if one_minute else 15.0
    value = getattr(s, name, None) if s is not None else None
    return default if value is None else max(0.0, float(value))


def lane_exit_fields(sig: Any) -> tuple:
    """(reason, time, price) of the engine's own exit for this signal, either lane."""
    if is_utema(sig):
        from engine import ut_ema_lane

        return ut_ema_lane.lane_exit_fields(sig)
    for prefix in ("s1", "uts", "s12"):
        reason = getattr(sig, f"{prefix}_exit_reason", None)
        when = getattr(sig, f"{prefix}_exit_time", None)
        if reason or when is not None:
            return reason or "", when, getattr(sig, f"{prefix}_exit_price", None)
    return "", None, None


def _tf_minutes(timeframe: str) -> int:
    text = str(timeframe or "").lower().replace("min", "").replace("m", "").strip()
    try:
        return max(1, int(float(text)))
    except ValueError:
        return 5


def _hhmm(text: str, default: int) -> int:
    try:
        hh, mm = str(text).split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return default


def entry_cutoff(timeframe: str, settings=None) -> str:
    """Last S1 entry time ("HH:MM"): the system-wide last entry (15m 14:50, 5m 15:10,
    1m 15:14; engine/entry_window.py)."""
    return lane_entry_cutoff("S1", timeframe, settings)


def _ladder(s) -> list:
    # Ordered rungs (not a de-duplicated set): equal percentages must not remove a step.
    return [_g(s, "s1_lane_guard_pct_1min", 5.0), _g(s, "s1_lane_guard_pct_5min", 10.0),
            _g(s, "s1_lane_guard_pct_15min", 15.0)]


def _regime_cap_step(regime: Optional[str]) -> int:
    """Clear trend: at most the middle rung (10%). Choppy/volatile/unknown: the top (15%)."""
    return 1 if str(regime or "").strip().upper() in CLEAR_TREND_REGIMES else 2


def guard_step(timeframe: str, when: Optional[datetime], score: float, settings=None,
               regime: Optional[str] = None) -> int:
    s = _settings(settings)
    tf = _tf_minutes(timeframe)
    step = 0 if tf <= 1 else 1 if tf <= 5 else 2
    if when is not None:
        late = _hhmm(getattr(s, "s1_lane_guard_late_after", "14:30") if s is not None else "14:30", 14 * 60 + 30)
        if when.hour * 60 + when.minute >= late:
            step -= 1
    if float(score or 0.0) >= _g(s, "s1_lane_guard_strong_score", 80.0):
        step += 1
    return max(0, min(_regime_cap_step(regime), step))


def premium_guard_pct(timeframe: str, when: Optional[datetime], score: float, settings=None,
                      regime: Optional[str] = None) -> float:
    """Max premium loss per trade: 5 / 10 / 15 % by timeframe, adjusted by time, strength and
    regime (clear trend caps it at 10%, choppy/volatile/unknown allow 15%)."""
    s = _settings(settings)
    return _ladder(s)[guard_step(timeframe, when, score, s, regime)]


def premium_stop_distance(premium: float, index_stop_distance: float, delta: float, guard_pct: float) -> float:
    """Premium risk: the S1 index stop mapped through delta, never wider than the guard."""
    premium = float(premium or 0.0)
    if premium <= 0:
        return 0.0
    guard = premium * float(guard_pct) / 100.0
    mapped = abs(float(index_stop_distance or 0.0)) * abs(float(delta or 0.5))
    risk = min(guard, mapped) if mapped > 0 else guard
    return max(0.05, risk)


def weak_move(adx, rel_volume, atr_expansion, settings=None) -> bool:
    """Not much move: at least two of volume below s1_lane_weak_rel_volume x its 20-bar
    average, ADX below s1_lane_weak_adx, and ATR below its 50-bar average."""
    s = _settings(settings)
    signs = 0
    for value, limit in ((rel_volume, _g(s, "s1_lane_weak_rel_volume", 1.2)),
                         (adx, _g(s, "s1_lane_weak_adx", 20.0)),
                         (atr_expansion, 1.0)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number < limit:
            signs += 1
    return signs >= 2


def _trend_regime_with(regime: Optional[str], direction: str) -> bool:
    reg = str(regime or "").strip().upper()
    side = str(direction or "").strip().upper()
    return reg in CLEAR_TREND_REGIMES and (
        (reg.endswith("UP") and side in {"LONG", "BUY"})
        or (reg.endswith("DOWN") and side in {"SHORT", "SELL"})
    )


def entry_is_weak(signal: Any, regime: Optional[str], direction: str, settings=None) -> bool:
    """A weak signal bar and no clear trend regime on the trade's side."""
    if _trend_regime_with(regime, direction):
        return False
    return weak_move(getattr(signal, "adx_value", None), getattr(signal, "s1_rel_volume", None),
                     getattr(signal, "s1_atr_expansion", None), settings)


def target_rr(settings=None, weak: bool = False) -> float:
    """S1 target in R of the premium risk: 1R normally, 0.7R when the move is weak."""
    s = _settings(settings)
    return max(0.1, _g(s, "s1_lane_weak_rr", 0.7) if weak else _g(s, "s1_lane_base_rr", 1.0))


def min_entry_rr(settings=None) -> float:
    """The final entry check's RR floor for S1 trades (their targets start at 0.7-1R)."""
    return max(0.1, _g(_settings(settings), "s1_lane_min_entry_rr", 0.5))


def special_trend(trade: Any, market: Optional[dict], regime: Optional[str], settings=None) -> bool:
    """A special trending moment: a heavy-trend entry, or S1's closed-bar score gap on the
    trade's side >= s1_lane_special_trend_margin while the regime is a clear trend its way."""
    s = _settings(settings)
    if str(getattr(trade, "lot_sizing_mode", "") or "").lower() == "heavy_trend":
        return True
    if not isinstance(market, dict):
        return False
    try:
        gap = (float(market.get("s1_bull_score")) - float(market.get("s1_bear_score"))) * _direction(trade)
    except (TypeError, ValueError):
        return False
    direction = "LONG" if _direction(trade) == 1 else "SHORT"
    return gap >= _g(s, "s1_lane_special_trend_margin", 40.0) and _trend_regime_with(regime, direction)


def runner_ceiling_r(trade: Any, market: Optional[dict], regime: Optional[str], settings=None) -> float:
    """4R (T3) in an ordinary market; s1_lane_runner_max_r (30R) in a special trend."""
    s = _settings(settings)
    normal = _g(s, "s1_t3_r", 4.0)
    if special_trend(trade, market, regime, s):
        return max(normal, _g(s, "s1_lane_runner_max_r", 30.0))
    return normal


def nearer_target(current: float, new: float, price_direction: int) -> float:
    """Only ever bring an existing target closer to entry (never invent or widen one)."""
    current, new = float(current or 0.0), float(new or 0.0)
    if current <= 0 or new <= 0:
        return current
    return min(current, new) if price_direction >= 0 else max(current, new)


def reentry_bars(timeframe: str, settings=None) -> int:
    """Bars after the signal candle's close a missed S1 signal may re-enter: 15m 1, 5m 1,
    1m 3 (s1_lane_reentry_bars_*)."""
    s = _settings(settings)
    tf = _tf_minutes(timeframe)
    name, default = (
        ("s1_lane_reentry_bars_15min", 1.0) if tf >= 15
        else ("s1_lane_reentry_bars_5min", 1.0) if tf >= 5
        else ("s1_lane_reentry_bars_1min", 3.0)
    )
    return max(1, int(_g(s, name, default)))


def reentry_max_age_seconds(timeframe: str, settings=None) -> float:
    """Age limit, counted from the signal candle's OPEN (the signal timestamp): the signal
    candle itself plus reentry_bars() more candles of the same timeframe."""
    return float((reentry_bars(timeframe, settings) + 1) * _tf_minutes(timeframe) * 60)


def reentry_strength(seed: Any, state: Optional[dict], settings=None) -> tuple:
    """(ok, current_score, detail) for re-entering a missed S1 signal.

    The latest closed-bar S1 score on the seed's side must be at least the missed signal's
    own score (and the S1 minimum), and the trend must not be against the trade."""
    s = _settings(settings)
    if not isinstance(state, dict):
        return False, 0.0, "no S1 state for this chart"
    side = 1 if str(getattr(seed, "direction", "LONG") or "LONG").upper() == "LONG" else -1
    try:
        bull = float(state.get("s1_bull_score"))
        bear = float(state.get("s1_bear_score"))
    except (TypeError, ValueError):
        return False, 0.0, "S1 scores unavailable"
    bar_time = state.get("s1_state_bar_time")
    signal_ts = getattr(seed, "source_signal_timestamp", None) or getattr(seed, "signal_timestamp", None)
    try:
        if bar_time is not None and signal_ts is not None:
            bt = bar_time.replace(tzinfo=None) if getattr(bar_time, "tzinfo", None) else bar_time
            st = signal_ts.replace(tzinfo=None) if getattr(signal_ts, "tzinfo", None) else signal_ts
            if bt < st:
                return False, 0.0, "S1 state is older than the missed signal"
    except (TypeError, AttributeError):
        pass
    now_score = bull if side > 0 else bear
    last_score = float(getattr(seed, "s1_score", 0.0) or 0.0)
    floor = max(last_score, _g(s, "s1_min_score", 60.0))
    if now_score + 1e-9 < floor:
        return False, now_score, f"S1 score {now_score:.0f} below the missed signal's {floor:.0f}"
    if trend_bias(state, side, s) < 0:
        return False, now_score, "S1 trend turned against the missed signal"
    return True, now_score, f"S1 score {now_score:.0f} >= missed {last_score:.0f}"


def trend_bias(state: Optional[dict], direction: int, settings=None) -> int:
    """+1 when the S1 scores on the last closed bar show a clear trend WITH the trade,
    -1 when clearly AGAINST it, 0 otherwise. Clear = score gap >= s1_lane_trend_margin."""
    if not isinstance(state, dict):
        return 0
    try:
        bull = float(state.get("s1_bull_score"))
        bear = float(state.get("s1_bear_score"))
    except (TypeError, ValueError):
        return 0
    margin = _g(_settings(settings), "s1_lane_trend_margin", 20.0)
    gap = (bull - bear) * (1 if direction >= 0 else -1)
    return 1 if gap >= margin else -1 if -gap >= margin else 0


@dataclass
class S1ExitDecision:
    exit: bool
    reason: str = ""
    index_stop: float = 0.0
    detail: str = ""
    premium_stop: float = 0.0   # trailed premium stop to ratchet the broker stop to (0 = none)
    runner: bool = False        # trend with the trade: the target must not close it
    target_premium: float = 0.0  # lower the target to this (the move died), 0 = unchanged


def _direction(trade: Any) -> int:
    return 1 if str(getattr(trade, "direction", "LONG") or "LONG").upper() == "LONG" else -1


def _entry_premium(trade: Any) -> float:
    """Trades carry entry_price; manual/session rows (TradeCandidate) carry price."""
    for name in ("entry_price", "price"):
        try:
            value = float(getattr(trade, name, 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def index_levels(trade: Any, settings=None):
    """(entry, sl1, t1, dist) on the index, from the persisted entry geometry."""
    s = _settings(settings)
    d = _direction(trade)
    entry = float(getattr(trade, "underlying_entry", 0.0) or getattr(trade, "entry_spot", 0.0) or 0.0)
    stop = float(getattr(trade, "underlying_stop", 0.0) or getattr(trade, "spot_stop", 0.0) or 0.0)
    dist = abs(entry - stop) if entry > 0 and stop > 0 else 0.0
    if dist <= 0 and entry > 0:
        mode = str(getattr(s, "s1_sl_mode", "fixed") if s is not None else "fixed")
        key, default = ("s1_sl1_pct", 0.10) if mode == "fixed" else ("s1_sl_max_pct", 0.25)
        dist = entry * _g(s, key, default) / 100.0
    t1_r = _g(s, "s1_t1_r", 2.0)
    return entry, entry - d * dist, entry + d * dist * t1_r, dist


def exit_decision(
    trade: Any,
    spot: float,
    premium: float,
    now: Optional[datetime] = None,
    engine_exit: Optional[dict] = None,
    settings=None,
    trend: int = 0,
    regime: Optional[str] = None,
    market: Optional[dict] = None,
    observed_contract_ticks=None,
    trend_evidence: Optional["TrendEvidence"] = None,
) -> S1ExitDecision:
    """One monitor pass for an S1 trade or row. Mutates only s1_* bookkeeping on it.

    ``trend`` is trend_bias() from the latest CLOSED bar after the signal bar (the caller
    passes 0 when there is no such bar yet); ``market`` is that bar's S1 state
    (closed_bar_state), used to drop the target to 0.7R when the move has died.
    ``trend_evidence`` (closed_bar_trend_evidence) supersedes ``trend`` when given and
    separates UNKNOWN from a measured NEUTRAL.
    """
    s = _settings(settings)
    if managed_lane(trade, s) == "UTEMA":
        from engine import ut_ema_lane

        return ut_ema_lane.exit_decision(
            trade, spot, premium, now=now, engine_exit=engine_exit, settings=s,
            market=market, observed_contract_ticks=observed_contract_ticks,
        )
    from engine import uts_lane

    # Frozen owner, not the admission switch: turning UTS off must not hand an open
    # UTS trade to the S1 policy below.
    if managed_lane(trade, s) == uts_lane.LANE_TAG:
        return uts_lane.exit_decision(trade, spot, premium, now=now, engine_exit=engine_exit, settings=s,
                                      observed_contract_ticks=observed_contract_ticks)
    if managed_lane(trade, s) in {"D1", "D2"}:
        from engine import s12_lane

        return s12_lane.exit_decision(trade, spot, premium, now=now, engine_exit=engine_exit,
                                      settings=s, observed_contract_ticks=observed_contract_ticks,
                                      market=market)
    if trend_evidence is not None:
        trend = int(trend_evidence.trend)
        setattr(trade, "s1_trend_evidence", trend_evidence.describe())
    trend_unknown = trend_evidence is not None and not trend_evidence.known
    trend_note = f"; {trend_evidence.describe()}" if trend_evidence is not None else ""
    d = _direction(trade)
    entry, sl1, t1, dist = index_levels(trade, s)
    spot = float(spot or 0.0)
    premium = float(premium or 0.0)
    runner = trend > 0

    # 1. Index SL1, lifted to entry + 0.7R once T1 prints (the extreme since entry decides).
    if spot > 0:
        best = getattr(trade, "s1_best_spot", None)
        best = spot if best is None else (max(best, spot) if d == 1 else min(best, spot))
        setattr(trade, "s1_best_spot", best)
        if entry > 0 and dist > 0 and not getattr(trade, "s1_t1_locked", False) and (best - t1) * d >= 0:
            setattr(trade, "s1_t1_locked", True)
    stop = sl1
    if getattr(trade, "s1_t1_locked", False) and entry > 0:
        stop = entry + d * dist * _g(s, "s1_lane_t1_lock_r", 0.7)
    if spot > 0 and entry > 0 and (spot - stop) * d <= 0:
        return S1ExitDecision(True, "S1_T1_LOCK" if getattr(trade, "s1_t1_locked", False) else "S1_SL1", stop,
                              f"index {spot:.2f} crossed {stop:.2f}")

    # 2. The S1 engine's own exit for this signal.
    if engine_exit:
        return S1ExitDecision(True, "S1_EXIT", stop, str(engine_exit.get("reason") or "S1 exit"))

    entry_premium = _entry_premium(trade)
    if premium <= 0 or entry_premium <= 0:
        return S1ExitDecision(False, "", stop, runner=runner)

    price_direction = d if str(getattr(trade, "inst_type", "OPT")).upper() == "FUT" else 1
    stored = getattr(trade, "s1_peak_premium", None)
    if stored is None:
        # Legacy rows: reconstruct the favourable price from the persisted rupee peak.
        qty = float(getattr(trade, "quantity", 0) or 0) or (
            float(getattr(trade, "lots", 0) or 0) * float(getattr(trade, "lot_size", 0) or 0))
        peak_rs = float(getattr(trade, "peak_pnl", 0.0) or 0.0)
        stored = entry_premium + price_direction * peak_rs / qty if qty > 0 and peak_rs > 0 else entry_premium
    peak = (max if price_direction == 1 else min)(float(stored), premium)
    setattr(trade, "s1_peak_premium", peak)
    initial = float(getattr(trade, "initial_stop", 0.0) or getattr(trade, "premium_stop", 0.0)
                    or getattr(trade, "stop", 0.0) or 0.0)
    risk = price_direction * (entry_premium - initial)
    if risk <= 0 or risk >= entry_premium:
        risk = entry_premium * 0.10
    peak_gain = price_direction * (peak - entry_premium)
    gain = price_direction * (premium - entry_premium)
    armed = peak_gain >= _g(s, "s1_lane_lock_arm_r", 1.0) * risk

    # 3. Cut the loser early when the trend has clearly turned against it.
    if trend < 0 and not armed:
        return S1ExitDecision(True, "S1_TREND_AGAINST", stop,
                              f"S1 trend turned against the trade; gain {gain:.2f}{trend_note}")

    # 4. Profit lock with a trend-dependent giveback. UNKNOWN (no usable post-signal
    #    closed bar) has its own giveback, bounded by the with-trend value so missing
    #    evidence can never grant more room than a confirmed trend would.
    with_trend = _g(s, "s1_lane_giveback_with_trend", 25.0)
    if trend > 0:
        giveback = with_trend
    elif trend < 0:
        giveback = _g(s, "s1_lane_giveback_against", 0.0)
    elif trend_unknown:
        giveback = min(with_trend, _g(s, "s1_lane_giveback_unknown",
                                      _g(s, "s1_lane_giveback_neutral", 10.0)))
    else:
        giveback = _g(s, "s1_lane_giveback_neutral", 10.0)
    if armed and gain < peak_gain * (1.0 - giveback / 100.0) - 1e-9:
        return S1ExitDecision(True, "S1_PROFIT_LOCK", stop,
                              f"gain {gain:.2f} gave back more than {giveback:.0f}% of peak {peak_gain:.2f}"
                              f"{trend_note}")

    # 5. Runner ceiling: while the trend is with the trade the target does not close it;
    #    the profit is taken at T3 (4R), or at s1_lane_runner_max_r (30R) in a special trend.
    runner_max = runner_ceiling_r(trade, market, regime, s)
    if runner and runner_max > 0 and gain >= runner_max * risk - 1e-9:
        return S1ExitDecision(True, "S1_RUNNER_MAX", stop,
                              f"gain {gain:.2f} reached {runner_max:.0f}R ({runner_max * risk:.2f})")

    # 6. The move died: a closed bar after the signal is weak and the trend is not with
    #    the trade, so the target comes in from 1R to 0.7R.
    target_premium = 0.0
    if not runner and isinstance(market, dict) and weak_move(
        market.get("adx_value"), market.get("s1_rel_volume"), market.get("s1_atr_expansion"), s
    ):
        target_premium = entry_premium + price_direction * risk * target_rr(s, weak=True)
        setattr(trade, "s1_weak_target", True)

    # 7. Premium trail for the broker stop: guard % from the peak, wider with the trend.
    premium_stop = 0.0
    if price_direction == 1 and peak > entry_premium:
        step = guard_step(getattr(trade, "timeframe", ""), None,
                          float(getattr(trade, "s1_score", 0.0) or 0.0), s, regime) + (1 if trend > 0 else -1 if trend < 0 else 0)
        pct = _ladder(s)[max(0, min(_regime_cap_step(regime), step))]
        premium_stop = peak * (1.0 - pct / 100.0)
    # 8. Breakeven (2026-10-08 audit MC2): S1 losers took a full 1R (about -1.07R with
    #    overshoot) because nothing protected a trade between entry and the 0.5R lock.
    #    Once the peak gain reaches s1_lane_breakeven_arm_r of the risk, the premium stop
    #    moves to entry plus the round-trip cost per unit. Only ever raises the stop.
    if (price_direction == 1 and _flag(s, "s1_lane_breakeven_enabled", True)
            and peak_gain >= _g(s, "s1_lane_breakeven_arm_r", 0.25) * risk - 1e-9):
        qty = float(getattr(trade, "quantity", 0) or 0) or (
            float(getattr(trade, "lots", 0) or 0) * float(getattr(trade, "lot_size", 0) or 0))
        cost_units = _g(s, "s1_lane_breakeven_cost_rs", 140.0) / qty if qty > 0 else 0.0
        breakeven = entry_premium + max(0.0, cost_units)
        if breakeven < peak:
            premium_stop = max(premium_stop, breakeven)
    return S1ExitDecision(False, "", stop, premium_stop=premium_stop, runner=runner,
                          target_premium=target_premium)


def engine_exit_for(trade: Any, result: Optional[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """The lane engine's exit for the signal this trade came from, once its bar has closed.

    An exit printed on the still-forming bar can vanish before the close (TradingView
    repaints it too); the lane's own index stop covers the real-time move meanwhile.
    Works for both lanes: the S1 EXIT label, or UT SMART EXIT's reverse / smart exit.
    """
    if managed_lane(trade) == "UTEMA":
        from engine import ut_ema_lane

        return ut_ema_lane.engine_exit_for(trade, result, now)
    from engine.scanner_modes import native_result
    result = native_result(trade, result)
    if not isinstance(result, dict):
        return None
    if managed_lane(trade) in {"D1", "D2"}:
        from engine import s12_lane

        return s12_lane.engine_exit_for(trade, result, now)
    ts = getattr(trade, "signal_time", None) or getattr(trade, "signal_timestamp", None)
    if ts is None:
        return None
    from engine import uts_lane as _uts

    if managed_lane(trade) == _uts.LANE_TAG and _uts.repaint_exit(trade, result, now):
        # An intra-candle entry whose flip is gone (checked every 10s while the candle
        # forms, then once on the closed candle).
        return {"reason": "REPAINT", "time": now, "price": None}
    try:
        import pandas as pd

        def market_time(value):
            stamp = pd.Timestamp(value)
            if pd.isna(stamp):
                raise ValueError("Missing S1 timestamp")
            return stamp.tz_convert("Asia/Kolkata").tz_localize(None) if stamp.tzinfo else stamp

        want = market_time(ts)
        clock = market_time(now) if now is not None else None
    except Exception:
        return None
    d = "BUY" if _direction(trade) == 1 else "SELL"
    from engine import uts_lane

    engine_key = uts_lane.ENGINE_KEY if managed_lane(trade) == uts_lane.LANE_TAG else "s1"
    for sig in reversed(result.get("signals") or []):
        if str(getattr(sig, "source_engine", "") or "").lower() != engine_key:
            continue
        try:
            st = market_time(getattr(sig, "timestamp", None))
        except Exception:
            continue
        if st == want and getattr(sig, "signal_type", "") == d:
            reason, exit_time, exit_price = lane_exit_fields(sig)
            if not reason:
                return None
            try:
                et = market_time(exit_time)
            except Exception:
                return None
            if et <= want:
                # The chart booked the signal bar's open and stopped out inside that bar,
                # before the live order (at the bar's close) existed. Not this trade's exit.
                return None
            if clock is not None:
                bar_close = et + pd.Timedelta(minutes=_tf_minutes(getattr(trade, "timeframe", "")))
                if bar_close > clock:
                    return None
            return {"reason": reason, "time": exit_time, "price": exit_price}
    return None


def closed_bar_state(trade: Any, result: Optional[dict]) -> Optional[dict]:
    """The engine's last closed-bar state, only once that bar is after the signal bar."""
    if managed_lane(trade) == "UTEMA":
        from engine import ut_ema_lane

        return ut_ema_lane.closed_bar_state(trade, result)
    from engine.scanner_modes import native_result
    result = native_result(trade, result)
    if not isinstance(result, dict):
        return None
    if managed_lane(trade) in {"D1", "D2"}:
        from engine import s12_lane

        return s12_lane.closed_bar_state(trade, result)
    state = result.get("state") if isinstance(result.get("state"), dict) else None
    if not state:
        return None
    try:
        import pandas as pd

        bar = state.get("s1_state_bar_time")
        sig = getattr(trade, "signal_time", None) or getattr(trade, "signal_timestamp", None)
        if bar is None or sig is None:
            return None
        norm = lambda v: (pd.Timestamp(v).tz_convert("Asia/Kolkata").tz_localize(None)
                          if pd.Timestamp(v).tzinfo else pd.Timestamp(v))
        if norm(bar) <= norm(sig):
            return None
    except Exception:
        return None
    return state


def closed_bar_trend(trade: Any, result: Optional[dict], settings=None) -> int:
    """trend_bias() from the engine's last closed bar, only once that bar is after the
    signal bar (the signal bar itself agrees with the trade by construction)."""
    state = closed_bar_state(trade, result)
    return trend_bias(state, _direction(trade), settings) if state else 0


# A closed bar older than this many bars is stale evidence (a stalled engine or feed).
TREND_STALE_BARS = 3


@dataclass(frozen=True)
class TrendEvidence:
    """The S1 trend an exit pass used, with where it came from and how old it is.

    ``known`` False means there was no usable post-signal closed bar: none had closed
    yet, the engine state was missing or unparseable, or the bar was stale. That is
    UNKNOWN, not a measured "no clear trend", and exit_decision prices it with its own
    giveback (s1_lane_giveback_unknown) instead of silently reusing NEUTRAL.
    """
    trend: int
    known: bool
    source: str
    age_seconds: Optional[float] = None

    @property
    def label(self) -> str:
        if not self.known:
            return "UNKNOWN"
        return "WITH" if self.trend > 0 else "AGAINST" if self.trend < 0 else "NEUTRAL"

    def describe(self) -> str:
        age = f", age {self.age_seconds:.0f}s" if self.age_seconds is not None else ""
        return f"trend {self.label} ({self.source}{age})"


def closed_bar_trend_evidence(
    trade: Any, result: Optional[dict], settings=None, now: Optional[datetime] = None
) -> TrendEvidence:
    """closed_bar_trend() plus its provenance and age.

    Missing evidence never grants more room than measured evidence would: a stale bar
    keeps an AGAINST reading (protective) but drops a WITH reading to UNKNOWN, so an old
    favourable score cannot keep widening the giveback or the premium trail.
    """
    from engine.scanner_modes import native_result

    native = native_result(trade, result)
    raw_state = native.get("state") if isinstance(native, dict) else None
    if not isinstance(raw_state, dict) or not raw_state:
        return TrendEvidence(0, False, "engine_state_missing")
    state = closed_bar_state(trade, result)
    if not state:
        age = None
        source = "no_closed_bar_after_signal"
        if now is not None:
            try:
                import pandas as pd
                def norm(value):
                    stamp = pd.Timestamp(value)
                    return stamp.tz_convert("Asia/Kolkata").tz_localize(None) if stamp.tzinfo else stamp
                minutes = _tf_minutes(getattr(trade, "timeframe", ""))
                bar = norm(raw_state["s1_state_bar_time"])
                signal = norm(getattr(trade, "signal_time", None) or getattr(trade, "signal_timestamp", None))
                clock = norm(now)
                age = max(0.0, (clock - bar - pd.Timedelta(minutes=minutes)).total_seconds())
                # Signal timestamps are bar opens; the first later bar closes two frames later.
                grace = max(1.0, float(getattr(settings, "scanner_stale_after_seconds", 5.0) or 5.0))
                if clock > signal + pd.Timedelta(minutes=2 * minutes, seconds=grace):
                    source = "post_signal_bar_overdue"
            except (KeyError, TypeError, ValueError):
                pass
        return TrendEvidence(0, False, source, age)
    try:
        float(state.get("s1_bull_score"))
        float(state.get("s1_bear_score"))
    except (TypeError, ValueError):
        return TrendEvidence(0, False, "closed_bar_scores_missing")
    trend = trend_bias(state, _direction(trade), settings)
    age = None
    if now is not None:
        try:
            import pandas as pd

            norm = lambda v: (pd.Timestamp(v).tz_convert("Asia/Kolkata").tz_localize(None)
                              if pd.Timestamp(v).tzinfo else pd.Timestamp(v))
            bar_minutes = _tf_minutes(getattr(trade, "timeframe", ""))
            bar_close = norm(state.get("s1_state_bar_time")) + pd.Timedelta(minutes=bar_minutes)
            age = max(0.0, float((norm(now) - bar_close).total_seconds()))
            if age > TREND_STALE_BARS * bar_minutes * 60.0:
                return TrendEvidence(min(trend, 0), trend < 0, "stale_closed_bar", age)
        except Exception:
            age = None
    return TrendEvidence(trend, True, "closed_bar", age)


# Dashboard-editable S1 settings: name -> (kind, *bounds_or_choices). Shared by the
# server's configure validation/forwarding and the broadcast config, so the three
# cannot drift. kind: float | int | bool | time | enum.
S1_SETTING_SPEC = {
    "s1_candle_type": ("enum", ("standard", "heikin_ashi")),
    "s1_min_score": ("float", 0.0, 100.0),
    "s1_conflict_margin": ("float", 0.0, 100.0),
    "s1_min_adx": ("float", 0.0, 60.0),
    "s1_use_htf": ("bool",),
    "s1_signal_cooldown": ("int", 0, 50),
    "s1_chart_sl1_pct": ("float", 0.01, 10.0),
    "s1_chart_sl2_pct": ("float", 0.05, 15.0),
    "s1_session_end": ("time",),
    "s1_sl_mode": ("enum", ("fixed", "anchor", "atr")),
    "s1_sl1_pct": ("float", 0.01, 2.0),
    "s1_sl_min_pct": ("float", 0.01, 2.0),
    "s1_sl_max_pct": ("float", 0.01, 2.0),
    "s1_sl_atr_mult": ("float", 0.1, 5.0),
    "s1_use_sl2": ("bool",),
    "s1_sl2_pct": ("float", 0.05, 5.0),
    "s1_t1_r": ("float", 0.5, 10.0),
    "s1_t2_r": ("float", 0.5, 10.0),
    "s1_t3_r": ("float", 0.5, 20.0),
    "s1_trail_start_r": ("float", 0.25, 10.0),
    "s1_trail_mult": ("float", 0.1, 10.0),
    "s1_exit_conf_bars": ("int", 1, 10),
    "s1_circuit_pause": ("int", 0, 100),
    "s1_max_consec_loss_nifty": ("int", 0, 50),
    "s1_max_consec_loss_banknifty": ("int", 0, 50),
    "s1_max_consec_loss_sensex": ("int", 0, 50),
    "s1_max_consec_loss_midcpnifty": ("int", 0, 50),
    "s1_entry_mode": ("enum", ("pine", "close")),
    "s1_lane_enabled": ("bool",),
    "s1_lane_guard_pct_1min": ("float", 1.0, 50.0),
    "s1_lane_guard_pct_5min": ("float", 1.0, 50.0),
    "s1_lane_guard_pct_15min": ("float", 1.0, 50.0),
    "s1_lane_guard_late_after": ("time",),
    "s1_lane_guard_strong_score": ("float", 0.0, 100.0),
    "s1_lane_t1_lock_r": ("float", 0.0, 5.0),
    "s1_lane_lock_arm_r": ("float", 0.1, 10.0),
    "s1_lane_rr_floor_at_target": ("bool",),
    "s1_lane_signal_only": ("bool",),
    "s1_lane_breakeven_enabled": ("bool",),
    "s1_lane_breakeven_arm_r": ("float", 0.05, 5.0),
    "s1_lane_breakeven_cost_rs": ("float", 0.0, 2000.0),
    "s1_sl1_pct_5min": ("float", 0.0, 2.0),
    "s1_sl1_pct_15min": ("float", 0.0, 2.0),
    "s1_lane_giveback_with_trend": ("float", 0.0, 95.0),
    "s1_lane_giveback_neutral": ("float", 0.0, 95.0),
    "s1_lane_giveback_unknown": ("float", 0.0, 95.0),
    "s1_lane_giveback_against": ("float", 0.0, 95.0),
    "s1_lane_trend_margin": ("float", 1.0, 100.0),
    "s1_lane_intrabar_wait_seconds": ("float", 0.0, 120.0),
    "s1_lane_intrabar_wait_seconds_1min": ("float", 0.0, 60.0),
    "s1_lane_forming_recompute_seconds": ("float", 0.0, 120.0),
    "s1_lane_base_rr": ("float", 0.3, 10.0),
    "s1_lane_weak_rr": ("float", 0.3, 10.0),
    "s1_lane_runner_max_r": ("float", 1.0, 100.0),
    "s1_lane_special_trend_margin": ("float", 1.0, 100.0),
    "s1_lane_weak_adx": ("float", 0.0, 100.0),
    "s1_lane_weak_rel_volume": ("float", 0.0, 10.0),
    "s1_lane_min_entry_rr": ("float", 0.1, 5.0),
    "s1_lane_reentry_enabled": ("bool",),
    "s1_lane_reentry_bars_1min": ("int", 1, 20),
    "s1_lane_reentry_bars_5min": ("int", 1, 20),
    "s1_lane_reentry_bars_15min": ("int", 1, 20),
}


def dashboard_values(settings=None) -> dict:
    s = _settings(settings)
    return {name: getattr(s, name, None) for name in S1_SETTING_SPEC} if s is not None else {}
