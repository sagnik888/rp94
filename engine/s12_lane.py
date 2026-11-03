"""S1+S2 DUAL lanes (D1, D2): how a signal from the S1+S2 Dual engine (engine/s12_engine.py)
is admitted and how its trade or row is managed.

D1 is the package's S1 and D2 its S2; the host's own "S1" is a different lane and never
shares a tag, owner or setting with these. The engine selects its own entries, so a D1/D2
signal skips the grading/selection stack as UTS and S1 do. Every safety control stays
(daily-loss and segment breakers, position/bucket/capital caps, quote and data checks,
Auto/Manual authority, broker stop protection, the dashboard TF toggle). Each index may hold
one D1 and one D2 trade at the same time; loss limits and suspensions are shared.

Admission (entry_verdict) is native, with a data/sanity check only: the lane and its
timeframe switched on, a closed-bar signal, an index stop on the losing side no wider than
s12_sl_max_pct, no engine exit already printed, and for D1 a next bar the engine will fill.

Exits for a D1/D2 trade or row, one pass per monitor tick (no other engine's guards, locks
or floors run):
    1. the premium catastrophe stop: the UTS tiers (uts_lane.premium_cap_tiers, MIDCPNIFTY
       has its own): 1m the tight tier, 5m the regular one, 15m the widest, never above
       min(s12_options_operator_cap_pct, options_sl_pct), frozen at admission;
    2. the index stop: spot crossing the trade's engine stop. D1 starts from the actual
       entry (entry x (1 -/+ stop_pct)), D2 from the package's stop; weakness-confirmed
       early-loss and R-based runner STOP_UPDATED values ratchet it from the next bar;
    3. the engine's EXIT for this trade id, once its bar has closed;
    4. the s12_square_off (15:15) hard exit.
T1/T2/T3 are milestones recorded on the row; D2 keeps running past T3. D1's legacy
exit-at-T3 remains an explicit dashboard override and is off by default.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

from engine import s12_engine as engine

ENGINE_KEY = engine.ENGINE_KEY
LANE_TAGS = engine.LANES
D1, D2 = LANE_TAGS
EXIT_POLICIES = engine.EXIT_POLICIES
POLICY_VERSION = "s12_native.v1"

TIMEFRAME_CHOICES = ("5min,15min", "5min", "15min", "1min,5min,15min", "1min,5min", "1min,15min", "1min")
DEFAULT_TIMEFRAMES = "1min,5min,15min"

LANE_SETTING_DEFAULTS = {
    "s12_timeframes": DEFAULT_TIMEFRAMES,
    "s12_d1_enabled": True,
    "s12_d2_enabled": True,
    "s12_signal_only": False,
    "s12_square_off": "15:15",
    "s12_sl_max_pct": 1.5,
    "s12_min_entry_rr": 0.5,
    "s12_target_r": 4.0,
    "s12_options_operator_cap_pct": 10.0,
}
SETTING_DEFAULTS = {**engine.SETTING_DEFAULTS, **LANE_SETTING_DEFAULTS}


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


def _flag(s, name: str, default: bool) -> bool:
    value = getattr(s, name, default) if s is not None else default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default if value is None else value)


def _num(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _tf_minutes(timeframe: str) -> int:
    text = str(timeframe or "").lower().replace("min", "").replace("m", "").strip()
    try:
        return max(1, int(float(text)))
    except ValueError:
        return 5


def _hhmm(text: str, default: int) -> int:
    try:
        hh, mm = str(text).split(":")[:2]
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return default
        return hour * 60 + minute
    except (ValueError, AttributeError):
        return default


def _market_time(value):
    import pandas as pd

    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("missing timestamp")
    return stamp.tz_convert("Asia/Kolkata").tz_localize(None) if stamp.tzinfo else stamp


# ─────────────────────────────────────────────────────────────── identity and switches


def lane_of(obj: Any) -> str:
    """"D1", "D2" or "" for a signal, candidate, session row or trade.

    Identity only, independent of the dashboard switches: an open D1/D2 position must never
    be handed to another lane's policy. A trade restored from SQLite keeps source_lane.
    """
    if obj is None:
        return ""
    for name in ("s12_lane", "source_lane", "exit_policy_owner"):
        value = str(getattr(obj, name, "") or "").strip().upper()
        if value in LANE_TAGS:
            return value
    if str(getattr(obj, "source_engine", "") or "").strip().lower() == ENGINE_KEY:
        trade_id = str(getattr(obj, "s12_trade_id", "") or "")
        return {"S1": D1, "S2": D2}.get(trade_id.split("|", 1)[0], "")
    return ""


def is_lane(obj: Any, settings=None) -> bool:
    return bool(lane_of(obj))


is_s12 = is_lane


def lane_enabled(lane: str, settings=None) -> bool:
    """The admission switch for one lane (s12_d1_enabled / s12_d2_enabled)."""
    tag = str(lane or "").upper()
    if tag not in LANE_TAGS:
        return False
    return _flag(_settings(settings), f"s12_{tag.lower()}_enabled", True)


def admission_enabled(obj: Any, settings=None) -> bool:
    tag = lane_of(obj)
    return bool(tag) and lane_enabled(tag, settings)


def enabled_timeframes(settings=None) -> tuple:
    s = _settings(settings)
    value = str(getattr(s, "s12_timeframes", None) or DEFAULT_TIMEFRAMES) if s is not None else DEFAULT_TIMEFRAMES
    if value not in TIMEFRAME_CHOICES:
        value = DEFAULT_TIMEFRAMES
    return tuple(value.split(","))


def timeframe_enabled(timeframe: str, settings=None) -> bool:
    return engine.host_timeframe(timeframe) in enabled_timeframes(settings)


def signal_only(settings=None) -> bool:
    """True when D1/D2 signals must not become Auto orders (s12_signal_only, default off)."""
    return _flag(_settings(settings), "s12_signal_only", False)


def square_off(settings=None) -> str:
    s = _settings(settings)
    value = str(getattr(s, "s12_square_off", "15:15") or "15:15") if s is not None else "15:15"
    return ":".join(value.split(":")[:2]) if _hhmm(value, -1) >= 0 else "15:15"


def sl_max_pct(settings=None) -> float:
    return _g(_settings(settings), "s12_sl_max_pct", 1.5)


def min_entry_rr(settings=None) -> float:
    return _g(_settings(settings), "s12_min_entry_rr", 0.5)


def target_rr(settings=None) -> float:
    """Nominal premium target in R; T1-T3 are milestones and the target never closes."""
    return _g(_settings(settings), "s12_target_r", 4.0)


def entry_cutoff(timeframe: str, settings=None) -> str:
    """The system-wide last entry (engine/entry_window.py); no lane-specific cutoff."""
    from engine import entry_window

    return entry_window.last_entry(timeframe, settings)


def intrabar_enabled(settings=None) -> bool:
    """The engine only reads closed bars, so a D1/D2 signal never fires intra-candle."""
    return False


def intrabar_wait_seconds(obj: Any = None, settings=None) -> float:
    """No stabilisation wait: the signal bar has already closed (D1's entry is that bar's
    close, the next open in practice)."""
    return 0.0


# ─────────────────────────────────────────────────────────────── premium guard


def premium_cap_tiers(obj=None):
    """Tight, regular and maximum premium risk: the UTS tiers (MIDCPNIFTY 3.5/7/10)."""
    from engine import uts_lane

    return uts_lane.premium_cap_tiers(obj)


def premium_cap_pct(obj=None, settings=None, *, operator_cap=None, regime="UNKNOWN", timeframe="") -> float:
    """The premium catastrophe stop %: 1m tight, 5m regular, 15m maximum, never above the
    S1+S2 cap or the global options cap; admission's percentage stays frozen for the
    trade's life. Regime does not move it (UTS call parity)."""
    s = _settings(settings)
    configured_cap = _g(s, "options_sl_pct", 15.0)
    if not 0 < configured_cap < float("inf"):
        configured_cap = 15.0
    cap = configured_cap if operator_cap is None else _num(operator_cap, configured_cap)
    if not 0 < cap < float("inf"):
        cap = configured_cap
    lane_cap = _g(s, "s12_options_operator_cap_pct", 10.0)
    if not 0 < lane_cap < float("inf"):
        lane_cap = 10.0
    lane_cap = min(lane_cap, 10.0)
    cap = min(cap, lane_cap)
    tight, normal, wide = premium_cap_tiers(obj)
    _restore_trade_state(obj)
    frozen_cap = _num(getattr(obj, "s12_premium_cap_pct", 0.0))
    if 0 < frozen_cap <= wide:
        return min(cap, frozen_cap)
    recorded = getattr(obj, "quality_verdict", {}) or {}
    if isinstance(recorded, dict) and recorded.get("policy_version") == POLICY_VERSION:
        approved = _num((recorded.get("checks") or {}).get("s12_premium_cap_pct"), 0.0)
        if 0 < approved <= wide:
            return min(cap, approved)
    minutes = _tf_minutes(timeframe or getattr(obj, "timeframe", ""))
    selected = tight if minutes <= 1 else normal if minutes <= 5 else wide
    return min(cap, selected, wide)


def premium_guard_pct(premium: float, index_stop_distance: float, delta: float, settings=None,
                      *, regime: str = "UNKNOWN", confidence=None, grade: str = "",
                      fast_entry: bool = False, setup=None, timeframe="") -> float:
    return premium_cap_pct(setup, settings, regime=regime, timeframe=timeframe)


def premium_stop_distance(premium: float, index_stop_distance: float, delta: float, settings=None,
                          *, regime: str = "UNKNOWN", confidence=None, grade: str = "",
                          fast_entry: bool = False, setup=None, timeframe="") -> float:
    """The broker premium stop distance: the catastrophe cap (the index stop is the lane's)."""
    premium = float(premium or 0.0)
    if premium <= 0:
        return 0.0
    return premium * premium_guard_pct(premium, index_stop_distance, delta, settings,
                                       setup=setup, timeframe=timeframe) / 100.0


# ─────────────────────────────────────────────────────────────── admission


def _direction(obj: Any) -> int:
    """+1 long / -1 short from signal_type (BUY/SELL) or direction (LONG/SHORT); 0 unknown."""
    for name in ("direction", "signal_type"):
        value = str(getattr(obj, name, "") or "").strip().upper()
        if value in {"LONG", "BUY"}:
            return 1
        if value in {"SHORT", "SELL"}:
            return -1
    return 0


def _ladder(obj: Any):
    ladder = getattr(obj, "s12_r_ladder", None)
    try:
        values = [float(x) for x in ladder]
    except (TypeError, ValueError):
        return None
    return values if len(values) == 3 and all(math.isfinite(x) and x > 0 for x in values) else None


def admission_levels(obj: Any, entry_spot: float, settings=None) -> dict:
    """The index stop and T1-T3 for a D1/D2 entry at ``entry_spot``.

    D1 is re-measured from the actual entry exactly as the package fills a SETUP (stop %
    clamped by the hard cap, tick-rounded toward entry, R ladder); D2 keeps the package's
    absolute levels. Returns the s12_* fields to set on the trade or row.
    """
    tag = lane_of(obj)
    d = _direction(obj)
    spot = _num(entry_spot)
    pct = _num(getattr(obj, "s12_stop_pct", None))
    ladder = _ladder(obj)
    if tag == D1 and spot > 0 and pct > 0 and ladder and d:
        stop, t1, t2, t3 = engine.d1_levels(spot, d == 1, pct, ladder)
    else:
        stop = _num(getattr(obj, "s12_stop", None))
        t1, t2, t3 = (_num(getattr(obj, f"s12_t{k}", None)) for k in (1, 2, 3))
        if spot > 0 and stop > 0:
            pct = abs(spot - stop) / spot * 100.0
    return dict(s12_stop=stop, s12_stop_initial=stop, s12_stop_pct=pct, s12_t1=t1, s12_t2=t2, s12_t3=t3,
                s12_entry_spot=spot)


def _exit_known(obj: Any, now) -> bool:
    """The engine's exit for this signal had closed by ``now`` (never guessed without now)."""
    if now is None or not getattr(obj, "s12_exit_reason", ""):
        return False
    try:
        import pandas as pd

        closed = _market_time(getattr(obj, "s12_exit_time", None)) + pd.Timedelta(
            minutes=_tf_minutes(getattr(obj, "timeframe", "")))
        return closed <= _market_time(now)
    except Exception:
        return False


def entry_verdict(obj, settings=None, *, regime="UNKNOWN", lane="LIVE", now=None, **_ignored):
    """Admit an engine signal natively after a data/sanity check (no grade filtering)."""
    from engine.quality_verdict import QualityVerdict

    s = _settings(settings)
    tag = lane_of(obj)
    d = _direction(obj)
    price = _num(getattr(obj, "price", None))
    stop = _num(getattr(obj, "s12_stop", None))
    stop_pct = abs(price - stop) / price * 100.0 if price > 0 and stop > 0 else float("nan")
    ceiling = sl_max_pct(s)
    is_exit = str(getattr(obj, "action", "ENTRY")).upper() == "EXIT"
    cancel = str(getattr(obj, "s12_fill_cancel", "") or "")
    if not tag:
        code, reason = "S12_UNKNOWN_LANE", "not a D1/D2 signal"
    elif not lane_enabled(tag, s):
        code, reason = "S12_LANE_DISABLED", f"{tag} admission is switched off (s12_{tag.lower()}_enabled)"
    elif not timeframe_enabled(getattr(obj, "timeframe", ""), s):
        code, reason = "S12_TIMEFRAME_DISABLED", f"{getattr(obj, 'timeframe', '')} is not in s12_timeframes"
    elif getattr(obj, "s12_forming", False):
        code, reason = "S12_FORMING_BAR", "the engine trades closed bars only"
    elif not d or price <= 0 or stop <= 0 or (stop - price) * d >= 0:
        code, reason = "S12_BAD_LEVELS", f"price {price:.2f} / stop {stop:.2f} are not a valid {tag} entry"
    elif stop_pct > ceiling + 1e-9:
        code, reason = "S12_STOP_TOO_WIDE", f"index stop {stop_pct:.2f}% > s12_sl_max_pct {ceiling:.2f}%"
    elif tag == D1 and cancel:
        code, reason = "S12_D1_FILL_CANCELLED", f"the engine cancels this SETUP at the next bar ({cancel})"
    elif _exit_known(obj, now):
        code, reason = "S12_ENGINE_EXITED", f"the engine already exited this trade ({obj.s12_exit_reason})"
    else:
        code, reason = "S12_NATIVE_ENTRY", f"S1+S2 Dual {tag} signal admitted natively without grade filtering"
    allowed = is_exit or code == "S12_NATIVE_ENTRY"
    tight = premium_cap_tiers(obj)[0]
    checks = {
        "lane": lane, "s12_lane": tag, "s12_trade_id": getattr(obj, "s12_trade_id", ""),
        "s12_event_id": getattr(obj, "s12_event_id", ""), "s12_score": _num(getattr(obj, "s12_score", None)),
        "s12_setup": getattr(obj, "s12_setup", ""), "s12_stop": stop, "s12_stop_pct": stop_pct,
        "s12_stop_initial": _num(getattr(obj, "s12_stop_initial", stop)),
        "s12_entry_spot": _num(getattr(obj, "s12_entry_spot", 0.0)),
        "s12_r_ladder": list(getattr(obj, "s12_r_ladder", []) or []),
        "s12_t1": _num(getattr(obj, "s12_t1", 0.0)),
        "s12_t2": _num(getattr(obj, "s12_t2", 0.0)),
        "s12_t3": _num(getattr(obj, "s12_t3", 0.0)),
        "s12_t1_hit": bool(getattr(obj, "s12_t1_hit", False)),
        "s12_t2_hit": bool(getattr(obj, "s12_t2_hit", False)),
        "s12_t3_hit": bool(getattr(obj, "s12_t3_hit", False)),
        "s12_premium_cap_pct": premium_cap_pct(obj, s),
        "s12_sl_max_pct": ceiling, "s12_fill_cancel": cancel, "grade_filter_enabled": False,
        "s12_premium_cap_profile": "midcap.v1" if tight == 3.5 else "standard.v1",
    }
    if getattr(obj, "s12_exit_reason", ""):
        checks["s12_exit_reason"] = str(getattr(obj, "s12_exit_reason", "") or "")
    if getattr(obj, "s12_exit_time", None) is not None:
        checks["s12_exit_time"] = str(getattr(obj, "s12_exit_time"))
    return QualityVerdict(allowed, "s12_native", code, reason, checks=checks, policy_version=POLICY_VERSION)


# ─────────────────────────────────────────────────────────────── engine view of a trade


def _signal_trade_id(trade: Any, result: dict) -> str:
    """The engine trade id of the signal ``trade`` came from (lane, bar and side match)."""
    tag = lane_of(trade)
    ts = getattr(trade, "signal_time", None) or getattr(trade, "signal_timestamp", None)
    if not tag or ts is None:
        return ""
    instrument = str(getattr(trade, "instrument", "") or "").split()
    if instrument and engine.index_key(result.get("instrument")) != engine.index_key(instrument[0]):
        return ""
    if str(result.get("timeframe") or "") != str(getattr(trade, "timeframe", "") or ""):
        return ""
    try:
        want = _market_time(ts)
    except Exception:
        return ""
    side = "BUY" if _direction(trade) == 1 else "SELL"
    for sig in result.get("signals") or []:
        if str(getattr(sig, "source_engine", "") or "").lower() != ENGINE_KEY or lane_of(sig) != tag:
            continue
        try:
            if _market_time(getattr(sig, "timestamp", None)) == want and getattr(sig, "signal_type", "") == side:
                return str(getattr(sig, "s12_trade_id", "") or "")
        except Exception:
            continue
    return ""


def _restore_trade_state(trade: Any) -> None:
    """Rehydrate the lane's JSON-backed bookkeeping after Trade.from_dict()."""
    if trade is None:
        return
    recorded = getattr(trade, "quality_verdict", {}) or {}
    checks = recorded.get("checks") if isinstance(recorded, dict) else None
    if not isinstance(checks, dict):
        return
    mapping = {
        "s12_lane": "s12_lane", "s12_trade_id": "s12_trade_id", "s12_event_id": "s12_event_id",
        "s12_score": "s12_score", "s12_setup": "s12_setup", "s12_entry_spot": "s12_entry_spot",
        "s12_stop": "s12_stop", "s12_stop_initial": "s12_stop_initial", "s12_stop_pct": "s12_stop_pct",
        "s12_r_ladder": "s12_r_ladder", "s12_t1": "s12_t1", "s12_t2": "s12_t2", "s12_t3": "s12_t3",
        "s12_t1_hit": "s12_t1_hit", "s12_t2_hit": "s12_t2_hit", "s12_t3_hit": "s12_t3_hit",
        "s12_premium_cap_pct": "s12_premium_cap_pct", "s12_exit_reason": "s12_exit_reason",
        "s12_exit_time": "s12_exit_time", "s12_exit_price": "s12_exit_price",
    }
    for key, attr in mapping.items():
        if key not in checks or hasattr(trade, attr):
            continue
        value = checks[key]
        if attr == "s12_exit_time" and value:
            try:
                import pandas as pd
                value = pd.Timestamp(value).to_pydatetime()
            except Exception:
                continue
        setattr(trade, attr, value)


def trade_record(trade: Any, result: Optional[dict]) -> Optional[dict]:
    """The engine's record for this trade (result["s12_trades"]), or None."""
    from engine.scanner_modes import native_result

    result = native_result(trade, result)
    if not isinstance(result, dict) or result.get("error"):
        return None
    if str(result.get("source_engine") or "").lower() != ENGINE_KEY:
        return None
    trades = result.get("s12_trades") or {}
    trade_id = str(getattr(trade, "s12_trade_id", "") or "") or _signal_trade_id(trade, result)
    record = trades.get(trade_id) if trade_id else None
    if not isinstance(record, dict) or record.get("lane") != lane_of(trade):
        return None
    return record


def closed_bar_state(trade: Any, result: Optional[dict], now=None) -> Optional[dict]:
    """The engine view exit_decision() reads as ``market`` (s1_lane naming)."""
    return trade_record(trade, result)


def engine_exit_for(trade: Any, result: Optional[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """The engine's EXIT for this trade id once its bar has closed (or a cancelled SETUP)."""
    record = trade_record(trade, result)
    exit_info = (record or {}).get("exit")
    if not exit_info:
        return None
    try:
        import pandas as pd

        when = _market_time(exit_info.get("time"))
        ts = getattr(trade, "signal_time", None) or getattr(trade, "signal_timestamp", None)
        if ts is not None and when <= _market_time(ts):
            return None
        if now is not None and when + pd.Timedelta(minutes=_tf_minutes(getattr(trade, "timeframe", ""))) > _market_time(now):
            return None
    except Exception:
        return None
    return {"reason": exit_info.get("reason", ""), "time": exit_info.get("time"),
            "price": exit_info.get("price"), "trade_id": record.get("trade_id")}


def current_stop(trade: Any) -> float:
    """The trade's index stop now: s12_stop, else the D1 stop from the entry spot, else the
    initial/persisted index stop."""
    _restore_trade_state(trade)
    stop = _num(getattr(trade, "s12_stop", None))
    if stop > 0:
        return stop
    continuity = getattr(trade, "monitoring_continuity", None)
    if isinstance(continuity, dict):
        stop = _num(continuity.get("observed_underlying_stop"))
        if stop > 0:
            return stop
    if lane_of(trade) == D1:
        spot = _num(getattr(trade, "s12_entry_spot", None) or getattr(trade, "underlying_entry", None)
                    or getattr(trade, "entry_spot", None))
        pct = _num(getattr(trade, "s12_stop_pct", None))
        ladder = _ladder(trade)
        d = _direction(trade)
        if spot > 0 and pct > 0 and ladder and d:
            return engine.d1_levels(spot, d == 1, pct, ladder)[0]
    return _num(getattr(trade, "s12_stop_initial", None)) or _num(getattr(trade, "underlying_stop", None))


def apply_engine_view(trade: Any, view: Optional[dict], now=None) -> bool:
    """Ratchet s12_stop by the engine's STOP_UPDATED values in force at ``now`` and record
    the T1-T3 milestones. Mutates only s12_* bookkeeping; True when anything changed."""
    _restore_trade_state(trade)
    if not isinstance(view, dict) or view.get("lane") != lane_of(trade):
        return False
    trade_id = str(getattr(trade, "s12_trade_id", "") or "")
    if trade_id and view.get("trade_id") != trade_id:
        return False
    try:
        clock = _market_time(now) if now is not None else None
    except Exception:
        clock = None
    d = _direction(trade) or (1 if view.get("side") == "LONG" else -1)
    stop = current_stop(trade)
    changed = False
    for update in view.get("stop_updates") or []:
        try:
            if clock is not None and _market_time(update.get("effective_from")) > clock:
                continue
        except Exception:
            continue
        level = _num(update.get("stop"))
        if level > 0 and (stop <= 0 or (level - stop) * d > 0):
            stop = level
            changed = True
    if changed or (stop > 0 and _num(getattr(trade, "s12_stop", None)) <= 0):
        setattr(trade, "s12_stop", stop)
        if stop > 0:
            setattr(trade, "underlying_stop", stop)
            setattr(trade, "spot_stop", stop)
        changed = True
    if not getattr(trade, "s12_trade_id", None) and view.get("trade_id"):
        setattr(trade, "s12_trade_id", view["trade_id"])
        changed = True
    import pandas as pd

    bar = pd.Timedelta(minutes=_tf_minutes(getattr(trade, "timeframe", "")))
    for name, milestone in (view.get("milestones") or {}).items():
        attr = f"s12_{str(name).lower()}_hit"
        if attr not in {"s12_t1_hit", "s12_t2_hit", "s12_t3_hit"} or getattr(trade, attr, False):
            continue
        try:
            if clock is not None and _market_time(milestone.get("time")) + bar > clock:
                continue
        except Exception:
            continue
        setattr(trade, attr, True)
        changed = True
    if changed:
        recorded = dict(getattr(trade, "quality_verdict", {}) or {})
        checks = dict(recorded.get("checks") or {})
        for key in SESSION_STATE_FIELDS:
            if hasattr(trade, key):
                checks[key] = getattr(trade, key)
        recorded["checks"] = checks
        trade.quality_verdict = recorded
    return changed


# ─────────────────────────────────────────────────────────────── exits


# The engine's exit label -> the trade's exit reason.
EXIT_REASONS = {
    "STOP GAP": "S12_STOP_GAP",
    "INITIAL STOP": "S12_ENGINE_STOP",
    "TRAIL/LOCK STOP": "S12_ENGINE_TRAIL_STOP",
    "EARLY LOSS CUT": "S12_EARLY_LOSS_CUT",
    "EARLY LOSS STOP": "S12_EARLY_LOSS_STOP",
    "OPEN GAP THROUGH T3": "S12_T3",
    "T3 AUTO EXIT": "S12_T3",
    "T3 TARGET": "S12_T3",
    "INTRADAY EOD (CLOSE EST)": "S12_EOD",
    "INTRADAY EOD": "S12_EOD",
    "MOMENTUM EXIT": "S12_MOMENTUM_EXIT",
    "SMART EXIT": "S12_SMART_EXIT",
    "WEAKNESS EXIT": "S12_WEAKNESS_EXIT",
    "MAX BARS FAILSAFE": "S12_MAX_BARS",
    "ORPHAN INTRADAY NEXT OPEN": "S12_ORPHAN",
    "NEW DAY GAP CLOSE": "S12_ORPHAN",
}
LANE_EXIT_REASONS = ("S12_PREMIUM_CAP", "S12_STOP", "S12_TRAIL_STOP", "S12_SQUARE_OFF")


def exit_reason_for(engine_reason: str) -> str:
    text = str(engine_reason or "")
    if text.startswith("SETUP_CANCELLED"):
        return "S12_SETUP_CANCELLED"
    return EXIT_REASONS.get(text, "S12_EXIT")


def _entry_premium(trade: Any) -> float:
    for name in ("entry_price", "price"):
        value = _num(getattr(trade, name, None))
        if value > 0:
            return value
    return 0.0


def _past_square_off(trade: Any, now, settings=None) -> bool:
    clock = _market_time(now)
    if clock.hour * 60 + clock.minute >= _hhmm(square_off(settings), 15 * 60 + 15):
        return True
    ts = getattr(trade, "signal_time", None) or getattr(trade, "signal_timestamp", None)
    try:
        return ts is not None and _market_time(ts).date() < clock.date()
    except Exception:
        return False


def exit_decision(trade: Any, spot: float, premium: float, now: Optional[datetime] = None,
                  engine_exit: Optional[dict] = None, settings=None, observed_contract_ticks=None,
                  market: Optional[dict] = None, result: Optional[dict] = None, **_ignored):
    """One monitor pass for a D1/D2 trade or row; mutates only s12_* bookkeeping.

    ``market`` is closed_bar_state() (the engine's record for this trade); ``result`` may be
    passed instead, and then also supplies ``engine_exit``. Order: premium catastrophe stop,
    index stop cross, engine EXIT, square-off.
    """
    from engine.s1_lane import S1ExitDecision

    s = _settings(settings)
    tag = lane_of(trade)
    view = market if isinstance(market, dict) and market.get("lane") == tag else None
    if view is None and result is not None:
        view = trade_record(trade, result)
    if view is not None:
        apply_engine_view(trade, view, now)
    if engine_exit is None and result is not None:
        engine_exit = engine_exit_for(trade, result, now)
    stop = current_stop(trade)
    if stop > 0 and _num(getattr(trade, "s12_stop", None)) <= 0:
        setattr(trade, "s12_stop", stop)
    d = _direction(trade)
    spot = _num(spot)
    premium = _num(premium)
    is_opt = str(getattr(trade, "inst_type", "OPT") or "OPT").upper() != "FUT"
    price_direction = 1 if is_opt else (d or 1)

    # 1. Premium catastrophe stop (the UTS tiers), including ticks seen since the last pass.
    entry = _entry_premium(trade)
    cap = premium_cap_pct(trade, s)
    floor = entry * (1.0 - price_direction * cap / 100.0) if entry > 0 and cap > 0 else 0.0
    broker_stop = floor if is_opt else 0.0
    # The premium catastrophe tier protects long option contracts. Futures are
    # protected by the native index stop below; applying an option cap to a future
    # would silently replace the engine's wider structural stop.
    if is_opt and floor > 0:
        last_stamp = None
        filled_at = getattr(trade, "entry_filled_at", None) or getattr(trade, "entry_time", None)
        for tick in observed_contract_ticks or ():
            try:
                observed = float(tick[0])
                stamp = _market_time(tick[1])
                if (not math.isfinite(observed) or observed <= 0
                        or (now is not None and stamp > _market_time(now))
                        or (last_stamp is not None and stamp < last_stamp)
                        or (filled_at is not None and stamp < _market_time(filled_at))):
                    continue
                last_stamp = stamp
            except (TypeError, ValueError, IndexError):
                continue
            if (observed - floor) * price_direction <= 0:
                return S1ExitDecision(True, "S12_PREMIUM_CAP", stop,
                                      f"{tag} premium {observed:.2f} touched the {cap:.1f}% cap {floor:.2f}",
                                      premium_stop=broker_stop, runner=True)
        if premium > 0 and (premium - floor) * price_direction <= 0:
            return S1ExitDecision(True, "S12_PREMIUM_CAP", stop,
                                  f"{tag} premium {premium:.2f} at the {cap:.1f}% cap {floor:.2f}",
                                  premium_stop=broker_stop, runner=True)

    # 2. The engine's index stop (D1 from the actual entry; ratcheted by STOP_UPDATED).
    if spot > 0 and stop > 0 and d and (spot - stop) * d <= 0:
        initial = _num(getattr(trade, "s12_stop_initial", None))
        reason = "S12_TRAIL_STOP" if initial > 0 and abs(stop - initial) > 1e-9 else "S12_STOP"
        return S1ExitDecision(True, reason, stop, f"{tag} index {spot:.2f} crossed {stop:.2f}",
                              premium_stop=broker_stop, runner=True)

    # 3. The engine's EXIT for this trade id.
    if engine_exit:
        chart = str(engine_exit.get("reason") or "")
        return S1ExitDecision(True, exit_reason_for(chart), stop, f"S1+S2 Dual {tag} exit: {chart or 'exit'}",
                              premium_stop=broker_stop, runner=True)

    # 4. Hard square-off.
    if now is not None and _past_square_off(trade, now, s):
        return S1ExitDecision(True, "S12_SQUARE_OFF", stop, f"{tag} square-off at {square_off(s)}",
                              premium_stop=broker_stop, runner=True)
    return S1ExitDecision(False, "", stop, f"{tag} stop {stop:.2f}; premium cap {cap:.1f}%",
                          premium_stop=broker_stop, runner=True)


# Row/trade bookkeeping a restart or EXIT copy must keep (s1_lane.SESSION_STATE_FIELDS style).
SESSION_STATE_FIELDS = (
    "s12_lane", "s12_trade_id", "s12_event_id", "s12_score", "s12_setup", "s12_entry_spot",
    "s12_stop", "s12_stop_initial", "s12_stop_pct", "s12_r_ladder", "s12_t1", "s12_t2", "s12_t3",
    "s12_t1_hit", "s12_t2_hit", "s12_t3_hit", "s12_premium_cap_pct",
    "s12_exit_reason", "s12_exit_time", "s12_exit_price",
)
SESSION_BOOL_FIELDS = frozenset({"s12_t1_hit", "s12_t2_hit", "s12_t3_hit"})
SESSION_TIME_FIELDS = frozenset({"s12_exit_time"})
SESSION_TEXT_FIELDS = frozenset({"s12_lane", "s12_trade_id", "s12_event_id", "s12_setup", "s12_exit_reason"})
SESSION_LIST_FIELDS = frozenset({"s12_r_ladder"})


# Dashboard-editable S1+S2 Dual settings (same shape as s1_lane.S1_SETTING_SPEC): the
# engine inputs with the package's Pine bounds, then the lane's own settings.
LANE_SETTING_SPEC = {
    "s12_timeframes": ("enum", TIMEFRAME_CHOICES),
    "s12_d1_enabled": ("bool",),
    "s12_d2_enabled": ("bool",),
    "s12_signal_only": ("bool",),
    "s12_square_off": ("time",),
    "s12_sl_max_pct": ("float", 0.05, 5.0),
    "s12_min_entry_rr": ("float", 0.1, 5.0),
    "s12_target_r": ("float", 0.5, 50.0),
    "s12_options_operator_cap_pct": ("float", 0.5, 10.0),
}
S12_SETTING_SPEC = {
    **{name: spec for name, (_, spec) in engine.SETTING_FIELDS.items()},
    **LANE_SETTING_SPEC,
}


def dashboard_values(settings=None) -> dict:
    s = _settings(settings)
    return {name: getattr(s, name, SETTING_DEFAULTS[name]) for name in S12_SETTING_SPEC} if s is not None else {}


def dashboard_schema(settings=None) -> dict:
    """JSON-safe field schema for the dashboard's generated S12 settings card."""
    s = _settings(settings)
    schema = {}
    for name, spec in S12_SETTING_SPEC.items():
        kind = spec[0]
        item = {"kind": kind, "default": getattr(s, name, SETTING_DEFAULTS[name]) if s is not None else SETTING_DEFAULTS[name]}
        if kind == "enum":
            item["choices"] = list(spec[1])
        elif kind in {"int", "float"}:
            item["min"], item["max"] = spec[1], spec[2]
            item["step"] = 1 if kind == "int" else max(0.01, (float(spec[2]) - float(spec[1])) / 100.0)
        schema[name] = item
    return schema
