"""Only a live broker fill may calibrate the execution-cost model.

SlippageTracker.record takes "one real fill", and trade_accounting lets the
measured EWMA REPLACE the static per-leg assumption. So a sample that is not a
broker fill does not add noise -- it overrides the model with itself.

The 2026-08-11 guard required two things: the trade is not a ghost, and the leg
has a broker order id. By 2026-08-15 that had not held. The store had grown to
14,244 FUT samples -- 2,193 added AFTER the guard -- still at ewma_frac exactly
0.0, still refreshing at 00:55 with the market closed and no order in the log,
while all 103 persisted trades were OPTIONS. Nothing that trades options can
measure futures execution.

Refreshing is what made it dangerous. The age bound added with the guard exists
so stale rows cannot outrank the static model, and rows that keep updating defeat
it: calibrated_fraction("FUT") returned 0.0, clamped to the 0.2bp floor of
_FUT_CALIBRATION_BOUNDS, standing in for the 2.0bp static assumption -- futures
priced ten times cheaper than configured.

The persisted history says why plainly: provenance across those 103 trades is 76
blank, 16 HISTORICAL, 11 BACKTEST, and not one LIVE. No live fill has ever been
recorded, so a sample must also come from a LIVE entry and must not be a futures
shadow companion.

Also pinned here: an entry fill exactly equal to its reference no longer feeds the
EWMA. Every one of the 14,244 poisoned samples was exactly that, and the exit leg
had always discarded it.
"""
import pytest

from trading.trade_manager import TradeManager


class _T:
    def __init__(self, **kw):
        self.is_ghost = False
        self.trading_symbol = "NIFTY26AUG24300CE"
        self.data_provenance = "LIVE"
        self.broker_order_id = "250814000123456"
        self.exit_order_id = "250814000123999"
        self.inst_type = "OPT"
        self.instrument = "NIFTY"
        self.direction = "LONG"
        self.signal_entry_price = 100.0
        self.entry_slippage = 0.0
        self.exit_slippage = 0.0
        for k, v in kw.items():
            setattr(self, k, v)


_real = TradeManager._slippage_sample_is_a_real_fill


# ── what still counts ────────────────────────────────────────────────────────

def test_a_live_option_fill_with_an_order_id_counts():
    assert _real(_T(), "ENTRY") is True
    assert _real(_T(), "EXIT") is True


# ── what the 2026-08-11 guard already caught ─────────────────────────────────

def test_a_ghost_never_counts():
    assert _real(_T(is_ghost=True), "ENTRY") is False


@pytest.mark.parametrize("missing", ["", None, "   "])
def test_a_leg_without_a_broker_order_id_never_counts(missing):
    assert _real(_T(broker_order_id=missing), "ENTRY") is False
    assert _real(_T(exit_order_id=missing), "EXIT") is False


# ── what it did not catch ────────────────────────────────────────────────────

@pytest.mark.parametrize("provenance", ["HISTORICAL", "BACKTEST", "RECOVERY", "EOD", "", None])
def test_only_a_live_entry_may_calibrate(provenance):
    """76 blank + 16 HISTORICAL + 11 BACKTEST, 0 LIVE -- the whole stored history."""
    assert _real(_T(data_provenance=provenance), "ENTRY") is False


def test_a_recovery_fill_is_reconstructed_not_observed():
    """Recovery re-establishes an existing position; its price was never executed."""
    assert _real(_T(data_provenance="RECOVERY"), "ENTRY") is False


def test_a_futures_shadow_companion_never_counts():
    """FUT_SHADOW is simulated by construction -- the shape of the poisoned rows."""
    assert _real(_T(trading_symbol="FUT_SHADOW", inst_type="FUT"), "ENTRY") is False
    assert _real(_T(trading_symbol="fut_shadow", inst_type="FUT"), "ENTRY") is False


def test_provenance_is_checked_case_insensitively():
    assert _real(_T(data_provenance="live"), "ENTRY") is True


# ── a fill equal to its reference is not evidence ────────────────────────────

def _record(monkeypatch, trade, fill):
    seen = []

    class _Tracker:
        def record(self, **kw):
            seen.append(kw)

    import engine.slippage_tracker as st

    monkeypatch.setattr(st, "get_slippage_tracker", lambda: _Tracker())
    TradeManager._record_entry_slippage(trade, fill)
    return seen


def test_a_genuine_zero_from_a_real_fill_is_still_a_measurement(monkeypatch):
    """Zero was the poisoned samples' symptom, not the defect -- they were simulated.

    Filtering zeros here would bias the EWMA toward adverse fills and discard real
    execution data. The provenance guard excludes the simulated ones at the source,
    which is the precise fix; this pins that the blunt one was not taken.
    """
    seen = _record(monkeypatch, _T(), 100.0)

    assert len(seen) == 1
    assert seen[0]["adverse_points"] == pytest.approx(0.0)


def test_a_real_adverse_entry_fill_still_calibrates(monkeypatch):
    seen = _record(monkeypatch, _T(), 100.9)

    assert len(seen) == 1
    assert seen[0]["leg"] == "ENTRY"
    assert seen[0]["adverse_points"] == pytest.approx(0.9)


def test_a_price_improvement_still_calibrates(monkeypatch):
    """Negative slippage is real information about execution and must be kept."""
    seen = _record(monkeypatch, _T(), 99.2)

    assert len(seen) == 1
    assert seen[0]["adverse_points"] == pytest.approx(-0.8)


def test_the_zero_is_still_reported_on_the_trade(monkeypatch):
    """Excluded from the EWMA only -- entry_slippage is reporting and keeps the zero."""
    trade = _T()
    _record(monkeypatch, trade, 100.0)

    assert trade.entry_slippage == 0.0


def test_a_backtest_fill_does_not_calibrate_even_when_adverse(monkeypatch):
    assert _record(monkeypatch, _T(data_provenance="BACKTEST"), 100.9) == []


# ── the measurement is durable ───────────────────────────────────────────────

def test_slippage_is_persisted_so_options_can_be_tuned():
    """Both fields round-trip through to_dict/from_dict but were dropped at save,
    leaving no stored history of real option fills to calibrate against."""
    import inspect

    import data.sqlite_db as sqlite_db

    src = inspect.getsource(sqlite_db)
    assert '"entry_slippage", "exit_slippage"' in src, "slippage not in the persisted fields"
    assert '"entry_slippage": "REAL"' in src, "no column migration for entry_slippage"
    assert '"exit_slippage": "REAL"' in src, "no column migration for exit_slippage"
