"""The UTS 5m intel gate (6 Oct): plain 5m flips need volume, ADX, directional RSI and no opposing PCR."""
from types import SimpleNamespace as NS

import pytest

from config.settings import Settings
from engine import uts_lane


def _settings(**overrides):
    return Settings(_env_file=None, **overrides)


def _signal(timeframe="5min", direction="LONG", adx=25.0, rsi=60.0, action="ENTRY", **extra):
    return NS(instrument="NIFTY", timeframe=timeframe, direction=direction, action=action,
              source_engine="ut_smart", adx_value=adx, rsi_value=rsi, **extra)


def _verdict(signal, settings=None, pcr=None, ratio=1.4, available=True, basis="EXCHANGE_VOLUME"):
    return uts_lane.entry_verdict(signal, settings or _settings(), pcr=pcr,
                                  volume_ratio=ratio, volume_available=available, volume_basis=basis)


def test_supported_5m_flip_is_admitted():
    verdict = _verdict(_signal())
    assert verdict.allowed
    assert verdict.checks["uts_5m_intel_failures"] == []


@pytest.mark.parametrize("kwargs,signal_kwargs,needle", [
    (dict(available=False), {}, "volume unmeasured"),
    (dict(basis="INDEX_TICK_ACTIVITY"), {}, "volume unmeasured"),
    (dict(ratio=0.6), {}, "volume 0.60x"),
    ({}, dict(adx=15.0), "ADX 15.0"),
    ({}, dict(rsi=52.0), "not directional"),
    ({}, dict(direction="SHORT", rsi=50.0), "not directional"),
])
def test_each_unsupported_leg_refuses_the_5m_flip(kwargs, signal_kwargs, needle):
    verdict = _verdict(_signal(**signal_kwargs), **kwargs)
    assert not verdict.allowed
    assert verdict.reason_code == "UTS_5MIN_INTEL_UNSUPPORTED"
    assert any(needle in item for item in verdict.checks["uts_5m_intel_failures"])


@pytest.mark.parametrize("direction,rsi,read", [("LONG", 60.0, "BEARISH"), ("SHORT", 40.0, "STRONG_BULLISH")])
def test_opposing_pcr_vetoes_but_missing_pcr_does_not(direction, rsi, read):
    signal = _signal(direction=direction, rsi=rsi)
    assert not _verdict(signal, pcr={"available": True, "signal": read}).allowed
    assert _verdict(signal, pcr={"available": False, "signal": read}).allowed
    assert _verdict(signal, pcr=None).allowed


def test_fifteen_minute_and_special_entries_are_not_gated():
    assert _verdict(_signal(timeframe="15min", adx=10.0, rsi=52.0), ratio=0.6).allowed
    special = _signal(adx=10.0, rsi=52.0, volume_spike_entry=True)
    assert _verdict(special, ratio=0.6).checks["uts_5m_intel_failures"] == []


def test_gate_can_be_switched_off():
    off = _settings(uts_lane_5m_intel_gate_enabled=False)
    assert _verdict(_signal(adx=10.0, rsi=52.0), off, ratio=0.6).allowed


def test_exit_rows_are_never_refused():
    exit_row = _signal(adx=10.0, rsi=52.0, action="EXIT")
    assert _verdict(exit_row, ratio=0.6).allowed
