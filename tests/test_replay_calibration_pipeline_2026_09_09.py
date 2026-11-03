from types import SimpleNamespace

import pandas as pd

from config.settings import get_settings
from data.sqlite_db import TRADE_PERSISTED_COLUMNS, _TRADE_JSON_COLUMNS
from engine.signal_processor import SignalProcessor
from intelligence import confidence_calibration
from intelligence.outcome_calibration import CALIBRATION_FIELDS, OutcomeCalibrationStore
from trading.trade_manager import Trade
from scanner import Scanner


def _trade(**overrides):
    values = dict(
        id="T1", instrument="NIFTY", timeframe="5min", direction="LONG",
        entry_price=100.0, entry_time=pd.Timestamp("2026-09-08 10:00").to_pydatetime(),
        trailing_stop=90.0, current_stop=90.0, lots=1, lot_size=65, grade="A",
    )
    values.update(overrides)
    return Trade(**values)


def test_entry_calibration_evidence_survives_trade_and_sqlite_round_trip_contract():
    trade = _trade(
        entry_intel={"regime_label": "TRENDING_UP", "pcr": 1.1},
        decision_intel={"terminal_fill_event": True},
        calibration_outcome_recorded=True,
    )
    restored = Trade.from_dict(trade.to_dict())
    assert restored.entry_intel == trade.entry_intel
    assert restored.decision_intel == trade.decision_intel
    assert restored.calibration_outcome_recorded is True
    assert {"entry_intel", "decision_intel", "calibration_outcome_recorded"} <= TRADE_PERSISTED_COLUMNS
    assert {"entry_intel", "decision_intel"} <= _TRADE_JSON_COLUMNS


def test_outcome_store_accepts_compact_entry_snapshot_without_inventing_fields(tmp_path):
    trade = _trade(market_regime="TRENDING_UP", pnl=125.0)
    row = OutcomeCalibrationStore(str(tmp_path / "out.csv")).build_row(
        trade,
        {"regime_label": "TRENDING_UP", "pcr": 1.2, "oi_signal": "BULLISH", "order_ratio": 1.3},
    )
    assert row["regime"] == "TRENDING_UP"
    assert row["pcr"] == 1.2
    assert row["oi_signal"] == "BULLISH"
    assert row["ofr"] == 1.3
    assert row["iv_percentile"] == ""


def test_real_terminal_fill_records_calibration_once(tmp_path):
    store = OutcomeCalibrationStore(str(tmp_path / "out.csv"))
    dirty = []
    scanner = SimpleNamespace(
        mode="REAL",
        outcome_calibration=store,
        trades=SimpleNamespace(_mark_dirty=lambda: dirty.append(True)),
        _rtracker_record_candidate=lambda *args, **kwargs: None,
    )
    trade = _trade(
        status="CLOSED", pnl=125.0, peak_pnl=300.0,
        exit_reason="TARGET_HIT",
        entry_intel={"regime_label": "TRENDING_UP", "pcr": 1.2},
    )
    Scanner._record_trade_manager_terminal(scanner, trade)
    Scanner._record_trade_manager_terminal(scanner, trade)
    assert trade.calibration_outcome_recorded is True
    assert store.validation_status(min_samples=1, min_samples_per_instrument=1)["samples"] == 1
    assert dirty == [True]


def test_confidence_needs_explicit_live_promotion_and_exact_ready_segment(tmp_path):
    path = tmp_path / "calibration.csv"
    store = OutcomeCalibrationStore(str(path))
    rows = []
    for i, (inst, conf, pnl) in enumerate(
        [("NIFTY", 0.2, -1), ("NIFTY", 0.8, 1), ("BANKNIFTY", 0.3, -1), ("BANKNIFTY", 0.7, 1)]
    ):
        row = {field: "" for field in CALIBRATION_FIELDS}
        row.update({
            "trade_id": f"T{i}", "instrument": inst, "timeframe": "5min",
            "regime": "TRENDING_UP", "confidence": conf, "pnl": pnl,
        })
        rows.append(row)
    store.append_rows(rows)
    confidence_calibration._CACHE.clear()
    advisory = SimpleNamespace(
        ut_confidence_calibration_enabled=True,
        ut_confidence_calibration_live_apply_enabled=False,
        ut_confidence_calibration_min_samples=4,
        ut_confidence_calibration_min_segment_samples=2,
    )
    assert confidence_calibration.calibrate(
        0.8, advisory, str(path), instrument="NIFTY", timeframe="5min", regime="TRENDING_UP"
    ) == 0.8
    promoted = SimpleNamespace(**{**advisory.__dict__, "ut_confidence_calibration_live_apply_enabled": True})
    assert confidence_calibration.calibrate(
        0.8, promoted, str(path), instrument="NIFTY", timeframe="5min", regime="TRENDING_UP"
    ) == 1.0
    assert confidence_calibration.calibrate(
        0.8, promoted, str(path), instrument="SENSEX", timeframe="5min", regime="TRENDING_UP"
    ) == 0.8


def test_replay_resolves_existing_broker_stop_before_bar_close_management(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "backtest_live_exits_enabled", True)
    processor = SignalProcessor.__new__(SignalProcessor)
    called = []

    def make_probe(*_args, **_kwargs):
        def evaluate(*_call_args):
            called.append(True)
            return "EARLY_LOSS_CUT", "late", 90.0, "UNKNOWN", [], []
        return evaluate

    processor._historical_live_management_probe = make_probe
    idx = pd.date_range("2026-09-08 10:00", periods=2, freq="1min")
    bars = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [101.0, 101.0], "low": [90.0, 90.0], "close": [92.0, 92.0]},
        index=idx,
    )
    out = processor._simulate_actual_option_exit(
        bars, idx[0], idx[-1] + pd.Timedelta(minutes=1), sl_price=95.0, rr=3.0,
        max_sl_pct=0.60, qty=65, tf15_exit_scanner=SimpleNamespace(),
        tf15_instrument="NIFTY", tf15_direction="LONG", tf15_option_type="CE",
        tf15_timeframe="5min",
    )
    assert out["reason"] == "SL HIT"
    assert called == []
