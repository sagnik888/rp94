import pandas as pd
import time
import copy
import re
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple, Optional
from loguru import logger
from dataclasses import dataclass, field
from config.settings import get_settings
from engine.intelligence_score import (
    LEGACY_SCORE_SCALE,
    aggregate_intelligence_score,
    normalize_intelligence_score,
)
from engine.option_history_store import get_option_history_store
from engine.signal_quality_policy import (
    b_grade_entry_allowed,
    choppy_confidence_gate,
    dynamic_rr,
    fifteen_min_confidence_floor,
    five_min_true_impulse_allowed,
    grade_min_confidence,
    grade_rank,
    is_choppy_regime,
    live_5min_choppy_confidence_floor,
    option_grade_allowed,
    option_index_stop_distance,
    option_premium_health_ok,
    passes_quality_gate,
    power_move_confirmed,
    relaxed_threshold,
    volume_spike_entry_confirmed,
    volume_spike_min_ratio,
)
from engine.market_regime import (
    futures_stop_tolerance_pct,
    is_trending_regime,
    regime_aligns,
)
from engine.entry_policy import effective_underlying_stop_cap_pct
from engine.position_sizing import resolve_trade_lots, size_lots_for_trade
from engine.trade_accounting import estimate_trade_charges
from engine.signal_manager import GradedSignal
from trading.trade_manager import IST, Trade
from engine.ut_bot_core import UTBotSignal


def _is_bfo_instrument(value) -> bool:
    normalized = str(value or "").upper()
    return "SENSEX" in normalized or "BANKEX" in normalized


def _setting_enabled(value, default: bool = True) -> bool:
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


def directional_fut_veto(
    settings,
    *,
    inst_type: str,
    signal_type: str,
    regime: str,
    adx_value: float,
    power_move: bool = False,
) -> str:
    """Return a rejection reason if a directional FUTURES entry is in trendless
    chop or fights a confirmed opposite trend, else "".

    Narrow veto (relies on the now-honest regime detector). Blocks directional
    FUT only when:
      (a) the regime is trendless chop (RANGING/VOLATILE/UNKNOWN) AND there is no
          ADX momentum (adx < floor) — the genuine negative-expectancy case, or
      (b) it fights a CONFIRMED opposite trend (regime trends against the signal).
    It does NOT block FUT in aligned trends, nor in choppy regimes that carry real
    ADX momentum. This is deliberately less aggressive than the old
    "block-unless-perfect-aligned-trend" rule, which (with the fixed detector)
    strangled legitimate FUT and cratered backtest gains. The low-ADX "breakout"
    case it used to need is now handled at the source: the detector no longer
    mislabels those as BREAKOUT, so they arrive as RANGING/VOLATILE and (a) catches
    them. Options and data-confirmed power-moves are exempt.
    """
    if str(inst_type or "").upper() != "FUT":
        return ""
    if power_move:
        return ""
    if not _setting_enabled(getattr(settings, "regime_veto_enabled", True), True):
        return ""
    veto_adx = float(getattr(settings, "regime_veto_adx_floor", 18.0) or 18.0)
    adx = float(adx_value or 0.0)
    # (a) trendless chop with no momentum
    if is_choppy_regime(regime) and adx < veto_adx:
        return (
            f"Regime veto: directional FUT in trendless {regime} chop "
            f"(ADX {adx:.0f} < {veto_adx:.0f})"
        )
    # (b) fighting a confirmed opposite trend
    if is_trending_regime(regime) and not regime_aligns(regime, signal_type):
        return f"Regime veto: directional FUT counter to confirmed {regime} trend"
    return ""


def session_trend_guard_census_yield(
    settings,
    *,
    census: dict,
    signal_type: str,
    expected: str,
    day_type: str = "",
) -> str:
    """Return a yield reason when the live census contradicts the day-type label.

    2026-07-08: the session-context label stayed TREND_DAY_UP and vetoed six
    midcap SELLs (11:19-12:36) while the session's own directional census was
    already 5L/10S bearish — midcap then fell 1.9%. When the census opposes the
    label by `dominance` with at least `min_total` votes, the label is stale and
    the trend-phase guard must yield instead of vetoing. Returns "" to keep the
    guard's veto in force."""
    if not _setting_enabled(
        getattr(settings, "session_trend_guard_census_yield_enabled", True), True
    ):
        return ""
    signal_side = "LONG" if str(signal_type or "").upper() == "BUY" else "SHORT"
    label_side = "LONG" if str(expected or "").upper() == "BUY" else "SHORT"
    if signal_side == label_side:
        return ""
    signal_votes = int((census or {}).get(signal_side, 0) or 0)
    label_votes = int((census or {}).get(label_side, 0) or 0)
    min_total = int(getattr(settings, "session_trend_guard_census_min_total", 6) or 6)
    dominance = float(getattr(settings, "session_trend_guard_census_dominance", 1.5) or 1.5)
    if (signal_votes + label_votes) < min_total:
        return ""
    if signal_votes < max(1, label_votes) * dominance:
        return ""
    return (
        f"Session trend guard yielded: census {signal_votes}{signal_side[0]}/"
        f"{label_votes}{label_side[0]} contradicts stale {str(day_type or 'trend-day')} label"
    )


def directional_census_block(
    settings,
    census: dict,
    *,
    signal_direction: str,
    confidence: float,
    grade_floor: float,
    power_move: bool = False,
    reversal_context: Optional[dict] = None,
    day_type: str = "",
    day_type_confidence: float = 0.0,
) -> str:
    """Return a rejection reason if a counter-session-bias entry is too weak, else "".

    When the session strongly favours one side, counter-direction entries must
    clear an elevated confidence floor. Shared by live and replay paths.

    The census counts the directions THIS SCANNER emitted, never price, so it can go
    stale against the tape. A CONFIRMED trend day agreeing with the signal outranks it —
    the mirror of session_trend_guard_census_yield above, which exists because a stale
    LABEL once vetoed signals the census had already turned against.
    """
    if power_move:
        return ""
    if not _setting_enabled(getattr(settings, "directional_census_enabled", True), True):
        return ""
    # 2026-08-24: every index fell (BANKNIFTY -582) and the scanner emitted 53 shorts
    # against 45 longs, but a long-heavy morning left the census at 18L/9S, so shorts
    # were refused as "counter-bias" while session context simultaneously read
    # day_type=TREND_DAY_DOWN, bias=BEARISH at 0.92 confidence. Both cannot be right;
    # the label is the one that looks at price.
    if _setting_enabled(
        getattr(settings, "directional_census_trend_day_yield_enabled", True), True
    ):
        _label = str(day_type or "").strip().upper()
        _agrees = (
            (_label == "TREND_DAY_UP" and str(signal_direction or "").upper() == "LONG")
            or (_label == "TREND_DAY_DOWN" and str(signal_direction or "").upper() == "SHORT")
        )
        if _agrees:
            try:
                _floor = float(
                    getattr(settings, "directional_census_trend_day_min_confidence", 0.60)
                    or 0.60
                )
            except (TypeError, ValueError):
                _floor = 0.60
            # A hesitant label must not be able to switch the guard off.
            if float(day_type_confidence or 0.0) >= _floor:
                return ""
    longs = int((census or {}).get("LONG", 0))
    shorts = int((census or {}).get("SHORT", 0))
    total = longs + shorts
    min_n = int(getattr(settings, "directional_census_min_signals", 12) or 12)
    if total < min_n:
        return ""
    ratio_thr = float(getattr(settings, "directional_census_ratio", 1.5) or 1.5)
    dom = "LONG" if longs >= shorts else "SHORT"
    dom_n = max(longs, shorts)
    weak_n = min(longs, shorts)
    skewed = weak_n == 0 or (dom_n / max(1, weak_n)) >= ratio_thr
    if not skewed or signal_direction == dom:
        return ""
    if reversal_context and _setting_enabled(
        getattr(settings, "directional_census_reversal_relief_enabled", True),
        True,
    ):
        relief_floor = float(
            getattr(settings, "directional_census_reversal_min_confidence", 0.20) or 0.20
        )
        if float(confidence or 0.0) >= relief_floor:
            return ""
    penalty = float(getattr(settings, "directional_census_penalty", 0.05) or 0.05)
    floor = float(grade_floor or 0.0) + penalty
    if float(confidence or 0.0) >= floor:
        return ""
    return (
        f"Counter-session-bias {signal_direction} (session {longs}L/{shorts}S) "
        f"needs >= {floor:.0%}, got {float(confidence or 0.0):.0%}"
    )


def price_action_context(scanner, instrument: str, timeframe: str, sig, settings):
    """Structural price-action context for a signal — SEPARATE, gated patch.

    Returns the PriceActionAnalyzer context (or None) using the instrument's
    timeframe candles sliced to the signal's bar (look-ahead safe). Default OFF.
    Self-contained: remove this function + its one call site + the module to drop
    the whole feature.
    """
    if not _setting_enabled(getattr(settings, "price_action_module_enabled", False), False):
        return None
    try:
        from intelligence.price_action import PriceActionAnalyzer
        df = scanner.candles.get_candles(instrument, timeframe)
        if df is None or len(df) < 12:
            return None
        sig_ts = getattr(sig, "timestamp", None)
        if sig_ts is not None and hasattr(df, "index") and isinstance(df.index, pd.DatetimeIndex):
            ts = pd.Timestamp(sig_ts)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
            df = df[df.index <= ts]
        if df is None or len(df) < 12:
            return None
        # Day-type-aware premium/discount: on a confirmed aligned trend day the
        # chase penalty is scaled down (continuation lives at the extreme). Uses
        # the same session-context source and 0.70 confidence bar as the trend
        # guard; instantly reversible via price_action_premium_trend_scaled.
        premium_trend_scale = 1.0
        try:
            if _setting_enabled(
                getattr(settings, "price_action_premium_trend_scaled", True), True
            ):
                base = str(instrument or "").split()[0].upper()
                _intel = scanner._cached_timeframe_intel(base, "5min", allow_fallback=True) or {}
                _sctx = _intel.get("session_context", {}) if isinstance(_intel, dict) else {}
                if isinstance(_sctx, dict):
                    _day = str(
                        _sctx.get("effective_day_type") or _sctx.get("day_type") or ""
                    ).upper()
                    _conf = float(
                        _sctx.get("phase_confidence")
                        if _sctx.get("phase_confidence") is not None
                        else _sctx.get("confidence") or 0.0
                    )
                    _side = str(getattr(sig, "signal_type", "") or "").upper()
                    _aligned = (_day == "TREND_DAY_UP" and _side == "BUY") or (
                        _day == "TREND_DAY_DOWN" and _side == "SELL"
                    )
                    _min_conf = float(
                        getattr(settings, "price_action_premium_trend_min_confidence", 0.70)
                        or 0.70
                    )
                    if _aligned and _conf >= _min_conf:
                        premium_trend_scale = max(
                            0.0,
                            min(
                                1.0,
                                float(
                                    getattr(settings, "price_action_premium_trend_scale", 0.5)
                                    or 0.5
                                ),
                            ),
                        )
        except Exception:
            premium_trend_scale = 1.0
        return PriceActionAnalyzer().analyze(
            df,
            getattr(sig, "signal_type", "BUY"),
            swing_strength=int(getattr(settings, "price_action_swing_strength", 5) or 5),
            use_premium_discount=_setting_enabled(
                getattr(settings, "price_action_premium_discount_enabled", True), True
            ),
            use_liquidity=_setting_enabled(
                getattr(settings, "price_action_liquidity_enabled", True), True
            ),
            premium_trend_scale=premium_trend_scale,
        )
    except Exception:
        return None


def _parse_clock(label: str, default: str):
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(label).strip(), fmt).time()
        except ValueError:
            continue
    return datetime.strptime(default, "%H:%M").time()


def _eod_protect_entry_cutoff(settings):
    """When EOD profit-protection engages, or None if it is off/unknowable.

    Entries taken after this point are opened into a regime whose own EOD logic closes
    them immediately, so this is a hard ceiling on any entry cutoff.
    """
    if not _setting_enabled(
        getattr(settings, "block_entries_during_eod_protect", True), True
    ):
        return None
    if not _setting_enabled(getattr(settings, "eod_protect_enabled", True), True):
        return None
    try:
        window = float(getattr(settings, "eod_protect_window_minutes", 30.0) or 0.0)
        if window <= 0:
            return None
        force_exit = _parse_clock(
            getattr(settings, "ut_force_exit_time", "15:22") or "15:22", "15:22"
        )
        start = datetime.combine(date.min, force_exit) - timedelta(minutes=window)
        return start.time()
    except Exception:
        return None


def _entry_cutoff(settings, timeframe: str):
    """Return the effective no-entry label and time for a timeframe.

    2026-08-11: ut_5min_no_entry_after ("15:14") sat 22 minutes AFTER EOD protection
    engaged (ut_force_exit_time 15:22 minus a 30-minute window = 14:52), so for 22
    minutes the system opened fresh 5min positions into a regime whose EOD logic closed
    them on the next tick. Four entries at 15:09-15:11 round-tripped in 0.06-0.19s and
    paid ~Rs.80 of round-trip charges each for holds of a fraction of a second.

    The two settings are now reconciled rather than left to contradict each other: the
    effective cutoff is whichever comes first.
    """
    if str(timeframe) in {"1min", "5min"}:
        label = str(getattr(settings, "ut_5min_no_entry_after", "15:15") or "15:15")
    else:
        label = str(getattr(settings, "ut_no_entry_after", "15:00") or "15:00")
    cutoff = _parse_clock(label, "15:15")
    eod_start = _eod_protect_entry_cutoff(settings)
    if eod_start is not None and eod_start < cutoff:
        return f"{eod_start.strftime('%H:%M')} (EOD protect)", eod_start
    return label, cutoff


#: Grading telemetry lives on the graded UTBotSignal, but TradeCandidate is built fresh
#: field-by-field and rtracker reads these off the CANDIDATE — so without an explicit hop
#: every `grading` block logged empty. Keep this list in sync with rtracker's grading block.
GRADING_TELEMETRY_ATTRS = (
    "graded_timeframe",
    "graded_intel_score",
    "graded_regime",
    "grade_pillars",
)


def carry_grading_telemetry(candidate, signal) -> None:
    """Copy grading telemetry from the graded signal onto the candidate that is recorded.

    `_grade_signal` attaches the pillar breakdown to the signal, and the per-timeframe
    grading context is set on that same object. TradeCandidate does not inherit from it,
    so this hop is what makes either visible in the decision tape. Telemetry only — it
    must never raise into the signal path.
    """
    for name in GRADING_TELEMETRY_ATTRS:
        value = getattr(signal, name, None)
        if value is None:
            continue
        try:
            setattr(candidate, name, value)
        except Exception:  # pragma: no cover - dataclass always accepts these
            pass


def _intelligence_stop_pct(base_pct: float, intel_score: float, settings) -> float:
    """Nudge the option stop percentage by how strong the corroborating evidence is.

    intel_score runs 0-100 with 50 as neutral. Above neutral the stop may widen slightly
    (conviction earns room), below it the stop tightens. The adjustment is capped by
    dynamic_stop_intel_max_shift_pct so a stop chosen by ATR and the per-timeframe rules
    is only ever nudged, never redefined, and the result is clamped to a sane band.

    Off unless dynamic_stop_from_intelligence is set: this moves the risk on every option
    entry, which is a strategy decision rather than a defect.
    """
    try:
        base = float(base_pct or 0.0)
    except (TypeError, ValueError):
        return float(base_pct or 0.0)
    if base <= 0:
        return base
    enabled = getattr(settings, "dynamic_stop_from_intelligence", False)
    if not (enabled is True or str(enabled).strip().lower() in {"1", "true", "yes", "on"}):
        return base
    try:
        score = float(intel_score or 0.0)
    except (TypeError, ValueError):
        return base
    if score <= 0:
        return base                      # no reading is not a reason to move the stop
    try:
        max_shift = float(getattr(settings, "dynamic_stop_intel_max_shift_pct", 20.0) or 0.0)
    except (TypeError, ValueError):
        max_shift = 20.0
    max_shift = max(0.0, min(50.0, max_shift)) / 100.0
    if max_shift <= 0:
        return base
    lean = max(-1.0, min(1.0, (score - 50.0) / 50.0))
    return max(0.5, min(base * 2.0, base * (1.0 + lean * max_shift)))


@dataclass

class TradeCandidate:
    instrument: str
    direction: str
    price: float
    stop: float
    target: float
    lots: int
    lot_size: int
    grade: str
    confidence: float
    timeframe: str
    inst_type: str
    option_type: str
    atm_strike: float
    multiplier: float
    trading_symbol: str
    symbol_token: str
    rr: float = 1.5
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    # Explicit, non-interchangeable price spaces. ``price/stop/target`` remain the
    # operational traded-contract tuple for compatibility; these fields make the
    # underlying risk and option-premium risk independently auditable.
    entry_spot: float = 0.0
    spot_stop: float = 0.0
    spot_target: float = 0.0
    underlying_entry: float = 0.0
    underlying_stop: float = 0.0
    underlying_target: float = 0.0
    premium_entry: float = 0.0
    premium_stop: float = 0.0
    premium_target: float = 0.0
    geometry_observed_at: str = ""
    geometry_source: str = ""
    geometry_verified: bool = False
    geometry_schema: str = "dual_price_geometry.v1"
    expected_move_forecast: dict[str, Any] = field(default_factory=dict)
    entry_policy_verdict: dict[str, Any] = field(default_factory=dict)
    signal_timestamp: Optional[datetime] = None
    current_price: float = 0.0
    pnl: float = 0.0
    status: str = "TRADE SIGNAL"
    action: str = "ENTRY"
    exit_reason: str = ""
    adx_value: float = 0.0
    is_explosive_bypass: bool = False
    impulse_confirmed: bool = False
    power_move: bool = False
    volume_spike_entry: bool = False
    market_ltp_shock_entry: bool = False
    late_session_reclaim_entry: bool = False
    one_way_cascade_entry: bool = False
    fifteen_min_choppy_scalp_entry: bool = False
    volume_ratio: float = 0.0
    range_atr_ratio: float = 0.0
    close_location: float = 0.0
    tight_stop_pct: float = 0.0
    initial_stop: float = 0.0
    scalp_lock_mode: bool = False
    quote_verified: bool = False
    quote_source: str = ""
    execution_bid_price: float = 0.0
    execution_ask_price: float = 0.0
    execution_touch_source: str = ""
    execution_touch_age_seconds: float = 0.0
    execution_touch_usable: bool = False
    execution_touch_reason: str = ""
    # Per-contract tradeability evidence carried from the cached option chain to
    # the execution boundary.  Availability is explicit because a measured zero
    # must fail a minimum-liquidity rule while a missing chain is merely unknown.
    strike_oi: float = 0.0
    strike_volume: float = 0.0
    strike_oi_available: bool = False
    strike_volume_available: bool = False
    strike_liquidity_available: bool = False
    source_engine: str = ""
    source_engines: list[str] = field(default_factory=list)
    source_lane: str = ""
    quad_union_entry: bool = False
    raw_union_entry: bool = False


def _sig_date(signal) -> Optional[date]:
    """The calendar date of a signal, or None when it carries no usable timestamp.

    Accepts either a signal object with a ``timestamp`` attribute or a bare
    datetime/date. The bare case is checked FIRST because datetime itself has a
    ``timestamp`` *method* -- reading the attribute would hand back a bound method
    and quietly resolve to None.

    Every caller of _estimate_opt_premium used to omit trade_date, which disabled
    both the DTE boost and the historical-VIX lookup. Returning None preserves that
    old behaviour for an unusable timestamp rather than raising on the hot path.
    """
    if isinstance(signal, datetime):
        return signal.date()
    if isinstance(signal, date):
        return signal
    ts = getattr(signal, "timestamp", None)
    if ts is None or callable(ts):
        return None
    try:
        if isinstance(ts, datetime):
            return ts.date()
        if isinstance(ts, date):
            return ts
        parsed = pd.to_datetime(ts)
        # NaN/None parse to NaT rather than raising, and NaT.date() is NaT again --
        # which would sail out of here as a truthy non-date.
        if parsed is None or pd.isna(parsed):
            return None
        return parsed.date()
    except Exception:
        return None


def _estimate_opt_premium(spot_price: float, scanner=None, instrument: str = "", trade_date=None) -> float:
    """Dynamic option premium estimation based on VIX.

    ``instrument`` and ``trade_date`` gate the DTE boost and the historical-VIX
    average. Every call site omitted them for the life of this function, so
    dte_boost was pinned at 1.0 and HISTORICAL runs silently priced against TODAY's
    live VIX -- lookahead bias in every backtest.
    """
    base_pct = 0.012
    vix_val = 15.0
    if scanner and trade_date is not None and hasattr(scanner, "_vix_data"):
        try:
            day = str(trade_date)[:10]
            day_values = [
                float(v)
                for k, v in getattr(scanner, "_vix_data", {}).items()
                if str(k).startswith(day) and float(v or 0.0) > 0
            ]
            if day_values:
                vix_val = sum(day_values) / len(day_values)
        except Exception:
            pass
    elif scanner and hasattr(scanner, "data"):
        try:
            live_vix = scanner.data.get_latest_price("INDIA VIX")
            if live_vix and live_vix > 0:
                vix_val = live_vix
        except Exception:
            pass

    dte_boost = 1.0
    if scanner and instrument and trade_date is not None and hasattr(scanner, "expiry"):
        try:
            dte = max(0.0, float(scanner.expiry.get_dte(instrument, trade_date)))
            dte_boost = max(0.75, min(1.35, (max(dte, 1.0) / 3.0) ** 0.25))
        except Exception:
            pass

    # Premium roughly scales with VIX. At VIX 15, pct is 1.2%
    scaler = max(10.0, vix_val) / 15.0
    return spot_price * base_pct * scaler * dte_boost

class SignalProcessor:
    """
    Handles signal candidate selection, scoring, and prioritization.
    Extracted from Scanner to reduce file size.
    """
    def __init__(self, scanner):
        self.scanner = scanner
        self._option_history_cache: Dict[Tuple[str, str, str], pd.DataFrame] = {}
        self._option_symbol_history_cache: Dict[Tuple[str, str], pd.DataFrame] = {}
        self._option_history_fetch_attempted = set()
        self._option_history_store = get_option_history_store()

    def _historical_audit_log(self, message: str) -> None:
        """Keep bulk backtest reject details out of INFO unless explicitly enabled."""
        scanner = getattr(self, "scanner", None)
        if str(getattr(scanner, "mode", "") or "").upper() == "HISTORICAL":
            settings = get_settings()
            if _setting_enabled(getattr(settings, "ut_historical_verbose_audit_logs", False), False):
                logger.info(message)
            else:
                logger.debug(message)
            return
        logger.info(message)

    @staticmethod
    def _candidate_timeframe_context(
        timeframe: str,
        fallback_intel_result: Dict,
        fallback_intel_score: float,
        fallback_regime: str,
        timeframe_contexts: Optional[Dict[str, Dict]] = None,
    ) -> Tuple[Dict, float, str]:
        """Return intelligence for a candidate's OWN timeframe.

        process_best_signal receives a single intel_result/intel_score/regime (the 5-minute
        snapshot) and grades every candidate with it, so 15-minute candidates were graded
        on 5-minute intelligence and regime. `timeframe_contexts` is optional: when it is
        absent or lacks an entry for `timeframe`, the caller's existing 5-minute values are
        returned unchanged, so behaviour is identical to before.
        """
        fallback_result = fallback_intel_result if isinstance(fallback_intel_result, dict) else {}
        context = (timeframe_contexts or {}).get(str(timeframe or ""))
        if not isinstance(context, dict):
            return fallback_result, float(fallback_intel_score or 0.0), str(fallback_regime or "UNKNOWN")

        nested_result = context.get("intel_result")
        result = nested_result if isinstance(nested_result, dict) else context
        score = context.get("intel_score", context.get("score", fallback_intel_score))
        try:
            score = float(score or 0.0)
        except (TypeError, ValueError):
            score = float(fallback_intel_score or 0.0)
        context_regime = context.get("regime", fallback_regime)
        if isinstance(context_regime, dict):
            context_regime = context_regime.get("regime", fallback_regime)
        if not context_regime and isinstance(result, dict):
            result_regime = result.get("regime", {})
            context_regime = result_regime.get("regime") if isinstance(result_regime, dict) else result_regime
        return result if isinstance(result, dict) else fallback_result, score, str(context_regime or "UNKNOWN")

    @staticmethod
    def _strike_liquidity_snapshot(chain, strike, option_type) -> Dict[str, Any]:
        """Measured OI/volume for one strike, preserving missing-vs-zero.

        The previous tuple-only helper collapsed both states to ``(0, 0)``.  That
        is safe for a ranking tie-break (missing data has no opinion) but unsafe
        for an absolute admission gate, where a broker-measured zero is evidence
        that the contract is not tradeable enough.
        """
        empty = {
            "oi": 0.0,
            "volume": 0.0,
            "oi_available": False,
            "volume_available": False,
            "available": False,
        }
        if chain is None or not len(chain) or "strike" not in getattr(chain, "columns", []):
            return empty
        side = "call" if str(option_type or "").upper() == "CE" else "put"
        oi_col, vol_col = f"{side}_oi", f"{side}_volume"
        try:
            target = float(strike or 0.0)
            if target <= 0:
                return empty
            # Strikes arrive as floats from the broker; match on proximity, not equality.
            row = chain[(chain["strike"] - target).abs() < 0.5]
            if not len(row):
                return empty
            values = row.iloc[0]
        except (TypeError, ValueError, KeyError, IndexError):
            return empty

        result = dict(empty)
        for metric, column in (("oi", oi_col), ("volume", vol_col)):
            if column not in chain.columns:
                continue
            try:
                value = float(values[column])
            except (TypeError, ValueError, KeyError):
                continue
            if value != value:  # NaN is absent evidence, not a measured zero.
                continue
            result[metric] = max(0.0, value)
            result[f"{metric}_available"] = True
        result["available"] = bool(
            result["oi_available"] and result["volume_available"]
        )
        return result

    @staticmethod
    def _strike_liquidity(chain, strike, option_type) -> Tuple[float, float]:
        """(open interest, traded volume) at ONE strike, from the already-cached chain.

        The chain carries these per strike -- call_oi / call_volume / put_oi /
        put_volume -- so nothing here costs a broker call. Returns (0.0, 0.0) whenever
        the answer is not knowable, which the caller treats as "no opinion" rather than
        as "illiquid": an unreadable chain must never move the selection.
        """
        snapshot = SignalProcessor._strike_liquidity_snapshot(
            chain, strike, option_type
        )
        # Backward-compatible ranking API: unavailable remains "no opinion".
        if not snapshot["available"]:
            return 0.0, 0.0
        return float(snapshot["oi"]), float(snapshot["volume"])

    @staticmethod
    def _prefer_liquid_strike(ranked, settings):
        """Break a near-tie between strike candidates on tradeability.

        `ranked` is already sorted by score, so ranked[0] is the incumbent and only a
        candidate within the configured band may displace it -- a materially better
        setup always wins regardless of liquidity.

        The challenger must be better on BOTH open interest and traded volume. A
        weighted blend of the two would need a weight nobody can justify; requiring both
        is scale-free and means "more standing interest AND more activity today".
        """
        if not ranked:
            return None
        leader = ranked[0]
        if len(ranked) < 2:
            return leader
        if not _setting_enabled(
            getattr(settings, "option_strike_liquidity_ranking_enabled", True), True
        ):
            return leader
        try:
            band = float(getattr(settings, "option_strike_liquidity_score_band", 0.05) or 0.0)
            edge = float(getattr(settings, "option_strike_liquidity_min_advantage", 1.25) or 1.25)
        except (TypeError, ValueError):
            return leader
        if band <= 0 or edge <= 1.0:
            return leader

        def _measured(candidate) -> float:
            """Score with the standing ITM preference removed.

            itm_bonus is a flat 0.15 added to the ITM candidate -- a policy preference,
            not a measurement of that contract. Comparing raw scores meant the gap was
            structurally at least 0.15 whenever ITM led, three times the default band,
            so the tie-break could not fire. Netting it out lets liquidity compete with
            the preference instead of being buried by it.
            """
            try:
                return float(candidate.score) - float(
                    getattr(candidate, "strike_preference_bonus", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                return float("nan")

        lead_oi, lead_vol = (
            getattr(leader, "strike_oi", 0.0) or 0.0,
            getattr(leader, "strike_volume", 0.0) or 0.0,
        )
        lead_measured = _measured(leader)
        for challenger in ranked[1:]:
            gap = lead_measured - _measured(challenger)
            if gap != gap:  # NaN: an unscored candidate cannot be compared
                continue
            if gap > band:
                # `continue`, not `break`: ranked is sorted by RAW score, and netting
                # out the preference bonus can reorder it, so a later candidate may
                # still be inside the band.
                continue
            oi = float(getattr(challenger, "strike_oi", 0.0) or 0.0)
            vol = float(getattr(challenger, "strike_volume", 0.0) or 0.0)
            # Absent liquidity is no opinion, not a verdict. Only a strike the chain
            # actually described may displace the incumbent.
            if oi <= 0 or vol <= 0 or lead_oi <= 0 or lead_vol <= 0:
                continue
            if oi >= lead_oi * edge and vol >= lead_vol * edge:
                return challenger
        return leader

    @staticmethod
    def _graded_candidate_context(
        best_sig,
        fallback_regime: str,
        fallback_intel_norm: float,
    ) -> Tuple[str, float]:
        """Return the (regime, normalised intel) a candidate should be STAMPED with.

        _candidate_timeframe_context grades a 15-minute candidate on 15-minute
        intelligence, but the candidate was then stamped with the 5-minute
        `regime`/`intel_norm` that happened to be in scope. Every downstream gate and
        exit reads candidate.market_regime / .intelligence_score, so enabling
        per-timeframe grading changed the grade and nothing else.

        The values recorded on the signal by the grading loop are the ones that
        actually graded it. With per_timeframe_grading_enabled off they resolve to the
        caller's 5-minute values, so this returns the previous pair unchanged.

        Presence, not truthiness: a genuine 0.0 intelligence score is a real reading
        and must not fall back to the 5-minute score.
        """
        regime = str(getattr(best_sig, "graded_regime", "") or "").strip()
        if not regime:
            regime = str(fallback_regime or "UNKNOWN")

        attr = getattr(best_sig, "graded_intel_score", None)
        if isinstance(attr, bool) or not isinstance(attr, (int, float)):
            return regime, float(fallback_intel_norm or 0.0)
        try:
            graded_raw = float(attr)
        except (TypeError, ValueError):
            return regime, float(fallback_intel_norm or 0.0)
        if graded_raw != graded_raw or graded_raw in (float("inf"), float("-inf")):
            return regime, float(fallback_intel_norm or 0.0)
        # Same unit as the caller's intel_score, which process_best_signal converts
        # with normalize_intelligence_score. This is a scalar already extracted by
        # _candidate_timeframe_context, not an aggregate payload -- reading score_norm
        # off a payload belongs there, at the extraction, not here.
        return regime, float(normalize_intelligence_score(graded_raw))

    @staticmethod
    def _graded_candidate_intel_result(best_sig, fallback_intel_result: Dict) -> Dict:
        """Return the full intelligence bundle that graded ``best_sig``.

        The scalar regime/score already crossed the grading→candidate seam, but the
        candidate's pillar/context payload was still built from the function-scope
        five-minute bundle.  Downstream gates and exits therefore saw a mixed object:
        a 15-minute scalar beside 5-minute OI/volume/session context.

        When per-timeframe grading is disabled, the grading loop stores the exact
        fallback object here, so this helper returns that same object and preserves
        flag-off behavior rather than manufacturing a second policy path.
        """
        graded = getattr(best_sig, "graded_intel_result", None)
        if isinstance(graded, dict):
            return graded
        return fallback_intel_result if isinstance(fallback_intel_result, dict) else {}

    @staticmethod
    def _build_timeframe_contexts(scanner, instrument: str, settings) -> Optional[Dict[str, Dict]]:
        """Collect each timeframe's own cached intelligence snapshot.

        Returns None only when the feature is disabled. With the live default enabled,
        callers can distinguish a genuinely missing own-timeframe snapshot from a
        deliberate legacy/fallback mode.
        """
        enabled = getattr(settings, "per_timeframe_grading_enabled", False)
        if not (enabled is True or str(enabled).strip().lower() in {"1", "true", "yes", "on"}):
            return None
        getter = getattr(scanner, "_cached_timeframe_intel", None)
        if not callable(getter):
            return {}
        contexts: Dict[str, Dict] = {}
        for tf in ("1min", "5min", "15min"):
            try:
                intel = getter(instrument, tf, allow_fallback=False)
            except Exception:
                intel = None
            if not isinstance(intel, dict):
                continue
            # Production cache entries carry a timeframe-qualified capture epoch.
            # Do not let an old exact-timeframe bundle grade a new candidate. Small
            # unit-test stubs without cache clocks remain usable; the live Scanner
            # always initializes _intel_cache_time and therefore fails closed.
            cache_times = getattr(scanner, "_intel_cache_time", None)
            if isinstance(cache_times, dict):
                key_builder = getattr(scanner, "_timeframe_intel_key", None)
                key = (
                    key_builder(instrument, tf)
                    if callable(key_builder)
                    else f"{str(instrument or '').upper()}_{tf}"
                )
                captured = cache_times.get(key)
                if captured is None:
                    captured = intel.get("as_of")
                try:
                    captured_ts = float(captured)
                    ttl_getter = getattr(scanner, "_intel_analysis_ttl_seconds", None)
                    ttl = float(ttl_getter() if callable(ttl_getter) else getattr(settings, "intelligence_analysis_ttl_seconds", 30.0))
                    age = time.time() - captured_ts
                except (TypeError, ValueError, OverflowError):
                    continue
                if not (0.0 <= age <= max(0.0, ttl)):
                    continue
            aggregate = intel.get("aggregate") if isinstance(intel.get("aggregate"), dict) else {}
            regime_block = intel.get("regime") if isinstance(intel.get("regime"), dict) else {}
            contexts[tf] = {
                "intel_result": intel,
                # CANONICAL, like every other producer of this value.
                #
                # This read `aggregate.get("score")` raw. That field is always
                # legacy-scaled (-100..100) and was then handed to
                # normalize_intelligence_score, which infers the scale from MAGNITUDE:
                # a legacy 0.9 -- all but neutral -- is under the `abs(score) > 1.0`
                # threshold, never divided, and reads as canonical 0.9, near-maximum
                # conviction. Only SMALL readings were affected, which is what made it
                # quiet; the loud cases (70 -> 0.70) looked perfectly correct.
                #
                # It was left unfixed on the reasoning that storing the canonical value
                # would diverge from the flag-off path "which hands the caller's LEGACY
                # intel_score to the same consumers". That premise was simply wrong.
                # scanner.py:10414 computes the caller's value as
                # `aggregate_intelligence_score(intel_result["aggregate"])`, so it is
                # already canonical and is passed to process_best_signal unchanged.
                # Storing canonical here CONVERGES the two paths rather than splitting
                # them, and normalize_intelligence_score downstream becomes the no-op it
                # is documented to be on values that are already canonical.
                "intel_score": aggregate_intelligence_score(aggregate),
                "regime": regime_block.get("regime") or regime_block.get("hard_label") or "",
            }
        # Empty means "feature enabled, but no own-timeframe evidence". Returning None
        # here made the grading loop interpret a total cache miss as legacy mode and
        # silently borrow the caller's 5m bundle, defeating fail-closed semantics.
        return contexts

    @staticmethod
    def _direction_mode_allows(direction: str, settings) -> Tuple[bool, str]:
        """Apply the configured strategy-side constraint to every entry path.

        `strategy_direction_mode` was declared, persisted and displayed but never
        enforced, so selecting LONG_ONLY / SHORT_ONLY did not actually restrict which
        side could trade. Default LONG_SHORT allows both, so this is a no-op unless the
        operator has deliberately narrowed the side.
        """
        mode = str(getattr(settings, "strategy_direction_mode", "LONG_SHORT") or "LONG_SHORT").upper()
        side = str(direction or "").upper()
        if side in {"BUY", "LONG"}:
            side = "LONG"
        elif side in {"SELL", "SHORT"}:
            side = "SHORT"
        if mode == "LONG_ONLY" and side == "SHORT":
            return False, "Strategy direction mode LONG_ONLY blocks SHORT entries"
        if mode == "SHORT_ONLY" and side == "LONG":
            return False, "Strategy direction mode SHORT_ONLY blocks LONG entries"
        return True, ""

    @staticmethod
    def _live_signal_candle_is_closed(signal_ts: datetime, timeframe: str, now: datetime) -> bool:
        """Return whether a signal's source candle has finalized."""
        tf_minutes = {"1min": 1, "5min": 5, "15min": 15}.get(str(timeframe or ""), 5)
        signal_check = signal_ts.replace(tzinfo=None) if getattr(signal_ts, "tzinfo", None) else signal_ts
        now_check = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
        return now_check >= signal_check + timedelta(minutes=tf_minutes)

    @staticmethod
    def _live_signal_is_new(
        signal_ts: datetime,
        signal_type: str,
        last_ts: datetime,
        last_identity,
    ) -> bool:
        """Return whether this exact timestamp/direction identity is still virgin.

        Union engines can legitimately retain both BUY and SELL at the same timestamp
        (different source families flipped on the same bar).  A single remembered
        identity therefore oscillates BUY -> SELL -> BUY on every scan and makes both
        arrows look new forever.  Accept the legacy scalar as well as the current
        collection so warm/partial scanner objects remain compatible.
        """
        identity = f"{signal_ts.isoformat()}|{signal_type}"
        identities = SignalProcessor._live_signal_identities(last_identity)
        return signal_ts > last_ts or (signal_ts == last_ts and identity not in identities)

    @staticmethod
    def _live_signal_identities(value) -> set[str]:
        """Normalize legacy scalar and current collection identity state."""
        if value is None:
            return set()
        if isinstance(value, str):
            return {value} if value else set()
        try:
            return {str(item) for item in value if str(item)}
        except TypeError:
            text = str(value)
            return {text} if text else set()

    @classmethod
    def _remember_live_signal_identity(
        cls,
        scanner,
        dedup_key: str,
        signal_ts: datetime,
        signal_identity: str,
    ) -> None:
        """Remember every direction seen at the lane's latest timestamp.

        The set is naturally bounded: when the lane advances to a newer candle it is
        replaced, and for the current union engines it contains at most BUY and SELL.
        """
        if not hasattr(scanner, "_last_signal_time"):
            scanner._last_signal_time = {}
        if not hasattr(scanner, "_last_signal_identity"):
            scanner._last_signal_identity = {}

        signal_check = (
            signal_ts.replace(tzinfo=None)
            if getattr(signal_ts, "tzinfo", None)
            else signal_ts
        )
        current_ts = scanner._last_signal_time.get(dedup_key) or datetime.min
        current_check = (
            current_ts.replace(tzinfo=None)
            if getattr(current_ts, "tzinfo", None)
            else current_ts
        )
        identities = cls._live_signal_identities(
            scanner._last_signal_identity.get(dedup_key)
        )
        if signal_check > current_check:
            identities.clear()
        if signal_check >= current_check:
            identities.add(signal_identity)
            scanner._last_signal_identity[dedup_key] = identities
            scanner._last_signal_time[dedup_key] = max(current_check, signal_check)

    @classmethod
    def _forget_live_signal_identity(
        cls,
        scanner,
        dedup_key: str,
        signal_ts: datetime,
        signal_identity: str,
    ) -> bool:
        """Withdraw only the repainted identity without reopening its opposite peer."""
        identity_store = getattr(scanner, "_last_signal_identity", {})
        identities = cls._live_signal_identities(identity_store.get(dedup_key))
        if signal_identity not in identities:
            return False

        identities.discard(signal_identity)
        if identities:
            identity_store[dedup_key] = identities
            return True

        identity_store.pop(dedup_key, None)
        signal_check = (
            signal_ts.replace(tzinfo=None)
            if getattr(signal_ts, "tzinfo", None)
            else signal_ts
        )
        last_times = getattr(scanner, "_last_signal_time", {})
        current_ts = last_times.get(dedup_key) or datetime.min
        current_check = (
            current_ts.replace(tzinfo=None)
            if getattr(current_ts, "tzinfo", None)
            else current_ts
        )
        if current_check == signal_check:
            last_times[dedup_key] = signal_check - timedelta(microseconds=1)
        return True

    @classmethod
    def _live_signal_recheck_due(
        cls,
        signal_ts: datetime,
        timeframe: str,
        now: datetime,
        eligible: bool,
        pending: bool,
        last_recheck: datetime,
        interval_seconds: float = 5.0,
    ) -> bool:
        """Re-evaluate a rejected arrow while forming and once after candle close."""
        if not eligible or pending:
            return False
        now_check = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
        last_check = (
            last_recheck.replace(tzinfo=None)
            if getattr(last_recheck, "tzinfo", None)
            else last_recheck
        )
        return (now_check - last_check).total_seconds() >= max(
            1.0,
            float(interval_seconds or 5.0),
        )

    @staticmethod
    def _futures_stop_distance(
        natural_stop_distance: float,
        reference_price: float,
        futures_sl_pct: float,
        regime: str,
    ) -> float:
        """Cap a natural FUT stop without ever widening it."""
        natural_stop = max(0.0, float(natural_stop_distance or 0.0))
        effective_pct = futures_stop_tolerance_pct(futures_sl_pct, regime)
        regime_cap = max(0.0, float(reference_price or 0.0)) * (effective_pct / 100.0)
        return min(natural_stop, regime_cap)

    @staticmethod
    def _futures_stop_distance_rejection(
        natural_stop_distance: float,
        spot: float,
        futures_sl_pct: float,
        safety_multiplier: float = 1.5,
        regime: str = "UNKNOWN",
    ) -> Optional[str]:
        """Explain when a natural FUT stop exceeds the hard safety envelope.

        The traded stop is separately capped at the regime-selected tolerance,
        with futures_sl_pct as the absolute ceiling (see caller), so
        this gate only rejects when the NATURAL ATR stop is more than
        `safety_multiplier` x that cap — i.e. our forced stop would sit so far
        inside the ATR that whipsaw risk is high. The multiplier is configurable
        (`futures_sl_safety_multiplier`) because a too-tight value rejects every
        FUT entry in higher-volatility regimes where ATR stops naturally run wide.
        """
        mult = max(1.0, float(safety_multiplier or 1.5))
        effective_pct = futures_stop_tolerance_pct(futures_sl_pct, regime)
        allowed_stop = float(spot or 0.0) * (effective_pct / 100.0)
        hard_limit = allowed_stop * mult
        natural_stop = float(natural_stop_distance or 0.0)
        if hard_limit > 0.0 and natural_stop > hard_limit:
            return (
                f"Natural FUT stop {natural_stop:.2f} pts exceeds "
                f"{hard_limit:.2f} pts safety limit "
                f"({str(regime or 'UNKNOWN').upper()} tolerance {effective_pct:.3f}%, "
                f"configured cap {float(futures_sl_pct or 0.0):.3f}% x {mult:g})"
            )
        return None

    @staticmethod
    def _contract_ltp(scanner, exchange: str, symbol: str, token: str) -> float:
        """Use the same quote abstraction for option and futures contracts."""
        if not scanner or not getattr(scanner, "data", None) or not symbol or not token:
            return 0.0
        try:
            price = scanner.data.get_ltp(exchange, symbol, token)
            return float(price or 0.0)
        except Exception as exc:
            logger.debug(f"Contract LTP fetch failed for {symbol}: {exc}")
            return 0.0

    @staticmethod
    def _grade_rank(grade: str) -> int:
        return grade_rank(grade)

    @staticmethod
    def _grade_min_confidence(grade: str) -> float:
        return grade_min_confidence(grade)

    @staticmethod
    def _option_signed_delta(spot, strike, option_type, intel_result, dte, fallback):
        """Black-Scholes signed delta for the ACTUAL strike (fixes ITM/OTM options
        being assigned the flat ATM ~0.5 delta). Falls back to the ATM/±0.5 estimate."""
        try:
            from intelligence.greeks_engine import GreeksEngine
            greeks = (intel_result or {}).get("greeks", {}) or {}
            leg = "call" if str(option_type or "").upper() == "CE" else "put"
            iv = float(greeks.get(leg, {}).get("iv", 0.0) or 0.0)
            if iv > 1.5:          # greeks store IV as a percentage
                iv = iv / 100.0
            if iv <= 0:
                iv = 0.15
            days = max(float(dte or 0.0), 0.25)   # keep 0DTE meaningful
            d = float(GreeksEngine().calculate(float(spot), float(strike), days, iv, leg).get("delta", 0.0))
            if leg == "put" and d > 0:
                d = -d
            if abs(d) < 0.05:
                return fallback
            return d
        except Exception:
            return fallback

    @staticmethod
    def _power_move_confirmed(*, volume_ratio, range_atr_ratio, close_location, adx_value, intel_score, trend_15m_agrees, settings) -> bool:
        return power_move_confirmed(
            volume_ratio=volume_ratio,
            range_atr_ratio=range_atr_ratio,
            close_location=close_location,
            adx_value=adx_value,
            intel_score=intel_score,
            trend_15m_agrees=trend_15m_agrees,
            settings=settings,
        )

    @staticmethod
    def _volume_spike_entry_confirmed(
        *,
        timeframe,
        volume_ratio,
        range_atr_ratio,
        close_location,
        adx_value=0.0,
        intel_score=0.0,
        power_move=False,
        settings,
        signal_type: str = "",
        depth_usable: bool = False,
        ofr: float = 0.0,
        volume_basis: str = "TRADED",
        surge_percentile: float = -1.0,
        instrument: str = "",
    ) -> bool:
        return volume_spike_entry_confirmed(
            timeframe=timeframe,
            volume_ratio=volume_ratio,
            range_atr_ratio=range_atr_ratio,
            close_location=close_location,
            adx_value=adx_value,
            intel_score=intel_score,
            power_move=power_move,
            settings=settings,
            signal_type=signal_type,
            depth_usable=depth_usable,
            ofr=ofr,
            volume_basis=volume_basis,
            surge_percentile=surge_percentile,
            instrument=instrument,
        )

    @staticmethod
    def _volume_spike_requirement_text(settings, instrument: str = "") -> str:
        """Human-readable fast-entry threshold using the unit actually measured."""
        traded_floor = float(volume_spike_min_ratio(settings, instrument) or 0.0)
        if _setting_enabled(
            getattr(settings, "one_min_scalping_high_burst_only", True), True
        ):
            traded_floor = max(
                traded_floor,
                float(
                    getattr(
                        settings,
                        "volume_spike_entry_strong_1min_min_volume_ratio",
                        traded_floor,
                    )
                    or traded_floor
                ),
            )
        tick_floor = float(
            getattr(settings, "surge_tick_percentile_floor", 97.0) or 97.0
        )
        return (
            f"traded volume >= {traded_floor:.2f}x or "
            f"tick activity >= p{tick_floor:g} (with range/close and burst context)"
        )

    @staticmethod
    def _volume_spike_metric_text(
        metrics: Optional[Dict[str, Any]],
        settings,
        instrument: str = "",
        timeframe: str = "",
    ) -> str:
        """Describe spike evidence without calling tick activity traded volume."""
        evidence = dict(metrics or {})
        basis = str(evidence.get("volume_basis") or "TRADED").upper()
        if basis == "TICK_ACTIVITY":
            percentile = float(evidence.get("surge_percentile", -1.0) or -1.0)
            floor = float(
                getattr(settings, "surge_tick_percentile_floor", 97.0) or 97.0
            )
            return f"tick activity p{percentile:.1f} (floor p{floor:g})"
        ratio = float(evidence.get("volume_ratio", 0.0) or 0.0)
        floor = float(volume_spike_min_ratio(settings, instrument) or 0.0)
        if str(timeframe or "").lower() == "1min" and _setting_enabled(
            getattr(settings, "one_min_scalping_high_burst_only", True), True
        ):
            floor = max(
                floor,
                float(
                    getattr(
                        settings,
                        "volume_spike_entry_strong_1min_min_volume_ratio",
                        floor,
                    )
                    or floor
                ),
            )
        return f"traded volume {ratio:.2f}x (floor {floor:.2f}x)"

    @staticmethod
    def _recheckable_rejection_status(status_msg: str) -> bool:
        """Return whether a forming rejection may improve before candle close."""
        status = str(status_msg or "")
        return status in {
            "REJECTED - Quality Gate",
            "REJECTED - 5min Quality",
            "REJECTED - Low Confidence",
            "REJECTED - 5min Choppy Confirmation",
            "REJECTED - Opt Gate",
            "REJECTED - FUT Stop Distance",
            "REJECTED - Weak 1min",
            # An opposite signal exits the existing trade THIS cycle but is blocked
            # from entering in the same cycle. It must stay recheck-eligible so the
            # NEXT cycle re-evaluates it (the existing trade now gone) and the
            # reversal entry can fire — otherwise the dedup lock orphans it forever.
            "REJECTED - Opposite Signal Wait",
            # Same statement, other path. quad_prime — the SHIPPED engine — refuses the
            # reverse entry while a live-book trade is open on the far side, and it did
            # so with a bare _emit_signal_decision + `return []`, which arms nothing. The
            # dedup lock is already advanced by then, so the flip was presented once and
            # dropped. 56 flips died that way on four sessions (2026-07-09 x24,
            # 07-31 x16, 08-19 x13, 07-10 x3); on 2026-07-09 one NIFTY trade refused
            # every SELL flip from 10:35 onward and no reversal was ever taken.
            "REJECTED - Existing Opposite Trade",
        } or status.startswith("REJECTED - 5Min ")

    @staticmethod
    def _is_live_book_trade(trade) -> bool:
        """True when an open/closed row is real book (not ghost/hist/replay contamination)."""
        if trade is None:
            return False
        if bool(getattr(trade, "is_ghost", False)):
            return False
        tid = str(getattr(trade, "id", "") or "")
        if tid.startswith("H_") or tid.startswith("EOD_"):
            return False
        if str(getattr(trade, "trading_symbol", "") or "").upper() == "FUT_SHADOW":
            return False
        prov = str(getattr(trade, "data_provenance", "") or "").upper()
        if prov in {
            "HISTORICAL",
            "HISTORY",
            "HIST",
            "BACKTEST",
            "REPLAY",
            "SIM",
            "SIMULATION",
            "PAPER_HIST",
        }:
            return False
        return True

    @staticmethod
    def _suppress_1min_no_spike_tape(tf, volume_spike_entry, settings) -> bool:
        """Whether to keep a 1min signal off the live tape because it never cleared
        its configured traded-volume or tick-percentile first filter."""
        return bool(
            str(tf or "") == "1min"
            and not bool(volume_spike_entry)
            and _setting_enabled(getattr(settings, "live_1min_log_only_volume_spike", True), True)
        )

    @staticmethod
    def _should_log_tf_select_diagnostic(best_tf, best_sig, scored, settings, trade_involved: bool = False) -> bool:
        """Log TF selection only when it is useful, not every ordinary 1min churn tick."""
        if trade_involved:
            return True
        if str(best_tf or "") != "1min":
            return len(scored or []) > 1 or str(best_tf or "") == "15min"
        if bool(getattr(best_sig, "volume_spike_entry", False)):
            return True
        if bool(getattr(best_sig, "power_move", False)):
            return True
        return not _setting_enabled(getattr(settings, "live_1min_log_only_volume_spike", True), True)

    @staticmethod
    def _entry_admissible_raw_candidates(
        candidates_raw,
        *,
        reversion_window_open: bool = False,
    ):
        """Keep 1min tracking rows from competing for entry unless spike or reversion window.

        - Continuous 1M SPIKE path: requires volume_spike_entry.
        - POST-WIN / distress SNAPBACK path: may admit 1min without spike while the
          reversion window is open (REV-SCLP ON, 1M-SCALP may be OFF).
        """
        allow_reversion_1min = bool(reversion_window_open)
        return [
            c
            for c in list(candidates_raw or [])
            if str(c.get("tf", "")) != "1min"
            or bool(getattr(c.get("sig"), "volume_spike_entry", False))
            or allow_reversion_1min
        ]

    @staticmethod
    def _evaluate_reversion_scalp(
        scanner,
        settings,
        *,
        instrument: str,
        best_tf: str,
        best_sig,
    ):
        """Decide POST-WIN 3M SNAPBACK eligibility for this 1min candidate.

        Window arms only after 5min/15min exits (not 1min scalps). Pure snapback
        does not require the full configured spike threshold but MUST pass bare-min tape data
        (volume/range/close) so entries are data-backed, not hunch-only.

        Returns (is_reversion_scalp: bool, meta: dict).
        """
        meta = {
            "minutes_since": 0.0,
            "view": "",
            "reason": "",
            "move_pct": 0.0,
            "source": "",
            "tape_ok": False,
        }
        if not _setting_enabled(getattr(settings, "post_exit_reversion_enabled", False), False):
            return False, meta
        if str(best_tf or "") != "1min" or best_sig is None:
            return False, meta
        base = str(instrument or "").split()[0].upper()
        _rev_info = (getattr(scanner, "_last_major_exits", {}) or {}).get(base)
        if not isinstance(_rev_info, dict):
            _rev_info = (getattr(scanner, "_last_major_exits", {}) or {}).get(instrument)
        last_exit_time = (
            _rev_info.get("time") if isinstance(_rev_info, dict) else None
        )
        last_exit_view = (
            str(_rev_info.get("direction") or "").upper()
            if isinstance(_rev_info, dict)
            else ""
        )
        _rev_reason = (
            str(_rev_info.get("reason") or "")
            if isinstance(_rev_info, dict)
            else ""
        )
        _rev_source = (
            str(_rev_info.get("source") or "")
            if isinstance(_rev_info, dict)
            else ""
        )
        stored_move = 0.0
        if isinstance(_rev_info, dict):
            try:
                stored_move = float(_rev_info.get("move_pct", 0.0) or 0.0)
            except Exception:
                stored_move = 0.0
        if last_exit_time is None and getattr(
            scanner, "_last_major_exit_instrument", None
        ) == base:
            last_exit_time = getattr(scanner, "_last_major_exit_time", None)
        if last_exit_time is None:
            return False, meta
        _now_ist = datetime.now(IST).replace(tzinfo=None)
        if getattr(last_exit_time, "tzinfo", None) is not None:
            last_exit_time = last_exit_time.astimezone(IST).replace(tzinfo=None)
        _rev_window = float(
            getattr(settings, "post_exit_reversion_window_minutes", 3.0) or 3.0
        )
        minutes_since_exit = (_now_ist - last_exit_time).total_seconds() / 60.0
        counter_trend = True
        if last_exit_view in {"LONG", "SHORT"}:
            _expected_rev = "SELL" if last_exit_view == "LONG" else "BUY"
            counter_trend = str(best_sig.signal_type).upper() == _expected_rev
        if not (0.0 <= minutes_since_exit <= max(0.5, _rev_window) and counter_trend):
            return False, meta

        # Size-of-event: 0.40%+/5m OR 0.50%+/10m (stored at arm, else live).
        qualifies = True
        move_info: dict = {"move_pct": float(stored_move)}
        if callable(getattr(scanner, "_index_event_move_qualifies", None)):
            try:
                qualifies, move_info = scanner._index_event_move_qualifies(
                    base,
                    as_of=_now_ist,
                    stored=_rev_info if isinstance(_rev_info, dict) else None,
                )
            except Exception:
                qualifies = stored_move >= float(
                    getattr(settings, "post_exit_reversion_fast_move_pct", 0.40) or 0.40
                )
                move_info = {"move_pct": float(stored_move)}
        elif stored_move > 0:
            thr = float(getattr(settings, "post_exit_reversion_fast_move_pct", 0.40) or 0.40)
            qualifies = thr <= 0 or stored_move + 1e-9 >= thr
        meta.update(
            {
                "minutes_since": float(minutes_since_exit),
                "view": last_exit_view,
                "reason": _rev_reason,
                "move_pct": float(move_info.get("move_pct", stored_move) or 0.0),
                "move_5m_pct": float(move_info.get("move_5m_pct", 0.0) or 0.0),
                "move_10m_pct": float(move_info.get("move_10m_pct", 0.0) or 0.0),
                "source": _rev_source,
                "band": str(move_info.get("band") or ""),
                "source_tf": str(
                    (_rev_info or {}).get("source_tf") if isinstance(_rev_info, dict) else ""
                ),
            }
        )
        if not qualifies:
            logger.info(
                f"🔄 [REVERSION SCALP] {base} skipped: "
                f"5m={meta.get('move_5m_pct', 0):.2f}% / "
                f"10m={meta.get('move_10m_pct', 0):.2f}% "
                f"(need 0.40%/5m OR 0.50%/10m)"
            )
            return False, meta

        # Bare-min tape confirm: full SPIKE already qualifies; pure SNAPBACK needs
        # directional thrust floors (configurable, below the full spike gate) so we do not
        # fire on a naked UT1 flip with zero volume/range evidence.
        if bool(getattr(best_sig, "volume_spike_entry", False)):
            meta["tape_ok"] = True
            meta["tape_path"] = "volume_spike"
            return True, meta

        min_vol = float(getattr(settings, "post_exit_reversion_min_volume_ratio", 1.5) or 1.5)
        min_rng = float(getattr(settings, "post_exit_reversion_min_range_atr_ratio", 1.0) or 1.0)
        min_close = float(getattr(settings, "post_exit_reversion_min_close_location", 0.55) or 0.55)
        vol_r = rng_r = close_loc = 0.0
        thrust = None
        try:
            thrust_fn = getattr(scanner, "_candle_thrust_metrics", None)
            if callable(thrust_fn):
                thrust = thrust_fn(
                    base,
                    "1min",
                    str(best_sig.signal_type or ""),
                    as_of=getattr(best_sig, "timestamp", None) or _now_ist,
                )
        except Exception:
            thrust = None
        if isinstance(thrust, dict):
            try:
                vol_r = float(thrust.get("volume_ratio", 0.0) or 0.0)
                rng_r = float(thrust.get("range_atr_ratio", 0.0) or 0.0)
                close_loc = float(thrust.get("close_location", 0.0) or 0.0)
            except Exception:
                vol_r = rng_r = close_loc = 0.0
        tape_ok = (
            vol_r + 1e-12 >= min_vol
            and rng_r + 1e-12 >= min_rng
            and close_loc + 1e-12 >= min_close
        )
        meta.update(
            {
                "tape_ok": bool(tape_ok),
                "tape_path": "bare_min" if tape_ok else "fail",
                "tape_vol": float(vol_r),
                "tape_range": float(rng_r),
                "tape_close": float(close_loc),
            }
        )
        if not tape_ok:
            logger.info(
                f"🔄 [REVERSION SCALP] {base} skipped: bare-min tape fail "
                f"vol={vol_r:.2f}x (need>={min_vol:.2f}) "
                f"rng={rng_r:.2f}x (need>={min_rng:.2f}) "
                f"close={close_loc:.0%} (need>={min_close:.0%})"
            )
            return False, meta
        return True, meta

    @staticmethod
    def _select_cascade_wave_scored_rows(
        scored,
        settings,
        scanner_engine_mode: str = "",
        instrument: str = "",
    ):
        """Pick multi-TF cascade stack.

        Core (NIFTY/BN/SENSEX): primary 1m/5m/15m + dual 5m/15m lane-B.
        MIDCAP: solo best 2 of 1/5/15; dual = best 2 main + best 1 dual of 5/15 (2+1).
        """
        if not scored:
            return []
        parts = str(instrument or "").split()
        inst = parts[0].upper() if parts else ""
        core = {
            str(x or "").upper()
            for x in (getattr(settings, "cascade_multi_tf_core_indices", None) or [
                "NIFTY", "BANKNIFTY", "SENSEX"
            ])
        }
        # Empty instrument → core full stack (default / unit tests)
        is_midcap = bool(inst) and (inst == "MIDCPNIFTY" or inst not in core)

        primary_by_tf = {}
        dual_by_tf = {}
        for row in scored:
            score, sig, tf, grade, conf, reasons, after = row
            lane = SignalProcessor._signal_source_lane(sig, scanner_engine_mode)
            tf_key = str(tf or "")
            if lane == "B" and tf_key in {"5min", "15min"}:
                prev = dual_by_tf.get(tf_key)
                if prev is None or score > prev[0]:
                    dual_by_tf[tf_key] = row
                continue
            if tf_key in {"1min", "5min", "15min"}:
                prev = primary_by_tf.get(tf_key)
                if prev is None or score > prev[0]:
                    primary_by_tf[tf_key] = row

        if is_midcap:
            solo_max = max(1, int(getattr(settings, "cascade_midcap_solo_max_tf", 2) or 2))
            dual_main_max = max(1, int(getattr(settings, "cascade_midcap_dual_main_max", 2) or 2))
            dual_addon_max = max(0, int(getattr(settings, "cascade_midcap_dual_addon_max", 1) or 1))
            main_ranked = sorted(primary_by_tf.values(), key=lambda r: r[0], reverse=True)
            if dual_by_tf:
                main = main_ranked[:dual_main_max]
                dual_ranked = sorted(dual_by_tf.values(), key=lambda r: r[0], reverse=True)
                dual = dual_ranked[:dual_addon_max]
                selected = main + dual
                selected = SignalProcessor._apply_scored_dual_clone_reduction(
                    selected, settings
                )
            else:
                selected = main_ranked[:solo_max]
            return selected if selected else [scored[0]]

        primary_max = max(1, int(getattr(settings, "cascade_primary_max_tf", 3) or 3))
        dual_max = max(0, int(getattr(settings, "cascade_dual_addon_max", 2) or 2))
        order = ["15min", "5min", "1min"]
        primary = [primary_by_tf[tf] for tf in order if tf in primary_by_tf][:primary_max]
        dual = [dual_by_tf[tf] for tf in ("5min", "15min") if tf in dual_by_tf][:dual_max]
        selected = primary + dual
        # Dual clone reduction: same TF main+B within 1m → keep first only
        selected = SignalProcessor._apply_scored_dual_clone_reduction(selected, settings)
        return selected if selected else [scored[0]]

    @staticmethod
    def _apply_scored_dual_clone_reduction(selected_rows, settings):
        """Same-TF dual engine rows within 1m → keep earliest only (first wins).

        selected_rows: list of scored tuples (score, sig, tf, grade, conf, reasons, after).
        """
        if not selected_rows:
            return selected_rows
        if not _setting_enabled(
            getattr(settings, "dual_clone_reduction_enabled", True), True
        ):
            return selected_rows
        win = max(
            0.0,
            float(getattr(settings, "dual_clone_reduction_window_seconds", 60) or 60),
        )

        def _ts(row):
            sig = row[1]
            t = getattr(sig, "timestamp", None)
            if t is None:
                return None
            if getattr(t, "tzinfo", None) is not None:
                try:
                    return t.replace(tzinfo=None)
                except Exception:
                    return t
            return t

        by_tf = {}
        for row in selected_rows:
            tf = str(row[2] or "")
            by_tf.setdefault(tf, []).append(row)

        keep = set()
        for tf, rows in by_tf.items():
            if len(rows) <= 1:
                keep.add(id(rows[0]))
                continue
            ordered = sorted(
                rows,
                key=lambda r: (_ts(r) is None, _ts(r) or datetime.max),
            )
            first = ordered[0]
            keep.add(id(first))
            first_ts = _ts(first)
            for other in ordered[1:]:
                other_ts = _ts(other)
                if first_ts is None or other_ts is None:
                    # missing stamps + same TF → clone, drop
                    continue
                if abs((first_ts - other_ts).total_seconds()) <= win:
                    continue  # drop clone
                keep.add(id(other))
        return [r for r in selected_rows if id(r) in keep]

    def _clone_cascade_tf_sibling(
        self,
        winner: "TradeCandidate",
        *,
        sig,
        tf: str,
        grade: str,
        confidence: float,
        reasons,
        scanner_engine_mode: str,
    ) -> Optional["TradeCandidate"]:
        """Materialize an extra cascade TF/lane entry from an already-approved winner.

        Reuses winner book (inst_type/lots/stops policy already cascade-sized) but
        retargets TF, signal stamp, grade, and source lane so concurrency + mixed
        book can keep the multi-TF stack.
        """
        try:
            sibling = copy.copy(winner)
        except Exception:
            return None
        direction = "LONG" if str(getattr(sig, "signal_type", "") or "").upper() == "BUY" else "SHORT"
        if direction != str(getattr(winner, "direction", "") or "").upper():
            return None
        sibling.timeframe = str(tf or winner.timeframe)
        sibling.grade = grade or winner.grade
        sibling.confidence = float(confidence or 0.0)
        sibling.score = float(confidence or 0.0)
        sibling.signal_timestamp = getattr(sig, "timestamp", None) or winner.signal_timestamp
        sibling.reasons = list(reasons or []) + [
            f"Cascade multi-TF stack sibling ({sibling.timeframe})"
        ]
        sibling.one_way_cascade_entry = True
        src_engine = str(getattr(sig, "source_engine", "") or "").lower()
        src_engines = [
            str(src).lower()
            for src in (getattr(sig, "source_engines", None) or [])
            if str(src or "").strip()
        ]
        lane = self._signal_source_lane(sig, scanner_engine_mode)
        setattr(sibling, "source_engine", src_engine or getattr(winner, "source_engine", ""))
        setattr(sibling, "source_engines", src_engines or list(getattr(winner, "source_engines", []) or []))
        setattr(sibling, "source_lane", lane)
        setattr(sibling, "source_signal_type", getattr(sig, "signal_type", ""))
        setattr(sibling, "source_signal_price", float(getattr(sig, "price", 0.0) or 0.0))
        setattr(sibling, "source_signal_bar_index", int(getattr(sig, "bar_index", -1) or -1))
        setattr(sibling, "dual_lane_entry", lane == "B")
        if lane == "B":
            try:
                fraction = max(
                    0.05,
                    min(1.0, float(getattr(get_settings(), "dual_lane_size_fraction", 0.65) or 0.65)),
                )
                base_lots = max(1, int(getattr(winner, "lots", 1) or 1))
                sibling.lots = max(1, int(base_lots * fraction))
            except Exception:
                pass
        # Spot/signal price for FUT siblings: use signal price when available
        if str(getattr(sibling, "inst_type", "") or "").upper() == "FUT":
            px = float(getattr(sig, "price", 0.0) or 0.0)
            if px > 0:
                risk = abs(float(winner.price or 0.0) - float(winner.stop or 0.0))
                rr = float(getattr(winner, "rr", 1.5) or 1.5)
                sibling.price = px
                if direction == "LONG":
                    sibling.stop = px - risk if risk > 0 else float(winner.stop or 0.0)
                    sibling.target = px + risk * rr if risk > 0 else float(winner.target or 0.0)
                else:
                    sibling.stop = px + risk if risk > 0 else float(winner.stop or 0.0)
                    sibling.target = px - risk * rr if risk > 0 else float(winner.target or 0.0)
                sibling.initial_stop = float(sibling.stop or 0.0)
        return sibling

    def _emit_rare_dual_tf_siblings(
        self,
        winner: "TradeCandidate",
        scored,
        settings,
        scanner_engine_mode: str,
    ) -> List["TradeCandidate"]:
        """Rare dual fill: best 1m (spike/snapback) + best 5m/15m when both excellent.

        One sibling max. Prefer OPT for the second leg so we never open 2x FUT
        on the same index. Cascade multi-TF is separate (already stacks more).
        """
        if not _setting_enabled(getattr(settings, "rare_dual_tf_entry_enabled", True), True):
            return []
        if bool(getattr(winner, "one_way_cascade_entry", False)):
            return []  # cascade path owns multi-TF stacking
        min_grade = str(getattr(settings, "rare_dual_tf_min_grade", "A") or "A").upper()
        min_conf = float(getattr(settings, "rare_dual_tf_min_confidence", 0.70) or 0.70)
        min_rank = self._grade_rank(min_grade)
        win_tf = str(getattr(winner, "timeframe", "") or "")
        win_dir = str(getattr(winner, "direction", "") or "").upper()
        win_grade = str(getattr(winner, "grade", "") or "")
        win_conf = float(getattr(winner, "confidence", 0.0) or 0.0)
        if self._grade_rank(win_grade) < min_rank or win_conf + 1e-9 < min_conf:
            return []
        # Need a fast leg (1m spike/snapback) and a structure leg (5/15).
        win_is_fast = win_tf == "1min" and (
            bool(getattr(winner, "volume_spike_entry", False))
            or bool(getattr(winner, "is_reversion_scalp", False))
            or bool(getattr(winner, "market_ltp_shock_entry", False))
        )
        win_is_structure = win_tf in {"5min", "15min"}
        if not (win_is_fast or win_is_structure):
            return []

        best_row = None
        best_score = -1.0
        for row in list(scored or []):
            score, sig, tf, grade, conf, reasons, _after = row
            tf_s = str(tf or "")
            if tf_s == win_tf:
                continue
            sig_dir = (
                "LONG"
                if str(getattr(sig, "signal_type", "") or "").upper() == "BUY"
                else "SHORT"
            )
            if sig_dir != win_dir:
                continue
            if self._grade_rank(grade) < min_rank:
                continue
            if float(conf or 0.0) + 1e-9 < min_conf:
                continue
            if win_is_fast and tf_s not in {"5min", "15min"}:
                continue
            if win_is_structure:
                if tf_s != "1min":
                    continue
                # Companion 1m must be spike / power / shock quality.
                if not (
                    bool(getattr(sig, "volume_spike_entry", False))
                    or bool(getattr(sig, "market_ltp_shock_entry", False))
                    or bool(getattr(sig, "power_move", False))
                ):
                    continue
            if float(score or 0.0) > best_score:
                best_score = float(score or 0.0)
                best_row = row
        if best_row is None:
            return []
        _sc, sig, tf, grade, conf, reasons, _af = best_row
        sib = self._clone_cascade_tf_sibling(
            winner,
            sig=sig,
            tf=str(tf),
            grade=str(grade or ""),
            confidence=float(conf or 0.0),
            reasons=list(reasons or []),
            scanner_engine_mode=scanner_engine_mode,
        )
        if sib is None:
            return []
        # Never dual FUT on same index — second leg becomes OPT when preferred.
        prefer_opt = _setting_enabled(
            getattr(settings, "rare_dual_tf_prefer_opt_second", True), True
        )
        win_type = str(getattr(winner, "inst_type", "FUT") or "FUT").upper()
        if prefer_opt or win_type == "FUT":
            if str(getattr(sib, "inst_type", "") or "").upper() == "FUT":
                # Mark for coordinator; force OPT book on sibling.
                sib.inst_type = "OPT"
                if not str(getattr(sib, "option_type", "") or ""):
                    sib.option_type = "CE" if win_dir == "LONG" else "PE"
        setattr(sib, "rare_dual_tf_entry", True)
        setattr(sib, "one_way_cascade_entry", False)
        # Strip cascade-only reason text if cloned from cascade helper.
        cleaned = [
            r
            for r in list(sib.reasons or [])
            if "Cascade multi-TF stack sibling" not in str(r)
        ]
        sib.reasons = cleaned + [
            f"Rare dual-TF fill: {win_tf}+{sib.timeframe} both ≥{min_grade}/{min_conf:.0%}"
        ]
        logger.info(
            f"🌟 Rare dual-TF: {getattr(winner, 'instrument', '')} "
            f"{win_tf}({win_grade}/{win_conf:.2f}) + "
            f"{sib.timeframe}({grade}/{float(conf or 0):.2f})"
        )
        return [sib]

    def _emit_cascade_tf_siblings(
        self,
        winner: "TradeCandidate",
        scored,
        settings,
        scanner_engine_mode: str,
    ) -> List["TradeCandidate"]:
        """After primary winner, emit remaining cascade multi-TF / dual siblings."""
        if not bool(getattr(winner, "one_way_cascade_entry", False)):
            return []
        if not _setting_enabled(getattr(settings, "cascade_multi_tf_stack_enabled", True), True):
            return []
        wave = self._select_cascade_wave_scored_rows(
            scored,
            settings,
            scanner_engine_mode,
            instrument=str(getattr(winner, "instrument", "") or ""),
        )
        if len(wave) <= 1:
            return []
        win_tf = str(getattr(winner, "timeframe", "") or "")
        win_lane = str(getattr(winner, "source_lane", "") or "").upper() or "A"
        siblings: List[TradeCandidate] = []
        seen = {(win_tf, win_lane)}
        for row in wave:
            _score, sig, tf, grade, conf, reasons, _after = row
            lane = self._signal_source_lane(sig, scanner_engine_mode) or "A"
            key = (str(tf or ""), str(lane or "A").upper())
            if key in seen:
                continue
            # Same TF primary as winner → already materialised
            if str(tf or "") == win_tf and key[1] == win_lane:
                continue
            sib = self._clone_cascade_tf_sibling(
                winner,
                sig=sig,
                tf=str(tf or ""),
                grade=grade,
                confidence=conf,
                reasons=reasons,
                scanner_engine_mode=scanner_engine_mode,
            )
            if sib is None:
                continue
            seen.add(key)
            siblings.append(sib)
        return siblings

    @staticmethod
    def _volume_spike_futures_stop_pct(configured_fut_sl_pct: float, settings) -> float:
        """Use the tighter of the spike cap and the user's global FUT stop setting."""
        spike_cap = max(
            0.0,
            float(
                getattr(settings, "volume_spike_entry_futures_sl_pct", 0.05)
                or 0.05
            ),
        )
        configured_cap = max(0.0, float(configured_fut_sl_pct or 0.0))
        return min(spike_cap, configured_cap)

    @staticmethod
    def _tight_futures_stop_pct(configured_fut_sl_pct: float, settings, attr_name: str, default: float) -> float:
        """Use the tighter of a special scalp cap and the user's global FUT cap."""
        tight_cap = max(0.0, float(getattr(settings, attr_name, default) or default))
        configured_cap = max(0.0, float(configured_fut_sl_pct or 0.0))
        return min(tight_cap, configured_cap)

    @staticmethod
    def _b_grade_entry_allowed(confidence, regime, *, trend_15m_agrees, adx_value, settings) -> bool:
        return b_grade_entry_allowed(
            confidence,
            regime,
            trend_15m_agrees=trend_15m_agrees,
            adx_value=adx_value,
            settings=settings,
        )

    @staticmethod
    def _is_choppy_regime(regime: str) -> bool:
        return is_choppy_regime(regime)

    @staticmethod
    def _relaxed_threshold(value: float, settings) -> float:
        return relaxed_threshold(value, settings)

    @staticmethod
    def _choppy_confidence_gate(settings) -> float:
        return choppy_confidence_gate(settings)

    @staticmethod
    def _dynamic_rr(grade: str, confidence: float, intel_score: float = 0.0, max_rr: Optional[float] = None, timeframe: str = "") -> float:
        return dynamic_rr(grade, confidence, intel_score, max_rr, timeframe)

    @staticmethod
    def _option_index_stop_distance(
        natural_stop_distance: float,
        est_premium: float,
        options_sl_pct: float,
        instrument_multiplier: float,
        settings,
    ) -> float:
        return option_index_stop_distance(
            natural_stop_distance,
            est_premium,
            options_sl_pct,
            instrument_multiplier,
            settings,
        )

    @staticmethod
    def _option_premium_stop_distance(
        live_trade_price: float,
        scanner,
        regime: str,
        timeframe: str = "",
        atr_premium_stop: float = 0.0,
        settings=None,
        intel_score: float = 0.0,
    ) -> float:
        try:
            opt_sl_pct = float(getattr(scanner, "options_sl_pct", getattr(get_settings(), "options_sl_pct", 15.0)) or 15.0)
        except Exception:
            opt_sl_pct = 15.0
        if is_choppy_regime(regime):
            opt_sl_pct = max(opt_sl_pct, 15.0)
        # RR, target and exit all flex with the intelligence score; the STOP was the one
        # of the four that ignored it entirely, moving only with regime and ATR. Strong
        # corroborating evidence earns a little more room to be right; weak evidence
        # gets less. Bounded hard so this can only ever nudge a stop the ATR and
        # per-timeframe rules already chose, never redefine it.
        #
        # Default OFF: this changes the risk on every option entry, which is the
        # operator's decision and not a defect being repaired.
        opt_sl_pct = _intelligence_stop_pct(opt_sl_pct, intel_score, settings)
        price = float(live_trade_price or 0.0)
        pct_stop = max(0.5, price * (opt_sl_pct / 100.0))

        # Timeframe- and ATR-aware stop: derive the premium stop from the signal's ATR
        # natural stop (already converted to premium points by the caller) bounded by
        # per-timeframe min/max %, so 5min stops sit tighter and 15min wider — driven by
        # real volatility rather than a flat %. options_sl_pct stays the hard ceiling.
        atr_stop = float(atr_premium_stop or 0.0)
        if atr_stop > 0.0 and price > 0.0:
            if settings is None:
                try:
                    settings = get_settings()
                except Exception:
                    settings = None
            if settings is not None and _setting_enabled(
                getattr(settings, "option_stop_tf_atr_aware", True), True
            ):
                tf = str(timeframe or "").lower()
                if tf in {"1min", "5min"}:
                    min_pct = float(getattr(settings, "option_stop_5min_min_pct", 4.0) or 4.0)
                    max_pct = float(getattr(settings, "option_stop_5min_max_pct", 10.0) or 10.0)
                else:
                    min_pct = float(getattr(settings, "option_stop_15min_min_pct", 6.0) or 6.0)
                    max_pct = float(getattr(settings, "option_stop_15min_max_pct", 15.0) or 15.0)
                # Never exceed the configured hard ceiling (options_sl_pct).
                max_pct = min(max_pct, opt_sl_pct)
                floor = price * (min_pct / 100.0)
                ceil = price * (max_pct / 100.0)
                stop = min(max(atr_stop, floor), ceil)
                return max(0.5, stop)
        return pct_stop

    def _option_grade_allowed(
        self,
        grade: str,
        confidence: float,
        regime: str,
        adx_value: float = 0.0,
        intel_score: float = 0.0,
        signal_score: float = 0.0,
        five_min_true_impulse: bool = False,
        timeframe: str = "",
    ) -> bool:
        return option_grade_allowed(
            grade,
            confidence,
            regime,
            mode=getattr(getattr(self, "scanner", None), "mode", ""),
            adx_value=adx_value,
            intel_score=intel_score,
            signal_score=signal_score,
            five_min_true_impulse=five_min_true_impulse,
            timeframe=timeframe,
        )

    def _passes_quality_gate(
        self,
        tf: str,
        grade: str,
        confidence: float,
        regime: str,
        inst_type: str,
        settings,
        adx_value: float = 0.0,
        trend_15m_agrees: bool = False,
        intel_score: float = 0.0,
        iv_percentile: Optional[float] = None,
        chain_quality: Optional[Dict] = None,
        option_premium_ok: Optional[bool] = None,
    ) -> bool:
        return passes_quality_gate(
            tf,
            grade,
            confidence,
            regime,
            inst_type,
            settings,
            mode=getattr(getattr(self, "scanner", None), "mode", ""),
            adx_value=adx_value,
            trend_15m_agrees=trend_15m_agrees,
            intel_score=intel_score,
            iv_percentile=iv_percentile,
            chain_quality=chain_quality,
            option_premium_ok=option_premium_ok,
        )

    @staticmethod
    def _trend_15m_agrees_at(mtf_result, signal_ts, signal_type: str) -> bool:
        result = getattr(mtf_result, "results_15min", None) or {}
        signals = result.get("signals", []) or []
        if not signals:
            state = result.get("state", {}) or {}
            position = int(state.get("position") or 0)
            expected = 1 if str(signal_type or "").upper() == "BUY" else -1
            return position == expected

        target_ts = signal_ts.replace(tzinfo=None) if getattr(signal_ts, "tzinfo", None) else signal_ts
        prior = []
        for signal in signals:
            ts = signal.timestamp.replace(tzinfo=None) if getattr(signal.timestamp, "tzinfo", None) else signal.timestamp
            if target_ts is None or ts <= target_ts:
                prior.append(signal)
        if not prior:
            return False
        latest = max(
            prior,
            key=lambda signal: signal.timestamp.replace(tzinfo=None)
            if getattr(signal.timestamp, "tzinfo", None)
            else signal.timestamp,
        )
        return str(getattr(latest, "signal_type", "") or "").upper() == str(signal_type or "").upper()

    def _is_5min_impulse_reversal(
        self,
        sig,
        mtf_result,
        graded_confidence: float,
        settings,
    ) -> bool:
        """Allow a 5m choppy reversal only when the flip is a real impulse."""
        candle = getattr(sig, "raw_candle", {}) or {}
        try:
            open_px = float(candle.get("open") or 0.0)
            high_px = float(candle.get("high") or 0.0)
            low_px = float(candle.get("low") or 0.0)
            close_px = float(candle.get("close") or getattr(sig, "price", 0.0) or 0.0)
            atr = max(float(getattr(sig, "atr_value", 0.0) or 0.0), 1e-9)
            body_atr = abs(close_px - open_px) / atr
            range_atr = abs(high_px - low_px) / atr
            close_near_extreme = (
                close_px >= high_px - (0.25 * atr)
                if getattr(sig, "signal_type", "") == "BUY"
                else close_px <= low_px + (0.25 * atr)
            )
        except Exception:
            return False

        adx_value = float(getattr(sig, "adx_value", 0.0) or 0.0)
        conf = float(graded_confidence or 0.0)
        strong_adx = adx_value >= 28.0
        impulse_body = body_atr >= 0.80 and range_atr >= 1.05 and close_near_extreme
        extreme_range = range_atr >= 1.45 and close_near_extreme and conf >= self._relaxed_threshold(0.78, settings)

        volume_confirmed = False
        try:
            candles = (getattr(mtf_result, "results_5min", None) or {}).get("candles") or []
            idx = int(getattr(sig, "bar_index", -1) or -1)
            if candles and 0 <= idx < len(candles):
                current_vol = float(candles[idx].get("volume") or 0.0)
                lookback = candles[max(0, idx - 12):idx]
                vols = [float(row.get("volume") or 0.0) for row in lookback if float(row.get("volume") or 0.0) > 0]
                avg_vol = sum(vols) / len(vols) if vols else 0.0
                volume_confirmed = avg_vol > 0 and current_vol >= (1.35 * avg_vol)
        except Exception:
            volume_confirmed = False

        quality_confirmed = volume_confirmed or conf >= self._relaxed_threshold(0.82, settings)
        return strong_adx and (impulse_body or extreme_range) and quality_confirmed

    def _historical_option_strike(self, instrument: str, direction: str, price: float, cfg: Dict) -> float:
        interval = cfg.get("strike_interval", 50)
        atm = round(float(price) / interval) * interval
        selection = getattr(get_settings(), "option_strike_selection", "ITM")
        option_type = "CE" if direction == "LONG" else "PE"
        if selection in ("ITM", "BOTH"):
            return atm - interval if option_type == "CE" else atm + interval
        return atm

    @staticmethod
    def _filter_option_history_date(df: pd.DataFrame, trade_date) -> pd.DataFrame:
        return get_option_history_store().filter_day(df, trade_date)

    def _load_cached_historical_option_contract(
        self,
        instrument: str,
        strike: float,
        option_type: str,
        interval: str,
        trade_day,
    ) -> Tuple[pd.DataFrame, str]:
        """Use an archived contract only when it contains the requested trade day.

        The live instrument master generally contains current and future expiries,
        so resolving a May signal from it can incorrectly produce a June contract.
        Local candle files retain the original expired contract symbol.  This lookup
        is historical-only and returns no token, so it cannot affect live execution.
        """
        if trade_day is None or trade_day >= datetime.now(IST).date():
            return pd.DataFrame(), ""
        try:
            from pathlib import Path

            desired = int(round(float(strike)))
            desired_width = len(str(abs(desired)))
            suffix = f"{str(option_type or '').upper()}_{interval}.csv"
            candidates = []
            for path in Path("data_store/candles").glob(f"{instrument}*{suffix}"):
                symbol = path.name[: -len(f"_{interval}.csv")]
                match = re.search(r"(\d+)(CE|PE)$", symbol.upper())
                if not match:
                    continue
                digit_text = match.group(1)
                if len(digit_text) < desired_width:
                    continue
                contract_strike = int(digit_text[-desired_width:])
                day_frame = self._filter_option_history_date(
                    self._option_history_store.load(path), trade_day
                )
                if not day_frame.empty:
                    candidates.append((abs(contract_strike - desired), symbol, day_frame))
            if candidates:
                _, symbol, frame = min(candidates, key=lambda row: (row[0], row[1]))
                return frame, symbol
        except Exception as exc:
            logger.debug(
                f"Historical option-cache lookup failed for {instrument} {strike} "
                f"{option_type} on {trade_day}: {exc}"
            )
        return pd.DataFrame(), ""

    def _load_option_candles(self, scanner, instrument: str, strike: float, option_type: str, interval: str, trade_date, now_dt=None) -> Tuple[pd.DataFrame, str, str]:
        trade_day = pd.Timestamp(trade_date).date() if trade_date is not None else None
        archived_frame, archived_symbol = self._load_cached_historical_option_contract(
            instrument, strike, option_type, interval, trade_day
        )
        if not archived_frame.empty:
            diag = getattr(scanner, "_diag_inc", None)
            if diag:
                diag("option_history", "archived_hits")
            return archived_frame, archived_symbol, ""
        opt_info = scanner.data.get_option_token(instrument, strike, option_type, trade_date=trade_date, now_dt=now_dt)
        if not opt_info:
            return pd.DataFrame(), "", ""
        symbol = str(opt_info.get("symbol", ""))
        token = str(opt_info.get("token", ""))
        exchange = str(
            opt_info.get("exch_seg")
            or ("BFO" if _is_bfo_instrument(instrument) else "NFO")
        )
        cache_key = (symbol, interval, trade_day.isoformat() if trade_day else "")
        symbol_cache_key = (symbol, interval)
        diag = getattr(scanner, "_diag_inc", None)
        local_cache_missing_day = False
        if cache_key in self._option_history_cache:
            if diag:
                diag("option_history", "hits" if not self._option_history_cache[cache_key].empty else "misses")
            return self._option_history_cache[cache_key], symbol, token
        if symbol_cache_key in self._option_symbol_history_cache:
            day_df = self._filter_option_history_date(
                self._option_symbol_history_cache[symbol_cache_key],
                trade_day,
            )
            if not day_df.empty or symbol_cache_key in self._option_history_fetch_attempted:
                self._option_history_cache[cache_key] = day_df
                if diag:
                    diag("option_history", "hits" if not day_df.empty else "misses")
                return day_df, symbol, token
        from pathlib import Path
        cache_path = Path("data_store/candles") / f"{symbol}_{interval}.csv"
        if cache_path.exists():
            try:
                df = self._option_history_store.load(cache_path)
                self._option_symbol_history_cache[symbol_cache_key] = df
                day_df = self._filter_option_history_date(df, trade_day)
                if not day_df.empty:
                    self._option_history_cache[cache_key] = day_df
                    if diag:
                        diag("option_history", "hits")
                    return day_df, symbol, token
                self._historical_audit_log(f"Option history cache for {symbol} has no rows for {trade_day}; fetching/using configured fallback.")
                local_cache_missing_day = True
            except Exception:
                pass
        mode = getattr(get_settings(), "ut_option_history_mode", "fetch_or_skip")
        if mode == "stored_or_synthetic":
            self._option_history_cache[cache_key] = pd.DataFrame()
            if diag:
                diag("option_history", "synthetic")
            return pd.DataFrame(), symbol, token
        # ``fetch_or_skip`` must still make one broker attempt for a historical
        # contract/day that is absent from the local cache.  Returning early here
        # turned it into ``stored_or_skip`` and silently downgraded valid option
        # candidates to futures.  The persistent miss store below bounds retries;
        # an unavailable contract still fails closed with no synthetic premium.
        days_back = max(15, int(getattr(scanner, "backtest_days", 7) or 7) * 3)
        miss_ttl = float(
            getattr(get_settings(), "option_history_miss_ttl_seconds", 900.0)
            or 900.0
        )
        if self._option_history_store.has_recent_miss(
            symbol,
            interval,
            days_back,
            miss_ttl,
        ):
            self._option_history_fetch_attempted.add(symbol_cache_key)
            self._option_symbol_history_cache.setdefault(
                symbol_cache_key, pd.DataFrame()
            )
            self._option_history_cache[cache_key] = pd.DataFrame()
            if diag:
                diag("option_history", "misses")
            return pd.DataFrame(), symbol, token
        if diag:
            diag("option_history", "attempts")
        self._option_history_fetch_attempted.add(symbol_cache_key)
        df = scanner.data.get_historical_candles(
            token,
            exchange,
            interval,
            days_back=days_back,
            instrument_name=symbol,
        )
        if df is not None and not df.empty:
            self._option_history_store.clear_miss(symbol, interval, days_back)
            df = df.sort_index()
            existing = self._option_symbol_history_cache.get(symbol_cache_key)
            if existing is not None and not existing.empty:
                df = pd.concat([existing, df]).sort_index()
                df = df[~df.index.duplicated(keep="last")]
            self._option_symbol_history_cache[symbol_cache_key] = df
            day_df = self._filter_option_history_date(df, trade_day)
            if diag and not day_df.empty:
                diag("option_history", "hits")
        else:
            self._option_history_store.record_miss(symbol, interval, days_back)
            self._option_symbol_history_cache.setdefault(symbol_cache_key, pd.DataFrame())
            day_df = pd.DataFrame()
        if day_df.empty:
            if diag:
                diag("option_history", "misses")
        self._option_history_cache[cache_key] = day_df
        return day_df, symbol, token

    def _simulate_actual_option_exit(
        self,
        option_df: pd.DataFrame,
        start_ts: datetime,
        end_ts: datetime,
        sl_price: float,
        rr: float,
        natural_sl_distance: Optional[float] = None,
        max_sl_pct: float = 0.15,
        # Units held, only needed to convert premium points into the rupee P&L the live
        # guards are written in. 0 (the default) leaves the live-exit path inert, so
        # every existing caller keeps the pure stop/target simulation unchanged.
        qty: int = 0,
    ) -> Optional[Dict]:
        if option_df is None or option_df.empty:
            return None
        start = start_ts.replace(tzinfo=None) if start_ts.tzinfo else start_ts
        end = end_ts.replace(tzinfo=None) if end_ts.tzinfo else end_ts
        force_exit_time = datetime.strptime(
            str(getattr(get_settings(), "ut_force_exit_time", "15:25") or "15:25"),
            "%H:%M",
        ).time()
        session_end = datetime.combine(start.date(), force_exit_time)
        end = min(end, session_end)
        window = option_df.loc[(option_df.index >= start) & (option_df.index <= end)]
        if window.empty:
            return None
        entry_premium = float(window.iloc[0]["close"])
        if entry_premium <= 0:
            return None
        risk_from_price = entry_premium - float(sl_price)
        max_sl_dist = max(0.5, entry_premium * float(max_sl_pct or 0.15))
        if natural_sl_distance is not None and float(natural_sl_distance or 0) > 0:
            risk = min(float(natural_sl_distance), max_sl_dist)
        elif risk_from_price >= max(0.5, entry_premium * 0.01):
            risk = min(risk_from_price, max_sl_dist)
        else:
            # The pre-estimated stop can be stale once real option candles supply the entry.
            # Re-anchor to a meaningful premium risk before checking SL/target hits.
            risk = max_sl_dist
        risk = max(0.5, risk)
        sl_price = max(0.05, entry_premium - risk)
        target_price = entry_premium + (risk * rr)
        exit_price = float(window.iloc[-1]["close"])
        exit_time = window.index[-1]
        exit_reason = "HISTORICAL"

        # Optionally let the replay see the LIVE trade-management guards. Without this
        # the only reachable outcomes are SL HIT / TARGET_HIT / HISTORICAL, so a
        # backtest measures entry selection and a fixed stop/target and nothing else.
        # The guard is CALLED, never copied — one implementation, no second copy to
        # drift. Imported here because scanner imports this module.
        live_guard = None
        live_qty = 0
        try:
            if _setting_enabled(
                getattr(get_settings(), "backtest_live_exits_enabled", False), False
            ):
                live_qty = max(0, int(qty or 0))
                if live_qty > 0:
                    from scanner import Scanner as _Scanner

                    live_guard = _Scanner._peak_giveback_cap_exit
        except Exception:
            live_guard = None          # a replay must never fail on an optional probe
        live_self = SimpleNamespace(_setting_enabled=_setting_enabled)
        live_trade = SimpleNamespace(instrument="", inst_type="OPT")
        peak_rs = 0.0

        for ts, row in window.iloc[1:].iterrows():
            low = float(row.get("low", row.get("close", 0.0)) or 0.0)
            high = float(row.get("high", row.get("close", 0.0)) or 0.0)
            op = float(row.get("open", row.get("close", 0.0)) or 0.0)

            if live_guard is not None:
                # Premium points -> rupees, which is what the guards are written in.
                close_px = float(row.get("close", op) or op)
                pnl_rs = (close_px - entry_premium) * live_qty
                peak_rs = max(peak_rs, (high - entry_premium) * live_qty)
                try:
                    verdict = live_guard(
                        live_self, live_trade,
                        current_pnl_rs=pnl_rs, peak_pnl_rs=peak_rs,
                    )
                    fired, why = verdict
                    # The guard hosts two rules; report the one that actually fired
                    # so a replay's exit reasons mean the same thing as a live log's.
                    fired_rule = getattr(verdict, "rule", "") or "PEAK_GIVEBACK_CAP"
                except Exception:
                    fired, why, fired_rule = False, "", ""
                if fired:
                    exit_price = close_px
                    exit_time = ts
                    exit_reason = fired_rule
                    break

            if low <= sl_price and high >= target_price:
                dist_to_sl = abs(op - sl_price)
                dist_to_tgt = abs(op - target_price)
                if dist_to_sl < dist_to_tgt:
                    exit_price = float(sl_price)
                    exit_reason = "SL HIT"
                else:
                    exit_price = float(target_price)
                    exit_reason = "TARGET_HIT"
                exit_time = ts
                break
            elif low <= sl_price:
                exit_price = float(sl_price)
                exit_time = ts
                exit_reason = "SL HIT"
                break
            elif high >= target_price:
                exit_price = float(target_price)
                exit_time = ts
                exit_reason = "TARGET_HIT"
                break
        return {
            "entry": entry_premium,
            "exit": max(0.05, exit_price),
            "stop": sl_price,
            "target": target_price,
            "exit_time": exit_time,
            "reason": exit_reason,
        }
    def _select_historical_option_candidate(
        self,
        scanner,
        instrument: str,
        direction: str,
        price: float,
        trailing_stop: float,
        index_stop_distance: float,
        rr: float,
        cfg: Dict,
        start_ts: datetime,
        end_ts: datetime,
    ) -> Optional[Dict]:
        """
        Evaluate candidate option strikes (ATM and/or ITM) chronologically against intraday candles
        and return the selected option details.
        """
        from config.settings import get_settings
        interval = cfg.get("strike_interval", 50)
        atm_strike = round(float(price) / interval) * interval
        option_type = "CE" if direction == "LONG" else "PE"
        itm_strike = (atm_strike - interval) if option_type == "CE" else (atm_strike + interval)
        
        selection = getattr(get_settings(), "option_strike_selection", "ATM")
        target_strikes = []
        if selection == "ATM":
            target_strikes = [atm_strike]
        elif selection == "ITM":
            target_strikes = [itm_strike]
        else:  # BOTH
            # Evaluate both ATM and ITM. ITM is preferred/safer, so evaluate it first
            target_strikes = [itm_strike, atm_strike]
            
        for strike in target_strikes:
            option_df, trading_symbol, symbol_token = self._load_option_candles(
                scanner, instrument, strike, option_type, "1min", start_ts.date(), now_dt=start_ts
            )
            if option_df.empty:
                continue
                
            # Estimate entry/stop/target/exit using _simulate_actual_option_exit
            # Estimate entry premium (1.2% of spot)
            est_entry = _estimate_opt_premium(price, scanner, instrument, _sig_date(start_ts))
            spot_stop_dist = abs(float(price) - float(trailing_stop))
            sim_mult = 0.5
            natural_sl_premium_dist = abs(spot_stop_dist * sim_mult)
            options_sl_pct = getattr(scanner, "options_sl_pct", 15.0)
            max_sl_dist = est_entry * (options_sl_pct / 100.0)
            effective_sl_dist = max(0.05, min(natural_sl_premium_dist, max_sl_dist))
            opt_sl_price = max(0.05, est_entry - effective_sl_dist)
            
            actual_exit = self._simulate_actual_option_exit(
                option_df=option_df,
                start_ts=start_ts,
                end_ts=end_ts,
                sl_price=opt_sl_price,
                rr=rr,
                natural_sl_distance=natural_sl_premium_dist,
                max_sl_pct=options_sl_pct / 100.0,
            )
            if actual_exit:
                return {
                    "strike": strike,
                    "trading_symbol": trading_symbol,
                    "symbol_token": symbol_token,
                    "option_df": option_df,
                    "entry": actual_exit["entry"],
                    "stop": actual_exit["stop"],
                    "exit": actual_exit["exit"],
                    "target": actual_exit["target"],
                    "exit_time": actual_exit["exit_time"],
                    "reason": actual_exit["reason"] if actual_exit["reason"] != "HISTORICAL" else "SESSION_END",
                }
        return None

    @staticmethod
    def _simulate_futures_exit_path(
        window,
        entry: float,
        stop: float,
        target: float,
        direction: str,
    ):
        """Walk index candles intrabar for a replayed FUT trade — SL/target with
        the same open-proximity tie-break the OPTION replay gets (exit parity,
        2026-07-09 FUT-vs-OPT audit: shadows previously never simulated their
        target/stop path and just rode to the option trade's exit, inflating
        backtest FUT P&L). Returns (exit_price, reason, exit_ts) — reason is
        "" when neither level hit (caller keeps its default)."""
        is_long = str(direction or "").upper() == "LONG"
        for ts, row in window.iterrows():
            low = float(row.get("low", row.get("close", 0.0)) or 0.0)
            high = float(row.get("high", row.get("close", 0.0)) or 0.0)
            op = float(row.get("open", row.get("close", 0.0)) or 0.0)
            if is_long:
                sl_hit, tgt_hit = low <= stop, high >= target
            else:
                sl_hit, tgt_hit = high >= stop, low <= target
            if sl_hit and tgt_hit:
                if abs(op - stop) < abs(op - target):
                    return float(stop), "SL HIT", ts
                return float(target), "TARGET_HIT", ts
            if sl_hit:
                return float(stop), "SL HIT", ts
            if tgt_hit:
                return float(target), "TARGET_HIT", ts
        return None, "", None

    @staticmethod
    def _hybrid_backtest_dual_futures_enabled(scanner) -> bool:
        pref = str(getattr(scanner, "inst_pref", "AUTO") or "AUTO").upper()
        enabled = _setting_enabled(
            getattr(get_settings(), "futures_shadow_enabled", False),
            False,
        )
        return (
            enabled
            and SignalProcessor._historical_trade_creation_allowed(scanner)
            and pref in {"AUTO", "HYBRID"}
        )

    @staticmethod
    def _historical_trade_creation_allowed(scanner) -> bool:
        guard = getattr(scanner, "allows_historical_trade_creation", None)
        if callable(guard):
            return bool(guard())
        return str(getattr(scanner, "mode", "") or "").upper() == "HISTORICAL"

    @staticmethod
    def _historical_signal_date_allowed(scanner, signal_ts) -> bool:
        if not SignalProcessor._historical_trade_creation_allowed(scanner):
            return False
        cutoff_getter = getattr(scanner, "historical_session_cutoff_date", None)
        if not callable(cutoff_getter) or signal_ts is None:
            return True
        try:
            return signal_ts.date() <= cutoff_getter()
        except Exception:
            return True

    def _build_historical_futures_shadow_trade(
        self,
        scanner,
        trade_id: str,
        instrument: str,
        timeframe: str,
        direction: str,
        signal,
        exit_spot: float,
        exit_time,
        grade: str,
        confidence: float,
        rr: float,
        lot_size: int,
        display_suffix: str,
        default_exit_reason: str,
        regime: str = "UNKNOWN",
    ) -> Optional[Trade]:
        entry = float(getattr(signal, "price", 0.0) or 0.0)
        if entry <= 0:
            return None
        trailing_stop = float(getattr(signal, "trailing_stop", entry) or entry)
        configured_cap = float(getattr(scanner, "futures_sl_pct", 0.30) or 0.30)
        effective_pct = futures_stop_tolerance_pct(configured_cap, regime)
        max_sl_dist = entry * (effective_pct / 100.0)
        if direction == "LONG":
            stop = max(trailing_stop, entry - max_sl_dist)
            target = entry + ((entry - stop) * rr)
        else:
            stop = min(trailing_stop, entry + max_sl_dist)
            target = entry - ((stop - entry) * rr)

        # Exit parity (2026-07-09 audit): simulate the shadow's OWN SL/target
        # path on the index candles — previously the shadow just rode to the
        # option trade's exit spot (no TARGET_HIT ever), inflating backtest FUT
        # P&L on trend days. Falls back to the legacy ride-to-exit when candles
        # are unavailable.
        resolved_exit = float(exit_spot or entry)
        exit_reason = default_exit_reason
        sim_exit_ts = None
        try:
            frame = scanner.candles.get_candles(instrument, timeframe)
            sig_ts = getattr(signal, "timestamp", None)
            if frame is not None and len(frame) and sig_ts is not None:
                start = sig_ts.replace(tzinfo=None) if getattr(sig_ts, "tzinfo", None) else sig_ts
                end = exit_time.replace(tzinfo=None) if (exit_time is not None and getattr(exit_time, "tzinfo", None)) else exit_time
                mask = frame.index > start
                if end is not None:
                    mask = mask & (frame.index <= end)
                window = frame.loc[mask]
                if not window.empty:
                    sim_price, sim_reason, sim_ts = self._simulate_futures_exit_path(
                        window, entry, stop, target, direction
                    )
                    if sim_reason:
                        resolved_exit = float(sim_price)
                        exit_reason = sim_reason
                        sim_exit_ts = sim_ts
        except Exception as exc:
            logger.debug(f"FUT shadow exit simulation skipped for {trade_id}: {exc}")

        if direction == "LONG":
            if resolved_exit <= stop:
                resolved_exit = stop
                exit_reason = "SL HIT"
            gross_pnl = (resolved_exit - entry) * (int(getattr(scanner, "user_lots_fut", {}).get(instrument, 1) or 1) * int(lot_size or 1))
        else:
            if resolved_exit >= stop:
                resolved_exit = stop
                exit_reason = "SL HIT"
            gross_pnl = (entry - resolved_exit) * (int(getattr(scanner, "user_lots_fut", {}).get(instrument, 1) or 1) * int(lot_size or 1))
        if sim_exit_ts is not None:
            exit_time = sim_exit_ts

        lots = int(getattr(scanner, "user_lots_fut", {}).get(instrument, 1) or 1)
        settings = get_settings()
        charges = estimate_trade_charges(
            entry,
            resolved_exit,
            lots * int(lot_size or 1),
            "FUT",
            settings,
        )
        entry_time = getattr(signal, "timestamp", None)
        e_time = IST.localize(entry_time) if (entry_time and getattr(entry_time, "tzinfo", None) is None) else entry_time
        ex_time = IST.localize(exit_time) if (exit_time and getattr(exit_time, "tzinfo", None) is None) else exit_time
        return Trade(
            id=trade_id,
            instrument=instrument,
            timeframe=timeframe,
            direction=direction,
            entry_price=entry,
            entry_time=e_time,
            trailing_stop=stop,
            current_stop=stop,
            lots=lots,
            lot_size=lot_size,
            grade=f"{grade} ({display_suffix}, Fut Shadow)",
            confidence=confidence,
            inst_type="FUT",
            option_type="",
            atm_strike=0.0,
            rr_ratio=rr,
            target=target,
            status="CLOSED",
            exit_price=resolved_exit,
            exit_time=ex_time,
            pnl=gross_pnl - charges,
            charges=charges,
            exit_reason=exit_reason,
            instrument_multiplier=1.0,
            entry_spot=entry,
            spot_stop=trailing_stop,
            spot_target=target,
            trading_symbol="FUT_SHADOW",
            symbol_token="",
            market_regime=str(regime or "UNKNOWN"),
            source_engine=str(getattr(signal, "source_engine", "") or ""),
            source_engines=list(getattr(signal, "source_engines", []) or []),
            source_lane=SignalProcessor._signal_source_lane(signal, get_settings().scanner_engine),
            data_provenance="BACKTEST",
        )

    @staticmethod
    def _signal_source_lane(signal, scanner_engine_mode: str = "") -> str:
        lane = str(getattr(signal, "source_lane", "") or "").upper()
        if lane in {"A", "B"}:
            return lane
        source = str(getattr(signal, "source_engine", "") or "").lower()
        if source in {"flow_burst", "apex_hybrid"}:
            return "B"
        if str(scanner_engine_mode or "").lower() == "adaptive_dual":
            return "A"
        return ""

    @staticmethod
    def _historical_replay_source_keys(signal, scanner_engine_mode: str = "") -> List[str]:
        """Return independent replay streams for historical lifecycle building.

        Only true multi-entry engines split into multiple streams. Rank/router engines
        keep one managed lifecycle even when they carry source_engines for context.
        """
        mode = str(scanner_engine_mode or "").lower()
        if mode == "adaptive_dual":
            return [SignalProcessor._signal_source_lane(signal, mode) or "A"]
        if mode == "quality_hybrid":
            return [SignalProcessor._quality_hybrid_replay_stream_key(signal)]

        split_modes = {"quad_union", "quad_prime", "utbot_ema", "utbot_consensus"}
        if mode not in split_modes:
            return ["main"]

        raw_sources = getattr(signal, "source_engines", None)
        sources: List[str] = []
        if isinstance(raw_sources, (list, tuple, set)):
            sources.extend(str(src).strip().lower() for src in raw_sources if str(src or "").strip())
        elif isinstance(raw_sources, str) and raw_sources.strip():
            sources.extend(
                part.strip().lower()
                for part in raw_sources.replace("+", ",").replace("/", ",").split(",")
                if part.strip()
            )

        src = str(getattr(signal, "source_engine", "") or "").strip().lower()
        if src == "both" and mode in {"utbot_ema", "utbot_consensus"}:
            sources.extend(["utbot", "ema" if mode == "utbot_ema" else "consensus"])
        elif src and src not in {"multi", "both", "partner", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"}:
            sources.append(src)

        allowed = (
            {"utbot", "ema", "supertrend", "consensus"}
            if mode in {"quad_union", "quad_prime"}
            else {"utbot", "ema" if mode == "utbot_ema" else "consensus"}
        )
        clean: List[str] = []
        for item in sources:
            for part in str(item).replace("+", ",").replace("/", ",").split(","):
                part = part.strip().lower()
                if part in allowed and part not in clean:
                    clean.append(part)
        if clean:
            return clean
        if mode == "utbot_ema":
            return ["ema"]
        if mode == "utbot_consensus":
            return ["consensus"]
        return ["utbot"]

    @staticmethod
    def _quality_hybrid_replay_stream_key(signal) -> str:
        raw = (
            str(getattr(signal, "quality_hybrid_stream", "") or "")
            or str(getattr(signal, "quality_hybrid_route", "") or "")
            or "quality_core"
        ).strip().lower()
        aliases = {
            "consensus_core": "quality_core",
            "chop_strict_consensus": "quality_core",
            "vote_flip": "quality_core",
            "broad_non_atr": "quality_breakout",
            "volume_thrust": "quality_thrust",
        }
        raw = aliases.get(raw, raw)
        if "thrust" in raw:
            return "quality_thrust"
        if "broad" in raw:
            return "quality_broad"
        if "pullback" in raw or "resume" in raw:
            return "quality_pullback"
        if "breakout" in raw or "breakdown" in raw:
            return "quality_breakout"
        return "quality_core"

    @staticmethod
    def _historical_clone_for_replay_stream(signal, stream_key: str, scanner_engine_mode: str = ""):
        mode = str(scanner_engine_mode or "").lower()
        key = str(stream_key or "main")
        if mode not in {"quad_union", "quad_prime", "utbot_ema", "utbot_consensus", "quality_hybrid"}:
            try:
                setattr(signal, "_historical_replay_stream", key)
            except Exception:
                pass
            return signal

        cloned = copy.copy(signal)
        try:
            if mode != "quality_hybrid":
                setattr(cloned, "source_engine", key)
                setattr(cloned, "source_engines", [key])
            setattr(cloned, "_historical_replay_stream", key)
        except Exception:
            pass
        return cloned

    @staticmethod
    def _historical_replay_stream_key(signal, scanner_engine_mode: str = "") -> str:
        existing = str(getattr(signal, "_historical_replay_stream", "") or "").strip()
        if existing:
            return existing
        mode = str(scanner_engine_mode or "").lower()
        if mode == "adaptive_dual":
            return SignalProcessor._signal_source_lane(signal, mode) or "A"
        return SignalProcessor._historical_replay_source_keys(signal, mode)[0]

    @staticmethod
    def _historical_replay_streams(signals: List[UTBotSignal], scanner_engine_mode: str = "") -> List[List[UTBotSignal]]:
        def naive_ts(sig) -> datetime:
            value = getattr(sig, "timestamp", None)
            if value is None:
                return datetime.min
            return value.replace(tzinfo=None) if getattr(value, "tzinfo", None) else value

        mode = str(scanner_engine_mode or "").lower()
        stream_order = {
            "A": 0,
            "B": 1,
            "utbot": 0,
            "ema": 1,
            "supertrend": 2,
            "consensus": 3,
            "quality_core": 0,
            "quality_broad": 1,
            "quality_thrust": 2,
            "quality_breakout": 3,
            "quality_pullback": 4,
            "main": 0,
        }
        grouped: Dict[str, List[UTBotSignal]] = {}
        for sig in list(signals or []):
            for key in SignalProcessor._historical_replay_source_keys(sig, mode):
                cloned = SignalProcessor._historical_clone_for_replay_stream(sig, key, mode)
                if mode == "adaptive_dual":
                    try:
                        setattr(cloned, "source_lane", key)
                        setattr(cloned, "_historical_replay_stream", key)
                    except Exception:
                        pass
                grouped.setdefault(str(key or "main"), []).append(cloned)

        streams = []
        for key in sorted(grouped, key=lambda item: (stream_order.get(item, 99), item)):
            stream = sorted(grouped[key], key=naive_ts)
            if stream:
                streams.append(stream)
        return streams

    @staticmethod
    def _historical_replay_order(signals: List[UTBotSignal], scanner_engine_mode: str = "") -> List[UTBotSignal]:
        return [
            sig
            for stream in SignalProcessor._historical_replay_streams(signals, scanner_engine_mode)
            for sig in stream
        ]

    @staticmethod
    def _historical_same_replay_stream(left, right, scanner_engine_mode: str = "") -> bool:
        if str(scanner_engine_mode or "").lower() not in {
            "adaptive_dual",
            "quad_union",
            "quad_prime",
            "utbot_ema",
            "utbot_consensus",
            "quality_hybrid",
        }:
            return True
        return SignalProcessor._historical_replay_stream_key(
            left,
            scanner_engine_mode,
        ) == SignalProcessor._historical_replay_stream_key(right, scanner_engine_mode)

    @staticmethod
    def _historical_trade_id(prefix: str, instrument: str, timeframe: str, signal, scanner_engine_mode: str = "", fmt: str = "%Y%m%d%H%M%S") -> str:
        ts = getattr(signal, "timestamp", None)
        ts_part = ts.strftime(fmt) if hasattr(ts, "strftime") else str(ts or "")
        mode = str(scanner_engine_mode or "").lower()
        stream_key = SignalProcessor._historical_replay_stream_key(signal, mode)
        stream_part = ""
        if mode in {"adaptive_dual", "quad_union", "quad_prime", "utbot_ema", "utbot_consensus", "quality_hybrid"} and stream_key:
            stream_part = "_" + re.sub(r"[^A-Za-z0-9]+", "_", str(stream_key)).strip("_").upper()
        return f"{prefix}_{instrument}_{timeframe}{stream_part}_{ts_part}"

    @staticmethod
    def _latest_candle_timestamp(df) -> Optional[datetime]:
        try:
            ts = df.index[-1]
            return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        except Exception:
            return None

    @staticmethod
    def _historical_compression_state(
        candle_5min: Optional[pd.DataFrame],
        signal_timestamp,
        signal_type: str,
    ) -> str:
        """Replay the one-lot box rule without using future session candles.

        Historical rows are completed bars.  The first three completed 5m bars
        qualify/freeze the box; a later completed close beyond the 3bp buffer is
        equivalent to live WATCH followed by confirmation.  A close back inside
        revokes that authorization for sizing at the current signal.
        """
        try:
            if (
                candle_5min is None
                or candle_5min.empty
                or not isinstance(candle_5min.index, pd.DatetimeIndex)
                or signal_timestamp is None
            ):
                return "FORMING"
            ts = pd.Timestamp(signal_timestamp)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
            frame = candle_5min.sort_index()
            idx = frame.index
            if idx.tz is not None:
                idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
                frame = frame.copy()
                frame.index = idx
            frame = frame[(frame.index.date == ts.date()) & (frame.index <= ts)]
            if len(frame) < 3:
                return "FORMING"
            first = frame.iloc[:3]
            ref = float(first["open"].iloc[0] or 0.0)
            box_low = float(first["low"].min())
            box_high = float(first["high"].max())
            if ref <= 0 or box_low <= 0 or box_high < box_low:
                return "FORMING"
            if (box_high - box_low) / ref * 100.0 > 0.20 + 1e-9:
                return "UNRESTRICTED"
            breach = max(ref * 0.0003, 0.01)
            latest_close = float(frame["close"].iloc[-1] or 0.0)
            want_up = str(signal_type or "").upper() == "BUY"
            if want_up and latest_close >= box_high + breach:
                return "BREAKOUT_CONFIRMED"
            if not want_up and latest_close <= box_low - breach:
                return "BREAKOUT_CONFIRMED"
            return "LOCKED"
        except Exception:
            return "FORMING"

    def _append_adaptive_dual_apex_candidates(
        self,
        *,
        scanner,
        instrument: str,
        mtf_result,
        intel_result: Dict,
        intel_score: float,
        regime: str,
        settings,
        candidates_raw: List[Dict],
    ) -> None:
        mode = str(getattr(settings, "scanner_engine", "") or "").lower()
        if mode != "adaptive_dual":
            return
        if not _setting_enabled(getattr(settings, "dual_lane_enabled", True), True):
            return

        timeframes = ["5min", "15min"]
        if str(getattr(scanner, "mode", "") or "").upper() == "HISTORICAL":
            return

        for tf in timeframes:
            df = scanner.candles.get_candles(instrument, tf)
            if df is None or len(df) < 200:
                continue
            try:
                from engine.apex_hybrid import build_apex_signals

                signals = build_apex_signals(
                    df,
                    instrument=instrument,
                    timeframe=tf,
                    df_5m=scanner.candles.get_candles(instrument, "5min"),
                    df_15m=scanner.candles.get_candles(instrument, "15min"),
                    latest_only=True,
                )
            except Exception as exc:
                logger.debug(f"Adaptive Dual APEX Lane B skipped for {instrument} {tf}: {exc}")
                continue
            for signal in signals:
                signal_time = signal.timestamp
                if getattr(signal_time, "tzinfo", None) is not None:
                    signal_time = signal_time.astimezone(IST).replace(tzinfo=None)
                signal_type = str(getattr(signal, "signal_type", "") or "").upper()
                identity = (
                    f"{instrument}|{tf}|B|APEX|{signal_type}|"
                    f"{signal_time.isoformat(timespec='seconds')}|"
                    f"{round(float(getattr(signal, 'price', 0.0) or 0.0), 2)}"
                )
                seen = getattr(scanner, "_last_apex_lane_b_identity", None)
                if seen is None:
                    scanner._last_apex_lane_b_identity = {}
                    seen = scanner._last_apex_lane_b_identity
                seen_key = f"{instrument}_{tf}_{signal_type}"
                if seen.get(seen_key) == identity:
                    continue
                seen[seen_key] = identity

                no_entry_label, no_entry_after = _entry_cutoff(settings, tf)
                is_after_no_entry = tf in ["1min", "5min", "15min"] and signal_time.time() >= no_entry_after
                ts_str = signal_time.strftime("%Y-%m-%d %H:%M:00")
                vix_value = scanner._vix_data.get(ts_str)
                if vix_value is None:
                    vix_value = scanner.data.get_ltp("NSE", "INDIAVIX", "99926017") or 15.0
                session_ctx = (
                    intel_result.get("session_context", {})
                    if isinstance(intel_result, dict)
                    else {}
                )
                grade, conf, reasons = scanner.signals._grade_signal(
                    signal,
                    mtf_result.confluence_score,
                    intel_score,
                    regime,
                    vix_value=vix_value,
                    day_type=str(session_ctx.get("day_type") or ""),
                    day_type_confidence=float(session_ctx.get("confidence") or 0.0),
                )
                candidates_raw.append({
                    "sig": signal,
                    "tf": tf,
                    "grade": grade,
                    "confidence": conf,
                    "reasons": list(reasons or []) + list(getattr(signal, "apex_reasons", []) or []),
                    "after_no_entry": is_after_no_entry,
                    "after_no_entry_label": no_entry_label,
                    "dedup_key": f"{instrument}_{tf}_apex",
                    "signal_identity": identity,
                })
                scanner.log_event(
                    f"Adaptive Dual Lane B APEX {signal_type}: {instrument} {tf} @ {signal.price:.2f}",
                    "trade",
                )

    def process_best_signal(
        self, instrument, mtf_result, intel_result, intel_score, regime,
        lots, lot_size, cfg, spot, atm_strike,
    ) -> List[TradeCandidate]:
        
        # Access Scanner properties via self.scanner
        scanner = self.scanner
        settings = get_settings()

        def naive_ts(value):
            if value is None:
                return datetime.min
            return value.replace(tzinfo=None) if getattr(value, "tzinfo", None) else value

        if not scanner.risk_manager.can_trade("ANY"):
            return []

        # None unless per_timeframe_grading_enabled is set, in which case each candidate
        # is graded with its own timeframe's cached intelligence instead of the 5-minute one.
        _timeframe_contexts = self._build_timeframe_contexts(scanner, instrument, settings)

        # Collect NEW signals from the normal entry TFs. Live mode also watches
        # 1min when: (a) 1M spike scalping is ON, or (b) post-exit reversion is
        # ON and this index has an open reversion window (decoupled toggles).
        scanner_engine_mode = str(getattr(settings, "scanner_engine", "utbot") or "utbot").lower()
        candidates_raw = []
        candidate_timeframes = ["5min", "15min"]
        _live = getattr(scanner, "mode", "").upper() != "HISTORICAL"
        _vol_spike_on = _setting_enabled(getattr(settings, "volume_spike_entry_enabled", True), True)
        _one_min_scalp_on = _setting_enabled(getattr(settings, "one_min_scalping_enabled", True), True)
        _reversion_on = _setting_enabled(getattr(settings, "post_exit_reversion_enabled", False), False)
        _reversion_window = False
        if _live and _reversion_on:
            try:
                _rw = getattr(scanner, "_reversion_window_active", None)
                _reversion_window = bool(_rw(instrument)) if callable(_rw) else False
            except Exception:
                _reversion_window = False
        # 1M SPIKE path: volume_spike_entry + one_min_scalping both ON.
        # POST-WIN SNAPBACK path: reversion window alone is enough (decoupled —
        # must not require volume_spike_entry_enabled, or pure snapback dies).
        if _live and (
            (_vol_spike_on and _one_min_scalp_on) or bool(_reversion_window)
        ):
            candidate_timeframes.insert(0, "1min")
        for tf in candidate_timeframes:
            sigs = [] # Master Fix: Clear sigs to prevent stale leak from previous iterations
            result = getattr(mtf_result, f"results_{tf}", None)
            if result is None:
                if tf == "15min": logger.debug(f"⚠️ No 15min results for {instrument} yet.")
                continue
            sigs = [s for s in result.get("signals", []) if s.instrument == instrument]
            logger.debug(f"🔍 [AUDIT-SIGS] {instrument} {tf}: found {len(sigs)} signals")
            if not sigs:
                continue
            dedup_key = f"{instrument}_{tf}"
            is_live = scanner.mode.upper() != "HISTORICAL"
            if tf == "1min" and is_live and dedup_key not in scanner._last_signal_time:
                scanner._last_signal_time[dedup_key] = (
                    naive_ts(sigs[-2].timestamp)
                    if len(sigs) >= 2
                    else naive_ts(sigs[-1].timestamp) - timedelta(microseconds=1)
                )
            if dedup_key not in scanner._last_signal_time:
                # ═══ Mode-Aware Historical Backfill (Atomic Lock) ═══
                # Immediately seed to prevent concurrent backfill from other tasks
                scanner._last_signal_time[dedup_key] = naive_ts(datetime.now(IST))
                # Load last N days for Historical mode, or 1 day for Live modes (Warm Startup)
                if (scanner.mode == "HISTORICAL" or is_live):
                    # Fetch 5min candles to determine trading dates for lookback
                    candles_df = scanner.candles.get_candles(instrument, "5min")
                    if candles_df is None or candles_df.empty:
                        return []
                    tf_candles_df = scanner.candles.get_candles(instrument, tf)
                    if tf_candles_df is None or getattr(tf_candles_df, "empty", True):
                        tf_candles_df = candles_df
                    
                    # Logic Fix: Data lookback vs Trade lookback
                    data_lookback = 1 if is_live else 45 # Live mode only replays the current session.
                    
                    # Trade lookback: The actual window the user wants to see/test
                    trade_lookback = 1 if is_live else scanner.backtest_days
                    
                    # ═══ Trading Day Awareness ═══
                    active_dates = sorted(list(set(candles_df.index.date)))
                    logger.debug(f"📅 Active dates in candles for {instrument}: {active_dates}")
                    if not active_dates: return []
                    
                    if is_live:
                        # Live/Paper mode replays the active trading session. After midnight but before
                        # 09:15 IST, that session is still the previous market day.
                        session_day_raw = (
                            scanner._get_current_session_day()
                            if hasattr(scanner, "_get_current_session_day")
                            else datetime.now(IST).date().isoformat()
                        )
                        session_day = datetime.fromisoformat(str(session_day_raw)).date()
                        trade_cutoff = session_day
                        data_cutoff = session_day
                    else:
                        # Trade Cutoff (for display/logging in closed_trades)
                        if trade_lookback <= 1 and hasattr(scanner, "_get_current_session_day"):
                            session_day = datetime.fromisoformat(str(scanner._get_current_session_day())).date()
                            session_dates = [d for d in active_dates if d <= session_day]
                            trade_cutoff = session_dates[-1] if session_dates else active_dates[-1]
                        else:
                            trade_cutoff = active_dates[-min(len(active_dates), trade_lookback)]
                        # Data Cutoff (for signal calculation)
                        data_cutoff = active_dates[-min(len(active_dates), data_lookback)]
                    
                    # Filter signals for this TF by data_cutoff and sort chronologically (oldest first)
                    def get_naive_ts(s):
                        return s.timestamp.replace(tzinfo=None) if s.timestamp.tzinfo else s.timestamp
                        
                    if (
                        not is_live
                        and scanner_engine_mode == "adaptive_dual"
                        and _setting_enabled(getattr(settings, "dual_lane_enabled", True), True)
                    ):
                        try:
                            from engine.engine_lab import EngineLab

                            sigs.extend(
                                EngineLab._apex_replay_signals(
                                    tf_candles_df,
                                    instrument,
                                    tf,
                                    df_5m=scanner.candles.get_candles(instrument, "5min"),
                                    df_15m=scanner.candles.get_candles(instrument, "15min"),
                                )
                            )
                        except Exception as exc:
                            logger.debug(f"Historical APEX Lane B replay signal build skipped for {instrument} {tf}: {exc}")

                    sigs = self._historical_replay_order(
                        [s for s in sigs if s.instrument == instrument and s.timestamp.date() >= data_cutoff],
                        scanner_engine_mode,
                    )
                    
                    # Process signals for this TF
                    for i in range(len(sigs) - 1):
                        s1, s2 = sigs[i], sigs[i+1]
                        if not self._historical_same_replay_stream(s1, s2, scanner_engine_mode):
                            continue
                        # Earlier rows are indicator warm-up only. They must not inflate
                        # diagnostics for the selected backtest window.
                        if s1.timestamp.date() < trade_cutoff:
                            continue
                        
                        hist_vix = scanner._get_historical_vix(s1.timestamp)
                        hist_intel_score, hist_intel_regime = scanner._compute_historical_intel_at(
                            instrument,
                            s1.timestamp,
                            tf,
                        )
                        hist_regime = hist_intel_regime if hist_intel_regime and hist_intel_regime != "UNKNOWN" else "UNKNOWN"
                        if hist_regime == "UNKNOWN":
                            try:
                                sub_df = candles_df.loc[:s1.timestamp] if getattr(candles_df.index, "is_monotonic_increasing", False) else candles_df[candles_df.index <= s1.timestamp]
                            except Exception:
                                sub_df = candles_df[candles_df.index <= s1.timestamp]
                            hist_regime_res = scanner.intel.regime.detect(sub_df, instrument, "5min")
                            hist_regime = hist_regime_res.get("regime", "UNKNOWN")
                        hist_confluence = scanner._compute_historical_confluence_at(
                            mtf_result,
                            s1.timestamp,
                        )

                        _hist_day_type, _hist_day_conf = "", 0.0
                        try:
                            _day_at = getattr(scanner, "_historical_day_type_at", None)
                            if callable(_day_at):
                                _hist_day_type, _hist_day_conf = _day_at(instrument, s1.timestamp, tf)
                        except Exception:
                            pass
                        hist_grade, hist_conf, hist_reasons = scanner.signals._grade_signal(
                            s1,
                            hist_confluence,
                            hist_intel_score,
                            regime=hist_regime,
                            vix_value=hist_vix,
                            day_type=_hist_day_type,
                            day_type_confidence=_hist_day_conf,
                        )
                        direction = "LONG" if s1.signal_type == "BUY" else "SHORT"

                        def record_reject(reason: str, inst_type: str = "FUT", strike: float = 0.0, option_type: str = "") -> None:
                            recorder = getattr(scanner, "_record_historical_reject", None)
                            if recorder:
                                recorder(
                                    s1,
                                    instrument,
                                    tf,
                                    direction,
                                    hist_grade,
                                    hist_conf,
                                    reason,
                                    inst_type,
                                    lot_size,
                                    strike,
                                    option_type,
                                    regime=hist_regime,
                                )
                            emit_once = getattr(scanner, "log_signal_decision_once", None)
                            if callable(emit_once):
                                emit_once(
                                    f"hist|{instrument}|{tf}|{s1.signal_type}|{s1.timestamp.isoformat(timespec='seconds')}|{inst_type}|{reason}",
                                    (
                                        f"Signal rejected: {instrument} {s1.signal_type} {tf} "
                                        f"@ {float(getattr(s1, 'price', 0.0) or 0.0):.2f} - {reason}"
                                    ),
                                    "trade",
                                )
                        
                        logger.debug(f"🔍 [AUDIT] Signal {i} for {instrument} at {s1.timestamp}: Grade={hist_grade}, Conf={hist_conf:.2f}, Regime={hist_regime}, Type={s1.signal_type}")
                        
                        more_results = (
                            _setting_enabled(getattr(settings, "ut_backtest_more_results", True), True)
                            and scanner.mode == "HISTORICAL"
                        )

                        # grade_gate_allow_c_enabled (default OFF) lets a C continue to the
                        # confidence/choppy/quality gates rather than being refused on
                        # grade alone. It admits nothing by itself — see config/settings.py.
                        allow_c_grade = _setting_enabled(
                            getattr(settings, "grade_gate_allow_c_enabled", False), False
                        )
                        if hist_grade == "C" and not allow_c_grade and not (more_results and hist_conf >= self._relaxed_threshold(0.62, settings) and not self._is_choppy_regime(hist_regime)):
                            record_reject("C grade blocked")
                            self._historical_audit_log(f"â­ï¸ [AUDIT] Skipping C grade signal at {s1.timestamp}")
                            continue

                        direction = "LONG" if s1.signal_type == "BUY" else "SHORT"
                        trend_15m_agrees_hist = self._trend_15m_agrees_at(
                            mtf_result,
                            s1.timestamp,
                            s1.signal_type,
                        )
                        adx_val = getattr(s1, "adx_value", 0.0)
                        momentum_override = float(adx_val or 0.0) >= float(getattr(settings, "momentum_override_threshold", 25.0) or 25.0)
                        intel_impulse = abs(normalize_intelligence_score(hist_intel_score)) >= 0.70
                        impulse_context_hist = bool(momentum_override or intel_impulse)
                        hist_reason_text = " ".join(str(r).lower() for r in (hist_reasons or []))
                        hist_extreme_exhaustion = "extreme" in hist_reason_text and "exhaustion" in hist_reason_text
                        hist_true_5min_impulse = tf == "5min" and five_min_true_impulse_allowed(
                            hist_grade,
                            hist_conf,
                            settings,
                            trend_15m_agrees=trend_15m_agrees_hist,
                            impulse_confirmed=impulse_context_hist,
                            explosive_bypass=impulse_context_hist,
                            extreme_exhaustion=hist_extreme_exhaustion,
                        )

                        # â”€â”€ Determine Instrument Type via Hybrid Rules â”€â”€
                        sim_inst = scanner._resolve_instrument_type(
                            instrument=instrument,
                            grade=hist_grade,
                            regime=hist_regime,
                            intel_score=hist_intel_score,
                            signal_score=hist_conf,
                            confidence=hist_conf,
                            adx_value=getattr(s1, "adx_value", 0.0),
                            atr_value=getattr(s1, "atr_value", 0.0),
                            price=s1.price,
                            signal_time=s1.timestamp,
                            timeframe=tf,
                            five_min_true_impulse=hist_true_5min_impulse,
                        )
                        diag_inc_unique = getattr(scanner, "_diag_inc_unique", None)
                        if diag_inc_unique:
                            diag_inc_unique(
                                "instrument_selection",
                                sim_inst,
                                "|".join([instrument, tf, str(s1.timestamp), str(s1.signal_type)]),
                            )
                        
                        grade_hierarchy = {"C": 0, "B": 1, "B+": 2, "A": 3, "A+": 4}
                        base_grade = hist_grade
                        sig_rank = grade_hierarchy.get(base_grade, 0)
                        
                        grade_pref = getattr(settings, "signal_grade_preference", "auto")
                        regime_adaptation = _setting_enabled(getattr(settings, "ut_regime_adaptation", True), True)
                        is_choppy = regime_adaptation and self._is_choppy_regime(hist_regime) and not momentum_override
                        
                        fifteen_min_bplus_ok = bool(
                            tf == "15min"
                            and sig_rank >= 1
                            and hist_conf >= fifteen_min_confidence_floor(
                                settings,
                                hist_regime,
                                adx_value=float(getattr(s1, "adx_value", 0.0) or 0.0),
                                intel_score=hist_intel_score,
                            )
                        )

                        # Data-confirmed power move (historical parity with live): a big
                        # volume/range/close spike with momentum+alignment bypasses the grade
                        # floor and the no-entry cutoff here too, so backtests reflect live.
                        power_move_hist = scanner._detect_power_move(
                            instrument,
                            tf,
                            s1.signal_type,
                            adx_value=float(getattr(s1, "adx_value", 0.0) or 0.0),
                            intel_score=hist_intel_score,
                            trend_15m_agrees=trend_15m_agrees_hist,
                            as_of=s1.timestamp,
                        )

                        # ── R2: regime veto + directional census (replay parity) ──
                        # Same gates as the live path so 22-Jun-style replays reflect them.
                        _hist_dir = "LONG" if s1.signal_type == "BUY" else "SHORT"
                        try:
                            _hist_day = s1.timestamp.date().isoformat()
                            if getattr(scanner, "_dir_census_date", None) != _hist_day:
                                scanner._dir_census_date = _hist_day
                                scanner._dir_census = {"LONG": 0, "SHORT": 0}
                                scanner._dir_census_seen = set()
                            _cid = f"{instrument}|{tf}|{str(s1.timestamp)}|{_hist_dir}"
                            if _cid not in scanner._dir_census_seen:
                                scanner._dir_census_seen.add(_cid)
                                scanner._dir_census[_hist_dir] = scanner._dir_census.get(_hist_dir, 0) + 1
                        except Exception:
                            pass
                        _hv = directional_fut_veto(
                            settings,
                            inst_type=sim_inst,
                            signal_type=s1.signal_type,
                            regime=hist_regime,
                            adx_value=float(getattr(s1, "adx_value", 0.0) or 0.0),
                            power_move=power_move_hist,
                        )
                        if _hv:
                            # If we got a regime veto on FUT and inst_pref is AUTO, fall back to OPT
                            if sim_inst == "FUT" and inst_pref == "AUTO":
                                self._historical_audit_log(
                                    f"[{instrument}] FUT regime vetoed ({_hv}), falling back to OPT at {s1.timestamp}"
                                )
                                sim_inst = "OPT"
                            else:
                                record_reject(_hv, sim_inst)
                                continue
                        _hist_reversal_relief = None
                        try:
                            _relief_fn = getattr(scanner, "_entry_reversal_relief_context", None)
                            if callable(_relief_fn):
                                _hist_reversal_relief = _relief_fn(
                                    instrument,
                                    tf,
                                    s1.signal_type,
                                    s1.timestamp,
                                    regime=hist_regime,
                                    confidence=hist_conf,
                                )
                        except Exception as exc:
                            logger.debug(f"Historical reversal relief check skipped for {instrument} {tf}: {exc}")
                        _hc = directional_census_block(
                            settings,
                            getattr(scanner, "_dir_census", {}) or {},
                            signal_direction=_hist_dir,
                            confidence=hist_conf,
                            grade_floor=self._grade_min_confidence("B+"),
                            power_move=power_move_hist,
                            reversal_context=_hist_reversal_relief,
                            day_type=_hist_day_type,
                            day_type_confidence=_hist_day_conf,
                        )
                        if _hc:
                            record_reject(_hc, sim_inst)
                            continue

                        # 1. Block weak 5min/option noise in choppy markets, while keeping
                        # confirmed 15min B-or-better setups on their separate confidence floor.
                        if (
                            is_choppy
                            and not hist_true_5min_impulse
                            and not fifteen_min_bplus_ok
                            and not power_move_hist
                            and base_grade in ["B", "B+"]
                            and not (more_results and (base_grade == "B+" or hist_conf >= self._relaxed_threshold(0.58, settings)))
                        ):
                            record_reject("Choppy market blocked", sim_inst)
                            self._historical_audit_log(f"⛔ Skipping choppy-blocked signal {s1.signal_type} {base_grade} at {s1.timestamp}")
                            continue
                            
                        # 2. Dynamic Confidence Filter
                        base_min_confidence = (0.50 if not is_choppy else 0.55) if more_results else (0.58 if not is_choppy else 0.65)
                        min_confidence = self._relaxed_threshold(base_min_confidence, settings)
                        if hist_conf < min_confidence and not fifteen_min_bplus_ok and not power_move_hist:
                            record_reject(f"Confidence below {min_confidence:.0%}", sim_inst)
                            self._historical_audit_log(f"â­ï¸ Skipping low-confidence signal {s1.signal_type} {base_grade} ({hist_conf} < {min_confidence}) at {s1.timestamp}")
                            continue
                            
                        # 3. Grade Preference Filter
                        if grade_pref == "B":
                            # Futures allow B (1), Options require B+ (2)
                            min_rank = 1 if sim_inst == "FUT" else 2
                        elif grade_pref == "B+":
                            min_rank = 2
                        elif grade_pref == "A":
                            min_rank = 3
                        else: # auto
                            min_rank = 3 if is_choppy else 2

                        # A/B override (default -1 = follow signal_grade_preference above).
                        # Read AFTER the preference ladder so it wins, and bounded to the
                        # hierarchy so a stray value cannot open or close the gate entirely.
                        try:
                            rank_override = int(getattr(settings, "grade_gate_min_rank_override", -1))
                        except (TypeError, ValueError):
                            rank_override = -1
                        if rank_override >= 0:
                            min_rank = max(0, min(4, rank_override))

                        if sig_rank < min_rank and not fifteen_min_bplus_ok and not power_move_hist:
                            record_reject(f"Grade rank below required {min_rank}", sim_inst)
                            self._historical_audit_log(f"🚫 Skipping grade-preference-blocked signal {s1.signal_type} {base_grade} (rank {sig_rank} < min {min_rank}) at {s1.timestamp}")
                            continue

                        # ── Late-trend entry ceiling ──────────────────────────
                        # A high ADX on a WITH-trend entry means the leg is mature, not
                        # that it is strong. Measured over 47 stored sessions:
                        #   TRENDING_DOWN shorts   median ADX 34.4   win rate  7.1%
                        #   BREAKOUT_DOWN  shorts  median ADX 21.4   win rate 71.4%
                        # Same direction, same instruments — the difference is WHEN.
                        # Independently, the whole ADX>=30 cell runs 32.4% / Rs.41 against
                        # 45.8% / Rs.248 for ADX<20. Blocking with-trend entries above the
                        # ceiling removes 43 of 240 historical trades worth -Rs.4,033 and
                        # lifts the book from 40.0%/Rs.197 to 46.2%/Rs.260.
                        # AGAINST-trend entries are untouched: they are the mean-reversion
                        # side of the same reading and measured positive.
                        late_ceiling = float(
                            getattr(settings, "late_trend_entry_adx_ceiling", 30.0) or 0.0
                        )
                        if (
                            late_ceiling > 0
                            and _setting_enabled(
                                getattr(settings, "late_trend_entry_guard_enabled", True), True
                            )
                            and not power_move_hist
                            and float(adx_val or 0.0) > late_ceiling
                            and (
                                (str(hist_regime).upper() == "TRENDING_DOWN" and direction == "SHORT")
                                or (str(hist_regime).upper() == "TRENDING_UP" and direction == "LONG")
                            )
                        ):
                            record_reject(
                                f"Late-trend entry: ADX {float(adx_val or 0.0):.0f} > {late_ceiling:.0f} "
                                f"with {hist_regime}",
                                sim_inst,
                            )
                            self._historical_audit_log(
                                f"🚫 Late-trend entry blocked {s1.signal_type} {hist_regime} "
                                f"ADX={adx_val} at {s1.timestamp}"
                            )
                            continue


                        # Aligned Quality Gates (mirror _passes_live_trade_ready_gate)
                        if not power_move_hist and not self._passes_quality_gate(
                            tf,
                            base_grade,
                            hist_conf,
                            hist_regime,
                            sim_inst,
                            settings,
                            getattr(s1, "adx_value", 0.0),
                            trend_15m_agrees=trend_15m_agrees_hist,
                            intel_score=hist_intel_score,
                        ):
                            record_reject("Quality gate blocked", sim_inst)
                            self._historical_audit_log(f"⭐ Skipping {instrument} {tf} signal due to quality gate at {s1.timestamp}")
                            continue
                            
                        if hist_regime == "UNKNOWN" and not (sig_rank >= 3 and hist_conf >= 0.75) and not power_move_hist:
                            record_reject("UNKNOWN regime requires A/A+ >=75%", sim_inst)
                            self._historical_audit_log(f"⛔ Skipping {instrument} {tf} signal: UNKNOWN regime requires A/A+ >=75% at {s1.timestamp}")
                            continue
                            
                        # ── No-Entry After Gate ──
                        try:
                            no_entry_after, gate_time = _entry_cutoff(settings, tf)
                            s1_time = s1.timestamp.replace(tzinfo=None).time() if s1.timestamp.tzinfo else s1.timestamp.time()
                            power_move_cutoff_ok = bool(power_move_hist and scanner._power_move_late_cutoff_ok(s1.timestamp))
                            if s1_time >= gate_time and not power_move_cutoff_ok:
                                record_reject(f"After no-entry gate {no_entry_after}", sim_inst)
                                self._historical_audit_log(f"🚫 [AUDIT] Skipping backfill entry at {s1.timestamp} due to ut_no_entry_after ({no_entry_after})")
                                continue
                        except Exception as e:
                            logger.exception(f"Error checking entry cutoff: {e}")
                        # Historical concurrency is applied after all workers finish.
                        if sim_inst == "OPT":
                            option_pref = (getattr(scanner, "inst_pref", "AUTO") or "AUTO").upper()
                            option_quality_ok = self._option_grade_allowed(
                                base_grade,
                                hist_conf,
                                hist_regime,
                                adx_value=getattr(s1, "adx_value", 0.0),
                                intel_score=hist_intel_score,
                                signal_score=hist_conf,
                                five_min_true_impulse=hist_true_5min_impulse,
                                timeframe=tf,
                            )
                            if not option_quality_ok:
                                if option_pref == "OPT":
                                    record_reject("Option premium quality gate blocked", sim_inst)
                                    self._historical_audit_log(f"Skipping option signal below premium quality gate at {s1.timestamp}")
                                    continue
                                self._historical_audit_log(
                                    f"Falling back to FUT for {instrument} {tf} at {s1.timestamp}: "
                                    f"option gate failed but AUTO/HYBRID should preserve the futures setup."
                                )
                                sim_inst = "FUT"

                        # Historical workers run concurrently. Filtering against the
                        # partially built shared trade list makes replay results depend
                        # on task completion order. TradeManager applies the configured
                        # concurrency policy deterministically after generation.
                        if s1.timestamp.date() >= trade_cutoff and s2.timestamp.date() >= trade_cutoff and s1.signal_type != s2.signal_type:
                            force_exit_time = datetime.strptime(
                                str(getattr(settings, "ut_force_exit_time", "15:25") or "15:25"),
                                "%H:%M",
                            ).time()
                            entry_ts = s1.timestamp.replace(tzinfo=None) if s1.timestamp.tzinfo else s1.timestamp
                            next_signal_ts = s2.timestamp.replace(tzinfo=None) if s2.timestamp.tzinfo else s2.timestamp
                            session_exit_ts = datetime.combine(entry_ts.date(), force_exit_time)
                            crosses_session = (
                                next_signal_ts.date() != entry_ts.date()
                                or next_signal_ts > session_exit_ts
                            )
                            effective_exit_ts = session_exit_ts if crosses_session else next_signal_ts

                            if sim_inst == "FUT":
                                sim_option_type = ""
                                sim_multiplier = 1.0
                                hist_strike = 0.0
                            else:
                                sim_option_type = "CE" if direction == "LONG" else "PE"
                                hist_strike = self._historical_option_strike(instrument, direction, s1.price, cfg)
                                # Signed delta (PE negative) — parity with live path (audit #6).
                                try:
                                    dte_h = scanner.expiry.get_dte(instrument, s1.timestamp.date())
                                except Exception:
                                    dte_h = 1
                                sim_multiplier = self._option_signed_delta(
                                    s1.price,
                                    hist_strike,
                                    sim_option_type,
                                    {},
                                    dte_h,
                                    0.5 if sim_option_type == "CE" else -0.5,
                                )

                            index_stop_distance = s1.stop_distance
                            # Provisional entry/stop for risk sizing (FUT uses index; OPT uses abs delta).
                            if sim_inst == "FUT":
                                hist_entry_px = float(s1.price or 0.0)
                                hist_stop_px = float(s1.trailing_stop or 0.0)
                                if direction == "LONG":
                                    hist_stop_px = max(
                                        hist_stop_px,
                                        hist_entry_px * (1.0 - float(scanner.futures_sl_pct or 0.0) / 100.0),
                                    )
                                else:
                                    hist_stop_px = min(
                                        hist_stop_px if hist_stop_px > 0 else hist_entry_px * 1.01,
                                        hist_entry_px * (1.0 + float(scanner.futures_sl_pct or 0.0) / 100.0),
                                    )
                                requested = int(getattr(scanner, "user_lots_fut", {}).get(instrument, 1) or 1)
                            else:
                                hist_entry_px = float(_estimate_opt_premium(s1.price, scanner, instrument, _sig_date(s1)) or 0.0)
                                natural = abs(float(index_stop_distance or 0.0) * abs(float(sim_multiplier or 0.5)))
                                max_sl = hist_entry_px * (float(scanner.options_sl_pct or 15.0) / 100.0)
                                hist_stop_px = max(0.05, hist_entry_px - max(0.05, min(natural, max_sl)))
                                requested = int(getattr(scanner, "user_lots", {}).get(instrument, 1) or 1)

                            hist_compression_state = self._historical_compression_state(
                                candles_df,
                                s1.timestamp,
                                s1.signal_type,
                            )
                            size_result = resolve_trade_lots(
                                settings=settings,
                                inst_type=sim_inst,
                                requested_lots=requested,
                                lot_size=lot_size,
                                entry_price=hist_entry_px,
                                stop_price=hist_stop_px,
                                compression_state=hist_compression_state,
                                intel_score=float(hist_intel_score or 0.0),
                            )
                            hist_lots = int(size_result.lots or 0)
                            if hist_lots < 1:
                                self._historical_audit_log(
                                    f"Skipping {instrument} {tf} {sim_inst}: {size_result.reason}"
                                )
                                continue

                            qty = hist_lots * lot_size
                            
                            actual_option = None
                            trading_symbol = ""
                            symbol_token = ""
                            if sim_inst == "OPT":
                                option_df, trading_symbol, symbol_token = self._load_option_candles(
                                    scanner, instrument, hist_strike, sim_option_type, "1min", s1.timestamp.date(), now_dt=s1.timestamp
                                )
                                if option_df.empty and getattr(settings, "ut_option_history_mode", "fetch_or_skip") == "fetch_or_skip":
                                    option_pref = (getattr(scanner, "inst_pref", "AUTO") or "AUTO").upper()
                                    if option_pref == "OPT":
                                        self._historical_audit_log(f"Skipping {instrument} {sim_option_type} {hist_strike} at {s1.timestamp}: no real option candles.")
                                        continue
                                    self._historical_audit_log(
                                        f"Falling back to FUT for {instrument} {tf} at {s1.timestamp}: "
                                        "no real option candles in AUTO/HYBRID."
                                    )
                                    diag = getattr(scanner, "_diag_inc", None)
                                    if diag:
                                        diag("option_history", "fallback_to_fut")
                                    sim_inst = "FUT"
                                    sim_option_type = ""
                                    sim_multiplier = 1.0
                                    hist_strike = 0.0
                                    entry_premium = s1.price
                                    fut_stop = float(s1.trailing_stop or 0.0)
                                    size_result = resolve_trade_lots(
                                        settings=settings,
                                        inst_type="FUT",
                                        requested_lots=int(
                                            getattr(scanner, "user_lots_fut", {}).get(instrument, 1) or 1
                                        ),
                                        lot_size=lot_size,
                                        entry_price=float(entry_premium or 0.0),
                                        stop_price=fut_stop,
                                        compression_state=hist_compression_state,
                                        intel_score=0.0,  # FUT: never enlarged by conviction
                                    )
                                    hist_lots = int(size_result.lots or 0)
                                    if hist_lots < 1:
                                        continue
                                    qty = hist_lots * lot_size
                                else:
                                    entry_premium = _estimate_opt_premium(s1.price, scanner, instrument, _sig_date(s1))
                            else:
                                entry_premium = s1.price
                            
                            if sim_inst == "FUT":
                                sl_price = s1.trailing_stop
                                hist_fut_sl_pct = futures_stop_tolerance_pct(
                                    scanner.futures_sl_pct,
                                    hist_regime,
                                )
                                max_sl_dist = entry_premium * (hist_fut_sl_pct / 100.0)
                                if direction == "LONG":
                                    sl_price = max(sl_price, entry_premium - max_sl_dist)
                                else:
                                    sl_price = min(sl_price, entry_premium + max_sl_dist)
                                index_stop_distance = max(1.0, abs(entry_premium - sl_price))
                            else:
                                # Options are always long-premium instruments (CE/PE buy). Keep SL below entry.
                                # Use absolute spot stop distance mapped by multiplier, capped by option SL%.
                                spot_stop_dist = abs(float(s1.price) - float(s1.trailing_stop))
                                natural_sl_dist = abs(spot_stop_dist * sim_multiplier)
                                max_sl_dist = entry_premium * (scanner.options_sl_pct / 100.0)
                                effective_sl_dist = max(0.05, min(natural_sl_dist, max_sl_dist))
                                sl_price = max(0.05, entry_premium - effective_sl_dist)
                            
                            raw_exit_spot = s2.price
                            if crosses_session:
                                raw_exit_spot = s1.price
                                day_candles = candles_df[
                                    (candles_df.index.date == entry_ts.date())
                                    & (candles_df.index.time <= force_exit_time)
                                ]
                                if not day_candles.empty:
                                    raw_exit_spot = float(day_candles.iloc[-1]["close"])
                            
                            if sim_inst == "OPT":
                                natural_sl_distance = abs(index_stop_distance * sim_multiplier)
                                hist_rr = self._dynamic_rr(hist_grade, hist_conf, timeframe=tf)
                                actual_option = self._simulate_actual_option_exit(
                                    option_df if "option_df" in locals() else pd.DataFrame(),
                                    s1.timestamp,
                                    effective_exit_ts,
                                    sl_price,
                                    hist_rr,
                                    natural_sl_distance=natural_sl_distance,
                                    max_sl_pct=scanner.options_sl_pct / 100.0,
                                )
                                if actual_option:
                                    entry_premium = actual_option["entry"]
                                    sl_price = actual_option["stop"]
                                    raw_exit_premium = actual_option["exit"]
                                    exit_premium = raw_exit_premium
                                    exit_time = actual_option["exit_time"]
                                    is_sl_hit = actual_option["reason"] == "SL HIT"
                                else:
                                    spot_diff = raw_exit_spot - s1.price if direction == "LONG" else s1.price - raw_exit_spot
                                    raw_exit_premium = entry_premium + (spot_diff * sim_multiplier)
                                    
                                    time_held_hours = (effective_exit_ts - entry_ts).total_seconds() / 3600.0
                                    if time_held_hours > 1.0 and abs(spot_diff) < (s1.atr_value * 0.5):
                                        time_held_hours = 1.0
                                        spot_diff = 0
                                        raw_exit_spot = s1.price
                                        
                                    if time_held_hours > 0:
                                        dte = scanner.expiry.get_dte(instrument, s1.timestamp.date())
                                        # Allow 0 or 1 to exist to apply high expiry day decay if not rolled over
                                        actual_dte = max(0, dte)
                                        theta_rate = 0.05 if actual_dte == 0 else (0.02 if actual_dte == 1 else 0.005)
                                        theta_decay = entry_premium * theta_rate * time_held_hours
                                        if spot_diff > 0:
                                            theta_decay *= 0.5 
                                        raw_exit_premium -= theta_decay
                                        
                                    raw_exit_premium -= (entry_premium * 0.002)
                                    raw_exit_premium = max(0.05, raw_exit_premium)
                            else:
                                raw_exit_premium = raw_exit_spot
                            
                            is_sl_hit = bool(actual_option and actual_option["reason"] == "SL HIT")
                            fut_target_hit = False
                            exit_premium = raw_exit_premium
                            exit_time = actual_option["exit_time"] if actual_option else effective_exit_ts
                            
                            candles_1min = scanner.candles.get_candles(instrument, "1min")
                            
                            if actual_option is None and candles_1min is not None and not candles_1min.empty:
                                try:
                                    s1_ts = s1.timestamp.replace(tzinfo=None) if s1.timestamp.tzinfo else s1.timestamp
                                    between_candles = candles_1min.loc[s1_ts:effective_exit_ts]
                                    
                                    if not between_candles.empty:
                                        if sim_inst == "FUT":
                                            # Exit parity (2026-07-09 audit): FUT replay
                                            # previously scanned ONLY the stop and rode
                                            # winners to the next flip/day end (zero
                                            # TARGET_HIT among 334 FUT rows) — the same
                                            # intrabar SL/target walk the OPTION replay
                                            # gets applies here now.
                                            fut_rr = self._dynamic_rr(hist_grade, hist_conf, timeframe=tf)
                                            fut_target = (
                                                entry_premium + (index_stop_distance * fut_rr)
                                                if direction == "LONG"
                                                else entry_premium - (index_stop_distance * fut_rr)
                                            )
                                            sim_price, sim_reason, sim_ts = self._simulate_futures_exit_path(
                                                between_candles, entry_premium, sl_price, fut_target, direction
                                            )
                                            if sim_reason == "SL HIT":
                                                is_sl_hit = True
                                                exit_premium = sl_price
                                                exit_time = sim_ts
                                            elif sim_reason == "TARGET_HIT":
                                                fut_target_hit = True
                                                exit_premium = float(sim_price)
                                                exit_time = sim_ts
                                        else:
                                            if direction == "LONG":
                                                all_premiums = entry_premium + ((between_candles['low'] - s1.price) * sim_multiplier)
                                                hit_rows = between_candles[all_premiums < sl_price]
                                                if not hit_rows.empty:
                                                    is_sl_hit = True
                                                    exit_premium = sl_price
                                                    exit_time = hit_rows.index[0]
                                            else:
                                                all_premiums = entry_premium + ((s1.price - between_candles['high']) * sim_multiplier)
                                                hit_rows = between_candles[all_premiums < sl_price]
                                                if not hit_rows.empty:
                                                    is_sl_hit = True
                                                    exit_premium = sl_price
                                                    exit_time = hit_rows.index[0]
                                except Exception as e:
                                    logger.warning(f"⚠️ Granular SL check failed: {e}")
                                    
                            if not is_sl_hit and not fut_target_hit:
                                if sim_inst == "FUT":
                                    if direction == "LONG" and raw_exit_spot < sl_price:
                                        exit_premium = sl_price
                                        is_sl_hit = True
                                    elif direction == "SHORT" and raw_exit_spot > sl_price:
                                        exit_premium = sl_price
                                        is_sl_hit = True
                                else:
                                    if raw_exit_premium < sl_price:
                                        exit_premium = sl_price
                                        is_sl_hit = True

                            if sim_inst == "OPT":
                                gross_pnl = (exit_premium - entry_premium) * qty
                            else:
                                if direction == "LONG":
                                    gross_pnl = (exit_premium - entry_premium) * qty
                                else:
                                    gross_pnl = (entry_premium - exit_premium) * qty
                            
                            sim_charges = estimate_trade_charges(
                                entry_premium,
                                exit_premium,
                                qty,
                                sim_inst,
                                settings,
                            )
                            net_pnl = gross_pnl - sim_charges
                            
                            if actual_option:
                                hist_exit_reason = actual_option["reason"]
                                if crosses_session and hist_exit_reason == "HISTORICAL":
                                    hist_exit_reason = "SESSION_END"
                            else:
                                hist_exit_reason = "SL HIT" if is_sl_hit else (
                                    "TARGET_HIT" if fut_target_hit
                                    else ("SESSION_END" if crosses_session else "HISTORICAL")
                                )
                            hist_rr = self._dynamic_rr(hist_grade, hist_conf, timeframe=tf)
                            hist_lane = self._signal_source_lane(s1, scanner_engine_mode)
                            hist_source_engine = str(getattr(s1, "source_engine", "") or "")
                            hist_source_engines = list(getattr(s1, "source_engines", []) or [])
                            hist_id = self._historical_trade_id("H", instrument, tf, s1, scanner_engine_mode)

                            if not self._historical_signal_date_allowed(scanner, s1.timestamp):
                                logger.info(
                                    f"Skipping in-progress session historical trade before 15:30 handoff: {hist_id}"
                                )
                                continue
                            
                            if any(ct.id == hist_id for ct in scanner.trades.closed_trades):
                                continue
                                
                            if entry_premium <= 0:
                                logger.warning(f"Skipping simulated candidate {hist_id} due to invalid entry premium: {entry_premium}")
                                continue
                                
                            if sim_inst == "OPT":
                                hist_target = actual_option["target"] if actual_option else entry_premium + ((entry_premium - sl_price) * hist_rr)
                            elif direction == "LONG":
                                hist_target = s1.price + (index_stop_distance * hist_rr)
                            else:
                                hist_target = s1.price - (index_stop_distance * hist_rr)
                            
                            e_time = IST.localize(s1.timestamp) if (s1.timestamp and getattr(s1.timestamp, "tzinfo", None) is None) else s1.timestamp
                            ex_time = IST.localize(exit_time) if (exit_time and getattr(exit_time, "tzinfo", None) is None) else exit_time
                            t = Trade(
                                id=hist_id, instrument=instrument, timeframe=tf,
                                direction=direction, entry_price=entry_premium, entry_time=e_time,
                                trailing_stop=sl_price,
                                current_stop=sl_price,
                                lots=hist_lots, lot_size=lot_size, grade=f"{hist_grade} (Hist)", confidence=hist_conf,
                                inst_type=sim_inst, option_type=sim_option_type, atm_strike=hist_strike,
                                rr_ratio=hist_rr, target=hist_target,
                                status="CLOSED", exit_price=exit_premium, exit_time=ex_time,
                                pnl=net_pnl, charges=sim_charges, exit_reason=hist_exit_reason,
                                instrument_multiplier=sim_multiplier, entry_spot=s1.price,
                                spot_stop=s1.trailing_stop,
                                spot_target=(s1.price + (index_stop_distance * hist_rr)) if direction == "LONG" else (s1.price - (index_stop_distance * hist_rr)),
                                trading_symbol=trading_symbol, symbol_token=symbol_token,
                                source_engine=hist_source_engine,
                                source_engines=hist_source_engines,
                                source_lane=hist_lane,
                                data_provenance="BACKTEST",
                            )
                            
                            live_session_day_raw = (
                                scanner._get_current_session_day()
                                if hasattr(scanner, "_get_current_session_day")
                                else datetime.now(IST).date().isoformat()
                            )
                            live_session_day = datetime.fromisoformat(str(live_session_day_raw)).date()
                            if is_live and s1.timestamp.date() == live_session_day:
                                logger.info(
                                    f"Skipping simulated backfill signal {hist_id} in live mode; "
                                    "live dashboard ledger only accepts real post-gate session rows."
                                )
                                continue
                            if is_live and s1.timestamp.date() == live_session_day:
                                exit_candidate = TradeCandidate(
                                    instrument=instrument,
                                    direction=direction,
                                    price=entry_premium,
                                    stop=sl_price,
                                    target=hist_target,
                                    lots=hist_lots,
                                    lot_size=lot_size,
                                    grade=hist_grade,
                                    confidence=hist_conf,
                                    timeframe=tf,
                                    inst_type=sim_inst,
                                    option_type=sim_option_type,
                                    atm_strike=hist_strike,
                                    multiplier=sim_multiplier,
                                    trading_symbol=trading_symbol,
                                    symbol_token=symbol_token,
                                    rr=hist_rr,
                                    score=hist_conf,
                                    reasons=list(hist_reasons or []) + [f"Signal closed by {hist_exit_reason}"],
                                    spot_stop=s1.trailing_stop,
                                    spot_target=(s1.price + (index_stop_distance * hist_rr)) if direction == "LONG" else (s1.price - (index_stop_distance * hist_rr)),
                                    signal_timestamp=s1.timestamp,
                                    current_price=exit_premium,
                                    pnl=net_pnl,
                                    status="EXIT SIGNAL",
                                    action="EXIT",
                                    exit_reason=hist_exit_reason,
                                    source_engine=hist_source_engine,
                                    source_engines=hist_source_engines,
                                    source_lane=hist_lane,
                                )
                                setattr(exit_candidate, "exit_timestamp", exit_time)
                                setattr(exit_candidate, "exit_price", exit_premium)
                                scanner._remember_trade_candidates([exit_candidate])
                                logger.info(f"⛔ Skipping adding historical trade {hist_id} to closed_trades in live mode.")
                            elif self._historical_trade_creation_allowed(scanner):
                                scanner.trades.closed_trades.append(t)
                                if sim_inst == "OPT" and self._hybrid_backtest_dual_futures_enabled(scanner):
                                    shadow_id = f"{hist_id}_FUT"
                                    if not any(ct.id == shadow_id for ct in scanner.trades.closed_trades):
                                        shadow = self._build_historical_futures_shadow_trade(
                                            scanner=scanner,
                                            trade_id=shadow_id,
                                            instrument=instrument,
                                            timeframe=tf,
                                            direction=direction,
                                            signal=s1,
                                            exit_spot=raw_exit_spot,
                                            exit_time=exit_time,
                                            grade=hist_grade,
                                            confidence=hist_conf,
                                            rr=hist_rr,
                                            lot_size=lot_size,
                                            display_suffix="Hist",
                                            default_exit_reason=("SL HIT" if is_sl_hit else "HISTORICAL"),
                                            regime=hist_regime,
                                        )
                                        if shadow:
                                            scanner.trades.closed_trades.append(shadow)
                            else:
                                logger.info(
                                    f"Blocked historical trade creation outside HISTORICAL mode: {hist_id}"
                                )

                # CRITICAL FIX: Only recover signals from TODAY's session.
                    # A signal from a previous day (e.g. 07 May 14:30) would have been 
                    # squared off at session end (15:30). It is NOT a live position.
                    # Master Fix: We DO want to recover trades if the bot crashes during a live/paper session!
                    allow_inferred_recovery = getattr(
                        scanner,
                        "_allow_inferred_signal_recovery",
                        lambda: False,
                    )
                    if is_live and sigs and bool(allow_inferred_recovery()):
                        logger.info(f"Running live recovery for {instrument} {tf}...")
                        last_s = sigs[-1]
                        session_day_raw = (
                            scanner._get_current_session_day()
                            if hasattr(scanner, "_get_current_session_day")
                            else datetime.now(IST).date().isoformat()
                        )
                        session_day = datetime.fromisoformat(str(session_day_raw)).date()
                        try:
                            now_ist = datetime.now(IST)
                            sess_end_str = get_settings().ut_session_end
                            sess_hr, sess_min = map(int, sess_end_str.split(':'))
                            kill_threshold = dtime(sess_hr, sess_min)
                            
                            market_session_active = (
                                now_ist.date() == session_day
                                and now_ist.time() < kill_threshold
                                and bool(scanner.data.is_market_open())
                            )
                        except Exception:
                            now_ist = datetime.now(IST)
                            sess_end_str = get_settings().ut_session_end
                            sess_hr, sess_min = map(int, sess_end_str.split(':'))
                            market_session_active = now_ist.date() == session_day and now_ist.time() < dtime(sess_hr, sess_min)
                        signal_is_from_today = (
                            (scanner.mode == "REAL")
                            and last_s.timestamp.date() == session_day
                            and market_session_active
                        )
                        
                        tf_minutes = {"1min": 1, "5min": 5, "15min": 15}.get(tf, 5)
                        candle_close_time = naive_ts(last_s.timestamp) + timedelta(minutes=tf_minutes)
                        now_naive = naive_ts(datetime.now(IST))
                        
                        recovery_window_ok = True
                        recovery_window_fn = getattr(scanner, "_signal_in_restart_recovery_window", None)
                        if callable(recovery_window_fn):
                            recovery_window_ok = bool(recovery_window_fn(last_s.timestamp, tf))

                        if signal_is_from_today and now_naive >= candle_close_time and recovery_window_ok:
                            direction = "LONG" if last_s.signal_type == "BUY" else "SHORT"
                            logger.info(f"🔄 Recovering Active {direction} state for {instrument} from {last_s.timestamp}")
                            if not scanner.is_warmup:
                                scanner.log_event(f"🔄 Recovered {direction} signal for {instrument} from {last_s.timestamp.strftime('%H:%M')}", "trade")
                            
                            # ── Recovery Cross-TF Guard ──
                            existing = [
                                t
                                for t in scanner.trades.open_trades.values()
                                if t.instrument == instrument and self._is_live_book_trade(t)
                            ]
                            
                            # Check if signal was already processed and closed
                            existing_closed = [
                                t for t in scanner.trades.closed_trades 
                                if t.instrument == instrument and 
                                getattr(t, 'entry_time', None) and
                                (t.entry_time.strftime('%Y%m%d%H%M') == last_s.timestamp.strftime('%Y%m%d%H%M') if isinstance(t.entry_time, datetime) else str(t.entry_time) == last_s.timestamp.strftime('%d %b %H:%M:%S'))
                                and "REPAINT" not in str(getattr(t, "exit_reason", "")).upper()
                            ]
                            if existing_closed:
                                logger.info(f"⛔ Signal for {instrument} at {last_s.timestamp} already processed and closed. Skipping recovery.")
                                continue
                            
                            logger.info(f"🔍 [AUDIT] Recovery logic running for {instrument} {tf}. last_s={last_s.timestamp} {last_s.signal_type}. scanner.inst_pref: {scanner.inst_pref}")
                            
                            # Data Sanity Check: Detect swapped prices!
                            if _is_bfo_instrument(instrument) and last_s.price < 50000:
                                logger.error(
                                    f"🚨 [AUDIT] {instrument} signal has NIFTY price: "
                                    f"{last_s.price}. Skipping recovery."
                                )
                                continue
                            if instrument == "NIFTY" and last_s.price > 50000:
                                logger.error(f"🚨 [AUDIT] NIFTY signal has SENSEX price: {last_s.price}. Skipping recovery.")
                                continue

                            # Strict instrument preference (same rule as missed re-entry / resolver):
                            # OPT → options only, FUT → futures only, AUTO → recovery defaults to OPT.
                            pref = str(getattr(scanner, "inst_pref", "AUTO") or "AUTO").upper()
                            if pref == "OPT":
                                rec_inst_type = "OPT"
                            elif pref == "FUT":
                                rec_inst_type = "FUT"
                            else:
                                rec_inst_type = "OPT"

                            if existing:
                                logger.info(f"🚷 Recovery Guard: Already have an open trade for {instrument}. Skipping recovery.")
                                continue

                            
                            try:
                                live_regime = regime
                            except Exception:
                                live_regime = "UNKNOWN"
                            try:
                                live_confluence = float(getattr(mtf_result, "confluence_score", 0.5) or 0.5)
                            except Exception:
                                live_confluence = 0.5
                            try:
                                live_intel_score = float(intel_score or 0.0)
                            except Exception:
                                live_intel_score = 0.0
                            try:
                                rec_vix_key = last_s.timestamp.strftime("%Y-%m-%d %H:%M:00")
                                rec_vix = scanner._vix_data.get(rec_vix_key)
                                if rec_vix is None:
                                    rec_vix = scanner.data.get_ltp("NSE", "INDIAVIX", "99926017") or 15.0
                            except Exception:
                                rec_vix = 15.0
                            try:
                                rec_grade, rec_conf, _ = scanner.signals._grade_signal(
                                    last_s,
                                    live_confluence,
                                    live_intel_score,
                                    live_regime,
                                    vix_value=rec_vix,
                                )
                            except Exception as exc:
                                logger.warning(f"Recovery quality grading failed for {instrument} {tf}: {exc}")
                                rec_grade, rec_conf, rec_reasons = "C", 0.0, ["Recovery grading failed"]

                            if not self._passes_quality_gate(
                                tf,
                                rec_grade,
                                rec_conf,
                                live_regime,
                                rec_inst_type,
                                get_settings(),
                                adx_value=float(getattr(last_s, "adx_value", 0.0) or 0.0),
                                trend_15m_agrees=self._trend_15m_agrees_at(
                                    mtf_result,
                                    last_s.timestamp,
                                    last_s.signal_type,
                                ),
                            ):
                                logger.info(
                                    f"Recovery Guard: {instrument} {tf} {last_s.signal_type} at {last_s.timestamp} "
                                    f"rejected by live quality gate (grade={rec_grade}, conf={rec_conf:.3f}, regime={live_regime})."
                                )
                                continue

                            # Calculate atm_strike for recovery if it's an Option
                            rec_atm_strike = 0.0
                            rec_option_type = ""
                            rec_trading_symbol = str(getattr(last_s, "trading_symbol", "") or "")
                            rec_symbol_token = str(getattr(last_s, "symbol_token", "") or "")
                            rec_multiplier = 1.0
                            if rec_inst_type == "OPT":
                                strike_interval = cfg.get("strike_interval", 50)
                                rec_atm_strike = round(last_s.price / strike_interval) * strike_interval
                                rec_option_type = "CE" if direction == "LONG" else "PE"
                                # Audit #4: never invent synthetic OPT recovery marks.
                                # Require a verified live premium; REAL also requires a
                                # confirmed broker position before opening a local trade.
                                # Instrument-lock (2026-07-24): under OPT the retained seed
                                # symbol/token may be a FUTURES contract inherited from an
                                # AUTO/hybrid signal. Re-resolve to a genuine option unless
                                # the retained symbol is already an option (…CE/…PE), and
                                # never open OPT on a futures contract.
                                _rec_sym_is_option = str(rec_trading_symbol).upper().endswith(("CE", "PE"))
                                if not rec_symbol_token or not rec_trading_symbol or not _rec_sym_is_option:
                                    opt_info = None
                                    try:
                                        opt_info = scanner.data.get_option_token(
                                            instrument, rec_atm_strike, rec_option_type
                                        )
                                    except Exception as exc:
                                        logger.warning(
                                            f"Recovery Guard: option token lookup failed for "
                                            f"{instrument} {rec_atm_strike} {rec_option_type}: {exc}"
                                        )
                                    if opt_info:
                                        rec_trading_symbol = str(opt_info.get("symbol") or "")
                                        rec_symbol_token = str(opt_info.get("token") or "")
                                if not rec_symbol_token or not str(rec_trading_symbol).upper().endswith(("CE", "PE")):
                                    logger.warning(
                                        f"Recovery Guard: skip OPT recovery for {instrument} {tf} — "
                                        f"no genuine option contract (refusing to open OPT on "
                                        f"'{rec_trading_symbol or 'unknown'}')."
                                    )
                                    continue
                                exchange = "BFO" if _is_bfo_instrument(instrument) else "NFO"
                                rec_entry = float(
                                    self._contract_ltp(
                                        scanner, exchange, rec_trading_symbol, rec_symbol_token
                                    )
                                    or 0.0
                                )
                                if rec_entry <= 0:
                                    logger.warning(
                                        f"Recovery Guard: skip OPT recovery for {instrument} {tf} — "
                                        f"no verified LTP for {rec_trading_symbol}."
                                    )
                                    continue
                                try:
                                    dte_val = scanner.expiry.get_dte(instrument, last_s.timestamp.date())
                                except Exception:
                                    dte_val = 1
                                rec_multiplier = self._option_signed_delta(
                                    last_s.price,
                                    rec_atm_strike,
                                    rec_option_type,
                                    {},
                                    dte_val,
                                    0.5 if rec_option_type == "CE" else -0.5,
                                )
                                if str(getattr(scanner, "mode", "") or "").upper() == "REAL":
                                    broker = getattr(scanner.trades, "broker", None)
                                    positions_fn = getattr(broker, "get_positions", None) if broker else None
                                    broker_qty = 0
                                    broker_avg = 0.0
                                    if callable(positions_fn):
                                        try:
                                            pos_resp = positions_fn() or {}
                                            for row in (pos_resp.get("data") or []):
                                                sym = str(
                                                    row.get("tradingsymbol")
                                                    or row.get("symbol")
                                                    or ""
                                                )
                                                tok = str(row.get("symboltoken") or row.get("token") or "")
                                                if tok == rec_symbol_token or (
                                                    rec_trading_symbol and rec_trading_symbol in sym
                                                ):
                                                    net = int(
                                                        float(
                                                            row.get("netqty")
                                                            or row.get("quantity")
                                                            or 0
                                                        )
                                                    )
                                                    broker_qty = abs(net)
                                                    broker_avg = float(
                                                        row.get("avgnetprice")
                                                        or row.get("averageprice")
                                                        or row.get("avgprice")
                                                        or 0.0
                                                    )
                                                    break
                                        except Exception as exc:
                                            logger.warning(
                                                f"Recovery Guard: broker position check failed: {exc}"
                                            )
                                    if broker_qty <= 0:
                                        logger.critical(
                                            f"Recovery Guard: REAL mode refuses synthetic OPT open for "
                                            f"{instrument} {rec_trading_symbol} — no broker position. "
                                            "Reconcile manually; not inventing a local book."
                                        )
                                        continue
                                    if broker_avg > 0:
                                        rec_entry = broker_avg
                            else:
                                rec_entry = float(last_s.price or 0.0)
                                if rec_entry <= 0:
                                    continue

                            # Use the same quality-scaled RR as live so a recovered
                            # position carries the target it would have had originally.
                            rec_rr = self._dynamic_rr(rec_grade, rec_conf, timeframe=tf)
                            if rec_inst_type == "OPT":
                                rec_risk = max(0.5, rec_entry * (scanner.options_sl_pct / 100.0))
                                rec_stop = max(0.05, rec_entry - rec_risk)
                                rec_target = rec_entry + (rec_risk * rec_rr)
                                requested_lots = int(
                                    getattr(scanner, "user_lots", {}).get(instrument, 1) or 1
                                )
                            else:
                                rec_fut_sl_pct = futures_stop_tolerance_pct(
                                    scanner.futures_sl_pct,
                                    live_regime,
                                )
                                rec_risk = max(1.0, rec_entry * (rec_fut_sl_pct / 100.0))
                                if direction == "LONG":
                                    rec_stop = max(last_s.trailing_stop, rec_entry - rec_risk)
                                    rec_target = rec_entry + ((rec_entry - rec_stop) * rec_rr)
                                else:
                                    rec_stop = min(last_s.trailing_stop, rec_entry + rec_risk)
                                    rec_target = rec_entry - ((rec_stop - rec_entry) * rec_rr)
                                requested_lots = int(
                                    getattr(scanner, "user_lots_fut", {}).get(instrument, 1) or 1
                                )
                            size_result = resolve_trade_lots(
                                settings=settings,
                                inst_type=rec_inst_type,
                                requested_lots=requested_lots,
                                lot_size=lot_size,
                                entry_price=rec_entry,
                                stop_price=rec_stop,
                                # Recovery re-establishes an EXISTING position; there is
                                # no fresh intelligence reading behind it and conviction
                                # must not resize what is already on.
                                intel_score=0.0,
                            )
                            rec_lots = int(size_result.lots or 0)
                            if rec_lots < 1:
                                logger.info(
                                    f"Recovery Guard: skip {instrument} {tf} — {size_result.reason}"
                                )
                                continue
                            scanner.trades.open_trade(
                                instrument=instrument, timeframe=tf, direction=direction,
                                price=rec_entry,
                                trailing_stop=rec_stop,
                                target=rec_target,
                                lots=rec_lots, lot_size=lot_size, grade=rec_grade,
                                confidence=rec_conf,
                                entry_time=last_s.timestamp,
                                instrument_multiplier=rec_multiplier,
                                option_type=rec_option_type,
                                atm_strike=rec_atm_strike,
                                trading_symbol=rec_trading_symbol,
                                symbol_token=rec_symbol_token,
                                inst_type=rec_inst_type,
                                entry_spot=last_s.price, 
                                is_explosive_bypass=bool(
                                    float(getattr(last_s, "adx_value", 0.0) or 0.0)
                                    >= float(getattr(settings, "momentum_override_threshold", 25.0) or 25.0)
                                ),
                                is_recovery=True,
                                market_regime=str(live_regime or ""),
                                data_provenance="RECOVERY",
                                intelligence_score=float(live_intel_score or 0.0),
                                strategy_revision=str(getattr(settings, "strategy_revision", "") or ""),
                                strategy_direction_mode=str(getattr(settings, "strategy_direction_mode", "") or ""),
                                source_engine=str(getattr(last_s, "source_engine", "") or ""),
                                source_engines=list(getattr(last_s, "source_engines", []) or []),
                                source_lane=self._signal_source_lane(last_s, scanner_engine_mode),
                                initial_stop=rec_stop,
                                scalp_lock_mode=False,
                            )
                            if scanner.trades.open_trades:
                                active_tid = list(scanner.trades.open_trades.keys())[-1]
                                active_trade = scanner.trades.open_trades[active_tid]
                                ts = pd.Timestamp(last_s.timestamp)
                                if ts.tzinfo is not None:
                                    active_trade.entry_time = ts.tz_convert(IST).tz_localize(None).to_pydatetime()
                                else:
                                    active_trade.entry_time = ts.to_pydatetime()
                                # Marks already set from verified LTP / broker avg — do not overwrite with estimates.
                                active_trade.entry_price = rec_entry
                                active_trade.current_price = rec_entry
                                active_trade.instrument_multiplier = rec_multiplier
                                active_trade.current_stop = rec_stop
                                active_trade.target = rec_target
                        elif last_s.timestamp.date() >= trade_cutoff:
                            # ═══ SYNTHETIC SESSION-END CLOSE ═══
                            # Signal from a previous day — create a closed trade at session end price
                            
                            # ── Grade Filter: Evaluate what we would trade ──
                            eod_regime = "UNKNOWN"
                            try:
                                eod_df = scanner.candles.get_candles(instrument, "5min")
                                if eod_df is not None and not eod_df.empty:
                                    eod_regime_res = scanner.intel.regime.detect(
                                        eod_df[eod_df.index <= last_s.timestamp],
                                        instrument,
                                        "5min",
                                    )
                                    eod_regime = eod_regime_res.get("regime", "UNKNOWN")
                            except Exception as exc:
                                logger.debug(f"EOD option regime fallback for {instrument}: {exc}")

                            eod_grade, eod_conf, _ = scanner.signals._grade_signal(last_s, 0.5, 0.0, regime=eod_regime)
                            if eod_grade == "C":
                                continue
                                
                            # Historical concurrency is applied after all workers finish.
                            # Check for existing manual exits before applying EOD close
                            already_exited = False
                            book = getattr(scanner, "_session_trade_candidates", {}).get(instrument, {})
                            for c in book.values():
                                if getattr(c, "action", "ENTRY") == "EXIT" and c.timeframe == tf and c.direction == ("LONG" if last_s.signal_type == "BUY" else "SHORT"):
                                    if getattr(c, "signal_timestamp", None) == last_s.timestamp:
                                        already_exited = True
                                        break
                            if already_exited:
                                continue

                            direction = "LONG" if last_s.signal_type == "BUY" else "SHORT"
                            sig_date = last_s.timestamp.date()
                            # Quality-scaled RR (same as live) so EOD-simulated targets
                            # match the live system rather than a divergent flat 1.5.
                            eod_rr = self._dynamic_rr(eod_grade, eod_conf, timeframe=tf)

                            # Find the closing price on that date from candle data
                            candles_for_tf = scanner.candles.get_candles(instrument, tf)
                            eod_price = last_s.price  # fallback
                            if candles_for_tf is not None and not candles_for_tf.empty:
                                day_candles = candles_for_tf[candles_for_tf.index.date == sig_date]
                                force_exit_time = datetime.strptime(
                                    str(getattr(settings, "ut_force_exit_time", "15:25") or "15:25"),
                                    "%H:%M",
                                ).time()
                                day_candles = day_candles[day_candles.index.time <= force_exit_time]
                                if not day_candles.empty:
                                    eod_price = day_candles['close'].iloc[-1]
                            
                            eod_lane = self._signal_source_lane(last_s, scanner_engine_mode)
                            eod_source_engine = str(getattr(last_s, "source_engine", "") or "")
                            eod_source_engines = list(getattr(last_s, "source_engines", []) or [])
                            eod_id = self._historical_trade_id("EOD", instrument, tf, last_s, scanner_engine_mode, fmt="%m%d%H%M")
                            if not self._historical_signal_date_allowed(scanner, last_s.timestamp):
                                logger.info(
                                    f"Skipping in-progress session historical EOD trade before 15:30 handoff: {eod_id}"
                                )
                                continue
                            if not any(ct.id == eod_id for ct in scanner.trades.closed_trades):
                                eod_exit_time = datetime.combine(sig_date, force_exit_time)
                                e_time = IST.localize(last_s.timestamp) if last_s.timestamp.tzinfo is None else last_s.timestamp
                                ex_time = IST.localize(eod_exit_time) if eod_exit_time.tzinfo is None else eod_exit_time
                                
                                
                                # ═══ EOD Mode-Aware Intelligence ═══
                                if scanner.inst_pref == "FUT":
                                    sim_inst = "FUT"
                                    sim_option_type = ""
                                    sim_multiplier = 1.0
                                    entry_premium = last_s.price
                                    exit_premium = eod_price
                                elif scanner.inst_pref == "OPT":
                                    sim_inst = "OPT"
                                    sim_option_type = "CE" if direction == "LONG" else "PE"
                                    sim_multiplier = 0.5
                                    entry_premium = _estimate_opt_premium(last_s.price, scanner, instrument, _sig_date(last_s))
                                    spot_diff = eod_price - last_s.price if direction == "LONG" else last_s.price - eod_price
                                    exit_premium = entry_premium + (spot_diff * sim_multiplier)
                                else: # AUTO
                                    eod_option_allowed = self._option_grade_allowed(
                                        eod_grade,
                                        eod_conf,
                                        eod_regime,
                                        adx_value=getattr(last_s, "adx_value", 0.0),
                                        intel_score=intel_score,
                                        signal_score=eod_conf,
                                        timeframe=tf,
                                    )
                                    sim_inst = "OPT" if eod_option_allowed else "FUT"
                                    sim_option_type = ("CE" if direction == "LONG" else "PE") if sim_inst == "OPT" else ""
                                    sim_multiplier = 0.5 if sim_inst == "OPT" else 1.0
                                    if sim_inst == "OPT":
                                        entry_premium = _estimate_opt_premium(last_s.price, scanner, instrument, _sig_date(last_s))
                                        spot_diff = eod_price - last_s.price if direction == "LONG" else last_s.price - eod_price
                                        exit_premium = entry_premium + (spot_diff * sim_multiplier)
                                    else:
                                        entry_premium = last_s.price
                                        exit_premium = eod_price

                                sim_atm_strike = 0.0
                                trading_symbol = ""
                                symbol_token = ""
                                eod_exit_reason = "SESSION_END"
                                eod_stop = last_s.trailing_stop
                                if sim_inst == "FUT":
                                    eod_fut_sl_pct = futures_stop_tolerance_pct(
                                        scanner.futures_sl_pct,
                                        eod_regime,
                                    )
                                    max_fut_sl_dist = entry_premium * (eod_fut_sl_pct / 100.0)
                                    if direction == "LONG":
                                        eod_stop = max(last_s.trailing_stop, entry_premium - max_fut_sl_dist)
                                        eod_target = entry_premium + ((entry_premium - eod_stop) * eod_rr)
                                    else:
                                        eod_stop = min(last_s.trailing_stop, entry_premium + max_fut_sl_dist)
                                        eod_target = entry_premium - ((eod_stop - entry_premium) * eod_rr)
                                else:
                                    eod_target = last_s.price + (abs(last_s.price - last_s.trailing_stop) * eod_rr) if direction == "LONG" else last_s.price - (abs(last_s.price - last_s.trailing_stop) * eod_rr)

                                if sim_inst == "OPT":
                                    option_pref = (getattr(scanner, "inst_pref", "AUTO") or "AUTO").upper()
                                    _, no_entry_after = _entry_cutoff(settings, tf)
                                    eod_option_quality_ok = (
                                        self._option_grade_allowed(
                                            eod_grade,
                                            eod_conf,
                                            eod_regime,
                                            adx_value=getattr(last_s, "adx_value", 0.0),
                                            intel_score=intel_score,
                                            signal_score=eod_conf,
                                            timeframe=tf,
                                        )
                                        and last_s.timestamp.time() <= no_entry_after
                                    )
                                    if not eod_option_quality_ok:
                                        if option_pref == "OPT":
                                            logger.info(f"Skipping EOD option below premium quality gate at {last_s.timestamp}")
                                            continue
                                        logger.info(
                                            f"Falling back to FUT for EOD {instrument} {tf} at {last_s.timestamp}: "
                                            f"option gate failed but AUTO/HYBRID should preserve the futures setup."
                                        )
                                        sim_inst = "FUT"
                                        sim_option_type = ""
                                        sim_multiplier = 1.0
                                        entry_premium = last_s.price
                                        exit_premium = eod_price
                                        eod_fut_sl_pct = futures_stop_tolerance_pct(
                                            scanner.futures_sl_pct,
                                            eod_regime,
                                        )
                                        max_fut_sl_dist = entry_premium * (eod_fut_sl_pct / 100.0)
                                        if direction == "LONG":
                                            eod_stop = max(last_s.trailing_stop, entry_premium - max_fut_sl_dist)
                                            eod_target = entry_premium + ((entry_premium - eod_stop) * eod_rr)
                                        else:
                                            eod_stop = min(last_s.trailing_stop, entry_premium + max_fut_sl_dist)
                                            eod_target = entry_premium - ((eod_stop - entry_premium) * eod_rr)

                                if sim_inst == "OPT":
                                    sim_atm_strike = self._historical_option_strike(instrument, direction, last_s.price, cfg)
                                    option_df, trading_symbol, symbol_token = self._load_option_candles(
                                        scanner, instrument, sim_atm_strike, sim_option_type, "1min", sig_date,
                                        now_dt=getattr(last_s, "timestamp", None)
                                    )
                                    history_mode = getattr(settings, "ut_option_history_mode", "fetch_or_skip")
                                    if option_df.empty and history_mode == "fetch_or_skip":
                                        option_pref = (getattr(scanner, "inst_pref", "AUTO") or "AUTO").upper()
                                        if option_pref == "OPT":
                                            logger.info(f"Skipping EOD option {instrument} {sim_option_type} {sim_atm_strike} at {last_s.timestamp}: no real option candles.")
                                            continue
                                        logger.info(
                                            f"Falling back to FUT for EOD {instrument} {tf} at {last_s.timestamp}: "
                                            "no real option candles in AUTO/HYBRID."
                                        )
                                        sim_inst = "FUT"
                                        sim_option_type = ""
                                        sim_multiplier = 1.0
                                        entry_premium = last_s.price
                                        exit_premium = eod_price
                                        eod_fut_sl_pct = futures_stop_tolerance_pct(
                                            scanner.futures_sl_pct,
                                            eod_regime,
                                        )
                                        max_fut_sl_dist = entry_premium * (eod_fut_sl_pct / 100.0)
                                        if direction == "LONG":
                                            eod_stop = max(last_s.trailing_stop, entry_premium - max_fut_sl_dist)
                                            eod_target = entry_premium + ((entry_premium - eod_stop) * eod_rr)
                                        else:
                                            eod_stop = min(last_s.trailing_stop, entry_premium + max_fut_sl_dist)
                                            eod_target = entry_premium - ((eod_stop - entry_premium) * eod_rr)

                                    opt_sl_price = max(
                                        0.05,
                                        entry_premium - max(0.05, entry_premium * (scanner.options_sl_pct / 100.0)),
                                    )
                                    if sim_inst == "OPT" and not option_df.empty:
                                        est_entry = _estimate_opt_premium(last_s.price, scanner, instrument, _sig_date(last_s))
                                        spot_stop_dist = abs(float(last_s.price) - float(last_s.trailing_stop))
                                        natural_sl_premium_dist = abs(spot_stop_dist * sim_multiplier)
                                        max_sl_dist = est_entry * (scanner.options_sl_pct / 100.0)
                                        effective_sl_dist = max(0.05, min(natural_sl_premium_dist, max_sl_dist))
                                        opt_sl_price = max(0.05, est_entry - effective_sl_dist)
                                        actual_eod = self._simulate_actual_option_exit(
                                            option_df,
                                            last_s.timestamp,
                                            eod_exit_time,
                                            opt_sl_price,
                                            eod_rr,
                                            natural_sl_distance=natural_sl_premium_dist,
                                            max_sl_pct=scanner.options_sl_pct / 100.0,
                                        )
                                        if actual_eod:
                                            entry_premium = actual_eod["entry"]
                                            opt_sl_price = actual_eod["stop"]
                                            exit_premium = actual_eod["exit"]
                                            eod_target = actual_eod["target"]
                                            eod_exit_time = actual_eod["exit_time"]
                                            ex_time = IST.localize(eod_exit_time) if (eod_exit_time and getattr(eod_exit_time, "tzinfo", None) is None) else eod_exit_time
                                            eod_exit_reason = actual_eod["reason"] if actual_eod["reason"] != "HISTORICAL" else "SESSION_END"
                                        else:
                                            fallback_risk = max(0.5, est_entry - opt_sl_price)
                                            eod_target = est_entry + (fallback_risk * eod_rr)
                                    else:
                                        fallback_risk = max(0.5, entry_premium - opt_sl_price)
                                        eod_target = entry_premium + (fallback_risk * eod_rr)

                                # Check quality gate
                                if not self._passes_quality_gate(
                                    tf,
                                    eod_grade,
                                    eod_conf,
                                    eod_regime,
                                    sim_inst,
                                    settings,
                                    trend_15m_agrees=self._trend_15m_agrees_at(
                                        mtf_result,
                                        last_s.timestamp,
                                        last_s.signal_type,
                                    ),
                                ):
                                    logger.info(f"⭐️ Skipping EOD {instrument} {tf} signal due to quality gate at {last_s.timestamp}")
                                    continue

                                eod_lots = getattr(scanner, "user_lots_fut", {}).get(instrument, 1) if sim_inst == "FUT" else getattr(scanner, "user_lots", {}).get(instrument, 1)
                                sim_charges = estimate_trade_charges(
                                    last_s.price if sim_inst == "FUT" else entry_premium,
                                    exit_premium,
                                    eod_lots * lot_size,
                                    sim_inst,
                                    settings,
                                )
                                if sim_inst == "FUT":
                                    if direction == "LONG" and eod_price <= eod_stop:
                                        exit_premium = eod_stop
                                        eod_exit_reason = "SL HIT"
                                    elif direction == "SHORT" and eod_price >= eod_stop:
                                        exit_premium = eod_stop
                                        eod_exit_reason = "SL HIT"
                                    if direction == "LONG":
                                        eod_pnl = (exit_premium - last_s.price) * (eod_lots * lot_size)
                                    else:
                                        eod_pnl = (last_s.price - exit_premium) * (eod_lots * lot_size)
                                    eod_pnl -= sim_charges
                                else:
                                    eod_pnl = (exit_premium - entry_premium) * (eod_lots * lot_size)
                                    eod_pnl -= sim_charges

                                t = Trade(
                                    id=eod_id, instrument=instrument, timeframe=tf,
                                    direction=direction, entry_price=entry_premium, entry_time=e_time,
                                    trailing_stop=(opt_sl_price if sim_inst == "OPT" else eod_stop),
                                    current_stop=(opt_sl_price if sim_inst == "OPT" else eod_stop),
                                    lots=eod_lots, lot_size=lot_size, grade=f"{eod_grade} (EOD, Hist)", confidence=eod_conf,
                                    inst_type=sim_inst, option_type=sim_option_type, rr_ratio=1.5,
                                    target=eod_target,
                                    status="CLOSED", exit_price=exit_premium, exit_time=ex_time,
                                    pnl=eod_pnl, charges=sim_charges, exit_reason=eod_exit_reason,
                                    instrument_multiplier=sim_multiplier, entry_spot=last_s.price,
                                    spot_stop=last_s.trailing_stop,
                                    spot_target=last_s.price + (abs(last_s.price - last_s.trailing_stop) * eod_rr) if direction == "LONG" else last_s.price - (abs(last_s.price - last_s.trailing_stop) * eod_rr),
                                    atm_strike=sim_atm_strike,
                                    trading_symbol=trading_symbol, symbol_token=symbol_token,
                                    source_engine=eod_source_engine,
                                    source_engines=eod_source_engines,
                                    source_lane=eod_lane,
                                    data_provenance="BACKTEST",
                                )
                                live_session_day_raw = (
                                    scanner._get_current_session_day()
                                    if hasattr(scanner, "_get_current_session_day")
                                    else datetime.now(IST).date().isoformat()
                                )
                                live_session_day = datetime.fromisoformat(str(live_session_day_raw)).date()
                                if is_live and last_s.timestamp.date() == live_session_day:
                                    logger.info(
                                        f"Skipping simulated EOD backfill signal {eod_id} in live mode; "
                                        "live dashboard ledger only accepts real post-gate session rows."
                                    )
                                    continue
                                if is_live and last_s.timestamp.date() == live_session_day:
                                    exit_candidate = TradeCandidate(
                                        instrument=instrument,
                                        direction=direction,
                                        price=entry_premium,
                                        stop=(opt_sl_price if sim_inst == "OPT" else eod_stop),
                                        target=eod_target,
                                        lots=eod_lots,
                                        lot_size=lot_size,
                                        grade=eod_grade,
                                        confidence=eod_conf,
                                        timeframe=tf,
                                        inst_type=sim_inst,
                                        option_type=sim_option_type,
                                        atm_strike=sim_atm_strike,
                                        multiplier=sim_multiplier,
                                        trading_symbol=trading_symbol,
                                        symbol_token=symbol_token,
                                        rr=1.5,
                                        score=eod_conf,
                                        reasons=[f"Signal closed by {eod_exit_reason}"],
                                        spot_stop=last_s.trailing_stop,
                                        spot_target=(
                                            last_s.price + (abs(last_s.price - last_s.trailing_stop) * eod_rr)
                                            if direction == "LONG"
                                            else last_s.price - (abs(last_s.price - last_s.trailing_stop) * eod_rr)
                                        ),
                                        signal_timestamp=last_s.timestamp,
                                        current_price=exit_premium,
                                        pnl=eod_pnl,
                                        status="EXIT SIGNAL",
                                        action="EXIT",
                                        exit_reason=eod_exit_reason,
                                        source_engine=eod_source_engine,
                                        source_engines=eod_source_engines,
                                        source_lane=eod_lane,
                                    )
                                    setattr(exit_candidate, "exit_timestamp", eod_exit_time)
                                    setattr(exit_candidate, "exit_price", exit_premium)
                                    scanner._remember_trade_candidates([exit_candidate])
                                elif self._historical_trade_creation_allowed(scanner):
                                    scanner.trades.closed_trades.append(t)
                                    if sim_inst == "OPT" and self._hybrid_backtest_dual_futures_enabled(scanner):
                                        shadow_id = f"{eod_id}_FUT"
                                        if not any(ct.id == shadow_id for ct in scanner.trades.closed_trades):
                                            shadow = self._build_historical_futures_shadow_trade(
                                                scanner=scanner,
                                                trade_id=shadow_id,
                                                instrument=instrument,
                                                timeframe=tf,
                                                direction=direction,
                                                signal=last_s,
                                                exit_spot=eod_price,
                                                exit_time=eod_exit_time,
                                                grade=eod_grade,
                                                confidence=eod_conf,
                                                rr=1.5,
                                                lot_size=lot_size,
                                                display_suffix="EOD, Hist",
                                                default_exit_reason=eod_exit_reason,
                                                regime=eod_regime,
                                            )
                                            if shadow:
                                                scanner.trades.closed_trades.append(shadow)
                                else:
                                    logger.info(
                                        f"Blocked historical EOD trade creation outside HISTORICAL mode: {eod_id}"
                                    )

                                logger.debug(f"📊 Synthetic EOD close: {eod_id} | PnL: ₹{eod_pnl:,.0f}")

                # Update lock with precise last signal time
                # CRITICAL FIX: Use the SECOND-TO-LAST signal's timestamp
                # The LAST signal may be unpaired (no exit yet) — if we set lock
                # to its timestamp, the live detector won't see it as "new"
                # (because s_ts > l_ts fails when they're equal).
                if len(sigs) >= 2:
                    scanner._last_signal_time[dedup_key] = naive_ts(sigs[-2].timestamp)
                elif sigs:
                    scanner._last_signal_time[dedup_key] = naive_ts(sigs[-1].timestamp - pd.Timedelta(seconds=1))
                else:
                    scanner._last_signal_time[dedup_key] = naive_ts(datetime.now(IST)) if is_live else datetime.min
                
            last_time = naive_ts(scanner._last_signal_time[dedup_key])
            
            for sig in sigs:
                # strategy_direction_mode was only ever persisted and displayed, never
                # enforced, so LONG_ONLY / SHORT_ONLY did not actually restrict trading.
                # No-op under the default LONG_SHORT.
                _dir_ok, _dir_reason = self._direction_mode_allows(sig.signal_type, settings)
                if not _dir_ok:
                    scanner.log_signal_decision_once(
                        f"{instrument}|{tf}|{sig.signal_type}|direction-mode",
                        f"Signal rejected: {instrument} {sig.signal_type} {tf} - {_dir_reason}",
                        "trade",
                    )
                    continue
                s_ts = naive_ts(sig.timestamp)
                l_ts = last_time
                signal_identity = f"{s_ts.isoformat()}|{sig.signal_type}"
                last_identity = getattr(scanner, "_last_signal_identity", {}).get(dedup_key, "")
                now_naive = naive_ts(datetime.now(IST))
                tf_minutes = {"1min": 1, "5min": 5, "15min": 15}.get(tf, 5)
                pending_key = (
                    f"{instrument}_{tf}_"
                    f"{'LONG' if sig.signal_type == 'BUY' else 'SHORT'}"
                )
                recheck_eligible = (
                    getattr(scanner, "_live_signal_recheck_eligible", {}).get(dedup_key)
                    == signal_identity
                )
                last_recheck = getattr(scanner, "_live_signal_recheck_at", {}).get(
                    dedup_key,
                    datetime.min,
                )
                signal_mode_getter = getattr(settings, "get_signal_fire_mode", None)
                intrabar_signals_enabled = (
                    signal_mode_getter() == "INTRACANDLE"
                    if callable(signal_mode_getter)
                    else bool(getattr(settings, "live_intrabar_signal_entries", True))
                )
                recheck_due = is_live and intrabar_signals_enabled and self._live_signal_recheck_due(
                    s_ts,
                    tf,
                    now_naive,
                    recheck_eligible,
                    pending_key in getattr(scanner, "_pending_live_signals", {}),
                    naive_ts(last_recheck),
                    float(getattr(settings, "live_signal_recheck_seconds", 5.0) or 5.0),
                )
                post_repaint_recheck_due = False
                post_repaint_recheck = getattr(scanner, "_post_repaint_recheck_due", None)
                if is_live and intrabar_signals_enabled and callable(post_repaint_recheck):
                    post_repaint_recheck_due = bool(
                        post_repaint_recheck(
                            instrument,
                            tf,
                            sig.signal_type,
                            s_ts,
                            now_naive,
                            pending_key in getattr(scanner, "_pending_live_signals", {}),
                        )
                    )

                is_new_signal = (
                    self._live_signal_is_new(s_ts, sig.signal_type, l_ts, last_identity)
                    if is_live
                    else s_ts > l_ts
                )
                is_new_signal = is_new_signal or recheck_due or post_repaint_recheck_due
                if is_new_signal:
                    if not hasattr(scanner, "_live_signal_recheck_at"):
                        scanner._live_signal_recheck_at = {}
                    scanner._live_signal_recheck_at[dedup_key] = now_naive
                    # ── Ghost Signal Age Guard (live mode only) ──
                    # Even though this signal is "new" per the dedup lock, in
                    # live/real mode we must verify its candle is actually
                    # current.  A signal from a candle that closed minutes ago
                    # is a historical-backfill echo, not a live signal.
                    if is_live:
                        deadline_fn = getattr(scanner, "_live_signal_processing_deadline", None)
                        deadline = (
                            deadline_fn(s_ts, tf)
                            if callable(deadline_fn)
                            else s_ts + timedelta(minutes=tf_minutes, seconds=90)
                        )
                        if deadline is not None and now_naive > deadline:
                            logger.warning(
                                f"👻 [GHOST GUARD] Skipping stale backfill signal "
                                f"{instrument} {tf} at {s_ts} "
                                f"(closed_age={(now_naive - (s_ts + timedelta(minutes=tf_minutes))).total_seconds():.0f}s). "
                                f"Not a live signal."
                            )
                            emit_once = getattr(scanner, "log_signal_decision_once", None)
                            if callable(emit_once):
                                emit_once(
                                    "stale-backfill-summary",
                                    "Live guard ignored stale historical signal echo(s); details retained in the server log.",
                                    "data",
                                )
                            # Still advance the dedup lock so we don't re-check. Keep
                            # every identity at this timestamp: quad-prime can retain an
                            # opposite source-family arrow on the same bar.
                            self._remember_live_signal_identity(
                                scanner, dedup_key, s_ts, signal_identity
                            )
                            getattr(scanner, "_live_signal_recheck_eligible", {}).pop(dedup_key, None)
                            continue

                        candle_close_time = s_ts + timedelta(minutes=tf_minutes)
                        intrabar_enabled = intrabar_signals_enabled
                        if (
                            not intrabar_enabled
                            and not self._live_signal_candle_is_closed(s_ts, tf, now_naive)
                        ):
                            logger.debug(
                                f"[PARTIAL CANDLE] Deferring live signal processing for {instrument} {tf} at {s_ts}. "
                                f"Candle closes at {candle_close_time}."
                            )
                            emit_once = getattr(scanner, "log_signal_decision_once", None)
                            if callable(emit_once):
                                emit_once(
                                    f"{instrument}|{tf}|{sig.signal_type}|{s_ts.isoformat(timespec='seconds')}|partial",
                                    f"Signal waiting: {instrument} {sig.signal_type} {tf} @ {sig.price:.2f} - partial candle deferred until {candle_close_time.strftime('%H:%M:%S')}",
                                    "trade",
                                )
                            continue

                    no_entry_label, no_entry_after = _entry_cutoff(settings, tf)
                    is_after_no_entry = tf in ["1min", "5min", "15min"] and sig.timestamp.time() >= no_entry_after
                    volume_spike_metrics = None
                    if is_live and tf in {"1min", "5min"}:
                        volume_spike_metrics = scanner._detect_volume_spike_entry(
                            instrument,
                            tf,
                            sig.signal_type,
                            adx_value=float(getattr(sig, "adx_value", 0.0) or 0.0),
                            intel_score=float(intel_score or 0.0),
                            as_of=sig.timestamp,
                        )
                    setattr(sig, "volume_spike_entry", bool(volume_spike_metrics))
                    setattr(
                        sig,
                        "volume_spike_metrics",
                        dict(volume_spike_metrics or {}),
                    )
                    ts_str = sig.timestamp.strftime("%Y-%m-%d %H:%M:00")
                    vix_value = scanner._vix_data.get(ts_str)
                    if vix_value is None:
                        vix_value = scanner.data.get_ltp("NSE", "INDIAVIX", "99926017") or 15.0

                    if is_live:
                        raw_key = f"{instrument}|{tf}|{sig.signal_type}|{s_ts.isoformat(timespec='seconds')}"
                        seen_raw = getattr(scanner, "_raw_utbot_activity_keys", None)
                        if seen_raw is None:
                            scanner._raw_utbot_activity_keys = set()
                            seen_raw = scanner._raw_utbot_activity_keys
                        if raw_key not in seen_raw:
                            seen_raw.add(raw_key)
                            if len(seen_raw) > 500:
                                scanner._raw_utbot_activity_keys = set(list(seen_raw)[-250:])
                            # Keep no-spike 1min raw detections off the live tape: they
                            # cannot enter without its configured surge gate, so logging them only
                            # floods the tape.
                            suppress_1min_raw = self._suppress_1min_no_spike_tape(
                                tf, getattr(sig, "volume_spike_entry", False), settings
                            )
                            if not suppress_1min_raw:
                                scanner.log_event(
                                    f"UT1 raw {sig.signal_type} detected: {instrument} {tf} @ {sig.price:.2f}. Watching for repaint.",
                                    "trade",
                                )
                        
                    # Grade each candidate with its OWN timeframe's intelligence when
                    # per-timeframe grading is enabled; otherwise these resolve to the
                    # caller's 5-minute values and grading is unchanged.
                    if (
                        _timeframe_contexts is not None
                        and _setting_enabled(
                            getattr(settings, "own_timeframe_intel_fail_closed", True), True
                        )
                        and tf not in _timeframe_contexts
                    ):
                        # An own-timeframe rule may wait for its own evidence; it may not
                        # quietly rename the caller's 5m bundle as 1m/15m intelligence.
                        try:
                            scanner.log_signal_decision_once(
                                f"{instrument}|{tf}|{sig.signal_type}|own-tf-intel-missing",
                                f"Signal held: {instrument} {tf} {sig.signal_type} has no fresh "
                                "own-timeframe intelligence; fail-closed grading is active.",
                                "trade",
                            )
                        except Exception:
                            pass
                        self._remember_live_signal_identity(
                            scanner, dedup_key, s_ts, signal_identity
                        )
                        continue

                    _tf_intel_result, _tf_intel_score, _tf_regime = self._candidate_timeframe_context(
                        tf, intel_result, intel_score, regime, _timeframe_contexts
                    )
                    # Telemetry: record WHICH context actually graded this candidate.
                    # Without it the per-timeframe grading flag cannot be A/B'd offline —
                    # the tracker showed the candidate's regime but not the one grading used.
                    try:
                        setattr(sig, "graded_timeframe", str(tf or ""))
                        setattr(sig, "graded_intel_score", float(_tf_intel_score or 0.0))
                        setattr(sig, "graded_regime", str(_tf_regime or ""))
                        # Kept off GRADING_TELEMETRY_ATTRS deliberately: the decision
                        # tape needs the scalar/pillars, not a duplicate full bundle.
                        # Candidate construction below consumes it directly and copies
                        # only the established compact context fields.
                        setattr(sig, "graded_intel_result", _tf_intel_result)
                    except Exception:
                        pass
                    _live_session_ctx = (
                        _tf_intel_result.get("session_context", {})
                        if isinstance(_tf_intel_result, dict)
                        else {}
                    )
                    grade, conf, reasons = scanner.signals._grade_signal(
                        sig,
                        mtf_result.confluence_score,
                        _tf_intel_score,
                        _tf_regime,
                        vix_value=vix_value,
                        day_type=str(_live_session_ctx.get("day_type") or ""),
                        day_type_confidence=float(_live_session_ctx.get("confidence") or 0.0),
                    )
                    candidates_raw.append({
                        "sig": sig, 
                        "tf": tf, 
                        "grade": grade, 
                        "confidence": conf,
                        "reasons": reasons,
                        "after_no_entry": is_after_no_entry,
                        "after_no_entry_label": no_entry_label,
                        "dedup_key": dedup_key,
                        "signal_identity": signal_identity,
                    })
                    
                    # Always update the lock to prevent evaluating the same signal on
                    # every tick. The identity state is a set because union engines may
                    # emit a legitimate BUY and SELL at the same timestamp.
                    self._remember_live_signal_identity(
                        scanner, dedup_key, s_ts, signal_identity
                    )
                    getattr(scanner, "_live_signal_recheck_eligible", {}).pop(dedup_key, None)

        self._append_adaptive_dual_apex_candidates(
            scanner=scanner,
            instrument=instrument,
            mtf_result=mtf_result,
            intel_result=intel_result,
            intel_score=intel_score,
            regime=regime,
            settings=settings,
            candidates_raw=candidates_raw,
        )

        # In HISTORICAL mode, we only run the backfill once and do not return live candidates
        if getattr(scanner, "mode", "").upper() == "HISTORICAL":
            return []

        if not candidates_raw:
            return []

        def _emit_signal_decision(raw: Dict, reason_msg: str, status_msg: str) -> None:
            sig = raw["sig"]
            tf = raw["tf"]
            # Tape hygiene: a 1min signal that never cleared its basis-aware surge
            # first filter can't enter, so its rejections only flood the live tape.
            # Suppress them (debug file log only); reject diagnostics still count.
            if self._suppress_1min_no_spike_tape(
                tf, getattr(sig, "volume_spike_entry", False), settings
            ):
                logger.debug(
                    f"Signal decision (1min no-spike, tape-suppressed): {instrument} "
                    f"{tf} {sig.signal_type} -> {status_msg}: {reason_msg}"
                )
                diag_inc = getattr(scanner, "_diag_inc", None)
                if callable(diag_inc):
                    diag_inc("rejects", reason_msg)
                return
            decision_key = (
                f"{instrument}|{tf}|{sig.signal_type}|"
                f"{sig.timestamp.isoformat(timespec='seconds')}|{status_msg}|{reason_msg}"
            )
            logger.info(f"Signal decision: {instrument} {tf} {sig.signal_type} -> {status_msg}: {reason_msg}")
            emit_once = getattr(scanner, "log_signal_decision_once", None)
            message = (
                f"Signal rejected: {instrument} {sig.signal_type} {tf} @ {sig.price:.2f} - "
                f"{reason_msg}"
            )
            if callable(emit_once):
                emit_once(decision_key, message, "trade")
            else:
                scanner.log_event(message, "trade")
                
            diag_inc = getattr(scanner, "_diag_inc", None)
            if callable(diag_inc):
                diag_inc("rejects", reason_msg)

        def _build_rejected_from_raw(raw: Dict, reason_msg: str, status_msg: str) -> List[TradeCandidate]:
            _emit_signal_decision(raw, reason_msg, status_msg)
            sig = raw["sig"]
            tf = raw["tf"]
            grade = raw["grade"]
            confidence = raw["confidence"]
            reasons = raw["reasons"]
            recheckable_status = self._recheckable_rejection_status(status_msg)
            if is_live and recheckable_status:
                now_check = naive_ts(datetime.now(IST))
                tf_minutes = {"1min": 1, "5min": 5, "15min": 15}.get(tf, 5)
                if now_check < naive_ts(sig.timestamp) + timedelta(minutes=tf_minutes):
                    if not hasattr(scanner, "_live_signal_recheck_eligible"):
                        scanner._live_signal_recheck_eligible = {}
                    scanner._live_signal_recheck_eligible[
                        raw.get("dedup_key", f"{instrument}_{tf}")
                    ] = raw.get(
                        "signal_identity",
                        f"{naive_ts(sig.timestamp).isoformat()}|{sig.signal_type}",
                    )
            direction = "LONG" if sig.signal_type == "BUY" else "SHORT"
            rej = TradeCandidate(
                instrument=instrument,
                direction=direction,
                price=sig.price,
                stop=sig.trailing_stop,
                target=sig.price,
                lots=0,
                lot_size=0,
                grade=grade,
                confidence=confidence,
                timeframe=tf,
                inst_type="FUT",
                option_type="",
                atm_strike=0.0,
                multiplier=1.0,
                trading_symbol="",
                symbol_token="",
                rr=0.0,
                score=confidence,
                reasons=list(reasons or []) + [reason_msg],
                signal_timestamp=sig.timestamp,
                status=status_msg,
                action="NO_ENTRY"
            )
            carry_grading_telemetry(rej, sig)
            src_engine = str(getattr(sig, "source_engine", "") or "").lower()
            src_engines = [
                str(src).lower()
                for src in (getattr(sig, "source_engines", None) or [])
                if str(src or "").strip()
            ]
            setattr(rej, "source_engine", src_engine)
            setattr(rej, "source_engines", src_engines)
            setattr(rej, "source_lane", self._signal_source_lane(sig, scanner_engine_mode))
            setattr(rej, "quad_union_entry", scanner_engine_mode in {"quad_union", "quad_prime"})
            setattr(rej, "raw_union_entry", scanner_engine_mode in {"quad_union", "quad_prime"})
            setattr(rej, "source_signal_type", sig.signal_type)
            setattr(rej, "source_signal_price", float(getattr(sig, "price", 0.0) or 0.0))
            setattr(rej, "adx_value", float(getattr(sig, "adx_value", 0.0) or 0.0))
            setattr(rej, "market_regime", str(regime or "UNKNOWN"))
            setattr(rej, "intelligence_score", float(normalize_intelligence_score(intel_score)))
            rejected_intel_result = self._graded_candidate_intel_result(sig, intel_result)
            if isinstance(rejected_intel_result, dict):
                setattr(
                    rej,
                    "market_intelligence_context",
                    {
                        "aggregate": dict(rejected_intel_result.get("aggregate", {}) or {}),
                        "regime": dict(rejected_intel_result.get("regime", {}) or {}),
                        "volume": dict(rejected_intel_result.get("volume", {}) or {}),
                        "order_flow": dict(rejected_intel_result.get("order_flow", {}) or {}),
                        "session_context": dict(rejected_intel_result.get("session_context", {}) or {}),
                        "anchor_regime": dict(rejected_intel_result.get("anchor_regime", {}) or {}),
                        "breadth_context": dict(rejected_intel_result.get("breadth_context", {}) or {}),
                        "top_down_context": dict(rejected_intel_result.get("top_down_context", {}) or {}),
                        "market_structure": dict(rejected_intel_result.get("market_structure", {}) or {}),
                        "aroon": dict(rejected_intel_result.get("aroon_context", {}) or {}),
                        "wavetrend": dict(rejected_intel_result.get("wavetrend", {}) or {}),
                    },
                )
            return [rej]

        exit_handled = False
        opposite_signal_raw = None
        for c in sorted(candidates_raw, key=lambda item: item["sig"].timestamp):
            if scanner._handle_opposite_signal_exit(instrument, c["sig"], c["tf"]):
                exit_handled = True
                opposite_signal_raw = c  # the signal whose flip forced the exit

        # Live/manual mode must not flip into a fresh opposite entry in the same
        # index/timeframe until the existing signal/trade has been exited. We block
        # the entry THIS cycle but reject it with a recheck-eligible status against
        # the *triggering* signal, so the NEXT cycle re-evaluates it (existing trade
        # now gone) and the reversal entry can fire — otherwise the dedup lock would
        # advance past the signal and orphan the reversal forever.
        #
        # Exception: multi-index market LTP shock / market-wide reverse — force-exit
        # the old side and allow the reverse entry this cycle so late dumps/rallies
        # are not missed while waiting for the next scan.
        if exit_handled and getattr(scanner, "mode", "") != "HISTORICAL":
            blocked_raw = opposite_signal_raw or candidates_raw[-1]
            _shock_relax = False
            try:
                settings_for_shock = get_settings()
                if _setting_enabled(
                    getattr(settings_for_shock, "market_ltp_shock_opposite_trade_relax", True),
                    True,
                ):
                    _flip_sig = (blocked_raw or {}).get("sig")
                    _flip_tf = str((blocked_raw or {}).get("tf") or "")
                    if _flip_sig is not None and (
                        bool(getattr(_flip_sig, "market_ltp_shock_entry", False))
                        or bool(getattr(_flip_sig, "market_wide_reversal_entry", False))
                    ):
                        _shock_relax = True
                    elif _flip_sig is not None and callable(
                        getattr(scanner, "_evaluate_market_ltp_shock", None)
                    ):
                        _shock_ctx = scanner._evaluate_market_ltp_shock(
                            instrument,
                            str(getattr(_flip_sig, "signal_type", "") or ""),
                            getattr(_flip_sig, "timestamp", None),
                            timeframe=_flip_tf,
                        )
                        if _shock_ctx:
                            setattr(_flip_sig, "market_ltp_shock_entry", True)
                            setattr(_flip_sig, "market_ltp_shock_context", _shock_ctx)
                            _shock_relax = True
                    if not _shock_relax and _flip_sig is not None:
                        _mwr = getattr(_flip_sig, "market_wide_reversal_context", None)
                        if not _mwr and callable(
                            getattr(scanner, "_market_wide_reversal_context", None)
                        ):
                            _mwr = scanner._market_wide_reversal_context(
                                instrument,
                                _flip_tf,
                                str(getattr(_flip_sig, "signal_type", "") or ""),
                                getattr(_flip_sig, "timestamp", None),
                            )
                            if _mwr:
                                setattr(_flip_sig, "market_wide_reversal_entry", True)
                                setattr(_flip_sig, "market_wide_reversal_context", _mwr)
                                _shock_relax = True
            except Exception as _shock_exc:
                logger.debug(f"Opposite-trade shock relax skipped for {instrument}: {_shock_exc}")
                _shock_relax = False
            if _shock_relax:
                scanner.log_event(
                    f"Opposite-trade relax: {instrument} market-wide shock allows reverse entry this cycle",
                    "trade",
                )
            else:
                scanner.log_event(
                    f"Entry blocked: {instrument} opposite signal first requires the existing {blocked_raw['tf']} signal/trade to exit",
                    "trade",
                )
                return _build_rejected_from_raw(blocked_raw, "Opposite signal must exit existing signal/trade first", "REJECTED - Opposite Signal Wait")

        entry_candidates_raw = self._entry_admissible_raw_candidates(
            candidates_raw,
            reversion_window_open=bool(_reversion_window),
        )
        if not entry_candidates_raw:
            return []
        if getattr(scanner, "mode", "") != "HISTORICAL":
            tf_policy = str(getattr(settings, "ut_timeframe_entry_policy", "PRIMARY_15") or "PRIMARY_15").upper()
            # PRIMARY_15 (non-quad_prime): 5min is timing/exit-only; 15min enters.
            # 1min SPIKE / POST-WIN SNAPBACK are independent fast paths — NEVER
            # blocked by engine choice or TF policy. Only their own settings
            # (one_min_scalping / volume_spike / post_exit_reversion + window/tape)
            # govern them; they already passed _entry_admissible_raw_candidates.
            if tf_policy == "PRIMARY_15" and scanner_engine_mode != "quad_prime":
                entry_candidates_raw = [
                    c
                    for c in entry_candidates_raw
                    if str(c.get("tf") or "") in {"15min", "1min"}
                ]
                if not entry_candidates_raw:
                    scanner.log_event(
                        f"Entry blocked: {instrument} 5min signal is exit/timing-only while 15M MAIN policy is active",
                        "trade",
                    )
                    return _build_rejected_from_raw(candidates_raw[-1], "5min signal is exit/timing-only while 15M MAIN policy is active", "REJECTED - 15M MAIN Policy Blocked")

        scored = []
        for c in entry_candidates_raw:
            sig, tf, grade, confidence, reasons = c["sig"], c["tf"], c["grade"], c["confidence"], c["reasons"]
            score = confidence
            if tf == "15min":
                score += 0.0001
            if bool(getattr(sig, "volume_spike_entry", False)) or bool(
                getattr(sig, "market_ltp_shock_entry", False)
            ):
                # A confirmed fast spike / multi-index LTP shock must outrank a
                # simultaneous 15min row that may already be inside the 15:00
                # no-entry window (5min/1min still trade until their later cutoff).
                score += 1.0
            if self._signal_source_lane(sig, scanner_engine_mode) == "B":
                # Engine 11's APEX lane is intentionally confirmation-heavy.
                # When it competes with the adaptive trend lane in the same
                # scan, give the fresh APEX candidate a small tiebreak without
                # bypassing grade, quality, risk, or quote gates.
                score += 0.75
            scored.append((score, sig, tf, grade, confidence, reasons, c.get("after_no_entry", False)))

        # Fix: Sort primarily by timestamp to pick the LATEST signal.
        # If multiple signals have the same timestamp, pick the one with highest score.
        scored.sort(key=lambda x: (x[1].timestamp, x[0]), reverse=True)
        best_score, best_sig, best_tf, best_grade, best_conf, best_reasons, best_after_no_entry = scored[0]
        best_source_engine = str(getattr(best_sig, "source_engine", "") or "").lower()
        best_source_engines = [
            str(src).lower()
            for src in (getattr(best_sig, "source_engines", None) or [])
            if str(src or "").strip()
        ]
        if not best_source_engines and best_source_engine:
            if best_source_engine == "both":
                best_source_engines = ["utbot", "partner"]
            elif best_source_engine == "multi":
                best_source_engines = ["multi"]
            else:
                best_source_engines = [best_source_engine]
        best_source_lane = self._signal_source_lane(best_sig, scanner_engine_mode)
        quad_union_entry = scanner_engine_mode in {"quad_union", "quad_prime"}
        quad_prime_entry = scanner_engine_mode == "quad_prime"
        best_direction = "LONG" if best_sig.signal_type == "BUY" else "SHORT"

        # ── DIAGNOSTIC: 5min-vs-15min selection (debug TF misdetection) ──
        # Fires live whenever more than one TF competed or 15min won, so the next
        # "5min real / 15min phantom" event is captured with full context.
        try:
            _now_dbg = naive_ts(datetime.now(IST))
            _admitted = []
            for _sc, _sg, _t, _gr, _cf, _r, _an in scored:
                _ts = naive_ts(_sg.timestamp)
                _closed = self._live_signal_candle_is_closed(_ts, _t, _now_dbg)
                _admitted.append(
                    f"{_t}@{_ts.strftime('%H:%M')}/{_sg.signal_type}/{_gr}/{_cf:.2f}/"
                    f"{'closed' if _closed else 'FORMING'}/score={_sc:.4f}"
                )
            _raw = []
            for _t, _res in (
                ("1min", getattr(mtf_result, "results_1min", None)),
                ("5min", mtf_result.results_5min),
                ("15min", mtf_result.results_15min),
            ):
                _rs = [s for s in (_res or {}).get("signals", []) if s.instrument == instrument]
                _latest = (
                    f"{naive_ts(_rs[-1].timestamp).strftime('%m-%d %H:%M')}/{_rs[-1].signal_type}"
                    if _rs else "none"
                )
                _raw.append(f"{_t}:raw={len(_rs)}(latest {_latest})")
            _open_trades = getattr(getattr(scanner, "trades", None), "open_trades", {}) or {}
            _trade_involved = any(
                getattr(_trade, "instrument", None) == instrument
                and getattr(_trade, "status", None) == "OPEN"
                and self._is_live_book_trade(_trade)
                for _trade in _open_trades.values()
            )
            if self._should_log_tf_select_diagnostic(
                best_tf,
                best_sig,
                scored,
                settings,
                trade_involved=_trade_involved,
            ):
                scanner.log_event(
                    f"🧪 [TF-SELECT] {instrument}: chose {best_tf}@"
                    f"{naive_ts(best_sig.timestamp).strftime('%H:%M')} | "
                    f"admitted=[{' ; '.join(_admitted)}] | {' '.join(_raw)}",
                    "trade",
                )
        except Exception as _dbg_exc:
            logger.debug(f"TF-select diagnostic failed for {instrument}: {_dbg_exc}")

        def _build_rejected_candidate(reason_msg: str, status_msg: str) -> List[TradeCandidate]:
            return _build_rejected_from_raw(
                {
                    "sig": best_sig,
                    "tf": best_tf,
                    "grade": best_grade,
                    "confidence": best_conf,
                    "reasons": best_reasons,
                },
                reason_msg,
                status_msg,
            )

        for tid, trade in list(scanner.trades.open_trades.items()):
            # Ghost / hist / replay rows never block live opposite/same-side guards.
            if not self._is_live_book_trade(trade):
                continue
            if trade.instrument == instrument and trade.status == "OPEN":
                if scanner_engine_mode == "adaptive_dual" and best_source_lane == "B":
                    probe = SimpleNamespace(
                        instrument=instrument,
                        timeframe=best_tf,
                        direction=best_direction,
                        grade=best_grade,
                        confidence=best_conf,
                        lots=1,
                        lot_size=1,
                        price=float(getattr(best_sig, "price", 0.0) or 0.0),
                        stop=float(getattr(best_sig, "trailing_stop", 0.0) or 0.0),
                        inst_type="FUT",
                        source_engine=best_source_engine,
                        source_engines=list(best_source_engines),
                        source_lane=best_source_lane,
                        signal_timestamp=getattr(best_sig, "timestamp", None),
                        source_signal_bar_index=int(getattr(best_sig, "bar_index", -1) or -1),
                        volume_spike_entry=bool(getattr(best_sig, "volume_spike_entry", False)),
                        power_move=bool(getattr(best_sig, "power_move", False)),
                        market_wide_reversal_entry=bool(getattr(best_sig, "market_wide_reversal_entry", False)),
                        vwap_break_context=getattr(best_sig, "vwap_break_context", None),
                        reasons=list(best_reasons or []) + list(getattr(best_sig, "apex_reasons", []) or []),
                    )
                    lane_decision = getattr(scanner, "_dual_lane_concurrency_decision", lambda *a, **k: (None, ""))(
                        probe,
                        [trade],
                        adjust_size=False,
                    )
                    if isinstance(lane_decision, tuple) and lane_decision[0] is not None:
                        allowed, lane_reason = lane_decision
                        if allowed:
                            best_reasons = list(best_reasons or []) + [f"Adaptive Dual Lane B active-trade pass: {lane_reason}"]
                            continue
                        _emit_signal_decision(
                            {
                                "sig": best_sig,
                                "tf": best_tf,
                                "grade": best_grade,
                                "confidence": best_conf,
                                "reasons": best_reasons,
                            },
                            f"Adaptive Dual Lane B blocked: {lane_reason}",
                            "REJECTED - Dual Lane Guard",
                        )
                        return []
                if quad_prime_entry:
                    trade_direction = str(getattr(trade, "direction", "") or "").upper()
                    if trade_direction != best_direction:
                        # _build_rejected_from_raw, not the bare emitter: this is a FLIP
                        # being refused only because the far-side trade has not closed
                        # yet, so it must stay recheck-eligible for the cycle after it
                        # does. The bare _emit_signal_decision only logs and counts, and
                        # the dedup lock (advanced when the raw signal was collected)
                        # then orphans the arrow permanently. The sibling refusal at
                        # "REJECTED - Opposite Signal Wait" has always done it this way.
                        # dedup_key/signal_identity are what the recheck is armed under,
                        # so they are carried through exactly as that path carries them.
                        _best_raw = next(
                            (
                                c for c in entry_candidates_raw
                                if c.get("sig") is best_sig
                            ),
                            {},
                        )
                        return _build_rejected_from_raw(
                            {
                                "sig": best_sig,
                                "tf": best_tf,
                                "grade": best_grade,
                                "confidence": best_conf,
                                "reasons": best_reasons,
                                "dedup_key": _best_raw.get(
                                    "dedup_key", f"{instrument}_{best_tf}"
                                ),
                                "signal_identity": _best_raw.get("signal_identity"),
                            },
                            f"Opposite {getattr(trade, 'timeframe', '')} {trade_direction} trade {tid} still active",
                            "REJECTED - Existing Opposite Trade",
                        )

                    candidate_sources = scanner._quad_union_sources(best_sig)
                    trade_sources = scanner._quad_union_sources(trade)
                    same_tf = str(getattr(trade, "timeframe", "") or "") == best_tf
                    source_overlaps = (
                        not candidate_sources
                        or not trade_sources
                        or bool(candidate_sources & trade_sources)
                    )
                    if same_tf and source_overlaps:
                        # THIS is the cascade confirming itself: the same engine firing
                        # again on the same chart, which is the commonest shape of the
                        # operator's add-a-lot rule. It was discarded here, upstream of
                        # the only place the add is offered -- _offer_cascade_add is
                        # reached from _coordinate_and_execute, and a candidate that
                        # never leaves process_best_signal never gets there. So the rule
                        # produced nothing at all in its most likely case.
                        #
                        # Gated on auto_mode for the same reason as the scanner-side call
                        # site: scale_in_trade reaches broker.place_order, and manual mode
                        # means the operator approves every entry.
                        add_note = ""
                        adder = getattr(scanner, "_offer_cascade_add", None)
                        if callable(adder) and bool(getattr(scanner, "auto_mode", False)):
                            try:
                                added, add_note = adder(best_sig)
                                if added:
                                    _emit_signal_decision(
                                        {
                                            "sig": best_sig,
                                            "tf": best_tf,
                                            "grade": best_grade,
                                            "confidence": best_conf,
                                            "reasons": best_reasons,
                                        },
                                        f"Cascade add: one more lot on {tid} instead of a "
                                        f"second {best_tf} trade",
                                        "CASCADE_ADD",
                                    )
                                    return []
                            except Exception as exc:
                                logger.debug(f"Cascade add offer failed for {instrument}: {exc}")
                        _emit_signal_decision(
                            {
                                "sig": best_sig,
                                "tf": best_tf,
                                "grade": best_grade,
                                "confidence": best_conf,
                                "reasons": best_reasons,
                            },
                            f"Already has active quad-prime {best_tf} {trade_direction} source {tid}"
                            + (f"; cascade add declined ({add_note})" if add_note else ""),
                            "REJECTED - Existing Active Trade",
                        )
                        return []
                    continue

                if best_tf == "15min" and trade.timeframe == "5min":
                    trade.timeframe = "15min"
                    trade.grade = best_grade
                    trade.confidence = best_conf
                    logger.info(f"🚀 UPGRADED {instrument} trade to 15-min status")
                    return []
                _emit_signal_decision(
                    {
                        "sig": best_sig,
                        "tf": best_tf,
                        "grade": best_grade,
                        "confidence": best_conf,
                        "reasons": best_reasons,
                    },
                    f"Already has active {trade.timeframe} {trade.direction} trade {tid}",
                    "REJECTED - Existing Active Trade",
                )
                return []

        # Do not call process_signal here. In live mode this stage can run
        # before the 25s repaint guard has matured, and process_signal mutates
        # active_signals/dedup state. Keep grading stateless; the execution
        # lifecycle starts only after stabilization in scanner._coordinate_and_execute.
        graded = GradedSignal(
            signal=best_sig,
            grade=best_grade,
            confidence=best_conf,
            reasons=list(best_reasons or []),
            intelligence_score=intel_score,
            confluence_score=mtf_result.confluence_score,
            is_actionable=best_grade in ["A+", "A", "B+", "B"],
        )
        state_15m = (getattr(mtf_result, "results_15min", None) or {}).get("state", {}) or {}
        # "The 15m engine has no state" and "the 15m trend disagrees" both collapse to
        # position 0, and 0 never equals +/-1, so both produce trend_15m_agrees=False.
        # In REAL mode that flag alone closes the entire 5min lane -- swept exhaustively,
        # 0 of 5,775 (confidence x ADX x intel x IV x premium) combinations pass with it
        # False, in every one of the seven regimes. 5min carries 181 of 252 recorded
        # entries, 72% of the book.
        #
        # The failure mode is not the refusal, which is the design intent -- a 5min entry
        # SHOULD be 15m-aligned. It is that a cold start, a 15m engine that failed to
        # build, or a lookup that raised is INVISIBLE: no decision row is written, so the
        # dashboard reads "no 5min signal today" rather than "the 15m state is missing".
        #
        # Admission is deliberately unchanged here. This only separates the two cases and
        # says the silent one out loud. A position of 0 with real state is a legitimate
        # "15m is flat", which is a genuine disagreement, not an absence.
        fifteen_min_available = bool(state_15m) and state_15m.get("position") is not None
        position_15m = int(state_15m.get("position") or 0)
        direction_position = 1 if best_sig.signal_type == "BUY" else -1
        trend_15m_agrees = position_15m == direction_position
        try:
            setattr(
                best_sig,
                "trend_15m_state",
                "agrees" if trend_15m_agrees
                else ("unavailable" if not fifteen_min_available else "disagrees"),
            )
            if not fifteen_min_available and scanner is not None:
                day = getattr(getattr(best_sig, "timestamp", None), "date", lambda: "")()
                scanner.log_signal_decision_once(
                    f"15m-state-unavailable|{instrument}|{best_tf}|{day}",
                    f"{instrument} {best_tf}: 15-minute state unavailable, so every "
                    f"5min entry for it is refused as 15m-misaligned. This is a missing "
                    f"engine or a cold start, not a disagreeing trend.",
                    "warning",
                )
        except Exception as exc:  # telemetry may never cost a signal
            logger.debug(f"15m availability note skipped for {instrument}: {exc}")
        adx_value = float(getattr(best_sig, "adx_value", 0.0) or 0.0)
        volume_spike_entry = bool(getattr(best_sig, "volume_spike_entry", False))
        volume_spike_metrics = dict(getattr(best_sig, "volume_spike_metrics", {}) or {})
        market_ltp_shock_entry = bool(getattr(best_sig, "market_ltp_shock_entry", False))
        market_ltp_shock_context = dict(getattr(best_sig, "market_ltp_shock_context", {}) or {})
        if not market_ltp_shock_entry:
            try:
                _shock_fn = getattr(scanner, "_evaluate_market_ltp_shock", None)
                if callable(_shock_fn):
                    _shock_ctx = _shock_fn(
                        instrument,
                        str(best_sig.signal_type),
                        best_sig.timestamp,
                        timeframe=best_tf,
                    )
                    if _shock_ctx:
                        market_ltp_shock_entry = True
                        market_ltp_shock_context = dict(_shock_ctx)
                        setattr(best_sig, "market_ltp_shock_entry", True)
                        setattr(best_sig, "market_ltp_shock_context", market_ltp_shock_context)
                        best_reasons = list(best_reasons or []) + [
                            str(_shock_ctx.get("reason") or "market LTP shock")
                        ]
            except Exception as _shock_exc:
                logger.debug(f"Market LTP shock detect skipped for {instrument}: {_shock_exc}")

        # ── Session directional census ──
        # Count each DISTINCT directional arrow once so we know the day's bias.
        # Used by the counter-trend guard below. Keyed identity dedups the many
        # re-evaluations of the same forming candle within a scan cycle.
        signal_direction = best_direction
        try:
            census_day = best_sig.timestamp.date().isoformat()
            if getattr(scanner, "_dir_census_date", None) != census_day:
                scanner._dir_census_date = census_day
                scanner._dir_census = {"LONG": 0, "SHORT": 0}
                scanner._dir_census_seen = set()
            _census_id = (
                f"{instrument}|{best_tf}|"
                f"{naive_ts(best_sig.timestamp).isoformat()}|{signal_direction}"
            )
            if _census_id not in scanner._dir_census_seen:
                scanner._dir_census_seen.add(_census_id)
                scanner._dir_census[signal_direction] = (
                    scanner._dir_census.get(signal_direction, 0) + 1
                )
        except Exception:
            pass
        momentum_threshold = float(getattr(settings, "momentum_override_threshold", 25.0) or 25.0)
        intel_norm = normalize_intelligence_score(intel_score)
        # When a smoothed trend engine (consensus/ema/auto) is active, the signal is
        # ALREADY a confirmed trend flip — not UTBot's noisy single-bar flip. The
        # impulse-context / choppy-confirmation gates exist to filter UT1 noise, so a
        # 15m-aligned signal from a trend engine satisfies them on its own (confidence
        # floor still applies). "auto" emits consensus/ema flips, so it counts too.
        trend_engine_signal = (
            scanner_engine_mode in {"consensus", "ema", "auto", "utbot_ema", "utbot_consensus", "quad_union", "quad_prime", "adaptive_hybrid", "adaptive_dual", "quality_hybrid"}
            and _setting_enabled(getattr(settings, "trend_engine_relaxed_gate", True), True)
        )
        # Data-confirmed late-session power move (volume + range + close + momentum/align).
        # When confirmed it may bypass the grade floor and (after 15:00) the no-entry
        # cutoff, entering in tight scalp-lock mode.
        power_move = scanner._detect_power_move(
            instrument,
            best_tf,
            best_sig.signal_type,
            adx_value=adx_value,
            intel_score=intel_score,
            trend_15m_agrees=trend_15m_agrees,
        )
        # Market LTP shock is a multi-index fast dump/rally — treat like volume spike
        # for gate bypasses (trend guard, quality floor) on 1min/5min only.
        fast_thrust_override = bool(power_move or volume_spike_entry or market_ltp_shock_entry)
        _reversal_relief_context = None
        try:
            _relief_fn = getattr(scanner, "_entry_reversal_relief_context", None)
            if callable(_relief_fn):
                _reversal_relief_context = _relief_fn(
                    instrument,
                    best_tf,
                    best_sig.signal_type,
                    best_sig.timestamp,
                    regime=regime,
                    confidence=graded.confidence,
                )
        except Exception as exc:
            logger.debug(f"Reversal relief check skipped for {instrument} {best_tf}: {exc}")
        late_session_reclaim_entry = bool(
            isinstance(_reversal_relief_context, dict)
            and _reversal_relief_context.get("type") == "late_session_reclaim"
        )
        one_way_cascade_entry = bool(
            isinstance(_reversal_relief_context, dict)
            and _reversal_relief_context.get("type") == "one_way_cascade"
        )
        if late_session_reclaim_entry:
            try:
                setattr(best_sig, "late_session_reclaim_entry", True)
                setattr(best_sig, "late_session_reclaim_context", dict(_reversal_relief_context))
            except Exception:
                pass
            best_reasons = list(best_reasons or []) + [
                str(_reversal_relief_context.get("reason") or "late-session synchronized reclaim")
            ]
        if one_way_cascade_entry:
            try:
                setattr(best_sig, "one_way_cascade_entry", True)
                setattr(best_sig, "one_way_cascade_context", dict(_reversal_relief_context))
            except Exception:
                pass
            best_reasons = list(best_reasons or []) + [
                str(_reversal_relief_context.get("reason") or "one-way cascade")
            ]
        extreme_exhaustion = any(
            "extreme" in str(reason).lower() and "exhaustion" in str(reason).lower()
            for reason in (best_reasons or [])
        )
        impulse_confirmed = (
            best_tf == "5min"
            and self._is_5min_impulse_reversal(
                best_sig,
                mtf_result,
                graded.confidence,
                settings,
            )
        )
        is_explosive_bypass = bool(
            impulse_confirmed
            and (
                adx_value >= momentum_threshold
                or abs(intel_norm) >= 0.70
            )
        )
        if best_tf == "5min" and self._is_choppy_regime(regime) and extreme_exhaustion:
            exhaustion_follow_through = (
                trend_15m_agrees
                and impulse_confirmed
                and (best_score >= 0.45 or abs(intel_norm) >= 0.70 or graded.confidence >= 0.90)
            )
            is_explosive_bypass = bool(is_explosive_bypass and exhaustion_follow_through)
        impulse_context_confirmed = bool(impulse_confirmed or is_explosive_bypass)
        iv_percentile = intel_result.get("greeks", {}).get("iv_percentile")
        chain_quality = intel_result.get("data_quality", {}) or {}
        option_premium_ok = option_premium_health_ok(
            settings,
            iv_percentile=iv_percentile,
            chain_quality=chain_quality,
        )
        true_5min_impulse = best_tf == "5min" and five_min_true_impulse_allowed(
            graded.grade,
            graded.confidence,
            settings,
            trend_15m_agrees=trend_15m_agrees,
            impulse_confirmed=impulse_confirmed,
            explosive_bypass=is_explosive_bypass,
            extreme_exhaustion=extreme_exhaustion,
            regime=regime,
            adx_value=adx_value,
            intel_score=intel_score,
            iv_percentile=iv_percentile,
            chain_quality=chain_quality,
            option_premium_ok=option_premium_ok,
        )
        quality_hybrid_entry = False
        try:
            quality_hybrid_entry = bool(
                getattr(scanner, "_is_quality_hybrid_route_entry", lambda *a, **k: False)(
                    settings,
                    best_sig,
                    confidence=float(getattr(graded, "confidence", 0.0) or 0.0),
                    grade_rank=self._grade_rank(getattr(graded, "grade", "")),
                    timeframe=best_tf,
                    trend_15m_agrees=trend_15m_agrees,
                    explosive_move=bool(power_move or volume_spike_entry or is_explosive_bypass),
                )
            )
        except Exception:
            quality_hybrid_entry = False
        if (not graded or not graded.is_actionable) and not (
            volume_spike_entry
            or market_ltp_shock_entry
            or quad_union_entry
            or quality_hybrid_entry
            or late_session_reclaim_entry
            or one_way_cascade_entry
        ):
            grade_txt = str(getattr(graded, "grade", "?")) if graded else "?"
            conf_txt = f"{float(getattr(graded, 'confidence', 0.0) or 0.0):.0%}" if graded else "0%"
            msg = (
                f"Not actionable after filters "
                f"[{grade_txt} @ {conf_txt}, regime={regime}, adx={adx_value:.0f}, "
                f"15m_agrees={trend_15m_agrees}]"
            )
            return _build_rejected_candidate(msg, "REJECTED - Quality Gate")

        if best_tf == "5min":
            try:
                sig_time = best_sig.timestamp.astimezone(IST).time() if getattr(best_sig.timestamp, "tzinfo", None) is not None else best_sig.timestamp.time()
            except Exception:
                sig_time = datetime.now(IST).time()

            early_cutoff_str = str(getattr(settings, "ut_early_session_cutoff", "09:30") or "09:30")
            pre_close_cutoff_str = str(getattr(settings, "ut_no_entry_after", "15:00") or "15:00")
            early_cutoff = datetime.strptime(early_cutoff_str, "%H:%M").time()
            pre_close_cutoff = datetime.strptime(pre_close_cutoff_str, "%H:%M").time()

            min_conf_5m = self._relaxed_threshold(
                float(getattr(settings, "ut_5min_option_min_confidence", 0.60) or 0.60),
                settings,
            )

            gate_name = "5min quality gate"

            if sig_time < early_cutoff:
                early_conf = self._relaxed_threshold(
                    float(getattr(settings, "live_5min_early_min_confidence", 0.70) or 0.70),
                    settings,
                )
                min_conf_5m = max(min_conf_5m, early_conf)
                gate_name = "5min early morning gate"
            elif sig_time >= pre_close_cutoff:
                pre_close_conf = self._relaxed_threshold(
                    float(getattr(settings, "live_5min_pre_close_min_confidence", 0.70) or 0.70),
                    settings,
                )
                min_conf_5m = max(min_conf_5m, pre_close_conf)
                gate_name = "5min pre-close gate"

            if self._is_choppy_regime(regime):
                # Aligned VOLATILE tapes relax toward 0.65; RANGING/UNKNOWN keep 0.70.
                choppy_min_conf_5m = live_5min_choppy_confidence_floor(
                    settings, regime, trend_15m_agrees
                )
                min_conf_5m = max(min_conf_5m, choppy_min_conf_5m)
            grade_conf_ok = self._grade_rank(graded.grade) >= 2 and graded.confidence >= min_conf_5m
            if not (
                quad_union_entry
                or
                quality_hybrid_entry
                or
                volume_spike_entry
                or late_session_reclaim_entry
                or one_way_cascade_entry
                or power_move
                or true_5min_impulse
                or (grade_conf_ok and trend_15m_agrees and impulse_context_confirmed)
                # Trend engine: a 15m-aligned, confidence-qualified consensus/ema flip is
                # itself the trend confirmation — no separate impulse reversal required.
                or (trend_engine_signal and grade_conf_ok and trend_15m_agrees)
            ):
                msg = (
                    f"{gate_name} needs B+/A/A+ with >= {min_conf_5m:.0%}, "
                    f"or special B+/A/A+ >=60% with 15min agreement, ADX>=30/very strong intel, "
                    f"known regime, healthy option premium, and no exhaustion "
                    f"[got {graded.grade} @ {graded.confidence:.0%}, regime={regime}, "
                    f"adx={adx_value:.0f}, 15m_agrees={trend_15m_agrees}]"
                )
                # Cap words for the REJECTED status msg (e.g. "REJECTED - 5Min Early Morning Gate")
                return _build_rejected_candidate(msg, f"REJECTED - {gate_name.title()}")
            
        min_confidence = 0.55
        if getattr(settings, "ut_dynamic_confidence", False):
            regime_adaptation = _setting_enabled(getattr(settings, "ut_regime_adaptation", True), True)
            if best_tf == "15min":
                min_confidence = max(
                    min_confidence,
                    fifteen_min_confidence_floor(
                        settings,
                        regime,
                        adx_value=adx_value,
                        intel_score=intel_score,
                    ),
                )
            elif regime_adaptation and self._is_choppy_regime(regime):
                min_confidence = max(
                    min_confidence,
                    self._relaxed_threshold(
                        self._choppy_confidence_gate(settings),
                        settings,
                    ),
                )
                
        if graded.confidence < min_confidence and not (
            quad_union_entry
            or quality_hybrid_entry
            or
            fast_thrust_override
            or late_session_reclaim_entry
            or one_way_cascade_entry
            or (best_tf == "5min" and (trend_15m_agrees or is_explosive_bypass))
        ):
            msg = (
                f"{graded.grade} {graded.confidence:.3%} below "
                f"{min_confidence:.3%} confidence gate"
            )
            return _build_rejected_candidate(msg, "REJECTED - Low Confidence")

        if best_tf == "5min" and self._is_choppy_regime(regime):
            # A trend-engine flip aligned with the 15m trend already cleared the
            # confidence floor above; the engine itself is the trend confirmation.
            if not (
                quad_union_entry
                or quality_hybrid_entry
                or true_5min_impulse
                or volume_spike_entry
                or late_session_reclaim_entry
                or one_way_cascade_entry
                or (trend_engine_signal and trend_15m_agrees)
            ):
                msg = "5min choppy gate needs true impulse: B+/A/A+ >=70%, or special B+/A/A+ >=60% with 15min agreement, ADX>=30/very strong intel, known regime, healthy option premium, no exhaustion"
                return _build_rejected_candidate(msg, "REJECTED - 5min Choppy Confirmation")

        # --- REVERSION / SNAPBACK eligibility (before Weak-1min gate) ---
        # Must run early: pure SNAPBACK has no volume_spike_entry and would die
        # at Weak-1min if tagged only later. SPIKE and SNAPBACK share the 1m
        # chassis but only ONE trade fires (best winner); dual-tag when both true.
        is_reversion_scalp, _rev_meta = self._evaluate_reversion_scalp(
            scanner,
            settings,
            instrument=instrument,
            best_tf=best_tf,
            best_sig=best_sig,
        )
        if is_reversion_scalp:
            logger.info(
                f"⚡ [REVERSION SCALP] {instrument} 1min {best_sig.signal_type} "
                f"eligible ({_rev_meta.get('minutes_since', 0):.1f}m after "
                f"{_rev_meta.get('view') or 'prior'} {_rev_meta.get('reason') or 'event'}; "
                f"move={_rev_meta.get('move_pct', 0):.2f}%)."
            )

        if best_tf == "1min":
            if not volume_spike_entry:
                if one_way_cascade_entry:
                    logger.info(f"🚀 Confirmed 1min one-way cascade entry for {instrument}")
                elif is_reversion_scalp:
                    logger.info(
                        f"🚀 Confirmed 1min POST-WIN SNAPBACK entry for {instrument} "
                        f"(window open; spike not required)"
                    )
                else:
                    spike_requirement = self._volume_spike_requirement_text(
                        settings, instrument
                    )
                    return _build_rejected_candidate(
                        f"1min entry requires confirmed directional burst: {spike_requirement}; "
                        "or an open POST-WIN SNAPBACK window with bare-min tape confirm",
                        "REJECTED - Weak 1min",
                    )
            else:
                path = "volume-spike"
                if is_reversion_scalp:
                    path = "volume-spike + SNAPBACK"
                logger.info(
                    f"🚀 Confirmed 1min {path} entry for {instrument}: "
                    f"{self._volume_spike_metric_text(volume_spike_metrics, settings, instrument, best_tf)}"
                )

        can_open, _ = scanner.trades.can_open_trade()
        if not can_open:
            return _build_rejected_candidate("Max overall trades reached or capital depleted", "REJECTED - Max Trades")

        iv_percentile_for_routing = float(iv_percentile if iv_percentile is not None else 50.0)
        is_high_iv = iv_percentile_for_routing > 80.0
        is_choppy = self._is_choppy_regime(regime)
        strong_momentum = abs(intel_norm) > 0.60 or best_score > 0.4
        
        trade_date = best_sig.timestamp.date()
        is_0dte = scanner.expiry.is_expiry_day(instrument, trade_date)
        is_late_session = best_sig.timestamp.time() >= datetime.strptime("14:00", "%H:%M").time()
        
        inst_type = scanner._resolve_instrument_type(
            instrument=instrument,
            grade=graded.grade,
            regime=regime,
            intel_score=intel_score,
            signal_score=best_score,
            confidence=graded.confidence,
            adx_value=getattr(best_sig, "adx_value", 0.0),
            atr_value=getattr(best_sig, "atr_value", 0.0),
            price=best_sig.price,
            signal_time=best_sig.timestamp,
            iv_percentile=iv_percentile_for_routing,
            timeframe=best_tf,
            five_min_true_impulse=true_5min_impulse,
        )
        if not scanner.risk_manager.can_trade(inst_type):
            return _build_rejected_candidate(
                f"{inst_type} circuit breaker is active",
                f"REJECTED - {inst_type} Risk Halt",
            )

        # ── Regime veto + directional census (shared with replay path) ──
        _veto_block = directional_fut_veto(
            settings,
            inst_type=inst_type,
            signal_type=best_sig.signal_type,
            regime=regime,
            adx_value=adx_value,
            power_move=fast_thrust_override or is_reversion_scalp,
        )
        if _veto_block and not (
            is_reversion_scalp or
            (late_session_reclaim_entry
            and isinstance(_reversal_relief_context, dict)
            and bool(_reversal_relief_context.get("allow_regime_veto")))
        ):
            # If we got a regime veto on FUT and inst_pref is AUTO, fall back to OPT
            scanner_inst_pref = (getattr(scanner, "inst_pref", "AUTO") or "AUTO").upper()
            if inst_type == "FUT" and scanner_inst_pref == "AUTO":
                logger.warning(
                    f"[{instrument}] FUT regime vetoed ({_veto_block}), falling back to OPT"
                )
                inst_type = "OPT"
                # Check if OPT is allowed by risk manager
                if not scanner.risk_manager.can_trade(inst_type):
                    return _build_rejected_candidate(
                        f"{inst_type} circuit breaker is active",
                        f"REJECTED - {inst_type} Risk Halt",
                    )
            else:
                return _build_rejected_candidate(_veto_block, "REJECTED - Regime Veto")
        if _veto_block and late_session_reclaim_entry:
            best_reasons = list(best_reasons or []) + [
                f"Late-session reclaim overrode stale regime veto: {_veto_block}"
            ]

        _session_ctx = intel_result.get("session_context", {}) if isinstance(intel_result, dict) else {}
        if (
            _setting_enabled(getattr(settings, "session_context_trend_day_guard_enabled", True), True)
            and isinstance(_session_ctx, dict)
            and best_tf in {"1min", "5min"}
            and not fast_thrust_override
            and not is_reversion_scalp
            and not late_session_reclaim_entry
            and not _reversal_relief_context
        ):
            # Prefer rolling effective phase so the guard tracks mood flips
            # (minute/hour/session pockets) instead of a sticky full-day label.
            _use_dynamic_mood = _setting_enabled(
                getattr(settings, "session_mood_dynamic_enabled", True), True
            )
            _day_type = str(
                (
                    (
                        _session_ctx.get("effective_day_type")
                        or _session_ctx.get("market_phase")
                        or _session_ctx.get("day_type")
                    )
                    if _use_dynamic_mood
                    else _session_ctx.get("day_type")
                )
                or ""
            ).upper()
            if _day_type == "MOOD_DUMP":
                _day_type = "TREND_DAY_DOWN"
            elif _day_type == "MOOD_RALLY":
                _day_type = "TREND_DAY_UP"
            elif _day_type == "MOOD_TRANSITION":
                _day_type = ""
            _day_conf = float(
                (
                    _session_ctx.get("phase_confidence")
                    if _use_dynamic_mood and _session_ctx.get("phase_confidence") is not None
                    else _session_ctx.get("confidence")
                )
                or 0.0
            )
            _opening_day = str(_session_ctx.get("opening_day_type") or _session_ctx.get("day_type") or "").upper()
            _expected = "BUY" if _day_type == "TREND_DAY_UP" else ("SELL" if _day_type == "TREND_DAY_DOWN" else "")
            _min_day_conf = float(getattr(settings, "session_context_trend_day_guard_min_confidence", 0.70) or 0.70)
            # Late-session multi-index shock overrides sticky opening TREND_DAY_*.
            # Active only inside shock tenure window (default 14:45–15:11 IST).
            _shock_yield = False
            if market_ltp_shock_entry or bool(getattr(best_sig, "market_wide_reversal_entry", False)):
                try:
                    _sig_t = best_sig.timestamp
                    if getattr(_sig_t, "tzinfo", None) is not None:
                        _sig_t = _sig_t.astimezone(IST).replace(tzinfo=None)
                    _win_start = str(
                        getattr(settings, "market_ltp_shock_session_start", None)
                        or getattr(settings, "market_ltp_shock_session_override_start", "14:45")
                        or "14:45"
                    )
                    _win_end = str(getattr(settings, "market_ltp_shock_session_end", "15:11") or "15:11")
                    _start_t = datetime.strptime(_win_start, "%H:%M").time()
                    _end_t = datetime.strptime(_win_end, "%H:%M").time()
                    _sig_clock = _sig_t.time()
                    if _start_t <= _sig_clock <= _end_t:
                        _shock_side = str(best_sig.signal_type).upper()
                        _opening_expected = (
                            "BUY" if _opening_day == "TREND_DAY_UP"
                            else ("SELL" if _opening_day == "TREND_DAY_DOWN" else "")
                        )
                        if _opening_expected and _shock_side != _opening_expected:
                            _shock_yield = True
                            best_reasons = list(best_reasons or []) + [
                                f"Session trend guard yielded: late LTP shock {_shock_side} "
                                f"overrides sticky {_opening_day} in {_win_start}-{_win_end}"
                            ]
                except Exception:
                    _shock_yield = False
            if (
                _expected
                and _day_conf >= _min_day_conf
                and str(best_sig.signal_type).upper() != _expected
                and not _shock_yield
            ):
                # Census-contradiction yield: when the session's own directional
                # census strongly opposes the day-type label, the label is stale
                # (2026-07-08: TREND_DAY_UP kept vetoing midcap SELLs 11:19-12:36
                # while the census was already 5L/10S; midcap then fell 1.9%).
                _census_yield_reason = session_trend_guard_census_yield(
                    settings,
                    census=getattr(scanner, "_dir_census", {}) or {},
                    signal_type=str(best_sig.signal_type).upper(),
                    expected=_expected,
                    day_type=_day_type,
                )
                if _census_yield_reason:
                    best_reasons = list(best_reasons or []) + [_census_yield_reason]
                else:
                    _session_bias = str(
                        _session_ctx.get("effective_bias")
                        or _session_ctx.get("bias")
                        or "NEUTRAL"
                    ).upper()
                    _delta = _session_ctx.get("session_delta", {}) if isinstance(_session_ctx.get("session_delta"), dict) else {}
                    msg = (
                        f"Session trend-phase guard: {_day_type} {_session_bias} "
                        f"conf={_day_conf:.0%}, delta={_delta.get('bias', 'NEUTRAL')} blocks "
                        f"{best_tf} counter-trend scalp"
                    )
                    return _build_rejected_candidate(msg, "REJECTED - Session Trend Guard")

        # ── Pre-candle flip veto (default OFF; telemetry-first) ──
        # The predictor projected the 15m flip that stopped the 14:53 A+ short on
        # 2026-07-03 but was wired to nothing. Telemetry always logs a would-veto
        # so live sessions accumulate evidence; the gate itself is opt-in.
        try:
            _pre_map = (getattr(scanner, "_pre_candle_predictions", {}) or {}).get(
                str(instrument or "").upper(), {}
            )
            _pre = _pre_map.get(best_tf, {}) if isinstance(_pre_map, dict) else {}
            _pre_state = str(_pre.get("state") or "").upper()
            _pre_dir = str(_pre.get("direction") or "").upper()
            _pre_conf = float(_pre.get("confidence") or 0.0)
            _sig_dir = "LONG" if str(best_sig.signal_type).upper() == "BUY" else "SHORT"
            _min_flip_conf = float(
                getattr(settings, "pre_candle_flip_veto_min_confidence", 0.70) or 0.70
            )
            if (
                _pre_state in {"LIKELY_5M_FLIP", "LIKELY_15M_FLIP"}
                and _pre_dir in {"LONG", "SHORT"}
                and _pre_dir != _sig_dir
                and _pre_conf >= _min_flip_conf
                and not fast_thrust_override
                and not is_reversion_scalp
            ):
                _flip_msg = (
                    f"Pre-candle flip against entry: {instrument} {best_tf} {_sig_dir} vs "
                    f"{_pre_state} {_pre_dir} conf {_pre_conf:.0%}"
                )
                if _setting_enabled(
                    getattr(settings, "pre_candle_flip_veto_enabled", False), False
                ):
                    return _build_rejected_candidate(_flip_msg, "REJECTED - Pre-Candle Flip Veto")
                logger.warning(f"[pre-candle would-veto] {_flip_msg}")
        except Exception as _flip_exc:
            logger.debug(f"Pre-candle flip veto check skipped for {instrument}: {_flip_exc}")

        # Day-type label for the census yield. Derived here rather than reusing the
        # trend-guard's _day_type, which is computed inside a conditional above and is
        # not guaranteed to be bound on this path. Same MOOD_* normalisation, so both
        # read the label the same way.
        _cs_day_type, _cs_day_conf = "", 0.0
        try:
            _cs_dyn = _setting_enabled(
                getattr(settings, "session_mood_dynamic_enabled", True), True
            )
            _cs_day_type = str(
                (
                    _session_ctx.get("effective_day_type")
                    or _session_ctx.get("market_phase")
                    or _session_ctx.get("day_type")
                )
                if _cs_dyn
                else _session_ctx.get("day_type")
                or ""
            ).upper()
            if _cs_day_type == "MOOD_DUMP":
                _cs_day_type = "TREND_DAY_DOWN"
            elif _cs_day_type == "MOOD_RALLY":
                _cs_day_type = "TREND_DAY_UP"
            elif _cs_day_type == "MOOD_TRANSITION":
                _cs_day_type = ""
            _cs_raw = (
                _session_ctx.get("phase_confidence")
                if _cs_dyn and _session_ctx.get("phase_confidence") is not None
                else _session_ctx.get("confidence")
            )
            _cs_day_conf = float(_cs_raw or 0.0)
        except Exception as _cs_exc:
            logger.debug(f"Census day-type context unavailable for {instrument}: {_cs_exc}")

        _census_block = directional_census_block(
            settings,
            getattr(scanner, "_dir_census", {}) or {},
            signal_direction=signal_direction,
            confidence=graded.confidence,
            grade_floor=self._grade_min_confidence("B+"),
            power_move=fast_thrust_override or is_reversion_scalp,
            reversal_context=_reversal_relief_context,
            day_type=_cs_day_type,
            day_type_confidence=_cs_day_conf,
        )
        if _census_block and not is_reversion_scalp:
            return _build_rejected_candidate(_census_block, "REJECTED - Counter Census")

        # ── Structural price-action patch (SEPARATE, gated; default OFF) ──
        # Adds S/R + swing-structure + candle-pattern + volume-profile awareness.
        # Advisory by default (telemetry only); blocks ADVERSE setups (e.g. LONG
        # into resistance / against structure) when price_action_block_adverse is on.
        # Power-moves / volume-spike thrusts are exempt. Remove the call + helper +
        # intelligence/price_action.py to fully drop the feature.
        _pa_ctx = price_action_context(scanner, instrument, best_tf, best_sig, settings)
        if _pa_ctx is not None:
            try:
                scanner._last_price_action_ctx = _pa_ctx
            except Exception:
                pass
            if (
                _setting_enabled(getattr(settings, "price_action_block_adverse", False), False)
                and not fast_thrust_override
                and _pa_ctx.get("verdict") == "ADVERSE"
            ):
                if _setting_enabled(
                    getattr(settings, "price_action_reversal_relief_enabled", True),
                    True,
                ) and _reversal_relief_context:
                    try:
                        _pa_ctx["relief_reason"] = _reversal_relief_context.get(
                            "reason",
                            "confirmed reversal relief",
                        )
                    except Exception:
                        pass
                else:
                    msg = (
                        f"Price-action ADVERSE [{_pa_ctx.get('score', 0):+.2f}: "
                        f"{', '.join(_pa_ctx.get('reasons', [])[:3])}]"
                    )
                    return _build_rejected_candidate(msg, "REJECTED - Price Action")

        if inst_type == "OPT":
            option_pref = (getattr(scanner, "inst_pref", "AUTO") or "AUTO").upper()
            if best_tf == "5min" and is_choppy and not (
                volume_spike_entry
                or quad_union_entry
                or quality_hybrid_entry
                or late_session_reclaim_entry
            ):
                min_conf_5m = max(
                    self._relaxed_threshold(
                        float(getattr(settings, "ut_5min_option_min_confidence", 0.60) or 0.60),
                        settings,
                    ),
                    self._relaxed_threshold(
                        float(getattr(settings, "live_5min_choppy_min_confidence", 0.70) or 0.70),
                        settings,
                    ),
                )
                if not true_5min_impulse:
                    msg = (
                        f"5min choppy option needs >= {min_conf_5m:.0%} confidence plus true impulse, "
                        "or special B+/A/A+ >=60% with strong ADX/intel and healthy option premium"
                    )
                    logger.info(f"Skipping option signal for {instrument}: {msg}")
                    return _build_rejected_candidate(msg, "REJECTED - 5min Choppy Opt")
                if extreme_exhaustion and not is_explosive_bypass:
                    msg = "Extreme ADX exhaustion in choppy regime needs 15min agreement plus follow-through"
                    logger.info(f"Skipping option signal for {instrument}: {msg}")
                    return _build_rejected_candidate(msg, "REJECTED - Exhaustion Risk")
            option_quality_ok = self._option_grade_allowed(
                graded.grade,
                graded.confidence,
                regime,
                adx_value=getattr(best_sig, "adx_value", 0.0),
                intel_score=intel_score,
                signal_score=best_score,
                five_min_true_impulse=true_5min_impulse,
                timeframe=best_tf,
            )
            if not option_quality_ok and not (
                volume_spike_entry
                or quad_union_entry
                or quality_hybrid_entry
                or late_session_reclaim_entry
            ):
                if option_pref == "OPT":
                    msg = "Below premium quality gate"
                    logger.info(f"Skipping option signal below premium quality gate for {instrument} at {best_sig.timestamp}")
                    return _build_rejected_candidate(msg, "REJECTED - Opt Gate")
                inst_type = "FUT"

                # ═══ Cross-Timeframe Monopoly Guard ═══
        existing_index_trades = [
            t
            for t in scanner.trades.open_trades.values()
            if t.instrument == instrument and self._is_live_book_trade(t)
        ]
        concurrency_guard_on = _setting_enabled(getattr(settings, "ut_concurrency_guard", True), True)
        cascade_multi_tf = (
            one_way_cascade_entry
            and _setting_enabled(getattr(settings, "cascade_multi_tf_stack_enabled", True), True)
        )
        if existing_index_trades and concurrency_guard_on and not quad_prime_entry:
            # Cascade multi-TF: allow additional TFs up to cascade cap (no forced FUT/OPT flip).
            if cascade_multi_tf:
                primary_max = max(1, int(getattr(settings, "cascade_primary_max_tf", 3) or 3))
                dual_max = max(0, int(getattr(settings, "cascade_dual_addon_max", 2) or 2))
                cascade_cap = primary_max + dual_max
                same_tf_open = any(
                    str(getattr(t, "timeframe", "") or "") == best_tf
                    and (
                        self._signal_source_lane(t, scanner_engine_mode)
                        == best_source_lane
                        or not best_source_lane
                    )
                    for t in existing_index_trades
                )
                if same_tf_open and best_source_lane != "B":
                    msg = f"Cascade same-TF {best_tf} already active"
                    logger.info(f"🚫 Monopoly Guard: {msg}. Skipping.")
                    return _build_rejected_candidate(msg, "REJECTED - Monopoly")
                if len(existing_index_trades) >= cascade_cap:
                    msg = f"Cascade multi-TF cap {cascade_cap} already filled"
                    logger.info(f"🚫 Monopoly Guard: {msg}. Skipping {best_tf}.")
                    return _build_rejected_candidate(msg, "REJECTED - Monopoly")
                logger.info(
                    f"🌟 Cascade multi-TF stack ({best_tf}): allowing alongside "
                    f"{len(existing_index_trades)} open on {instrument}"
                )
            else:
                # Check if this is a DIFFERENT best_tf
                is_different_tf = all(getattr(t, "timeframe", "") != best_tf for t in existing_index_trades)
                candidate_rank = self._grade_rank(graded.grade)
                candidate_conf = float(getattr(graded, "confidence", 0.0) or 0.0)

                def _same_grade_conf_or_better(existing_trade) -> bool:
                    existing_rank = self._grade_rank(getattr(existing_trade, "grade", ""))
                    existing_conf = float(getattr(existing_trade, "confidence", 0.0) or 0.0)
                    if candidate_rank > existing_rank:
                        return True
                    return candidate_rank == existing_rank and candidate_conf + 1e-9 >= existing_conf

                can_add = False
                if (
                    is_different_tf
                    and len(existing_index_trades) == 1
                    and _same_grade_conf_or_better(existing_index_trades[0])
                ):
                    # Force switch type (FUT -> OPT or vice versa)
                    existing_trade = existing_index_trades[0]
                    existing_type = getattr(existing_trade, 'inst_type', 'FUT')
                    
                    # Respect instrument preference!
                    if scanner.inst_pref == "FUT" and existing_type == "FUT":
                        msg = "Already have FUT trade and preference is FUT"
                        logger.info(f"🚫 Monopoly Guard: {msg}. Skipping.")
                        return _build_rejected_candidate(msg, "REJECTED - Monopoly")
                    elif scanner.inst_pref == "OPT" and existing_type == "OPT":
                        msg = "Already have OPT trade and preference is OPT"
                        logger.info(f"🚫 Monopoly Guard: {msg}. Skipping.")
                        return _build_rejected_candidate(msg, "REJECTED - Monopoly")
                    
                    # If no preference conflict, force switch type
                    inst_type = "OPT" if existing_type == "FUT" else "FUT"
                    can_add = True
                    logger.info(f"🌟 Cross-TF Confluence ({best_tf}): Adding {inst_type} to complement {getattr(existing_trade, 'timeframe', '')} {existing_type}")

                # Rare dual-TF: same-direction, both excellent, different TF →
                # allow second leg as OPT (never 2x FUT) even when preference is OPT.
                if (
                    not can_add
                    and is_different_tf
                    and len(existing_index_trades) == 1
                    and _setting_enabled(getattr(settings, "rare_dual_tf_entry_enabled", True), True)
                ):
                    existing_trade = existing_index_trades[0]
                    min_grade = str(getattr(settings, "rare_dual_tf_min_grade", "A") or "A").upper()
                    min_conf = float(getattr(settings, "rare_dual_tf_min_confidence", 0.70) or 0.70)
                    min_rank = self._grade_rank(min_grade)
                    ex_dir = str(getattr(existing_trade, "direction", "") or "").upper()
                    ex_tf = str(getattr(existing_trade, "timeframe", "") or "")
                    ex_grade = str(getattr(existing_trade, "grade", "") or "")
                    ex_conf = float(getattr(existing_trade, "confidence", 0.0) or 0.0)
                    same_dir = ex_dir == best_direction
                    tfs_pair = {ex_tf, best_tf}
                    has_fast = "1min" in tfs_pair
                    has_struct = bool(tfs_pair & {"5min", "15min"})
                    both_strong = (
                        self._grade_rank(ex_grade) >= min_rank
                        and ex_conf + 1e-9 >= min_conf
                        and candidate_rank >= min_rank
                        and candidate_conf + 1e-9 >= min_conf
                    )
                    if same_dir and has_fast and has_struct and both_strong:
                        prefer_opt = _setting_enabled(
                            getattr(settings, "rare_dual_tf_prefer_opt_second", True), True
                        )
                        existing_type = str(getattr(existing_trade, "inst_type", "FUT") or "FUT").upper()
                        if prefer_opt or existing_type == "FUT":
                            # Second leg OPT so we never dual FUT.
                            if existing_type == "FUT" or scanner.inst_pref != "FUT":
                                inst_type = "OPT"
                                can_add = True
                                logger.info(
                                    f"🌟 Rare dual-TF monopoly allow ({best_tf}): "
                                    f"OPT alongside {ex_tf} {existing_type} "
                                    f"(both ≥{min_grade}/{min_conf:.0%})"
                                )

                if not can_add:
                    msg = f"Already has an active {getattr(existing_index_trades[0], 'timeframe', '')} trade"
                    logger.info(f"🚫 Monopoly Guard: {instrument} {msg}. Skipping {best_tf}.")
                    return _build_rejected_candidate(msg, "REJECTED - Monopoly")

        option_type = "CE" if best_sig.signal_type == "BUY" else "PE"
        strike_interval = cfg.get("strike_interval", 50)
        
        atm_strike_val = round(spot / strike_interval) * strike_interval if spot > 0 else 0
        itm_strike_val = (atm_strike_val - strike_interval) if option_type == "CE" else (atm_strike_val + strike_interval)
        
        strike_selection = getattr(settings, "option_strike_selection", "ATM")
        target_strikes = []
        if inst_type == "FUT":
            target_strikes = [0.0]
        else:
            if strike_selection == "ATM": target_strikes = [atm_strike_val]
            elif strike_selection == "ITM": target_strikes = [itm_strike_val]
            else: target_strikes = [atm_strike_val, itm_strike_val]

        # Read ONCE whenever an option needs either strike ranking or absolute
        # liquidity admission. allow_fetch=False means this uses the chain already
        # in cache or nothing at all -- no broker call and no latency added to entry.
        _liquidity_chain = None
        _needs_liquidity_chain = str(inst_type or "").upper() == "OPT" and (
            _setting_enabled(
                getattr(settings, "option_contract_liquidity_gate_enabled", True), True
            )
            or (
                len(target_strikes) > 1
                and _setting_enabled(
                    getattr(settings, "option_strike_liquidity_ranking_enabled", True), True
                )
            )
        )
        if _needs_liquidity_chain:
            try:
                _liquidity_chain, _ = scanner._get_option_chain_cached(
                    instrument, cfg, allow_fetch=False
                )
            except Exception as exc:
                logger.debug(f"Strike liquidity chain unavailable for {instrument}: {exc}")
                _liquidity_chain = None

        results_list = []
        for strike in target_strikes:
            if inst_type == "FUT":
                option_type_local = ""
                strike_local = 0.0
                instrument_multiplier = 1.0
            else:
                option_type_local = option_type
                strike_local = strike
                # Per-strike Black-Scholes delta (ITM/OTM aware), not the flat ATM delta.
                try:
                    dte_val = scanner.expiry.get_dte(instrument, best_sig.timestamp.date())
                except Exception:
                    dte_val = 1
                instrument_multiplier = self._option_signed_delta(
                    spot, strike, option_type_local, intel_result, dte_val,
                    0.5 if option_type_local == "CE" else -0.5,
                )

            final_rr = self._dynamic_rr(best_grade, best_conf, intel_score, timeframe=best_tf)
            if volume_spike_entry or market_ltp_shock_entry:
                if best_tf == "1min" or market_ltp_shock_entry:
                    # Late shock dumps/rallies are short-horizon scalp/OPT paths.
                    final_rr = float(
                        getattr(settings, "volume_spike_entry_1min_scalp_rr", 1.5)
                        or 1.5
                    )
                else:
                    final_rr = max(
                        final_rr,
                        float(getattr(settings, "volume_spike_entry_min_rr", 2.5) or 2.5),
                    )
            if late_session_reclaim_entry:
                final_rr = float(getattr(settings, "late_session_reclaim_rr", 1.5) or 1.5)
            if one_way_cascade_entry:
                final_rr = float(getattr(settings, "one_way_cascade_rr", 1.5) or 1.5)
            if is_reversion_scalp:
                # RR 1.5 floor (aligned with 1m spike scalp RR).
                final_rr = max(
                    1.5,
                    float(getattr(settings, "post_exit_reversion_min_rr", 1.5) or 1.5),
                )
            direction = "LONG" if best_sig.signal_type == "BUY" else "SHORT"
            
            quality_factor = min(1.0, (best_conf + (0.1 if best_grade in ["A", "A+"] else 0.0)))
            # RR-scaled sizing. The legacy `1.0 if final_rr >= 1.5 else final_rr/1.5`
            # was dead code — dynamic_rr already floors at 1.5R, so it was always 1.0 and never
            # shrank size. Gate a real implementation behind an explicit setting
            # (default OFF = full size, preserving live behavior); when enabled, size
            # scales down for low-RR setups toward rr_size_floor and reaches full size
            # once final_rr >= rr_size_reference_rr.
            if _setting_enabled(getattr(settings, "rr_size_scaling_enabled", False), False):
                ref_rr = max(1e-6, float(getattr(settings, "rr_size_reference_rr", 2.5) or 2.5))
                rr_floor = float(getattr(settings, "rr_size_floor", 0.5) or 0.5)
                rr_factor = max(rr_floor, min(1.0, float(final_rr) / ref_rr))
            else:
                rr_factor = 1.0
            total_multiplier = quality_factor * rr_factor
            cap_based_lots = None
            effective_fut_sl_pct = None

            if inst_type == "FUT":
                if (
                    volume_spike_entry
                    or market_ltp_shock_entry
                    or late_session_reclaim_entry
                    or one_way_cascade_entry
                    or is_reversion_scalp
                ):
                    if is_reversion_scalp:
                        effective_fut_sl_pct = self._tight_futures_stop_pct(
                            scanner.futures_sl_pct,
                            settings,
                            "reversion_scalp_futures_sl_pct",
                            0.05,
                        )
                    elif one_way_cascade_entry:
                        effective_fut_sl_pct = self._tight_futures_stop_pct(
                            scanner.futures_sl_pct,
                            settings,
                            "one_way_cascade_futures_sl_pct",
                            0.05,
                        )
                    elif late_session_reclaim_entry:
                        effective_fut_sl_pct = self._tight_futures_stop_pct(
                            scanner.futures_sl_pct,
                            settings,
                            "late_session_reclaim_futures_sl_pct",
                            0.05,
                        )
                    else:
                        # volume spike + market LTP shock share tight FUT stop
                        effective_fut_sl_pct = self._volume_spike_futures_stop_pct(
                            scanner.futures_sl_pct,
                            settings,
                        )
                    index_stop_distance = min(
                        max(0.0, float(best_sig.stop_distance or 0.0)),
                        max(0.0, float(spot or 0.0))
                        * (effective_fut_sl_pct / 100.0),
                    )
                else:
                    effective_fut_sl_pct = futures_stop_tolerance_pct(
                        scanner.futures_sl_pct,
                        regime,
                    )
                    index_stop_distance = self._futures_stop_distance(
                        best_sig.stop_distance,
                        spot,
                        scanner.futures_sl_pct,
                        regime,
                    )

                    stop_rejection = self._futures_stop_distance_rejection(
                        best_sig.stop_distance,
                        spot,
                        scanner.futures_sl_pct,
                        safety_multiplier=float(getattr(settings, "futures_sl_safety_multiplier", 2.0) or 2.0),
                        regime=regime,
                    )
                    if stop_rejection:
                        return _build_rejected_candidate(
                            stop_rejection,
                            "REJECTED - FUT Stop Distance",
                        )
                # Final lots come from resolve_trade_lots (fixed settings lots).
            else:
                est_premium = _estimate_opt_premium(
                    spot, getattr(self, "scanner", None), instrument, _sig_date(best_sig)
                )
                # No capital hardcap on lots when auto sizing is OFF (default).
                cap_based_lots = None
                
                option_stop_pct = (
                    float(getattr(settings, "one_way_cascade_options_sl_pct", 5.0) or 5.0)
                    if one_way_cascade_entry
                    else
                    float(getattr(settings, "late_session_reclaim_options_sl_pct", 5.0) or 5.0)
                    if late_session_reclaim_entry
                    else
                    float(getattr(settings, "volume_spike_entry_options_sl_pct", 5.0) or 5.0)
                    if (volume_spike_entry or market_ltp_shock_entry or is_reversion_scalp)
                    else float(scanner.options_sl_pct or 0.0)
                )
                max_premium_sl_pts = est_premium * (option_stop_pct / 100.0)
                hard_cap_index_sl = max_premium_sl_pts / max(0.1, abs(instrument_multiplier))
                # Option premium risk and underlying index risk are separate ceilings.
                # The 15% premium cap used to permit roughly 0.36% of NIFTY spot at
                # ATM delta, even though the approved underlying regime ceiling is
                # 0.10-0.15%. Clamp the underlying geometry independently before it is
                # converted into premium space.
                policy_caps = getattr(
                    settings, "entry_policy_underlying_stop_max_pct_by_index", {}
                )
                try:
                    configured_index_cap = effective_underlying_stop_cap_pct(
                        instrument,
                        (policy_caps or {}).get(instrument, scanner.futures_sl_pct),
                    )
                except (AttributeError, TypeError, ValueError):
                    configured_index_cap = float(scanner.futures_sl_pct or 0.0)
                regime_index_cap = futures_stop_tolerance_pct(
                    configured_index_cap, regime
                )
                policy_index_sl = max(0.0, float(spot or 0.0)) * (
                    regime_index_cap / 100.0
                )
                index_stop_distance = min(
                    best_sig.stop_distance,
                    hard_cap_index_sl,
                    policy_index_sl,
                )

            if direction == "LONG":
                entry_stop = spot - index_stop_distance
                target = spot + (index_stop_distance * final_rr)
            else:
                entry_stop = spot + index_stop_distance
                target = spot - (index_stop_distance * final_rr)

            trading_symbol = ""
            symbol_token = ""
            if inst_type == "FUT":
                info = scanner.market_info.get(instrument, {})
                trading_symbol = info.get("current_fut", "")
                symbol_token = info.get("current_fut_token", "")
            else:
                opt_info = scanner.data.get_option_token(instrument, strike_local, option_type_local)
                if opt_info:
                    trading_symbol = opt_info['symbol']
                    symbol_token = opt_info['token']

            # Never emit an OPT entry candidate without a tradeable contract token: the
            # premium-space stop/target are computed only inside the `if symbol_token:`
            # block below, so a token-less option would otherwise carry a spot-space stop
            # on a premium instrument. Skip it (an untradeable contract anyway).
            if inst_type == "OPT" and not symbol_token:
                logger.warning(
                    "Skipping live option candidate {} {}: no contract token for strike {} {}",
                    instrument,
                    best_tf,
                    strike_local,
                    option_type_local,
                )
                continue

            if symbol_token:
                strike_alpha = (instrument_multiplier * 0.4)
                oi_score = intel_result.get("oi", {}).get("score", 50) / 100.0
                vol_score = intel_result.get("volume", {}).get("score", 50) / 100.0
                itm_bonus = 0.15 if (strike_local == itm_strike_val and inst_type == "OPT") else 0.0
                final_strike_score = strike_alpha + (oi_score * 0.3) + (vol_score * 0.3) + itm_bonus

                live_trade_price = spot
                exchange = "BFO" if _is_bfo_instrument(instrument) else "NFO"
                candidate_stop = entry_stop
                candidate_target = target
                
                if symbol_token:
                    try:
                        if inst_type == "OPT":
                            quote_probe = SimpleNamespace(
                                instrument=instrument,
                                inst_type="OPT",
                                option_type=option_type_local,
                                atm_strike=strike_local,
                                trading_symbol=trading_symbol,
                                symbol_token=symbol_token,
                                price=spot,
                                target=target,
                                entry_spot=spot,
                            )
                            live_premium = (
                                scanner._get_live_premium_cached(quote_probe)
                                if hasattr(scanner, "_get_live_premium_cached")
                                else self._contract_ltp(scanner, exchange, trading_symbol, symbol_token)
                            )
                            live_premium = float(live_premium or 0.0)
                            if live_premium <= 0:
                                # Bounded retry before dropping: transient chain/quote
                                # hiccups cost 16 tradeable candidates on 2026-07-03.
                                import time as _time
                                retry_attempts = max(0, int(getattr(settings, "option_quote_retry_attempts", 2) or 0))
                                retry_delay = max(0.1, float(getattr(settings, "option_quote_retry_delay_seconds", 0.5) or 0.5))
                                for _ in range(retry_attempts):
                                    _time.sleep(retry_delay)
                                    live_premium = float(
                                        (
                                            scanner._get_live_premium_cached(quote_probe)
                                            if hasattr(scanner, "_get_live_premium_cached")
                                            else self._contract_ltp(scanner, exchange, trading_symbol, symbol_token)
                                        )
                                        or 0.0
                                    )
                                    if live_premium > 0:
                                        logger.info(
                                            "Option quote recovered on retry for {} {} ({})",
                                            instrument,
                                            best_tf,
                                            trading_symbol,
                                        )
                                        break
                            if live_premium <= 0:
                                logger.warning(
                                    "Skipping live option candidate {} {}: no verified LTP for {} ({})",
                                    instrument,
                                    best_tf,
                                    trading_symbol,
                                    symbol_token,
                                )
                                continue
                            live_trade_price = live_premium
                            if (
                                volume_spike_entry
                                or market_ltp_shock_entry
                                or late_session_reclaim_entry
                                or one_way_cascade_entry
                                or is_reversion_scalp
                            ):
                                tight_pct = (
                                    float(getattr(settings, "one_way_cascade_options_sl_pct", 5.0) or 5.0)
                                    if one_way_cascade_entry
                                    else
                                    float(getattr(settings, "late_session_reclaim_options_sl_pct", 5.0) or 5.0)
                                    if late_session_reclaim_entry
                                    else float(getattr(settings, "volume_spike_entry_options_sl_pct", 5.0) or 5.0)
                                )
                                max_sl_dist = max(0.5, live_trade_price * (tight_pct / 100.0))
                            else:
                                max_sl_dist = self._option_premium_stop_distance(
                                    live_trade_price,
                                    scanner,
                                    regime,
                                    timeframe=best_tf,
                                    atr_premium_stop=float(index_stop_distance) * abs(float(instrument_multiplier or 0.0)),
                                    settings=settings,
                                    intel_score=float(intel_score or 0.0),
                                )
                            candidate_stop = live_trade_price - max_sl_dist
                            candidate_target = live_trade_price + (max_sl_dist * final_rr)
                        else: # FUT
                            live_fut_ltp = self._contract_ltp(scanner, exchange, trading_symbol, symbol_token)
                            live_trade_price = live_fut_ltp if live_fut_ltp > 0 else spot
                            max_sl_dist = self._futures_stop_distance(
                                index_stop_distance,
                                live_trade_price,
                                scanner.futures_sl_pct,
                                regime,
                            )
                            if direction == "LONG":
                                candidate_stop = live_trade_price - max_sl_dist
                                candidate_target = live_trade_price + (max_sl_dist * final_rr)
                            else:
                                candidate_stop = live_trade_price + max_sl_dist
                                candidate_target = live_trade_price - (max_sl_dist * final_rr)
                    except Exception as e:
                        if inst_type == "OPT":
                            logger.warning(
                                "Skipping live option candidate {} {} after quote failure for {}: {}",
                                instrument,
                                best_tf,
                                trading_symbol,
                                e,
                            )
                            continue
                        else:
                            live_trade_price = spot
                            max_sl_dist = self._futures_stop_distance(
                                index_stop_distance,
                                live_trade_price,
                                scanner.futures_sl_pct,
                                regime,
                            )
                            if direction == "LONG":
                                candidate_stop = live_trade_price - max_sl_dist
                                candidate_target = live_trade_price + (max_sl_dist * final_rr)
                            else:
                                candidate_stop = live_trade_price + max_sl_dist
                                candidate_target = live_trade_price - (max_sl_dist * final_rr)
                
                no_entry_label, _ = _entry_cutoff(settings, best_tf)
                # A confirmed power-move may enter past the no-entry cutoff, but only
                # up to the hard late cutoff so there is still time before square-off.
                power_move_cutoff_ok = bool(
                    best_after_no_entry
                    and fast_thrust_override
                    and scanner._power_move_late_cutoff_ok(best_sig.timestamp)
                )
                effective_no_entry = best_after_no_entry and not power_move_cutoff_ok
                candidate_status = f"NO ENTRY - AFTER {no_entry_label}" if effective_no_entry else "TRADE SIGNAL"
                candidate_action = "NO_ENTRY" if effective_no_entry else "ENTRY"
                candidate_reasons = list(best_reasons or [])
                if effective_no_entry:
                    candidate_reasons.append(f"After no-fresh-entry gate ({no_entry_label} IST)")
                elif power_move_cutoff_ok:
                    candidate_reasons.append(
                        f"⚡ Fast-thrust override: late entry allowed past {no_entry_label} cutoff"
                    )
                if volume_spike_entry:
                    volume_evidence = self._volume_spike_metric_text(
                        volume_spike_metrics, settings, instrument, best_tf
                    )
                    candidate_reasons.append(
                        f"⚡ Volume spike entry: {volume_evidence}, "
                        f"{volume_spike_metrics.get('range_atr_ratio', 0.0):.2f}x range, "
                        f"{volume_spike_metrics.get('close_location', 0.0):.0%} directional close"
                    )
                if market_ltp_shock_entry:
                    candidate_reasons.append(
                        f"⚡ Market LTP shock {market_ltp_shock_context.get('side', '')}: "
                        f"avg |move|={float(market_ltp_shock_context.get('avg_abs_move_pct', 0.0) or 0.0):.2f}%, "
                        f"indices={', '.join(market_ltp_shock_context.get('indices', []) or [])}, "
                        f"RR {final_rr:.1f}, short stab"
                    )
                if volume_spike_entry and best_tf == "1min":
                    candidate_reasons.append(
                        f"1min scalp mode: fast thrust entry with RR {final_rr:.1f}, tight stop, and scalp-lock"
                    )
                if late_session_reclaim_entry:
                    _late_move_label = str(
                        (_reversal_relief_context or {}).get("move_label")
                        or ("reclaim" if str(best_sig.signal_type).upper() == "BUY" else "rollover")
                    )
                    candidate_reasons.append(
                        f"Late-session synchronized {str(best_sig.signal_type).upper()} {_late_move_label}: tight {float(getattr(settings, 'late_session_reclaim_options_sl_pct', 5.0) or 5.0):.1f}% option stop, RR {final_rr:.1f}, scalp-lock"
                    )
                if one_way_cascade_entry:
                    candidate_reasons.append(
                        f"One-way cascade {str(best_sig.signal_type).upper()}: tight {float(getattr(settings, 'one_way_cascade_options_sl_pct', 5.0) or 5.0):.1f}% option stop, RR {final_rr:.1f}, runner trail {float(getattr(settings, 'one_way_cascade_runner_lock_pct', 0.85) or 0.85):.0%}"
                    )
                if quad_union_entry:
                    source_label = "+".join(best_source_engines or [best_source_engine or "quad_union"])
                    entry_label = (
                        "Adaptive dual"
                        if scanner_engine_mode == "adaptive_dual"
                        else "Adaptive hybrid"
                        if scanner_engine_mode == "adaptive_hybrid"
                        else ("Quality hybrid" if scanner_engine_mode == "quality_hybrid" else ("Quad prime" if scanner_engine_mode == "quad_prime" else "Quad union"))
                    )
                    candidate_reasons.append(
                        f"{entry_label} raw entry after repaint buffer: {source_label}"
                    )
                if inst_type == "FUT":
                    candidate_reasons.append(
                        f"FUT stop tolerance: {float(effective_fut_sl_pct or 0.0):.3f}% "
                        f"for {str(regime or 'UNKNOWN').upper()} "
                        f"(configured cap {float(scanner.futures_sl_pct):.3f}%)"
                    )

                if live_trade_price <= 0:
                    logger.warning(f"Skipping candidate for {instrument} due to invalid live price: {live_trade_price}")
                    continue

                requested_lots = (
                    int(getattr(scanner, "user_lots", {}).get(instrument, 1) or 1)
                    if inst_type == "OPT"
                    else int(getattr(scanner, "user_lots_fut", {}).get(instrument, 1) or 1)
                )
                # Lot scale: cascade OPT 2–3×; heavy-trend (2/4 align) OPT 2×; else 1×.
                # FUT always 1×. A+/power_move alone never multiplies.
                cascade_scale = bool(one_way_cascade_entry)
                heavy_trend_scale = False
                if not cascade_scale and str(inst_type or "").upper() == "OPT":
                    try:
                        _ht_fn = getattr(scanner, "_heavy_trend_lot_scale_eligible", None)
                        if callable(_ht_fn):
                            heavy_trend_scale = bool(
                                _ht_fn(instrument, best_direction, best_tf)
                            )
                    except Exception:
                        heavy_trend_scale = False
                # Scaling is not a breakout detector.  It is permitted only
                # after the locked compression box has a confirmed breach and
                # the current canonical regime agrees with this trade.
                # Missing compression evidence is fail-closed during live sizing.
                _compression_state = str((_session_ctx or {}).get("compression_state") or "FORMING").upper()
                _compression_box = (_session_ctx or {}).get("compression_box", {}) or {}
                _breakout_direction = str(_compression_box.get("breakout_direction") or "").upper()
                _signal_breakout_direction = "UP" if str(best_sig.signal_type).upper() == "BUY" else "DOWN"
                _confirmed_breakout_valid = bool(
                    _compression_state == "BREAKOUT_CONFIRMED"
                    and _compression_box.get("live_valid") is True
                    and _breakout_direction == _signal_breakout_direction
                )
                _compression_allows_scale = bool(
                    _compression_state == "UNRESTRICTED"
                    or _confirmed_breakout_valid
                )
                _strict_trend = bool(
                    _compression_allows_scale
                    and is_trending_regime(str(regime or ""))
                    and regime_aligns(str(regime or ""), best_sig.signal_type)
                )
                cascade_scale = bool(cascade_scale and _strict_trend)
                heavy_trend_scale = bool(heavy_trend_scale and _strict_trend)
                _effective_compression_state = (
                    "REVERSION_LOCKED"
                    if _compression_state == "BREAKOUT_CONFIRMED"
                    and not _confirmed_breakout_valid
                    else _compression_state
                )
                size_result = resolve_trade_lots(
                    settings=settings,
                    inst_type=inst_type,
                    requested_lots=requested_lots,
                    lot_size=lot_size,
                    entry_price=float(live_trade_price or 0.0),
                    stop_price=float(candidate_stop or 0.0),
                    cap_lots=cap_based_lots if inst_type == "OPT" else None,
                    cascade_scale=cascade_scale,
                    heavy_trend_scale=heavy_trend_scale,
                    high_conviction=False,
                    compression_state=_effective_compression_state,
                    intel_score=float(intel_score or 0.0),
                )
                if size_result.lots < 1:
                    logger.info(
                        "Skipping %s %s %s: %s",
                        instrument,
                        best_tf,
                        inst_type,
                        size_result.reason,
                    )
                    continue
                actual_lots = size_result.lots
                setattr(best_sig, "lot_sizing_mode", str(size_result.mode or "fixed"))
                setattr(best_sig, "sizing_compression_state", _effective_compression_state)
                if size_result.mode == "cascade":
                    candidate_reasons.append(
                        f"Cascade lot scale: {actual_lots} lot(s) "
                        f"(base {requested_lots}; OPT 2–3× max, FUT 1×)"
                    )
                elif size_result.mode == "heavy_trend":
                    candidate_reasons.append(
                        f"Heavy-trend lot scale: {actual_lots} lot(s) "
                        f"(base {requested_lots}; OPT 2× only, 2/4 index align)"
                    )
                else:
                    candidate_reasons.append(
                        f"Fixed lot sizing: {actual_lots} lot(s) from settings "
                        f"(1×; auto risk sizing OFF; stop from settings SL%)"
                    )

                candidate = TradeCandidate(
                    instrument=instrument, direction=direction, price=live_trade_price,
                    stop=candidate_stop, target=candidate_target, lots=actual_lots, lot_size=lot_size, grade=best_grade,
                    confidence=best_conf, timeframe=best_tf, inst_type=inst_type, option_type=option_type_local,
                    atm_strike=strike_local, multiplier=instrument_multiplier, trading_symbol=trading_symbol,
                    symbol_token=symbol_token, rr=round(final_rr, 2), score=final_strike_score,
                    reasons=candidate_reasons, signal_timestamp=best_sig.timestamp,
                    entry_spot=float(spot or 0.0),
                    spot_stop=float(entry_stop or 0.0),
                    spot_target=float(target or 0.0),
                    underlying_entry=float(spot or 0.0),
                    underlying_stop=float(entry_stop or 0.0),
                    underlying_target=float(target or 0.0),
                    premium_entry=float(live_trade_price or 0.0) if inst_type == "OPT" else 0.0,
                    premium_stop=float(candidate_stop or 0.0) if inst_type == "OPT" else 0.0,
                    premium_target=float(candidate_target or 0.0) if inst_type == "OPT" else 0.0,
                    geometry_observed_at=(
                        best_sig.timestamp.isoformat()
                        if isinstance(best_sig.timestamp, datetime)
                        else str(best_sig.timestamp or "")
                    ),
                    geometry_source="signal_processor.dual_geometry",
                    geometry_verified=True,
                    status=candidate_status,
                    action=candidate_action,
                    volume_spike_entry=volume_spike_entry,
                    market_ltp_shock_entry=market_ltp_shock_entry,
                    late_session_reclaim_entry=late_session_reclaim_entry,
                    one_way_cascade_entry=one_way_cascade_entry,
                    volume_ratio=float(volume_spike_metrics.get("volume_ratio", 0.0) or 0.0),
                    range_atr_ratio=float(volume_spike_metrics.get("range_atr_ratio", 0.0) or 0.0),
                    close_location=float(volume_spike_metrics.get("close_location", 0.0) or 0.0),
                    initial_stop=float(candidate_stop or 0.0),
                    source_engine=best_source_engine,
                    source_engines=list(best_source_engines),
                    source_lane=best_source_lane,
                    quad_union_entry=bool(quad_union_entry),
                    raw_union_entry=bool(quad_union_entry),
                    tight_stop_pct=(
                        float(
                            (
                                getattr(settings, "one_way_cascade_options_sl_pct", 5.0)
                                if one_way_cascade_entry
                                else getattr(settings, "late_session_reclaim_options_sl_pct", 5.0)
                                if late_session_reclaim_entry
                                else getattr(settings, "volume_spike_entry_options_sl_pct", 5.0)
                            )
                            if inst_type == "OPT"
                            else effective_fut_sl_pct
                        )
                        if (
                            volume_spike_entry
                            or market_ltp_shock_entry
                            or late_session_reclaim_entry
                            or one_way_cascade_entry
                            or is_reversion_scalp
                        )
                        else 0.0
                    ),
                )
                candidate.quote_verified = bool(inst_type != "OPT" or live_trade_price > 0)
                candidate.quote_source = (
                    str(getattr(quote_probe, "quote_source", "") or "broker_ltp")
                    if inst_type == "OPT"
                    else "contract_ltp"
                )
                carry_grading_telemetry(candidate, best_sig)
                setattr(candidate, "source_signal_type", best_sig.signal_type)
                setattr(candidate, "source_signal_price", float(best_sig.price or 0.0))
                setattr(candidate, "source_signal_bar_index", int(getattr(best_sig, "bar_index", -1) or -1))
                setattr(candidate, "source_engine", best_source_engine)
                setattr(candidate, "source_engines", list(best_source_engines))
                setattr(candidate, "source_lane", best_source_lane)
                setattr(candidate, "quad_union_entry", bool(quad_union_entry))
                setattr(candidate, "raw_union_entry", bool(quad_union_entry))
                setattr(candidate, "late_session_reclaim_entry", bool(late_session_reclaim_entry))
                if late_session_reclaim_entry and isinstance(_reversal_relief_context, dict):
                    setattr(candidate, "late_session_reclaim_context", dict(_reversal_relief_context))
                setattr(candidate, "one_way_cascade_entry", bool(one_way_cascade_entry))
                setattr(candidate, "lot_sizing_mode", str(size_result.mode or "fixed"))
                setattr(candidate, "sizing_compression_state", _effective_compression_state)
                setattr(candidate, "market_ltp_shock_entry", bool(market_ltp_shock_entry))
                if market_ltp_shock_entry and market_ltp_shock_context:
                    setattr(candidate, "market_ltp_shock_context", dict(market_ltp_shock_context))
                setattr(
                    candidate,
                    "market_wide_reversal_entry",
                    bool(getattr(best_sig, "market_wide_reversal_entry", False)),
                )
                if getattr(best_sig, "market_wide_reversal_context", None):
                    setattr(
                        candidate,
                        "market_wide_reversal_context",
                        dict(getattr(best_sig, "market_wide_reversal_context") or {}),
                    )
                setattr(candidate, "is_reversion_scalp", bool(is_reversion_scalp))
                if one_way_cascade_entry and isinstance(_reversal_relief_context, dict):
                    setattr(candidate, "one_way_cascade_context", dict(_reversal_relief_context))
                setattr(candidate, "trend_15m_agrees", bool(trend_15m_agrees))
                # Stamp the intelligence that GRADED this candidate, not the 5-minute
                # snapshot in scope. No-op while per-timeframe grading is off.
                _stamp_regime, _stamp_intel_norm = self._graded_candidate_context(
                    best_sig, regime, intel_norm
                )
                setattr(candidate, "intelligence_score", float(_stamp_intel_norm))
                candidate_intel_result = self._graded_candidate_intel_result(
                    best_sig, intel_result
                )
                if isinstance(candidate_intel_result, dict):
                    setattr(
                        candidate,
                        "market_intelligence_context",
                        {
                            "aggregate": dict(candidate_intel_result.get("aggregate", {}) or {}),
                            "regime": dict(candidate_intel_result.get("regime", {}) or {}),
                            "volume": dict(candidate_intel_result.get("volume", {}) or {}),
                            "order_flow": dict(candidate_intel_result.get("order_flow", {}) or {}),
                            "session_context": dict(candidate_intel_result.get("session_context", {}) or {}),
                            "anchor_regime": dict(candidate_intel_result.get("anchor_regime", {}) or {}),
                            "breadth_context": dict(candidate_intel_result.get("breadth_context", {}) or {}),
                            "top_down_context": dict(candidate_intel_result.get("top_down_context", {}) or {}),
                            "market_structure": dict(candidate_intel_result.get("market_structure", {}) or {}),
                            "aroon": dict(candidate_intel_result.get("aroon_context", {}) or {}),
                            "wavetrend": dict(candidate_intel_result.get("wavetrend", {}) or {}),
                        },
                    )
                setattr(candidate, "strategy_revision", str(getattr(settings, "strategy_revision", "") or ""))
                setattr(candidate, "strategy_direction_mode", str(getattr(settings, "strategy_direction_mode", "") or ""))
                setattr(candidate, "data_provenance", "LIVE" if getattr(scanner, "mode", "") == "REAL" else "BACKTEST")
                if iv_percentile is not None:
                    setattr(candidate, "iv_percentile", float(iv_percentile))
                setattr(candidate, "option_premium_ok", bool(option_premium_ok))
                setattr(candidate, "chain_quality", dict(chain_quality or {}))
                setattr(candidate, "market_regime", _stamp_regime)
                # Tag with the structural price-action verdict (advisory telemetry).
                _pa = getattr(scanner, "_last_price_action_ctx", None)
                if isinstance(_pa, dict):
                    setattr(candidate, "price_action_verdict", str(_pa.get("verdict") or ""))
                    setattr(candidate, "price_action_score", float(_pa.get("score") or 0.0))
                    profile_note = str(_pa.get("target_context") or "")
                    trail_note = str(_pa.get("trailing_context") or "")
                    if profile_note or trail_note:
                        candidate.reasons = list(candidate.reasons or []) + [
                            f"Market-structure context: {profile_note}; {trail_note}"
                        ]
                candidate.adx_value = adx_value
                candidate.impulse_confirmed = bool(impulse_confirmed)
                candidate.is_explosive_bypass = bool(is_explosive_bypass)
                # Power-move: bypass grade floor / late cutoff downstream, enter tight (scalp-lock).
                candidate.power_move = bool(power_move)
                if fast_thrust_override:
                    candidate.scalp_lock_mode = True
                    candidate.reasons = list(candidate.reasons or []) + [
                        "⚡ Fast thrust (data-confirmed): tight stop + scalp-lock risk"
                    ]
                if late_session_reclaim_entry:
                    candidate.scalp_lock_mode = True
                if market_ltp_shock_entry:
                    candidate.scalp_lock_mode = True
                    candidate.reasons = list(candidate.reasons or []) + [
                        "⚡ Market LTP shock (multi-index): tight stop + short stab + scalp-lock"
                    ]
                if is_reversion_scalp:
                    candidate.scalp_lock_mode = True
                    _rev_rr = max(
                        1.5,
                        float(getattr(settings, "post_exit_reversion_min_rr", 1.5) or 1.5),
                    )
                    candidate.reasons = list(candidate.reasons or []) + [
                        f"⚡ Post-exit snapback scalp: RR {_rev_rr:.2f}+, tight stop, scalp-lock"
                    ]
                if one_way_cascade_entry:
                    candidate.scalp_lock_mode = False
                # ── Weak-market scalp mode ──
                # On a confirmed range day a non-burst entry is a scalp, not a
                # runner: arm scalp-lock trailing from entry and tighten the RR
                # target so the book beats charges instead of round-tripping
                # toward a 2.5-4.5R target that chop never pays (2026-07-03).
                # Bursts (thrust / market-wide reversal / explosive) run free.
                try:
                    _wm_ctx = (
                        intel_result.get("session_context", {})
                        if isinstance(intel_result, dict)
                        else {}
                    )
                    _wm_day = str(_wm_ctx.get("day_type") or "").upper()
                    _wm_conf = float(_wm_ctx.get("confidence") or 0.0)
                    if (
                        _setting_enabled(getattr(settings, "weak_market_scalp_mode_enabled", True), True)
                        and _wm_day == "RANGE_DAY"
                        and _wm_conf
                        >= float(getattr(settings, "weak_market_scalp_min_day_confidence", 0.60) or 0.60)
                        and not fast_thrust_override
                        and not bool(getattr(candidate, "market_wide_reversal_entry", False))
                        and not bool(getattr(candidate, "one_way_cascade_entry", False))
                        and not candidate.is_explosive_bypass
                    ):
                        candidate.scalp_lock_mode = True
                        _wm_max_rr = float(getattr(settings, "weak_market_scalp_max_rr", 1.5) or 1.5)
                        _wm_risk = abs(float(candidate.price or 0.0) - float(candidate.stop or 0.0))
                        _wm_span = abs(float(candidate.target or 0.0) - float(candidate.price or 0.0))
                        if _wm_risk > 0 and _wm_span > _wm_risk * _wm_max_rr:
                            _wm_sign = 1.0 if float(candidate.target or 0.0) >= float(candidate.price or 0.0) else -1.0
                            candidate.target = float(candidate.price) + _wm_sign * _wm_risk * _wm_max_rr
                            if hasattr(candidate, "rr"):
                                candidate.rr = _wm_max_rr
                        candidate.reasons = list(candidate.reasons or []) + [
                            f"Weak-market scalp: range day conf {_wm_conf:.0%} -> scalp-lock + RR capped {_wm_max_rr:.1f}"
                        ]
                except Exception as _wm_exc:
                    logger.debug(f"Weak-market scalp mode skipped for {instrument}: {_wm_exc}")
                if candidate.is_explosive_bypass:
                    candidate.reasons = list(candidate.reasons or []) + [
                        f"Confirmed impulse bypass ADX={adx_value:.1f}, intel={normalize_intelligence_score(intel_score):.2f}"
                    ]
                # Tradeability of THIS strike, for the near-tie break below.
                _liquidity = self._strike_liquidity_snapshot(
                    _liquidity_chain, strike_local, option_type_local
                )
                candidate.strike_oi = float(_liquidity["oi"])
                candidate.strike_volume = float(_liquidity["volume"])
                candidate.strike_oi_available = bool(_liquidity["oi_available"])
                candidate.strike_volume_available = bool(
                    _liquidity["volume_available"]
                )
                candidate.strike_liquidity_available = bool(
                    _liquidity["available"]
                )
                # The ITM bonus is a standing PREFERENCE for ITM, not a measurement of
                # this contract. Carried separately so the tie-break can compare the two
                # candidates on measured quality alone: at 0.15 it is three times the
                # default 0.05 band, so leaving it inside the score meant the ATM and ITM
                # scores were never "near" and the liquidity comparison never ran.
                setattr(candidate, "strike_preference_bonus", float(itm_bonus))
                results_list.append(candidate)
                logger.info(
                    f"Resolved candidate: {instrument} {best_tf} {best_sig.signal_type} -> "
                    f"{direction} {inst_type} {option_type_local or 'FUT'} {trading_symbol}"
                    + (" | no-entry display only" if effective_no_entry else "")
                    + (" | ⚡power-move" if power_move else "")
                    + (" | ⚡volume-spike" if volume_spike_entry else "")
                    + (" | ⚡ltp-shock" if market_ltp_shock_entry else "")
                    + (" | late-directional-move" if late_session_reclaim_entry else "")
                    + (" | one-way-cascade" if one_way_cascade_entry else "")
                )

        if not results_list:
            return []

        results_list.sort(key=lambda x: x.score, reverse=True)
        # Score decides. Liquidity only separates candidates the score cannot.
        winner = self._prefer_liquid_strike(results_list, settings) or results_list[0]
        if winner is not results_list[0]:
            logger.info(
                f"Strike liquidity tie-break for {instrument}: "
                f"{getattr(winner, 'trading_symbol', '')} "
                f"(OI {getattr(winner, 'strike_oi', 0.0):,.0f} / "
                f"vol {getattr(winner, 'strike_volume', 0.0):,.0f}) preferred over "
                f"{getattr(results_list[0], 'trading_symbol', '')} "
                f"(OI {getattr(results_list[0], 'strike_oi', 0.0):,.0f} / "
                f"vol {getattr(results_list[0], 'strike_volume', 0.0):,.0f})"
            )
        getattr(scanner, "_live_signal_recheck_eligible", {}).pop(
            f"{instrument}_{best_tf}",
            None,
        )
        
        scanner.log_event(f"🎯 Potential {winner.direction} Signal for {winner.instrument} ({winner.timeframe})", "trade")
        # Cascade multi-TF: emit 1m+5m+15m (+ dual 5/15) siblings in the same wave.
        # Rare dual-TF: when cascade is not active, allow one excellent 1m+5/15 pair.
        out = [winner]
        try:
            siblings = self._emit_cascade_tf_siblings(
                winner, scored, settings, scanner_engine_mode
            )
            if siblings:
                out.extend(siblings)
                stack_desc = [
                    f"{c.timeframe}:{getattr(c, 'source_lane', '') or 'A'}" for c in out
                ]
                logger.info(f"Cascade multi-TF stack for {instrument}: {stack_desc}")
                scanner.log_event(
                    f"Cascade multi-TF stack {instrument}: "
                    f"{len(out)} entries ({', '.join(c.timeframe for c in out)})",
                    "trade",
                )
        except Exception as sib_exc:
            logger.debug(f"Cascade multi-TF siblings skipped for {instrument}: {sib_exc}")
        if len(out) == 1:
            try:
                rare = self._emit_rare_dual_tf_siblings(
                    winner, scored, settings, scanner_engine_mode
                )
                if rare:
                    out.extend(rare)
                    scanner.log_event(
                        f"Rare dual-TF {instrument}: "
                        f"{', '.join(c.timeframe for c in out)}",
                        "trade",
                    )
            except Exception as rare_exc:
                logger.debug(f"Rare dual-TF skipped for {instrument}: {rare_exc}")
        return out
