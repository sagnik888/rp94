"""Pure signal quality, grade, and risk-target policy helpers."""

from __future__ import annotations

from typing import Mapping, Optional

from loguru import logger

from config.settings import get_settings
from engine.intelligence_score import normalize_intelligence_score
from engine.market_regime import is_risky_regime, is_trending_regime


def setting_enabled(value, default: bool = True) -> bool:
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


# ── SINGLE SOURCE OF TRUTH: grade is a display bucket of confidence ──
# Confidence is the only quality metric the engine computes. A "grade" letter
# is purely bucket(confidence) for display/logging. Keep the band edges and the
# rank map in ONE place so nothing can drift.
GRADE_BANDS = (("A+", 0.85), ("A", 0.70), ("B+", 0.55), ("B", 0.40))
GRADE_RANKS = {"C": 0, "B": 1, "B+": 2, "A": 3, "A+": 4, "Recovered": 4}


def confidence_to_grade(confidence) -> str:
    """Map a confidence score to its display grade letter (single source of truth)."""
    conf = float(confidence or 0.0)
    for label, floor in GRADE_BANDS:
        if conf >= floor:
            return label
    return "C"


def is_top_tier_setup(grade, confidence) -> bool:
    """Single source of truth for "is this a top-tier setup?".

    The expression `grade_rank >= A+ OR confidence >= 0.90` was duplicated verbatim in
    trading/trade_manager.py and scanner.py. It is currently harmless REDUNDANCY, because
    grade is bucket(confidence) (see confidence_to_grade) and the A+ band floor is 0.85,
    so any confidence >= 0.90 already grades A+.

    It stops being redundant the moment grade is decoupled from confidence — then a
    grade-C signal whose pillars contradict each other but which posts a high collapsed
    confidence would be promoted to top tier, where it can supersede a properly confirmed
    trade and pass the all-A+ concurrency allowance. Keeping one definition means the two
    call sites cannot drift apart if that decoupling ever happens.
    """
    return grade_rank(grade) >= 4 or float(confidence or 0.0) >= 0.90


def grade_min_confidence(grade: str) -> float:
    """Lowest confidence that still maps to this grade (inverse of confidence_to_grade)."""
    base = str(grade or "C").split()[0]
    return {label: floor for label, floor in GRADE_BANDS}.get(base, 0.0)


def grade_rank(grade: str) -> int:
    base = str(grade or "C").split()[0]
    return GRADE_RANKS.get(base, 0)


def is_choppy_regime(regime: str) -> bool:
    return is_risky_regime(regime)


def relaxed_threshold(value: float, settings) -> float:
    leniency = min(max(float(getattr(settings, "live_filter_leniency_pct", 0.15) or 0.0), 0.0), 0.20)
    return float(value or 0.0) * (1.0 - leniency)


def choppy_confidence_gate(settings) -> float:
    return float(getattr(settings, "live_choppy_gate_confidence", 0.72) or 0.72)


def live_5min_choppy_confidence_floor(settings, regime, trend_15m_agrees: bool) -> float:
    """5min confidence floor for risky (choppy) regimes.

    Default 0.70. When the regime is specifically VOLATILE (not RANGING/UNKNOWN)
    AND the 15m trend agrees with the signal, relax toward 0.65: an aligned
    pullback-entry in a volatile-but-directional tape is not the same risk as
    counter-trend chop. RANGING/UNKNOWN always keep the full floor.
    """
    base = relaxed_threshold(
        float(getattr(settings, "live_5min_choppy_min_confidence", 0.70) or 0.70),
        settings,
    )
    aligned_volatile = (
        str(regime or "").upper() == "VOLATILE"
        and bool(trend_15m_agrees)
        and setting_enabled(
            getattr(settings, "live_5min_volatile_aligned_relax_enabled", True), True
        )
    )
    if aligned_volatile:
        return relaxed_threshold(
            float(getattr(settings, "live_5min_volatile_aligned_min_confidence", 0.65) or 0.65),
            settings,
        )
    return base


def is_fifteen_min_trending_momentum(
    settings,
    regime: str,
    *,
    adx_value: float = 0.0,
    intel_score: float = 0.0,
) -> bool:
    return bool(
        is_trending_regime(regime)
        or float(adx_value or 0.0)
        >= float(getattr(settings, "live_15min_trending_momentum_adx_threshold", 24.0) or 24.0)
        or abs(normalize_intelligence_score(intel_score))
        >= float(getattr(settings, "live_15min_trending_momentum_intel_threshold", 0.60) or 0.60)
    )


def fifteen_min_confidence_floor(
    settings,
    regime: str,
    *,
    adx_value: float = 0.0,
    intel_score: float = 0.0,
) -> float:
    base_floor = float(getattr(settings, "live_15min_min_confidence", 0.62) or 0.62)
    if is_fifteen_min_trending_momentum(
        settings,
        regime,
        adx_value=adx_value,
        intel_score=intel_score,
    ):
        base_floor = min(
            base_floor,
            float(getattr(settings, "live_15min_trending_momentum_min_confidence", 0.58) or 0.58),
        )
    return relaxed_threshold(base_floor, settings)


def fifteen_min_momentum_override_allowed(
    confidence: float,
    settings,
    *,
    volume_ratio: float = 0.0,
    range_atr_ratio: float = 0.0,
    close_location: float = 0.0,
    adx_value: float = 0.0,
    intel_score: float = 0.0,
    trend_15m_agrees: bool = False,
    regime: str = "",
) -> bool:
    """Allow low-grade 15min signals only when the candle proves real momentum.

    This is intentionally independent of the display grade. A C/B 15min arrow may
    pass the grade gate when the source candle itself shows volume thrust, range
    expansion, directional close, and ADX momentum. Only extremely weak signals
    still fail before those momentum checks.
    """
    if not setting_enabled(
        getattr(settings, "live_15min_momentum_override_enabled", True), True
    ):
        return False
    conf = float(confidence or 0.0)
    if conf < relaxed_threshold(
        float(getattr(settings, "live_15min_momentum_min_confidence", 0.20) or 0.20),
        settings,
    ):
        return False
    if float(volume_ratio or 0.0) < float(
        getattr(settings, "live_15min_momentum_volume_ratio", 2.0) or 2.0
    ):
        return False
    if float(range_atr_ratio or 0.0) < float(
        getattr(settings, "live_15min_momentum_range_atr_ratio", 1.25) or 1.25
    ):
        return False
    if float(close_location or 0.0) < float(
        getattr(settings, "live_15min_momentum_close_location", 0.65) or 0.65
    ):
        return False

    adx_ok = float(adx_value or 0.0) >= float(
        getattr(settings, "live_15min_momentum_adx_threshold", 24.0) or 24.0
    )
    if not adx_ok:
        return False

    intel_ok = abs(normalize_intelligence_score(intel_score)) >= float(
        getattr(settings, "live_15min_momentum_intel_threshold", 0.65) or 0.65
    )
    context_ok = bool(trend_15m_agrees or is_trending_regime(regime) or intel_ok)
    return context_ok


def option_premium_health_ok(
    settings,
    *,
    iv_percentile: Optional[float] = None,
    chain_quality: Optional[Mapping] = None,
    explicit_ok: Optional[bool] = None,
) -> bool:
    """Return whether option premium/context is healthy enough for special 5min entries."""
    if explicit_ok is not None:
        return bool(explicit_ok)
    if iv_percentile is None:
        return False
    max_iv = float(getattr(settings, "live_5min_special_max_iv_percentile", 70.0) or 70.0)
    try:
        if float(iv_percentile) > max_iv:
            return False
    except (TypeError, ValueError):
        return False
    if chain_quality:
        min_score = float(getattr(settings, "live_5min_special_min_chain_score", 50.0) or 50.0)
        try:
            score = float(chain_quality.get("score", min_score))
        except (AttributeError, TypeError, ValueError):
            score = 0.0
        if bool(getattr(chain_quality, "get", lambda *_: False)("stale", False)):
            return False
        if score < min_score:
            return False
    return True


def five_min_true_impulse_allowed(
    grade: str,
    confidence: float,
    settings,
    *,
    trend_15m_agrees: bool = False,
    impulse_confirmed: bool = False,
    explosive_bypass: bool = False,
    extreme_exhaustion: bool = False,
    regime: str = "",
    adx_value: float = 0.0,
    intel_score: float = 0.0,
    iv_percentile: Optional[float] = None,
    chain_quality: Optional[Mapping] = None,
    option_premium_ok: Optional[bool] = None,
) -> bool:
    """Single source of truth for live 5min impulse exceptions.

    Math: B+/A/A+ rank >= 2, confidence >= configured floor, 15min agrees,
    impulse context confirms, and exhaustion is absent. A separate B+/A/A+
    60% exception exists only for known regimes with strong ADX/intel and
    healthy option premium context.
    """
    rank = grade_rank(grade)
    conf = float(confidence or 0.0)
    min_conf = relaxed_threshold(
        float(getattr(settings, "live_5min_bplus_min_confidence", 0.70) or 0.70),
        settings,
    )
    strict_ok = bool(
        rank >= 2
        and conf >= min_conf
        and trend_15m_agrees
        and (impulse_confirmed or explosive_bypass)
        and not extreme_exhaustion
    )
    if strict_ok:
        return True

    special_min_conf = relaxed_threshold(
        float(
            getattr(
                settings,
                "live_5min_special_min_confidence",
                getattr(settings, "live_5min_special_bplus_min_confidence", 0.60),
            )
            or 0.60
        ),
        settings,
    )
    strong_impulse = bool(
        float(adx_value or 0.0) >= float(getattr(settings, "live_5min_special_adx_threshold", 30.0) or 30.0)
        or abs(normalize_intelligence_score(intel_score)) >= float(getattr(settings, "live_5min_special_intel_threshold", 0.75) or 0.75)
    )
    premium_ok = option_premium_health_ok(
        settings,
        iv_percentile=iv_percentile,
        chain_quality=chain_quality,
        explicit_ok=option_premium_ok,
    )
    special_ok = bool(
        rank >= 2
        and conf >= special_min_conf
        and trend_15m_agrees
        and strong_impulse
        and not extreme_exhaustion
        and str(regime or "").upper() != "UNKNOWN"
        and premium_ok
    )
    if special_ok:
        return True

    # ── Strong trend CONTINUATION (previously-missing path) ──
    # The strict/special paths above only admit impulse *reversals* (or specials
    # gated on option-premium health). A high-grade, high-ADX, 15m-aligned signal
    # in a CONFIRMED trending regime is a valid 5min entry on its own — it is the
    # core "ride the move" trade. Without this, A+/95% signals at ADX 46-49 with
    # 15m agreement were rejected and big directional moves were missed entirely
    # (UT Bot only fires on flips, so the rejected flip is the only entry). This
    # path stays whipsaw-safe: it requires a trending regime (not RANGING/VOLATILE)
    # AND strong ADX, so it cannot fire in chop.
    if setting_enabled(getattr(settings, "live_5min_trend_continuation_enabled", True), True):
        cont_adx = float(getattr(settings, "live_5min_trend_continuation_adx", 28.0) or 28.0)
        trend_continuation_ok = bool(
            rank >= 2
            and conf >= special_min_conf
            and trend_15m_agrees
            and not extreme_exhaustion
            and is_trending_regime(regime)
            and float(adx_value or 0.0) >= cont_adx
        )
        # Higher-conviction tier: a strongly-confident, 15m-aligned signal in a
        # trending regime may enter at a LOWER ADX, since conviction substitutes for
        # raw ADX. Catches grind trends that never spike ADX to 28 (the core
        # "ride the move" miss). Still trend-gated, so it cannot fire in chop.
        strong_conf = float(
            getattr(settings, "live_5min_trend_continuation_strong_conf", 0.65) or 0.65
        )
        min_adx = float(
            getattr(settings, "live_5min_trend_continuation_min_adx", 22.0) or 22.0
        )
        strong_conf_continuation_ok = bool(
            rank >= 2
            and conf >= strong_conf
            and trend_15m_agrees
            and not extreme_exhaustion
            and is_trending_regime(regime)
            and float(adx_value or 0.0) >= min_adx
        )
        if trend_continuation_ok or strong_conf_continuation_ok:
            return True
    return False


def dynamic_rr(
    grade: str,
    confidence: float,
    intel_score: float = 0.0,
    max_rr: Optional[float] = None,
    timeframe: str = "",
) -> float:
    """Return quality-scaled initial RR; runner mode handles exceptional moves."""
    conf = min(max(float(confidence or 0.0), 0.0), 1.0)
    min_rr = 1.6
    settings = None
    if max_rr is None:
        try:
            settings = get_settings()
            min_rr = float(getattr(settings, "dynamic_rr_min", 1.6) or 1.6)
            max_rr = float(getattr(settings, "dynamic_rr_max", 12.0) or 12.0)
        except Exception:
            max_rr = 12.0
    max_rr = max(1.60, float(max_rr or 12.0))
    min_rr = min(max(1.60, float(min_rr or 1.6)), max_rr)
    base_grade = str(grade or "C").split()[0]
    grade_bonus = {
        "A+": 1.15,
        "A": 0.75,
        "B+": 0.35,
        "B": 0.0,
        "C": -0.20,
        "Recovered": 0.75,
    }.get(base_grade, 0.0)
    confidence_runway = max(0.0, conf - 0.55) * 3.2
    intel_adjustment = min(max(normalize_intelligence_score(intel_score) / 1.20, -0.40), 1.25)
    rr = max(min_rr, min(max_rr, min_rr + confidence_runway + grade_bonus + intel_adjustment))
    # Short intraday timeframes can't realistically travel the top of the RR ladder
    # within one bar's horizon before the profit-lock/square-off fires, so cap them.
    tf = str(timeframe or "").lower()
    if tf in {"1min", "5min"}:
        if settings is None:
            try:
                settings = get_settings()
            except Exception:
                settings = None
        tf_cap = 2.5
        if settings is not None:
            try:
                tf_cap = float(getattr(settings, "dynamic_rr_5min_max", 2.5) or 2.5)
            except Exception:
                tf_cap = 2.5
        rr = min(rr, max(min_rr, tf_cap))
    return round(rr, 2)


def option_index_stop_distance(
    natural_stop_distance: float,
    est_premium: float,
    options_sl_pct: float,
    instrument_multiplier: float,
    settings,
) -> float:
    """Convert option premium stop policy into index points."""
    natural = float(natural_stop_distance or 0.0)
    if str(getattr(settings, "sl_mode", "NATURAL") or "NATURAL").upper() == "NATURAL":
        return natural
    premium_risk = float(est_premium or 0.0) * (float(options_sl_pct or 0.0) / 100.0)
    multiplier = max(0.1, abs(float(instrument_multiplier or 0.0)))
    percentage_distance = premium_risk / multiplier
    return min(natural, percentage_distance) if natural > 0 else percentage_distance


#: Smallest separation that still means anything. The margin itself is a setting
#: (power_move_spike_range_margin) because a parameter should be adjustable — but
#: it is clamped ABOVE this, because the one thing the knob may not express is the
#: two tiers meeting. That is the relationship, not a parameter.
_POWER_MOVE_SPIKE_MARGIN_FLOOR = 1.01


def power_move_spike_threshold(settings) -> float:
    """Explosive-spike range multiple, guaranteed ABOVE the ordinary range gate.

    power_move_confirmed uses two multiples: `power_move_range_atr_mult` gates
    entry on range expansion, and `power_move_spike_range_atr_mult` decides
    whether the move is explosive enough to WAIVE 15-minute alignment. That
    waiver is justified because a genuine spike LEADS the 15m bar — reasoning
    that only holds while "explosive" is a higher bar than "expanded".

    Code ships 2.0 and 2.5. On 2026-08-13 the operator's .env set the spike
    multiple to 2.0, equal to the range gate, so every candidate clearing
    range_ok also counted as explosive, align_ok was always satisfied through
    the bypass, and the 15-minute requirement became unreachable for power
    moves. Two independent numbers with nothing enforcing the ordering.

    So the tier is derived rather than assumed: whatever the range gate is set
    to, the spike tier keeps the separation the defaults express. Raising the
    gate carries the spike tier with it. A deliberately HIGHER spike setting is
    honoured untouched; only the collapse is corrected.
    """
    try:
        gate = float(getattr(settings, "power_move_range_atr_mult", 2.0) or 2.0)
    except (TypeError, ValueError):
        gate = 2.0
    try:
        margin = float(getattr(settings, "power_move_spike_range_margin", 1.25) or 1.25)
    except (TypeError, ValueError):
        margin = 1.25
    floor = max(0.0, gate) * max(_POWER_MOVE_SPIKE_MARGIN_FLOOR, margin)
    try:
        configured = float(getattr(settings, "power_move_spike_range_atr_mult", 2.5) or 2.5)
    except (TypeError, ValueError):
        configured = 0.0
    return max(floor, configured) if configured > 0 else floor


def power_move_confirmed(
    *,
    volume_ratio: float,
    range_atr_ratio: float,
    close_location: float,
    adx_value: float,
    intel_score: float,
    trend_15m_agrees: bool,
    settings,
) -> bool:
    """Data-confirmed late-session 'power move' detector (all 4 factors required).

    A genuine big/explosive move shows a confluence of:
      1. Volume surge       — candle volume >= power_move_volume_mult x recent avg
      2. Range expansion    — candle true range >= power_move_range_atr_mult x ATR
      3. Directional close  — close in the top/bottom power_move_close_location of the range
      4. Momentum + align   — (ADX >= momentum threshold OR strong intel) AND 15m agrees

    `close_location` is the DIRECTION-AWARE fraction: (close-low)/range for BUY,
    (high-close)/range for SELL — i.e. ~1.0 = closed hard in the signal's direction.
    """
    if not setting_enabled(getattr(settings, "power_move_override_enabled", True), True):
        return False
    vol_ratio = float(volume_ratio or 0.0)
    vol_ok = vol_ratio >= float(getattr(settings, "power_move_volume_mult", 2.5) or 2.5)
    range_ok = float(range_atr_ratio or 0.0) >= float(getattr(settings, "power_move_range_atr_mult", 2.0) or 2.0)
    dir_ok = float(close_location or 0.0) >= float(getattr(settings, "power_move_close_location", 0.70) or 0.70)
    # Spike-aware momentum: ADX lags on sudden reversals, so a big volume/range
    # explosion counts as momentum even before ADX builds.
    adx_floor = float(getattr(settings, "power_move_adx_threshold", 20.0) or 20.0)
    strong_vol = float(getattr(settings, "power_move_momentum_volume_mult", 3.5) or 3.5)
    mom_ok = (
        float(adx_value or 0.0) >= adx_floor
        or abs(normalize_intelligence_score(intel_score)) >= 0.70
        or vol_ratio >= strong_vol
    )
    # 15m alignment normally mandatory — BUT a sudden spike by definition LEADS
    # the 15m (the 15m bar hasn't flipped yet), so requiring 15m agreement makes
    # the system structurally blind to post-2:59pm spikes. When the move is
    # genuinely explosive (big range expansion + a hard directional close +
    # momentum), that confluence IS the confirmation, so 15m agreement is waived.
    spike_bypass = setting_enabled(getattr(settings, "power_move_spike_bypass_15m", True), True)
    strong_spike = float(range_atr_ratio or 0.0) >= power_move_spike_threshold(settings)
    align_ok = bool(trend_15m_agrees) or (spike_bypass and strong_spike and dir_ok and mom_ok)
    return bool(vol_ok and range_ok and dir_ok and mom_ok and align_ok)


def volume_spike_depth_ofr_aligned(
    *,
    signal_type: str,
    ofr: float,
    depth_usable: bool,
    settings,
) -> bool:
    """True when L1 OFR agrees with spike direction (or depth not usable / gate OFF).

    When depth is usable and confirm is enabled:
      BUY  → OFR >= volume_spike_depth_ofr_bullish (default 1.08)
      SELL → OFR <= volume_spike_depth_ofr_bearish (default 0.92)
    Unavailable depth never blocks a candle-confirmed spike.
    """
    if not setting_enabled(
        getattr(settings, "volume_spike_depth_confirm_enabled", True), True
    ):
        return True
    if not bool(depth_usable):
        return True
    try:
        ofr_v = float(ofr or 0.0)
    except Exception:
        return True
    if ofr_v <= 0:
        return True
    bull = float(getattr(settings, "volume_spike_depth_ofr_bullish", 1.08) or 1.08)
    bear = float(getattr(settings, "volume_spike_depth_ofr_bearish", 0.92) or 0.92)
    side = str(signal_type or "").upper()
    if side in {"BUY", "LONG"}:
        return ofr_v + 1e-12 >= bull
    if side in {"SELL", "SHORT"}:
        return ofr_v - 1e-12 <= bear
    return True


def volume_spike_min_ratio(settings, instrument: str = "") -> float:
    """The volume-ratio floor for THIS index, not one number for all four.

    A flat 2.5x is not one filter, it is four different filters. Measured over 24
    sessions (2026-07-14..08-14, 1,650 five-minute bars per index) against the
    profiler's own time-of-day baseline, 2.5x sits at a completely different place in
    each index's distribution:

        NIFTY       p95 1.63   ->  flat 2.0x fires on   1.3% of bars
        BANKNIFTY   p95 2.14   ->  flat 2.0x fires on   6.2% of bars
        MIDCPNIFTY  p95 2.13   ->  flat 2.0x fires on   6.3% of bars
        SENSEX      p95 3.71   ->  flat 2.0x fires on  12.4% of bars

    Nearly ten times more often on SENSEX than on NIFTY, from one setting. SENSEX is the
    thinnest of the four by absolute volume (median 137k a bar against NIFTY's 3.5m),
    and thin books produce lumpy prints, so its ratios scatter far wider.

    The defaults below are each index's own 95th percentile, which puts the spike lane
    on the top 5.0% of bars on every index instead of 1.3% on one and 12.4% on another.

    Selectivity is all this equalises. It does not manufacture signal: over the same 24
    sessions the forward-move lift at >=2.5x was 1.64x on BANKNIFTY but 1.07x on SENSEX
    and 1.05x on MIDCPNIFTY, so on those two a volume spike says little about what
    happens next regardless of where the bar is set. Empty entries mean nothing is
    per-index and the flat setting applies, which is the pre-2026-08-15 behaviour.
    """
    base = float(getattr(settings, "volume_spike_entry_min_volume_ratio", 2.5) or 2.5)
    name = str(instrument or "").strip().upper()
    if not name:
        return base
    try:
        per_index = getattr(settings, "volume_spike_min_ratio_by_index", None) or {}
        value = float(per_index.get(name, 0.0) or 0.0)
    except (TypeError, ValueError, AttributeError):
        return base
    return value if value > 0 else base


def volume_spike_entry_confirmed(
    *,
    timeframe: str,
    volume_ratio: float,
    range_atr_ratio: float,
    close_location: float,
    adx_value: float = 0.0,
    intel_score: float = 0.0,
    power_move: bool = False,
    settings,
    signal_type: str = "",
    depth_usable: bool = False,
    ofr: float = 0.0,
    volume_basis: str = "TRADED",
    surge_percentile: float = -1.0,
    instrument: str = "",
) -> bool:
    """Confirm a fast 1min/5min directional volume-spike entry.

    This is intentionally narrower than "volume >= threshold" alone: an actual
    entry also needs expanded price range and a close near the directional edge,
    preventing high-volume dojis/absorption candles from becoming blind trades.
    For 1min, when L1 depth is usable, OFR must align with signal direction.
    """
    if not setting_enabled(getattr(settings, "volume_spike_entry_enabled", True), True):
        return False
    tf = str(timeframe or "").lower()
    if tf not in {"1min", "5min"}:
        return False
    volume_floor = volume_spike_min_ratio(settings, instrument)
    if tf == "1min":
        strong_1min_context = bool(
            power_move
            or float(adx_value or 0.0) >= float(
                getattr(settings, "volume_spike_entry_strong_1min_adx_threshold", 25.0)
                or 25.0
            )
            or abs(normalize_intelligence_score(intel_score)) >= float(
                getattr(settings, "volume_spike_entry_strong_1min_intel_threshold", 0.70)
                or 0.70
            )
        )
        # High-burst-only: 1min continuous scalps must show strong momentum context
        # (not bare volume). Strong path still needs a high volume floor.
        high_burst_only = setting_enabled(
            getattr(settings, "one_min_scalping_high_burst_only", True), True
        )
        if high_burst_only and not strong_1min_context:
            return False
        if strong_1min_context:
            strong_floor = float(
                getattr(settings, "volume_spike_entry_strong_1min_min_volume_ratio", 2.5)
                or 2.5
            )
            # Never drop below strong floor when burst-only; otherwise may ease slightly.
            volume_floor = (
                max(volume_floor, strong_floor)
                if high_burst_only
                else min(volume_floor, strong_floor)
            )
        # Depth/OFR confirm only for continuous 1min spikes (not 5min path).
        if not volume_spike_depth_ofr_aligned(
            signal_type=signal_type,
            ofr=ofr,
            depth_usable=depth_usable,
            settings=settings,
        ):
            return False
    # The surge test is basis-aware. A fixed ratio floor only means what it was cut for:
    # traded volume is heavily tailed (a z-score of 5 still admits 4.7% of 1min bars)
    # while tick activity is near-Poisson, so applying the 2.2x traded floor to ticks
    # would admit almost nothing and leave 1min as dead as the zero-numerator bug left
    # it. Percentile rank is uniform for any continuous distribution, so one threshold
    # carries the same meaning on either measurement.
    #
    # Calibrated on the real 1min candles: ratio>=2.2 admits 4.73% of measurable
    # traded-volume bars and rank>=97 admits 4.98% — the same strictness, in a form that
    # survives the change of basis. TRADED readings keep the ratio test untouched, so
    # 5min and 15min behaviour is unchanged.
    if str(volume_basis or "TRADED").upper() == "TICK_ACTIVITY":
        floor_pct = float(
            getattr(settings, "surge_tick_percentile_floor", 97.0) or 97.0
        )
        surge_ok = float(surge_percentile or -1.0) >= floor_pct
    else:
        surge_ok = float(volume_ratio or 0.0) >= volume_floor
    return bool(
        surge_ok
        and float(range_atr_ratio or 0.0)
        >= float(getattr(settings, "volume_spike_entry_min_range_atr_ratio", 1.5) or 1.5)
        and float(close_location or 0.0)
        >= float(getattr(settings, "volume_spike_entry_min_close_location", 0.65) or 0.65)
    )


def b_grade_min_confidence(settings) -> float:
    """Top-of-band floor a B-grade signal must reach to be eligible to trade."""
    return float(getattr(settings, "live_b_grade_min_confidence", 0.50) or 0.50)


def b_grade_entry_allowed(
    confidence: float,
    regime: str,
    *,
    trend_15m_agrees: bool,
    adx_value: float,
    settings,
) -> bool:
    """Strict, dedicated B-grade allowance.

    B (confidence 0.40-0.55) may trade ONLY when:
      * confidence >= b_grade floor (0.50, top of the B band),
      * the 15-minute trend agrees with the signal direction,
      * ADX momentum >= momentum_override_threshold (default 25),
      * the regime is trending/breakout (NOT choppy/ranging/volatile/unknown).
    It governs the B band only; B+/A/A+ use the standard confidence floors.
    """
    conf = float(confidence or 0.0)
    if conf < relaxed_threshold(b_grade_min_confidence(settings), settings):
        return False
    if conf >= grade_min_confidence("B+"):
        return False  # not a B-band signal; standard floors apply
    if not is_trending_regime(regime):
        return False
    if not trend_15m_agrees:
        return False
    momentum_threshold = float(getattr(settings, "momentum_override_threshold", 25.0) or 25.0)
    return float(adx_value or 0.0) >= momentum_threshold


def option_grade_allowed(
    grade: str,
    confidence: float,
    regime: str,
    *,
    mode: str,
    adx_value: float = 0.0,
    intel_score: float = 0.0,
    signal_score: float = 0.0,
    five_min_true_impulse: bool = False,
    timeframe: str = "",
    fifteen_min_momentum_override: Optional[bool] = None,
    volume_ratio: float = 0.0,
    range_atr_ratio: float = 0.0,
    close_location: float = 0.0,
    trend_15m_agrees: bool = False,
) -> bool:
    # Grade == bucket(confidence), so every `rank >= N` check is just a
    # confidence threshold (grade_min_confidence). Expressed confidence-only.
    conf = float(confidence or 0.0)
    settings = get_settings()
    more_results = (
        setting_enabled(getattr(settings, "ut_backtest_more_results", True), True)
        and str(mode or "").upper() == "HISTORICAL"
    )
    fifteen_min_momentum_ok = bool(fifteen_min_momentum_override)
    if fifteen_min_momentum_override is None and str(timeframe or "") == "15min":
        fifteen_min_momentum_ok = fifteen_min_momentum_override_allowed(
            conf,
            settings,
            volume_ratio=volume_ratio,
            range_atr_ratio=range_atr_ratio,
            close_location=close_location,
            adx_value=adx_value,
            intel_score=intel_score,
            trend_15m_agrees=trend_15m_agrees,
            regime=regime,
        )
    if str(timeframe or "") == "15min" and fifteen_min_momentum_ok:
        return True
    # B-band options are allowed only in trending/breakout regimes with momentum.
    # (15m alignment is enforced by the entry gate, which has that signal.)
    if conf < grade_min_confidence("B+"):
        momentum_threshold = float(getattr(settings, "momentum_override_threshold", 25.0) or 25.0)
        return (
            is_trending_regime(regime)
            and conf >= relaxed_threshold(b_grade_min_confidence(settings), settings)
            and float(adx_value or 0.0) >= momentum_threshold
        )
    if str(timeframe or "") == "15min" and conf >= fifteen_min_confidence_floor(
        settings,
        regime,
        adx_value=adx_value,
        intel_score=intel_score,
    ):
        return True
    if is_choppy_regime(regime):
        if str(mode or "").upper() == "REAL" and five_min_true_impulse:
            return True
        if more_results:
            return conf >= min(grade_min_confidence("B+"), relaxed_threshold(0.62, settings))
        return conf >= min(grade_min_confidence("A"), relaxed_threshold(0.90, settings))

    # Trending/normal: B+ confidence is sufficient. (The old momentum/intel/
    # signal_score disjuncts were dead — they only mattered when conf >= 0.55,
    # which already implies B+, so they never changed the result.)
    return conf >= grade_min_confidence("B+")


def passes_quality_gate(
    tf: str,
    grade: str,
    confidence: float,
    regime: str,
    inst_type: str,
    settings,
    *,
    mode: str,
    adx_value: float = 0.0,
    trend_15m_agrees: bool = False,
    intel_score: float = 0.0,
    iv_percentile: Optional[float] = None,
    chain_quality: Optional[Mapping] = None,
    option_premium_ok: Optional[bool] = None,
    fifteen_min_momentum_override: Optional[bool] = None,
    volume_ratio: float = 0.0,
    range_atr_ratio: float = 0.0,
    close_location: float = 0.0,
) -> bool:
    # Grade == bucket(confidence): all gates below are confidence-only.
    momentum_threshold = float(getattr(settings, "momentum_override_threshold", 25.0) or 25.0)
    momentum_override = adx_value >= momentum_threshold
    explosive_move = (
        momentum_override
        and trend_15m_agrees
        and abs(normalize_intelligence_score(intel_score)) >= 0.70
    )
    regime_adaptation = setting_enabled(getattr(settings, "ut_regime_adaptation", True), True)
    choppy = regime_adaptation and is_choppy_regime(regime) and not explosive_move
    if explosive_move and is_choppy_regime(regime):
        log_override = logger.debug if str(mode or "").upper() == "HISTORICAL" else logger.info
        log_override(
            f"[OVERRIDE] Choppy regime bypassed due to strong ADX "
            f"({adx_value:.1f} >= {momentum_threshold})"
        )
    more_results = (
        setting_enabled(getattr(settings, "ut_backtest_more_results", True), True)
        and str(mode or "").upper() == "HISTORICAL"
    )
    # Dedicated B-grade allowance: trending + 15m alignment + ADX momentum only.
    b_ok = b_grade_entry_allowed(
        confidence,
        regime,
        trend_15m_agrees=trend_15m_agrees,
        adx_value=adx_value,
        settings=settings,
    )
    fifteen_min_ok = False
    fifteen_min_momentum_ok = False
    if tf == "15min":
        fifteen_min_momentum_ok = bool(fifteen_min_momentum_override)
        if fifteen_min_momentum_override is None:
            fifteen_min_momentum_ok = fifteen_min_momentum_override_allowed(
                confidence,
                settings,
                volume_ratio=volume_ratio,
                range_atr_ratio=range_atr_ratio,
                close_location=close_location,
                adx_value=adx_value,
                intel_score=intel_score,
                trend_15m_agrees=trend_15m_agrees,
                regime=regime,
            )
        fifteen_min_min_conf = fifteen_min_confidence_floor(
            settings,
            regime,
            adx_value=adx_value,
            intel_score=intel_score,
        )
        fifteen_min_ok = confidence >= fifteen_min_min_conf or fifteen_min_momentum_ok
        if str(mode or "").upper() == "REAL" and not fifteen_min_ok and not b_ok:
            return False

    policy = str(getattr(settings, "ut_timeframe_entry_policy", "PRIMARY_15") or "PRIMARY_15").upper()
    if policy == "PRIMARY_15" and tf == "5min":
        return False

    five_min_impulse = False
    if tf == "5min":
        minimum_confidence = max(
            relaxed_threshold(
                float(getattr(settings, "ut_5min_option_min_confidence", 0.60) or 0.60),
                settings,
            ),
            relaxed_threshold(
                float(getattr(settings, "live_5min_bplus_min_confidence", 0.70) or 0.70),
                settings,
            ),
        )
        if more_results:
            minimum_confidence = min(minimum_confidence, relaxed_threshold(0.72, settings))
        five_min_impulse = five_min_true_impulse_allowed(
            grade,
            confidence,
            settings,
            trend_15m_agrees=trend_15m_agrees,
            impulse_confirmed=explosive_move,
            explosive_bypass=explosive_move,
            extreme_exhaustion=False,
            regime=regime,
            adx_value=adx_value,
            intel_score=intel_score,
            iv_percentile=iv_percentile,
            chain_quality=chain_quality,
            option_premium_ok=option_premium_ok,
        )
        grade_confidence_ok = confidence >= max(grade_min_confidence("B+"), minimum_confidence)
        if not (b_ok or five_min_impulse or (more_results and (grade_confidence_ok or trend_15m_agrees or explosive_move))):
            return False
        if choppy and not five_min_impulse:
            return False

    if (
        more_results
        and choppy
        and inst_type == "OPT"
        and not explosive_move
        and not five_min_impulse
        and not fifteen_min_ok
        and confidence < min(
            max(grade_min_confidence("B+"), relaxed_threshold(0.58, settings)),
            relaxed_threshold(0.72, settings),
        )
    ):
        return False
    if (
        not more_results
        and choppy
        and inst_type == "OPT"
        and not explosive_move
        and not five_min_impulse
        and not fifteen_min_ok
        and confidence < min(
            max(grade_min_confidence("A"), relaxed_threshold(0.72, settings)),
            relaxed_threshold(0.90, settings),
        )
    ):
        return False
    if more_results and choppy and confidence < min(grade_min_confidence("B"), relaxed_threshold(0.62, settings)):
        return False
    if (
        not more_results
        and choppy
        and not five_min_impulse
        and not fifteen_min_ok
        and confidence < min(
            grade_min_confidence("A"),
            relaxed_threshold(choppy_confidence_gate(settings), settings),
        )
    ):
        return False
    return True
