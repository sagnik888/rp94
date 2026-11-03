"""UT SMART EXIT lane: how a signal from the UT SMART EXIT engine is admitted and managed.

The engine (engine/ut_smart_engine.py) selects UT flips on closed candles by default.
UTS uses its native flip and a joint extremely-weak-setup check, without grade vetoes. It retains its
own chart exits and every safety control (daily-loss and segment breakers,
position/bucket/capital caps, quote and data checks, Auto/Manual authority, broker stop
protection, the dashboard TF toggle and the 1m burst rule). s1_lane.is_lane() is True for both
lanes; the values that differ live here.

Exits for a UTS trade or row (no other engine's guards, locks or floors run):
    1. the engine's own exit for this signal once its bar has closed: the opposite UT
       signal (a close through the UT trailing stop), the Smart Peak Exit, or the
       emergency peak retracement;
    2. the premium capital stop: 5% for bursts, cascade/alignment and strong trends,
       10% normally, or 15% for choppy/volatile 15m trades. MIDCPNIFTY uses
       3.5%, 7% and 10% respectively;
    3. the option premium profit lock, armed at 1R with 30% peak-gain giveback;
    4. the 15:15 squareoff.
The premium capital stop can exit before the chart stop. A strong, fresh 5m setup may
earn one recorded extension from 10% to 15% before its original stop is touched.
For MIDCPNIFTY, that controlled extension is from 7% to 10%.
The chart has no target, so the target is nominal (uts_lane_target_r) and never closes
the trade.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from engine import ut_smart_engine as engine

ENGINE_KEY = engine.ENGINE_KEY
LANE_TAG = engine.LANE_TAG


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
    return bool(getattr(s, "uts_lane_enabled", True)) if s is not None else True


def is_uts(obj: Any, settings=None) -> bool:
    """True for a UTS signal, candidate, session row or trade while the lane is on.

    A trade restored from SQLite has lost source_engine, so the persisted source_lane
    "UTS" is accepted too.
    """
    if obj is None or not lane_enabled(settings):
        return False
    if str(getattr(obj, "source_engine", "") or "").strip().lower() == ENGINE_KEY:
        return True
    if str(getattr(obj, "source_lane", "") or "").strip().upper() == LANE_TAG:
        return True
    sources: Iterable = getattr(obj, "source_engines", None) or ()
    try:
        return [str(x).strip().lower() for x in sources] == [ENGINE_KEY]
    except TypeError:
        return False


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


def entry_cutoff(timeframe: str, settings=None) -> str:
    """Last UTS entry time ("HH:MM"): the system-wide last entry (2026-09-26 user: 15m
    14:50, 5m 15:10, 1m 15:14; engine/entry_window.py). No lane-specific cutoff."""
    from engine import entry_window

    return entry_window.last_entry(timeframe, settings)


def reclaim_start(settings=None) -> str:
    """"HH:MM" from which the late big-move (late_session_reclaim) check runs for UTS."""
    s = _settings(settings)
    value = str(getattr(s, "uts_lane_reclaim_start", "13:00") or "13:00") if s is not None else "13:00"
    return ":".join(value.split(":")[:2]) if _hhmm(value, -1) >= 0 else "13:00"


def sl_max_pct(settings=None) -> float:
    return _g(_settings(settings), "uts_lane_sl_max_pct", 1.0)


def min_entry_rr(settings=None) -> float:
    return _g(_settings(settings), "uts_lane_min_entry_rr", 0.5)


def target_rr(settings=None) -> float:
    return _g(_settings(settings), "uts_lane_target_r", 3.0)


def setup_values(obj, *, volume_ratio=None, volume_available=None, volume_basis=None):
    """Native measured inputs; grade and confidence do not participate."""
    import math

    stored = getattr(obj, "uts_setup", {}) or {}
    stored = stored if isinstance(stored, dict) else {}
    def number(name, default=0.0):
        try:
            value = float(getattr(obj, name, None) or stored.get(name, default))
            return value if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default
    direction = str(getattr(obj, "direction", "") or getattr(obj, "signal_type", "")).upper()
    raw_rsi = getattr(obj, "rsi_value", None)
    if raw_rsi is None:
        raw_rsi = stored.get("rsi_value")
    try:
        rsi = float(raw_rsi)
        rsi_measured = math.isfinite(rsi) and 0 <= rsi <= 100
    except (TypeError, ValueError):
        rsi_measured = False
    if not rsi_measured:
        rsi = 50.0
    raw_ratio = volume_ratio
    if raw_ratio is None:
        raw_ratio = getattr(obj, "volume_ratio", None)
        if raw_ratio is None:
            raw_ratio = stored.get("volume_ratio")
    try:
        ratio = float(raw_ratio)
        ratio_measured = math.isfinite(ratio) and ratio >= 0
    except (TypeError, ValueError):
        ratio, ratio_measured = 0.0, False
    basis = str(volume_basis if volume_basis is not None else
                getattr(obj, "volume_basis", None) or stored.get("volume_basis", "")).upper()
    available = (volume_available if volume_available is not None else
                 getattr(obj, "volume_available", stored.get("volume_available", False))) is True
    measured = available and ratio_measured and basis not in {"", "UNKNOWN", "UNAVAILABLE", "PROXY", "TICK_ACTIVITY",
        "INDEX_TICK_ACTIVITY", "TICK_ROLLING_AVG", "TICK_TIME_OF_DAY", "MIXED", "MIXED_UNITS"}
    return dict(adx=number("adx_value"), rsi=rsi, rsi_measured=rsi_measured, direction=direction,
        directional_rsi=rsi_measured and (rsi >= 55 if direction in {"LONG", "BUY"} else
                         rsi <= 45 if direction in {"SHORT", "SELL"} else False),
        volume_ratio=ratio if math.isfinite(ratio) else 0.0, measured_volume=measured)


def premium_cap_tiers(obj=None):
    """Special, regular and maximum UTS premium risk for the signal's index."""
    index = str(getattr(obj, 'instrument', '') or '').strip().upper().split()
    if index and index[0] in {'MIDCPNIFTY', 'MIDCAPNIFTY'}:
        return 3.5, 7.0, 10.0
    return 5.0, 10.0, 15.0


def premium_cap_pct(obj=None, settings=None, *, operator_cap=None, regime="UNKNOWN", timeframe=""):
    """UTS capital policy, including the tighter MIDCPNIFTY profile."""
    from engine.market_regime import normalize_regime, MarketRegime

    s = _settings(settings)
    cap = _g(s, "options_sl_pct", 15.0) if operator_cap is None else float(operator_cap)
    if not 0 < cap < float("inf"):
        cap = 15.0
    tf = timeframe or getattr(obj, "timeframe", "")
    tight, normal, wide = premium_cap_tiers(obj)
    evidence = setup_values(obj)
    canonical = normalize_regime(regime)
    aligned = ((evidence["direction"] in {"LONG", "BUY"} and canonical in
                {MarketRegime.TRENDING_UP, MarketRegime.BREAKOUT_UP}) or
               (evidence["direction"] in {"SHORT", "SELL"} and canonical in
                {MarketRegime.TRENDING_DOWN, MarketRegime.BREAKOUT_DOWN}))
    special_risk = any(bool(getattr(obj, key, False)) for key in (
        "one_way_cascade_entry", "uts_multi_index_alignment", "market_ltp_shock_entry",
        "market_wide_reversal_entry"))
    if special_risk or _tf_minutes(tf) == 1 or (aligned and evidence["adx"] >= 35 and evidence["directional_rsi"]):
        selected = tight
    elif _tf_minutes(tf) == 15 and canonical in {MarketRegime.RANGING, MarketRegime.VOLATILE}:
        selected = wide
    else:
        selected = normal
    # Freeze approved entry facts across the order queue and recovery. A completed
    # extension is recorded in the existing persisted verdict, not a volatile flag.
    recorded = getattr(obj, "quality_verdict", {}) or {}
    if isinstance(recorded, dict) and recorded.get("policy_version") == "uts_native.v1":
        checks = recorded.get("checks") or {}
        approved = checks.get("uts_premium_cap_pct")
        if tight == 3.5 and checks.get('uts_premium_cap_profile') != 'midcap.v1' and approved in (5.0, 10.0, 15.0):
            # Legacy MIDCPNIFTY verdicts used the common index profile. Translate
            # their approved tier rather than preserving a wider old percentage.
            selected = {5.0: tight, 10.0: normal, 15.0: wide}[approved]
        elif approved in (tight, normal, wide):
            selected = float(approved)
        if checks.get("uts_stop_extended"):
            selected = wide
    if special_risk:
        selected = min(selected, tight)
    return min(cap, selected, wide)


def _flag(s, name: str, default: bool) -> bool:
    value = getattr(s, name, default) if s is not None else default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(default if value is None else value)


def _pcr_signal(pcr) -> str:
    """The intel engine's own PCR read, or "" when PCR is unavailable."""
    if not isinstance(pcr, dict) or pcr.get("available") is False:
        return ""
    return str(pcr.get("contrarian_signal") or pcr.get("signal") or "").upper()


def five_minute_intel_failures(obj, evidence: dict, settings=None, *, pcr=None) -> list:
    """Why a plain 5m UTS flip lacks market-intelligence support ([] = supported).

    The legs are joint: volume above its average, a trending ADX, and RSI on the
    trade's side. PCR only vetoes a clearly opposing intel read.
    """
    s = _settings(settings)
    if _tf_minutes(getattr(obj, "timeframe", "")) != 5 or not _flag(s, "uts_lane_5m_intel_gate_enabled", True):
        return []
    failures = []
    min_volume = _g(s, "uts_lane_5m_min_volume_ratio", 1.0)
    if min_volume > 0:
        if not evidence["measured_volume"]:
            failures.append("volume unmeasured")
        elif evidence["volume_ratio"] < min_volume:
            failures.append(f"volume {evidence['volume_ratio']:.2f}x < {min_volume:.2f}x")
    min_adx = _g(s, "uts_lane_5m_min_adx", 20.0)
    if min_adx > 0 and evidence["adx"] < min_adx:
        failures.append(f"ADX {evidence['adx']:.1f} < {min_adx:.1f}")
    if _flag(s, "uts_lane_5m_require_directional_rsi", True) and not evidence["directional_rsi"]:
        failures.append(f"RSI {evidence['rsi']:.1f} not directional")
    if _flag(s, "uts_lane_5m_pcr_veto_enabled", True):
        read = _pcr_signal(pcr)
        long_side = evidence["direction"] in {"LONG", "BUY"}
        if read in ({"BEARISH", "STRONG_BEARISH"} if long_side else {"BULLISH", "STRONG_BULLISH"}):
            failures.append(f"PCR intel {read}")
    return failures


def entry_verdict(obj, settings=None, *, regime="UNKNOWN", lane="LIVE", pcr=None, **volume):
    """Reject jointly weak setups, and 5m flips without market-intelligence support."""
    from engine.quality_verdict import QualityVerdict

    evidence = setup_values(obj, **volume)
    weak_rsi = ((evidence["direction"] in {"LONG", "BUY"} and evidence["rsi"] <= 50) or
                (evidence["direction"] in {"SHORT", "SELL"} and evidence["rsi"] >= 50))
    extremely_weak = (evidence["measured_volume"] and evidence["volume_ratio"] < 0.5
                      and evidence["rsi_measured"]
                      and 0 < evidence["adx"] < 12 and weak_rsi)
    special = any(bool(getattr(obj, key, False)) for key in (
        "volume_spike_entry", "rare_burst_entry", "is_reversion_scalp", "market_ltp_shock_entry",
        "market_wide_reversal_entry", "late_session_reclaim_entry", "one_way_cascade_entry"))
    one_minute_plain = _tf_minutes(getattr(obj, "timeframe", "")) == 1 and not special
    intel_failures = [] if special else five_minute_intel_failures(obj, evidence, settings, pcr=pcr)
    is_exit = str(getattr(obj, "action", "ENTRY")).upper() == "EXIT"
    allowed = is_exit or not (extremely_weak or one_minute_plain or intel_failures)
    code = ("UTS_EXTREMELY_WEAK_SETUP" if extremely_weak else "UTS_1MIN_SPECIAL_REQUIRED" if one_minute_plain
            else "UTS_5MIN_INTEL_UNSUPPORTED" if intel_failures else "UTS_NATIVE_ENTRY")
    reason = ("UTS setup has measured volume <0.5x, ADX <12 and no directional RSI support"
              if extremely_weak else "UTS 1min requires a filtered burst, spike or reversal"
              if one_minute_plain else "UTS 5min intel gate: " + "; ".join(intel_failures)
              if intel_failures else "UTS native signal admitted without grade filtering")
    return QualityVerdict(allowed, "uts_native", code, reason,
        checks={**evidence, "lane": lane, "grade_filter_enabled": False,
                "uts_5m_intel_failures": intel_failures, "pcr_intel": _pcr_signal(pcr),
                "uts_premium_cap_pct": premium_cap_pct(obj, settings, regime=regime),
                "uts_premium_cap_profile": 'midcap.v1' if premium_cap_tiers(obj)[0] == 3.5 else 'standard.v1'},
        policy_version="uts_native.v1")


def extension_stop(trade, premium, *, result, context, now, settings=None):
    """Fresh strong 5m evidence may request one extension before any stop touch."""
    from engine.market_regime import normalize_regime, MarketRegime
    from engine.scanner_modes import native_result
    result = native_result(trade, result)

    from engine.s1_lane import managed_lane

    # An open trade: its frozen owner decides, not the admission switch.
    if managed_lane(trade, settings) != LANE_TAG or _tf_minutes(getattr(trade, "timeframe", "")) != 5:
        return 0.0
    if (not isinstance(result, dict) or result.get("error") or not isinstance(context, dict)
            or str(result.get("source_engine") or "").lower() != ENGINE_KEY):
        return 0.0
    recorded = getattr(trade, "quality_verdict", {}) or {}
    if not isinstance(recorded, dict):
        return 0.0
    checks = recorded.get("checks") or {}
    _, normal, wide = premium_cap_tiers(trade)
    normal_ratio, wide_ratio = 1.0 - normal / 100., 1.0 - wide / 100.
    approach_ratio = 1.0 - normal * .7 / 100.
    if recorded.get("policy_version") != "uts_native.v1" or checks.get("uts_premium_cap_pct") != normal:
        return 0.0
    if checks.get("uts_stop_extension_attempted") or checks.get("uts_stop_extended"):
        return 0.0
    if any(bool(getattr(trade, key, False)) for key in (
        "one_way_cascade_entry", "uts_multi_index_alignment", "market_ltp_shock_entry",
        "market_wide_reversal_entry")) or getattr(trade, "is_recovery", False):
        return 0.0
    if normalize_regime(context.get("regime", "UNKNOWN")) not in {MarketRegime.RANGING, MarketRegime.VOLATILE}:
        return 0.0
    entry = float(getattr(trade, "entry_price", 0.0) or getattr(trade, "price", 0.0) or 0.0)
    stop = float(getattr(trade, "current_stop", 0.0) or getattr(trade, "stop", 0.0) or 0.0)
    if entry <= 0 or abs(stop / entry - normal_ratio) > 0.001 or not stop < premium <= entry * approach_ratio:
        return 0.0
    if _g(_settings(settings), "options_sl_pct", 15.0) < wide:
        return 0.0
    metadata = result.get("uts_evaluation") or {}
    try:
        clock = _market_time(now)
        age = (clock - _market_time(metadata.get("observed_at"))).total_seconds()
        bar_age = (clock - _market_time(metadata.get("last_closed_bar"))).total_seconds()
        if age < 0 or age > 30 or not 300 <= bar_age <= 390:
            return 0.0
    except Exception:
        return 0.0
    if result.get("instrument") != getattr(trade, "instrument", "") or result.get("timeframe") != "5min":
        return 0.0
    state = result.get("state") or {}
    desired = 1 if str(getattr(trade, "direction", "")).upper() == "LONG" else -1
    if state.get("position") != desired:
        return 0.0
    from types import SimpleNamespace
    evidence = setup_values(SimpleNamespace(direction=getattr(trade, "direction", ""),
        adx_value=state.get("adx_value"), rsi_value=state.get("rsi_value")),
        volume_ratio=context.get("volume_ratio", 0.0),
        volume_available=context.get("volume_available", False),
        volume_basis=context.get("volume_basis", "UNAVAILABLE"))
    if not (evidence["adx"] >= 25 and evidence["directional_rsi"]
            and evidence["measured_volume"] and evidence["volume_ratio"] >= 1.2):
        return 0.0
    return round(entry * wide_ratio, 2)


def commit_extension(trade, stop, now):
    """Record the single approved exception in already-persisted trade fields."""
    recorded = dict(getattr(trade, "quality_verdict", {}) or {})
    checks = dict(recorded.get("checks") or {})
    checks.update(uts_stop_extension_attempted=True, uts_stop_extended=True,
                  uts_extended_stop=stop, uts_extension_at=str(now))
    recorded["checks"] = checks
    trade.quality_verdict = recorded
    trade.current_stop = trade.trailing_stop = stop
    if hasattr(trade, "stop"):
        trade.stop = stop


def premium_guard_pct(premium: float, index_stop_distance: float, delta: float, settings=None,
                      *, regime: str = "UNKNOWN", confidence=None, grade: str = "",
                      fast_entry: bool = False, setup=None, timeframe="") -> float:
    """Capital stop independent of the wider chart ATR and display grade."""
    return premium_cap_pct(setup, settings, regime=regime, timeframe=timeframe)


def premium_stop_distance(premium: float, index_stop_distance: float, delta: float, settings=None,
                          *, regime: str = "UNKNOWN", confidence=None, grade: str = "",
                          fast_entry: bool = False, setup=None, timeframe="") -> float:
    premium = float(premium or 0.0)
    if premium <= 0:
        return 0.0
    return premium * premium_guard_pct(
        premium, index_stop_distance, delta, settings,
        regime=regime, confidence=confidence, grade=grade, fast_entry=fast_entry,
        setup=setup, timeframe=timeframe,
    ) / 100.0


def exit_decision(trade: Any, spot: float, premium: float, now: Optional[datetime] = None,
                  engine_exit: Optional[dict] = None, settings=None,
                  observed_contract_ticks=None, **_ignored):
    """Chart exits plus a premium profit lock; hard stops and EOD remain with callers.

    The premium peak contains only observed post-entry prices. This protection does
    not wait for the chart's multi-bar ATR arm, which measures a different asset.
    """
    from engine.s1_lane import S1ExitDecision

    if engine_exit:
        chart = str(engine_exit.get("reason") or "")
        return S1ExitDecision(True, EXIT_REASONS.get(chart, "UTS_EXIT"), 0.0,
                              f"UT SMART EXIT chart exit: {chart or 'exit'}", runner=True)
    if str(getattr(trade, "inst_type", "") or "").upper() == "OPT":
        import math

        s = _settings(settings)
        entry = float(getattr(trade, "entry_price", 0.0) or getattr(trade, "price", 0.0) or 0.0)
        initial = float(getattr(trade, "initial_stop", 0.0) or getattr(trade, "premium_stop", 0.0)
                        or getattr(trade, "stop", 0.0) or 0.0)
        risk = entry - initial
        if all(math.isfinite(x) for x in (entry, initial, premium)) and 0 < risk < entry and premium > 0:
            # The exact contract stream may contain a peak and subsequent stop
            # touch before this monitor pass. Process it in received order;
            # taking the maximum first would use prices after an earlier exit.
            active = float(getattr(trade, "current_stop", 0.0) or initial)
            last_stamp = None
            for tick in observed_contract_ticks or ():
                try:
                    observed = float(tick[0])
                    stamp = _market_time(tick[1])
                    if (not math.isfinite(observed) or observed <= 0
                            or (now is not None and stamp > _market_time(now))
                            or (last_stamp is not None and stamp < last_stamp)):
                        continue
                    filled_at = getattr(trade, "entry_filled_at", None) or getattr(trade, "entry_time", None)
                    if filled_at is not None and stamp < _market_time(filled_at):
                        continue
                    last_stamp = stamp
                except (TypeError, ValueError, IndexError):
                    continue
                if active > 0 and observed <= active:
                    reason = "UTS_PROFIT_LOCK" if active > entry else "TRAILING_STOP"
                    return S1ExitDecision(True, reason, 0.0,
                        f"UTS observed premium {observed:.2f} touched active stop {active:.2f}",
                        premium_stop=active, runner=True)
                step = exit_decision(trade, spot, observed, now=stamp, settings=s)
                active = max(active, float(step.premium_stop or 0.0))
                if step.exit:
                    return step
            stored = float(getattr(trade, "s1_peak_premium", None) or entry)
            peak = max(entry, premium, stored if math.isfinite(stored) else entry)
            setattr(trade, "s1_peak_premium", peak)
            if peak - entry >= _g(s, "uts_lane_lock_arm_r", 1.0) * risk:
                giveback = min(100.0, max(0.0, _g(s, "uts_lane_lock_giveback_pct", 30.0)))
                floor = entry + (peak - entry) * (1.0 - giveback / 100.0)
                return S1ExitDecision(
                    premium <= floor, "UTS_PROFIT_LOCK" if premium <= floor else "", 0.0,
                    f"UTS premium peak {peak:.2f}; profit floor {floor:.2f}",
                    premium_stop=floor, runner=True,
                )
    return S1ExitDecision(False, "", 0.0, runner=True)


# The chart's exit label -> the trade's exit reason.
EXIT_REASONS = {
    "UT_REVERSE": "UTS_REVERSE",
    "SMART_EXIT": "UTS_SMART_EXIT",
    "SMART_HARD_EXIT": "UTS_HARD_EXIT",
    "REPAINT": "UTS_REPAINT",
}


def intrabar_enabled(settings=None) -> bool:
    """Forming entries require both the UTS opt-in and the general intrabar toggle."""
    s = _settings(settings)
    if not bool(getattr(s, 'uts_lane_allow_intrabar_entries', False)):
        return False
    try:
        return str(s.get_signal_fire_mode()).upper() == "INTRACANDLE"
    except Exception:
        return False


def intrabar_wait_seconds(obj: Any, settings=None) -> float:
    """Keep the requested UTS buffer independent of generic momentum timing."""
    s = _settings(settings)
    if str(getattr(obj, 'timeframe', '')) == '1min':
        return max(5.0, min(7.0, _g(s, 'uts_lane_intrabar_wait_seconds_1min', 5.0)))
    return max(0.0, _g(s, 'uts_lane_intrabar_wait_seconds', 15.0))


def _market_time(value):
    import pandas as pd

    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("missing timestamp")
    return stamp.tz_convert("Asia/Kolkata").tz_localize(None) if stamp.tzinfo else stamp


def signal_observation(trade: Any, result: Optional[dict], now: Optional[datetime], settings=None):
    from engine.scanner_modes import native_result
    result = native_result(trade, result)
    """Read a current, source-matched UTS evaluation without mutating trade state."""
    import pandas as pd

    if not isinstance(result, dict) or now is None:
        return None
    ts = getattr(trade, "signal_time", None) or getattr(trade, "signal_timestamp", None)
    if ts is None:
        return None
    try:
        want = _market_time(ts)
        clock = _market_time(now)
    except Exception:
        return None
    s = _settings(settings)
    bar_minutes = _tf_minutes(getattr(trade, "timeframe", ""))
    bar_close = want + pd.Timedelta(minutes=bar_minutes)
    evidence = result.get('uts_evaluation')
    if result.get('error') or str(result.get('source_engine') or '').lower() != ENGINE_KEY or not isinstance(evidence, dict):
        return None
    if str(result.get('timeframe') or '') != str(getattr(trade, 'timeframe', '') or ''):
        return None
    instrument = str(getattr(trade, 'instrument', '') or '').split()
    if not instrument or result.get('instrument') != instrument[0]:
        return None
    try:
        observed = _market_time(evidence['observed_at'])
        first = _market_time(evidence['first_bar'])
        last_closed = _market_time(evidence['last_closed_bar'])
        forming_bar = _market_time(evidence['forming_bar']) if evidence.get('forming_bar') is not None else None
        age = (clock - observed).total_seconds()
        if not (0 <= age <= _g(s, 'uts_lane_result_max_age_seconds', 30.0)) or first > want:
            return None
        if clock >= bar_close and last_closed < want:
            return None
        if clock < bar_close and forming_bar != want:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    side = "BUY" if str(getattr(trade, "direction", "LONG") or "LONG").upper() == "LONG" else "SELL"
    closed_hit = forming_hit = opposite_hit = False
    for sig in result.get("signals") or []:
        if str(getattr(sig, "source_engine", "") or "").lower() != ENGINE_KEY:
            continue
        try:
            if _market_time(getattr(sig, "timestamp", None)) != want:
                continue
            if getattr(sig, "signal_type", "") != side:
                opposite_hit = opposite_hit or getattr(sig, "signal_type", "") in {"BUY", "SELL"}
                continue
        except Exception:
            continue
        if getattr(sig, "uts_forming", False):
            forming_hit = True
        else:
            closed_hit = True
    return dict(clock=clock, bar_closed=clock >= bar_close, closed_hit=closed_hit,
                forming_hit=forming_hit, opposite_hit=opposite_hit)


def repaint_signal_status(trade: Any, result: Optional[dict], now: Optional[datetime], settings=None) -> str:
    """Classify a buffer observation only after its freshness and coverage checks."""
    observation = signal_observation(trade, result, now, settings)
    if observation is None:
        return "UNKNOWN"
    if observation["bar_closed"] and observation["forming_hit"] and not observation["closed_hit"]:
        return "UNKNOWN"
    if observation["closed_hit"] or observation["forming_hit"]:
        return "PRESENT"
    return "FLIPPED" if observation["opposite_hit"] else "VANISHED"


def repaint_exit(trade: Any, result: Optional[dict], now: Optional[datetime], settings=None, *, force_check=False) -> str:
    """Latch a proven intrabar repaint until exit completes; confirm closed flips."""
    if getattr(trade, "uts_repaint_exit_required", False):
        return "REPAINT"
    if getattr(trade, "uts_confirmed", False) or not bool(getattr(trade, "entered_intracandle", True)):
        return ""
    observation = signal_observation(trade, result, now, settings)
    if observation is None:
        return ""
    clock = observation["clock"]
    if observation["bar_closed"]:
        if observation["closed_hit"]:
            setattr(trade, "uts_confirmed", True)
            return ""
        if observation["forming_hit"]:
            return ""
    else:
        every = max(1.0, _g(_settings(settings), "uts_lane_repaint_check_seconds", 10.0))
        last = (getattr(trade, "uts_repaint_checked_at", None)
                or getattr(trade, "entry_filled_at", None) or getattr(trade, "entry_time", None))
        try:
            if not force_check and last is not None and (clock - _market_time(last)).total_seconds() < every:
                return ""
        except Exception:
            pass
        setattr(trade, "uts_repaint_checked_at", clock.to_pydatetime())
        if observation["forming_hit"] or observation["closed_hit"]:
            return ""
    setattr(trade, "uts_repaint_exit_required", True)
    return "REPAINT"


# Dashboard-editable UT SMART EXIT settings (same shape as s1_lane.S1_SETTING_SPEC).
UTS_SETTING_SPEC = {
    "uts_key_value": ("float", 0.1, 10.0),
    "uts_atr_period": ("int", 1, 100),
    "uts_key_value_5min": ("float", 0.1, 30.0),
    "uts_atr_period_5min": ("int", 1, 100),
    "uts_key_value_1min": ("float", 0.1, 30.0),
    "uts_atr_period_1min": ("int", 1, 100),
    "uts_use_heikin_ashi": ("bool",),
    "uts_smart_exit": ("bool",),
    "uts_arm_atr": ("float", 0.1, 10.0),
    "uts_retrace_atr": ("float", 0.05, 10.0),
    "uts_hard_retrace_atr": ("float", 0.2, 10.0),
    "uts_min_bars": ("int", 0, 50),
    "uts_confirmations": ("int", 1, 4),
    "uts_rsi_length": ("int", 2, 100),
    "uts_rsi_ob": ("float", 50.0, 100.0),
    "uts_rsi_os": ("float", 0.0, 50.0),
    "uts_rsi_lookback": ("int", 2, 50),
    "uts_exit_ema": ("int", 1, 100),
    "uts_wick_ratio": ("float", 0.1, 0.9),
    "uts_session_end": ("time",),
    "uts_lane_enabled": ("bool",),
    "uts_lane_allow_intrabar_entries": ("bool",),
    "uts_lane_result_max_age_seconds": ("float", 1.0, 120.0),
    "uts_lane_reclaim_start": ("time",),
    "uts_lane_sl_max_pct": ("float", 0.05, 5.0),
    "uts_lane_min_entry_rr": ("float", 0.1, 5.0),
    "uts_lane_target_r": ("float", 0.5, 50.0),
    "uts_lane_guard_mult": ("float", 1.0, 10.0),
    "uts_lane_guard_min_pct": ("float", 1.0, 80.0),
    "uts_lane_guard_max_pct": ("float", 1.0, 90.0),
    "uts_lane_lock_arm_r": ("float", 0.5, 10.0),
    "uts_lane_lock_giveback_pct": ("float", 0.0, 100.0),
    "uts_lane_5m_intel_gate_enabled": ("bool",),
    "uts_lane_5m_min_volume_ratio": ("float", 0.0, 5.0),
    "uts_lane_5m_min_adx": ("float", 0.0, 60.0),
    "uts_lane_5m_require_directional_rsi": ("bool",),
    "uts_lane_5m_pcr_veto_enabled": ("bool",),
    "uts_lane_intrabar_wait_seconds": ("float", 0.0, 120.0),
    "uts_lane_intrabar_wait_seconds_1min": ("float", 5.0, 7.0),
    "uts_lane_repaint_check_seconds": ("float", 1.0, 120.0),
    "uts_lane_repaint_reentry_grace_seconds": ("float", 0.0, 600.0),
}


def dashboard_values(settings=None) -> dict:
    s = _settings(settings)
    return {name: getattr(s, name, None) for name in UTS_SETTING_SPEC} if s is not None else {}
