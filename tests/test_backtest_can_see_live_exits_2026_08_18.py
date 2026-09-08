"""The backtest could not see any exit logic, so no exit fix could ever move its number.

`SignalProcessor._simulate_actual_option_exit` walks the option candles for a stop or a
target and force-closes at the window end. Its complete vocabulary is:

    SL HIT · TARGET_HIT · HISTORICAL · SESSION_END

Every live guard — profit lock, peak-threat, loss-threat, early loss cut, breakeven stop
— lives in scanner.py and is never reached from that path. PEAK_GIVEBACK_CAP had not
fired in a single log line for exactly that reason: the fix was not in the code being
measured. Two builds compared on this harness are compared on entry selection plus a
fixed stop/target, and on nothing else.

This adds an opt-in toggle so the replay also evaluates the live guard. It CALLS
Scanner._peak_giveback_cap_exit rather than reimplementing it — one rule, one
implementation. A second copy would drift from the live one and the backtest would then
be confidently wrong instead of merely blind.

Default OFF: turning it on changes the numbers, so a run with it on is not comparable to
a run with it off.
"""
import inspect
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from config.settings import Settings, get_settings
from engine.signal_processor import SignalProcessor


def _bars(prices, start="2026-08-17 09:20"):
    """One-minute option candles from a close series."""
    idx = pd.date_range(start, periods=len(prices), freq="1min")
    return pd.DataFrame(
        {"open": prices, "high": prices, "low": prices, "close": prices}, index=idx
    )


def _run(prices, qty=0, **kw):
    df = _bars(prices)
    return SignalProcessor._simulate_actual_option_exit(
        SignalProcessor.__new__(SignalProcessor),
        df, df.index[0], df.index[-1],
        sl_price=float(prices[0]) * 0.60,     # far away, so it cannot interfere
        rr=99.0,                              # target unreachable
        natural_sl_distance=None, max_sl_pct=0.60, qty=qty, **kw
    )


# ── the gap this exists to close ─────────────────────────────────────────────

def test_the_simulator_alone_can_only_produce_stop_target_or_session_end():
    """The vocabulary limit that made every exit fix invisible."""
    src = inspect.getsource(SignalProcessor._simulate_actual_option_exit)
    produced = {
        line.split('exit_reason')[1].split('"')[1]
        for line in src.splitlines()
        if 'exit_reason = "' in line
    }
    assert produced <= {
        "HISTORICAL",
        "SL HIT",
        "TARGET_HIT",
        "PEAK_GIVEBACK_CAP",
        "TF15_FAST_REVERSAL",
    }
    # Nothing from the live trade-management stack is reachable without the toggle.
    for live_only in ("SMART_PROFIT_LOCK", "PEAK_THREAT_EXIT", "LOSS_THREAT_EXIT",
                      "EARLY_LOSS_CUT", "PROFIT_GIVEBACK_GUARD"):
        assert live_only not in produced


def test_off_by_default():
    assert Settings.model_fields["backtest_live_exits_enabled"].default is False


def test_with_the_toggle_off_nothing_changes(monkeypatch):
    """A trade that peaks and round-trips to negative rides to the window end, exactly
    as it always did."""
    monkeypatch.setattr(get_settings(), "backtest_live_exits_enabled", False)
    out = _run([100, 130, 140, 120, 95, 90], qty=75)
    assert out["reason"] == "HISTORICAL"


# ── the toggle ───────────────────────────────────────────────────────────────

def test_with_the_toggle_on_the_live_guard_fires(monkeypatch):
    """Same candles, same stop, same target — now the breakeven stop catches the
    round-trip instead of riding it to the close."""
    settings = get_settings()
    monkeypatch.setattr(settings, "backtest_live_exits_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_arm_rs", 150.0)
    monkeypatch.setattr(settings, "breakeven_stop_floor_rs", 0.0)
    # Isolate the rule this test names. peak_trail ships ON from 2026-08-24 and a
    # Rs.3,000 peak clears its Rs.600 gate, so without this it is PEAK_TRAIL that
    # fires and the assertion below would be testing a different guard.
    monkeypatch.setattr(settings, "peak_trail_enabled", False)
    out = _run([100, 130, 140, 120, 95, 90], qty=75)   # peak +Rs.3000, back below entry
    # Named for the rule that fired, not for the method that hosts it. This assertion
    # used to read PEAK_GIVEBACK_CAP while the docstring above said "breakeven stop";
    # the docstring was the accurate one (2026-08-19).
    assert out["reason"] == "BREAKEVEN_STOP"
    assert out["exit"] < 100                            # cut on the way down


def test_a_winner_that_keeps_running_is_not_touched(monkeypatch):
    """The guard must not clip a trade that never comes back."""
    settings = get_settings()
    monkeypatch.setattr(settings, "backtest_live_exits_enabled", True)
    monkeypatch.setattr(settings, "breakeven_stop_enabled", True)
    out = _run([100, 120, 140, 160, 180], qty=75)
    assert out["reason"] == "HISTORICAL"


def test_zero_quantity_leaves_the_path_inert(monkeypatch):
    """qty is only needed to convert premium points into rupees. Without it the guard
    cannot be evaluated, so the simulation must degrade to its old behaviour rather than
    guess a size."""
    monkeypatch.setattr(get_settings(), "backtest_live_exits_enabled", True)
    out = _run([100, 130, 140, 120, 95, 90], qty=0)
    assert out["reason"] == "HISTORICAL"


# ── no second copy of the rule ───────────────────────────────────────────────

def test_the_guard_is_called_not_reimplemented():
    """The whole point. A duplicated rule drifts from the live one and then the backtest
    is confidently wrong rather than merely blind."""
    src = inspect.getsource(SignalProcessor._simulate_actual_option_exit)
    assert "_peak_giveback_cap_exit" in src, "the simulator does not call the live guard"
    # None of the guard's own arithmetic may be restated here.
    for owned_by_the_guard in ("breakeven_stop_arm_rs", "peak_giveback_cap_rs",
                               "peak_giveback_cap_pct", "peak_giveback_cap_max_peak_rs"):
        assert owned_by_the_guard not in src, f"{owned_by_the_guard} is duplicated here"


def test_a_guard_failure_cannot_break_a_replay(monkeypatch):
    """An optional probe must never take down a backtest."""
    import scanner as _s

    monkeypatch.setattr(get_settings(), "backtest_live_exits_enabled", True)
    monkeypatch.setattr(
        _s.Scanner, "_peak_giveback_cap_exit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = _run([100, 130, 140, 120, 95, 90], qty=75)
    assert out is not None and out["reason"] == "HISTORICAL"


def test_existing_callers_are_unaffected():
    """qty defaults to 0, so every current call site keeps the pure stop/target sim."""
    sig = inspect.signature(SignalProcessor._simulate_actual_option_exit)
    assert sig.parameters["qty"].default == 0


def test_opt_in_replay_calls_production_tf15_exit(monkeypatch):
    import scanner as scanner_module

    settings = get_settings()
    monkeypatch.setattr(settings, "backtest_live_exits_enabled", True)
    monkeypatch.setattr(settings, "fifteen_min_one_min_support_exit_enabled", True)
    monkeypatch.setattr(
        scanner_module.Scanner,
        "_peak_giveback_cap_exit",
        lambda *args, **kwargs: (False, ""),
    )
    processor = SignalProcessor.__new__(SignalProcessor)
    seen = []
    processor._historical_tf15_exit_probe = lambda *args, **kwargs: (
        lambda ts, close, high, peak: (seen.append((ts, close, high, peak)) or True, "fast")
    )
    df = _bars([100.0, 99.0, 98.0])

    out = processor._simulate_actual_option_exit(
        df,
        df.index[0],
        df.index[-1],
        sl_price=50.0,
        rr=99.0,
        max_sl_pct=0.60,
        tf15_qty=75,
        tf15_exit_scanner=SimpleNamespace(),
        tf15_instrument="NIFTY",
        tf15_direction="LONG",
        tf15_option_type="CE",
        tf15_timeframe="15min",
    )

    assert out["reason"] == "TF15_FAST_REVERSAL"
    assert seen


def test_tf15_replay_candle_view_never_exposes_future_rows(monkeypatch):
    import scanner as scanner_module
    from intelligence.regime_detector import RegimeDetector

    idx = pd.date_range("2026-08-17 09:15", periods=16, freq="1min")
    frame = pd.DataFrame(
        {
            "open": range(100, 116),
            "high": range(101, 117),
            "low": range(99, 115),
            "close": range(100, 116),
            "volume": [100] * 16,
        },
        index=idx,
    )

    class Source:
        @staticmethod
        def get_candles(_instrument, timeframe):
            return frame.copy() if timeframe == "1min" else None

    captured = {}

    def decision(scanner, trade, spot, latest_time, **kwargs):
        one = scanner.candles.get_candles("NIFTY", "1min")
        fifteen = scanner.candles.get_candles("NIFTY", "15min")
        captured["one_last"] = one.index[-1]
        captured["fifteen_close"] = float(fifteen.iloc[-1]["close"])
        captured["evaluation"] = latest_time
        captured["regime"] = scanner._get_trade_regime(trade)
        return False, ""

    monkeypatch.setattr(
        scanner_module.Scanner,
        "_fifteen_min_one_min_support_exit_decision",
        decision,
    )
    monkeypatch.setattr(
        RegimeDetector,
        "detect",
        lambda *args, **kwargs: {"regime": "TRENDING_DOWN"},
    )
    processor = SignalProcessor.__new__(SignalProcessor)
    probe = processor._historical_tf15_exit_probe(
        SimpleNamespace(candles=Source(), active_indices=["NIFTY"]),
        instrument="NIFTY",
        direction="LONG",
        option_type="CE",
        entry_time=idx[0].to_pydatetime(),
        entry_price=100.0,
        stop_price=90.0,
        qty=75,
    )

    probe(idx[2], 102.0, 103.0, 150.0)

    assert captured["one_last"] == idx[2]
    assert captured["fifteen_close"] == 102.0
    assert captured["evaluation"] == idx[2].to_pydatetime() + timedelta(minutes=1)
    assert captured["regime"] == "TRENDING_DOWN"


# ── the dashboard controls ───────────────────────────────────────────────────

@pytest.mark.parametrize("field,element", [
    ("breakeven_stop_enabled", "settBreakevenStop"),
    ("breakeven_stop_arm_rs", "settBreakevenStopArm"),
    ("breakeven_stop_floor_rs", "settBreakevenStopFloor"),
    ("peak_giveback_cap_enabled", "settPeakGivebackCap"),
    ("peak_giveback_cap_arm_rs", "settPeakGivebackCapArm"),
    ("peak_giveback_cap_rs", "settPeakGivebackCapRs"),
    ("peak_giveback_cap_pct", "settPeakGivebackCapPct"),
    ("peak_giveback_cap_max_peak_rs", "settPeakGivebackCapMaxPeak"),
    ("backtest_live_exits_enabled", "settBacktestLiveExits"),
])
def test_every_new_setting_is_reachable_from_the_dashboard(field, element):
    """FIVE wiring points are required, not three. Backend registration alone leaves the
    field invisible: these settings were registered on 2026-08-16 and still had no
    control, so the only way to change them was .env. The widget, its load and its save
    all live in the JS.
    """
    import pathlib

    js = pathlib.Path("dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    py = pathlib.Path("dashboard/server.py").read_text(encoding="utf-8")

    assert (f"makeSwitchCard('{element}'" in js or f"makeInputGroup('{element}'" in js), "no widget"
    assert (f"setChecked('{element}'" in js or f"setVal('{element}'" in js), "never loaded"
    assert (f"{field}: checkedVal('{element}'" in js
            or f"{field}: numberVal('{element}'" in js), "never saved"
    assert f'"{field}",' in py, "not in _ADVANCED_CONFIG_FIELDS"
    assert f'{field}=msg.get("{field}")' in py, "not forwarded by name"


def test_the_dashboard_defaults_match_the_settings_defaults():
    """A control that loads a different default than the backend silently rewrites the
    setting the first time anyone presses Save."""
    import pathlib
    import re

    js = pathlib.Path("dashboard/static/js/dashboard-settings.js").read_text(encoding="utf-8")
    for field, default in (
        ("breakeven_stop_arm_rs", 100.0),   # 150 -> 100, measured 2026-08-26
        ("breakeven_stop_floor_rs", 80.0),
        ("peak_giveback_cap_arm_rs", 100.0),
        ("peak_giveback_cap_rs", 200.0),
        ("peak_giveback_cap_pct", 0.25),
        ("peak_giveback_cap_max_peak_rs", 1500.0),
    ):
        assert Settings.model_fields[field].default == default
        # Parse the fallback out of the load line itself, e.g.
        #   setVal('settX', cfg.field !== undefined ? cfg.field : 150.0);
        # A looser "is 150.0 anywhere in the file" check passes on any file that
        # happens to mention the number, which asserts nothing.
        match = re.search(
            rf"cfg\.{re.escape(field)}\s*!==\s*undefined\s*\?\s*cfg\.{re.escape(field)}\s*:\s*([-0-9.]+)",
            js,
        )
        assert match, f"{field} has no parseable load fallback in the JS"
        assert float(match.group(1)) == default, (
            f"{field}: dashboard loads {match.group(1)} but the backend defaults to "
            f"{default} — the first Save would silently rewrite it"
        )


def test_the_settings_script_is_cache_busted_past_v24():
    """A control can be present in the JS, mounted in the DOM, and still invisible.

    On 2026-08-18 all nine new controls existed and rendered, but the running dashboard
    showed none of them: index.html still requested
        dashboard-settings.js?v=24-per-tf-grading
    so every browser served its cached copy. Changing the JS without bumping the query
    string ships nothing.
    """
    import pathlib
    import re

    html = pathlib.Path("dashboard/static/index.html").read_text(encoding="utf-8")
    match = re.search(r"dashboard-settings\.js\?v=(\d+)", html)
    assert match, "the settings script lost its cache-busting version"
    assert int(match.group(1)) >= 26, (
        "dashboard-settings.js changed but ?v= was not bumped — browsers will serve "
        "the cached copy and the new controls will not appear"
    )
