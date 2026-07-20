"""
Behavior-parity guard for the grading/confidence consolidation.

Grade is bucket(confidence); the gates were refactored to drop the redundant
`rank AND/OR conf` double-checks in favour of confidence-only thresholds. With
`live_filter_leniency_pct == 0.0` (production setting) this MUST be exactly
behavior-neutral. This test pins a golden hash over a 61,440-cell grid of
consistent (grade == bucket(confidence)) inputs.

If you intentionally change a confidence floor, regenerate GOLDEN_SHA256.
"""

import hashlib
import itertools
import json

import pytest

from config.settings import get_settings
from engine.signal_quality_policy import (
    confidence_to_grade,
    option_grade_allowed,
    passes_quality_gate,
)

# Updated after the deliberate B-grade rule + 15m-UNKNOWN(0.85->0.70) + 5m-choppy-OPT
# (0.65->0.70) changes. Regenerated 2026-06-23 for the 5min trend-CONTINUATION path
# (live_5min_trend_continuation_enabled): admits 240 strong-trend 5min cells
# (trending regime + ADX>=28 + 15m-aligned) previously rejected, so the system
# stops missing big directional moves. Diff audited: all 240 are False->True,
# all 5min, all trending regimes, all ADX>=28, all 15m-agree.
GOLDEN_SHA256 = "a1948c1b9530535be6554e64e0b9057c5549386a797a35e5dfa89fbb2b4eb351"

_CONFS = [0.30, 0.42, 0.50, 0.54, 0.58, 0.62, 0.68, 0.72, 0.80, 0.88]
_REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "BREAKOUT_UP", "RANGING",
            "VOLATILE", "UNKNOWN", "CHOPPY", "MOMENTUM"]
_TFS = ["5min", "15min"]
_INSTS = ["FUT", "OPT"]
_MODES = ["REAL", "HISTORICAL"]
_ADX = [10.0, 28.0, 40.0]
_INTEL = [0.0, 0.6]
_SCORE = [0.0, 0.6]
_T15 = [True, False]
_IMP = [True, False]
_POLS = ["PRIMARY_15", "INCLUDE_5MIN"]


def test_gate_behavior_parity_golden():
    s = get_settings()
    s.live_filter_leniency_pct = 0.0
    s.ut_regime_adaptation = True
    rows = []
    for conf, regime, tf, inst, mode, adx, intel, score, tr, im, pol in itertools.product(
        _CONFS, _REGIMES, _TFS, _INSTS, _MODES, _ADX, _INTEL, _SCORE, _T15, _IMP, _POLS
    ):
        s.ut_timeframe_entry_policy = pol
        g = confidence_to_grade(conf)
        q = passes_quality_gate(
            tf, g, conf, regime, inst, s, mode=mode, adx_value=adx,
            trend_15m_agrees=tr, intel_score=intel, iv_percentile=50.0,
            option_premium_ok=True,
        )
        o = option_grade_allowed(
            g, conf, regime, mode=mode, adx_value=adx, intel_score=intel,
            signal_score=score, five_min_true_impulse=im, timeframe=tf,
        )
        rows.append((conf, regime, tf, inst, mode, adx, intel, score, tr, im, pol, q, o))

    assert len(rows) == 61440
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    assert digest == GOLDEN_SHA256, (
        "Gate behavior changed vs golden snapshot. If intentional, regenerate "
        f"GOLDEN_SHA256 (got {digest})."
    )
