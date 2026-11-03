"""A pre-armed Auto Mode must not deadlock the switch to REAL (live, 2026-09-17).

08:31 main.py started in HISTORICAL. 08:51 the operator enabled Auto Mode (still
HISTORICAL). 09:02:08 and 09:02:25 they asked for REAL and both requests were refused:

    REAL auto-execution blocked by safe mode: morning readiness unavailable for 2026-09-17

The mode stayed HISTORICAL. The morning refresh that produces that readiness is only
scheduled by the scan loop once the process IS in REAL, so the pre-armed Auto blocked
REAL and REAL was the only thing that could build what Auto needed. The previous day the
operator switched with Auto OFF at 09:02:48, the refresh completed at 09:03:26 and Auto
was enabled later without trouble.

The fix keeps the Auto gate and removes only the deadlock: when the ONLY auto-execution
blockers are today's morning-readiness blockers, the switch to REAL completes with Auto
forced OFF (manual). The operator is told to re-enable Auto after the refresh completes.
Nothing re-arms Auto on its own. Every other blocker still refuses the whole request.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import config.settings as settings_module
import scanner as scanner_module
from scanner import Scanner

MORNING_MISSING = "morning readiness unavailable for 2026-09-17"
MORNING_PARTIAL = "morning readiness incomplete for 2026-09-17: chains, chain:NIFTY"


class _RealBroker:
    def __init__(self, established=True, acquired=True):
        self._established = established
        self._acquired = acquired

    def execution_lease_established(self):
        return self._established

    def acquire_execution_lease(self):
        return self._acquired

    def execution_lease_block_reason(self):
        return "owned by another process"


class _Trades:
    def __init__(self, real_broker, auto_mode):
        self.real_broker = real_broker
        self.mock_broker = object()
        self.broker = self.mock_broker
        self.mode = "HISTORICAL"
        self.auto_mode = auto_mode

    def update_risk_settings(self, *args, **kwargs):
        return None

    def has_live_exposure(self):
        return False


def _settings(auto_mode):
    return SimpleNamespace(
        production_safe_mode_required=True,
        active_indices=["NIFTY"],
        auto_mode=auto_mode,
        trading_mode="HISTORICAL",
        capital_total=500_000.0,
        capital_fut=250_000.0,
        capital_opt=100_000.0,
        risk_fut_pct=2.0,
        risk_opt_pct=8.0,
        fut_cost=200.0,
        opt_cost=80.0,
        futures_sl_pct=0.3,
        options_sl_pct=15.0,
        max_trade_loss_abs=0.0,
        circuit_breaker_slippage_bps=10.0,
        purge_intel_cache_on_mode_change=False,
        auto_real_mode_startup_enabled=False,
        real_mode_premarket_arm_start="09:00:00",
        real_mode_premarket_settle_end="09:08:00",
        real_mode_premarket_refresh_end="09:14:30",
        real_mode_live_start="09:15:00",
        real_mode_live_end="15:30:00",
        morning_refresh_catchup_at_live_open=True,
        morning_refresh_max_attempts=3,
    )


@pytest.fixture
def harness(monkeypatch):
    """Build a Scanner shell that can run configure() end to end without I/O."""

    def build(
        *,
        mode="HISTORICAL",
        auto_mode=True,
        morning=(MORNING_MISSING,),
        resource=(),
        base_blockers=(),
        chain_blockers=(),
        broker=None,
    ):
        settings = _settings(auto_mode)
        monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
        monkeypatch.setattr(scanner_module, "get_settings", lambda: settings)

        state = {
            "morning": list(morning),
            "resource": list(resource),
            "base": list(base_blockers),
            "chain": list(chain_blockers),
        }
        validations = []

        def validate(staged, provider, **kwargs):
            validations.append(dict(kwargs))
            key = "chain" if kwargs.get("require_option_chains") else "base"
            blockers = list(state[key])
            return {"ok": not blockers, "blockers": blockers, "warnings": []}

        monkeypatch.setattr(scanner_module, "validate_real_mode_readiness", validate)

        notes = []
        events = []
        sc = Scanner.__new__(Scanner)
        sc.mode = mode
        sc.auto_mode = auto_mode
        sc.trades = _Trades(broker or _RealBroker(), auto_mode)
        sc.trades.mode = mode
        if mode == "REAL":
            sc.trades.broker = sc.trades.real_broker
        sc.data = SimpleNamespace(
            is_connected=True,
            is_special_trading_session=lambda dt: False,
            is_market_holiday=lambda dt: False,
            is_market_open=lambda dt=None: False,
        )
        sc.risk_manager = SimpleNamespace(update_settings=lambda **kwargs: None)
        sc.on_notification = lambda message, kind: notes.append((message, kind))
        sc.log_event = lambda message, type="info": events.append((message, type))
        sc.active_indices = ["NIFTY"]
        sc.capital_total = 500_000.0
        sc.capital_fut = 250_000.0
        sc.capital_opt = 100_000.0
        sc.risk_fut_pct = 2.0
        sc.risk_opt_pct = 8.0
        sc.futures_sl_pct = 0.3
        sc.options_sl_pct = 15.0
        sc.max_daily_loss_pct = 3.0
        sc._hist_candidates_loaded = True
        sc._purge_intel_regime_caches = lambda **kwargs: None
        sc._purge_session_contamination = lambda **kwargs: None
        sc._surface_readiness_warnings = lambda *args, **kwargs: None
        sc._set_candle_history_capacity = lambda: None
        sc._auto_execution_morning_readiness_blockers = (
            lambda now_ist=None, *, active_indices=None: list(state["morning"])
        )
        sc._auto_execution_resource_blockers = lambda: list(state["resource"])
        sc.perf = SimpleNamespace(reset=lambda: None)
        sc._cached_results = {}
        sc.mtf = SimpleNamespace()
        sc._load_session_trade_candidates = lambda: None
        sc._next_simulation_id = lambda: None
        sc._stamp_simulation_diagnostics = lambda: None
        sc.latest_results = {}
        sc.get_broadcast_config = lambda: {"auto_mode": sc.auto_mode}
        sc._build_dashboard_trade_payload = lambda: {}
        sc._update_dashboard_cache = lambda payload: None
        sc._publish_config_snapshot = lambda: None
        sc.scan_count = 7
        sc._morning_refresh_last_date = None
        sc._morning_refresh_inflight = False
        sc._morning_refresh_summary = {}
        sc._morning_refresh_attempts = {}
        sc._morning_refresh_catchup_done_date = None
        return SimpleNamespace(
            scanner=sc,
            settings=settings,
            state=state,
            notes=notes,
            events=events,
            validations=validations,
        )

    return build


def _switch_to_real(sc, **extra):
    """The dashboard set_mode command: mode + confirmation, Auto not in the message."""
    return sc.configure(
        mode="REAL",
        confirm_real_mode=True,
        real_mode_verification="YES",
        persist_settings=False,
        **extra,
    )


def _assert_armed_manual(h, configured):
    sc = h.scanner
    assert configured is True, (
        "the switch to REAL was refused only because today's morning refresh has not run "
        f"yet, and that refresh only runs in REAL: {sc._last_configure_block_reason}"
    )
    assert sc.mode == "REAL"
    assert sc.trades.mode == "REAL"
    assert h.settings.trading_mode == "REAL"
    assert sc.trades.broker is sc.trades.real_broker
    # Live-money authority is NOT carried into REAL on a missing warmup.
    assert sc.auto_mode is False
    assert sc.trades.auto_mode is False
    assert h.settings.auto_mode is False


def test_prearmed_auto_with_only_missing_morning_readiness_switches_real_manual(harness):
    """The 2026-09-17 09:02 request, replayed."""
    h = harness(morning=(MORNING_MISSING,))

    configured = _switch_to_real(h.scanner)

    _assert_armed_manual(h, configured)
    held = str(getattr(h.scanner, "_auto_mode_held_reason", "") or "")
    assert "morning readiness unavailable for 2026-09-17" in held
    assert "held pending morning readiness" in h.scanner._last_configure_block_reason
    warnings = [message for message, kind in h.notes if kind == "warning"]
    assert warnings, f"operator was not told Auto was held: {h.notes}"
    assert "MANUAL" in warnings[-1].upper()
    assert "Morning refresh COMPLETE" in warnings[-1]
    assert not [message for message, kind in h.notes if kind == "error"]


def test_incomplete_morning_readiness_is_also_held_not_refused(harness):
    h = harness(morning=(MORNING_PARTIAL,))

    configured = _switch_to_real(h.scanner)

    _assert_armed_manual(h, configured)
    assert "chain:NIFTY" in h.scanner._auto_mode_held_reason


def test_explicit_batched_real_auto_request_is_downgraded_to_manual(harness):
    """Asking for REAL+Auto in one confirmed request gets REAL manual, never Auto."""
    h = harness(auto_mode=False, morning=(MORNING_MISSING,))

    configured = _switch_to_real(h.scanner, auto_mode=True)

    _assert_armed_manual(h, configured)


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"resource": ("resource guard: memory pressure 93%",)}, "resource guard"),
        ({"chain_blockers": ("NIFTY option chain stale",)}, "option chain stale"),
        ({"morning": (), "chain_blockers": ("Kite quote feed stale",)}, "quote feed stale"),
    ],
)
def test_any_other_auto_blocker_still_refuses_the_whole_switch(harness, overrides, expected):
    h = harness(**overrides)

    configured = _switch_to_real(h.scanner)

    assert configured is False
    assert h.scanner.mode == "HISTORICAL"
    assert h.scanner.trades.mode == "HISTORICAL"
    assert h.settings.trading_mode == "HISTORICAL"
    assert h.scanner.trades.broker is h.scanner.trades.mock_broker
    assert h.scanner.auto_mode is True  # untouched, exactly as before
    assert expected in h.scanner._last_configure_block_reason
    assert not getattr(h.scanner, "_auto_mode_held_reason", "")
    assert any(kind == "error" for _, kind in h.notes)


def test_plain_real_readiness_gate_is_unchanged(harness):
    """A failed broker/feed gate refuses before the Auto gate is even consulted."""
    h = harness(base_blockers=("Kite session missing",), morning=(MORNING_MISSING,))

    configured = _switch_to_real(h.scanner)

    assert configured is False
    assert h.scanner.mode == "HISTORICAL"
    assert h.scanner._last_configure_block_reason == "Kite session missing"
    assert [v.get("require_option_chains") for v in h.validations] == [None]


def test_real_confirmation_is_still_required(harness):
    h = harness(morning=(MORNING_MISSING,))

    configured = h.scanner.configure(mode="REAL", persist_settings=False)

    assert configured is False
    assert h.scanner.mode == "HISTORICAL"
    assert h.scanner.auto_mode is True
    assert h.scanner._last_configure_block_reason == "explicit REAL confirmation required"


def test_batched_real_auto_without_auto_confirmation_is_still_refused(harness):
    h = harness(auto_mode=False, morning=(MORNING_MISSING,))

    configured = h.scanner.configure(
        mode="REAL",
        auto_mode=True,
        real_mode_verification="YES",
        persist_settings=False,
    )

    assert configured is False
    assert h.scanner.mode == "HISTORICAL"
    assert h.scanner.auto_mode is False
    assert (
        h.scanner._last_configure_block_reason
        == "explicit REAL auto-execution confirmation required"
    )


def test_a_refused_lease_after_the_hold_does_not_announce_real_manual(harness):
    h = harness(morning=(MORNING_MISSING,), broker=_RealBroker(acquired=False))

    configured = _switch_to_real(h.scanner)

    assert configured is False
    assert h.scanner.mode == "HISTORICAL"
    assert h.settings.trading_mode == "HISTORICAL"
    assert not [message for message, kind in h.notes if "MANUAL" in message.upper()]


def test_auto_toggle_while_real_still_refuses_and_refresh_is_scheduled_then_retry_arms(
    harness,
):
    """After the held switch: Auto stays refused until the warmup exists, the scan loop
    schedules that warmup in REAL, and the operator's retry then succeeds. Nothing arms
    Auto on its own in between."""
    h = harness(morning=(MORNING_MISSING,))
    sc = h.scanner
    _assert_armed_manual(h, _switch_to_real(sc))

    # Operator toggles Auto before the refresh has completed: refused, as it should be.
    toggled = sc.configure(
        auto_mode=True,
        confirm_real_mode=True,
        real_mode_verification="YES",
        persist_settings=False,
    )
    assert toggled is False
    assert sc.mode == "REAL"
    assert sc.auto_mode is False
    assert "morning readiness unavailable" in sc._last_configure_block_reason

    # The scan loop in REAL (not warmup) schedules today's morning refresh.
    scheduled = []
    sc._schedule_morning_market_refresh = (
        lambda indices, now, *, reason="scheduled", force_full=False: scheduled.append(
            (reason, force_full)
        )
    )
    holding = sc._ensure_market_session_refresh({"NIFTY": {}}, datetime(2026, 9, 17, 9, 2, 30))
    assert holding is True
    assert scheduled and scheduled[0][1] is True

    # Refresh completes; the scan loop keeps running; Auto is still OFF by itself.
    sc._morning_refresh_last_date = "2026-09-17"
    sc._morning_refresh_summary = {"session_day": "2026-09-17", "complete": True}
    h.state["morning"] = []
    assert sc._ensure_market_session_refresh({"NIFTY": {}}, datetime(2026, 9, 17, 9, 20)) is False
    assert sc.auto_mode is False

    retried = sc.configure(
        auto_mode=True,
        confirm_real_mode=True,
        real_mode_verification="YES",
        persist_settings=False,
    )
    assert retried is True
    assert sc.auto_mode is True
    assert sc.trades.auto_mode is True
    assert not sc._auto_mode_held_reason


def test_dashboard_ack_reports_the_scanners_actual_mode(harness):
    """The mode selector and Auto switch are synced from the scanner, not the request."""
    from dashboard.server import _configuration_result_payload

    h = harness(morning=(MORNING_MISSING,))
    configured = _switch_to_real(h.scanner)

    payload = _configuration_result_payload(configured, h.scanner, "REAL")

    assert payload["status"] == "ok"
    assert payload["mode"] == "REAL"
    assert h.scanner.get_broadcast_config()["auto_mode"] is False
