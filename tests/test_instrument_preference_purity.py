"""
Instrument-preference purity tests.

Guarantees that when the operator pins the system to a single instrument type
(`inst_pref="FUT"` or `inst_pref="OPT"`), the selection engine
(`Scanner._resolve_instrument_type`) NEVER emits the other type — regardless of
regime, grade, confidence, ADX, IV, timeframe, impulse state, or expiry-day.

These tests also pin the regression that broke the selector entirely:
`process_best_signal` calls `_resolve_instrument_type(..., timeframe=...,
five_min_true_impulse=...)`, so the signature MUST accept those kwargs. Before
the fix every call raised TypeError and produced zero trades.
"""

import inspect
import itertools
from datetime import datetime
from types import SimpleNamespace

import pytest

from scanner import Scanner


class _StubSignalProcessor:
    def _option_grade_allowed(
        self,
        grade,
        confidence,
        regime,
        adx_value=0.0,
        intel_score=0.0,
        signal_score=0.0,
        five_min_true_impulse=False,
        timeframe="",
    ):
        # Mirror the real gate's spirit: only strong setups clear the OPT gate.
        return grade in {"A", "A+", "B+"} and confidence >= 0.62


class _StubExpiry:
    def __init__(self, expiry_day=False):
        self._expiry_day = expiry_day

    def is_expiry_day(self, instrument, day):
        return self._expiry_day


def _make_scanner(inst_pref, expiry_day=False):
    return SimpleNamespace(
        inst_pref=inst_pref,
        mode="REAL",
        signal_processor=_StubSignalProcessor(),
        expiry=_StubExpiry(expiry_day),
        _setting_enabled=lambda value, default: default,
    )


# A broad grid that exercises every decision branch in _resolve_instrument_type.
_REGIMES = ["TRENDING", "RANGING", "VOLATILE", "CHOPPY", "MEAN_REVERTING", "UNKNOWN"]
_GRADES = ["C", "B", "B+", "A", "A+"]
_CONFS = [0.30, 0.62, 0.85, 0.95]
_ADX = [10.0, 28.0, 40.0]
_IVP = [30.0, 75.0, 95.0]
_TFS = ["5min", "15min"]
_IMPULSE = [False, True]
_TIMES = [datetime(2026, 6, 19, 10, 0), datetime(2026, 6, 19, 14, 30)]


def _grid():
    for regime, grade, conf, adx, ivp, tf, impulse, ts in itertools.product(
        _REGIMES, _GRADES, _CONFS, _ADX, _IVP, _TFS, _IMPULSE, _TIMES
    ):
        yield dict(
            instrument="NIFTY",
            grade=grade,
            regime=regime,
            intel_score=0.6,
            signal_score=0.5,
            confidence=conf,
            adx_value=adx,
            atr_value=20.0,
            price=25000.0,
            signal_time=ts,
            iv_percentile=ivp,
            timeframe=tf,
            five_min_true_impulse=impulse,
        )


def test_signature_accepts_caller_kwargs():
    """Regression guard: process_best_signal passes these; signature must accept them."""
    params = inspect.signature(Scanner._resolve_instrument_type).parameters
    assert "timeframe" in params
    assert "five_min_true_impulse" in params


@pytest.mark.parametrize("expiry_day", [False, True])
def test_fut_only_mode_never_emits_options(expiry_day):
    scanner = _make_scanner("FUT", expiry_day=expiry_day)
    for kwargs in _grid():
        result = Scanner._resolve_instrument_type(scanner, **kwargs)
        assert result == "FUT", f"FUT-only leaked {result} for {kwargs}"


@pytest.mark.parametrize("expiry_day", [False, True])
def test_opt_only_mode_never_emits_futures(expiry_day):
    scanner = _make_scanner("OPT", expiry_day=expiry_day)
    for kwargs in _grid():
        result = Scanner._resolve_instrument_type(scanner, **kwargs)
        assert result == "OPT", f"OPT-only leaked {result} for {kwargs}"


def test_auto_mode_actually_chooses_both():
    """Proves the forcing is meaningful: AUTO yields a genuine mix over the grid."""
    scanner = _make_scanner("AUTO")
    results = {Scanner._resolve_instrument_type(scanner, **kwargs) for kwargs in _grid()}
    assert results == {"FUT", "OPT"}, f"AUTO did not produce both types: {results}"
