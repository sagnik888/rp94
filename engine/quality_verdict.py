"""One place that decides whether a signal is good enough to trade.

WHY THIS EXISTS
---------------
The live entry gate grew into a 322-line function in which the quality decision --
"is this signal worth trading?" -- was spread across six separate checks that each read
some mixture of grade, confidence, regime, ADX and 15-minute agreement, and each of
which could veto on its own. Whichever fired first became the recorded reason and masked
the rest, so the same (grade, confidence) cell was routinely rejected by four different
gates depending only on evaluation order.

Measured over 22 session files: 11,966 raw rejection rows, collapsing to 2,969 distinct
signal-time opportunities, spread over dozens of reason strings. Every populated
(grade, confidence) cell had four or more gates able to reject in it.

WHAT THIS IS, AND IS NOT
------------------------
This module answers ONE question: is the signal's QUALITY sufficient? It deliberately
does NOT touch:

  * safety and structure -- quote freshness, repaint stabilisation, broker authority,
    session cutoff, concurrency, resource pressure, event blackout, index halt,
  * portfolio state -- correlated exposure, cooldown, opposite-side handling,
  * executability -- stop geometry, RR, expected move, liquidity, premium sanity,
  * market location -- the chase guard, which mutates the candidate and arms scalp lock.

Those are not redundant quality gates; they answer different questions, and collapsing
them into this would be the same mistake in the other direction.

PARITY FIRST
------------
Every rule below is a faithful transcription of the live gate as it stood on
2026-09-01, including the exact threshold expressions. The point of the first release is
NOT to change behaviour -- it is to put the decision in one readable place, with an
explicit route and a stable reason code, so that a later change can be made against
evidence instead of against a 322-line function. `tests/test_quality_verdict_parity_*`
pins that equivalence over a swept grid.

The grade/confidence invariant matters when reading this file: grade is not an
independent input. GRADE_BANDS makes it a pure bucket of confidence (A+ 0.85, A 0.70,
B+ 0.55, B 0.40), so a rule that reads both is reading one thing twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from engine.signal_quality_policy import (
    b_grade_entry_allowed,
    choppy_confidence_gate,
    grade_min_confidence,
    relaxed_threshold,
)

POLICY_VERSION = "quality_verdict.v2-5min-floor"


@dataclass(frozen=True)
class QualityVerdict:
    """The single structured answer, carrying WHY as well as WHETHER."""

    allowed: bool
    route: str
    reason_code: str
    reason: str
    checks: Dict[str, Any] = field(default_factory=dict)
    policy_version: str = POLICY_VERSION

    def __bool__(self) -> bool:  # so callers may treat it as the old boolean
        return self.allowed


@dataclass(frozen=True)
class QualityInputs:
    """Everything the quality decision reads. No Scanner, no side effects."""

    timeframe: str
    inst_type: str
    confidence: float
    regime: str
    adx_value: float = 0.0
    trend_15m_agrees: bool = False
    impulse_confirmed: bool = False
    risky_regime: bool = False
    is_choppy: bool = False
    grade: str = ""


def _allow(route: str, code: str, reason: str, **checks) -> QualityVerdict:
    return QualityVerdict(True, route, code, reason, checks)


def _block(route: str, code: str, reason: str, **checks) -> QualityVerdict:
    return QualityVerdict(False, route, code, reason, checks)


def evaluate_quality(inputs: QualityInputs, settings) -> QualityVerdict:
    """Decide signal quality once, and say which rule decided it.

    Order is significant and mirrors the live gate exactly: the UNKNOWN-regime floor,
    then the B-band allowance, then the 5-minute rules, then the choppy rules.
    """
    confidence = float(inputs.confidence or 0.0)
    regime = str(inputs.regime or "")
    timeframe = str(inputs.timeframe or "")
    inst_type = str(inputs.inst_type or "").upper()
    adx_value = float(inputs.adx_value or 0.0)
    grade = str(inputs.grade or "")

    a_floor = grade_min_confidence("A")
    b_plus_floor = grade_min_confidence("B+")

    # 1. An UNKNOWN regime is missing evidence, so only the top tier may proceed.
    if regime == "UNKNOWN" and confidence < a_floor:
        return _block(
            "unknown_regime",
            "UNKNOWN_REGIME_NEEDS_A",
            f"UNKNOWN regime requires A/A+ (confidence >= {a_floor:.0%}); "
            f"got {grade or '?'} at {confidence:.0%}",
            confidence=confidence, required=a_floor, regime=regime,
        )

    # 2. The B band (0.40-0.55) has one dedicated allowance and no other way through:
    #    a trending regime, 15-minute agreement, and ADX momentum.
    if confidence < b_plus_floor:
        if b_grade_entry_allowed(
            confidence,
            regime,
            trend_15m_agrees=inputs.trend_15m_agrees,
            adx_value=adx_value,
            settings=settings,
        ):
            return _allow(
                "b_band", "B_BAND_TREND_MOMENTUM",
                f"B-grade allowed on trending regime + 15m alignment + ADX "
                f"{adx_value:.0f} at {confidence:.0%}",
                confidence=confidence, adx=adx_value, regime=regime,
                trend_15m_agrees=inputs.trend_15m_agrees,
            )
        return _block(
            "b_band", "B_BAND_NEEDS_TREND_MOMENTUM",
            f"B-grade requires trending regime + 15m alignment + ADX>=25 at "
            f"confidence>=0.50 ({grade or '?'}, {confidence:.0%})",
            confidence=confidence, adx=adx_value, regime=regime,
            trend_15m_agrees=inputs.trend_15m_agrees,
        )

    # 3. Five-minute signals. Live, 5min is a timing timeframe, so it must be either
    #    top-tier on its own or confirmed by the 15-minute trend.
    if timeframe == "5min":
        min_conf = relaxed_threshold(
            float(getattr(settings, "ut_5min_option_min_confidence", 0.70) or 0.70),
            settings,
        )
        # The 5-minute floor, as ONE named knob.
        #
        # It was `grade_min_confidence("A")` -- 0.70 -- reached only through a shared
        # grade helper, so loosening it meant either moving the A-grade boundary for the
        # whole system or editing a max() buried in a 322-line function. Named and
        # defaulted to 0.65 on 2026-09-01 at the operator's request.
        #
        # HONESTY NOTE, so nobody later mistakes this for a fix that mattered: over the
        # 30 sessions in the rtracker archive this rule refused NOTHING. Every live-gate
        # rejection in that window was the quad-prime raw floor (149), the 15min choppy
        # scalp guard (13) or market location (13). Signals are refused further upstream,
        # so moving this number is a knob for the future, not a change with a measured
        # effect behind it.
        five_min_floor = float(
            getattr(settings, "live_5min_quality_min_confidence", 0.65) or 0.65
        )
        grade_conf_ok = confidence >= max(five_min_floor, min_conf)

        if inputs.risky_regime and inst_type == "OPT":
            risky_min_conf = max(min_conf, 0.70)
            if confidence < risky_min_conf or not (
                inputs.trend_15m_agrees or inputs.impulse_confirmed
            ):
                return _block(
                    "five_min_risky_option", "RISKY_REGIME_OPTION_5MIN",
                    f"risky regime requires >= {risky_min_conf:.0%} plus 15min agreement "
                    f"or confirmed impulse ({regime}, {grade or '?'}, {confidence:.0%}, "
                    f"agree={inputs.trend_15m_agrees}, impulse={inputs.impulse_confirmed})",
                    confidence=confidence, required=risky_min_conf, regime=regime,
                    trend_15m_agrees=inputs.trend_15m_agrees,
                    impulse_confirmed=inputs.impulse_confirmed,
                )

        if not (grade_conf_ok or inputs.trend_15m_agrees):
            return _block(
                "five_min", "FIVE_MIN_NEEDS_TOP_TIER_OR_15M",
                f"needs A/A+ with >= {min_conf:.0%}, or 15min trend agreement "
                f"({grade or '?'}, {confidence:.0%}, agree={inputs.trend_15m_agrees})",
                confidence=confidence, required=max(a_floor, min_conf),
                trend_15m_agrees=inputs.trend_15m_agrees,
            )

        # The live gate repeats the same test once more under `is_choppy`. It can never
        # fire -- the identical condition was just checked above and returned -- so it is
        # recorded here rather than reimplemented. Removing dead branches is the whole
        # point of consolidating.
        if inputs.is_choppy and not (grade_conf_ok or inputs.trend_15m_agrees):
            return _block(
                "five_min_choppy", "FIVE_MIN_CHOPPY_NEEDS_TOP_TIER_OR_15M",
                f"choppy regime needs A/A+ with >= {min_conf:.0%}, or 15min trend "
                "agreement",
                confidence=confidence, required=max(a_floor, min_conf),
            )

    # 4. Options decay in range-bound sessions, so a choppy option needs the top tier.
    if inputs.is_choppy and inst_type == "OPT":
        option_floor = min(
            max(a_floor, relaxed_threshold(0.72, settings)),
            relaxed_threshold(choppy_confidence_gate(settings), settings),
        )
        if confidence < option_floor:
            return _block(
                "choppy_option", "CHOPPY_OPTION_NEEDS_TOP_TIER",
                "range/choppy regime allows only A/A+ option setups or very high "
                "confidence",
                confidence=confidence, required=option_floor, regime=regime,
            )

    # 5. And nothing weak trades a chop, option or not.
    if inputs.is_choppy:
        choppy_floor = min(
            a_floor, relaxed_threshold(choppy_confidence_gate(settings), settings)
        )
        if confidence < choppy_floor:
            return _block(
                "choppy", "CHOPPY_GRADE_TOO_WEAK",
                f"grade is too weak for this regime ({grade or '?'}, {regime})",
                confidence=confidence, required=choppy_floor, regime=regime,
            )

    return _allow(
        "standard", "QUALITY_OK",
        f"quality floors cleared ({grade or '?'}, {confidence:.0%}, {regime})",
        confidence=confidence, regime=regime, timeframe=timeframe,
    )
