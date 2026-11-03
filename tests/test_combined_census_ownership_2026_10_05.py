"""An S1 companion must not change Quad Prime's native directional-census gate.

The native candidate collection seam supplies controlled, already-graded signals.
Production partitioning, census mutation, selection refusal and contract building run
unchanged. Broker data are the isolated fixture's quotes; no live account is accessed.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest

import engine.signal_processor as processor_module
from engine.signal_processor import SignalProcessor
from tests.test_ut_smart_live_path_2026_09_26 import live


def _session(live, monkeypatch, mode, companion=False):
    h = live(tf="5min", settings_overrides={
        "scanner_engine": mode,
        "ut_timeframe_entry_policy": "PRIMARY_5",
        "directional_census_enabled": True,
        "directional_census_min_signals": 12,
        "directional_census_ratio": 1.5,
        "directional_census_penalty": 0.05,
        "session_context_trend_day_guard_enabled": False,
        "price_action_block_adverse": False,
        "volume_spike_entry_enabled": False,
        "option_contract_liquidity_gate_enabled": False,
        "option_strike_selection": "ATM",
    })
    settings = processor_module.get_settings()
    monkeypatch.setattr("config.settings._settings", settings)
    # Ten Quad Prime arrows are not enough to activate its 12-arrow census guard.
    h.sc._dir_census_date = h.sig.timestamp.date().isoformat()
    h.sc._dir_census = {"LONG": 8, "SHORT": 2}
    h.sc._dir_census_seen = set()
    for name in ("_detect_power_move", "_evaluate_market_ltp_shock",
                 "_entry_reversal_relief_context", "_stale_regime_reversal_context"):
        monkeypatch.setattr(h.sc, name, lambda *a, **k: False)
    monkeypatch.setattr(h.sc, "_handle_opposite_signal_exit", lambda *a, **k: False)
    monkeypatch.setattr(h.processor, "_append_adaptive_dual_apex_candidates", lambda **k: None)

    def collect(instrument, mtf, *args, **kwargs):
        return [dict(sig=s, tf="5min", grade="B+", confidence=0.58,
                     reasons=["Controlled eligible setup"], after_no_entry=False)
                for s in (getattr(mtf, "results_5min", None) or {}).get("signals", [])]

    monkeypatch.setattr(h.processor, "_collect_signal_candidates", collect)

    def run(native, side):
        sig = deepcopy(h.sig)
        sig.signal_type = side
        sig.source_engine = "s1" if native == "s1" else "utbot"
        sig.source_engines = [sig.source_engine]
        sig.source_lane = "S1" if native == "s1" else "QP"
        distance = abs(sig.price - sig.trailing_stop)
        sig.trailing_stop = sig.price + (distance if side == "SELL" else -distance)
        sig.volume_spike_entry = False
        sig.market_ltp_shock_entry = False
        sig.s1_score = 75.0
        sig.s1_sl1 = sig.trailing_stop
        result = {"signals": [sig], "state": {"position": -1 if side == "SELL" else 1}}
        if mode == "quad_prime_s1":
            result = {"native_results": {
                "quad_prime": result if native == "quad_prime" else {"signals": [], "state": None},
                "s1": result if native == "s1" else {"signals": [], "state": None},
            }}
        mtf = NS(instrument="NIFTY", results_1min=None, results_5min=result,
                 results_15min=None, confluence_score=-0.5 if side == "SELL" else 0.5,
                 confluence_signal=side, active_signals=[sig])
        args = list(h.args)
        args[1] = mtf
        return h.processor.process_best_signal(*args)

    if companion:
        out = run("s1", "BUY")
        assert [c.status for c in out] == ["TRADE SIGNAL"], "The S1 setup must actually reach admission"
    out = run("quad_prime", "SELL")
    return out, h.sc


def test_s1_companion_cannot_activate_quad_prime_counter_census(live, monkeypatch):
    native, _ = _session(live, monkeypatch, "quad_prime")
    combined, _ = _session(live, monkeypatch, "quad_prime_s1", companion=True)
    assert [c.status for c in native] == ["TRADE SIGNAL"]
    assert [c.status for c in combined] == ["TRADE SIGNAL"]
    assert (combined[0].direction, combined[0].grade, combined[0].confidence) == (
        native[0].direction, native[0].grade, native[0].confidence)


def test_s1_companion_cannot_add_to_quad_prime_direction_counts(live, monkeypatch):
    _, native = _session(live, monkeypatch, "quad_prime")
    _, combined = _session(live, monkeypatch, "quad_prime_s1", companion=True)
    # The companion may own a separate census; the existing QP census remains native.
    assert combined._dir_census == native._dir_census == {"LONG": 8, "SHORT": 3}


@pytest.mark.parametrize("identity_format", ["live", "historical"])
@pytest.mark.parametrize("mode,source,lane", [
    ("quad_prime_s1", "s1", "S1"),
    ("ut_smart_s1", "s1", "S1"),
    ("ut_smart_s1", "ut_smart", "UTS"),
    ("ut_smart", "ut_smart", "UTS"),
])
def test_live_and_replay_census_skip_independent_lane_arrows(identity_format, mode, source, lane):
    day = datetime(2026, 10, 5, 10, 0)
    scanner = NS(_dir_census_date=day.date().isoformat(),
                 _dir_census={"LONG": 8, "SHORT": 2}, _dir_census_seen={"existing"})
    settings = NS(scanner_engine=mode, s1_lane_enabled=True, uts_lane_enabled=True)
    # A companion from another session must not reset the native census either.
    signal = NS(timestamp=day + timedelta(days=1), source_engine=source, source_lane=lane)
    stamp = signal.timestamp.isoformat() if identity_format == "live" else str(signal.timestamp)
    SignalProcessor._record_directional_census(scanner, signal, "NIFTY", "5min", "LONG", stamp, settings)
    assert scanner._dir_census == {"LONG": 8, "SHORT": 2}
    assert scanner._dir_census_seen == {"existing"}
    assert scanner._dir_census_date == "2026-10-05"


@pytest.mark.parametrize("identity_format", ["live", "historical"])
def test_native_quad_prime_census_keeps_day_reset_and_identity_dedup(identity_format):
    day = datetime(2026, 10, 5, 10, 0)
    scanner = NS()
    settings = NS(scanner_engine="quad_prime", s1_lane_enabled=True, uts_lane_enabled=True)
    signal = NS(timestamp=day, source_engine="utbot", source_lane="QP")

    def record():
        stamp = signal.timestamp.isoformat() if identity_format == "live" else str(signal.timestamp)
        SignalProcessor._record_directional_census(scanner, signal, "NIFTY", "5min", "SHORT", stamp, settings)
        return stamp

    stamp = record()
    record()
    assert scanner._dir_census == {"LONG": 0, "SHORT": 1}
    assert scanner._dir_census_seen == {f"NIFTY|5min|{stamp}|SHORT"}
    signal.timestamp += timedelta(days=1)
    stamp = record()
    assert scanner._dir_census == {"LONG": 0, "SHORT": 1}
    assert scanner._dir_census_date == "2026-10-06"
    assert scanner._dir_census_seen == {f"NIFTY|5min|{stamp}|SHORT"}
