"""Golden matrix for the canonical scanner LIVE quality/safety funnel.

Every consistent grade/confidence input must return a decision, never an exception.
The hash pins the current B+ evidence floor, explicit A/A+ operator preference and
own-timeframe regime behavior over a 115,200-cell grid.
"""

import hashlib
import itertools
import json
from types import SimpleNamespace

from loguru import logger

from scanner import Scanner
from config.settings import get_settings
from engine.signal_quality_policy import confidence_to_grade

# Regenerated 2026-09-02 after removing the B-band admission bypass and making the
# canonical evidence verdict the only B+ authority. Row-level assertions below first
# guarantee that an exception cannot be hidden inside a merely updated hash.
LIVEGATE_SHA256 = "d6cba60e5d23520211f91f1cca7369348728685f332a3e17d74e8b731bb2d537"

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
    # The gates resolve the CANDIDATE's own-timeframe regime instead of the
    # instrument-keyed 5-minute label; unbound, the gate paths raise AttributeError.
    "_get_trade_regime",
    "_trade_regime_is_own_timeframe",
]
_STATIC_METHODS = ["_normalize_signal_grade_preference", "_setting_enabled",
                   "_is_quad_prime_raw_candidate", "_timeframe_intel_key"]


def _stub(regime, tf_regimes=None, *, trend_agrees=True):
    import time
    st = SimpleNamespace(mode="REAL", latest_regimes={"NIFTY": regime},
                         _timeframe_regimes=dict(tf_regimes or {}),
                         _intel_cache_time={key: time.time() for key in {'NIFTY_5min', *(tf_regimes or {})}})
    # The matrix's agreement axis represents current market evidence, not a
    # candidate flag that may have become stale while waiting for admission.
    st.mtf = SimpleNamespace(engines={
        "NIFTY_15min": SimpleNamespace(
            get_state=lambda _key: SimpleNamespace(position=1 if trend_agrees else -1)
        ),
    })
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
    logger.disable("scanner")
    # own_tf: whether the regime label exists on the CANDIDATE's own timeframe, or only
    # on the instrument-keyed 5-minute one it would be borrowing. Added 2026-08-09 --
    # the matrix previously only ever exercised the borrowed shape, so it could not see
    # a privilege being granted on the wrong timeframe.
    for conf, reg, tf, inst, a, adx, im, ex, pref, pol, own_tf in itertools.product(
        confs, regs, ["5min", "15min"], ["FUT", "OPT"], [True, False],
        [10.0, 28.0, 40.0], [True, False], [True, False],
        ["auto", "B", "B+", "A", "A+"], ["PRIMARY_15", "INCLUDE_5MIN"], [True, False],
    ):
        s.signal_grade_preference = pref
        s.ut_timeframe_entry_policy = pol
        g = confidence_to_grade(conf)
        tfr = {f"NIFTY_{tf}": reg} if own_tf else {}
        try:
            gp = _stub(reg, tfr, trend_agrees=a)._passes_candidate_grade_preference(_cand(g, conf, tf, inst, a, adx, im, ex))[0]
        except Exception as e:
            gp = "ERR:" + type(e).__name__
        try:
            lr = _stub(reg, tfr, trend_agrees=a)._passes_live_trade_ready_gate(_cand(g, conf, tf, inst, a, adx, im, ex))
        except Exception as e:
            lr = "ERR:" + type(e).__name__
        rows.append((conf, reg, tf, inst, a, adx, im, ex, pref, pol, own_tf, gp, lr))
    logger.enable("scanner")

    assert len(rows) == 115200
    errors = [row for row in rows if str(row[-2]).startswith("ERR:") or str(row[-1]).startswith("ERR:")]
    assert not errors, f"live-gate grid raised exceptions: {errors[:3]}"
    borrowed_15min_admissions = [
        row for row in rows if row[2] == "15min" and row[10] is False and row[-1] is True
    ]
    assert not borrowed_15min_admissions, (
        "15min admission borrowed a regime label from the 5min pocket"
    )
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    assert digest == LIVEGATE_SHA256, (
        f"Live-gate behavior changed vs golden. If intentional, regenerate (got {digest})."
    )
