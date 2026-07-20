"""
Behavior-parity guard for the scanner LIVE gate consolidation.

`_passes_live_trade_ready_gate` / `_passes_candidate_grade_preference` had the
same redundant `grade_rank AND/OR confidence` double-checks as the policy gate.
They were collapsed to confidence-only. With `live_filter_leniency_pct == 0.0`
this MUST be exactly behavior-neutral over consistent (grade == bucket(conf))
inputs. Golden hash over a 57,600-cell grid.

If you intentionally change a live floor, regenerate LIVEGATE_SHA256.
"""

import hashlib
import itertools
import json
from types import SimpleNamespace

from scanner import Scanner
from config.settings import get_settings
from engine.signal_quality_policy import confidence_to_grade

# Updated after the deliberate B-grade rule + 15m-UNKNOWN(0.85->0.70)
# and current advanced gate/volume-spike behavior changes.
# Now a regression guard for the CURRENT intended live-gate behavior.
LIVEGATE_SHA256 = "9a0b15ecccc4bb14c7bfce9b98a07b8a9a689565458bafadf35bea3cc7b42b44"

_INST_METHODS = [
    "_passes_live_trade_ready_gate",
    "_passes_candidate_grade_preference",
    "_passes_timeframe_entry_policy",
    "_market_location_entry_gate",
    "_candidate_15m_trend_agrees",
    "_late_entry_scalp_active",
    "_set_scalp_lock",
    "_minutes_to_force_exit",
    # Rejection helper that gate paths route through for live-tape logging.
    # It must be bound on the stub or block paths raise AttributeError instead
    # of returning False, silently turning this regression guard into a no-op.
    "_gate_block",
]
_STATIC_METHODS = ["_normalize_signal_grade_preference", "_setting_enabled", "_is_quad_prime_raw_candidate"]


def _stub(regime):
    st = SimpleNamespace(mode="REAL", latest_regimes={"NIFTY": regime})
    for m in _INST_METHODS:
        setattr(st, m, getattr(Scanner, m).__get__(st, Scanner))
    for m in _STATIC_METHODS:
        setattr(st, m, getattr(Scanner, m))
    # `_gate_block` logs the rejection to the live tape; on a bare stub there is
    # no activity log, so make tape logging a no-op (decision is unaffected).
    st.log_signal_decision_once = lambda *a, **k: None
    return st


def _cand(g, conf, tf, inst, a, adx, im, ex):
    return SimpleNamespace(
        grade=g, confidence=conf, timeframe=tf, inst_type=inst, instrument="NIFTY",
        direction="LONG", adx_value=adx, impulse_confirmed=im, is_explosive_bypass=ex,
        trend_15m_agrees=a, reasons=[],
    )


def test_live_gate_behavior_parity_golden():
    s = get_settings()
    s.live_filter_leniency_pct = 0.0
    s.ut_regime_adaptation = True
    confs = [0.30, 0.42, 0.50, 0.54, 0.58, 0.62, 0.68, 0.72, 0.80, 0.88]
    regs = ["TRENDING_UP", "BREAKOUT_UP", "RANGING", "VOLATILE", "UNKNOWN", "MOMENTUM"]
    rows = []
    for conf, reg, tf, inst, a, adx, im, ex, pref, pol in itertools.product(
        confs, regs, ["5min", "15min"], ["FUT", "OPT"], [True, False],
        [10.0, 28.0, 40.0], [True, False], [True, False],
        ["auto", "B", "B+", "A", "A+"], ["PRIMARY_15", "INCLUDE_5MIN"],
    ):
        s.signal_grade_preference = pref
        s.ut_timeframe_entry_policy = pol
        g = confidence_to_grade(conf)
        try:
            gp = _stub(reg)._passes_candidate_grade_preference(_cand(g, conf, tf, inst, a, adx, im, ex))[0]
        except Exception as e:
            gp = "ERR:" + type(e).__name__
        try:
            lr = _stub(reg)._passes_live_trade_ready_gate(_cand(g, conf, tf, inst, a, adx, im, ex))
        except Exception as e:
            lr = "ERR:" + type(e).__name__
        rows.append((conf, reg, tf, inst, a, adx, im, ex, pref, pol, gp, lr))

    assert len(rows) == 57600
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    assert digest == LIVEGATE_SHA256, (
        f"Live-gate behavior changed vs golden. If intentional, regenerate (got {digest})."
    )
