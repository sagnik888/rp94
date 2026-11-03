"""The comparison lab must preserve native lifecycles without inventing premiums."""

from types import SimpleNamespace

import pandas as pd
import pytest

import engine.engine_lab as lab_module
from engine.engine_lab import EngineLab, build_shadow_engine


def signal(action, ident="cycle", price=1000.0, minute=0):
    timestamp = pd.Timestamp("2026-10-23 10:00") + pd.Timedelta(minutes=minute)
    return SimpleNamespace(
        utema_action=action, utema_state={"lifecycle_id": ident},
        utema_event={"price": price, "observed_at": timestamp.isoformat()},
        utema_event_id=f"{ident}|{action}|{minute}", source_lane="UTEMA",
        signal_type="BUY", timestamp=timestamp, price=price, bar_index=1,
        source_engine="ut_ema", instrument="NIFTY", timeframe="5min",
    )


def result(*, side=1, entry=1000.0, exit_price=1001.0, alignment=None, ident="cycle"):
    record = dict(lifecycle_id=ident, side=side, entry_spot=entry,
                  entry_time="2026-10-23T10:00:00+05:30", initial_stop=entry-side*2,
                  exit_time="2026-10-23T10:10:00+05:30" if exit_price is not None else "",
                  exit_price=exit_price, exit_reason="EMA RSI-50")
    signals = [signal("ENTRY", ident, entry)]
    if alignment is not None:
        signals.extend([signal("ADD", ident, alignment, 5), signal("ADD", ident, alignment, 5)])
    if exit_price is not None:
        signals.append(signal("EXIT", ident, exit_price, 10))
    return dict(source_engine="ut_ema", state={"position": 0, "current_price": 1004.0},
                signals=signals, utema_records={ident: record})


def bare_lab():
    return EngineLab.__new__(EngineLab)


def candles():
    return pd.DataFrame(dict(open=[1000, 1001, 1002], high=[1002, 1003, 1004],
                             low=[999, 1000, 1001], close=[1001, 1002, 1003]),
                        index=pd.date_range("2026-10-23 10:00", periods=3, freq="5min"), dtype=float)


def test_native_factory_ignores_global_filters_and_uses_synchronous_defaults():
    adapter = build_shadow_engine("ut_ema", dict(key_value=9, atr_period=99,
        adx_filter=True, session_filter=True, signal_mode="confirmed"))
    assert adapter.synchronous is True
    assert adapter._native.params.ut_factor == 1
    assert adapter._native.params.base_stop_pct == .2
    assert adapter._native.params.ut_target_exit == "T2"
    assert adapter._native.params.ema_target_exit == "T2"


def test_native_first_exit_is_authoritative_without_generic_1_5r_target(monkeypatch):
    lab = bare_lab()
    monkeypatch.setattr(lab, "_simulate_trade_path", lambda *a, **k: pytest.fail("generic exit used"))
    summary = lab._utema_scorecard({("NIFTY", "5min"): result(exit_price=1001)}, {}, "now")
    assert summary["closed"] == 1
    assert summary["closed_points"] == 1
    assert summary["evaluation_basis"] == "lot-weighted index points; closed-bar native replay"
    assert summary["option_target_evaluated"] is False


def test_shared_alignment_adds_exactly_one_lot_and_uses_observed_add_price():
    summary = bare_lab()._utema_scorecard(
        {("NIFTY", "5min"): result(exit_price=1005, alignment=1003)}, {"NIFTY": 2}, "now")
    assert summary["closed"] == 1
    assert summary["alignment_lots"] == 1
    assert summary["closed_points"] == 12  # 2*(1005-1000) + (1005-1003)


def test_timeframes_and_directions_remain_independent():
    summary = bare_lab()._utema_scorecard({
        ("NIFTY", "5min"): result(exit_price=1001),
        ("NIFTY", "15min"): result(side=-1, exit_price=995, ident="other"),
    }, {}, "now")
    assert summary["closed"] == 2
    assert summary["wins"] == 2
    assert summary["closed_points"] == 6


def test_open_alignment_is_one_shared_row_with_weighted_entry():
    summary = bare_lab()._utema_scorecard(
        {("NIFTY", "5min"): result(exit_price=None, alignment=1002)}, {}, "now")
    assert summary["open"] == 1
    assert summary["open_points"] == 6
    assert summary["open_rows"][0]["entry"] == 1001
    assert summary["open_rows"][0]["lots"] == 2
    assert summary["open_rows"][0]["source_lane"] == "UTEMA"


def test_process_counts_entries_only_and_never_calls_generic_trade_policy(monkeypatch):
    monkeypatch.setattr(lab_module, "ENGINE_LAB_MODES", ("ut_ema",))
    fake = SimpleNamespace(signal_mode="realtime", process=lambda **k: result(alignment=1000.5))
    monkeypatch.setattr(lab_module, "build_shadow_engine", lambda *a: fake)
    lab = EngineLab()
    monkeypatch.setattr(lab, "_open_or_reverse", lambda *a, **k: pytest.fail("generic entry"))
    monkeypatch.setattr(lab, "_mark_open_trade", lambda *a, **k: pytest.fail("generic mark"))
    monkeypatch.setattr(lab, "_simulate_trade_path", lambda *a, **k: pytest.fail("generic path"))
    snapshot = lab.process(candles_by_instrument={"NIFTY": {"5min": candles()}},
                           active_engine="ut_ema")
    row = snapshot["rows"][0]
    assert row["active"] is True
    assert row["signals"] == 1
    assert row["closed"] == 1
    assert row["option_target_evaluated"] is False
    assert row["errors"] == 0
    assert not any(key.endswith("|1min") for key in lab._engines)


def test_signal_identity_keeps_same_bar_native_entries_distinct():
    first, second = signal("ENTRY"), signal("ENTRY")
    second.utema_event_id += "|next"
    assert EngineLab._signal_key("ut_ema", "NIFTY", "5min", first) != EngineLab._signal_key("ut_ema", "NIFTY", "5min", second)
    assert EngineLab._source_lane(first, "ut_ema") == "UTEMA"


def test_historical_prefix_correction_invalidates_lab_native_cache(monkeypatch):
    monkeypatch.setattr(lab_module, "ENGINE_LAB_MODES", ("ut_ema",))
    calls = []
    def process(**kwargs):
        calls.append(kwargs["df"].copy())
        return result()
    fake = SimpleNamespace(signal_mode="realtime", process=process)
    monkeypatch.setattr(lab_module, "build_shadow_engine", lambda *a: fake)
    lab = EngineLab()
    frame = candles()
    lab.process(candles_by_instrument={"NIFTY": {"5min": frame}})
    lab.process(candles_by_instrument={"NIFTY": {"5min": frame}})
    changed = frame.copy()
    changed.iloc[0, changed.columns.get_loc("close")] += .5
    lab.process(candles_by_instrument={"NIFTY": {"5min": changed}})
    assert len(calls) == 2
    assert all(call is not frame for call in calls)


def test_bar_close_boundary_invalidates_unchanged_native_frame(monkeypatch):
    import engine.ut_ema_engine as native

    monkeypatch.setattr(lab_module, "ENGINE_LAB_MODES", ("ut_ema",))
    calls = []
    def process(**kwargs):
        calls.append(kwargs["as_of"])
        return result()
    fake = SimpleNamespace(signal_mode="realtime", process=process)
    monkeypatch.setattr(lab_module, "build_shadow_engine", lambda *a: fake)
    original_stamp = native.market_stamp
    clock = [original_stamp("2026-10-23 10:12")]
    monkeypatch.setattr(native, "market_stamp", lambda value=None: clock[0] if value is None else original_stamp(value))
    lab = EngineLab()
    frame = candles()
    lab.process(candles_by_instrument={"NIFTY": {"5min": frame}})
    lab.process(candles_by_instrument={"NIFTY": {"5min": frame}})
    clock[0] = original_stamp("2026-10-23 10:15")
    lab.process(candles_by_instrument={"NIFTY": {"5min": frame}})
    assert len(calls) == 2
