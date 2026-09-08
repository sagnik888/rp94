"""Golden grid for canonical quality and option-product routing helpers.

Grade is bucket(confidence), B/C never enter, lanes share one quality verdict, and
ADX/intelligence cannot be counted again as a synthetic impulse confirmation.
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

# Regenerated 2026-09-02 after retiring B-band option routing and removing implicit
# ADX+trend+intel -> impulse double-counting from the canonical compatibility wrapper.
GOLDEN_SHA256 = "f60dddb100e5c8f33684baebddef9a0cfef103e6aa1471f17ee6aa6a2d949fb9"

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
    assert not [row for row in rows if row[0] < 0.55 and (row[-2] or row[-1])], (
        "a B/C row crossed either quality admission or option routing"
    )
    lane_groups = {}
    for row in rows:
        lane_key = row[:4] + row[5:11]
        lane_groups.setdefault(lane_key, set()).add(row[-2])
    assert all(len(decisions) == 1 for decisions in lane_groups.values()), (
        "quality verdict differs between REAL and HISTORICAL lanes"
    )
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    assert digest == GOLDEN_SHA256, (
        "Gate behavior changed vs golden snapshot. If intentional, regenerate "
        f"GOLDEN_SHA256 (got {digest})."
    )
