"""Two checks that reported confidently about things they had not measured.

1. THE LOT-SIZE CROSS-CHECK FAILED OPEN AND SILENTLY.
   `lot_size_mismatches` calls `option_lot_size(name)`, which returns None when the
   Kite contract master holds no CE/PE rows for that index. The comparison is
   `if mine > 0 and actual and actual != mine` -- a None `actual` is falsy, so the
   index is skipped without a word. The function therefore returned [] both when all
   four indices verified clean AND when nothing was checked at all, and readiness
   read [] as "clean".

   That is reachable, not theoretical: load_instruments keeps the previous cache on
   ANY exchange failure, and the checker's provider requests four exchanges
   (NFO/BFO/NSE/BSE) where contract resolution requests two (NFO/BFO) and falls back
   to an on-disk cache. So the checker's master goes empty in cases where option
   trading keeps working normally.

   Mitigating, and worth stating plainly: config/instruments.json currently holds
   65/30/20/120, which ARE the correct lot sizes, so nothing is mis-sized today. This
   is a tripwire that would not fire, not an active mis-size.

2. PUBLISHED SCAN LATENCY MEASURED A PREFIX OF THE CYCLE.
   `elapsed = time.time() - t0` is taken right after _update_active_trades, but the
   cycle then builds the diagnostics payload, strips charts per instrument, builds the
   trade payload, writes the dashboard cache, writes warm memory to DISK and finally
   awaits the websocket broadcast. None of that was counted -- so the high-latency and
   freeze alarms were judged on a number that structurally excluded the work a freeze
   is most likely to be stuck in.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

INDICES = ["NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]
CONFIGURED = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "MIDCPNIFTY": 120}


# ── 1. the cross-check distinguishes clean from unchecked ────────────────────

def _source(sizes):
    """A KiteDataSource stub whose option_lot_size answers from `sizes`."""
    from data.kite_market_data import KiteDataSource

    src = KiteDataSource.__new__(KiteDataSource)
    src.option_lot_size = lambda name: sizes.get(str(name).upper())
    return src


def test_an_empty_contract_master_is_reported_as_unverified():
    """The headline: nothing checked must not read as everything clean."""
    src = _source({})
    assert src.lot_size_mismatches(CONFIGURED) == []
    assert src.lot_size_unresolved(CONFIGURED) == sorted(CONFIGURED)


def test_a_fully_verified_master_reports_nothing_unresolved():
    src = _source(dict(CONFIGURED))
    assert src.lot_size_mismatches(CONFIGURED) == []
    assert src.lot_size_unresolved(CONFIGURED) == []


def test_the_two_states_are_actually_distinguishable():
    """Both return [] from lot_size_mismatches; only the new call separates them."""
    blind, clean = _source({}), _source(dict(CONFIGURED))
    assert blind.lot_size_mismatches(CONFIGURED) == clean.lot_size_mismatches(CONFIGURED)
    assert blind.lot_size_unresolved(CONFIGURED) != clean.lot_size_unresolved(CONFIGURED)


@pytest.mark.parametrize("index", INDICES)
def test_one_missing_index_is_named_on_its_own(index):
    """Four indices trade separately; a partial master must name the gap."""
    sizes = {k: v for k, v in CONFIGURED.items() if k != index}
    assert _source(sizes).lot_size_unresolved(CONFIGURED) == [index]


def test_a_real_mismatch_is_still_a_blocker_not_a_warning():
    src = _source({**CONFIGURED, "NIFTY": 75})
    assert src.lot_size_mismatches(CONFIGURED) == [("NIFTY", 65, 75)]
    assert "NIFTY" not in src.lot_size_unresolved(CONFIGURED)


def test_a_zero_configured_size_is_not_reported_as_unresolved():
    assert _source({}).lot_size_unresolved({"NIFTY": 0}) == []


def test_junk_configuration_does_not_raise():
    assert _source({}).lot_size_unresolved({"NIFTY": "x", "BANKNIFTY": None}) == []


def test_readiness_warns_when_the_check_could_not_run():
    import engine.safe_mode as sm

    src = inspect.getsource(sm)
    assert "lot_size_unresolved" in src, (
        "readiness still cannot tell a verified lot size from an unverifiable one"
    )
    marker = "lot_size_unresolved"
    window = src[src.index(marker): src.index(marker) + 700]
    assert "warnings.append" in window, "the unverified state is computed but discarded"


def test_the_unverified_state_is_a_warning_not_a_blocker():
    """A blank contract master must not stop trading — today's sizes are correct."""
    import engine.safe_mode as sm

    src = inspect.getsource(sm)
    window = src[src.index("lot_size_unresolved"): src.index("lot_size_unresolved") + 700]
    assert "blockers.append" not in window


# ── 2. latency counts the whole cycle ────────────────────────────────────────

def _scan_source():
    from scanner import Scanner

    return textwrap.dedent(inspect.getsource(Scanner._scan_cycle))


def test_the_cycle_duration_is_measured_after_the_broadcast():
    src = _scan_source()
    assert "cycle_ms = round((time.time() - t0) * 1000)" in src
    assert src.index("await self.on_update(results)") < src.index("cycle_ms = round("), (
        "cycle_ms is still computed before the broadcast it is meant to include"
    )


def test_the_freeze_alarm_reads_the_whole_cycle():
    src = _scan_source()
    assert "if cycle_ms > 30000" in src, "the freeze notice still judges a prefix"
    assert "if latency_ms > 30000" not in src


def test_the_high_latency_alarm_reads_the_whole_cycle():
    src = _scan_source()
    assert "if cycle_ms > 10000" in src
    assert "if latency_ms > 10000" not in src


def test_the_scan_phase_metric_is_kept_too():
    """cycle_ms replaces nothing — the phase split is what makes it diagnosable."""
    src = _scan_source()
    assert "elapsed = time.time() - t0" in src
    assert "latency_ms = round(elapsed * 1000)" in src
    assert '"latency": latency_ms' in src


def test_both_numbers_are_published():
    src = _scan_source()
    assert '"cycle_ms": self._last_cycle_ms' in src
    assert '["last_cycle_ms"] = cycle_ms' in src


def test_the_published_cycle_is_the_previous_ones():
    """The payload is built before the broadcast, so this cycle's total is not yet
    known. Carrying the previous value is honest; recomputing it there would not be."""
    src = _scan_source()
    assert src.index('"cycle_ms": self._last_cycle_ms') < src.index("self._last_cycle_ms = cycle_ms")


def test_the_carrier_is_initialised():
    from scanner import Scanner

    src = inspect.getsource(Scanner.__init__)
    assert "self._last_cycle_ms = 0" in src


def test_the_telemetry_payload_carries_it_through():
    """The latency block spreads diagnostics['latency'], so last_cycle_ms rides along."""
    from engine.scanner_telemetry import ScannerTelemetryMixin

    src = inspect.getsource(ScannerTelemetryMixin)
    assert '**(diagnostics.get("latency", {}) or {})' in src


def test_the_measured_tail_is_not_trivial():
    """If everything between the two marks were cheap this fix would be pointless."""
    src = _scan_source()
    start = src.index("latency_ms = round(elapsed * 1000)")
    tail = src[start:src.index("cycle_ms = round(", start)]
    for expensive in ("_get_diagnostics_payload", "_build_dashboard_trade_payload",
                      "_update_dashboard_cache", "on_update"):
        assert expensive in tail, f"{expensive} is no longer inside the unmeasured tail"
    assert "_save_warm_memory" not in tail, (
        "periodic recovery persistence must not block the scanner event loop"
    )


def test_periodic_warm_memory_checkpoint_runs_off_scanner_loop():
    from scanner import Scanner

    src = inspect.getsource(Scanner._background_recovery_checkpoint_worker)
    assert "await asyncio.to_thread(self._save_warm_memory)" in src
