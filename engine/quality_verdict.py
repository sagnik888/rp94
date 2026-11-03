"""Versioned, evidence-first admission policy for every signal lane.

This module owns the *quality* question only. Quote freshness, market-hours,
portfolio capacity, broker authority, geometry and market-location checks remain in
their own gates. Keeping them separate makes an audit answerable: a verdict says
whether the setup was good enough, not merely whether some unrelated safeguard fired.

The policy deliberately does not use historical tracker outcomes to lower the live
floor. The legacy tracker has sparse post-decision paths and mixed accounting bases;
the 55--70% cohort is therefore admitted only when it has contemporaneous setup
evidence, while its shadow outcome is recorded for a future causal evaluation.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict

from engine.signal_quality_policy import confidence_to_grade, grade_min_confidence, grade_rank


POLICY_VERSION = "quality_verdict.v9-independent-risky-evidence"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _json_safe(value: Any) -> Any:
    """Return a small JSON-safe representation suitable for sessions and rtracker."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def policy_configuration(settings) -> Dict[str, Any]:
    """The deliberately small policy surface, stable enough for attribution."""
    return {
        "policy_version": POLICY_VERSION,
        "bplus_min_confidence": _number(
            getattr(settings, "quality_policy_bplus_min_confidence", 0.55), 0.55
        ),
        # This is the sole standalone 5-minute threshold. The older UT setting is
        # deliberately not consulted here; it remains available to unrelated legacy code.
        "standalone_5min_confidence": _number(
            getattr(settings, "live_5min_quality_min_confidence", 0.70), 0.70
        ),
        "bplus_min_adx": _number(
            getattr(settings, "quality_policy_bplus_min_adx", 22.0), 22.0
        ),
        "risky_bplus_min_adx": _number(
            getattr(settings, "quality_policy_risky_bplus_min_adx", 25.0), 25.0
        ),
        # Whether two agreeing engines count as the risky-regime B+ "independent" leg.
        # Off since 2026-10-07: a quad-union raw entry exists *because* two engines
        # agreed, so source_count >= 2 is true by construction and the leg reduced the
        # risky fence to ADX + 15m trend. Live ledger 2026-09-15..10-07: every risky B+
        # admission resting only on trend + multi-source -- 6 entries, 1 winner,
        # -Rs.2,751 -- versus +Rs.99 for the 3 that carried impulse or volume.
        "risky_bplus_count_multi_source": bool(
            getattr(settings, "quality_policy_risky_bplus_count_multi_source", False)
        ),
        "min_volume_ratio": _number(
            getattr(settings, "quality_policy_min_volume_ratio", 1.20), 1.20
        ),
        "unknown_confidence": grade_min_confidence("A"),
        # The route that lets market-shift evidence waive the regime/ADX/confirmation fence.
        # Default OFF since 2026-09-23 — every source agreed it loses:
        #   real money     12 trades, 3 winners, -Rs.2,447
        #   shadow ledger  signals accepted through it: +101 / -277 / -574 per signal at
        #                  15 / 30 / 60 min, negative in both halves at 30-60 min, against
        #                  +1,299 / +906 / +440 for the normal high_confidence_confirmed route
        # With it off, the same candidate simply meets the normal fence below.
        # full_core_market_shift (5min, all three core indices, ADX >= 18) is a separate,
        # stricter route and is not governed by this switch.
        "market_shift_route_enabled": bool(
            getattr(settings, "quality_policy_market_shift_route_enabled", False)
        ),
        "market_shift_min_confidence": _number(
            getattr(settings, "quality_policy_market_shift_min_confidence", 0.60), 0.60
        ),
        # The market-shift route waives the regime/ADX/confirmation fence, so it must at
        # least be looking at measured participation. Live 2026-09-09..09-18: the route
        # took 12 broker-filled trades, 3 winners, -Rs.2,447, mostly B+ at 62-68% in
        # RANGING with ADX 12-22 — exactly the setups that fence refuses. The 09-18 09:17
        # SENSEX loser carried volume_available=false and intelligence 0.0. The parallel
        # RP50 build gates the same vote with min volume ratio 1.05; this is that floor.
        "market_shift_min_volume_ratio": _number(
            getattr(settings, "quality_policy_market_shift_min_volume_ratio", 1.05), 1.05
        ),
        "full_core_market_shift_min_confidence": _number(
            getattr(settings, "quality_policy_full_core_market_shift_min_confidence", 0.35), 0.35
        ),
        "full_core_market_shift_min_adx": _number(
            getattr(settings, "quality_policy_full_core_market_shift_min_adx", 18.0), 18.0
        ),
        "stale_regime_reversal_enabled": bool(
            getattr(settings, "stale_regime_reversal_enabled", True)
        ),
        "confidence_calibration_enabled": bool(
            getattr(settings, "ut_confidence_calibration_enabled", False)
        ),
        "confidence_calibration_live_apply_enabled": bool(
            getattr(settings, "ut_confidence_calibration_live_apply_enabled", False)
        ),
    }


def policy_configuration_hash(settings) -> str:
    payload = json.dumps(policy_configuration(settings), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class QualityVerdict:
    """The audit-persistent decision and the exact route that made it."""

    allowed: bool
    route: str
    reason_code: str
    reason: str
    checks: Dict[str, Any] = field(default_factory=dict)
    policy_version: str = POLICY_VERSION
    configuration_hash: str = ""

    def __bool__(self) -> bool:
        return self.allowed

    def to_dict(self) -> Dict[str, Any]:
        return _json_safe(
            {
                "allowed": bool(self.allowed),
                "route": str(self.route),
                "reason_code": str(self.reason_code),
                "reason": str(self.reason),
                "checks": dict(self.checks or {}),
                "policy_version": str(self.policy_version),
                "configuration_hash": str(self.configuration_hash),
            }
        )


@dataclass(frozen=True)
class QualityInputs:
    """Only the evidence that quality admission is allowed to read."""

    timeframe: str
    inst_type: str
    confidence: float
    regime: str
    adx_value: float = 0.0
    # ``adx_value`` reaches this policy as 0.0 from three separate upstream NaN guards
    # (warm-up bars, an empty series, a non-finite read), so an absent ADX and a measured
    # zero are indistinguishable once inside the verdict. Callers that can tell the two
    # apart set this flag. It is recorded for attribution only and never participates in
    # admission: the comparison below is still ``adx >= threshold`` in every route.
    adx_available: bool = True
    trend_15m_agrees: bool = False
    impulse_confirmed: bool = False
    risky_regime: bool = False
    is_choppy: bool = False
    grade: str = ""
    source_count: int = 1
    volume_confirmed: bool = False
    volume_ratio: float = 0.0
    # Direct/replay callers that predate provenance are already supplying a measured
    # ratio. Production scanner calls always set these explicitly.
    volume_available: bool = True
    volume_basis: str = "TRADED"
    power_move: bool = False
    market_shift_confirmed: bool = False
    market_shift_core_votes: int = 0
    market_shift_total_votes: int = 0
    # Set only by Scanner._stale_regime_reversal_context: a 5m/15m signal against a trend
    # label whose session extreme is stale. Never inferred from the other fields.
    stale_regime_reversal: bool = False
    lane: str = "LIVE"
    direction: str = ""
    confidence_calibrated: bool = False


def _verdict(
    allowed: bool,
    route: str,
    code: str,
    reason: str,
    settings,
    **checks: Any,
) -> QualityVerdict:
    checks.setdefault("configuration", policy_configuration(settings))
    return QualityVerdict(
        allowed=allowed,
        route=route,
        reason_code=code,
        reason=reason,
        checks=_json_safe(checks),
        configuration_hash=policy_configuration_hash(settings),
    )


def _is_risky(regime: str, inputs: QualityInputs) -> bool:
    return bool(inputs.risky_regime or inputs.is_choppy or regime in {
        "CHOPPY", "SIDEWAYS", "MEAN_REVERTING", "RANGING", "VOLATILE",
    })


def evaluate_quality(inputs: QualityInputs, settings) -> QualityVerdict:
    """Policy entry point: v9, plus the v10 multi-source decision (shadow by default).

    2026-10-08 audit AC-02: 'multi_source' is mostly one consensus flip expanded into
    ema+supertrend 'sources' (87% of QP candidates are single-source) and the trades it
    confirmed did worse. v10 stops counting it as a confirmation anywhere. Mode
    (quality_policy_multi_source_mode): 'shadow' (default) enforces v9 and records the
    v10 result in checks; 'enforce' enforces v10; 'legacy' is v9 only.
    """
    legacy = _evaluate_quality_v9(inputs, settings)
    mode = str(getattr(settings, "quality_policy_multi_source_mode", "shadow") or "shadow").strip().lower()
    if mode not in {"shadow", "enforce"}:
        return legacy
    try:
        from dataclasses import replace as _replace

        v10 = _evaluate_quality_v9(_replace(inputs, source_count=1), settings)
    except Exception:
        return legacy
    legacy_checks = dict(legacy.checks or {})
    shadow = {
        "v10_shadow_allowed": bool(v10.allowed),
        "v10_shadow_reason_code": str(v10.reason_code),
        "v10_confirmation_count": (v10.checks or {}).get("confirmation_count"),
        "multi_source_mode": mode,
    }
    if mode == "shadow":
        return _replace(legacy, checks={**legacy_checks, **shadow})
    return _replace(v10, checks={**dict(v10.checks or {}), **shadow,
                                 "v9_allowed": bool(legacy.allowed), "v9_reason_code": str(legacy.reason_code)},
                    policy_version="quality_verdict.v10-no-multi-source-confirmation")


def _evaluate_quality_v9(inputs: QualityInputs, settings) -> QualityVerdict:
    """Return one monotonic, lane-independent evidence-based quality decision.

    Grade is validated as the display bucket of confidence, then confidence and setup
    evidence drive the decision. Increasing confidence can never revoke an already-valid
    setup. Historical, recovery and EOD wrappers call this same function.
    """
    cfg = policy_configuration(settings)
    confidence = _number(inputs.confidence)
    regime = str(inputs.regime or "UNKNOWN").upper()
    timeframe = str(inputs.timeframe or "").lower()
    inst_type = str(inputs.inst_type or "").upper()
    adx = _number(inputs.adx_value)
    adx_measured = bool(inputs.adx_available) and adx > 0.0

    def _adx_leg() -> str:
        """Name the ADX leg by *why* it bound, so 0.0-as-missing stops reading as weak trend."""
        return "adx_below_threshold" if adx_measured else "adx_unavailable"

    source_count = max(0, int(_number(inputs.source_count, 1)))
    volume_basis = str(inputs.volume_basis or "").upper()
    measured_volume = bool(inputs.volume_available) and volume_basis not in {
        "TICK_ACTIVITY", "INDEX_TICK_ACTIVITY", "TICK_ROLLING_AVG", "TICK_TIME_OF_DAY",
        "PROXY", "UNAVAILABLE", "MIXED_UNITS", "MIXED", "UNKNOWN", ""
    }
    volume_confirmed = measured_volume and (
        bool(inputs.volume_confirmed) or _number(inputs.volume_ratio) >= cfg["min_volume_ratio"]
    )
    trend = bool(inputs.trend_15m_agrees)
    impulse = bool(inputs.impulse_confirmed)
    power = bool(inputs.power_move)
    market_shift = bool(inputs.market_shift_confirmed)
    market_shift_core_votes = max(0, int(_number(inputs.market_shift_core_votes)))
    market_shift_total_votes = max(0, int(_number(inputs.market_shift_total_votes)))
    risky = _is_risky(regime, inputs)
    # QualityInputs permits an omitted grade because confidence is canonical.
    # Avoid indexing an empty token list for direct/recovery callers that rely on
    # that default; the confidence-derived grade remains the admission input.
    grade_tokens = str(inputs.grade or "").split()
    supplied_grade = grade_tokens[0].upper() if grade_tokens else ""
    canonical_grade = confidence_to_grade(confidence)
    grade = supplied_grade or canonical_grade
    recognized_grade = supplied_grade in {"C", "B", "B+", "A", "A+"}
    momentum = bool(impulse or power)
    confirmation_legs = {
        "trend_15m": trend,
        "impulse_or_power": momentum,
        "volume": volume_confirmed,
        "multi_source": source_count >= 2,
    }
    confirmation_count = sum(bool(value) for value in confirmation_legs.values())
    confidence_calibrated = bool(inputs.confidence_calibrated)
    base_checks = {
        "lane": str(inputs.lane or "LIVE").upper(),
        "timeframe": timeframe,
        "inst_type": inst_type,
        "regime": regime,
        "confidence": confidence,
        "confidence_calibrated": confidence_calibrated,
        "confidence_semantics": (
            "calibrated_probability" if confidence_calibrated else "heuristic_score"
        ),
        "grade": grade,
        "canonical_grade": canonical_grade,
        "grade_consistent": not recognized_grade or grade == canonical_grade,
        "adx": adx,
        "adx_measured": adx_measured,
        "adx_basis": "MEASURED" if adx_measured else "UNAVAILABLE",
        "trend_15m_agrees": trend,
        "impulse_confirmed": impulse,
        "volume_confirmed": volume_confirmed,
        "volume_ratio": _number(inputs.volume_ratio),
        "volume_available": bool(inputs.volume_available),
        "volume_basis": volume_basis or "UNKNOWN",
        "source_count": source_count,
        "power_move": power,
        "market_shift_confirmed": market_shift,
        "market_shift_core_votes": market_shift_core_votes,
        "market_shift_total_votes": market_shift_total_votes,
        "stale_regime_reversal": bool(inputs.stale_regime_reversal),
        "confirmation_legs": confirmation_legs,
        "confirmation_count": confirmation_count,
    }

    # Grade is not an independent score: it must match the canonical confidence bucket.
    # Reject malformed/stale candidates instead of letting a C@95% or A@68% row exploit
    # whichever representation happens to be more permissive.
    if recognized_grade and grade != canonical_grade:
        return _verdict(
            False, "grade_integrity", "GRADE_CONFIDENCE_MISMATCH",
            f"grade {grade} is inconsistent with {confidence:.0%} confidence (expected {canonical_grade})",
            settings, expected_grade=canonical_grade,
            binding_legs=["grade_consistency"], **base_checks,
        )

    # A 5m reversal independently confirmed by all three core indices is the sole
    # exception to canonical B+.  The detector already requires synchronized
    # directional candles, range expansion and close location for every vote; this
    # route additionally needs measured ADX and a 35% confidence floor.  It fixes
    # the prior contradiction where strict 3/3 market evidence was recorded but
    # discarded before it could influence admission.  No 1m/15m, partial-core, or
    # generic market-shift row can use this exception.
    full_core_market_shift = bool(
        market_shift
        and timeframe == "5min"
        and market_shift_core_votes >= 3
        and market_shift_total_votes >= 3
        and confidence >= cfg["full_core_market_shift_min_confidence"]
        and adx >= cfg["full_core_market_shift_min_adx"]
    )
    if full_core_market_shift:
        return _verdict(
            True, "full_core_market_shift", "FULL_CORE_MARKET_SHIFT",
            "5min full-core market shift confirmed by synchronized candles, breadth and ADX",
            settings,
            required=cfg["full_core_market_shift_min_confidence"],
            required_adx=cfg["full_core_market_shift_min_adx"],
            required_core_votes=3,
            binding_legs=[],
            **base_checks,
        )

    # A trend label whose own extreme has gone stale grades a genuine reversal as C:
    # the regime, multi-timeframe and intelligence pillars all still describe the old
    # move. Over 22 sessions those signals beat the B+ cohort the floor admits (+0.77
    # vs -0.13 ATR at 30m), so confidence is not the evidence here -- the staleness is.
    stale_regime_reversal = bool(
        inputs.stale_regime_reversal
        and cfg["stale_regime_reversal_enabled"]
        and timeframe in {"5min", "15min"}
    )
    if stale_regime_reversal:
        return _verdict(
            True, "stale_regime_reversal", "STALE_REGIME_REVERSAL",
            "trend label's session extreme is stale; the reversal is graded against a lagging regime",
            settings, binding_legs=[], **base_checks,
        )

    # Every remaining route starts at canonical B+. No raw/context/power/high-confidence
    # flag can admit a B or C, and no retrospective tracker label participates here.
    if confidence < cfg["bplus_min_confidence"] or grade_rank(canonical_grade) < grade_rank("B+"):
        return _verdict(
            False, "minimum_confidence", "BELOW_BPLUS_ELIGIBILITY",
            f"requires B+ eligibility (confidence >= {cfg['bplus_min_confidence']:.0%}); got {grade} at {confidence:.0%}",
            settings, required=cfg["bplus_min_confidence"],
            binding_legs=["confidence_floor"], **base_checks,
        )

    # Slow 15-minute state must not veto a strictly confirmed fast market turn.
    # This flag is supplied only after one of the existing multi-index reversal,
    # LTP-shock, cascade or late-reclaim detectors has cleared its own breadth,
    # candle, timing and volume rules.  It substitutes for stale regime/trend
    # evidence, never for the canonical B+ floor or downstream risk/quote gates.
    if (
        market_shift
        and cfg["market_shift_route_enabled"]
        and confidence >= cfg["market_shift_min_confidence"]
    ):
        # Measured participation is the one leg this route may not waive: without it the
        # "market shift" is a price pattern with nothing behind it. Unmeasurable volume
        # reads as unmeasurable, never as confirmation.
        shift_volume_ratio = _number(inputs.volume_ratio)
        if not measured_volume or shift_volume_ratio < cfg["market_shift_min_volume_ratio"]:
            return _verdict(
                False, "market_shift_volume", "MARKET_SHIFT_VOLUME_UNCONFIRMED",
                (
                    "market-shift override needs measured volume >= "
                    f"{cfg['market_shift_min_volume_ratio']:.2f}x; got "
                    + (f"{shift_volume_ratio:.2f}x" if measured_volume else f"{volume_basis or 'UNAVAILABLE'}")
                ),
                settings, required=cfg["market_shift_min_volume_ratio"],
                binding_legs=["market_shift_volume"], **base_checks,
            )
        return _verdict(
            True, "confirmed_market_shift", "CONFIRMED_MARKET_SHIFT",
            "strict contemporaneous market-shift evidence supersedes stale regime and 15m direction",
            settings, required=cfg["market_shift_min_confidence"],
            binding_legs=[], **base_checks,
        )

    standalone_floor = cfg["standalone_5min_confidence"]

    # UNKNOWN is absence of regime evidence. Even A/A+ needs a strong measured setup;
    # confidence alone is never a substitute for regime, ADX and confirmations.
    if regime == "UNKNOWN":
        min_adx = cfg["risky_bplus_min_adx"]
        allowed = bool(
            confidence >= cfg["unknown_confidence"]
            and adx >= min_adx
            and trend
            and confirmation_count >= 3
        )
        if allowed:
            return _verdict(
                True, "unknown_strong_confirmed", "UNKNOWN_STRONG_SETUP_CONFIRMED",
                "UNKNOWN regime admitted only with A confidence, ADX, 15m trend and two independent confirmations",
                settings, required=cfg["unknown_confidence"], required_adx=min_adx,
                required_confirmations=3, binding_legs=[], **base_checks,
            )
        binding_legs = []
        if confidence < cfg["unknown_confidence"]:
            binding_legs.append("confidence_floor")
        if adx < min_adx:
            binding_legs.append(_adx_leg())
        if not trend:
            binding_legs.append("trend_15m")
        if confirmation_count < 3:
            binding_legs.append("confirmations")
        return _verdict(
            False, "unknown_regime", "UNKNOWN_REGIME_SETUP_INSUFFICIENT",
            f"UNKNOWN regime needs confidence >= {cfg['unknown_confidence']:.0%}, ADX >= {min_adx:.0f}, "
            "15m trend and two independent confirmations",
            settings, required=cfg["unknown_confidence"], required_adx=min_adx,
            required_confirmations=3, binding_legs=binding_legs, **base_checks,
        )

    # The 55--70% cohort is not promoted by retrospective tracker labels. It needs
    # contemporaneous evidence. Noisy/ranging regimes require trend plus another leg;
    # trending/breakout regimes permit either trend or independent volume-confirmed proof.
    if risky:
        min_adx = cfg["risky_bplus_min_adx"]
        # The independent leg must be evidence the candidate was not built from.
        # Multi-source agreement is how a union entry is formed, so it only counts
        # when the operator explicitly re-enables it.
        count_multi_source = bool(cfg["risky_bplus_count_multi_source"])
        independent_leg = bool(
            momentum or volume_confirmed or (count_multi_source and source_count >= 2)
        )
        evidence_text = (
            "trend_15m + one of (impulse/power | volume | multi_source)"
            if count_multi_source
            else "trend_15m + one of (impulse/power | measured volume); multi-source union is not independent"
        )
        evidence_ok = trend and independent_leg
        if adx >= min_adx and evidence_ok:
            return _verdict(
                True, "bplus_risky_confirmed", "BPLUS_RISKY_SETUP_CONFIRMED",
                f"B+ risky-regime setup confirmed by ADX >= {min_adx:.0f}, 15m trend and independent setup evidence",
                settings, required_adx=min_adx,
                evidence=evidence_text, independent_leg=independent_leg,
                binding_legs=[], **base_checks,
            )
        binding_legs = []
        if adx < min_adx:
            binding_legs.append(_adx_leg())
        if not trend:
            binding_legs.append("trend_15m")
        if not independent_leg:
            # Same leg name as before; checks["independent_leg"] says which rule bound.
            binding_legs.append("confirmations")
        return _verdict(
            False, "bplus_risky_confirmed", "BPLUS_RISKY_SETUP_INSUFFICIENT",
            f"B+ in {regime} needs ADX >= {min_adx:.0f}, 15m trend, and impulse/power or measured-volume "
            "confirmation" + ("" if not count_multi_source else " (multi-source counts)"),
            settings, required_adx=min_adx,
            evidence=evidence_text, independent_leg=independent_leg,
            binding_legs=binding_legs, **base_checks,
        )

    min_adx = cfg["bplus_min_adx"]
    required_confirmations = 1 if confidence >= standalone_floor else 2
    evidence_ok = confirmation_count >= required_confirmations
    if adx >= min_adx and evidence_ok:
        route = "high_confidence_confirmed" if confidence >= standalone_floor else "bplus_setup_confirmed"
        return _verdict(
            True, route, "BPLUS_SETUP_CONFIRMED",
            f"setup confirmed by ADX >= {min_adx:.0f} and {required_confirmations} contemporaneous confirmation(s)",
            settings, required_adx=min_adx,
            required_confirmations=required_confirmations,
            binding_legs=[], **base_checks,
        )

    binding_legs = []
    if adx < min_adx:
        binding_legs.append(_adx_leg())
    if confirmation_count < required_confirmations:
        binding_legs.append("confirmations")
    return _verdict(
        False, "bplus_setup", "BPLUS_SETUP_INSUFFICIENT",
        f"B+ is eligible but needs ADX >= {min_adx:.0f} and {required_confirmations} contemporaneous confirmation(s)",
        settings, required_adx=min_adx,
        required_confirmations=required_confirmations,
        binding_legs=binding_legs, **base_checks,
    )


__all__ = [
    "POLICY_VERSION", "QualityInputs", "QualityVerdict", "evaluate_quality",
    "policy_configuration", "policy_configuration_hash",
]
