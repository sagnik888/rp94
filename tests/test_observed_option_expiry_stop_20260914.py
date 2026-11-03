from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from engine.signal_processor import SignalProcessor
from intelligence.greeks_engine import GreeksEngine
from intelligence.intelligence_aggregator import IntelligenceAggregator


@pytest.mark.parametrize("side", ["CE", "PE"])
@pytest.mark.parametrize("expiry,dte", [("2026-08-18", 0), ("2026-08-25", 7), ("2026-09-29", 42)])
def test_observed_contract_drives_stop_mapping_and_trace(monkeypatch, side, expiry, dte):
    monkeypatch.setattr("engine.signal_processor.get_settings", lambda: SimpleNamespace(ut_force_exit_time="15:25", backtest_live_exits_enabled=False))
    processor = SignalProcessor(SimpleNamespace(mode="HISTORICAL"))
    frame = pd.DataFrame({"open": [200., 200.], "high": [201., 201.], "low": [199., 199.], "close": [200., 200.]}, index=pd.date_range("2026-08-18 10:00", periods=2, freq="1min"))
    frame.attrs.update(contract_expiry=expiry, contract_verified=True, contract_symbol="exact", contract_strike=25000, contract_option_type=side)
    evidence = processor._observed_option_stop_context(frame, frame.index[0], 25050, 25000, side, 40)
    delta = GreeksEngine().calculate(25050, 25000, max(dte, .25), .15, "call" if side == "CE" else "put")["delta"]
    assert evidence["natural_stop_delta"] == pytest.approx(delta)
    assert evidence["expiry_context"]["days_to_expiry"] == dte
    assert evidence["natural_sl_distance"] == pytest.approx(abs(40 * delta))
    out = processor._simulate_actual_option_exit(frame, frame.index[0], frame.index[-1] + pd.Timedelta(minutes=1), 100, 3, natural_sl_distance=evidence["natural_sl_distance"], max_sl_pct=.9, replay_stop_context=evidence)
    assert out["stop"] == pytest.approx(200 - min(abs(40 * delta), 200 * .10))
    trace = out["exit_trace"][0]
    for key, value in evidence.items():
        assert trace[key] == value
    assert trace["natural_stop_delta_source"] == "estimated_black_scholes_iv_15pct"
    assert trace["natural_stop_iv"] == .15


@pytest.mark.parametrize("expiry,verified", [(None, True), ("2026-08-18", False), ("2026-08-17", True), ("bad", True)])
def test_unknown_or_expired_contract_cannot_map_observed_stop(expiry, verified):
    frame = pd.DataFrame()
    frame.attrs.update(contract_expiry=expiry, contract_verified=verified)
    assert SignalProcessor._observed_option_stop_context(frame, pd.Timestamp("2026-08-18 10:00"), 25000, 25000, "CE", 40) is None


def test_missing_expiry_abstains_iv_inversion_and_greeks_but_keeps_pcr(monkeypatch):
    intel = IntelligenceAggregator()
    intel._chain_iv_context = Mock(side_effect=AssertionError("IV inversion requires verified DTE"))
    intel.greeks.analyze_atm = Mock(side_effect=AssertionError("Greeks require verified DTE"))
    chain = pd.DataFrame({"strike": [24950, 25000, 25050], "call_oi": [10000, 9000, 15000], "put_oi": [14000, 11000, 10000], "call_ltp": [100, 80, 60], "put_ltp": [60, 80, 100], "call_volume": [100, 110, 120], "put_volume": [120, 100, 100]})
    frame = pd.DataFrame({"open": range(25000, 25060), "high": range(25001, 25061), "low": range(24999, 25059), "close": range(25000, 25060), "volume": 1000}, index=pd.date_range("2026-08-18 09:15", periods=60, freq="1min"))
    out = intel.analyze("NIFTY", "1min", frame, options_chain=chain, spot_price=25000, days_to_expiry=None)
    assert out["greeks"]["available"] is False
    assert out["greeks"]["instrument_iv"] is None
    assert "call" not in out["greeks"]
    assert out["pcr"]["available"] is True
    intel._chain_iv_context.assert_not_called()


def test_unknown_dte_cannot_be_treated_as_zero_day_greeks():
    assert SignalProcessor._option_signed_delta(25000, 25000, "CE", {}, None, .42) == .42
