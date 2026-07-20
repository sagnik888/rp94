"""1M SPIKE: depth/OFR confirm + aggressive stabilization floor (3s)."""

from types import SimpleNamespace

import pytest

from config.settings import Settings, get_settings
from engine.signal_quality_policy import (
    volume_spike_depth_ofr_aligned,
    volume_spike_entry_confirmed,
)
from scanner import Scanner


@pytest.fixture
def settings():
    s = get_settings()
    s.volume_spike_entry_enabled = True
    s.one_min_scalping_enabled = True
    s.one_min_scalping_high_burst_only = False
    s.volume_spike_entry_min_volume_ratio = 2.5
    s.volume_spike_entry_min_range_atr_ratio = 1.5
    s.volume_spike_entry_min_close_location = 0.65
    s.volume_spike_depth_confirm_enabled = True
    s.volume_spike_depth_ofr_bullish = 1.08
    s.volume_spike_depth_ofr_bearish = 0.92
    s.volume_spike_entry_stabilization_seconds = 3.0
    s.volume_spike_entry_stabilization_seconds_min = 3.0
    s.volume_spike_entry_stabilization_seconds_max = 15.0
    s.live_signal_stabilization_seconds_5min = 30.0
    s.live_signal_stabilization_seconds_15min = 30.0
    return s


def test_defaults_spike_stab_floor_3s_and_depth_on():
    # Field defaults (ignore persisted env/json overrides on Settings()).
    fields = Settings.model_fields
    assert fields["volume_spike_entry_stabilization_seconds"].default == 3.0
    assert fields["volume_spike_entry_stabilization_seconds_min"].default == 3.0
    assert fields["volume_spike_depth_confirm_enabled"].default is True
    assert fields["live_signal_stabilization_seconds_5min"].default == 30.0
    assert fields["live_signal_stabilization_seconds_15min"].default == 30.0
    assert fields["midcap_15min_risky_stabilization_seconds"].default == 60.0


def test_ofr_aligned_buy_sell(settings):
    assert volume_spike_depth_ofr_aligned(
        signal_type="BUY", ofr=1.15, depth_usable=True, settings=settings
    )
    assert not volume_spike_depth_ofr_aligned(
        signal_type="BUY", ofr=1.00, depth_usable=True, settings=settings
    )
    assert volume_spike_depth_ofr_aligned(
        signal_type="SELL", ofr=0.85, depth_usable=True, settings=settings
    )
    assert not volume_spike_depth_ofr_aligned(
        signal_type="SELL", ofr=1.05, depth_usable=True, settings=settings
    )


def test_depth_unavailable_never_blocks(settings):
    assert volume_spike_depth_ofr_aligned(
        signal_type="BUY", ofr=0.5, depth_usable=False, settings=settings
    )
    # Gate OFF → always pass even with adverse OFR
    settings.volume_spike_depth_confirm_enabled = False
    assert volume_spike_depth_ofr_aligned(
        signal_type="BUY", ofr=0.5, depth_usable=True, settings=settings
    )


def test_1min_spike_blocked_when_depth_opposes(settings):
    base = dict(
        timeframe="1min",
        volume_ratio=3.0,
        range_atr_ratio=1.6,
        close_location=0.70,
        adx_value=30.0,
        settings=settings,
        signal_type="BUY",
        depth_usable=True,
        ofr=0.80,  # asks dominate — blocks BUY
    )
    assert volume_spike_entry_confirmed(**base) is False
    base["ofr"] = 1.20
    assert volume_spike_entry_confirmed(**base) is True


def test_1min_spike_ok_without_depth(settings):
    assert volume_spike_entry_confirmed(
        timeframe="1min",
        volume_ratio=3.0,
        range_atr_ratio=1.6,
        close_location=0.70,
        adx_value=30.0,
        settings=settings,
        signal_type="SELL",
        depth_usable=False,
        ofr=0.0,
    ) is True


def test_5min_spike_ignores_depth_gate(settings):
    # Depth gate is 1min-only; 5min still passes with adverse OFR.
    assert volume_spike_entry_confirmed(
        timeframe="5min",
        volume_ratio=2.5,
        range_atr_ratio=1.5,
        close_location=0.65,
        settings=settings,
        signal_type="BUY",
        depth_usable=True,
        ofr=0.50,
    ) is True


def test_spike_stabilization_clamped_to_floor_and_ceiling(settings):
    st = Scanner.__new__(Scanner)
    st.latest_regimes = {}
    cand = SimpleNamespace(
        timeframe="1min",
        volume_spike_entry=True,
        market_ltp_shock_entry=False,
        market_wide_reversal_entry=False,
        inst_type="FUT",
        instrument="NIFTY",
    )
    # Request below floor → clamp to 3
    settings.volume_spike_entry_stabilization_seconds = 1.0
    sec, scans, adapt = Scanner._live_signal_stabilization_requirements(st, cand)
    assert sec == 3.0
    assert scans == 2
    assert adapt is True
    # Request above max → clamp to 15
    settings.volume_spike_entry_stabilization_seconds = 99.0
    sec2, _, _ = Scanner._live_signal_stabilization_requirements(st, cand)
    assert sec2 == 15.0
    # Default path
    settings.volume_spike_entry_stabilization_seconds = 3.0
    sec3, _, _ = Scanner._live_signal_stabilization_requirements(st, cand)
    assert sec3 == 3.0


def test_5m_15m_stabilization_unchanged_not_spike(settings):
    st = Scanner.__new__(Scanner)
    st.latest_regimes = {"NIFTY": "TRENDING_UP"}
    c5 = SimpleNamespace(
        timeframe="5min",
        volume_spike_entry=False,
        market_ltp_shock_entry=False,
        market_wide_reversal_entry=False,
        inst_type="FUT",
        instrument="NIFTY",
    )
    c15 = SimpleNamespace(
        timeframe="15min",
        volume_spike_entry=False,
        market_ltp_shock_entry=False,
        market_wide_reversal_entry=False,
        inst_type="FUT",
        instrument="NIFTY",
    )
    sec5, scans5, _ = Scanner._live_signal_stabilization_requirements(st, c5)
    sec15, scans15, _ = Scanner._live_signal_stabilization_requirements(st, c15)
    assert sec5 == 30.0
    assert sec15 == 30.0
    assert scans5 == 1
    assert scans15 == 1


def test_advanced_settings_ui_exposes_spike_stab_and_depth():
    from pathlib import Path

    js = Path("dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    assert "settVolumeSpikeStabilization" in js
    assert "settVolumeSpikeDepthConfirm" in js
    assert "volume_spike_depth_confirm_enabled" in js
    assert "Math.max(3" in js  # floor clamp on save
    assert "Math.min(15" in js  # ceiling clamp on save
    assert "settLiveStab5" in js and "Math.max(30" in js
    assert "Math.min(60" in js  # 5m/15m stay in 30–60 band
